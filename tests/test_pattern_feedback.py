"""Tests for planner/pattern_feedback.py — Phase 7.5 planner feedback tracing."""

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from planner.pattern_feedback import (
    USEFULNESS_VALUES,
    _compute_usefulness,
    finalize_pattern_trace,
    init_pattern_trace,
    log_pattern_trace,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class MockStep:
    step_id: str = "test_step"
    skill_name: str = "web-research"
    goal: str = "test"
    inputs: dict = field(default_factory=dict)


@dataclass
class MockPlan:
    plan_id: str = "plan_test"
    task_id: str = "test"
    strategy: str = "test"
    steps: list = field(default_factory=list)


def _plan_with_patterns(guidance="## Pattern guidance", paths=None):
    """Create a mock plan with pattern guidance injected."""
    step = MockStep(
        inputs={
            "task_text": "research multi-query strategy",
            "pattern_guidance": guidance,
            "pattern_paths": paths or ["20-agent-patterns/test.md"],
        }
    )
    return MockPlan(steps=[step])


def _plan_without_patterns():
    """Create a mock plan without pattern guidance."""
    step = MockStep(inputs={"task_text": "simple task"})
    return MockPlan(steps=[step])


def _plan_empty():
    """Create a mock plan with no steps."""
    return MockPlan(steps=[])


def _successful_summary(grade="A"):
    return {
        "status": "done",
        "evaluation": {"grade": grade, "aggregate_score": 0.9},
        "steps": [{"step_id": "s1", "status": "done"}],
    }


def _failed_summary():
    return {
        "status": "failed",
        "evaluation": {},
        "error": "execution failed",
    }


def _rejected_summary():
    return {
        "status": "rejected",
        "verifier_rejected": True,
        "evaluation": {"grade": "B"},
    }


# ===================================================================
# Trace initialization
# ===================================================================


class TestInitPatternTrace:
    def test_with_pattern_guidance(self):
        plan = _plan_with_patterns()
        trace = init_pattern_trace("0051", "research", plan)

        assert trace["task_id"] == "0051"
        assert trace["task_class"] == "research"
        assert trace["pattern_retrieval_activated"] is True
        assert trace["guidance_injected"] is True
        assert trace["guidance_size_bytes"] > 0
        assert trace["selected_pattern_paths"] == ["20-agent-patterns/test.md"]
        assert trace["selected_pattern_count"] == 1
        assert trace["usefulness"] == "unknown"  # awaiting finalization
        assert trace["trace_finalized"] is False

    def test_without_pattern_guidance(self):
        plan = _plan_without_patterns()
        trace = init_pattern_trace("0052", "code_impl", plan)

        assert trace["pattern_retrieval_activated"] is False
        assert trace["guidance_injected"] is False
        assert trace["usefulness"] == "not_applicable"

    def test_empty_plan(self):
        plan = _plan_empty()
        trace = init_pattern_trace("0053", "research", plan)

        assert trace["pattern_retrieval_activated"] is False
        assert trace["rationale"] == "no_plan_steps"

    def test_paths_found_but_no_guidance(self):
        """Patterns were found by search but no extractable guidance."""
        step = MockStep(
            inputs={
                "task_text": "test",
                "pattern_guidance": "",
                "pattern_paths": ["20-agent-patterns/found.md"],
            }
        )
        plan = MockPlan(steps=[step])
        trace = init_pattern_trace("0054", "research", plan)

        assert trace["pattern_retrieval_activated"] is True
        assert trace["guidance_injected"] is False
        assert trace["usefulness"] == "unused"
        assert "no_extractable" in trace["rationale"]

    def test_has_timestamp(self):
        plan = _plan_with_patterns()
        trace = init_pattern_trace("0055", "research", plan)
        assert "timestamp_init" in trace
        assert trace["timestamp_init"] is not None

    def test_errors_list_initialized(self):
        plan = _plan_with_patterns()
        trace = init_pattern_trace("0056", "research", plan)
        assert trace["errors"] == []


# ===================================================================
# Usefulness computation
# ===================================================================


class TestComputeUsefulness:
    def test_not_injected(self):
        assert _compute_usefulness(False, "done", "A", False) == "unused"

    def test_injected_success_grade_a(self):
        assert _compute_usefulness(True, "done", "A", False) == "useful"

    def test_injected_success_grade_b(self):
        assert _compute_usefulness(True, "done", "B", False) == "useful"

    def test_injected_success_grade_b_plus(self):
        assert _compute_usefulness(True, "done", "B+", False) == "useful"

    def test_injected_success_grade_c(self):
        assert _compute_usefulness(True, "done", "C", False) == "neutral"

    def test_injected_success_no_grade(self):
        assert _compute_usefulness(True, "done", "", False) == "neutral"

    def test_injected_failed(self):
        assert _compute_usefulness(True, "failed", "A", False) == "unknown"

    def test_injected_rejected(self):
        assert _compute_usefulness(True, "rejected", "B", False) == "unknown"

    def test_injected_verifier_rejected(self):
        assert _compute_usefulness(True, "done", "A", True) == "unknown"

    def test_injected_partial(self):
        assert _compute_usefulness(True, "partial", "B", False) == "unknown"

    def test_injected_unknown_status(self):
        assert _compute_usefulness(True, "", "", False) == "unknown"

    def test_all_values_in_taxonomy(self):
        """All computed values must be in the defined taxonomy."""
        cases = [
            (False, "done", "A", False),
            (True, "done", "A", False),
            (True, "done", "B", False),
            (True, "done", "C", False),
            (True, "done", "", False),
            (True, "failed", "", False),
            (True, "rejected", "", False),
            (True, "partial", "", False),
            (True, "", "", False),
            (True, "done", "A", True),
        ]
        for injected, status, grade, vr in cases:
            result = _compute_usefulness(injected, status, grade, vr)
            assert result in USEFULNESS_VALUES, f"Unexpected value: {result}"


# ===================================================================
# Trace finalization
# ===================================================================


class TestFinalizePatternTrace:
    def test_finalize_useful(self):
        trace = init_pattern_trace("0057", "research", _plan_with_patterns())
        finalize_pattern_trace(trace, _successful_summary("A"))

        assert trace["trace_finalized"] is True
        assert trace["execution_status"] == "done"
        assert trace["execution_grade"] == "A"
        assert trace["usefulness"] == "useful"
        assert "timestamp_final" in trace

    def test_finalize_neutral(self):
        trace = init_pattern_trace("0058", "research", _plan_with_patterns())
        finalize_pattern_trace(trace, _successful_summary("C"))

        assert trace["usefulness"] == "neutral"

    def test_finalize_unknown_on_failure(self):
        trace = init_pattern_trace("0059", "research", _plan_with_patterns())
        finalize_pattern_trace(trace, _failed_summary())

        assert trace["usefulness"] == "unknown"
        assert trace["execution_status"] == "failed"

    def test_finalize_unknown_on_verifier_rejection(self):
        trace = init_pattern_trace("0060", "research", _plan_with_patterns())
        finalize_pattern_trace(trace, _rejected_summary())

        assert trace["usefulness"] == "unknown"

    def test_finalize_not_applicable_no_patterns(self):
        trace = init_pattern_trace("0061", "code_impl", _plan_without_patterns())
        finalize_pattern_trace(trace, _successful_summary("A"))

        # Usefulness was already set to not_applicable in init
        assert trace["usefulness"] == "not_applicable"
        assert trace["trace_finalized"] is True

    def test_finalize_preserves_existing_fields(self):
        trace = init_pattern_trace("0062", "research", _plan_with_patterns())
        finalize_pattern_trace(trace, _successful_summary("B"))

        assert trace["task_id"] == "0062"
        assert trace["task_class"] == "research"
        assert trace["selected_pattern_paths"] == ["20-agent-patterns/test.md"]

    def test_rationale_present(self):
        trace = init_pattern_trace("0063", "research", _plan_with_patterns())
        finalize_pattern_trace(trace, _successful_summary("A"))

        assert trace["rationale"]
        assert "useful" in trace["rationale"]


# ===================================================================
# Trace logging
# ===================================================================


class TestLogPatternTrace:
    def test_logs_to_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch("planner.pattern_feedback.LOGS_DIR", Path(tmpdir)):
            trace = {
                "task_id": "0064",
                "usefulness": "useful",
                "trace_finalized": True,
            }
            result = log_pattern_trace(trace)

            assert result is True
            log_path = Path(tmpdir) / "pattern_feedback.jsonl"
            assert log_path.exists()

            content = log_path.read_text()
            parsed = json.loads(content.strip())
            assert parsed["task_id"] == "0064"
            assert parsed["usefulness"] == "useful"

    def test_appends_multiple(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch("planner.pattern_feedback.LOGS_DIR", Path(tmpdir)):
            log_pattern_trace({"task_id": "a", "usefulness": "useful"})
            log_pattern_trace({"task_id": "b", "usefulness": "neutral"})

            log_path = Path(tmpdir) / "pattern_feedback.jsonl"
            lines = log_path.read_text().strip().split("\n")
            assert len(lines) == 2

    def test_fail_open(self):
        """Logging failure returns False, does not raise."""
        with patch("planner.pattern_feedback.LOGS_DIR", Path("/nonexistent/dir")):
            result = log_pattern_trace({"task_id": "fail"})
            # Should not raise, just return False
            assert result is False


# ===================================================================
# Safety boundaries
# ===================================================================


class TestSafetyBoundaries:
    def test_no_vault_writes(self):
        """pattern_feedback module has no vault write imports."""
        import inspect

        import planner.pattern_feedback as pf

        source = inspect.getsource(pf)
        assert "vault_write" not in source
        assert "vault_update" not in source

    def test_log_is_runtime_only(self):
        """Log path is in LOGS/, not in the vault."""
        from planner.pattern_feedback import LOGS_DIR

        assert "LOGS" in str(LOGS_DIR)
        assert "nova-vault" not in str(LOGS_DIR)

    def test_usefulness_taxonomy_bounded(self):
        """Usefulness values are a fixed bounded set."""
        assert len(USEFULNESS_VALUES) == 5
        assert "useful" in USEFULNESS_VALUES
        assert "neutral" in USEFULNESS_VALUES
        assert "unused" in USEFULNESS_VALUES
        assert "unknown" in USEFULNESS_VALUES
        assert "not_applicable" in USEFULNESS_VALUES

    def test_trace_never_modifies_plan(self):
        """init_pattern_trace reads plan but does not modify it."""
        plan = _plan_with_patterns()
        original_guidance = plan.steps[0].inputs["pattern_guidance"]
        original_paths = plan.steps[0].inputs["pattern_paths"][:]

        init_pattern_trace("test", "research", plan)

        assert plan.steps[0].inputs["pattern_guidance"] == original_guidance
        assert plan.steps[0].inputs["pattern_paths"] == original_paths


# ===================================================================
# Orchestrator integration
# ===================================================================


class TestOrchestratorIntegration:
    def test_feedback_imports(self):
        from planner.pattern_feedback import (
            finalize_pattern_trace,
            init_pattern_trace,
            log_pattern_trace,
        )

        assert callable(init_pattern_trace)
        assert callable(finalize_pattern_trace)
        assert callable(log_pattern_trace)

    def test_feedback_in_execute_via_orchestrator(self):
        import inspect

        import tools.orchestrator_adapter as oa

        source = inspect.getsource(oa.execute_via_orchestrator)
        assert "init_pattern_trace" in source
        assert "finalize_pattern_trace" in source
        assert "log_pattern_trace" in source
        assert "pattern_feedback" in source  # in return dict

    def test_pattern_feedback_key_in_return(self):
        """Return dict includes pattern_feedback key."""
        import inspect

        import tools.orchestrator_adapter as oa

        source = inspect.getsource(oa.execute_via_orchestrator)
        assert '"pattern_feedback"' in source
