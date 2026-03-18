"""Pydantic v2 schema for golden regression test cases.

Each GoldenTestCase represents a canonical input/output pair used to validate
that NovaCore agent outputs maintain quality over time.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# Known categories for golden test cases.
VALID_CATEGORIES = frozenset(
    {
        "heartbeat",
        "codegen",
        "research",
        "plan",
        "bugfix",
        "novatrade",
    }
)

# Known expected-property tags that structural checks can evaluate.
VALID_PROPERTIES = frozenset(
    {
        # Heartbeat
        "contains_metrics",
        "has_service_status",
        "status_healthy",
        "has_failed_checks",
        "has_repair_suggestions",
        "has_resource_warnings",
        "has_warning_thresholds",
        "has_trading_metrics",
        "has_equity_drawdown",
        # Codegen
        "valid_python",
        "has_docstring",
        "has_tests",
        "root_cause_identified",
        "fix_applied",
        "tests_pass",
        "pydantic_model",
        "has_validators",
        "error_handling",
        "thread_safe",
        # Research
        "has_citations",
        "structured_findings",
        "recommendations",
        "vulnerability_list",
        "severity_ratings",
        "remediation",
        "endpoints_listed",
        "auth_methods",
        "code_examples",
        "categorized_practices",
        # Plan
        "phases_listed",
        "dependencies_shown",
        "estimates",
        "rollback_strategy",
        "testing_plan",
        "risk_assessment",
        "components_defined",
        "interfaces_specified",
        "tasks_prioritized",
        "acceptance_criteria",
        # Bugfix
        "root_cause",
        "fix_description",
        "test_added",
        "bottleneck_identified",
        "optimization_applied",
        # NovaTrade
        "entry_rules",
        "exit_rules",
        "risk_params",
        "drawdown_limits",
        "position_sizing",
        "ftmo_compliance",
        # Adversarial / edge-case
        "input_sanitized",
        "injection_rejected",
        "handles_malformed_input",
    }
)


class GoldenTestCase(BaseModel):
    """A single golden regression test case.

    Attributes:
        id: Unique identifier, e.g. ``heartbeat_001``.
        category: One of the six NovaCore output categories.
        input_prompt: The prompt or task description fed to the agent.
        expected_output: A realistic summary of the expected agent output.
        expected_properties: Checkable property tags for structural validation.
        metadata: Optional key-value pairs for filtering or annotation.
    """

    id: str = Field(..., min_length=1, description="Unique test-case identifier")
    category: str = Field(..., description="Output category")
    input_prompt: str = Field(..., min_length=10, description="Agent input prompt")
    expected_output: str = Field(..., min_length=50, description="Expected output summary (200-500 chars)")
    expected_properties: list[str] = Field(..., min_length=1, description="Structural property tags to verify")
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("category")
    @classmethod
    def category_must_be_valid(cls, v: str) -> str:
        if v not in VALID_CATEGORIES:
            raise ValueError(f"category must be one of {sorted(VALID_CATEGORIES)}, got {v!r}")
        return v

    @field_validator("expected_properties")
    @classmethod
    def properties_must_be_known(cls, v: list[str]) -> list[str]:
        unknown = set(v) - VALID_PROPERTIES
        if unknown:
            raise ValueError(f"unknown properties: {sorted(unknown)}. Valid: {sorted(VALID_PROPERTIES)}")
        return v
