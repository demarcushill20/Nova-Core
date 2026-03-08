"""Tests for planner.workflow_promoter — controlled post-execution promotion.

Phase 5.5: verifies eligibility rules, compaction, promotion execution,
and safety boundaries for workflow-learning vault promotion.
"""

import pytest
import time

from planner.workflow_promoter import (
    _LEARNING_SIGNALS,
    _MIN_LEARNING_SIGNALS,
    _MIN_PROMOTION_GRADES,
    _MIN_STEPS_FOR_PROMOTION,
    _PROMOTABLE_TASK_CLASSES,
    attempt_promotion,
    compact_workflow_learning,
    is_eligible_for_promotion,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_summary(
    status="done",
    grade="A",
    score=0.9,
    steps=None,
    decisions=None,
    verifier_rejected=False,
):
    """Build a minimal plan summary dict for testing."""
    if steps is None:
        steps = [
            {"step_id": "s1_research", "status": "done",
             "skill_name": "web-research", "contract_valid": True,
             "validation_errors": [], "retry_count": 0},
            {"step_id": "s2_synthesize", "status": "done",
             "skill_name": "file-ops", "contract_valid": True,
             "validation_errors": [], "retry_count": 0},
            {"step_id": "s3_verify", "status": "done",
             "skill_name": "self-verification", "contract_valid": True,
             "validation_errors": [], "retry_count": 0},
        ]
    return {
        "plan_id": "plan_test_001",
        "task_id": "test_001",
        "status": status,
        "steps": steps,
        "decisions": decisions or [],
        "evaluation": {
            "grade": grade,
            "aggregate_score": score,
            "summary": "All steps completed successfully with good quality.",
        },
        "verifier_rejected": verifier_rejected,
    }


# Task text with learning signals
_RICH_TASK = (
    "Research multi-agent architecture patterns and document lessons learned. "
    "Identify what worked and what failed in prior approaches. "
    "Provide reusable guidance and highlight any gotchas or caveats."
)

# Task text without learning signals
_THIN_TASK = "hello world"


# ============================================================================
# Eligibility
# ============================================================================

class TestEligibility:

    def test_eligible_research_task(self):
        ok, reason = is_eligible_for_promotion(
            "research", _make_summary(), _RICH_TASK,
        )
        assert ok is True
        assert reason == "eligible"

    def test_eligible_code_impl_task(self):
        ok, reason = is_eligible_for_promotion(
            "code_impl", _make_summary(), _RICH_TASK,
        )
        assert ok is True

    def test_eligible_code_review_task(self):
        ok, reason = is_eligible_for_promotion(
            "code_review", _make_summary(), _RICH_TASK,
        )
        assert ok is True

    def test_ineligible_simple_task(self):
        ok, reason = is_eligible_for_promotion(
            "simple", _make_summary(), _RICH_TASK,
        )
        assert ok is False
        assert "ineligible_class" in reason

    def test_ineligible_unknown_task(self):
        ok, reason = is_eligible_for_promotion(
            "unknown", _make_summary(), _RICH_TASK,
        )
        assert ok is False

    def test_ineligible_system_task(self):
        ok, reason = is_eligible_for_promotion(
            "system", _make_summary(), _RICH_TASK,
        )
        assert ok is False

    def test_failed_workflow_ineligible(self):
        ok, reason = is_eligible_for_promotion(
            "research", _make_summary(status="failed"), _RICH_TASK,
        )
        assert ok is False
        assert "not_done" in reason

    def test_verifier_rejected_ineligible(self):
        ok, reason = is_eligible_for_promotion(
            "code_impl",
            _make_summary(verifier_rejected=True),
            _RICH_TASK,
        )
        assert ok is False
        assert "verifier_rejected" in reason

    def test_low_grade_ineligible(self):
        ok, reason = is_eligible_for_promotion(
            "research", _make_summary(grade="D"), _RICH_TASK,
        )
        assert ok is False
        assert "low_grade" in reason

    def test_grade_C_ineligible(self):
        ok, reason = is_eligible_for_promotion(
            "research", _make_summary(grade="C"), _RICH_TASK,
        )
        assert ok is False
        assert "low_grade" in reason

    def test_grade_A_eligible(self):
        ok, _ = is_eligible_for_promotion(
            "research", _make_summary(grade="A"), _RICH_TASK,
        )
        assert ok is True

    def test_grade_B_eligible(self):
        ok, _ = is_eligible_for_promotion(
            "research", _make_summary(grade="B"), _RICH_TASK,
        )
        assert ok is True

    def test_too_few_steps_ineligible(self):
        one_step = [
            {"step_id": "s1", "status": "done", "skill_name": "file-ops",
             "contract_valid": True, "validation_errors": [], "retry_count": 0},
        ]
        ok, reason = is_eligible_for_promotion(
            "research", _make_summary(steps=one_step), _RICH_TASK,
        )
        assert ok is False
        assert "too_few_steps" in reason

    def test_thin_output_ineligible(self):
        """Task text with insufficient learning signals is rejected."""
        ok, reason = is_eligible_for_promotion(
            "research", _make_summary(), _THIN_TASK,
        )
        assert ok is False
        assert "insufficient_signals" in reason

    def test_promotable_classes_set(self):
        assert _PROMOTABLE_TASK_CLASSES == {"research", "code_impl", "code_review"}

    def test_min_grades_set(self):
        assert _MIN_PROMOTION_GRADES == {"A", "B"}


# ============================================================================
# Compaction
# ============================================================================

class TestCompaction:

    def test_produces_valid_frontmatter(self):
        learning = compact_workflow_learning(
            "test_001", "research", _RICH_TASK,
            _make_summary(), "stageB_research",
        )
        fm = learning["frontmatter"]
        assert fm["type"] == "workflow-learning"
        assert fm["source"] == "nova-core-memory"
        assert fm["task_class"] == "research"
        assert "learning_id" in fm
        assert fm["learning_id"].startswith("wl-")
        assert isinstance(fm["roles_involved"], list)
        assert len(fm["roles_involved"]) > 0
        assert fm["date"] == time.strftime("%Y-%m-%d", time.gmtime())

    def test_produces_body(self):
        learning = compact_workflow_learning(
            "test_002", "code_impl", _RICH_TASK,
            _make_summary(), "stageC_coding",
        )
        assert "## Task Summary" in learning["body"]
        assert "## Execution Summary" in learning["body"]
        assert "## Trace" in learning["body"]

    def test_path_in_target_folder(self):
        learning = compact_workflow_learning(
            "test_003", "research", _RICH_TASK,
            _make_summary(), "stageB_research",
        )
        assert learning["path"].startswith("30-workflow-learnings/")
        assert learning["path"].endswith(".md")

    def test_auto_promoted_tag(self):
        learning = compact_workflow_learning(
            "test_004", "research", _RICH_TASK,
            _make_summary(), "stageB_research",
        )
        assert "#source/auto-promoted" in learning["frontmatter"]["tags"]

    def test_roles_extracted_from_steps(self):
        learning = compact_workflow_learning(
            "test_005", "research", _RICH_TASK,
            _make_summary(), "stageB_research",
        )
        roles = learning["frontmatter"]["roles_involved"]
        # web-research → research, file-ops → coder, self-verification → verifier
        assert "research" in roles
        assert "coder" in roles
        assert "verifier" in roles

    def test_title_extracted(self):
        learning = compact_workflow_learning(
            "test_006", "research",
            "# Investigate MCP server patterns\nMore details here.",
            _make_summary(), "stageB_research",
        )
        # Should strip markdown header
        assert learning["frontmatter"]["title"].startswith("Investigate")

    def test_long_title_truncated(self):
        learning = compact_workflow_learning(
            "test_007", "research", "x" * 200,
            _make_summary(), "stageB_research",
        )
        assert len(learning["frontmatter"]["title"]) <= 83  # 80 + "..."

    def test_verification_outcome_from_steps(self):
        learning = compact_workflow_learning(
            "test_008", "research", _RICH_TASK,
            _make_summary(), "stageB_research",
        )
        assert learning["frontmatter"]["verification_outcome"] == "approved"

    def test_no_verify_step_outcome(self):
        steps = [
            {"step_id": "s1_research", "status": "done",
             "skill_name": "web-research", "contract_valid": True,
             "validation_errors": [], "retry_count": 0},
            {"step_id": "s2_write", "status": "done",
             "skill_name": "file-ops", "contract_valid": True,
             "validation_errors": [], "retry_count": 0},
        ]
        learning = compact_workflow_learning(
            "test_009", "research", _RICH_TASK,
            _make_summary(steps=steps), "stageB_research",
        )
        assert learning["frontmatter"]["verification_outcome"] == "not_verified"


# ============================================================================
# Promotion execution (mocked vault)
# ============================================================================

class TestAttemptPromotion:

    def test_ineligible_skips(self):
        result = attempt_promotion(
            "test_010", "simple", _THIN_TASK,
            _make_summary(), "single_skill",
        )
        assert result["promoted"] is False
        assert "ineligible_class" in result["reason"]
        assert result["note_path"] is None

    def test_failed_workflow_skips(self):
        result = attempt_promotion(
            "test_011", "research", _RICH_TASK,
            _make_summary(status="failed"), "stageB_research",
        )
        assert result["promoted"] is False
        assert "not_done" in result["reason"]

    def test_eligible_with_mock_vault(self, monkeypatch):
        """Eligible workflow with mocked vault tools promotes successfully."""
        import planner.workflow_promoter as wp

        def mock_validate(frontmatter, body):
            return {"valid": True, "errors": []}

        def mock_write(path, frontmatter, body):
            return {"path": path, "size": len(body)}

        # Patch the import inside attempt_promotion
        import types
        mock_module = types.ModuleType("tools.mcp_vault_server")
        mock_module.vault_validate = mock_validate
        mock_module.vault_write = mock_write

        import sys
        monkeypatch.setitem(sys.modules, "tools.mcp_vault_server", mock_module)

        result = attempt_promotion(
            "test_012", "research", _RICH_TASK,
            _make_summary(), "stageB_research",
        )
        assert result["promoted"] is True
        assert result["reason"] == "eligible"
        assert result["note_path"] is not None
        assert result["note_path"].startswith("30-workflow-learnings/")
        assert result["learning_id"] is not None
        assert result["errors"] == []

    def test_validation_failure_blocks_promotion(self, monkeypatch):
        """Vault validation failure blocks promotion gracefully."""
        import types
        mock_module = types.ModuleType("tools.mcp_vault_server")

        def _mock_validate(frontmatter, body=""):
            return {"valid": False, "errors": ["missing required field: title"]}

        mock_module.vault_validate = _mock_validate
        mock_module.vault_write = lambda *a, **kw: {"error": "should not reach"}

        import sys
        monkeypatch.setitem(sys.modules, "tools.mcp_vault_server", mock_module)

        result = attempt_promotion(
            "test_013", "research", _RICH_TASK,
            _make_summary(), "stageB_research",
        )
        assert result["promoted"] is False
        assert result["reason"] == "validation_failed"
        assert len(result["errors"]) > 0

    def test_write_rejection_blocks_promotion(self, monkeypatch):
        """Vault write rejection (e.g. file exists) blocks promotion."""
        import types
        mock_module = types.ModuleType("tools.mcp_vault_server")

        def _mock_validate(frontmatter, body=""):
            return {"valid": True, "errors": []}

        def _mock_write(path, frontmatter, body):
            return {"error": "file_exists"}

        mock_module.vault_validate = _mock_validate
        mock_module.vault_write = _mock_write

        import sys
        monkeypatch.setitem(sys.modules, "tools.mcp_vault_server", mock_module)

        result = attempt_promotion(
            "test_014", "research", _RICH_TASK,
            _make_summary(), "stageB_research",
        )
        assert result["promoted"] is False
        assert "write_rejected" in result["reason"]

    def test_vault_unavailable_fails_open(self, monkeypatch):
        """If vault MCP server is not importable, promotion fails open."""
        import sys
        # Remove the module if cached
        monkeypatch.delitem(sys.modules, "tools.mcp_vault_server", raising=False)

        # Make import fail
        import builtins
        real_import = builtins.__import__

        def block_import(name, *args, **kwargs):
            if name == "tools.mcp_vault_server":
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_import)

        result = attempt_promotion(
            "test_015", "research", _RICH_TASK,
            _make_summary(), "stageB_research",
        )
        assert result["promoted"] is False
        assert "vault_unavailable" in result["reason"]

    def test_thin_output_not_promoted(self):
        result = attempt_promotion(
            "test_016", "research", _THIN_TASK,
            _make_summary(), "stageB_research",
        )
        assert result["promoted"] is False
        assert "insufficient_signals" in result["reason"]


