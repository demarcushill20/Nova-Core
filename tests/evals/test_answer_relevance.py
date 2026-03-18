"""Structural relevance tests for different NovaCore output categories.

Validates that outputs contain the expected sections, formatting, and
structural elements for their category (heartbeat, codegen, research).

All tests in this module are deterministic — no LLM access required.
"""

from __future__ import annotations

import re

import pytest

# ---------------------------------------------------------------------------
pytestmark = pytest.mark.eval
# ---------------------------------------------------------------------------


class TestHeartbeatStructuralRelevance:
    """Heartbeat outputs must follow a predictable report structure."""

    def test_has_timestamp(self, heartbeat_output: str) -> None:
        # ISO-8601 style timestamp
        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", heartbeat_output), (
            "Heartbeat must contain an ISO-8601 timestamp"
        )

    def test_has_system_metrics_section(self, heartbeat_output: str) -> None:
        assert re.search(r"#+\s*(system\s+)?metrics", heartbeat_output, re.IGNORECASE), (
            "Heartbeat must have a System Metrics section"
        )

    def test_has_service_status_section(self, heartbeat_output: str) -> None:
        assert re.search(r"#+\s*service\s+status", heartbeat_output, re.IGNORECASE), (
            "Heartbeat must have a Service Status section"
        )

    def test_reports_cpu_percentage(self, heartbeat_output: str) -> None:
        assert re.search(r"cpu.*?\d+%", heartbeat_output, re.IGNORECASE), (
            "Heartbeat must report CPU usage as a percentage"
        )

    def test_reports_memory_usage(self, heartbeat_output: str) -> None:
        assert re.search(r"memory.*?\d+\.?\d*\s*GB", heartbeat_output, re.IGNORECASE), (
            "Heartbeat must report memory usage in GB"
        )

    def test_reports_disk_usage(self, heartbeat_output: str) -> None:
        assert re.search(r"disk.*?\d+\.?\d*\s*GB", heartbeat_output, re.IGNORECASE), (
            "Heartbeat must report disk usage in GB"
        )

    def test_has_next_actions(self, heartbeat_output: str) -> None:
        assert re.search(r"#+\s*next\s+actions", heartbeat_output, re.IGNORECASE), (
            "Heartbeat must include a Next Actions section"
        )

    def test_services_listed(self, heartbeat_output: str) -> None:
        """At least one service should be listed with a status."""
        service_line = re.findall(r"(running|stopped|error|degraded)", heartbeat_output, re.IGNORECASE)
        assert len(service_line) >= 1, "Heartbeat must list at least one service with a status"

    def test_anomalies_section_present(self, heartbeat_output: str) -> None:
        assert re.search(r"#+\s*anomal", heartbeat_output, re.IGNORECASE), "Heartbeat must include an Anomalies section"


