"""Block Packer — Capacity-Aware Scheduling for the Dynamic Block Scheduler.

Phase 3: Takes a list of ScoredTasks and packs them into a BlockPlan,
respecting commitment levels, energy curves, context-switch costs, and
capacity limits.  Deterministic — no randomness.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field, model_validator

from utils.scheduler.block import (
    DEFAULT_ENERGY_CURVE,
    ENERGY_MODE_MAP,
    BlockPlan,
    EnergyLevel,
    ScheduledSlot,
)
from utils.scheduler.scorer import ScoredTask
from utils.scheduler.work_unit import CommitmentLevel, TaskClass, WorkMode

log = logging.getLogger("nova.scheduler.packer")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class PackerConfig(BaseModel):
    """Configuration for the block packer."""

    model_config = ConfigDict(extra="forbid")

    committed_ratio: float = Field(default=0.75, ge=0.0, le=1.0)
    buffer_ratio: float = Field(default=0.17, ge=0.0, le=1.0)
    filler_ratio: float = Field(default=0.08, ge=0.0, le=1.0)
    same_mode_switch_cost_min: float = Field(default=2.0, ge=0.0)
    different_mode_switch_cost_min: float = Field(default=10.0, ge=0.0)
    energy_curve: list[tuple[float, str]] = Field(
        default_factory=lambda: [(f, e.value) for f, e in DEFAULT_ENERGY_CURVE],
    )
    reject_if_confidence_below: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_ratios(self) -> PackerConfig:
        total = self.committed_ratio + self.buffer_ratio + self.filler_ratio
        if total > 1.0 + 0.01:
            # Normalize to sum to 1.0
            self.committed_ratio /= total
            self.buffer_ratio /= total
            self.filler_ratio /= total
        return self


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _calculate_switch_cost(
    prev_mode: WorkMode | None,
    current_mode: WorkMode,
    current_task_switch_cost: float,
    config: PackerConfig,
) -> float:
    """Calculate context-switch cost between two adjacent tasks."""
    if prev_mode is None:
        return 0.0

    if prev_mode == current_mode:
        base = config.same_mode_switch_cost_min
    else:
        base = config.different_mode_switch_cost_min

    # Use the task's own switch cost if larger
    return max(base, current_task_switch_cost)


def _get_energy_level_at(offset_fraction: float, config: PackerConfig) -> EnergyLevel:
    """Return the energy level for a given position (0.0-1.0) in the block."""
    for threshold, level_str in config.energy_curve:
        if offset_fraction <= threshold:
            return EnergyLevel(level_str)
    # Fallback to LOW if past all thresholds
    return EnergyLevel.LOW


def _preferred_energy(mode: WorkMode) -> EnergyLevel:
    """Return the preferred energy level for a work mode."""
    return ENERGY_MODE_MAP.get(mode, EnergyLevel.MEDIUM)


def _sort_by_energy_and_score(
    scored_tasks: list[ScoredTask],
    config: PackerConfig,
) -> list[ScoredTask]:
    """Group tasks by preferred energy level, sorted by score within each group.

    Returns: HIGH-energy tasks first (score desc), then MEDIUM, then LOW.
    """
    groups: dict[EnergyLevel, list[ScoredTask]] = {
        EnergyLevel.HIGH: [],
        EnergyLevel.MEDIUM: [],
        EnergyLevel.LOW: [],
    }

    for st in scored_tasks:
        energy = _preferred_energy(st.work_unit.work_mode)
        groups[energy].append(st)

    # Sort within each group by final_score descending
    for energy in groups:
        groups[energy].sort(key=lambda s: s.final_score, reverse=True)

    return groups[EnergyLevel.HIGH] + groups[EnergyLevel.MEDIUM] + groups[EnergyLevel.LOW]


# ---------------------------------------------------------------------------
# Main packer
# ---------------------------------------------------------------------------


def pack_block(  # noqa: C901
    scored_tasks: list[ScoredTask],
    block_duration_min: float = 480.0,
    config: PackerConfig | None = None,
) -> BlockPlan:
    """Pack scored tasks into a capacity-aware block plan.

    Algorithm:
      1. Calculate capacity buckets (committed / buffer / filler).
      2. Separate by commitment level.
      3. Filter for schedulable_now, handle EPIC.
      4. Place HARD tasks into committed capacity.
      5. Place SOFT tasks into remaining committed capacity.
      6. Place OPPORTUNISTIC tasks into filler capacity.
      7. Apply energy-aware ordering.
      8. Calculate context switches and switch costs.
      9. Assign time offsets.
      10. Build and return BlockPlan.
    """
    cfg = config or PackerConfig()

    plan = BlockPlan(
        block_duration_min=block_duration_min,
        committed_ratio=cfg.committed_ratio,
        buffer_ratio=cfg.buffer_ratio,
        filler_ratio=cfg.filler_ratio,
        total_committed_min=block_duration_min * cfg.committed_ratio,
        total_buffer_min=block_duration_min * cfg.buffer_ratio,
        total_filler_min=block_duration_min * cfg.filler_ratio,
    )

    if block_duration_min <= 0 or not scored_tasks:
        return plan

    # ------------------------------------------------------------------
    # Step 2+3: Separate by commitment, filter schedulable, handle EPIC
    # ------------------------------------------------------------------
    hard_tasks: list[ScoredTask] = []
    soft_tasks: list[ScoredTask] = []
    opportunistic_tasks: list[ScoredTask] = []

    for st in scored_tasks:
        wu = st.work_unit

        # Skip non-schedulable tasks
        if not wu.schedulable_now:
            plan.deferred_tasks.append(st)
            continue

        # EPIC handling
        if wu.task_class == TaskClass.EPIC:
            epic_id = wu.id or wu.title or "unknown"
            warning_msg = f"EPIC task scheduled: {epic_id}"
            if wu.must_do_today:
                plan.epic_warnings.append(warning_msg + " (must_do_today override)")
                # Fall through to normal placement
            else:
                plan.epic_warnings.append(warning_msg + " (deferred — split first)")
                plan.deferred_tasks.append(st)
                continue

        # Route by commitment level
        if wu.commitment_level == CommitmentLevel.HARD:
            hard_tasks.append(st)
        elif wu.commitment_level == CommitmentLevel.SOFT:
            soft_tasks.append(st)
        else:
            opportunistic_tasks.append(st)

    # ------------------------------------------------------------------
    # Step 4: Place HARD tasks (must fit)
    # ------------------------------------------------------------------
    hard_tasks.sort(key=lambda s: s.final_score, reverse=True)
    committed_placed: list[ScoredTask] = []
    committed_used = 0.0

    buffer_borrowed = 0.0

    for st in hard_tasks:
        task_time = st.work_unit.estimated_expected_min
        if committed_used + task_time <= plan.total_committed_min + 0.01:
            committed_placed.append(st)
            committed_used += task_time
        else:
            # HARD that doesn't fit in committed — borrow from buffer
            overflow = (committed_used + task_time) - plan.total_committed_min
            if buffer_borrowed + overflow <= plan.total_buffer_min + 0.01:
                log.warning(
                    "HARD task '%s' (%.1f min) borrows %.1f min from buffer",
                    st.work_unit.id,
                    task_time,
                    overflow,
                )
                committed_placed.append(st)
                committed_used += task_time
                buffer_borrowed += overflow
            else:
                # Buffer exhausted — still place (HARD is mandatory) but warn loudly
                log.warning(
                    "HARD task '%s' (%.1f min) exceeds committed + buffer capacity",
                    st.work_unit.id,
                    task_time,
                )
                committed_placed.append(st)
                committed_used += task_time
                buffer_borrowed += overflow

    # ------------------------------------------------------------------
    # Step 5: Place SOFT tasks into remaining committed
    # ------------------------------------------------------------------
    soft_tasks.sort(key=lambda s: s.final_score, reverse=True)

    for st in soft_tasks:
        task_time = st.work_unit.estimated_expected_min
        if committed_used + task_time <= plan.total_committed_min + 0.01:
            committed_placed.append(st)
            committed_used += task_time
        else:
            plan.deferred_tasks.append(st)

    # ------------------------------------------------------------------
    # Step 6: Place OPPORTUNISTIC into filler
    # ------------------------------------------------------------------
    opportunistic_tasks.sort(key=lambda s: s.final_score, reverse=True)
    filler_placed: list[ScoredTask] = []
    filler_used = 0.0

    for st in opportunistic_tasks:
        task_time = st.work_unit.estimated_expected_min
        if filler_used + task_time <= plan.total_filler_min + 0.01:
            filler_placed.append(st)
            filler_used += task_time
        else:
            plan.deferred_tasks.append(st)

    # ------------------------------------------------------------------
    # Step 7: Energy-aware ordering
    # ------------------------------------------------------------------
    all_placed = committed_placed + filler_placed
    ordered = _sort_by_energy_and_score(all_placed, cfg)

    # ------------------------------------------------------------------
    # Step 8+9: Calculate switch costs and assign time offsets
    # ------------------------------------------------------------------
    slots: list[ScheduledSlot] = []
    cursor = 0.0  # current time offset in minutes
    prev_mode: WorkMode | None = None
    context_switches = 0
    total_switch_cost = 0.0

    for st in ordered:
        wu = st.work_unit
        task_time = wu.estimated_expected_min

        # Calculate switch cost
        switch_cost = _calculate_switch_cost(prev_mode, wu.work_mode, wu.context_switch_cost_min, cfg)

        # Check if adding this task + switch cost would overflow
        if cursor + switch_cost + task_time > block_duration_min + 0.01:
            plan.deferred_tasks.append(st)
            continue

        # Energy matching — compute at actual task start (after switch cost)
        offset_frac = (cursor + switch_cost) / block_duration_min if block_duration_min > 0 else 0.0
        block_energy = _get_energy_level_at(offset_frac, cfg)
        task_energy = _preferred_energy(wu.work_mode)
        energy_match = block_energy == task_energy

        start = cursor + switch_cost
        end = start + task_time

        slots.append(
            ScheduledSlot(
                scored_task=st,
                start_offset_min=round(start, 2),
                end_offset_min=round(end, 2),
                includes_switch_cost=switch_cost > 0,
                switch_cost_min=round(switch_cost, 2),
                energy_match=energy_match,
            )
        )

        if prev_mode is not None and prev_mode != wu.work_mode:
            context_switches += 1

        total_switch_cost += switch_cost
        cursor = end
        prev_mode = wu.work_mode

    # ------------------------------------------------------------------
    # Step 8b: Ensure HARD tasks survived reordering (M1 fix)
    # ------------------------------------------------------------------
    dropped_hard: list[ScoredTask] = []
    surviving_deferred: list[ScoredTask] = []
    for st in plan.deferred_tasks:
        if st.work_unit.commitment_level == CommitmentLevel.HARD and st.work_unit.schedulable_now:
            dropped_hard.append(st)
        else:
            surviving_deferred.append(st)

    if dropped_hard:
        plan.deferred_tasks = surviving_deferred
        for hard_st in dropped_hard:
            # Evict lowest-score filler/opportunistic slot to make room
            evictable = [
                (i, s)
                for i, s in enumerate(slots)
                if s.scored_task.work_unit.commitment_level == CommitmentLevel.OPPORTUNISTIC
            ]
            if not evictable:
                evictable = [
                    (i, s)
                    for i, s in enumerate(slots)
                    if s.scored_task.work_unit.commitment_level == CommitmentLevel.SOFT
                ]
            if evictable:
                # Sort by score ascending — evict lowest
                evictable.sort(key=lambda x: x[1].scored_task.final_score)
                evict_idx, evict_slot = evictable[0]
                if evict_slot.scored_task.final_score < hard_st.final_score:
                    plan.deferred_tasks.append(evict_slot.scored_task)
                    slots.pop(evict_idx)
                    log.info(
                        "Re-inserted HARD task '%s' by evicting '%s'",
                        hard_st.work_unit.id,
                        evict_slot.scored_task.work_unit.id,
                    )

            # Rebuild cursor to append HARD task at end
            end_cursor = max((s.end_offset_min for s in slots), default=0.0) if slots else 0.0
            last_mode = slots[-1].scored_task.work_unit.work_mode if slots else None
            sc = _calculate_switch_cost(
                last_mode, hard_st.work_unit.work_mode, hard_st.work_unit.context_switch_cost_min, cfg
            )
            new_start = end_cursor + sc
            new_end = new_start + hard_st.work_unit.estimated_expected_min
            if new_end <= block_duration_min + 0.01:
                h_offset_frac = new_start / block_duration_min if block_duration_min > 0 else 0.0
                h_block_energy = _get_energy_level_at(h_offset_frac, cfg)
                h_task_energy = _preferred_energy(hard_st.work_unit.work_mode)
                slots.append(
                    ScheduledSlot(
                        scored_task=hard_st,
                        start_offset_min=round(new_start, 2),
                        end_offset_min=round(new_end, 2),
                        includes_switch_cost=sc > 0,
                        switch_cost_min=round(sc, 2),
                        energy_match=h_block_energy == h_task_energy,
                    )
                )
                if last_mode is not None and last_mode != hard_st.work_unit.work_mode:
                    context_switches += 1
            else:
                # Cannot fit even after eviction — force-place anyway (HARD is mandatory)
                log.warning("HARD task '%s' could not fit after eviction — force-appending", hard_st.work_unit.id)
                slots.append(
                    ScheduledSlot(
                        scored_task=hard_st,
                        start_offset_min=round(new_start, 2),
                        end_offset_min=round(new_end, 2),
                        includes_switch_cost=sc > 0,
                        switch_cost_min=round(sc, 2),
                        energy_match=False,
                    )
                )

    # ------------------------------------------------------------------
    # Step 10: Build metrics
    # ------------------------------------------------------------------
    # Recalculate used capacity from actually-placed slots
    actual_committed = 0.0
    actual_filler = 0.0
    for slot in slots:
        st = slot.scored_task
        task_time = st.work_unit.estimated_expected_min + slot.switch_cost_min
        if st.work_unit.commitment_level == CommitmentLevel.OPPORTUNISTIC:
            actual_filler += task_time
        else:
            actual_committed += task_time

    plan.scheduled_slots = slots
    plan.used_committed_min = round(actual_committed, 2)
    plan.used_filler_min = round(actual_filler, 2)
    plan.buffer_borrowed_min = round(buffer_borrowed, 2)
    plan.context_switches = context_switches

    # ------------------------------------------------------------------
    # Compute plan_confidence (H2)
    # ------------------------------------------------------------------
    if slots:
        energy_matches = sum(1 for s in slots if s.energy_match)
        energy_match_pct = energy_matches / len(slots)

        utilization = (actual_committed + actual_filler) / block_duration_min if block_duration_min > 0 else 0.0
        # 70-85% is ideal → score 1.0; taper outside
        if 0.70 <= utilization <= 0.85:
            utilization_score = 1.0
        elif utilization < 0.70:
            utilization_score = utilization / 0.70
        else:
            utilization_score = max(0.0, 1.0 - (utilization - 0.85) / 0.15)

        total_input = len(slots) + len(plan.deferred_tasks)
        deferred_ratio = len(plan.deferred_tasks) / total_input if total_input > 0 else 0.0

        plan.plan_confidence = round(
            energy_match_pct * 0.4 + utilization_score * 0.4 + (1.0 - deferred_ratio) * 0.2,
            4,
        )

    return plan
