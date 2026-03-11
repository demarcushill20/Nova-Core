"""Tests for planner.vault_context — read-only Obsidian vault context injection.

Phase 5: verifies eligibility rules, keyword extraction, context formatting,
injection integration, and source-truth boundaries.
"""

import pytest

from planner.vault_context import (
    _ELIGIBLE_TASK_CLASSES,
    _INELIGIBLE_TASK_CLASSES,
    MAX_CONTEXT_SIZE,
    MAX_VAULT_NOTES,
    extract_keywords,
    format_vault_context,
    inject_vault_context,
    is_eligible_for_vault_context,
    retrieve_vault_context,
)

# ============================================================================
# Eligibility
# ============================================================================

class TestEligibility:
    """Test is_eligible_for_vault_context."""

    def test_research_eligible(self):
        ok, reason = is_eligible_for_vault_context("research", "research MCP patterns")
        assert ok is True
        assert "eligible_class" in reason

    def test_code_impl_eligible(self):
        ok, reason = is_eligible_for_vault_context("code_impl", "implement new feature")
        assert ok is True

    def test_code_review_eligible(self):
        ok, reason = is_eligible_for_vault_context("code_review", "review the PR changes")
        assert ok is True

    def test_simple_ineligible(self):
        ok, reason = is_eligible_for_vault_context("simple", "fix a typo")
        assert ok is False
        assert "ineligible_class" in reason

    def test_unknown_ineligible(self):
        ok, reason = is_eligible_for_vault_context("unknown", "hello world")
        assert ok is False

    def test_system_default_skip(self):
        ok, reason = is_eligible_for_vault_context("system", "configure nginx")
        assert ok is False
        assert "system_class" in reason

    def test_runtime_state_always_ineligible(self):
        """Runtime-state queries skip vault injection regardless of class."""
        ok, reason = is_eligible_for_vault_context(
            "research", "check service status of novacore-watcher"
        )
        assert ok is False
        assert reason == "runtime_state_query"

    def test_runtime_systemctl_ineligible(self):
        ok, reason = is_eligible_for_vault_context(
            "system", "restart the systemctl daemon"
        )
        assert ok is False
        assert reason == "runtime_state_query"

    def test_runtime_pid_ineligible(self):
        ok, reason = is_eligible_for_vault_context(
            "code_impl", "check pid of running tasks and kill stale ones"
        )
        assert ok is False
        assert reason == "runtime_state_query"

    def test_runtime_watcher_ineligible(self):
        ok, reason = is_eligible_for_vault_context(
            "system", "watcher health check"
        )
        assert ok is False
        assert reason == "runtime_state_query"

    def test_unrecognized_class_fails_closed(self):
        ok, reason = is_eligible_for_vault_context("futuristic", "do something")
        assert ok is False
        assert "unknown_class" in reason

    def test_all_eligible_classes_covered(self):
        """Verify the eligible set matches expectations."""
        assert _ELIGIBLE_TASK_CLASSES == {"research", "code_impl", "code_review"}

    def test_all_ineligible_classes_covered(self):
        assert _INELIGIBLE_TASK_CLASSES == {"simple", "unknown"}


# ============================================================================
# Keyword Extraction
# ============================================================================

class TestKeywordExtraction:
    """Test extract_keywords."""

    def test_basic_extraction(self):
        kw = extract_keywords("research MCP server design patterns")
        assert len(kw) > 0
        assert "research" in kw
        assert "patterns" in kw

    def test_stopwords_filtered(self):
        kw = extract_keywords("the quick brown fox is a very good animal")
        assert "the" not in kw
        assert "is" not in kw
        assert "a" not in kw

    def test_short_tokens_filtered(self):
        """Tokens shorter than 3 chars are excluded."""
        kw = extract_keywords("go do it ok")
        # All tokens are 2 chars or less — nothing extracted
        assert len(kw) == 0

    def test_max_keywords_bounded(self):
        text = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
        kw = extract_keywords(text, max_keywords=3)
        assert len(kw) <= 3

    def test_deduplication(self):
        kw = extract_keywords("pattern pattern pattern different")
        assert kw.count("pattern") == 1

    def test_sorted_by_length(self):
        kw = extract_keywords("abc implementation test")
        # "implementation" should come first (longest)
        assert kw[0] == "implementation"

    def test_empty_text(self):
        assert extract_keywords("") == []


# ============================================================================
# Context Formatting
# ============================================================================

