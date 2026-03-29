"""Block model for the Intelligent Dynamic Block Scheduler.

Phase 3: Defines the scheduling block structure — energy levels, time slots,
and the BlockPlan that holds a capacity-aware packing of scored tasks.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from utils.scheduler.scorer import ScoredTask
from utils.scheduler.work_unit import TaskClass, WorkMode

# ---------------------------------------------------------------------------
# Energy
# ---------------------------------------------------------------------------


class EnergyLevel(str, Enum):
    """Cognitive energy level within a scheduling block."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


ENERGY_MODE_MAP: dict[WorkMode, EnergyLevel] = {
    WorkMode.DEEP_IMPLEMENTATION: EnergyLevel.HIGH,
    WorkMode.DEBUGGING: EnergyLevel.HIGH,
    WorkMode.RESEARCH: EnergyLevel.HIGH,
    WorkMode.ANALYSIS: EnergyLevel.MEDIUM,
    WorkMode.REVIEW: EnergyLevel.MEDIUM,
    WorkMode.DEPLOYMENT: EnergyLevel.MEDIUM,
    WorkMode.MONITORING: EnergyLevel.LOW,
    WorkMode.ADMIN: EnergyLevel.LOW,
    WorkMode.COMMUNICATION: EnergyLevel.LOW,
}

DEFAULT_ENERGY_CURVE: list[tuple[float, EnergyLevel]] = [
    (0.40, EnergyLevel.HIGH),
    (0.70, EnergyLevel.MEDIUM),
    (1.00, EnergyLevel.LOW),
]


# ---------------------------------------------------------------------------
# Slot / Plan models
# ---------------------------------------------------------------------------


class ScheduledSlot(BaseModel):
    """A single task placed into a scheduling block."""

    model_config = ConfigDict(extra="forbid")

    scored_task: ScoredTask
    start_offset_min: float
    end_offset_min: float
    includes_switch_cost: bool = False
    switch_cost_min: float = 0.0
    energy_match: bool = True


class BlockPlan(BaseModel):
    """A capacity-aware packing of scored tasks into a time block."""

    model_config = ConfigDict(extra="forbid")

    block_start_min: float = 0.0
    block_duration_min: float = 480.0
    committed_ratio: float = 0.75
    buffer_ratio: float = 0.17
    filler_ratio: float = 0.08

    scheduled_slots: list[ScheduledSlot] = Field(default_factory=list)
    deferred_tasks: list[ScoredTask] = Field(default_factory=list)

    total_committed_min: float = 0.0
    total_buffer_min: float = 0.0
    total_filler_min: float = 0.0
    used_committed_min: float = 0.0
    used_filler_min: float = 0.0
    buffer_borrowed_min: float = 0.0
    context_switches: int = 0
    plan_confidence: float = 0.0
    epic_warnings: list[str] = Field(default_factory=list)

    # -------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------

    @property
    def remaining_committed_min(self) -> float:
        return self.total_committed_min - self.used_committed_min

    @property
    def remaining_filler_min(self) -> float:
        return self.total_filler_min - self.used_filler_min

    @property
    def utilization_pct(self) -> float:
        if self.block_duration_min <= 0:
            return 0.0
        return (self.used_committed_min + self.used_filler_min) / self.block_duration_min * 100


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_block_plan(plan: BlockPlan) -> list[str]:
    """Validate a BlockPlan and return a list of warnings (empty = valid)."""
    warnings: list[str] = []

    # Check: used capacity does not exceed block duration
    total_used = plan.used_committed_min + plan.used_filler_min
    if total_used > plan.block_duration_min + 0.01:  # small epsilon for floats
        warnings.append(
            f"Over-scheduled: used {total_used:.1f} min exceeds block duration {plan.block_duration_min:.1f} min"
        )

    # Check: buffer_ratio >= 10%
    if plan.buffer_ratio < 0.10:
        warnings.append(f"Buffer ratio {plan.buffer_ratio:.2f} is below minimum 0.10")

    # Check: no EPIC tasks without warning
    for slot in plan.scheduled_slots:
        wu = slot.scored_task.work_unit
        if wu.task_class == TaskClass.EPIC:
            epic_id = wu.id or wu.title
            if epic_id not in plan.epic_warnings and f"EPIC task scheduled: {epic_id}" not in plan.epic_warnings:
                # Check if any warning mentions this task
                has_warning = any(epic_id in w for w in plan.epic_warnings)
                if not has_warning:
                    warnings.append(f"EPIC task '{epic_id}' scheduled without warning")

    # Check: committed capacity overflow from HARD tasks
    if plan.used_committed_min > plan.total_committed_min + 0.01:
        warnings.append(
            f"Committed capacity exceeded: used {plan.used_committed_min:.1f} min "
            f"vs total {plan.total_committed_min:.1f} min"
        )

    # Check: all scheduled slots fit within block duration
    for slot in plan.scheduled_slots:
        if slot.end_offset_min > plan.block_duration_min + 0.01:
            warnings.append(
                f"Slot for '{slot.scored_task.work_unit.id}' ends at "
                f"{slot.end_offset_min:.1f} min, exceeding block duration "
                f"{plan.block_duration_min:.1f} min"
            )

    # Check: no overlapping slots
    sorted_slots = sorted(plan.scheduled_slots, key=lambda s: s.start_offset_min)
    for i in range(1, len(sorted_slots)):
        prev = sorted_slots[i - 1]
        curr = sorted_slots[i]
        if curr.start_offset_min < prev.end_offset_min - 0.01:
            warnings.append(
                f"Overlapping slots: '{prev.scored_task.work_unit.id}' "
                f"(ends {prev.end_offset_min:.1f}) overlaps "
                f"'{curr.scored_task.work_unit.id}' (starts {curr.start_offset_min:.1f})"
            )

    return warnings
