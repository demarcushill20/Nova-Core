"""Rolling-Horizon Replanner for the Intelligent Dynamic Block Scheduler.

Phase 4: Monitors block execution and triggers mid-block replanning when
conditions change.  Three-horizon model provides strategic (full block),
tactical (next 2 hours), and execution (current + next task) views.

Lifecycle hooks (on_task_start, on_task_complete, on_task_fail) integrate
with the watcher dispatch loop (Phase 10).  State is persisted atomically
to STATE/scheduler/ for crash recovery.
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from utils.scheduler.block import BlockPlan, ScheduledSlot
from utils.scheduler.packer import PackerConfig, pack_block
from utils.scheduler.scorer import ScoringConfig, rank_tasks
from utils.scheduler.work_unit import WorkUnit

log = logging.getLogger("nova.scheduler.replanner")

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ReplanReason(str, Enum):
    """Why a replan was triggered."""

    EARLY_COMPLETION = "early_completion"
    OVERRUN = "overrun"
    DEPENDENCY_FAILURE = "dependency_failure"
    NEW_HIGH_PRIORITY_TASK = "new_high_priority_task"
    BUFFER_HEALTH_LOW = "buffer_health_low"
    CRITICAL_INTERRUPT = "critical_interrupt"
    MANUAL = "manual"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class ReplannerConfig(BaseModel):
    """Configuration for the rolling-horizon replanner."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, description="Feature flag — must be True to trigger replans")
    replan_cooldown_min: float = Field(
        default=10.0,
        ge=0.0,
        description="Minimum minutes between replans",
    )
    early_completion_threshold_pct: float = Field(
        default=20.0,
        ge=0.0,
        le=100.0,
        description="Task finishes >N% early triggers replan",
    )
    overrun_threshold_pct: float = Field(
        default=25.0,
        ge=0.0,
        le=100.0,
        description="Task takes >N% over estimate triggers replan",
    )
    buffer_health_min_pct: float = Field(
        default=15.0,
        ge=0.0,
        le=100.0,
        description="Buffer drops below N% triggers replan",
    )
    max_replans_per_block: int = Field(
        default=5,
        ge=0,
        description="Anti-thrashing cap",
    )


# ---------------------------------------------------------------------------
# Block state
# ---------------------------------------------------------------------------


class BlockState(BaseModel):
    """Tracks execution progress within a scheduled block."""

    model_config = ConfigDict(extra="forbid")

    block_id: str
    original_plan: BlockPlan
    block_start_utc: datetime
    block_end_utc: datetime
    completed_task_ids: list[str] = Field(default_factory=list)
    failed_task_ids: list[str] = Field(default_factory=list)
    in_progress_task_id: str | None = None
    actual_durations: dict[str, float] = Field(
        default_factory=dict,
        description="task_id -> actual minutes",
    )
    elapsed_min: float = 0.0
    replan_count: int = 0
    last_replan_at: datetime | None = None

    # The plan may be replaced by replanning; starts as original_plan
    current_plan: BlockPlan | None = None

    # ----- computed (not serialized — use property instead of computed_field
    #        to avoid extra="forbid" conflict on deserialization) -----

    @property
    def remaining_min(self) -> float:
        """Minutes remaining in the block."""
        total = (self.block_end_utc - self.block_start_utc).total_seconds() / 60.0
        return max(0.0, total - self.elapsed_min)

    # ----- helpers -----

    @property
    def active_plan(self) -> BlockPlan:
        """Return current_plan if set, otherwise original_plan."""
        return self.current_plan if self.current_plan is not None else self.original_plan

    def remaining_scheduled(self) -> list[ScheduledSlot]:
        """Slots not yet completed or failed."""
        done = set(self.completed_task_ids) | set(self.failed_task_ids)
        return [slot for slot in self.active_plan.scheduled_slots if slot.scored_task.work_unit.id not in done]

    def is_task_done(self, task_id: str) -> bool:
        """Whether a task is completed or failed."""
        return task_id in self.completed_task_ids or task_id in self.failed_task_ids

    def mark_complete(self, task_id: str, actual_duration_min: float) -> None:
        """Record a task as completed."""
        if task_id not in self.completed_task_ids:
            self.completed_task_ids.append(task_id)
        self.actual_durations[task_id] = actual_duration_min
        if self.in_progress_task_id == task_id:
            self.in_progress_task_id = None

    def mark_failed(self, task_id: str, reason: str = "") -> None:
        """Record a task as failed."""
        if task_id not in self.failed_task_ids:
            self.failed_task_ids.append(task_id)
        if self.in_progress_task_id == task_id:
            self.in_progress_task_id = None
        if reason:
            log.info("Task '%s' failed: %s", task_id, reason)

    def mark_started(self, task_id: str) -> None:
        """Mark a task as in-progress."""
        if self.in_progress_task_id and self.in_progress_task_id != task_id:
            log.warning(
                "Overwriting in-progress task %s with %s",
                self.in_progress_task_id,
                task_id,
            )
        self.in_progress_task_id = task_id


