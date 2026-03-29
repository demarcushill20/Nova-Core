"""Starvation Prevention & Maintenance Guarantees for the Dynamic Block Scheduler.

Phase 9: Ensures long-deferred tasks eventually get attention through aging-
based priority boosts, starvation alerts, maintenance reservations, and work
mode balance monitoring.

Key capabilities:
  - Effective priority scoring with commitment-aware aging
  - Multi-tier starvation alerts (warning → escalation → auto_review)
  - Maintenance slot reservation within block plans
  - Work mode balance analysis from execution history
  - Deferment age recalculation from generated_at timestamps
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from utils.scheduler.block import BlockPlan, ScheduledSlot
from utils.scheduler.execution_log import ExecutionRecord
from utils.scheduler.scorer import ScoredTask
from utils.scheduler.work_unit import CommitmentLevel, WorkUnit

log = logging.getLogger("nova.scheduler.starvation")


# ---------------------------------------------------------------------------
# Aging config & effective priority
# ---------------------------------------------------------------------------


class AgingConfig(BaseModel):
    """Configuration for deferment aging and starvation thresholds."""

    model_config = ConfigDict(extra="forbid")

    rate_per_day: dict[str, float] = Field(
        default_factory=lambda: {"hard": 1.0, "soft": 0.5, "opportunistic": 0.2},
    )
    max_deferment_days_before_soft: int = Field(
        default=5,
        ge=1,
        description="After this many days, OPPORTUNISTIC tasks get minimum SOFT",
    )
    starvation_warning_days: int = Field(default=7, ge=1)
    starvation_escalation_days: int = Field(default=14, ge=1)
    starvation_auto_review_days: int = Field(default=21, ge=1)


_PRIORITY_BASE: dict[str, float] = {
    "high": 3.0,
    "medium": 2.0,
    "low": 1.0,
}


def compute_effective_priority(
    work_unit: WorkUnit,
    config: AgingConfig | None = None,
) -> tuple[float, WorkUnit]:
    """Compute aging-adjusted effective priority for a work unit.

    Returns (effective_priority, possibly_upgraded_work_unit).
    If the task has been deferred long enough, its commitment level may be
    upgraded from OPPORTUNISTIC to SOFT.
    """
    cfg = config or AgingConfig()

    base = _PRIORITY_BASE.get(work_unit.priority, 2.0)
    commitment_key = work_unit.commitment_level.value
    aging_rate = cfg.rate_per_day.get(commitment_key, 0.5)
    aging_bonus = work_unit.deferment_age_days * aging_rate

    effective = base + aging_bonus

    # Upgrade OPPORTUNISTIC → SOFT after prolonged deferment
    upgraded_unit = work_unit
    if (
        work_unit.deferment_age_days >= cfg.max_deferment_days_before_soft
        and work_unit.commitment_level == CommitmentLevel.OPPORTUNISTIC
    ):
        upgraded_unit = work_unit.model_copy(
            update={"commitment_level": CommitmentLevel.SOFT},
        )

    return effective, upgraded_unit


# ---------------------------------------------------------------------------
# Starvation checks
# ---------------------------------------------------------------------------


def check_starvation(
    work_units: list[WorkUnit],
    config: AgingConfig | None = None,
) -> list[dict]:
    """Check work units for starvation and return tiered alerts.

    Alert levels:
      - warning (7+ days): task is aging and needs attention
      - escalation (14+ days): task is critically delayed
      - auto_review (21+ days): task should be reviewed for relevance
    """
    cfg = config or AgingConfig()
    alerts: list[dict] = []

    for wu in work_units:
        age = wu.deferment_age_days
        task_id = wu.id or wu.title or "unknown"

        if age >= cfg.starvation_auto_review_days:
            alerts.append(
                {
                    "level": "auto_review",
                    "task_id": task_id,
                    "age_days": age,
                    "message": (f"Task '{task_id}' deferred {age:.0f} days — review for relevance"),
                    "action": "review_relevance",
                }
            )
        elif age >= cfg.starvation_escalation_days:
            alerts.append(
                {
                    "level": "escalation",
                    "task_id": task_id,
                    "age_days": age,
                    "message": (f"Task '{task_id}' deferred {age:.0f} days — escalation: critically delayed"),
                }
            )
        elif age >= cfg.starvation_warning_days:
            alerts.append(
                {
                    "level": "warning",
                    "task_id": task_id,
                    "age_days": age,
                    "message": (f"Task '{task_id}' deferred {age:.0f} days — needs attention"),
                }
            )

    return alerts


# ---------------------------------------------------------------------------
# Maintenance reservation
# ---------------------------------------------------------------------------


class MaintenanceConfig(BaseModel):
    """Configuration for maintenance slot reservation within block plans."""

    model_config = ConfigDict(extra="forbid")

    reservation_pct: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description="Fraction of block to reserve for maintenance",
    )
    maintenance_modes: list[str] = Field(
        default_factory=lambda: ["admin", "monitoring"],
        description="WorkModes that count as maintenance",
    )
    min_maintenance_min: float = Field(
        default=30.0,
        ge=0.0,
        description="Minimum maintenance minutes per block",
    )


def reserve_maintenance(
    block_plan: BlockPlan,
    maintenance_tasks: list[ScoredTask],
    config: MaintenanceConfig | None = None,
) -> BlockPlan:
    """Ensure a block plan meets its maintenance reservation quota.

    Calculates the required maintenance minutes, checks how much is already
    scheduled, and adds maintenance tasks from the provided list to fill any
    shortfall.  Never evicts committed tasks — only uses remaining filler/buffer
    capacity.

    Returns a new BlockPlan (does not mutate the original).
    """
    cfg = config or MaintenanceConfig()
    maint_modes = set(cfg.maintenance_modes)

    # Calculate target reservation
    target = max(
        block_plan.block_duration_min * cfg.reservation_pct,
        cfg.min_maintenance_min,
    )

    # How much maintenance is already scheduled?
    existing_maint_min = 0.0
    for slot in block_plan.scheduled_slots:
        if slot.scored_task.work_unit.work_mode.value in maint_modes:
            duration = slot.end_offset_min - slot.start_offset_min - slot.switch_cost_min
            existing_maint_min += max(0.0, duration)

    shortfall = target - existing_maint_min
    if shortfall <= 0.0:
        # Already meets quota — return unchanged
        return block_plan

    if not maintenance_tasks:
        # No tasks available to fill the gap
        return block_plan

    # Determine available capacity (filler + buffer minus what's used)
    # We only add into remaining filler/buffer — never evict committed
    used_total = 0.0
    for slot in block_plan.scheduled_slots:
        used_total += slot.end_offset_min - slot.start_offset_min

    available = block_plan.block_duration_min - used_total
    if available <= 0.0:
        return block_plan

    # Sort maintenance tasks by score descending (best first)
    sorted_maint = sorted(
        maintenance_tasks,
        key=lambda st: st.final_score,
        reverse=True,
    )

    # Build new slots
    new_slots = list(block_plan.scheduled_slots)
    cursor = max(
        (s.end_offset_min for s in new_slots),
        default=0.0,
    )
    added_min = 0.0

    for st in sorted_maint:
        if shortfall - added_min <= 0.0:
            break

        task_time = st.work_unit.estimated_expected_min
        if cursor + task_time > block_plan.block_duration_min + 0.01:
            continue
        if task_time > available - added_min + 0.01:
            continue

        new_slot = ScheduledSlot(
            scored_task=st,
            start_offset_min=round(cursor, 2),
            end_offset_min=round(cursor + task_time, 2),
            includes_switch_cost=False,
            switch_cost_min=0.0,
            energy_match=False,  # maintenance typically low-energy
        )
        new_slots.append(new_slot)
        cursor += task_time
        added_min += task_time

    if added_min <= 0.0:
        return block_plan

    # Build updated plan via model_copy
    new_used_filler = block_plan.used_filler_min + added_min
    updated = block_plan.model_copy(
        update={
            "scheduled_slots": new_slots,
            "used_filler_min": round(new_used_filler, 2),
        },
    )
    return updated


# ---------------------------------------------------------------------------
# Work mode balance
# ---------------------------------------------------------------------------


class WorkModeBalance(BaseModel):
    """Analysis of work mode distribution over a time period."""

    model_config = ConfigDict(extra="forbid")

    period_days: int = 7
    mode_distribution: dict[str, float] = Field(
        default_factory=dict,
        description="work_mode → percentage (0-100)",
    )
    total_minutes: float = 0.0
    imbalances: list[str] = Field(
        default_factory=list,
        description="Human-readable warnings about distribution issues",
    )


def check_work_mode_balance(  # noqa: C901
    records: list[ExecutionRecord],
    period_days: int = 7,
) -> WorkModeBalance:
    """Analyse work mode distribution from execution records.

    Checks the last `period_days` of records and flags:
      - 0% research in last 5+ days
      - >70% any single mode
      - Any mode that appeared previously but dropped off entirely
    """
    if not records:
        return WorkModeBalance(period_days=period_days)

    # Filter records to the period
    now = datetime.now(timezone.utc)
    period_records: list[ExecutionRecord] = []
    older_records: list[ExecutionRecord] = []

    for rec in records:
        if rec.timestamp:
            try:
                ts = datetime.fromisoformat(rec.timestamp)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                delta = (now - ts).total_seconds() / 86400.0
                if delta <= period_days:
                    period_records.append(rec)
                else:
                    older_records.append(rec)
                continue
            except (ValueError, TypeError):
                pass
        # Records without valid timestamps are included in the period
        period_records.append(rec)

    if not period_records:
        return WorkModeBalance(period_days=period_days)

    # Calculate minutes per mode
    mode_minutes: dict[str, float] = {}
    for rec in period_records:
        mode = rec.work_mode.value
        mode_minutes[mode] = mode_minutes.get(mode, 0.0) + rec.actual_duration_min

    total = sum(mode_minutes.values())
    if total <= 0.0:
        return WorkModeBalance(period_days=period_days, total_minutes=0.0)

    # Calculate percentages
    distribution = {mode: round((mins / total) * 100, 2) for mode, mins in mode_minutes.items()}

    # Detect imbalances
    imbalances: list[str] = []

    # Check for missing research
    research_pct = distribution.get("research", 0.0)
    if research_pct == 0.0 and period_days >= 5:
        imbalances.append(f"No research activity in {period_days} days")

    # Check for single-mode dominance
    for mode, pct in distribution.items():
        if pct > 70.0:
            imbalances.append(f"{mode} dominates at {pct}%")

    # Check for modes that dropped off
    older_modes = set()
    for rec in older_records:
        older_modes.add(rec.work_mode.value)

    current_modes = set(distribution.keys())
    for mode in older_modes:
        if mode not in current_modes:
            imbalances.append(f"{mode} dropped off")

    return WorkModeBalance(
        period_days=period_days,
        mode_distribution=distribution,
        total_minutes=round(total, 2),
        imbalances=imbalances,
    )


# ---------------------------------------------------------------------------
# Deferment age recalculation
# ---------------------------------------------------------------------------


def update_deferment_ages(
    work_units: list[WorkUnit],
    reference_date: datetime | None = None,
) -> list[WorkUnit]:
    """Recalculate deferment_age_days from generated_at timestamps.

    Returns new WorkUnit copies — originals are not mutated.
    Tasks without a parseable generated_at are returned as-is (copy).
    """
    ref = reference_date or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)

    result: list[WorkUnit] = []

    for wu in work_units:
        if not wu.generated_at:
            result.append(wu.model_copy())
            continue

        try:
            gen_dt = datetime.fromisoformat(wu.generated_at)
            if gen_dt.tzinfo is None:
                gen_dt = gen_dt.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (ref - gen_dt).total_seconds() / 86400.0)
            result.append(wu.model_copy(update={"deferment_age_days": round(age_days, 2)}))
        except (ValueError, TypeError):
            result.append(wu.model_copy())

    return result
