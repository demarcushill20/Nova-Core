"""Task-completion contract tests for NovaCore agent outputs.

Validates that outputs meet the structural contracts defined for each
task type:
- Heartbeat: system metrics present and parseable
- Code generation: valid Python syntax, includes tests
- Research: structured findings with citations

All basic tests are deterministic. LLM-graded completion tests are
behind ``@pytest.mark.eval_llm``.
"""

from __future__ import annotations

import ast
import json
import re

import pytest
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
pytestmark = pytest.mark.eval
# ---------------------------------------------------------------------------


# ===================================================================
# Pydantic schemas for structural validation
# ===================================================================


class ServiceStatus(BaseModel):
    name: str
    status: str


class SystemMetrics(BaseModel):
    cpu_percent: float
    memory_used_gb: float
    memory_total_gb: float
    disk_used_gb: float
    disk_total_gb: float
    uptime_seconds: int


class HeartbeatSchema(BaseModel):
    timestamp: str
    system_metrics: SystemMetrics
    services: list[ServiceStatus]
    open_threads: list[str] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


# ===================================================================
# Heartbeat completion contracts
# ===================================================================


class TestHeartbeatCompletion:
    """Heartbeat outputs must contain parseable system metrics."""

    def test_markdown_has_all_required_sections(self, heartbeat_output: str) -> None:
        required_sections = [
            r"system\s+metrics",
            r"service\s+status",
            r"next\s+actions",
        ]
        for pattern in required_sections:
            assert re.search(pattern, heartbeat_output, re.IGNORECASE), f"Missing required section matching '{pattern}'"

    def test_cpu_value_is_numeric(self, heartbeat_output: str) -> None:
        match = re.search(r"cpu.*?(\d+)%", heartbeat_output, re.IGNORECASE)
        assert match, "CPU usage must be reported as a percentage"
        cpu_val = int(match.group(1))
        assert 0 <= cpu_val <= 100, f"CPU value {cpu_val}% out of range [0, 100]"

    def test_memory_values_are_numeric(self, heartbeat_output: str) -> None:
        match = re.search(
            r"memory.*?(\d+\.?\d*)\s*GB\s*/\s*(\d+\.?\d*)\s*GB",
            heartbeat_output,
            re.IGNORECASE,
        )
        assert match, "Memory must be reported as X GB / Y GB"
        used = float(match.group(1))
        total = float(match.group(2))
        assert 0 < used <= total, f"Memory used ({used}) must be <= total ({total})"

    def test_disk_values_are_numeric(self, heartbeat_output: str) -> None:
        match = re.search(
            r"disk.*?(\d+\.?\d*)\s*GB\s*/\s*(\d+\.?\d*)\s*GB",
            heartbeat_output,
            re.IGNORECASE,
        )
        assert match, "Disk must be reported as X GB / Y GB"
        used = float(match.group(1))
        total = float(match.group(2))
        assert 0 < used <= total, f"Disk used ({used}) must be <= total ({total})"

    def test_json_validates_against_schema(self, heartbeat_json_output: str) -> None:
        """Structured JSON heartbeat must conform to HeartbeatSchema."""
        data = json.loads(heartbeat_json_output)
        hb = HeartbeatSchema(**data)
        assert hb.timestamp, "Timestamp must not be empty"
        assert hb.system_metrics.cpu_percent >= 0
        assert len(hb.services) >= 1, "At least one service must be listed"

    def test_json_services_have_valid_status(self, heartbeat_json_output: str) -> None:
        data = json.loads(heartbeat_json_output)
        hb = HeartbeatSchema(**data)
        valid_statuses = {"running", "stopped", "error", "degraded", "starting"}
        for svc in hb.services:
            assert svc.status in valid_statuses, f"Service '{svc.name}' has invalid status '{svc.status}'"

    def test_malformed_json_rejected(self, malformed_json_output: str) -> None:
        """Malformed JSON should fail to parse."""
        with pytest.raises(json.JSONDecodeError):
            json.loads(malformed_json_output)


# ===================================================================
# Code generation completion contracts
# ===================================================================