class TestCodegenStructuralRelevance:
    """Code generation outputs must contain implementation and tests."""

    def test_has_task_description(self, codegen_output: str) -> None:
        assert re.search(r"#+\s*task", codegen_output, re.IGNORECASE), "Codegen must include a Task description section"

    def test_has_implementation_section(self, codegen_output: str) -> None:
        assert re.search(r"#+\s*implementation", codegen_output, re.IGNORECASE), (
            "Codegen must include an Implementation section"
        )

    def test_has_python_code_block(self, codegen_output: str) -> None:
        code_blocks = re.findall(r"```python\s*\n(.*?)```", codegen_output, re.DOTALL)
        assert len(code_blocks) >= 1, "Codegen must contain at least one ```python code block"

    def test_implementation_has_function(self, codegen_output: str) -> None:
        code_blocks = re.findall(r"```python\s*\n(.*?)```", codegen_output, re.DOTALL)
        all_code = "\n".join(code_blocks)
        assert re.search(r"def \w+\(", all_code), "Implementation code must contain at least one function definition"

    def test_has_tests_section(self, codegen_output: str) -> None:
        assert re.search(r"#+\s*tests", codegen_output, re.IGNORECASE), "Codegen must include a Tests section"

    def test_tests_use_assert(self, codegen_output: str) -> None:
        # Find the test code block (after ### Tests header)
        tests_match = re.search(
            r"#+\s*Tests.*?```python\s*\n(.*?)```",
            codegen_output,
            re.DOTALL | re.IGNORECASE,
        )
        assert tests_match, "Could not find test code block"
        test_code = tests_match.group(1)
        assert "assert" in test_code, "Test code must contain assert statements"

    def test_has_docstring(self, codegen_output: str) -> None:
        code_blocks = re.findall(r"```python\s*\n(.*?)```", codegen_output, re.DOTALL)
        all_code = "\n".join(code_blocks)
        assert '"""' in all_code or "'''" in all_code, "Implementation should include docstrings"

    def test_has_type_hints(self, codegen_output: str) -> None:
        code_blocks = re.findall(r"```python\s*\n(.*?)```", codegen_output, re.DOTALL)
        all_code = "\n".join(code_blocks)
        # Look for type hints in function signatures (: type or -> type)
        has_param_hint = re.search(r"def \w+\([^)]*:\s*\w+", all_code)
        has_return_hint = re.search(r"\)\s*->", all_code)
        assert has_param_hint or has_return_hint, "Implementation should include type hints"

    def test_has_verification_section(self, codegen_output: str) -> None:
        assert re.search(r"#+\s*verification", codegen_output, re.IGNORECASE), (
            "Codegen should include a Verification section"
        )


class TestResearchStructuralRelevance:
    """Research outputs must follow academic/analytical structure."""

    def test_has_summary_title(self, research_output: str) -> None:
        assert re.search(r"#+\s*(research\s+)?summary", research_output, re.IGNORECASE), (
            "Research output must have a summary/title header"
        )

    def test_has_key_findings(self, research_output: str) -> None:
        assert re.search(r"#+\s*key\s+findings", research_output, re.IGNORECASE), (
            "Research output must have a Key Findings section"
        )

    def test_findings_are_numbered(self, research_output: str) -> None:
        numbered_items = re.findall(r"^\s*\d+\.\s+", research_output, re.MULTILINE)
        assert len(numbered_items) >= 2, (
            f"Research should have at least 2 numbered findings (found {len(numbered_items)})"
        )

    def test_has_inline_citations(self, research_output: str) -> None:
        """Inline [Source: ...] or similar reference markers."""
        refs = re.findall(r"\[(?:Source|Ref|See):.*?\]", research_output)
        assert len(refs) >= 1, "Research output should contain at least one inline citation"

    def test_has_citations_section(self, research_output: str) -> None:
        assert re.search(
            r"#+\s*citations|#+\s*references|#+\s*bibliography",
            research_output,
            re.IGNORECASE,
        ), "Research output must have a Citations/References section"

    def test_has_recommendations(self, research_output: str) -> None:
        assert re.search(r"#+\s*recommendations", research_output, re.IGNORECASE), (
            "Research output must include Recommendations"
        )

    def test_has_confidence_level(self, research_output: str) -> None:
        assert re.search(r"confidence", research_output, re.IGNORECASE), (
            "Research output should include a confidence assessment"
        )

    def test_recommendations_are_actionable(self, research_output: str) -> None:
        """Recommendations section should contain action verbs."""
        rec_match = re.search(
            r"#+\s*Recommendations\s*\n(.*?)(?=\n#+|\Z)",
            research_output,
            re.DOTALL | re.IGNORECASE,
        )
        assert rec_match, "Could not find Recommendations section"
        rec_text = rec_match.group(1)
        action_verbs = re.findall(
            r"\b(adopt|add|implement|use|integrate|instrument|configure|deploy|review)\b",
            rec_text,
            re.IGNORECASE,
        )
        assert len(action_verbs) >= 1, "Recommendations should contain actionable verbs"
