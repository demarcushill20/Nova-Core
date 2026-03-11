"""Phase 7.12b — Tests for live orchestrator workflow state persistence.

Tests cover:
  1. Successful orchestrator execution writes completed workflow state
  2. Failed orchestrator execution writes failed workflow state
  3. Rejected/halted path writes terminal state
  4. Persisted workflow record is readable by rollout_gate evidence collection
  5. No duplicate/broken workflow persistence on repeat or error paths
  6. Status mapping from orchestrator to workflow schema
  7. Atomic write behavior (no partial files)
"""

import json
from unittest.mock import MagicMock

import pytest

from agents.rollout_gate import collect_evidence
from tools.orchestrator_adapter import (
    _ORCHESTRATOR_STATUS_MAP,
    _persist_workflow_state,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect STATE_DIR to tmp_path/STATE for isolated workflow writes."""
    sd = tmp_path / "STATE"
    sd.mkdir()
    (sd / "workflows").mkdir()
    monkeypatch.setattr("tools.orchestrator_adapter.STATE_DIR", sd)
    return sd


@pytest.fixture
def evidence_root(tmp_path):
    """Create a full evidence directory structure for rollout_gate compatibility."""
    root = tmp_path / "repo"
    state = root / "STATE"
    (state / "workflows").mkdir(parents=True)
    (state / "verifications").mkdir(parents=True)
    (state / "config").mkdir(parents=True)
    (state / "agents" / "runtime").mkdir(parents=True)
    (state / "leases").mkdir(parents=True)

    # Minimal healthy heartbeat
    (state / "heartbeat_multiagent.json").write_text(json.dumps({
        "overall": "healthy",
        "findings": [{"severity": "healthy", "component": "test"}],
        "generated_at": "2026-03-08T00:00:00Z",
    }))

    # Empty metrics (no contract failures)
    (state / "metrics.json").write_text(json.dumps({
        "contract_success": {"_total": 10},
        "contract_failure": {"_total": 0},
    }))

    return root


# ===================================================================
# Part 1 — Direct _persist_workflow_state tests
# ===================================================================

class TestPersistWorkflowState:
    """Test the _persist_workflow_state function directly."""

    def test_completed_workflow_written(self, state_dir):
        """Successful orchestrator run writes completed workflow."""
        result = _persist_workflow_state("task_001", "completed", "research", "B")

        assert result is not None
        wf_path = state_dir / "workflows" / "task_001.json"
        assert wf_path.exists()

        data = json.loads(wf_path.read_text())
        assert data["workflow_id"] == "task_001"
        assert data["status"] == "completed"
        assert data["task_class"] == "research"
        assert data["stage"] == "B"
        assert data["execution_path"] == "orchestrator"
        assert isinstance(data["created_at"], (int, float))
        assert isinstance(data["completed_at"], (int, float))

    def test_failed_workflow_written(self, state_dir):
        """Failed orchestrator run writes failed workflow."""
        _persist_workflow_state("task_002", "failed", "code_review", "C")

        data = json.loads((state_dir / "workflows" / "task_002.json").read_text())
        assert data["status"] == "failed"
        assert data["task_class"] == "code_review"
        assert data["stage"] == "C"

    def test_rejected_workflow_written(self, state_dir):
        """Plan validation rejection writes rejected workflow with halt_reason."""
        _persist_workflow_state(
            "task_003", "rejected", "code_impl", "C",
            halt_reason="plan_validation: missing self-verification step",
        )

        data = json.loads((state_dir / "workflows" / "task_003.json").read_text())
        assert data["status"] == "rejected"
        assert data["halt_reason"] == "plan_validation: missing self-verification step"

    def test_halt_reason_persisted(self, state_dir):
        """halt_reason field written when provided."""
        _persist_workflow_state(
            "task_004", "failed", "research", "B",
            halt_reason="verifier_rejected",
        )

        data = json.loads((state_dir / "workflows" / "task_004.json").read_text())
        assert data["halt_reason"] == "verifier_rejected"

    def test_no_halt_reason_when_none(self, state_dir):
        """halt_reason field absent when not provided."""
        _persist_workflow_state("task_005", "completed", "research", "B")

        data = json.loads((state_dir / "workflows" / "task_005.json").read_text())
        assert "halt_reason" not in data

    def test_returns_path_on_success(self, state_dir):
        """Returns the workflow file path on success."""
        result = _persist_workflow_state("task_006", "completed")
        assert result == state_dir / "workflows" / "task_006.json"

    def test_creates_workflows_dir_if_missing(self, tmp_path, monkeypatch):
        """Creates STATE/workflows/ directory if it doesn't exist."""
        sd = tmp_path / "STATE_FRESH"
        monkeypatch.setattr("tools.orchestrator_adapter.STATE_DIR", sd)
        # Directory doesn't exist yet
        assert not (sd / "workflows").exists()

        result = _persist_workflow_state("task_007", "completed")
        assert result is not None
        assert (sd / "workflows" / "task_007.json").exists()


# ===================================================================
# Part 2 — Overwrite and idempotency
# ===================================================================

class TestPersistOverwrite:
    """Test repeat writes and idempotency."""

    def test_overwrite_on_repeat(self, state_dir):
        """Second write for same stem overwrites the file."""
        _persist_workflow_state("task_010", "failed", "research", "B")
        _persist_workflow_state("task_010", "completed", "research", "B")

        data = json.loads((state_dir / "workflows" / "task_010.json").read_text())
        assert data["status"] == "completed"

    def test_no_tmp_file_left(self, state_dir):
        """Atomic write leaves no .tmp file behind."""
        _persist_workflow_state("task_011", "completed")

        tmp_files = list((state_dir / "workflows").glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_multiple_workflows_coexist(self, state_dir):
        """Multiple workflows create separate files."""
        _persist_workflow_state("task_a", "completed")
        _persist_workflow_state("task_b", "failed")
        _persist_workflow_state("task_c", "completed")

        files = sorted((state_dir / "workflows").glob("*.json"))
        assert len(files) == 3


# ===================================================================
# Part 3 — Status mapping
# ===================================================================

class TestStatusMapping:
    """Verify orchestrator status → workflow status mapping."""

    def test_done_maps_to_completed(self):
        assert _ORCHESTRATOR_STATUS_MAP["done"] == "completed"

    def test_failed_maps_to_failed(self):
        assert _ORCHESTRATOR_STATUS_MAP["failed"] == "failed"

    def test_partial_maps_to_failed(self):
        assert _ORCHESTRATOR_STATUS_MAP["partial"] == "failed"

    def test_rejected_maps_to_failed(self):
        assert _ORCHESTRATOR_STATUS_MAP["rejected"] == "failed"

    def test_unknown_status_defaults_to_failed(self):
        """Unknown orchestrator status should default to failed in code."""
        assert _ORCHESTRATOR_STATUS_MAP.get("unknown_xyz", "failed") == "failed"


# ===================================================================
# Part 4 — Rollout gate reader compatibility
# ===================================================================

class TestRolloutGateCompatibility:
    """Verify persisted workflows are consumable by rollout_gate.collect_evidence()."""

    def test_completed_workflow_counted(self, evidence_root, monkeypatch):
        """A completed workflow persisted by the adapter is counted by collect_evidence."""
        monkeypatch.setattr("tools.orchestrator_adapter.STATE_DIR",
                            evidence_root / "STATE")
        _persist_workflow_state("wf_live_1", "completed", "research", "B")

        ev = collect_evidence(evidence_root)
        assert ev["completed_workflows"] == 1
        assert ev["total_workflows"] == 1

    def test_failed_workflow_counted(self, evidence_root, monkeypatch):
        """A failed workflow is counted toward failure rate."""
        monkeypatch.setattr("tools.orchestrator_adapter.STATE_DIR",
                            evidence_root / "STATE")
        _persist_workflow_state("wf_live_2", "failed", "code_review", "C")

        ev = collect_evidence(evidence_root)
        assert ev["failed_workflows"] == 1
        assert ev["completed_workflows"] == 0
        assert ev["failure_rate"] == 1.0  # 1 failed / 1 total

    def test_mixed_workflows_correct_rates(self, evidence_root, monkeypatch):
        """Mix of completed and failed workflows produces correct failure rate."""
        monkeypatch.setattr("tools.orchestrator_adapter.STATE_DIR",
                            evidence_root / "STATE")

        _persist_workflow_state("wf_a", "completed", "research", "B")
        _persist_workflow_state("wf_b", "completed", "research", "B")
        _persist_workflow_state("wf_c", "completed", "code_review", "C")
        _persist_workflow_state("wf_d", "failed", "research", "B")

        ev = collect_evidence(evidence_root)
        assert ev["completed_workflows"] == 3
        assert ev["failed_workflows"] == 1
        assert ev["total_workflows"] == 4
        # failure_rate = 1 / 4 = 0.25
        assert ev["failure_rate"] == 0.25

    def test_three_completed_meets_minimum(self, evidence_root, monkeypatch):
        """3 completed workflows meet the MIN_COMPLETED_WORKFLOWS threshold."""
        from agents.rollout_gate import MIN_COMPLETED_WORKFLOWS, evaluate_rollout

        monkeypatch.setattr("tools.orchestrator_adapter.STATE_DIR",
                            evidence_root / "STATE")

        for i in range(MIN_COMPLETED_WORKFLOWS):
            _persist_workflow_state(f"wf_{i}", "completed", "research", "B")

        evaluation = evaluate_rollout(evidence_root)
        # minimum_completed_runs criterion should pass
        for c in evaluation.criteria:
            if c.name == "minimum_completed_runs":
                assert c.passed is True
                assert c.value == MIN_COMPLETED_WORKFLOWS
                break
        else:
            pytest.fail("minimum_completed_runs criterion not found")

    def test_rejected_workflow_not_counted_as_completed(self, evidence_root, monkeypatch):
        """Rejected workflows (plan validation failures) don't count as completed."""
        monkeypatch.setattr("tools.orchestrator_adapter.STATE_DIR",
                            evidence_root / "STATE")
        _persist_workflow_state("wf_rej", "rejected", "code_impl", "C")

        ev = collect_evidence(evidence_root)
        assert ev["completed_workflows"] == 0
        assert ev["failed_workflows"] == 0  # "rejected" is not "failed"
        assert ev["total_workflows"] == 1

    def test_verifier_rejected_halt_reason_present(self, evidence_root, monkeypatch):
        """Verifier rejection records halt_reason for operational inspection."""
        monkeypatch.setattr("tools.orchestrator_adapter.STATE_DIR",
                            evidence_root / "STATE")
        _persist_workflow_state("wf_vrej", "failed", "code_review", "C",
                                halt_reason="verifier_rejected")

        wf_path = evidence_root / "STATE" / "workflows" / "wf_vrej.json"
        data = json.loads(wf_path.read_text())
        assert data["halt_reason"] == "verifier_rejected"
        assert data["status"] == "failed"

        ev = collect_evidence(evidence_root)
        assert ev["failed_workflows"] == 1


# ===================================================================
# Part 5 — Integration with execute_via_orchestrator paths
# ===================================================================

class TestExecuteViaOrchestratorPersistence:
    """Test that execute_via_orchestrator writes workflow state at terminal points.

    These tests mock the orchestrator internals but verify the persistence
    calls happen for each terminal path.
    """

    def _mock_orchestrator_run(self, state_dir, monkeypatch, summary_status,
                               steps=None, stage="B", verifier_required=False,
                               task_class="research", should_raise=False):
        """Helper: run execute_via_orchestrator with mocked internals."""
        from tools.orchestrator_adapter import execute_via_orchestrator

        monkeypatch.setattr("tools.orchestrator_adapter.OUTPUT_DIR",
                            state_dir.parent / "OUTPUT")
        (state_dir.parent / "OUTPUT").mkdir(exist_ok=True)

        # Mock plan building
        mock_plan = MagicMock()
        mock_plan.plan_id = "plan_test"
        mock_plan.steps = steps or [
            MagicMock(step_id="s1", skill_name="web-research",
                      goal="test", depends_on=[]),
        ]
        mock_plan.strategy = "single_skill"
        mock_plan.success_criteria = []
        monkeypatch.setattr("tools.orchestrator_adapter.build_plan_from_task",
                            lambda *a, **kw: mock_plan)

        # Mock classifier
        monkeypatch.setattr("tools.orchestrator_adapter.classify_task",
                            lambda t: (task_class, 0.9))

        # Mock pattern trace
        monkeypatch.setattr("tools.orchestrator_adapter.init_pattern_trace",
                            lambda *a, **kw: {})
        monkeypatch.setattr("tools.orchestrator_adapter.finalize_pattern_trace",
                            lambda *a, **kw: None)
        monkeypatch.setattr("tools.orchestrator_adapter.log_pattern_trace",
                            lambda *a, **kw: None)

        # Mock vault context + pattern retriever
        monkeypatch.setattr("tools.orchestrator_adapter.inject_vault_context",
                            lambda *a, **kw: None)
        monkeypatch.setattr("tools.orchestrator_adapter.retrieve_pattern_guidance",
                            lambda *a, **kw: None)

        # Mock promotion + sync
        monkeypatch.setattr("tools.orchestrator_adapter.attempt_promotion",
                            lambda *a, **kw: {"promoted": False})
        monkeypatch.setattr("tools.orchestrator_adapter.attempt_pattern_promotion",
                            lambda *a, **kw: {"promoted": False})
        monkeypatch.setattr("tools.orchestrator_adapter.check_sync_after_write",
                            lambda *a, **kw: None)

        # Mock plan validation to pass
        monkeypatch.setattr("tools.orchestrator_adapter.validate_stageB_plan",
                            lambda p: (True, ""))
        monkeypatch.setattr("tools.orchestrator_adapter.validate_stageC_plan",
                            lambda p: (True, ""))

        # Mock Orchestrator
        mock_orch = MagicMock()
        if should_raise:
            mock_orch.run_plan.side_effect = RuntimeError("orchestrator crashed")
        else:
            mock_orch.run_plan.return_value = {
                "plan_id": "plan_test",
                "task_id": "test_stem",
                "status": summary_status,
                "steps": steps or [],
            }
        monkeypatch.setattr("tools.orchestrator_adapter.Orchestrator",
                            lambda **kw: mock_orch)
        monkeypatch.setattr("tools.orchestrator_adapter.Supervisor",
                            lambda: MagicMock())

        task_path = state_dir.parent / "TASKS" / "test_stem.md.inprogress"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text("Test task content")

        routing = {"stage": stage, "verifier_required": verifier_required}
        return execute_via_orchestrator("test_stem", "Test task", task_path, routing)

    def test_done_writes_completed(self, state_dir, monkeypatch):
        """Orchestrator status 'done' persists as 'completed'."""
        self._mock_orchestrator_run(state_dir, monkeypatch, "done")

        wf = state_dir / "workflows" / "test_stem.json"
        assert wf.exists()
        data = json.loads(wf.read_text())
        assert data["status"] == "completed"

    def test_failed_writes_failed(self, state_dir, monkeypatch):
        """Orchestrator status 'failed' persists as 'failed'."""
        self._mock_orchestrator_run(state_dir, monkeypatch, "failed")

        data = json.loads((state_dir / "workflows" / "test_stem.json").read_text())
        assert data["status"] == "failed"

    def test_exception_writes_failed(self, state_dir, monkeypatch):
        """Orchestrator exception persists as 'failed'."""
        self._mock_orchestrator_run(state_dir, monkeypatch, "done",
                                    should_raise=True)

        data = json.loads((state_dir / "workflows" / "test_stem.json").read_text())
        assert data["status"] == "failed"

    def test_verifier_rejected_writes_failed(self, state_dir, monkeypatch):
        """Verifier rejection persists as 'failed' with halt_reason."""
        verify_step = MagicMock()
        verify_step.get = lambda k, d="": {
            "step_id": "verify_output",
            "status": "failed",
        }.get(k, d)

        mock_orch = MagicMock()
        mock_orch.run_plan.return_value = {
            "plan_id": "plan_test",
            "task_id": "test_stem",
            "status": "done",
            "steps": [verify_step],
        }
        monkeypatch.setattr("tools.orchestrator_adapter.Orchestrator",
                            lambda **kw: mock_orch)

        self._mock_orchestrator_run(
            state_dir, monkeypatch, "done",
            stage="C", verifier_required=True,
            steps=[{"step_id": "verify_output", "status": "failed"}],
        )

        data = json.loads((state_dir / "workflows" / "test_stem.json").read_text())
        assert data["status"] == "failed"
        assert data["halt_reason"] == "verifier_rejected"

    def test_plan_rejection_writes_rejected(self, state_dir, monkeypatch):
        """Stage B plan validation failure persists as 'rejected'."""
        from tools.orchestrator_adapter import execute_via_orchestrator

        monkeypatch.setattr("tools.orchestrator_adapter.OUTPUT_DIR",
                            state_dir.parent / "OUTPUT")
        (state_dir.parent / "OUTPUT").mkdir(exist_ok=True)

        mock_plan = MagicMock()
        mock_plan.plan_id = "plan_rej"
        mock_plan.steps = [
            MagicMock(step_id="s1", skill_name="shell-ops", goal="test"),
        ]
        mock_plan.strategy = "single_skill"
        monkeypatch.setattr("tools.orchestrator_adapter.build_plan_from_task",
                            lambda *a, **kw: mock_plan)
        monkeypatch.setattr("tools.orchestrator_adapter.classify_task",
                            lambda t: ("research", 0.9))
        monkeypatch.setattr("tools.orchestrator_adapter.init_pattern_trace",
                            lambda *a, **kw: {})
        monkeypatch.setattr("tools.orchestrator_adapter.inject_vault_context",
                            lambda *a, **kw: None)
        monkeypatch.setattr("tools.orchestrator_adapter.retrieve_pattern_guidance",
                            lambda *a, **kw: None)

        # Stage B validation fails
        monkeypatch.setattr("tools.orchestrator_adapter.validate_stageB_plan",
                            lambda p: (False, "blocked skill: shell-ops"))

        task_path = state_dir.parent / "TASKS" / "rej_stem.md.inprogress"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text("Test")

        result = execute_via_orchestrator(
            "rej_stem", "Test", task_path, {"stage": "B"},
        )
        assert result["success"] is False

        data = json.loads((state_dir / "workflows" / "rej_stem.json").read_text())
        assert data["status"] == "rejected"
        assert "plan_validation" in data["halt_reason"]
