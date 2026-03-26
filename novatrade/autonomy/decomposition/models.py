"""Pydantic v2 models for goal decomposition."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class SubGoalStatus(str, Enum):
    """Lifecycle states for a sub-goal."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class SubGoal(BaseModel):
    """A decomposed piece of a high-level goal."""

    sub_goal_id: str
    parent_goal_id: str
    dimension: str  # which scoring dimension this serves
    title: str
    description: str = ""
    status: SubGoalStatus = SubGoalStatus.NOT_STARTED
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    dependencies: list[str] = Field(default_factory=list)  # sub_goal_ids
    tasks_generated: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    priority: int = Field(default=5, ge=1, le=10)  # 1=highest

    @model_validator(mode="after")
    def sync_status_progress(self) -> SubGoal:
        """Auto-complete when progress hits 1.0."""
        if self.progress >= 1.0 and self.status != SubGoalStatus.COMPLETED:
            self.status = SubGoalStatus.COMPLETED
            if self.completed_at is None:
                self.completed_at = datetime.now(timezone.utc)
        return self


class GoalDecomposition(BaseModel):
    """Full decomposition tree for a goal."""

    goal_id: str
    goal_description: str
    sub_goals: list[SubGoal] = Field(default_factory=list)
    dag_edges: list[tuple[str, str]] = Field(default_factory=list)  # (prerequisite, dependent)
    decomposed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = 1

    @model_validator(mode="after")
    def validate_dag(self) -> GoalDecomposition:
        """Ensure all edge references point to existing sub-goals."""
        ids = {sg.sub_goal_id for sg in self.sub_goals}
        for src, dst in self.dag_edges:
            if src not in ids:
                raise ValueError(f"DAG edge source '{src}' not in sub_goals")
            if dst not in ids:
                raise ValueError(f"DAG edge target '{dst}' not in sub_goals")
            if src == dst:
                raise ValueError(f"Self-loop detected: '{src}'")
        return self

    @property
    def completion_pct(self) -> float:
        """Overall completion percentage."""
        if not self.sub_goals:
            return 0.0
        return sum(sg.progress for sg in self.sub_goals) / len(self.sub_goals)

    def get_subgoal(self, sub_goal_id: str) -> SubGoal | None:
        """Look up a sub-goal by ID."""
        for sg in self.sub_goals:
            if sg.sub_goal_id == sub_goal_id:
                return sg
        return None
