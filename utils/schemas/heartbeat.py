"""Heartbeat cycle decision schemas."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class WriteStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"


class CycleOutcome(BaseModel):
    """Outcome of a research or planning cycle."""

    title: str = Field(..., max_length=200, description="Brief title of what the cycle produced")
    summary: str = Field(default="", max_length=2000, description="Detailed summary of findings or plan")
    vault_write: WriteStatus = Field(default=WriteStatus.UNKNOWN, description="Whether vault write succeeded")
    memory_write: WriteStatus = Field(default=WriteStatus.UNKNOWN, description="Whether memory write succeeded")
    artifacts: list[str] = Field(default_factory=list, description="Files created or modified")
    next_steps: list[str] = Field(default_factory=list, description="Suggested follow-up actions")


class HeartbeatDecision(BaseModel):
    """Decision output from heartbeat agent."""

    action: str = Field(..., description="Action to take: 'research', 'planning', 'task', 'idle', 'maintenance'")
    reason: str = Field(default="", max_length=500, description="Why this action was chosen")
    priority: int = Field(default=5, ge=1, le=10, description="Priority level 1-10")
    target: str = Field(default="", description="Target task or topic if applicable")
    estimated_duration_minutes: int = Field(default=30, ge=1, le=180, description="Estimated time needed")
