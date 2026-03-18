"""Contract output schemas -- replaces regex-based CONTRACT block parsing."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ContractConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ContractField(BaseModel):
    """A single key-value field in a contract."""

    key: str
    value: str


class ContractOutput(BaseModel):
    """Structured contract output -- replaces ## CONTRACT markdown blocks."""

    summary: str = Field(..., max_length=2000, description="What was done")
    files_changed: list[str] = Field(default_factory=list, description="Files modified")
    verification: str = Field(default="", max_length=2000, description="How to verify the work")
    confidence: ContractConfidence = Field(default=ContractConfidence.MEDIUM)
    tests_passed: bool | None = Field(default=None, description="Whether tests passed")
    test_count: int | None = Field(default=None, ge=0, description="Number of tests run")
    lint_clean: bool | None = Field(default=None, description="Whether linting passed")
    extra_fields: list[ContractField] = Field(default_factory=list, description="Additional contract fields")

    # Enhanced contract fields (Phase 5.5)
    max_retries: int | None = Field(default=None, ge=0, le=10, description="Max retry attempts before failing")
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600, description="Max execution time")
    success_criteria: list[str] = Field(default_factory=list, description="Machine-checkable success conditions")
    resource_constraints: dict[str, Any] = Field(
        default_factory=dict,
        description="Resource limits e.g. {'max_tokens': 4000, 'max_files': 10}",
    )
    preconditions: list[str] = Field(default_factory=list, description="Conditions that must be true before execution")
    postconditions: list[str] = Field(default_factory=list, description="Conditions that must be true after execution")
    rollback_steps: list[str] = Field(default_factory=list, description="Steps to undo the work if needed")

    @field_validator("files_changed", mode="before")
    @classmethod
    def parse_files_string(cls, v):
        if isinstance(v, str):
            return [f.strip() for f in v.split(",") if f.strip()]
        return v

    def to_markdown(self) -> str:
        """Render as the legacy ## CONTRACT markdown format for backwards compatibility."""
        lines = ["## CONTRACT", f"summary: {self.summary}"]
        if self.files_changed:
            lines.append(f"files_changed: {', '.join(self.files_changed)}")
        if self.verification:
            lines.append(f"verification: {self.verification}")
        lines.append(f"confidence: {self.confidence.value}")
        if self.tests_passed is not None:
            lines.append(f"tests_passed: {self.tests_passed}")
        if self.test_count is not None:
            lines.append(f"test_count: {self.test_count}")
        if self.lint_clean is not None:
            lines.append(f"lint_clean: {self.lint_clean}")
        if self.max_retries is not None:
            lines.append(f"max_retries: {self.max_retries}")
        if self.timeout_seconds is not None:
            lines.append(f"timeout_seconds: {self.timeout_seconds}")
        if self.success_criteria:
            lines.append(f"success_criteria: {'; '.join(self.success_criteria)}")
        if self.resource_constraints:
            lines.append(f"resource_constraints: {json.dumps(self.resource_constraints)}")
        if self.preconditions:
            lines.append(f"preconditions: {'; '.join(self.preconditions)}")
        if self.postconditions:
            lines.append(f"postconditions: {'; '.join(self.postconditions)}")
        if self.rollback_steps:
            lines.append(f"rollback_steps: {'; '.join(self.rollback_steps)}")
        for ef in self.extra_fields:
            lines.append(f"{ef.key}: {ef.value}")
        return "\n".join(lines)
