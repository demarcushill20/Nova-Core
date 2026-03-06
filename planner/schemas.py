"""Data classes for the NovaCore planner subsystem.

Exact field shapes per Phase 5.2 specification.
Phase 5.3 adds ExecutionEvaluation and PlanEvaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskIntent:
    """Parsed intent extracted from a raw task description."""

    task_id: str
    goal: str
    source: str
    priority: str = "normal"
    constraints: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillScore:
    """A skill's relevance score with 4-component breakdown."""

    skill_name: str
    semantic_match: float
    activation_rules: float
    recency: float
    success_rate: float
    total_score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class PlanStep:
    """One step in an execution plan."""

    step_id: str
    skill_name: str
    goal: str
    inputs: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"


@dataclass
class ExecutionPlan:
    """A complete execution plan for a task."""

    plan_id: str
    task_id: str
    strategy: str
    steps: list[PlanStep]
    success_criteria: list[str]
    status: str = "queued"


@dataclass
class StepResult:
    """Result of executing a single plan step."""

    step_id: str
    status: str
    output_path: str | None = None
    contract_valid: bool | None = None
    validation_errors: list[str] = field(default_factory=list)
    retry_count: int = 0


@dataclass
class SupervisorDecision:
    """Decision made by the supervisor after evaluating a step result."""

    action: str  # continue | retry | escalate | fail
    reason: str
    retry_allowed: bool


@dataclass
class ExecutionEvaluation:
    """Evaluation of a single executed plan step.

    Distinguishes execution outcome, validation outcome, and quality score.
    """

    step_id: str
    execution_success: bool
    contract_valid: bool
    tests_passed: bool | None = None
    retry_penalty: float = 0.0
    duration_score: float = 0.0
    verification_score: float = 0.0
    total_score: float = 0.0
    grade: str = "unknown"
    reasons: list[str] = field(default_factory=list)


@dataclass
class PlanEvaluation:
    """Aggregate evaluation of a complete execution plan."""

    plan_id: str
    step_evaluations: list[ExecutionEvaluation]
    aggregate_score: float
    grade: str
    summary: str
    followup_recommended: bool
    followup_reason: str | None = None


@dataclass
class ContractAuditRecord:
    """Audit record for a single output file's contract compliance."""

    output_file: str
    task_id: str | None = None
    has_contract: bool = False
    valid_contract: bool = False
    missing_fields: list[str] = field(default_factory=list)
    detected_timestamp: str | None = None
    classification: str = "unknown"


@dataclass
class ContractAuditSummary:
    """Aggregate summary of a contract compliance audit run."""

    audit_id: str
    total_outputs: int
    valid_contracts: int
    invalid_contracts: int
    no_contract: int
    compliance_rate: float
    missing_field_counts: dict[str, int] = field(default_factory=dict)
    records: list[ContractAuditRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