# ============================================================================
# Source / ownership safety
# ============================================================================

class TestSafetyBoundaries:

    def test_source_always_nova_core_memory(self):
        learning = compact_workflow_learning(
            "safe_001", "research", _RICH_TASK,
            _make_summary(), "stageB_research",
        )
        assert learning["frontmatter"]["source"] == "nova-core-memory"

    def test_target_folder_always_workflow_learnings(self):
        for tc in ("research", "code_impl", "code_review"):
            learning = compact_workflow_learning(
                f"safe_{tc}", tc, _RICH_TASK,
                _make_summary(), "stageB_research",
            )
            assert learning["path"].startswith("30-workflow-learnings/")

    def test_promotion_result_structure(self):
        result = attempt_promotion(
            "safe_003", "simple", _THIN_TASK,
            _make_summary(), "single_skill",
        )
        # Must always have these keys
        assert "promoted" in result
        assert "reason" in result
        assert "note_path" in result
        assert "learning_id" in result
        assert "errors" in result

    def test_promotion_does_not_block_on_failure(self):
        """Even with bad inputs, promotion returns a result, never raises."""
        result = attempt_promotion(
            "", "", "", {}, "",
        )
        assert result["promoted"] is False


# ============================================================================
# Orchestrator adapter integration
# ============================================================================