# ---------------------------------------------------------------------------
# Replan decision
# ---------------------------------------------------------------------------


class ReplanDecision(BaseModel):
    """Result of a replan trigger check or replan execution."""

    model_config = ConfigDict(extra="forbid")

    should_replan: bool
    reason: ReplanReason | None = None
    reason_detail: str = ""
    new_plan: BlockPlan | None = None
    deferred_from_old: list[str] = Field(
        default_factory=list,
        description="Task IDs removed from the old plan",
    )
    newly_scheduled: list[str] = Field(
        default_factory=list,
        description="Task IDs added in the new plan",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_slot(state: BlockState, task_id: str) -> ScheduledSlot | None:
    """Find the scheduled slot for a given task ID."""
    for slot in state.active_plan.scheduled_slots:
        if slot.scored_task.work_unit.id == task_id:
            return slot
    return None


def _utcnow() -> datetime:
    """Return current UTC time (mockable in tests)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Three-horizon model
# ---------------------------------------------------------------------------


def get_strategic_horizon(state: BlockState) -> BlockPlan:
    """Strategic horizon: the full block plan (current or replanned)."""
    return state.active_plan


def get_tactical_horizon(state: BlockState) -> list[ScheduledSlot]:
    """Tactical horizon: next 2 hours of scheduled slots from current position.

    Uses elapsed_min to determine which slots fall in the next 120 minutes.
    """
    remaining = state.remaining_scheduled()
    window_end = state.elapsed_min + 120.0

    result: list[ScheduledSlot] = []
    for slot in remaining:
        if slot.start_offset_min < window_end:
            result.append(slot)
    return result


def get_execution_horizon(
    state: BlockState,
) -> tuple[ScheduledSlot | None, ScheduledSlot | None]:
    """Execution horizon: (current_task, next_task).

    If a task is in progress, it is the current task.  Otherwise the first
    remaining slot is "current" (ready to start).  The slot after that is
    "next".
    """
    remaining = state.remaining_scheduled()
    if not remaining:
        return (None, None)

    # Sort by start_offset to get ordering
    remaining_sorted = sorted(remaining, key=lambda s: s.start_offset_min)

    current: ScheduledSlot | None = None
    next_task: ScheduledSlot | None = None

    if state.in_progress_task_id:
        # Find the in-progress slot
        for slot in remaining_sorted:
            if slot.scored_task.work_unit.id == state.in_progress_task_id:
                current = slot
                break
        # Next is the first remaining that is NOT the current
        for slot in remaining_sorted:
            if (
                current is None or slot.scored_task.work_unit.id != current.scored_task.work_unit.id
            ) and current is not None:
                next_task = slot
                break
    else:
        # No task in progress — first remaining is "current" (ready to dispatch)
        current = remaining_sorted[0] if remaining_sorted else None
        next_task = remaining_sorted[1] if len(remaining_sorted) > 1 else None

    return (current, next_task)


# ---------------------------------------------------------------------------
# Replan trigger checks
# ---------------------------------------------------------------------------


def check_replan_triggers(  # noqa: C901
    state: BlockState,
    config: ReplannerConfig,
    *,
    now: datetime | None = None,
) -> ReplanDecision:
    """Check whether a replan should be triggered.

    Evaluates triggers in priority order:
      1. Overrun detection (in-progress task exceeding estimate)
      2. Early completion (last completed task finished ahead of schedule)
      3. Dependency failure (remaining tasks depend on a failed task)
      4. Buffer health (cumulative overruns eroding buffer)

    Returns a ReplanDecision with should_replan=True if any trigger fires.
    """
    no_replan = ReplanDecision(should_replan=False)

    # Guard: feature flag
    if not config.enabled:
        return no_replan

    # Guard: cooldown
    now_ts = now or _utcnow()
    if state.last_replan_at is not None:
        elapsed_since_replan = (now_ts - state.last_replan_at).total_seconds() / 60.0
        if elapsed_since_replan < config.replan_cooldown_min:
            return no_replan

    # Guard: max replans
    if state.replan_count >= config.max_replans_per_block:
        return no_replan

    # 1. Overrun detection
    if state.in_progress_task_id:
        slot = _find_slot(state, state.in_progress_task_id)
        if slot:
            estimated = slot.scored_task.work_unit.estimated_expected_min
            # Compute how long the current task has been running
            # Use actual_durations if partially tracked, otherwise derive from elapsed
            actual_elapsed = state.actual_durations.get(state.in_progress_task_id)
            if actual_elapsed is not None and actual_elapsed > estimated * (1 + config.overrun_threshold_pct / 100.0):
                return ReplanDecision(
                    should_replan=True,
                    reason=ReplanReason.OVERRUN,
                    reason_detail=(
                        f"Task '{state.in_progress_task_id}' at {actual_elapsed:.1f} min "
                        f"exceeds estimate {estimated:.1f} min by "
                        f">{config.overrun_threshold_pct:.0f}%"
                    ),
                )

    # 2. Early completion
    if state.completed_task_ids:
        last_completed = state.completed_task_ids[-1]
        slot = _find_slot(state, last_completed)
        if slot:
            actual = state.actual_durations.get(last_completed, 0.0)
            estimated = slot.scored_task.work_unit.estimated_expected_min
            if estimated > 0 and actual < estimated * (1 - config.early_completion_threshold_pct / 100.0):
                return ReplanDecision(
                    should_replan=True,
                    reason=ReplanReason.EARLY_COMPLETION,
                    reason_detail=(
                        f"Task '{last_completed}' completed in {actual:.1f} min "
                        f"vs estimated {estimated:.1f} min "
                        f"(>{config.early_completion_threshold_pct:.0f}% early)"
                    ),
                )

    # 3. Dependency failure
    if state.failed_task_ids:
        remaining = state.remaining_scheduled()
        failed_set = set(state.failed_task_ids)
        for slot in remaining:
            deps = set(slot.scored_task.work_unit.dependency_ids)
            if deps & failed_set:
                failed_dep = (deps & failed_set).pop()
                return ReplanDecision(
                    should_replan=True,
                    reason=ReplanReason.DEPENDENCY_FAILURE,
                    reason_detail=(f"Task '{slot.scored_task.work_unit.id}' depends on failed task '{failed_dep}'"),
                )

    # 4. Buffer health
    plan = state.active_plan
    if plan.total_buffer_min > 0:
        buffer_used = 0.0
        for task_id, actual in state.actual_durations.items():
            slot = _find_slot(state, task_id)
            if slot:
                estimated = slot.scored_task.work_unit.estimated_expected_min
                overrun = max(0.0, actual - estimated)
                buffer_used += overrun

        buffer_remaining = plan.total_buffer_min - buffer_used
        buffer_health_pct = (buffer_remaining / plan.total_buffer_min) * 100.0
        if buffer_health_pct < config.buffer_health_min_pct:
            return ReplanDecision(
                should_replan=True,
                reason=ReplanReason.BUFFER_HEALTH_LOW,
                reason_detail=(
                    f"Buffer health at {buffer_health_pct:.1f}% (below {config.buffer_health_min_pct:.0f}% threshold)"
                ),
            )

    return no_replan


# ---------------------------------------------------------------------------
# Replan execution
# ---------------------------------------------------------------------------


def execute_replan(
    state: BlockState,
    new_tasks: list[WorkUnit] | None = None,
    config: ReplannerConfig | None = None,
    scoring_config: ScoringConfig | None = None,
    packer_config: PackerConfig | None = None,
    *,
    reason: ReplanReason = ReplanReason.MANUAL,
    reason_detail: str = "",
    now: datetime | None = None,
) -> ReplanDecision:
    """Execute a replan: re-score and re-pack remaining work.

    Steps:
      1. Collect remaining work units from state.remaining_scheduled()
      2. Add any new_tasks if provided
      3. Remove tasks whose dependencies have failed
      4. Re-score with rank_tasks()
      5. Re-pack with pack_block(remaining_time)
      6. Build ReplanDecision with new plan, deferred, newly scheduled
      7. Update state: increment replan_count, set last_replan_at, set current_plan
      8. Return decision
    """
    now_ts = now or _utcnow()
    cfg = config or ReplannerConfig()

    # Guard: feature flag
    if not cfg.enabled:
        return ReplanDecision(should_replan=False)

    # 1. Collect remaining work units
    remaining_slots = state.remaining_scheduled()
    old_task_ids = {slot.scored_task.work_unit.id for slot in remaining_slots}
    remaining_units: list[WorkUnit] = [slot.scored_task.work_unit for slot in remaining_slots]

    # 2. Add new tasks
    incoming_ids: set[str] = set()
    if new_tasks:
        for wu in new_tasks:
            if wu.id not in old_task_ids:
                remaining_units.append(wu)
                incoming_ids.add(wu.id)

    # 3. Remove tasks whose dependencies have failed
    failed_set = set(state.failed_task_ids)
    filtered_units: list[WorkUnit] = []
    removed_dep_failure: list[str] = []
    for wu in remaining_units:
        dep_set = set(wu.dependency_ids)
        if dep_set & failed_set:
            removed_dep_failure.append(wu.id)
        else:
            filtered_units.append(wu)

    # 4. Re-score
    scored = rank_tasks(filtered_units, scoring_config)

    # 5. Re-pack with remaining time
    remaining_time = state.remaining_min
    new_plan = pack_block(scored, remaining_time, packer_config)

    # 6. Build decision
    new_scheduled_ids = {slot.scored_task.work_unit.id for slot in new_plan.scheduled_slots}
    deferred_from_old = [tid for tid in old_task_ids if tid not in new_scheduled_ids] + removed_dep_failure
    newly_scheduled = [tid for tid in new_scheduled_ids if tid in incoming_ids]

    # 7. Update state
    state.replan_count += 1
    state.last_replan_at = now_ts
    state.current_plan = new_plan

    log.info(
        "Replan #%d for block '%s': %d tasks scheduled, %d deferred, %d new",
        state.replan_count,
        state.block_id,
        len(new_plan.scheduled_slots),
        len(deferred_from_old),
        len(newly_scheduled),
    )

    return ReplanDecision(
        should_replan=True,
        reason=reason,
        reason_detail=reason_detail,
        new_plan=new_plan,
        deferred_from_old=deferred_from_old,
        newly_scheduled=newly_scheduled,
    )


# ---------------------------------------------------------------------------
# Task lifecycle hooks
# ---------------------------------------------------------------------------


def on_task_start(state: BlockState, task_id: str) -> None:
    """Mark a task as in-progress."""
    state.mark_started(task_id)


def on_task_progress(
    state: BlockState,
    task_id: str,
    elapsed_min: float,
    config: ReplannerConfig | None = None,
    *,
    now: datetime | None = None,
) -> ReplanDecision:
    """Update progress for in-progress task and check for overrun.

    Call periodically during task execution to detect overruns before
    the task completes.  Updates actual_durations with the current
    elapsed time and runs the trigger checks.
    """
    state.actual_durations[task_id] = elapsed_min
    if config and config.enabled:
        return check_replan_triggers(state, config, now=now)
    return ReplanDecision(should_replan=False)


def on_task_complete(
    state: BlockState,
    task_id: str,
    actual_duration_min: float,
    config: ReplannerConfig | None = None,
    scoring_config: ScoringConfig | None = None,
    packer_config: PackerConfig | None = None,
    *,
    now: datetime | None = None,
) -> ReplanDecision:
    """Handle task completion: update state, check triggers, maybe replan.

    Checks for overrun BEFORE clearing in_progress (mark_complete clears it),
    then checks remaining triggers (early completion, dep failure, buffer).

    Returns a ReplanDecision indicating whether a replan was executed.
    """
    cfg = config or ReplannerConfig()

    # Check overrun BEFORE clearing in_progress (mark_complete clears it)
    overrun_decision = None
    if state.in_progress_task_id == task_id and cfg.enabled:
        state.actual_durations[task_id] = actual_duration_min
        overrun_decision = check_replan_triggers(state, cfg, now=now)

    # Now mark complete (clears in_progress_task_id)
    state.mark_complete(task_id, actual_duration_min)

    # If overrun was detected, execute that replan
    if overrun_decision and overrun_decision.should_replan and overrun_decision.reason is not None:
        return execute_replan(
            state,
            config=cfg,
            scoring_config=scoring_config,
            packer_config=packer_config,
            reason=overrun_decision.reason,
            reason_detail=overrun_decision.reason_detail,
            now=now,
        )

    # Otherwise check other triggers (early completion, dep failure, buffer)
    decision = check_replan_triggers(state, cfg, now=now)
    if decision.should_replan and decision.reason is not None:
        return execute_replan(
            state,
            config=cfg,
            scoring_config=scoring_config,
            packer_config=packer_config,
            reason=decision.reason,
            reason_detail=decision.reason_detail,
            now=now,
        )

    return decision


def on_task_fail(
    state: BlockState,
    task_id: str,
    reason: str = "",
    config: ReplannerConfig | None = None,
    scoring_config: ScoringConfig | None = None,
    packer_config: PackerConfig | None = None,
    *,
    now: datetime | None = None,
) -> ReplanDecision:
    """Handle task failure: update state, check triggers, maybe replan.

    Returns a ReplanDecision indicating whether a replan was executed.
    """
    state.mark_failed(task_id, reason)

    cfg = config or ReplannerConfig()
    decision = check_replan_triggers(state, cfg, now=now)

    if decision.should_replan and decision.reason is not None:
        return execute_replan(
            state,
            config=cfg,
            scoring_config=scoring_config,
            packer_config=packer_config,
            reason=decision.reason,
            reason_detail=decision.reason_detail,
            now=now,
        )

    return decision


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

_DEFAULT_STATE_DIR = Path("STATE/scheduler")


def save_block_state(state: BlockState, state_dir: Path | None = None) -> Path:
    """Persist block state to STATE/scheduler/{block_id}_state.json.

    Uses atomic write (tempfile + rename) to prevent corruption.
    """
    sdir = state_dir or _DEFAULT_STATE_DIR
    sdir.mkdir(parents=True, exist_ok=True)

    target = sdir / f"{state.block_id}_state.json"
    data = state.model_dump_json(indent=2)

    # Atomic write
    fd = tempfile.NamedTemporaryFile(  # noqa: SIM115 — atomic write pattern
        mode="w",
        dir=str(sdir),
        suffix=".tmp",
        delete=False,
    )
    try:
        fd.write(data)
        fd.flush()
        fd.close()
        Path(fd.name).replace(target)
    except Exception:
        # Clean up temp file on failure
        try:
            Path(fd.name).unlink(missing_ok=True)
        except OSError:
            pass
        raise

    log.debug("Saved block state to %s", target)
    return target


def load_block_state(block_id: str, state_dir: Path | None = None) -> BlockState | None:
    """Load block state from STATE/scheduler/{block_id}_state.json.

    Returns None if the file does not exist.
    """
    sdir = state_dir or _DEFAULT_STATE_DIR
    target = sdir / f"{block_id}_state.json"

    if not target.exists():
        return None

    try:
        raw = target.read_text()
        return BlockState.model_validate_json(raw)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        log.warning("Failed to load block state from %s: %s", target, exc)
        return None
