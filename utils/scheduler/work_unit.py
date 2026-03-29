"""Work unit data model for the Intelligent Dynamic Block Scheduler.

Defines the core scheduling primitives: WorkMode, CommitmentLevel,
TaskClass, and the WorkUnit Pydantic v2 model that enriches existing
task frontmatter with scheduler-aware metadata.
"""

from __future__ import annotations

import os
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class WorkMode(str, Enum):
    """Type of cognitive work a task demands."""

    DEEP_IMPLEMENTATION = "deep_implementation"
    DEBUGGING = "debugging"
    RESEARCH = "research"
    ANALYSIS = "analysis"
    REVIEW = "review"
    COMMUNICATION = "communication"
    DEPLOYMENT = "deployment"
    MONITORING = "monitoring"
    ADMIN = "admin"


class CommitmentLevel(str, Enum):
    """How firmly the task is locked into the schedule."""

    HARD = "hard"  # Cannot be moved (deadline, external dependency)
    SOFT = "soft"  # Should be done today but can slide
    OPPORTUNISTIC = "opportunistic"  # Fill spare capacity


class TaskClass(str, Enum):
    """Size bucket that determines scheduling strategy."""

    ATOMIC = "atomic"  # 5-20 min — single context, no interrupts
    BOUNDED = "bounded"  # 20-90 min — most shift blocks
    EPIC = "epic"  # >90 min — must be split before scheduling


# ---------------------------------------------------------------------------
# Duration ranges per TaskClass (min_minutes, max_minutes)
# ---------------------------------------------------------------------------

TASK_CLASS_DURATION_RANGES: dict[TaskClass, tuple[float, float]] = {
    TaskClass.ATOMIC: (5.0, 20.0),
    TaskClass.BOUNDED: (20.0, 90.0),
    TaskClass.EPIC: (90.0, 480.0),
}


# ---------------------------------------------------------------------------
# WorkUnit model
# ---------------------------------------------------------------------------


