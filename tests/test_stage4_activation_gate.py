"""Phase 7.16 — Tests for Stage 4 activation gate (system_inspect readiness).

Tests cover:
  1. ready_to_activate_stage4 — all criteria pass, plan valid
  2. hold_stage4_activation — soft concerns (orphans, stale leases, rollbacks)
  3. block_stage4_activation — hard failures (Stage 3 unstable, bad plan, etc.)
  4. Plan validation (valid, missing fields, bad structure)
  5. Artifact generation and rendering
  6. Gate does not activate system
"""

import json
import time
from pathlib import Path

import pytest

from agents.rollout_gate import (
    _PLAN_REQUIRED_FIELDS,
    _PLAN_REQUIRED_NONEMPTY,
    STAGE4_ABORT_CONDITIONS,
    STAGE4_ACTIVATION_PREREQUISITES,
    STAGE4_ALLOWED_OPERATIONS,
    STAGE4_ALLOWED_SKILLS,
    STAGE4_BLOCKED_OPERATIONS,
    STAGE4_BLOCKED_SKILLS,
    STAGE4_SUCCESS_CRITERIA,
    RolloutCriterion,
    decide_activation_gate,
    evaluate_activation_gate,
    evaluate_activation_gate_criteria,
    render_activation_gate_markdown,
    validate_stage4_plan,
    write_activation_gate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_workflow(task_class: str, status: str = "completed", **extra) -> dict:
    """Create a minimal workflow record."""
    return {"task_class": task_class, "status": status, **extra}


def _make_valid_plan() -> dict:
    """Create a structurally valid Stage 4 rollout plan."""
    return {
        "plan_status": "draft",
        "initial_scope": "system_inspect ONLY",
        "allowed_operations": sorted(STAGE4_ALLOWED_OPERATIONS),
        "blocked_operations": sorted(STAGE4_BLOCKED_OPERATIONS),
        "allowed_skills": sorted(STAGE4_ALLOWED_SKILLS),
        "blocked_skills": sorted(STAGE4_BLOCKED_SKILLS),
        "activation_prerequisites": dict(STAGE4_ACTIVATION_PREREQUISITES),
        "success_criteria": dict(STAGE4_SUCCESS_CRITERIA),
        "abort_conditions": list(STAGE4_ABORT_CONDITIONS),
        "rollback_procedure": [
            "Remove system from supported_classes",
            "Reset rollout_stage",
            "Restart watcher",
        ],
        "system_class_status": "blocked",
        "stage4_evaluation_decision": "ready_for_stage4_planning",
        "generated_at": "2026-03-08T14:00:00Z",
    }


def _populate_healthy_env(root: Path) -> None:
    """Populate a test environment that passes all gates."""
    state = root / "STATE"
    (state / "config").mkdir(parents=True, exist_ok=True)
    (state / "workflows").mkdir(parents=True, exist_ok=True)
    (state / "verifications").mkdir(parents=True, exist_ok=True)
    (root / "LOGS").mkdir(parents=True, exist_ok=True)
    (root / "WORK").mkdir(parents=True, exist_ok=True)

    # Feature flags — Stage 3 active, system blocked
    flags = {
        "phase7_orchestrator": {
            "enabled": True,
            "supported_classes": ["research", "code_review", "code_impl"],
            "stage": "C",
            "rollout_stage": "stage3_research_code_review_code_impl",
            "verifier_required": True,
        },
        "version": 7,
    }
    (state / "config" / "feature_flags.json").write_text(json.dumps(flags))

    # Healthy heartbeat
    hb = {"overall": "healthy", "findings": [], "generated_at": "2026-03-08T10:00:00Z"}
    (state / "heartbeat_multiagent.json").write_text(json.dumps(hb))

    # Clean metrics
    metrics = {
        "contract_success": {"_total": 10},
        "contract_failure": {"_total": 0},
    }
    (state / "metrics.json").write_text(json.dumps(metrics))

    # Activation log — one successful activation, no rollbacks
    log_entry = {
        "attempted_at": "2026-03-07T12:00:00Z",
        "outcome": "activated",
        "decision": "ready_to_expand",
        "reason": "expansion applied",
        "pre_config": {},
        "post_config": {},
        "blocking_criteria": [],
    }
    (state / "activation_log.jsonl").write_text(json.dumps(log_entry) + "\n")

    # 5 code_impl + 3 research workflows (passes all Stage 3/4 gates)
    for i in range(5):
        wf = _make_workflow("code_impl", "completed")
        (state / "workflows" / f"code_impl_{i}.json").write_text(json.dumps(wf))
    for i in range(3):
        wf = _make_workflow("research", "completed")
        (state / "workflows" / f"research_{i}.json").write_text(json.dumps(wf))

    # Valid Stage 4 rollout plan
    (state / "stage4_rollout_plan.json").write_text(
        json.dumps(_make_valid_plan(), indent=2)
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def gate_root(tmp_path):
    """Set up a healthy environment that passes the activation gate."""
    _populate_healthy_env(tmp_path)
    return tmp_path


@pytest.fixture
def healthy_evidence(gate_root):
    """Collect evidence from the healthy environment."""
    from agents.rollout_gate import collect_evidence
    ev = collect_evidence(gate_root)
    ev["_base_path"] = gate_root
    return ev


# ===========================================================================
# Test Plan Validation
# ===========================================================================

class TestPlanValidation:
    """Tests for validate_stage4_plan()."""

    def test_valid_plan_passes(self, gate_root):
        valid, errors = validate_stage4_plan(gate_root)
        assert valid is True
        assert errors == []

    def test_missing_plan_file(self, gate_root):
        (gate_root / "STATE" / "stage4_rollout_plan.json").unlink()
        valid, errors = validate_stage4_plan(gate_root)
        assert valid is False
        assert any("does not exist" in e for e in errors)

    def test_invalid_json(self, gate_root):
        (gate_root / "STATE" / "stage4_rollout_plan.json").write_text("{bad json")
        valid, errors = validate_stage4_plan(gate_root)
        assert valid is False
        assert any("not valid JSON" in e for e in errors)

    def test_not_a_dict(self, gate_root):
        (gate_root / "STATE" / "stage4_rollout_plan.json").write_text("[]")
        valid, errors = validate_stage4_plan(gate_root)
        assert valid is False
        assert any("not a dict" in e for e in errors)

    def test_missing_required_fields(self, gate_root):
        plan = {"plan_status": "draft"}  # missing most fields
        (gate_root / "STATE" / "stage4_rollout_plan.json").write_text(json.dumps(plan))
        valid, errors = validate_stage4_plan(gate_root)
        assert valid is False
        assert any("missing required fields" in e for e in errors)

    def test_empty_required_section(self, gate_root):
        plan = _make_valid_plan()
        plan["allowed_operations"] = []
        (gate_root / "STATE" / "stage4_rollout_plan.json").write_text(json.dumps(plan))
        valid, errors = validate_stage4_plan(gate_root)
        assert valid is False
        assert any("allowed_operations is empty" in e for e in errors)

    def test_non_inspect_ops_in_allowed(self, gate_root):
        plan = _make_valid_plan()
        plan["allowed_operations"].append("deployment")
        (gate_root / "STATE" / "stage4_rollout_plan.json").write_text(json.dumps(plan))
        valid, errors = validate_stage4_plan(gate_root)
        assert valid is False
        assert any("non-inspect ops" in e for e in errors)

    def test_missing_mutation_ops_in_blocked(self, gate_root):
        plan = _make_valid_plan()
        plan["blocked_operations"] = ["deployment"]  # missing most
        (gate_root / "STATE" / "stage4_rollout_plan.json").write_text(json.dumps(plan))
        valid, errors = validate_stage4_plan(gate_root)
        assert valid is False
        assert any("missing mutation ops" in e for e in errors)

    def test_system_not_blocked(self, gate_root):
        plan = _make_valid_plan()
        plan["system_class_status"] = "inspect_only"
        (gate_root / "STATE" / "stage4_rollout_plan.json").write_text(json.dumps(plan))
        valid, errors = validate_stage4_plan(gate_root)
        assert valid is False
        assert any("system_class_status" in e for e in errors)

    def test_required_fields_match_constants(self):
        """All required fields are accounted for in the plan dataclass."""
        for field_name in _PLAN_REQUIRED_FIELDS:
            assert isinstance(field_name, str)
        for field_name in _PLAN_REQUIRED_NONEMPTY:
            assert field_name in _PLAN_REQUIRED_FIELDS


# ===========================================================================
# Test Ready to Activate
# ===========================================================================

class TestReadyToActivate:
    """Tests for clean state → ready_to_activate_stage4."""

    def test_clean_state_is_ready(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        assert gate.decision == "ready_to_activate_stage4"

    def test_all_criteria_pass(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        for c in gate.criteria:
            assert c.passed, f"criterion {c.name} should pass: {c.detail}"

    def test_plan_is_valid(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        assert gate.plan_validation["valid"] is True
        assert gate.plan_validation["errors"] == []

    def test_blocking_criteria_empty(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        assert gate.blocking_criteria == []

    def test_stage3_decision_propagated(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        assert gate.stage3_stability_decision == "stable_continue"

    def test_stage4_eval_decision_propagated(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        assert gate.stage4_evaluation_decision == "ready_for_stage4_planning"

    def test_system_remains_blocked(self, gate_root):
        """The activation gate must NOT modify feature flags."""
        gate = evaluate_activation_gate(gate_root)
        assert gate.decision == "ready_to_activate_stage4"
        # Verify feature flags unchanged
        ff = json.loads(
            (gate_root / "STATE" / "config" / "feature_flags.json").read_text()
        )
        supported = ff["phase7_orchestrator"]["supported_classes"]
        assert "system" not in supported

    def test_criteria_count(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        assert len(gate.criteria) == 10


# ===========================================================================
# Test Block Activation
# ===========================================================================

class TestBlockActivation:
    """Tests for hard failures → block_stage4_activation."""

    def test_unhealthy_heartbeat_blocks(self, gate_root):
        hb = {"overall": "unhealthy", "findings": [{"severity": "unhealthy"}]}
        (gate_root / "STATE" / "heartbeat_multiagent.json").write_text(json.dumps(hb))
        gate = evaluate_activation_gate(gate_root)
        assert gate.decision == "block_stage4_activation"
        assert "heartbeat_healthy" in gate.blocking_criteria

    def test_degraded_heartbeat_blocks(self, gate_root):
        """Heartbeat must be exactly 'healthy', not just 'not unhealthy'."""
        hb = {"overall": "degraded", "findings": []}
        (gate_root / "STATE" / "heartbeat_multiagent.json").write_text(json.dumps(hb))
        gate = evaluate_activation_gate(gate_root)
        assert gate.decision == "block_stage4_activation"
        assert "heartbeat_healthy" in gate.blocking_criteria

    def test_policy_violations_block(self, gate_root):
        (gate_root / "STATE" / "policy_denials.jsonl").write_text(
            json.dumps({"reason": "test violation"}) + "\n"
        )
        gate = evaluate_activation_gate(gate_root)
        assert gate.decision == "block_stage4_activation"
        assert "no_policy_violations" in gate.blocking_criteria

    def test_budget_exhaustions_block(self, gate_root):
        wf = _make_workflow("code_impl", "halted", halt_reason="budget exhausted")
        (gate_root / "STATE" / "workflows" / "budget_halt.json").write_text(
            json.dumps(wf)
        )
        gate = evaluate_activation_gate(gate_root)
        assert gate.decision == "block_stage4_activation"
        assert "no_budget_exhaustions" in gate.blocking_criteria

    def test_system_already_enabled_blocks(self, gate_root):
        ff = json.loads(
            (gate_root / "STATE" / "config" / "feature_flags.json").read_text()
        )
        ff["phase7_orchestrator"]["supported_classes"].append("system")
        (gate_root / "STATE" / "config" / "feature_flags.json").write_text(
            json.dumps(ff)
        )
        gate = evaluate_activation_gate(gate_root)
        assert gate.decision == "block_stage4_activation"
        assert "system_class_blocked" in gate.blocking_criteria

    def test_invalid_plan_blocks(self, gate_root):
        (gate_root / "STATE" / "stage4_rollout_plan.json").write_text("{bad")
        gate = evaluate_activation_gate(gate_root)
        assert gate.decision == "block_stage4_activation"
        assert "plan_valid" in gate.blocking_criteria

    def test_missing_plan_blocks(self, gate_root):
        (gate_root / "STATE" / "stage4_rollout_plan.json").unlink()
        gate = evaluate_activation_gate(gate_root)
        assert gate.decision == "block_stage4_activation"
        assert "plan_valid" in gate.blocking_criteria

    def test_stage3_unstable_cascades_to_block(self, gate_root):
        """If Stage 3 is not stable_continue, the activation gate blocks."""
        # Make Stage 3 unstable by adding policy violations
        (gate_root / "STATE" / "policy_denials.jsonl").write_text(
            json.dumps({"reason": "test"}) + "\n"
        )
        gate = evaluate_activation_gate(gate_root)
        assert gate.decision == "block_stage4_activation"
        # Both stage3_stable and no_policy_violations should fail
        assert "stage3_stable" in gate.blocking_criteria

    def test_insufficient_code_impl_cascades_to_block(self, gate_root):
        """If Stage 4 evaluation gate doesn't pass, activation is blocked."""
        # Remove code_impl workflows to fail Stage 4 eval
        for f in (gate_root / "STATE" / "workflows").glob("code_impl_*.json"):
            f.unlink()
        # Add 1 code_impl (not enough for Stage 4's threshold of 5)
        wf = _make_workflow("code_impl", "completed")
        (gate_root / "STATE" / "workflows" / "ci_0.json").write_text(json.dumps(wf))
        gate = evaluate_activation_gate(gate_root)
        # Stage 4 eval should be hold or block, which cascades
        assert gate.decision == "block_stage4_activation"
        assert "stage4_evaluation_ready" in gate.blocking_criteria


# ===========================================================================
# Test Hold Activation
# ===========================================================================

class TestHoldActivation:
    """Tests for soft concerns → hold_stage4_activation."""

    def test_rollback_history_cascades_to_block(self, gate_root):
        """A rollback event cascades: Stage 4 eval holds → activation blocks.

        Rollback event fails Stage 4 evaluation's no_rollback_history (soft)
        → Stage 4 eval becomes hold_stage4 (not ready_for_stage4_planning)
        → Activation gate's stage4_evaluation_ready hard criterion fails
        → block_stage4_activation.
        """
        log = (gate_root / "STATE" / "activation_log.jsonl")
        existing = log.read_text()
        rollback = json.dumps({
            "attempted_at": "2026-03-07T13:00:00Z",
            "outcome": "rollback",
            "decision": "rollback_recommended",
            "reason": "test rollback",
            "pre_config": {},
            "post_config": {},
            "blocking_criteria": [],
        })
        log.write_text(existing + rollback + "\n")
        gate = evaluate_activation_gate(gate_root)
        assert gate.decision == "block_stage4_activation"
        assert "stage4_evaluation_ready" in gate.blocking_criteria

    def test_orphaned_agents_cascades_to_block(self, gate_root):
        """Orphaned agents cause Stage 3 instability → cascades to block."""
        # Create orphaned agent (started > 600s ago)
        runtime_dir = gate_root / "STATE" / "agents" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        agent = {"status": "executing", "started_at": time.time() - 700}
        (runtime_dir / "orphan.json").write_text(json.dumps(agent))
        gate = evaluate_activation_gate(gate_root)
        # Orphaned agents fail Stage 3 soft → Stage 3 holds → Stage 4 eval
        # blocks on stage3_stable → activation blocks
        assert gate.decision == "block_stage4_activation"

    def test_stale_leases_cascades_to_block(self, gate_root):
        """Stale leases cause Stage 3 instability → cascades to block."""
        lease_dir = gate_root / "STATE" / "leases"
        lease_dir.mkdir(parents=True, exist_ok=True)
        lease = {"expires_at": time.time() - 100}
        (lease_dir / "stale.json").write_text(json.dumps(lease))
        gate = evaluate_activation_gate(gate_root)
        assert gate.decision == "block_stage4_activation"


# ===========================================================================
# Test Decision Logic (unit)
# ===========================================================================

class TestDecisionLogic:
    """Unit tests for decide_activation_gate()."""

    def _make_criteria(self, overrides: dict | None = None) -> list[RolloutCriterion]:
        """Create a full set of passing criteria, then override specifics."""
        base = [
            RolloutCriterion("stage3_stable", True, "stable_continue", "stable_continue", "ok", "hard"),
            RolloutCriterion(
                "stage4_evaluation_ready", True,
                "ready_for_stage4_planning", "ready_for_stage4_planning",
                "ok", "hard",
            ),
            RolloutCriterion("heartbeat_healthy", True, "healthy", "healthy", "ok", "hard"),
            RolloutCriterion("no_policy_violations", True, 0, 0, "ok", "hard"),
            RolloutCriterion("no_budget_exhaustions", True, 0, 0, "ok", "hard"),
            RolloutCriterion("system_class_blocked", True, "blocked", "blocked", "ok", "hard"),
            RolloutCriterion("plan_valid", True, "valid", "valid", "ok", "hard"),
            RolloutCriterion("no_rollback_history", True, 0, 0, "ok", "soft"),
            RolloutCriterion("no_orphaned_agents", True, 0, 0, "ok", "soft"),
            RolloutCriterion("no_stale_leases", True, 0, 0, "ok", "soft"),
        ]
        if overrides:
            for c in base:
                if c.name in overrides:
                    c.passed = overrides[c.name]
        return base

    def test_all_pass_is_ready(self):
        criteria = self._make_criteria()
        decision, _ = decide_activation_gate(criteria)
        assert decision == "ready_to_activate_stage4"

    def test_hard_failure_blocks(self):
        criteria = self._make_criteria({"heartbeat_healthy": False})
        decision, _ = decide_activation_gate(criteria)
        assert decision == "block_stage4_activation"

    def test_soft_failure_holds(self):
        criteria = self._make_criteria({"no_rollback_history": False})
        decision, _ = decide_activation_gate(criteria)
        assert decision == "hold_stage4_activation"

    def test_hard_trumps_soft(self):
        """Hard failure takes precedence over soft failure."""
        criteria = self._make_criteria({
            "plan_valid": False,
            "no_stale_leases": False,
        })
        decision, _ = decide_activation_gate(criteria)
        assert decision == "block_stage4_activation"

    def test_multiple_hard_failures(self):
        criteria = self._make_criteria({
            "stage3_stable": False,
            "heartbeat_healthy": False,
            "plan_valid": False,
        })
        decision, action = decide_activation_gate(criteria)
        assert decision == "block_stage4_activation"
        assert "stage3_stable" in action
        assert "heartbeat_healthy" in action
        assert "plan_valid" in action

    def test_multiple_soft_failures(self):
        criteria = self._make_criteria({
            "no_rollback_history": False,
            "no_orphaned_agents": False,
        })
        decision, action = decide_activation_gate(criteria)
        assert decision == "hold_stage4_activation"
        assert "no_rollback_history" in action
        assert "no_orphaned_agents" in action


# ===========================================================================
# Test Artifact Generation
# ===========================================================================

class TestArtifactGeneration:
    """Tests for rendering and writing artifacts."""

    def test_markdown_contains_decision(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        md = render_activation_gate_markdown(gate)
        assert "ready_to_activate_stage4" in md

    def test_markdown_contains_header(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        md = render_activation_gate_markdown(gate)
        assert "Phase 7.16" in md
        assert "Stage 4 Activation Gate" in md

    def test_markdown_contains_criteria_table(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        md = render_activation_gate_markdown(gate)
        assert "Activation Gate Criteria" in md
        assert "stage3_stable" in md
        assert "plan_valid" in md

    def test_markdown_contains_plan_validation(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        md = render_activation_gate_markdown(gate)
        assert "Plan Validation" in md
        assert "structurally valid" in md

    def test_markdown_contains_evidence_summary(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        md = render_activation_gate_markdown(gate)
        assert "Evidence Summary" in md

    def test_markdown_contains_scope_disclaimer(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        md = render_activation_gate_markdown(gate)
        assert "does NOT" in md

    def test_blocked_markdown_shows_blocking(self, gate_root):
        """When blocked, markdown shows blocking criteria section."""
        (gate_root / "STATE" / "stage4_rollout_plan.json").unlink()
        gate = evaluate_activation_gate(gate_root)
        md = render_activation_gate_markdown(gate)
        assert "Blocking Criteria" in md
        assert "plan_valid" in md

    def test_write_creates_files(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        md_path, json_path = write_activation_gate(gate, gate_root)
        assert md_path.exists()
        assert json_path.exists()

    def test_written_json_is_valid(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        _, json_path = write_activation_gate(gate, gate_root)
        data = json.loads(json_path.read_text())
        assert data["decision"] == "ready_to_activate_stage4"
        assert data["plan_validation"]["valid"] is True

    def test_written_md_path(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        md_path, _ = write_activation_gate(gate, gate_root)
        assert md_path.name == "phase7_stage4_activation_gate.md"
        assert md_path.parent.name == "WORK"

    def test_written_json_path(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        _, json_path = write_activation_gate(gate, gate_root)
        assert json_path.name == "stage4_activation_gate.json"
        assert json_path.parent.name == "STATE"

    def test_to_dict_roundtrip(self, gate_root):
        gate = evaluate_activation_gate(gate_root)
        d = gate.to_dict()
        assert d["decision"] == gate.decision
        assert len(d["criteria"]) == len(gate.criteria)
        assert d["plan_validation"] == gate.plan_validation
        assert d["blocking_criteria"] == gate.blocking_criteria


# ===========================================================================
# Test System Remains Blocked
# ===========================================================================

class TestSystemRemainsBlocked:
    """Verify that the activation gate never modifies system state."""

    def test_feature_flags_unchanged_after_ready(self, gate_root):
        before = (gate_root / "STATE" / "config" / "feature_flags.json").read_text()
        gate = evaluate_activation_gate(gate_root)
        assert gate.decision == "ready_to_activate_stage4"
        after = (gate_root / "STATE" / "config" / "feature_flags.json").read_text()
        assert before == after

    def test_feature_flags_unchanged_after_block(self, gate_root):
        (gate_root / "STATE" / "stage4_rollout_plan.json").unlink()
        before = (gate_root / "STATE" / "config" / "feature_flags.json").read_text()
        gate = evaluate_activation_gate(gate_root)
        assert gate.decision == "block_stage4_activation"
        after = (gate_root / "STATE" / "config" / "feature_flags.json").read_text()
        assert before == after

    def test_plan_json_unchanged(self, gate_root):
        before = (gate_root / "STATE" / "stage4_rollout_plan.json").read_text()
        evaluate_activation_gate(gate_root)
        after = (gate_root / "STATE" / "stage4_rollout_plan.json").read_text()
        assert before == after

    def test_activation_log_unchanged(self, gate_root):
        before = (gate_root / "STATE" / "activation_log.jsonl").read_text()
        evaluate_activation_gate(gate_root)
        after = (gate_root / "STATE" / "activation_log.jsonl").read_text()
        assert before == after


# ===========================================================================
# Test Criteria Evaluation (unit)
# ===========================================================================

class TestCriteriaEvaluation:
    """Unit tests for evaluate_activation_gate_criteria()."""

    def _base_evidence(self, tmp_path) -> dict:
        """Minimal healthy evidence dict."""
        (tmp_path / "STATE").mkdir(parents=True, exist_ok=True)
        (tmp_path / "STATE" / "activation_log.jsonl").write_text("")
        return {
            "heartbeat_overall": "healthy",
            "policy_violations": 0,
            "budget_exhaustions": 0,
            "supported_classes": ["research", "code_review", "code_impl"],
            "orphaned_agents": 0,
            "stale_leases": 0,
            "_base_path": tmp_path,
        }

    def test_all_pass(self, tmp_path):
        ev = self._base_evidence(tmp_path)
        criteria = evaluate_activation_gate_criteria(
            ev, "stable_continue", "ready_for_stage4_planning", True, [],
        )
        assert all(c.passed for c in criteria)

    def test_heartbeat_must_be_healthy(self, tmp_path):
        ev = self._base_evidence(tmp_path)
        ev["heartbeat_overall"] = "degraded"
        criteria = evaluate_activation_gate_criteria(
            ev, "stable_continue", "ready_for_stage4_planning", True, [],
        )
        hb = next(c for c in criteria if c.name == "heartbeat_healthy")
        assert not hb.passed

    def test_plan_invalid_fails(self, tmp_path):
        ev = self._base_evidence(tmp_path)
        criteria = evaluate_activation_gate_criteria(
            ev, "stable_continue", "ready_for_stage4_planning",
            False, ["missing fields"],
        )
        pv = next(c for c in criteria if c.name == "plan_valid")
        assert not pv.passed
        assert "missing fields" in pv.detail

    def test_stage4_eval_not_ready_fails(self, tmp_path):
        ev = self._base_evidence(tmp_path)
        criteria = evaluate_activation_gate_criteria(
            ev, "stable_continue", "hold_stage4", True, [],
        )
        s4 = next(c for c in criteria if c.name == "stage4_evaluation_ready")
        assert not s4.passed

    def test_hard_vs_soft_severity(self, tmp_path):
        ev = self._base_evidence(tmp_path)
        criteria = evaluate_activation_gate_criteria(
            ev, "stable_continue", "ready_for_stage4_planning", True, [],
        )
        hard_names = {c.name for c in criteria if c.severity == "hard"}
        soft_names = {c.name for c in criteria if c.severity == "soft"}
        assert hard_names == {
            "stage3_stable", "stage4_evaluation_ready", "heartbeat_healthy",
            "no_policy_violations", "no_budget_exhaustions",
            "system_class_blocked", "plan_valid",
        }
        assert soft_names == {
            "no_rollback_history", "no_orphaned_agents", "no_stale_leases",
        }

    def test_criteria_count_is_10(self, tmp_path):
        ev = self._base_evidence(tmp_path)
        criteria = evaluate_activation_gate_criteria(
            ev, "stable_continue", "ready_for_stage4_planning", True, [],
        )
        assert len(criteria) == 10
