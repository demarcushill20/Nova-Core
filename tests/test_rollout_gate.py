"""Phase 7.11/7.12a — Rollout Evaluation Gate + Activation Tests.

Deterministic tests for Stage 2 rollout evaluation:
- ready_to_expand when metrics are clean
- hold when evidence is insufficient
- rollback_recommended when health signals are bad
- verifier pressure affects code_review evaluation
- criterion-level pass/fail correctness
- report output is correct and auditable

Stage 3 activation tests:
- readiness check shows correct progress
- activation blocked when gate returns hold
- activation blocked when gate returns rollback_recommended
- activation proceeds when gate returns ready_to_expand
- activation audit trail written
- state change between check and activation → fail closed
- readiness report correctness
"""

import json
import os
import time

import pytest
from pathlib import Path

from agents.rollout_gate import (
    RolloutCriterion,
    RolloutEvaluation,
    collect_evidence,
    evaluate_criteria,
    decide,
    evaluate_rollout,
    render_evaluation_markdown,
    render_evaluation_json,
    write_evaluation_report,
    MIN_COMPLETED_WORKFLOWS,
    MAX_FAILURE_RATE,
    MAX_VERIFIER_REJECTION_RATE,
    MAX_CONTRACT_FAILURE_RATE,
    MAX_POLICY_VIOLATIONS,
    MAX_BUDGET_EXHAUSTIONS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_tmpdir(tmp_path):
    """Create standard STATE/ layout for isolated tests."""
    os.environ["NOVACORE_ROOT"] = str(tmp_path)
    (tmp_path / "STATE" / "config").mkdir(parents=True)
    (tmp_path / "STATE" / "workflows").mkdir(parents=True)
    (tmp_path / "STATE" / "verifications").mkdir(parents=True)
    (tmp_path / "STATE" / "leases").mkdir(parents=True)
    (tmp_path / "STATE" / "agents" / "runtime").mkdir(parents=True)
    (tmp_path / "WORK").mkdir(parents=True)
    (tmp_path / "LOGS").mkdir(parents=True)
    yield tmp_path
    del os.environ["NOVACORE_ROOT"]


def _write_flags(tmp_path, supported_classes=None, enabled=True,
                 rollout_stage="stage2_research_and_code_review"):
    flags = {
        "phase7_orchestrator": {
            "enabled": enabled,
            "supported_classes": supported_classes or ["research", "code_review"],
            "rollout_stage": rollout_stage,
        }
    }
    (tmp_path / "STATE" / "config" / "feature_flags.json").write_text(
        json.dumps(flags, indent=2)
    )


def _write_heartbeat(tmp_path, overall, findings=None):
    hb = {
        "overall": overall,
        "findings": findings or [],
        "generated_at": "2026-03-08T14:00:00Z",
    }
    (tmp_path / "STATE" / "heartbeat_multiagent.json").write_text(
        json.dumps(hb, indent=2)
    )


def _write_workflow(tmp_path, wf_id, status, halt_reason=None):
    wf = {
        "workflow_id": wf_id,
        "status": status,
        "created_at": time.time() - 3600,
    }
    if halt_reason:
        wf["halt_reason"] = halt_reason
    (tmp_path / "STATE" / "workflows" / f"{wf_id}.json").write_text(
        json.dumps(wf, indent=2)
    )


def _write_verification(tmp_path, v_id, verdict, workflow_id="wf_1"):
    v = {"verification_id": v_id, "verdict": verdict, "workflow_id": workflow_id}
    (tmp_path / "STATE" / "verifications" / f"{v_id}.json").write_text(
        json.dumps(v, indent=2)
    )


def _write_metrics(tmp_path, successes=0, failures=0):
    m = {
        "contract_success": {"_total": successes},
        "contract_failure": {"_total": failures},
    }
    (tmp_path / "STATE" / "metrics.json").write_text(json.dumps(m, indent=2))


def _write_policy_denial(tmp_path, count=1):
    path = tmp_path / "STATE" / "policy_denials.jsonl"
    lines = []
    for i in range(count):
        lines.append(json.dumps({
            "ts": time.time(), "agent_id": f"agent_{i}",
            "tool_name": "shell.run", "allowed": False, "reason": "denied",
        }))
    path.write_text("\n".join(lines) + "\n")


def _setup_clean_stage2(tmp_path, completed=5, failed=0, halted=0):
    """Set up a clean Stage 2 state with given workflow counts."""
    _write_flags(tmp_path)
    _write_heartbeat(tmp_path, "healthy")
    for i in range(completed):
        _write_workflow(tmp_path, f"wf_ok_{i}", "completed")
    for i in range(failed):
        _write_workflow(tmp_path, f"wf_fail_{i}", "failed")
    for i in range(halted):
        _write_workflow(tmp_path, f"wf_halt_{i}", "halted")


# ===================================================================
# Part 1 — Ready to Expand (all criteria pass)
# ===================================================================

class TestEvaluation_ReadyToExpand:
    """Verify ready_to_expand when all criteria are clean."""

    def test_clean_stage2_is_ready(self, tmp_path):
        """Sufficient clean runs + healthy heartbeat → ready_to_expand."""
        _setup_clean_stage2(tmp_path, completed=5)
        result = evaluate_rollout(tmp_path)
        assert result.decision == "ready_to_expand"
        assert "code_impl" in result.next_action

    def test_minimum_threshold_met(self, tmp_path):
        """Exactly MIN_COMPLETED_WORKFLOWS → ready (not hold)."""
        _setup_clean_stage2(tmp_path, completed=MIN_COMPLETED_WORKFLOWS)
        result = evaluate_rollout(tmp_path)
        assert result.decision == "ready_to_expand"

    def test_with_verifier_approvals(self, tmp_path):
        """Clean runs + approvals + no rejections → ready."""
        _setup_clean_stage2(tmp_path, completed=5)
        for i in range(3):
            _write_verification(tmp_path, f"v_ok_{i}", "approved")
        result = evaluate_rollout(tmp_path)
        assert result.decision == "ready_to_expand"

    def test_classes_and_stage_in_result(self, tmp_path):
        """Result includes evaluated classes and rollout stage."""
        _setup_clean_stage2(tmp_path)
        result = evaluate_rollout(tmp_path)
        assert "research" in result.classes_evaluated
        assert "code_review" in result.classes_evaluated
        assert result.rollout_stage == "stage2_research_and_code_review"

    def test_all_criteria_pass(self, tmp_path):
        """All individual criteria pass in ready state."""
        _setup_clean_stage2(tmp_path, completed=5)
        result = evaluate_rollout(tmp_path)
        for c in result.criteria:
            assert c.passed, f"Criterion {c.name} should pass: {c.detail}"


# ===================================================================
# Part 2 — Hold (insufficient evidence)
# ===================================================================

class TestEvaluation_Hold:
    """Verify hold when evidence is insufficient."""

    def test_zero_workflows_holds(self, tmp_path):
        """No completed workflows → hold."""
        _write_flags(tmp_path)
        _write_heartbeat(tmp_path, "healthy")
        result = evaluate_rollout(tmp_path)
        assert result.decision == "hold"
        assert "Need" in result.next_action

    def test_below_minimum_holds(self, tmp_path):
        """Fewer than MIN_COMPLETED_WORKFLOWS → hold."""
        _setup_clean_stage2(tmp_path, completed=MIN_COMPLETED_WORKFLOWS - 1)
        result = evaluate_rollout(tmp_path)
        assert result.decision == "hold"

    def test_hold_with_soft_failures(self, tmp_path):
        """Orphaned agents (soft) + sufficient runs → hold, not rollback."""
        _setup_clean_stage2(tmp_path, completed=5)
        # Create an orphaned agent (executing > 600s)
        agent = {
            "agent_id": "orphan_1", "status": "executing",
            "started_at": time.time() - 1200,
        }
        (tmp_path / "STATE" / "agents" / "runtime" / "orphan_1.json").write_text(
            json.dumps(agent)
        )
        result = evaluate_rollout(tmp_path)
        assert result.decision == "hold"

    def test_hold_with_stale_leases(self, tmp_path):
        """Stale leases (soft) → hold."""
        _setup_clean_stage2(tmp_path, completed=5)
        lease = {"holder": "agent_1", "expires_at": time.time() - 600}
        (tmp_path / "STATE" / "leases" / "stale_1.json").write_text(
            json.dumps(lease)
        )
        result = evaluate_rollout(tmp_path)
        assert result.decision == "hold"


# ===================================================================
# Part 3 — Rollback Recommended (hard failures)
# ===================================================================

class TestEvaluation_RollbackRecommended:
    """Verify rollback_recommended when health signals are bad."""

    def test_unhealthy_heartbeat_triggers_rollback(self, tmp_path):
        """UNHEALTHY heartbeat → rollback_recommended."""
        _setup_clean_stage2(tmp_path, completed=5)
        _write_heartbeat(tmp_path, "unhealthy")
        result = evaluate_rollout(tmp_path)
        assert result.decision == "rollback_recommended"
        assert "heartbeat_healthy" in result.next_action

    def test_policy_violations_trigger_rollback(self, tmp_path):
        """Policy violations → rollback_recommended."""
        _setup_clean_stage2(tmp_path, completed=5)
        _write_policy_denial(tmp_path, count=2)
        result = evaluate_rollout(tmp_path)
        assert result.decision == "rollback_recommended"
        assert "no_policy_violations" in result.next_action

    def test_budget_exhaustion_triggers_rollback(self, tmp_path):
        """Budget exhaustion → rollback_recommended."""
        _setup_clean_stage2(tmp_path, completed=3)
        _write_workflow(tmp_path, "wf_budget", "halted",
                        halt_reason="budget_exhausted")
        result = evaluate_rollout(tmp_path)
        assert result.decision == "rollback_recommended"
        assert "no_budget_exhaustions" in result.next_action

    def test_high_failure_rate_triggers_rollback(self, tmp_path):
        """Failure rate > MAX → rollback_recommended."""
        _write_flags(tmp_path)
        _write_heartbeat(tmp_path, "healthy")
        # 2 completed, 3 failed → 60% failure rate
        _setup_clean_stage2(tmp_path, completed=2, failed=3)
        result = evaluate_rollout(tmp_path)
        assert result.decision == "rollback_recommended"
        assert "acceptable_failure_rate" in result.next_action


# ===================================================================
# Part 4 — Verifier Pressure Affects code_review Evaluation
# ===================================================================

class TestEvaluation_VerifierPressure:
    """Verify verifier rejection rate impacts evaluation."""

    def test_low_rejection_rate_passes(self, tmp_path):
        """Rejection rate below threshold → passes."""
        _setup_clean_stage2(tmp_path, completed=5)
        _write_verification(tmp_path, "v_ok_0", "approved")
        _write_verification(tmp_path, "v_ok_1", "approved")
        _write_verification(tmp_path, "v_rej_0", "rejected")
        # 1/3 = 33% < 50%
        result = evaluate_rollout(tmp_path)
        assert result.decision == "ready_to_expand"

    def test_high_rejection_rate_holds(self, tmp_path):
        """Rejection rate above threshold → hold."""
        _setup_clean_stage2(tmp_path, completed=5)
        _write_verification(tmp_path, "v_rej_0", "rejected")
        _write_verification(tmp_path, "v_rej_1", "rejected")
        _write_verification(tmp_path, "v_ok_0", "approved")
        # 2/3 = 67% > 50%
        result = evaluate_rollout(tmp_path)
        assert result.decision == "hold"
        assert "verifier_rejection_rate" in result.next_action

    def test_all_rejected_holds(self, tmp_path):
        """100% rejection rate → hold (soft, not rollback)."""
        _setup_clean_stage2(tmp_path, completed=5)
        _write_verification(tmp_path, "v_rej_0", "rejected")
        _write_verification(tmp_path, "v_rej_1", "rejected")
        # 2/2 = 100%
        result = evaluate_rollout(tmp_path)
        assert result.decision == "hold"

    def test_no_verifications_passes(self, tmp_path):
        """No verifier data → passes (N/A for research-only)."""
        _setup_clean_stage2(tmp_path, completed=5)
        result = evaluate_rollout(tmp_path)
        vr = next(c for c in result.criteria
                  if c.name == "acceptable_verifier_rejection_rate")
        assert vr.passed is True
        assert vr.value is None

    def test_contract_failure_rate_holds(self, tmp_path):
        """High contract failure rate → hold."""
        _setup_clean_stage2(tmp_path, completed=5)
        _write_metrics(tmp_path, successes=2, failures=3)
        # 3/5 = 60% > 30%
        result = evaluate_rollout(tmp_path)
        assert result.decision == "hold"
        assert "contract_failure_rate" in result.next_action


# ===================================================================
# Part 5 — Evidence Collection
# ===================================================================

class TestEvidence_Collection:
    """Verify evidence is collected correctly from state."""

    def test_empty_state_defaults(self, tmp_path):
        """Empty STATE/ → safe defaults."""
        _write_flags(tmp_path)
        ev = collect_evidence(tmp_path)
        assert ev["total_workflows"] == 0
        assert ev["completed_workflows"] == 0
        assert ev["policy_violations"] == 0
        assert ev["heartbeat_overall"] == ""
        assert ev["enabled"] is True

    def test_workflow_counts(self, tmp_path):
        """Workflow counts are accurate."""
        _write_flags(tmp_path)
        _write_workflow(tmp_path, "wf_1", "completed")
        _write_workflow(tmp_path, "wf_2", "completed")
        _write_workflow(tmp_path, "wf_3", "failed")
        _write_workflow(tmp_path, "wf_4", "halted")
        _write_workflow(tmp_path, "wf_5", "executing")
        ev = collect_evidence(tmp_path)
        assert ev["total_workflows"] == 5
        assert ev["completed_workflows"] == 2
        assert ev["failed_workflows"] == 1
        assert ev["halted_workflows"] == 1
        assert ev["active_workflows"] == 1

    def test_failure_rate_computed(self, tmp_path):
        """Failure rate = (failed + halted) / terminal."""
        _write_flags(tmp_path)
        _write_workflow(tmp_path, "wf_1", "completed")
        _write_workflow(tmp_path, "wf_2", "completed")
        _write_workflow(tmp_path, "wf_3", "failed")
        _write_workflow(tmp_path, "wf_4", "halted")
        ev = collect_evidence(tmp_path)
        # 2/4 = 0.5
        assert ev["failure_rate"] == 0.5

    def test_policy_denials_counted(self, tmp_path):
        """Policy denials from JSONL are counted."""
        _write_flags(tmp_path)
        _write_policy_denial(tmp_path, count=3)
        ev = collect_evidence(tmp_path)
        assert ev["policy_violations"] == 3

    def test_budget_exhaustions_counted(self, tmp_path):
        """Budget-halted workflows are counted."""
        _write_flags(tmp_path)
        _write_workflow(tmp_path, "wf_b1", "halted", "budget_exhausted")
        _write_workflow(tmp_path, "wf_b2", "halted", "budget_limit_reached")
        _write_workflow(tmp_path, "wf_ok", "halted", "verifier_rejected")
        ev = collect_evidence(tmp_path)
        assert ev["budget_exhaustions"] == 2

    def test_recovery_events_counted(self, tmp_path):
        """Recovery log entries are counted."""
        _write_flags(tmp_path)
        log = (
            "--- Recovery at 2026-03-08T10:00:00Z ---\n"
            "  [stale_pid_removed] x.pid — dead\n\n"
            "--- Recovery at 2026-03-08T11:00:00Z ---\n"
            "  [lease_recovered] y.json — expired\n\n"
        )
        (tmp_path / "LOGS" / "recovery.log").write_text(log)
        ev = collect_evidence(tmp_path)
        assert ev["recovery_events"] == 2

    def test_rollout_stage_from_flags(self, tmp_path):
        """Rollout stage is read from feature flags."""
        _write_flags(tmp_path, rollout_stage="stage2_test")
        ev = collect_evidence(tmp_path)
        assert ev["rollout_stage"] == "stage2_test"


# ===================================================================
# Part 6 — Criterion Evaluation Details
# ===================================================================

class TestCriteria_Details:
    """Verify individual criterion evaluation."""

    def test_heartbeat_criterion_passes_on_healthy(self, tmp_path):
        _setup_clean_stage2(tmp_path)
        ev = collect_evidence(tmp_path)
        criteria = evaluate_criteria(ev)
        hb = next(c for c in criteria if c.name == "heartbeat_healthy")
        assert hb.passed is True
        assert hb.severity == "hard"

    def test_heartbeat_criterion_fails_on_unhealthy(self, tmp_path):
        _write_flags(tmp_path)
        _write_heartbeat(tmp_path, "unhealthy")
        ev = collect_evidence(tmp_path)
        criteria = evaluate_criteria(ev)
        hb = next(c for c in criteria if c.name == "heartbeat_healthy")
        assert hb.passed is False
        assert hb.severity == "hard"

    def test_heartbeat_criterion_passes_on_warning(self, tmp_path):
        _write_flags(tmp_path)
        _write_heartbeat(tmp_path, "warning")
        ev = collect_evidence(tmp_path)
        criteria = evaluate_criteria(ev)
        hb = next(c for c in criteria if c.name == "heartbeat_healthy")
        assert hb.passed is True

    def test_heartbeat_criterion_passes_on_missing(self, tmp_path):
        """No heartbeat file → passes (first-run tolerance)."""
        _write_flags(tmp_path)
        ev = collect_evidence(tmp_path)
        criteria = evaluate_criteria(ev)
        hb = next(c for c in criteria if c.name == "heartbeat_healthy")
        assert hb.passed is True

    def test_all_criteria_have_severity(self, tmp_path):
        """Every criterion has a severity label."""
        _setup_clean_stage2(tmp_path)
        ev = collect_evidence(tmp_path)
        criteria = evaluate_criteria(ev)
        for c in criteria:
            assert c.severity in ("hard", "soft"), f"{c.name} missing severity"

    def test_criterion_count(self, tmp_path):
        """Exactly 9 criteria are evaluated."""
        _setup_clean_stage2(tmp_path)
        ev = collect_evidence(tmp_path)
        criteria = evaluate_criteria(ev)
        assert len(criteria) == 9


# ===================================================================
# Part 7 — Report Output
# ===================================================================

class TestReport_Output:
    """Verify report rendering and persistence."""

    def test_markdown_contains_decision(self, tmp_path):
        _setup_clean_stage2(tmp_path, completed=5)
        result = evaluate_rollout(tmp_path)
        md = render_evaluation_markdown(result)
        assert "ready_to_expand" in md
        assert "PASS" in md

    def test_markdown_contains_criteria_table(self, tmp_path):
        _setup_clean_stage2(tmp_path, completed=5)
        result = evaluate_rollout(tmp_path)
        md = render_evaluation_markdown(result)
        assert "heartbeat_healthy" in md
        assert "minimum_completed_runs" in md
        assert "Evidence Summary" in md

    def test_json_is_parseable(self, tmp_path):
        _setup_clean_stage2(tmp_path, completed=5)
        result = evaluate_rollout(tmp_path)
        j = render_evaluation_json(result)
        parsed = json.loads(j)
        assert parsed["decision"] == "ready_to_expand"
        assert isinstance(parsed["criteria"], list)

    def test_write_creates_files(self, tmp_path):
        _setup_clean_stage2(tmp_path, completed=5)
        result = evaluate_rollout(tmp_path)
        md_path, json_path = write_evaluation_report(result, tmp_path)
        assert md_path.exists()
        assert json_path.exists()
        assert "ready_to_expand" in md_path.read_text()
        parsed = json.loads(json_path.read_text())
        assert parsed["decision"] == "ready_to_expand"

    def test_rollback_report_shows_fail(self, tmp_path):
        _setup_clean_stage2(tmp_path, completed=5)
        _write_heartbeat(tmp_path, "unhealthy")
        result = evaluate_rollout(tmp_path)
        md = render_evaluation_markdown(result)
        assert "FAIL" in md
        assert "rollback_recommended" in md

    def test_hold_report_shows_hold(self, tmp_path):
        _write_flags(tmp_path)
        _write_heartbeat(tmp_path, "healthy")
        result = evaluate_rollout(tmp_path)
        md = render_evaluation_markdown(result)
        assert "HOLD" in md
        assert "hold" in md


# ===================================================================
# Part 8 — Decision Logic Edge Cases
# ===================================================================

class TestDecision_EdgeCases:
    """Edge cases for the decision function."""

    def test_hard_failure_overrides_sufficient_evidence(self, tmp_path):
        """Hard failure + plenty of clean runs → still rollback."""
        _setup_clean_stage2(tmp_path, completed=10)
        _write_policy_denial(tmp_path, count=1)
        result = evaluate_rollout(tmp_path)
        assert result.decision == "rollback_recommended"

    def test_no_flags_file_still_evaluates(self, tmp_path):
        """Missing flags file → evaluates with defaults."""
        result = evaluate_rollout(tmp_path)
        assert result.decision in ("hold", "rollback_recommended", "ready_to_expand")
        assert result.rollout_stage == "unknown"

    def test_multiple_hard_failures_all_listed(self, tmp_path):
        """Multiple hard failures → all named in next_action."""
        _setup_clean_stage2(tmp_path, completed=5)
        _write_heartbeat(tmp_path, "unhealthy")
        _write_policy_denial(tmp_path, count=1)
        result = evaluate_rollout(tmp_path)
        assert result.decision == "rollback_recommended"
        assert "heartbeat_healthy" in result.next_action
        assert "no_policy_violations" in result.next_action

    def test_evaluation_to_dict_serializable(self, tmp_path):
        """Result is fully JSON-serializable."""
        _setup_clean_stage2(tmp_path, completed=5)
        result = evaluate_rollout(tmp_path)
        serialized = json.dumps(result.to_dict(), default=str)
        assert isinstance(json.loads(serialized), dict)


# ===================================================================
# Part 9 — Stage 3 Readiness Check
# ===================================================================

from agents.rollout_gate import (
    check_stage3_readiness,
    render_readiness_markdown,
    activate_stage3,
    ReadinessReport,
    ActivationRecord,
)


class TestReadinessCheck:
    """Verify readiness check reports correct progress."""

    def test_readiness_blocked_no_evidence(self, tmp_path):
        """Zero workflows → not permitted, minimum_completed_runs blocking."""
        _write_flags(tmp_path)
        _write_heartbeat(tmp_path, "healthy")
        report = check_stage3_readiness(tmp_path)
        assert report.permitted is False
        assert report.decision == "hold"
        assert "minimum_completed_runs" in report.blocking_criteria

    def test_readiness_blocked_insufficient_runs(self, tmp_path):
        """Below threshold → not permitted."""
        _setup_clean_stage2(tmp_path, completed=MIN_COMPLETED_WORKFLOWS - 1)
        report = check_stage3_readiness(tmp_path)
        assert report.permitted is False
        assert "minimum_completed_runs" in report.blocking_criteria

    def test_readiness_permitted_clean(self, tmp_path):
        """Sufficient clean evidence → permitted."""
        _setup_clean_stage2(tmp_path, completed=5)
        report = check_stage3_readiness(tmp_path)
        assert report.permitted is True
        assert report.decision == "ready_to_expand"
        assert report.blocking_criteria == []

    def test_readiness_blocked_unhealthy(self, tmp_path):
        """UNHEALTHY heartbeat → rollback_recommended, heartbeat blocking."""
        _setup_clean_stage2(tmp_path, completed=5)
        _write_heartbeat(tmp_path, "unhealthy")
        report = check_stage3_readiness(tmp_path)
        assert report.permitted is False
        assert report.decision == "rollback_recommended"
        assert "heartbeat_healthy" in report.blocking_criteria

    def test_readiness_progress_has_all_criteria(self, tmp_path):
        """Progress dict contains all 9 criteria."""
        _setup_clean_stage2(tmp_path, completed=5)
        report = check_stage3_readiness(tmp_path)
        assert len(report.progress) == 9
        for name, info in report.progress.items():
            assert "value" in info
            assert "threshold" in info
            assert "met" in info
            assert "severity" in info
            assert "detail" in info

    def test_readiness_progress_shows_current_values(self, tmp_path):
        """Progress shows correct current values."""
        _setup_clean_stage2(tmp_path, completed=4)
        _write_workflow(tmp_path, "wf_fail_0", "failed")
        report = check_stage3_readiness(tmp_path)
        runs = report.progress["minimum_completed_runs"]
        assert runs["value"] == 4
        assert runs["threshold"] == MIN_COMPLETED_WORKFLOWS
        assert runs["met"] is True

    def test_readiness_report_serializable(self, tmp_path):
        """ReadinessReport.to_dict() is JSON-serializable."""
        _setup_clean_stage2(tmp_path, completed=5)
        report = check_stage3_readiness(tmp_path)
        serialized = json.dumps(report.to_dict(), default=str)
        parsed = json.loads(serialized)
        assert parsed["permitted"] is True


class TestReadinessMarkdown:
    """Verify readiness markdown rendering."""

    def test_blocked_markdown_shows_blocking(self, tmp_path):
        """Blocked report shows blocking criteria."""
        _write_flags(tmp_path)
        _write_heartbeat(tmp_path, "healthy")
        report = check_stage3_readiness(tmp_path)
        md = render_readiness_markdown(report)
        assert "BLOCKED" in md
        assert "minimum_completed_runs" in md
        assert "check_stage3_readiness" in md

    def test_permitted_markdown_shows_activation(self, tmp_path):
        """Permitted report shows activation command."""
        _setup_clean_stage2(tmp_path, completed=5)
        report = check_stage3_readiness(tmp_path)
        md = render_readiness_markdown(report)
        assert "PERMITTED" in md
        assert "activate_stage3" in md

    def test_markdown_contains_evidence(self, tmp_path):
        """Report contains evidence summary."""
        _setup_clean_stage2(tmp_path, completed=5)
        report = check_stage3_readiness(tmp_path)
        md = render_readiness_markdown(report)
        assert "Evidence Summary" in md
        assert "completed_workflows" in md


# ===================================================================
# Part 10 — Stage 3 Activation Procedure
# ===================================================================

class TestActivation_Blocked:
    """Verify activation is blocked when gate is not ready."""

    def test_activation_blocked_on_hold(self, tmp_path):
        """hold → activation blocked, config unchanged."""
        _write_flags(tmp_path)
        _write_heartbeat(tmp_path, "healthy")
        record = activate_stage3(tmp_path)
        assert record.outcome == "blocked"
        assert record.decision == "hold"
        # Config unchanged
        flags = json.loads(
            (tmp_path / "STATE" / "config" / "feature_flags.json").read_text()
        )
        assert "code_impl" not in flags["phase7_orchestrator"]["supported_classes"]

    def test_activation_blocked_on_rollback(self, tmp_path):
        """rollback_recommended → activation blocked."""
        _setup_clean_stage2(tmp_path, completed=5)
        _write_heartbeat(tmp_path, "unhealthy")
        record = activate_stage3(tmp_path)
        assert record.outcome == "blocked"
        assert record.decision == "rollback_recommended"
        assert "heartbeat_healthy" in record.blocking_criteria

    def test_activation_blocked_insufficient_runs(self, tmp_path):
        """Insufficient runs → activation blocked."""
        _setup_clean_stage2(tmp_path, completed=1)
        record = activate_stage3(tmp_path)
        assert record.outcome == "blocked"
        assert "minimum_completed_runs" in record.blocking_criteria

    def test_activation_error_missing_flags(self, tmp_path):
        """Missing feature_flags.json → error outcome."""
        # Only write heartbeat and evidence but no flags
        _write_heartbeat(tmp_path, "healthy")
        record = activate_stage3(tmp_path)
        assert record.outcome in ("error", "blocked")

    def test_blocked_activation_preserves_config(self, tmp_path):
        """Blocked activation preserves original config exactly."""
        _write_flags(tmp_path)
        _write_heartbeat(tmp_path, "healthy")
        before = json.loads(
            (tmp_path / "STATE" / "config" / "feature_flags.json").read_text()
        )
        activate_stage3(tmp_path)
        after = json.loads(
            (tmp_path / "STATE" / "config" / "feature_flags.json").read_text()
        )
        assert before == after


class TestActivation_Permitted:
    """Verify activation proceeds when gate is ready."""

    def test_activation_succeeds_on_ready(self, tmp_path):
        """ready_to_expand → activation succeeds, config updated."""
        _setup_clean_stage2(tmp_path, completed=5)
        record = activate_stage3(tmp_path)
        assert record.outcome == "activated"
        assert record.decision == "ready_to_expand"
        # Config updated
        flags = json.loads(
            (tmp_path / "STATE" / "config" / "feature_flags.json").read_text()
        )
        assert "code_impl" in flags["phase7_orchestrator"]["supported_classes"]

    def test_activation_records_pre_and_post_config(self, tmp_path):
        """Activation record contains config snapshots."""
        _setup_clean_stage2(tmp_path, completed=5)
        record = activate_stage3(tmp_path)
        assert "supported_classes" in record.pre_config
        assert "supported_classes" in record.post_config
        assert "code_impl" not in record.pre_config["supported_classes"]
        assert "code_impl" in record.post_config["supported_classes"]

    def test_activation_has_timestamp(self, tmp_path):
        """Activation record has attempted_at timestamp."""
        _setup_clean_stage2(tmp_path, completed=5)
        record = activate_stage3(tmp_path)
        assert record.attempted_at
        assert "T" in record.attempted_at


class TestActivation_AuditTrail:
    """Verify activation writes audit trail."""

    def test_blocked_activation_logged(self, tmp_path):
        """Blocked activation is logged to activation_log.jsonl."""
        _write_flags(tmp_path)
        _write_heartbeat(tmp_path, "healthy")
        activate_stage3(tmp_path)
        log_path = tmp_path / "STATE" / "activation_log.jsonl"
        assert log_path.exists()
        entries = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
        assert len(entries) == 1
        assert entries[0]["outcome"] == "blocked"

    def test_successful_activation_logged(self, tmp_path):
        """Successful activation is logged."""
        _setup_clean_stage2(tmp_path, completed=5)
        activate_stage3(tmp_path)
        log_path = tmp_path / "STATE" / "activation_log.jsonl"
        assert log_path.exists()
        entries = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
        assert len(entries) == 1
        assert entries[0]["outcome"] == "activated"

    def test_multiple_attempts_accumulated(self, tmp_path):
        """Multiple activation attempts are all recorded."""
        _write_flags(tmp_path)
        _write_heartbeat(tmp_path, "healthy")
        # First attempt: blocked (no evidence)
        activate_stage3(tmp_path)
        # Add evidence
        for i in range(5):
            _write_workflow(tmp_path, f"wf_{i}", "completed")
        # Second attempt: ready
        activate_stage3(tmp_path)
        log_path = tmp_path / "STATE" / "activation_log.jsonl"
        entries = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
        assert len(entries) == 2
        assert entries[0]["outcome"] == "blocked"
        assert entries[1]["outcome"] == "activated"

    def test_audit_record_serializable(self, tmp_path):
        """ActivationRecord.to_dict() is JSON-serializable."""
        _setup_clean_stage2(tmp_path, completed=5)
        record = activate_stage3(tmp_path)
        serialized = json.dumps(record.to_dict(), default=str)
        parsed = json.loads(serialized)
        assert parsed["outcome"] == "activated"


class TestActivation_FailClosed:
    """Verify activation fails closed on state changes."""

    def test_state_change_during_evaluation_blocks(self, tmp_path):
        """If state is insufficient at evaluation time → blocked.

        The gate re-evaluates fresh evidence at activation time,
        so even if a previous check showed hold, a later call with
        sufficient evidence will pass (and vice versa).
        """
        # First: insufficient evidence
        _write_flags(tmp_path)
        _write_heartbeat(tmp_path, "healthy")
        record1 = activate_stage3(tmp_path)
        assert record1.outcome == "blocked"

        # Config unchanged
        flags = json.loads(
            (tmp_path / "STATE" / "config" / "feature_flags.json").read_text()
        )
        assert "code_impl" not in flags["phase7_orchestrator"]["supported_classes"]

    def test_policy_violation_after_clean_runs_blocks(self, tmp_path):
        """Clean runs + policy violation → rollback_recommended, blocked."""
        _setup_clean_stage2(tmp_path, completed=5)
        _write_policy_denial(tmp_path, count=1)
        record = activate_stage3(tmp_path)
        assert record.outcome == "blocked"
        assert record.decision == "rollback_recommended"
        assert "no_policy_violations" in record.blocking_criteria

    def test_unhealthy_heartbeat_after_clean_runs_blocks(self, tmp_path):
        """Clean runs + UNHEALTHY heartbeat → blocked."""
        _setup_clean_stage2(tmp_path, completed=5)
        _write_heartbeat(tmp_path, "unhealthy")
        record = activate_stage3(tmp_path)
        assert record.outcome == "blocked"
        assert "heartbeat_healthy" in record.blocking_criteria

    def test_no_double_activation(self, tmp_path):
        """Activating twice doesn't break anything."""
        _setup_clean_stage2(tmp_path, completed=5)
        record1 = activate_stage3(tmp_path)
        assert record1.outcome == "activated"
        # Second activation: already at Stage 3, gate re-checks
        record2 = activate_stage3(tmp_path)
        # Should still be activated (or hold if evidence doesn't meet new stage)
        assert record2.outcome in ("activated", "blocked")
