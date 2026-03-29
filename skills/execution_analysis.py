"""Execution analysis schemas for skill feedback loop.

Phase 2 of Self-Evolving Skills — structured post-execution analysis
that feeds back into skill quality tracking.

Uses Pydantic BaseModel following the project convention in utils/schemas/task.py.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Literal


class SkillJudgment(BaseModel):
    """Per-skill execution judgment."""

    skill_name: str
    skill_id: str = ""  # may be empty if skill not in version store
    applied: bool = False  # was the skill actually used during execution
    completed: bool = False  # did the skill-guided work finish successfully
    failure_reason: str = ""  # why it failed, empty if completed
    quality_score: float = Field(default=0.5, ge=0.0, le=1.0)
    tool_issues: list[str] = Field(default_factory=list)


class EvolutionSuggestion(BaseModel):
    """Suggested evolution action from analysis."""

    type: Literal["FIX", "DERIVED", "CAPTURED"]
    target_skill_name: str
    target_skill_id: str = ""
    direction: str  # what to change
    priority: int = Field(default=3, ge=1, le=5)


class ExecutionAnalysis(BaseModel):
    """Full analysis of a skill-guided task execution."""

    task_id: str
    task_description: str = ""
    skill_judgments: list[SkillJudgment] = Field(default_factory=list)
    evolution_suggestions: list[EvolutionSuggestion] = Field(default_factory=list)
    overall_quality: float = Field(default=0.5, ge=0.0, le=1.0)
    analysis_timestamp: str = ""  # ISO 8601
    raw_response: str = ""  # for debugging


class SkillHealth(BaseModel):
    """Health metrics for a single skill."""

    skill_name: str
    skill_id: str
    selections: int = 0
    executions: int = 0
    completions: int = 0
    failures: int = 0
    fallbacks: int = 0
    applied_rate: float = 0.0  # executions / selections
    completion_rate: float = 0.0  # completions / executions
    effective_rate: float = 0.0  # completions / selections
    fallback_rate: float = 0.0  # fallbacks / selections
    avg_quality_score: float = 0.0
    is_healthy: bool = True
    health_issues: list[str] = Field(default_factory=list)
