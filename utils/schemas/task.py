"""Task execution result schemas."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TaskVerification(BaseModel):
    """Verification step for a completed task."""

    description: str = Field(..., max_length=500)
    passed: bool = Field(...)
    detail: str = Field(default="", max_length=1000)


class TaskResult(BaseModel):
    """Structured result from a task execution."""

    summary: str = Field(..., max_length=2000, description="What was accomplished")
    files_changed: list[str] = Field(default_factory=list, description="List of files created or modified")
    verification: list[TaskVerification] = Field(
        default_factory=list, description="Verification steps and their outcomes"
    )
    confidence: Confidence = Field(default=Confidence.MEDIUM, description="Confidence in the result")
    errors: list[str] = Field(default_factory=list, description="Any errors encountered")
    warnings: list[str] = Field(default_factory=list, description="Any warnings")
    next_actions: list[str] = Field(default_factory=list, description="Suggested follow-up actions")

    @field_validator("files_changed", mode="before")
    @classmethod
    def parse_files_string(cls, v):
        """Accept comma-separated string or list."""
        if isinstance(v, str):
            return [f.strip() for f in v.split(",") if f.strip()]
        return v