class TestOrchestratorIntegration:

    def test_promotion_called_for_successful_governed_task(self, monkeypatch):
        """Verify execute_via_orchestrator includes promotion result."""
        from pathlib import Path

        import tools.orchestrator_adapter as oa

        # Mock Orchestrator class so run_plan returns a successful summary
        class MockOrchestrator:
            def __init__(self, **kw):
                pass
            def run_plan(self, plan):
                return _make_summary()

        monkeypatch.setattr(oa, "Orchestrator", MockOrchestrator)
        monkeypatch.setattr(oa, "Supervisor", lambda: None)

        # Mock vault tools for both context injection and promotion
        import types, sys
        mock_module = types.ModuleType("tools.mcp_vault_server")
        mock_module.vault_validate = lambda frontmatter, body="": {"valid": True, "errors": []}
        mock_module.vault_write = lambda path, frontmatter, body: {"path": path, "size": 100}
        mock_module.vault_search = lambda query, max_results=5: []
        mock_module.vault_read = lambda path: ""
        monkeypatch.setitem(sys.modules, "tools.mcp_vault_server", mock_module)

        # Create temp task path
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write(_RICH_TASK)
            task_path = Path(f.name)

        oa.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        result = oa.execute_via_orchestrator(
            stem="test_integration",
            task_text=_RICH_TASK,
            task_path=task_path,
            routing={"stage": "B"},
        )

        assert "promotion" in result
        assert result["promotion"] is not None
        # Promotion was attempted for governed stage B task
        assert isinstance(result["promotion"], dict)

    def test_no_promotion_for_non_governed_task(self, monkeypatch):
        """Tasks without stage B/C should not trigger promotion."""
        from pathlib import Path

        import tools.orchestrator_adapter as oa

        class MockOrchestrator:
            def __init__(self, **kw):
                pass
            def run_plan(self, plan):
                return _make_summary()

        monkeypatch.setattr(oa, "Orchestrator", MockOrchestrator)
        monkeypatch.setattr(oa, "Supervisor", lambda: None)

        oa.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write(_RICH_TASK)
            task_path = Path(f.name)

        result = oa.execute_via_orchestrator(
            stem="test_no_promo",
            task_text=_RICH_TASK,
            task_path=task_path,
            routing={},  # No stage
        )

        # promotion should be None (not attempted)
        assert result["promotion"] is None