class WorkUnit(BaseModel):
    """A single schedulable unit of work.

    Enriches existing task frontmatter (priority, category, scheduled_at)
    with multi-dimensional scoring, time estimates, and dependency info.
    """

    model_config = ConfigDict(extra="forbid")

    # Identity
    id: str = Field(default="", description="Unique identifier (derived from filename)")
    title: str = Field(default="", description="Human-readable title")
    goal_id: str = Field(default="", description="Parent goal in the goal DAG")
    parent_task_id: str = Field(default="", description="Parent task if this is a subtask")
    task_type: str = Field(default="", description="Maps to existing category field")

    # Classification
    work_mode: WorkMode = Field(default=WorkMode.ADMIN, description="Type of cognitive work")
    priority: str = Field(default="medium", description="Existing priority: high/medium/low")
    commitment_level: CommitmentLevel = Field(
        default=CommitmentLevel.SOFT,
        description="How firmly locked into the schedule",
    )
    task_class: TaskClass = Field(
        default=TaskClass.BOUNDED,
        description="Size bucket for scheduling strategy",
    )

    # Multi-dimensional scoring (all 0.0-1.0)
    urgency: float = Field(default=0.5, ge=0.0, le=1.0, description="Time pressure")
    strategic_value: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Alignment with long-term goals",
    )
    unblock_value: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="How many other tasks this unblocks",
    )
    risk_reduction: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="How much system risk this mitigates",
    )

    # Dependencies
    dependency_ids: list[str] = Field(
        default_factory=list,
        description="Task IDs this depends on",
    )

    # Three-point time estimates (PERT-style)
    estimated_optimistic_min: float = Field(
        default=10.0,
        ge=1.0,
        description="Best-case duration in minutes",
    )
    estimated_expected_min: float = Field(
        default=30.0,
        ge=1.0,
        description="Most-likely duration in minutes",
    )
    estimated_pessimistic_min: float = Field(
        default=60.0,
        ge=1.0,
        description="Worst-case duration in minutes",
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Confidence in the estimate",
    )
    success_probability: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Probability of successful completion",
    )
    context_switch_cost_min: float = Field(
        default=5.0,
        ge=0.0,
        description="Minutes lost when context-switching to this task",
    )

    # Scheduling metadata
    deferment_age_days: float = Field(
        default=0.0,
        ge=0.0,
        description="How many days this task has been deferred",
    )
    hard_deadline: datetime | None = Field(
        default=None,
        description="Absolute deadline (None = no deadline)",
    )
    must_do_today: bool = Field(default=False, description="Forced into today's schedule")
    schedulable_now: bool = Field(default=True, description="Ready for scheduling")

    # Execution guidance
    done_condition: str = Field(default="", description="What 'done' looks like")
    fallback_plan: str = Field(default="", description="What to do if blocked")

    # Passthrough from existing frontmatter
    auto_execute: bool = Field(default=True, description="Preserve existing auto_execute gating")
    generated_at: str = Field(default="", description="Timestamp from original task")
    source: str = Field(default="", description="Origin of the task")
    file_path: str = Field(default="", description="Path to the TASKS/*.md file")

    # -----------------------------------------------------------------------
    # Validators
    # -----------------------------------------------------------------------

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, v: object) -> str:
        """Accept various priority formats, normalize to high/medium/low."""
        s = str(v).strip().lower()
        if s in ("high", "critical", "urgent"):
            return "high"
        if s in ("low", "minor"):
            return "low"
        return "medium"

    @model_validator(mode="after")
    def check_estimate_ordering(self) -> WorkUnit:
        """Ensure pessimistic >= expected >= optimistic."""
        if self.estimated_expected_min < self.estimated_optimistic_min:
            raise ValueError(
                f"expected ({self.estimated_expected_min}) must be >= optimistic ({self.estimated_optimistic_min})"
            )
        if self.estimated_pessimistic_min < self.estimated_expected_min:
            raise ValueError(
                f"pessimistic ({self.estimated_pessimistic_min}) must be >= expected ({self.estimated_expected_min})"
            )
        return self

    # -----------------------------------------------------------------------
    # Factory
    # -----------------------------------------------------------------------

    @classmethod
    def from_frontmatter(cls, frontmatter: dict, file_path: str = "") -> WorkUnit:
        """Create a WorkUnit from existing task YAML frontmatter.

        Maps known frontmatter keys to WorkUnit fields with sensible defaults.
        Unknown keys are silently ignored (they stay in the original file).
        """
        fm = frontmatter or {}

        # Derive ID from file_path if available
        task_id = ""
        if file_path:
            # Strip directory and .md extension
            task_id = os.path.splitext(os.path.basename(file_path))[0]

        # Map priority string to urgency score
        prio_str = str(fm.get("priority", "medium")).lower()
        urgency_map = {"high": 0.8, "critical": 1.0, "urgent": 0.9, "medium": 0.5, "low": 0.2}
        urgency = urgency_map.get(prio_str, 0.5)

        # Map shift_block type to work_mode heuristic
        block_type = str(fm.get("type", ""))
        shift_block = fm.get("shift_block")

        # Default work mode based on block number patterns
        work_mode = WorkMode.ADMIN
        if block_type == "shift_block" and shift_block is not None:
            try:
                block = int(shift_block) if shift_block else 0
            except (ValueError, TypeError):
                block = 0
            if block == 1:
                work_mode = WorkMode.MONITORING  # System health
            elif block in (3, 10, 11):
                work_mode = WorkMode.DEEP_IMPLEMENTATION
            elif block == 4:
                work_mode = WorkMode.RESEARCH
            elif block in (5, 15):
                work_mode = WorkMode.ANALYSIS  # Free will / autonomy
            elif block == 6:
                work_mode = WorkMode.REVIEW  # Testing & quality
            elif block == 7:
                work_mode = WorkMode.ADMIN  # Memory hygiene
            elif block in (8, 16):
                work_mode = WorkMode.COMMUNICATION  # Session wrap

        # Map category if present
        task_type = str(fm.get("category", fm.get("type", "")))

        return cls(
            id=task_id,
            title=str(fm.get("title", task_id)),
            task_type=task_type,
            work_mode=work_mode,
            priority=str(fm.get("priority", "medium")),
            urgency=urgency,
            auto_execute=bool(fm.get("auto_execute", True)),
            generated_at=str(fm.get("generated_at", "")),
            source=str(fm.get("source", "")),
            file_path=file_path,
            must_do_today=(block_type == "shift_block" and shift_block is not None),
            schedulable_now=True,
        )