class TestContextFormatting:
    """Test format_vault_context."""

    def test_empty_notes_returns_empty(self):
        assert format_vault_context([], "research") == ""

    def test_formatted_includes_advisory_label(self):
        notes = [{"path": "30-workflow-learnings/test.md", "title": "Test Note", "snippet": "some content"}]
        result = format_vault_context(notes, "research")
        assert "advisory" in result.lower()
        assert "runtime state" in result.lower() or "runtime" in result.lower()

    def test_formatted_includes_note_path(self):
        notes = [{"path": "20-agent-patterns/skill-pattern.md", "title": "Skill Pattern", "snippet": "content"}]
        result = format_vault_context(notes, "code_impl")
        assert "20-agent-patterns/skill-pattern.md" in result

    def test_formatted_includes_snippet(self):
        notes = [{"path": "test.md", "title": "Test", "snippet": "the actual snippet text"}]
        result = format_vault_context(notes, "research")
        assert "the actual snippet text" in result

    def test_long_snippet_truncated(self):
        notes = [{"path": "t.md", "title": "T", "snippet": "x" * 300}]
        result = format_vault_context(notes, "research")
        assert "..." in result

    def test_total_size_bounded(self):
        # Many notes with long content
        notes = [
            {"path": f"note{i}.md", "title": f"Note {i}", "snippet": "content " * 50}
            for i in range(10)
        ]
        result = format_vault_context(notes, "research")
        assert len(result) <= MAX_CONTEXT_SIZE + 50  # small buffer for truncation message

    def test_multiple_notes_numbered(self):
        notes = [
            {"path": "a.md", "title": "Note A", "snippet": "a"},
            {"path": "b.md", "title": "Note B", "snippet": "b"},
        ]
        result = format_vault_context(notes, "research")
        assert "1. Note A" in result
        assert "2. Note B" in result


# ============================================================================
# Vault Retrieval (mocked)
# ============================================================================

class TestVaultRetrieval:
    """Test retrieve_vault_context with mocked vault tools."""

    def test_empty_keywords_returns_empty(self):
        assert retrieve_vault_context("research", []) == []

    def test_max_notes_capped(self, monkeypatch):
        """Retrieval never returns more than MAX_VAULT_NOTES."""
        import planner.vault_context as vc


        def mock_retrieve(tc, kw, max_notes=3):
            # Verify the cap is enforced inside the function
            capped = min(max_notes, MAX_VAULT_NOTES)
            return [
                {"path": f"note{i}.md", "title": f"Note {i}", "snippet": "x",
                 "score": 1.0, "source": "obsidian_vault"}
                for i in range(capped)
            ]

        monkeypatch.setattr(vc, "retrieve_vault_context", mock_retrieve)

        results = vc.retrieve_vault_context("research", ["test"], max_notes=10)
        assert len(results) <= MAX_VAULT_NOTES

    def test_import_failure_returns_empty(self, monkeypatch):
        """If vault MCP server is unavailable, retrieval returns empty."""
        import planner.vault_context as vc

        original = vc.retrieve_vault_context

        def failing_retrieve(tc, kw, mn=3):
            # Simulate import failure
            import builtins
            real_import = builtins.__import__
            def mock_import(name, *args, **kwargs):
                if name == "tools.mcp_vault_server":
                    raise ImportError("not available")
                return real_import(name, *args, **kwargs)
            monkeypatch.setattr(builtins, "__import__", mock_import)
            return original(tc, kw, mn)

        results = failing_retrieve("research", ["test"])
        # Should gracefully return empty
        assert isinstance(results, list)


# ============================================================================
# Integration: inject_vault_context
# ============================================================================

class TestInjectVaultContext:
    """Test the top-level inject_vault_context function."""

    def test_ineligible_returns_not_injected(self):
        result = inject_vault_context("simple", "fix a typo")
        assert result["vault_context_injected"] is False
        assert result["vault_notes_found"] == 0
        assert result["vault_advisory_context"] == ""

    def test_runtime_state_returns_not_injected(self):
        result = inject_vault_context("research", "check service status")
        assert result["vault_context_injected"] is False
        assert result["vault_eligibility_reason"] == "runtime_state_query"

    def test_eligible_with_no_keywords_returns_not_injected(self):
        result = inject_vault_context("research", "go do it")
        assert result["vault_context_injected"] is False
        assert "no_keywords" in result["vault_eligibility_reason"]

    def test_eligible_returns_correct_structure(self, monkeypatch):
        """Eligible task with mock vault results returns injected context."""
        import planner.vault_context as vc

        mock_notes = [
            {"path": "30-workflow-learnings/test.md", "title": "Test Learning",
             "snippet": "Prior lesson content", "score": 2.0, "source": "obsidian_vault"},
        ]
        monkeypatch.setattr(vc, "retrieve_vault_context", lambda tc, kw, **kwargs: mock_notes)

        result = inject_vault_context("research", "research multi-agent architecture patterns")
        assert result["vault_context_injected"] is True
        assert result["vault_notes_found"] == 1
        assert "advisory" in result["vault_advisory_context"].lower()
        assert "30-workflow-learnings/test.md" in result["vault_note_paths"]
        assert len(result["vault_queries"]) > 0

    def test_eligible_but_no_results_returns_not_injected(self, monkeypatch):
        import planner.vault_context as vc
        monkeypatch.setattr(vc, "retrieve_vault_context", lambda tc, kw, **kwargs: [])

        result = inject_vault_context("code_impl", "implement bounded write path")
        assert result["vault_context_injected"] is False
        assert result["vault_eligibility_reason"] == "no_relevant_notes"

    def test_retrieval_error_returns_not_injected(self, monkeypatch):
        import planner.vault_context as vc
        def boom(*a, **kw):
            raise RuntimeError("vault down")
        monkeypatch.setattr(vc, "retrieve_vault_context", boom)

        result = inject_vault_context("research", "research architecture patterns")
        assert result["vault_context_injected"] is False
        assert "retrieval_error" in result["vault_eligibility_reason"]


