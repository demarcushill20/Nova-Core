"""Semantic similarity and content-quality evaluation tests.

The *basic* suite uses deterministic checks (no LLM required):
- Non-empty output validation
- Keyword/topic presence
- Structural coherence (sections, formatting)

LLM-powered semantic similarity tests are gated behind ``@pytest.mark.eval_llm``
and require OPENAI_API_KEY to be set.
"""

from __future__ import annotations

import re

import pytest

# ---------------------------------------------------------------------------
# Mark every test in this module with 'eval'
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.eval


# ===================================================================
# Deterministic suite — no LLM required
# ===================================================================


class TestOutputNonEmpty:
    """Agent outputs must never be empty."""

    def test_heartbeat_non_empty(self, heartbeat_output: str) -> None:
        assert heartbeat_output.strip(), "Heartbeat output must not be empty"

    def test_codegen_non_empty(self, codegen_output: str) -> None:
        assert codegen_output.strip(), "Code generation output must not be empty"

    def test_research_non_empty(self, research_output: str) -> None:
        assert research_output.strip(), "Research output must not be empty"

    def test_empty_output_detected(self, empty_output: str) -> None:
        assert not empty_output.strip(), "Empty output fixture should be empty"


class TestKeywordPresence:
    """Outputs must contain keywords semantically related to their input task."""

    # -- Heartbeat ---------------------------------------------------------
    @pytest.mark.parametrize(
        "keyword",
        [
            "cpu",
            "memory",
            "disk",
            "uptime",
            "status",
            "running",
        ],
    )
    def test_heartbeat_contains_system_keywords(self, heartbeat_output: str, keyword: str) -> None:
        assert keyword.lower() in heartbeat_output.lower(), f"Heartbeat output should mention '{keyword}'"

    # -- Code generation ---------------------------------------------------
    @pytest.mark.parametrize(
        "keyword",
        [
            "def ",
            "return",
            "import",
            "```python",
        ],
    )
    def test_codegen_contains_code_keywords(self, codegen_output: str, keyword: str) -> None:
        assert keyword in codegen_output, f"Code generation output should contain '{keyword}'"

    # -- Research ----------------------------------------------------------
    @pytest.mark.parametrize(
        "keyword",
        [
            "findings",
            "source",
            "recommendations",
            "citations",
        ],
    )
    def test_research_contains_research_keywords(self, research_output: str, keyword: str) -> None:
        assert keyword.lower() in research_output.lower(), f"Research output should mention '{keyword}'"


class TestTopicCoherence:
    """Outputs should stay on-topic relative to the task domain."""

    def test_heartbeat_mentions_services(self, heartbeat_output: str) -> None:
        service_pattern = re.compile(r"novacore-\w+|service|watcher|telegram", re.IGNORECASE)
        assert service_pattern.search(heartbeat_output), "Heartbeat should reference NovaCore services"

    def test_codegen_has_function_definition(self, codegen_output: str) -> None:
        assert re.search(r"def \w+\(", codegen_output), (
            "Code generation output should contain at least one function definition"
        )

    def test_research_has_numbered_findings(self, research_output: str) -> None:
        numbered = re.findall(r"^\s*\d+\.\s+\*\*", research_output, re.MULTILINE)
        assert len(numbered) >= 2, "Research output should have at least 2 numbered findings"

    def test_research_has_citation_references(self, research_output: str) -> None:
        # Check for [Source: ...] inline references or a Citations section
        inline_refs = re.findall(r"\[Source:.*?\]", research_output)
        has_citation_section = "### Citations" in research_output
        assert inline_refs or has_citation_section, (
            "Research output should include inline [Source:] references or a Citations section"
        )


class TestOutputLength:
    """Outputs should meet minimum length expectations for their type."""

    def test_heartbeat_minimum_length(self, heartbeat_output: str) -> None:
        # A meaningful heartbeat should be at least 200 characters
        assert len(heartbeat_output) >= 200, f"Heartbeat too short ({len(heartbeat_output)} chars); expected >= 200"

    def test_codegen_minimum_length(self, codegen_output: str) -> None:
        # Code output with implementation + tests should be substantial
        assert len(codegen_output) >= 300, f"Codegen too short ({len(codegen_output)} chars); expected >= 300"

    def test_research_minimum_length(self, research_output: str) -> None:
        assert len(research_output) >= 300, f"Research too short ({len(research_output)} chars); expected >= 300"


# ===================================================================
# LLM-powered suite — requires OPENAI_API_KEY
# ===================================================================


class TestSemanticSimilarityLLM:
    """Use DeepEval GEval to measure semantic quality (requires LLM)."""

    pytestmark = [pytest.mark.eval_llm]  # noqa: RUF012

    @pytest.fixture(autouse=True)
    def _require_llm(self) -> None:
        api_key = __import__("os").environ.get("OPENAI_API_KEY")
        if not api_key:
            pytest.skip("OPENAI_API_KEY not set — skipping LLM eval")

    def test_heartbeat_relevance_geval(self, heartbeat_output: str) -> None:
        """GEval: is the heartbeat output relevant to a system health check?"""
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams

        metric = GEval(
            name="heartbeat-relevance",
            evaluation_params=[
                LLMTestCaseParams.INPUT,
                LLMTestCaseParams.ACTUAL_OUTPUT,
            ],
            evaluation_steps=[
                "Check that the output reports system metrics (CPU, memory, disk).",
                "Check that the output lists service statuses.",
                "Check that the output identifies anomalies or confirms none.",
                "Score 1.0 if all criteria met, 0.5 if partially met, 0.0 if irrelevant.",
            ],
            threshold=0.5,
        )
        test_case = LLMTestCase(
            input="Generate a system heartbeat report for NovaCore.",
            actual_output=heartbeat_output,
        )
        metric.measure(test_case)
        assert metric.is_successful(), f"Heartbeat relevance score {metric.score:.2f} below threshold"

    def test_codegen_quality_geval(self, codegen_output: str) -> None:
        """GEval: does the code generation output contain valid, tested code?"""
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams

        metric = GEval(
            name="codegen-quality",
            evaluation_params=[
                LLMTestCaseParams.INPUT,
                LLMTestCaseParams.ACTUAL_OUTPUT,
            ],
            evaluation_steps=[
                "Check that the output contains a Python function implementation.",
                "Check that the code includes type hints and docstrings.",
                "Check that the output contains unit tests for the implementation.",
                "Score 1.0 if all criteria met, 0.5 if partially, 0.0 if missing.",
            ],
            threshold=0.5,
        )
        test_case = LLMTestCase(
            input="Implement a retry decorator with exponential backoff, including tests.",
            actual_output=codegen_output,
        )
        metric.measure(test_case)
        assert metric.is_successful(), f"Codegen quality score {metric.score:.2f} below threshold"

    def test_research_quality_geval(self, research_output: str) -> None:
        """GEval: does the research summary have proper structure and citations?"""
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams

        metric = GEval(
            name="research-quality",
            evaluation_params=[
                LLMTestCaseParams.INPUT,
                LLMTestCaseParams.ACTUAL_OUTPUT,
            ],
            evaluation_steps=[
                "Check that the output has numbered key findings.",
                "Check that findings cite sources.",
                "Check that the output includes actionable recommendations.",
                "Score 1.0 if all criteria met, 0.5 if partially, 0.0 if missing.",
            ],
            threshold=0.5,
        )
        test_case = LLMTestCase(
            input="Research circuit breaker patterns for distributed systems.",
            actual_output=research_output,
        )
        metric.measure(test_case)
        assert metric.is_successful(), f"Research quality score {metric.score:.2f} below threshold"