class TestCodegenCompletion:
    """Code generation outputs must contain syntactically valid Python."""

    def _extract_python_blocks(self, output: str) -> list[str]:
        """Extract all ```python ... ``` code blocks."""
        return re.findall(r"```python\s*\n(.*?)```", output, re.DOTALL)

    def test_has_at_least_two_code_blocks(self, codegen_output: str) -> None:
        """One for implementation, one for tests."""
        blocks = self._extract_python_blocks(codegen_output)
        assert len(blocks) >= 2, f"Expected at least 2 code blocks (impl + tests), found {len(blocks)}"

    def test_implementation_is_valid_python(self, codegen_output: str) -> None:
        """First code block must parse as valid Python AST."""
        blocks = self._extract_python_blocks(codegen_output)
        assert blocks, "No Python code blocks found"
        try:
            ast.parse(blocks[0])
        except SyntaxError as e:
            pytest.fail(f"Implementation code has syntax error: {e}")

    def test_test_code_is_valid_python(self, codegen_output: str) -> None:
        """Test code block must parse as valid Python AST."""
        blocks = self._extract_python_blocks(codegen_output)
        assert len(blocks) >= 2, "No test code block found"
        try:
            ast.parse(blocks[1])
        except SyntaxError as e:
            pytest.fail(f"Test code has syntax error: {e}")

    def test_implementation_defines_requested_function(self, codegen_output: str) -> None:
        """The implementation should define a function related to the task."""
        blocks = self._extract_python_blocks(codegen_output)
        assert blocks, "No code blocks found"
        tree = ast.parse(blocks[0])
        func_names = [node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        assert func_names, "Implementation must define at least one function"

    def test_tests_define_test_functions(self, codegen_output: str) -> None:
        """Test code block must contain test_* functions."""
        blocks = self._extract_python_blocks(codegen_output)
        assert len(blocks) >= 2, "No test code block found"
        tree = ast.parse(blocks[1])
        test_funcs = [
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        ]
        assert len(test_funcs) >= 1, "Test code must contain at least one test_* function"

    def test_implementation_has_imports(self, codegen_output: str) -> None:
        """Implementation should import necessary modules."""
        blocks = self._extract_python_blocks(codegen_output)
        assert blocks, "No code blocks found"
        tree = ast.parse(blocks[0])
        imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        assert imports, "Implementation should have import statements"

    def test_implementation_has_docstring(self, codegen_output: str) -> None:
        """Main function should have a docstring."""
        blocks = self._extract_python_blocks(codegen_output)
        assert blocks, "No code blocks found"
        tree = ast.parse(blocks[0])
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                docstring = ast.get_docstring(node)
                if docstring:
                    return  # pass — at least one function has a docstring
        pytest.fail("No function in the implementation has a docstring")


# ===================================================================
# Research completion contracts
# ===================================================================


class TestResearchCompletion:
    """Research outputs must meet analytical structure contracts."""

    def test_has_minimum_findings(self, research_output: str) -> None:
        """At least 3 distinct findings should be present."""
        findings = re.findall(r"^\s*\d+\.\s+\*\*", research_output, re.MULTILINE)
        assert len(findings) >= 3, f"Expected at least 3 findings, found {len(findings)}"

    def test_each_finding_has_body(self, research_output: str) -> None:
        """Each numbered finding should have explanatory text."""
        findings = re.split(r"\n\s*\d+\.\s+\*\*", research_output)
        # Skip everything before the first finding
        for i, finding in enumerate(findings[1:], start=1):
            # Strip the bold title and check remaining text
            body = re.sub(r"^\*\*[^*]*\*\*", "", finding).strip()
            assert len(body) >= 20, f"Finding #{i} has insufficient body text ({len(body)} chars)"

    def test_citations_section_has_entries(self, research_output: str) -> None:
        """Citations section must list at least 2 references."""
        citations_match = re.search(
            r"#+\s*Citations\s*\n(.*?)(?=\n#+|\Z)",
            research_output,
            re.DOTALL | re.IGNORECASE,
        )
        assert citations_match, "Citations section not found"
        citation_text = citations_match.group(1)
        entries = re.findall(r"^\s*-\s+", citation_text, re.MULTILINE)
        assert len(entries) >= 2, f"Citations section should have at least 2 entries (found {len(entries)})"

    def test_recommendations_are_present(self, research_output: str) -> None:
        rec_match = re.search(
            r"#+\s*Recommendations\s*\n(.*?)(?=\n#+|\Z)",
            research_output,
            re.DOTALL | re.IGNORECASE,
        )
        assert rec_match, "Recommendations section not found"
        items = re.findall(r"^\s*-\s+", rec_match.group(1), re.MULTILINE)
        assert len(items) >= 1, "At least one recommendation must be listed"

    def test_confidence_assessment_present(self, research_output: str) -> None:
        """Output should state a confidence level."""
        confidence_match = re.search(
            r"#+\s*Confidence\s*\n(.*?)(?=\n#+|\Z)",
            research_output,
            re.DOTALL | re.IGNORECASE,
        )
        assert confidence_match, "Confidence section not found"
        confidence_text = confidence_match.group(1).strip().lower()
        valid_levels = {"high", "medium", "low", "very high", "moderate"}
        has_level = any(level in confidence_text for level in valid_levels)
        assert has_level, f"Confidence section should contain a level (high/medium/low), got: '{confidence_text}'"

    def test_findings_cross_reference_citations(self, research_output: str) -> None:
        """At least one finding should reference a source inline."""
        # Look in the Key Findings section for inline source references
        findings_match = re.search(
            r"#+\s*Key\s+Findings\s*\n(.*?)(?=\n#+)",
            research_output,
            re.DOTALL | re.IGNORECASE,
        )
        assert findings_match, "Key Findings section not found"
        findings_text = findings_match.group(1)
        inline_refs = re.findall(r"\[Source:", findings_text)
        assert len(inline_refs) >= 1, "At least one finding should have an inline [Source:] reference"


# ===================================================================
# Cross-category completion: DeepEval pattern matching
# ===================================================================


class TestDeepEvalPatternCompletion:
    """Use DeepEval's deterministic metrics where applicable."""

    def test_heartbeat_matches_report_pattern(self, heartbeat_output: str) -> None:
        """Heartbeat matches the full-report pattern via DeepEval PatternMatchMetric."""
        from deepeval.metrics import PatternMatchMetric
        from deepeval.test_case import LLMTestCase

        # PatternMatchMetric uses fullmatch, so we wrap with .*
        metric = PatternMatchMetric(
            pattern=r".*Heartbeat Report.*System Metrics.*Service Status.*",
            ignore_case=True,
        )
        test_case = LLMTestCase(
            input="Generate a heartbeat report",
            actual_output=heartbeat_output.replace("\n", " "),
        )
        metric.measure(test_case)
        assert metric.is_successful(), f"Heartbeat did not match report pattern (score={metric.score})"

    def test_codegen_matches_code_pattern(self, codegen_output: str) -> None:
        """Codegen output matches implementation + tests pattern."""
        from deepeval.metrics import PatternMatchMetric
        from deepeval.test_case import LLMTestCase

        metric = PatternMatchMetric(
            pattern=r".*Implementation.*```python.*```.*Tests.*```python.*```.*",
            ignore_case=True,
        )
        test_case = LLMTestCase(
            input="Generate code with tests",
            actual_output=codegen_output.replace("\n", " "),
        )
        metric.measure(test_case)
        assert metric.is_successful(), f"Codegen did not match expected pattern (score={metric.score})"

    def test_research_matches_academic_pattern(self, research_output: str) -> None:
        """Research output matches findings + citations pattern."""
        from deepeval.metrics import PatternMatchMetric
        from deepeval.test_case import LLMTestCase

        metric = PatternMatchMetric(
            pattern=r".*Key Findings.*Recommendations.*Citations.*",
            ignore_case=True,
        )
        test_case = LLMTestCase(
            input="Research circuit breaker patterns",
            actual_output=research_output.replace("\n", " "),
        )
        metric.measure(test_case)
        assert metric.is_successful(), f"Research did not match academic pattern (score={metric.score})"

    def test_json_heartbeat_exact_match(self, heartbeat_json_output: str) -> None:
        """JSON heartbeat round-trips through parse and re-serialize."""
        from deepeval.metrics import ExactMatchMetric
        from deepeval.test_case import LLMTestCase

        # Parse and re-serialize to normalize
        parsed = json.loads(heartbeat_json_output)
        normalized = json.dumps(parsed, indent=2)

        metric = ExactMatchMetric()
        test_case = LLMTestCase(
            input="Normalize heartbeat JSON",
            actual_output=normalized,
            expected_output=normalized,
        )
        metric.measure(test_case)
        assert metric.is_successful(), "JSON round-trip should produce exact match"


# ===================================================================
# LLM-graded task completion
# ===================================================================


class TestTaskCompletionLLM:
    """LLM-graded task completion checks (requires OPENAI_API_KEY)."""

    pytestmark = [pytest.mark.eval_llm]  # noqa: RUF012

    @pytest.fixture(autouse=True)
    def _require_llm(self) -> None:
        import os

        if not os.environ.get("OPENAI_API_KEY"):
            pytest.skip("OPENAI_API_KEY not set — skipping LLM eval")

    def test_heartbeat_task_complete(self, heartbeat_output: str) -> None:
        """GEval: did the agent fully complete the heartbeat task?"""
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams

        metric = GEval(
            name="heartbeat-task-completion",
            evaluation_params=[
                LLMTestCaseParams.INPUT,
                LLMTestCaseParams.ACTUAL_OUTPUT,
            ],
            evaluation_steps=[
                "The task was to produce a system heartbeat report.",
                "Check: Does the output report CPU, memory, and disk usage?",
                "Check: Does the output list service statuses?",
                "Check: Does the output identify open threads and next actions?",
                "Score 1.0 if the task is fully completed, 0.5 if partially, 0.0 if not.",
            ],
            threshold=0.7,
        )
        test_case = LLMTestCase(
            input="Generate a comprehensive system heartbeat report.",
            actual_output=heartbeat_output,
        )
        metric.measure(test_case)
        assert metric.is_successful(), f"Heartbeat task completion score {metric.score:.2f} below 0.7"

    def test_codegen_task_complete(self, codegen_output: str) -> None:
        """GEval: did the agent fully complete the code generation task?"""
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams

        metric = GEval(
            name="codegen-task-completion",
            evaluation_params=[
                LLMTestCaseParams.INPUT,
                LLMTestCaseParams.ACTUAL_OUTPUT,
            ],
            evaluation_steps=[
                "The task was to implement a retry decorator with exponential backoff.",
                "Check: Is there a working retry decorator implementation?",
                "Check: Does it support max_attempts, base_delay, and backoff_factor?",
                "Check: Are unit tests provided that cover success and retry paths?",
                "Score 1.0 if fully complete, 0.5 if partially, 0.0 if not.",
            ],
            threshold=0.7,
        )
        test_case = LLMTestCase(
            input="Implement a retry decorator with exponential backoff, with tests.",
            actual_output=codegen_output,
        )
        metric.measure(test_case)
        assert metric.is_successful(), f"Codegen task completion score {metric.score:.2f} below 0.7"

    def test_research_task_complete(self, research_output: str) -> None:
        """GEval: did the agent fully complete the research task?"""
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams

        metric = GEval(
            name="research-task-completion",
            evaluation_params=[
                LLMTestCaseParams.INPUT,
                LLMTestCaseParams.ACTUAL_OUTPUT,
            ],
            evaluation_steps=[
                "The task was to research circuit breaker patterns for distributed systems.",
                "Check: Are there multiple distinct findings with sources?",
                "Check: Are there actionable recommendations?",
                "Check: Is there a confidence assessment?",
                "Score 1.0 if fully complete, 0.5 if partially, 0.0 if not.",
            ],
            threshold=0.7,
        )
        test_case = LLMTestCase(
            input="Research circuit breaker patterns for distributed systems.",
            actual_output=research_output,
        )
        metric.measure(test_case)
        assert metric.is_successful(), f"Research task completion score {metric.score:.2f} below 0.7"