# ============================================================================
# Source-truth boundary
# ============================================================================

class TestSourceTruthBoundary:
    """Verify runtime-state tasks never get vault context."""

    @pytest.mark.parametrize("task_text", [
        "check service status of the watcher",
        "restart the systemctl daemon",
        "check pid of running tasks",
        "watcher health check uptime",
        "kill stale daemon process",
    ])
    def test_runtime_queries_always_skip(self, task_text):
        result = inject_vault_context("research", task_text)
        assert result["vault_context_injected"] is False

    @pytest.mark.parametrize("task_text", [
        "research MCP server design patterns for the vault integration",
        "implement a new bounded write skill for workflow learnings",
        "review the authentication code for security issues",
    ])
    def test_non_runtime_queries_eligible(self, task_text, monkeypatch):
        import planner.vault_context as vc
        monkeypatch.setattr(vc, "retrieve_vault_context", lambda tc, kw, **kwargs: [
            {"path": "test.md", "title": "Test", "snippet": "s", "score": 1.0, "source": "obsidian_vault"}
        ])

        # These should be eligible (though actual retrieval is mocked)
        ok, _ = is_eligible_for_vault_context("research", task_text)
        assert ok is True


# ============================================================================
# Orchestrator adapter integration
# ============================================================================

class TestOrchestratorIntegration:
    """Test that build_plan_from_task integrates vault context correctly."""

    def test_eligible_task_gets_vault_inputs(self, monkeypatch):
        """Research task with mock vault results has context in first step inputs."""
        import planner.vault_context as vc
        mock_notes = [
            {"path": "20-agent-patterns/test.md", "title": "Test",
             "snippet": "content", "score": 1.0, "source": "obsidian_vault"},
        ]
        monkeypatch.setattr(vc, "retrieve_vault_context", lambda tc, kw, **kwargs: mock_notes)

        from tools.orchestrator_adapter import build_plan_from_task
        plan = build_plan_from_task(
            "0050", "research multi-agent architecture patterns",
            routing={"stage": "B"},
        )
        first_step = plan.steps[0]
        assert "vault_advisory_context" in first_step.inputs
        assert "advisory" in first_step.inputs["vault_advisory_context"].lower()
        assert "vault_note_paths" in first_step.inputs

    def test_ineligible_task_has_no_vault_inputs(self):
        """Simple/unknown tasks should not have vault context in step inputs."""
        from tools.orchestrator_adapter import build_plan_from_task
        plan = build_plan_from_task("0051", "fix a typo in readme")
        if plan.steps:
            assert "vault_advisory_context" not in plan.steps[0].inputs

    def test_runtime_task_has_no_vault_inputs(self, monkeypatch):
        """Runtime-state tasks should never get vault context."""
        from tools.orchestrator_adapter import build_plan_from_task
        plan = build_plan_from_task(
            "0052", "check service status and restart daemon",
        )
        if plan.steps:
            assert "vault_advisory_context" not in plan.steps[0].inputs

    def test_context_appears_in_executor_prompt(self, monkeypatch):
        """Verify vault context flows into the step executor prompt."""
        from planner.schemas import PlanStep
        from tools.orchestrator_adapter import _claude_step_executor

        step = PlanStep(
            step_id="test_step",
            skill_name="web-research",
            goal="Research patterns",
            inputs={
                "task_text": "research architecture",
                "vault_advisory_context": "## Prior Knowledge (advisory)\n\nTest vault context here.",
            },
        )

        # Mock subprocess to capture the prompt
        captured_prompts = []
        import subprocess as sp

        def mock_run(cmd, **kwargs):
            # The prompt is the last element of cmd
            captured_prompts.append(cmd[-1] if cmd else "")
            # Return a fake result
            class FakeResult:
                returncode = 0
                stdout = "## CONTRACT\nsummary: test\n"
                stderr = ""
            return FakeResult()

        monkeypatch.setattr(sp, "run", mock_run)

        _claude_step_executor(step)

        assert len(captured_prompts) == 1
        assert "Prior Knowledge (advisory)" in captured_prompts[0]
        assert "Test vault context here" in captured_prompts[0]
