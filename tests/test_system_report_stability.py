"""Tests for Phase 7.23 — Post-Activation System Report Stability Monitoring.

Covers:
  - Stable continue (all criteria pass)
  - Hold (insufficient runs/elapsed — soft failures)
  - Rollback recommended (hard failures: governance, shell violations, scope)
  - Post-activation workflow filtering
  - System report metrics collection
  - Shell audit violation counting
  - Scope integrity verification
  - Artifact generation and rendering
  - No-scope-expansion safety checks
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.rollout_gate import (
    REPORT_STABILITY_MIN_ELAPSED_SECONDS,
    REPORT_STABILITY_MIN_RUNS,
    SystemReportStabilityReview,
    _collect_post_activation_system_workflows,
    _collect_system_report_metrics,
    _count_shell_audit_violations,
    _verify_system_report_scope_integrity,
    decide_system_report_stability,
    evaluate_system_report_stability,
    render_system_report_stability_markdown,
    review_system_report_stability,
    write_system_report_stability_review,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_temp_repo():
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    (root / "STATE" / "config").mkdir(parents=True)
    (root / "STATE" / "workflows").mkdir(parents=True)
    (root / "WORK").mkdir(parents=True)
    (root / "LOGS").mkdir(parents=True)
    return root


def _write_feature_flags(root, system_scope="report"):
    orch = {
        "enabled": True,
        "supported_classes": ["research", "code_review", "code_impl", "system"],
        "stage": "D",
        "rollout_stage": "stage4_research_code_review_code_impl_system_report",
        "system_scope": system_scope,
        "system_allowed_skills": [
            "file-ops", "http-fetch", "reading-obsidian-memory",
            "self-verification", "web-research", "shell-ops",
        ],
        "system_blocked_skills": ["git-ops", "task-execution"],
    }
    data = {"phase7_orchestrator": orch, "version": 9}
    (root / "STATE" / "config" / "feature_flags.json").write_text(
        json.dumps(data, indent=2)
    )


def _write_heartbeat(root, overall="healthy"):
    (root / "STATE" / "heartbeat_multiagent.json").write_text(
        json.dumps({
            "overall": overall,
            "metrics": {
                "budget_exhaustion_count": 0,
                "contract_success_count": 10,
                "contract_failure_count": 0,
            },
        })
    )


def _write_activation_log(root, activation_ts="2026-03-08T22:52:56Z"):
    path = root / "STATE" / "activation_log.jsonl"
    records = [
        {"event": "stage_D_activation", "stage": "D", "timestamp": "2026-03-08T14:33:13Z"},
        {"event": "system_report_activation", "phase": "7.22", "timestamp": activation_ts},
    ]
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _write_workflow(root, wf_id, status="completed", task_class="system", created_at=None):
    if created_at is None:
        created_at = datetime(2026, 3, 9, 0, 0, tzinfo=timezone.utc).timestamp()
    data = {
        "workflow_id": wf_id,
        "status": status,
        "task_class": task_class,
        "stage": "D",
        "created_at": created_at,
        "completed_at": created_at,
    }
    (root / "STATE" / "workflows" / f"{wf_id}.json").write_text(json.dumps(data))


def _make_stable_repo(num_workflows=5, elapsed_hours=3):
    """Create a repo where all stability criteria pass."""
    root = _make_temp_repo()
    _write_feature_flags(root)
    _write_heartbeat(root)
    activation_ts = "2026-03-08T22:52:56Z"
    _write_activation_log(root, activation_ts)
    # Create post-activation workflows
    base_epoch = datetime(2026, 3, 9, 0, 0, tzinfo=timezone.utc).timestamp()
    for i in range(num_workflows):
        _write_workflow(root, f"system_report_{i:03d}", created_at=base_epoch + i * 60)
    return root


def _good_scope_integrity():
    return {
        "intact": True,
        "reason": "scope intact",
        "system_scope": "report",
        "shell_ops_allowed": True,
        "git_ops_blocked": True,
        "system_in_classes": True,
    }


def _all_pass_kwargs(total_runs=5):
    return {
        "system_report_metrics": {
            "total_runs": total_runs,
            "completed": total_runs,
            "failed": 0,
            "verifier_rejected": 0,
            "failure_rate": 0.0,
            "rejection_rate": 0.0,
        },
        "elapsed_seconds": 3 * 3600,  # 3 hours > 2h threshold
        "heartbeat_overall": "healthy",
        "policy_violations": 0,
        "budget_exhaustions": 0,
        "unresolved_recovery_anomalies": 0,
        "shell_audit_violations": 0,
        "scope_integrity": _good_scope_integrity(),
    }


# ============================================================================
# Stable Continue Tests
# ============================================================================

class TestStableContinue:
    """When all criteria pass -> stable_continue_system_report."""

    def test_all_criteria_pass(self):
        criteria = evaluate_system_report_stability(**_all_pass_kwargs())
        assert all(c.passed for c in criteria)

    def test_decision_is_stable(self):
        criteria = evaluate_system_report_stability(**_all_pass_kwargs())
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "stable_continue_system_report"

    def test_next_action_mentions_stable(self):
        criteria = evaluate_system_report_stability(**_all_pass_kwargs())
        _, next_action = decide_system_report_stability(criteria)
        assert "stable" in next_action

    def test_criteria_count(self):
        criteria = evaluate_system_report_stability(**_all_pass_kwargs())
        assert len(criteria) == 10

    def test_hard_count(self):
        criteria = evaluate_system_report_stability(**_all_pass_kwargs())
        hard = [c for c in criteria if c.severity == "hard"]
        assert len(hard) == 7

    def test_soft_count(self):
        criteria = evaluate_system_report_stability(**_all_pass_kwargs())
        soft = [c for c in criteria if c.severity == "soft"]
        assert len(soft) == 3


# ============================================================================
# Hold Tests (soft failures)
# ============================================================================

class TestHold:
    """Soft criteria failures -> hold_system_report."""

    def test_insufficient_runs(self):
        kwargs = _all_pass_kwargs(total_runs=2)
        criteria = evaluate_system_report_stability(**kwargs)
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "hold_system_report"

    def test_insufficient_elapsed(self):
        kwargs = _all_pass_kwargs()
        kwargs["elapsed_seconds"] = 3600  # 1 hour < 2h threshold
        criteria = evaluate_system_report_stability(**kwargs)
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "hold_system_report"

    def test_zero_runs_holds(self):
        kwargs = _all_pass_kwargs(total_runs=0)
        criteria = evaluate_system_report_stability(**kwargs)
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "hold_system_report"

    def test_next_action_says_hold(self):
        kwargs = _all_pass_kwargs(total_runs=2)
        criteria = evaluate_system_report_stability(**kwargs)
        _, next_action = decide_system_report_stability(criteria)
        assert "held" in next_action

    def test_boundary_runs_exactly_at_threshold(self):
        kwargs = _all_pass_kwargs(total_runs=REPORT_STABILITY_MIN_RUNS)
        criteria = evaluate_system_report_stability(**kwargs)
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "stable_continue_system_report"

    def test_boundary_runs_one_below(self):
        kwargs = _all_pass_kwargs(total_runs=REPORT_STABILITY_MIN_RUNS - 1)
        criteria = evaluate_system_report_stability(**kwargs)
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "hold_system_report"

    def test_boundary_elapsed_exactly_at_threshold(self):
        kwargs = _all_pass_kwargs()
        kwargs["elapsed_seconds"] = REPORT_STABILITY_MIN_ELAPSED_SECONDS
        criteria = evaluate_system_report_stability(**kwargs)
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "stable_continue_system_report"

    def test_boundary_elapsed_one_below(self):
        kwargs = _all_pass_kwargs()
        kwargs["elapsed_seconds"] = REPORT_STABILITY_MIN_ELAPSED_SECONDS - 1
        criteria = evaluate_system_report_stability(**kwargs)
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "hold_system_report"


# ============================================================================
# Rollback Tests (hard failures)
# ============================================================================

class TestRollback:
    """Hard criteria failures -> rollback_system_report_recommended."""

    def test_unhealthy_heartbeat(self):
        kwargs = _all_pass_kwargs()
        kwargs["heartbeat_overall"] = "unhealthy"
        criteria = evaluate_system_report_stability(**kwargs)
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "rollback_system_report_recommended"

    def test_policy_violation(self):
        kwargs = _all_pass_kwargs()
        kwargs["policy_violations"] = 1
        criteria = evaluate_system_report_stability(**kwargs)
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "rollback_system_report_recommended"

    def test_budget_exhaustion(self):
        kwargs = _all_pass_kwargs()
        kwargs["budget_exhaustions"] = 1
        criteria = evaluate_system_report_stability(**kwargs)
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "rollback_system_report_recommended"

    def test_recovery_anomaly(self):
        kwargs = _all_pass_kwargs()
        kwargs["unresolved_recovery_anomalies"] = 1
        criteria = evaluate_system_report_stability(**kwargs)
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "rollback_system_report_recommended"

    def test_shell_violation_triggers_rollback(self):
        kwargs = _all_pass_kwargs()
        kwargs["shell_audit_violations"] = 1
        criteria = evaluate_system_report_stability(**kwargs)
        decision, next_action = decide_system_report_stability(criteria)
        assert decision == "rollback_system_report_recommended"
        assert "no_shell_violations" in next_action

    def test_scope_integrity_broken(self):
        kwargs = _all_pass_kwargs()
        kwargs["scope_integrity"] = {"intact": False, "reason": "system_scope is 'inspect_only'"}
        criteria = evaluate_system_report_stability(**kwargs)
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "rollback_system_report_recommended"

    def test_high_failure_rate(self):
        kwargs = _all_pass_kwargs()
        kwargs["system_report_metrics"] = {
            "total_runs": 10, "completed": 7, "failed": 3,
            "verifier_rejected": 0, "failure_rate": 0.3, "rejection_rate": 0.0,
        }
        criteria = evaluate_system_report_stability(**kwargs)
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "rollback_system_report_recommended"

    def test_hard_trumps_soft(self):
        """Hard failure overrides soft hold."""
        kwargs = _all_pass_kwargs(total_runs=2)  # soft fail
        kwargs["shell_audit_violations"] = 1  # hard fail
        criteria = evaluate_system_report_stability(**kwargs)
        decision, _ = decide_system_report_stability(criteria)
        assert decision == "rollback_system_report_recommended"

    def test_next_action_mentions_rollback(self):
        kwargs = _all_pass_kwargs()
        kwargs["heartbeat_overall"] = "unhealthy"
        criteria = evaluate_system_report_stability(**kwargs)
        _, next_action = decide_system_report_stability(criteria)
        assert "rollback" in next_action.lower()
        assert "deactivate_system_report_scope" in next_action


# ============================================================================
# Post-Activation Workflow Filtering Tests
# ============================================================================

class TestPostActivationFiltering:
    """Test _collect_post_activation_system_workflows."""

    def test_filters_pre_activation(self):
        activation_ts = "2026-03-08T22:52:56Z"
        act_epoch = datetime(2026, 3, 8, 22, 52, 56, tzinfo=timezone.utc).timestamp()
        workflows = [
            {"task_class": "system", "created_at": act_epoch - 100},  # before
            {"task_class": "system", "created_at": act_epoch + 100},  # after
        ]
        result = _collect_post_activation_system_workflows(workflows, activation_ts)
        assert len(result) == 1
        assert result[0]["created_at"] == act_epoch + 100

    def test_filters_non_system(self):
        activation_ts = "2026-03-08T22:52:56Z"
        act_epoch = datetime(2026, 3, 8, 22, 52, 56, tzinfo=timezone.utc).timestamp()
        workflows = [
            {"task_class": "research", "created_at": act_epoch + 100},
            {"task_class": "system", "created_at": act_epoch + 100},
        ]
        result = _collect_post_activation_system_workflows(workflows, activation_ts)
        assert len(result) == 1
        assert result[0]["task_class"] == "system"

    def test_empty_activation_ts(self):
        result = _collect_post_activation_system_workflows([], "")
        assert result == []

    def test_includes_exact_timestamp(self):
        activation_ts = "2026-03-08T22:52:56Z"
        act_epoch = datetime(2026, 3, 8, 22, 52, 56, tzinfo=timezone.utc).timestamp()
        workflows = [{"task_class": "system", "created_at": act_epoch}]
        result = _collect_post_activation_system_workflows(workflows, activation_ts)
        assert len(result) == 1


# ============================================================================
# System Report Metrics Tests
# ============================================================================

class TestSystemReportMetrics:
    """Test _collect_system_report_metrics."""

    def test_all_completed(self):
        workflows = [
            {"status": "completed"},
            {"status": "completed"},
            {"status": "completed"},
        ]
        m = _collect_system_report_metrics(workflows)
        assert m["total_runs"] == 3
        assert m["completed"] == 3
        assert m["failed"] == 0
        assert m["failure_rate"] == 0.0

    def test_mixed_status(self):
        workflows = [
            {"status": "completed"},
            {"status": "failed"},
            {"status": "completed"},
            {"status": "halted"},
        ]
        m = _collect_system_report_metrics(workflows)
        assert m["total_runs"] == 4
        assert m["completed"] == 2
        assert m["failed"] == 2
        assert m["failure_rate"] == 0.5

    def test_verifier_rejected(self):
        workflows = [
            {"status": "completed", "halt_reason": None},
            {"status": "failed", "halt_reason": "verifier_rejected"},
        ]
        m = _collect_system_report_metrics(workflows)
        assert m["verifier_rejected"] == 1
        assert m["rejection_rate"] == 0.5

    def test_empty_workflows(self):
        m = _collect_system_report_metrics([])
        assert m["total_runs"] == 0
        assert m["failure_rate"] == 0.0


# ============================================================================
# Shell Audit Violation Tests
# ============================================================================

class TestShellAuditViolations:
    """Test _count_shell_audit_violations."""

    def test_no_audit_log(self):
        root = _make_temp_repo()
        assert _count_shell_audit_violations(root) == 0

    def test_empty_audit_log(self):
        root = _make_temp_repo()
        (root / "LOGS" / "system_shell_audit.log").write_text("")
        assert _count_shell_audit_violations(root) == 0

    def test_violations_counted(self):
        root = _make_temp_repo()
        (root / "LOGS" / "system_shell_audit.log").write_text(
            "2026-03-08T23:00:00Z [ALLOWED] ps aux\n"
            "2026-03-08T23:01:00Z [VIOLATION] rm -rf /tmp\n"
            "2026-03-08T23:02:00Z [DENIED] sudo apt update\n"
            "2026-03-08T23:03:00Z [ALLOWED] df -h\n"
        )
        assert _count_shell_audit_violations(root) == 2

    def test_clean_audit_log(self):
        root = _make_temp_repo()
        (root / "LOGS" / "system_shell_audit.log").write_text(
            "2026-03-08T23:00:00Z [ALLOWED] ps aux\n"
            "2026-03-08T23:01:00Z [ALLOWED] df -h\n"
        )
        assert _count_shell_audit_violations(root) == 0


# ============================================================================
# Scope Integrity Tests
# ============================================================================

class TestScopeIntegrity:
    """Test _verify_system_report_scope_integrity."""

    def test_intact_scope(self):
        orch = {
            "system_scope": "report",
            "system_allowed_skills": ["shell-ops", "file-ops"],
            "system_blocked_skills": ["git-ops", "task-execution"],
            "supported_classes": ["system"],
        }
        result = _verify_system_report_scope_integrity(orch)
        assert result["intact"] is True

    def test_wrong_scope(self):
        orch = {
            "system_scope": "inspect_only",
            "system_allowed_skills": ["file-ops"],
            "system_blocked_skills": ["git-ops", "task-execution"],
            "supported_classes": ["system"],
        }
        result = _verify_system_report_scope_integrity(orch)
        assert result["intact"] is False
        assert "inspect_only" in result["reason"]

    def test_shell_ops_not_allowed(self):
        orch = {
            "system_scope": "report",
            "system_allowed_skills": ["file-ops"],
            "system_blocked_skills": ["git-ops", "task-execution", "shell-ops"],
            "supported_classes": ["system"],
        }
        result = _verify_system_report_scope_integrity(orch)
        assert result["intact"] is False
        assert "shell-ops" in result["reason"]

    def test_git_ops_not_blocked(self):
        orch = {
            "system_scope": "report",
            "system_allowed_skills": ["shell-ops"],
            "system_blocked_skills": ["task-execution"],
            "supported_classes": ["system"],
        }
        result = _verify_system_report_scope_integrity(orch)
        assert result["intact"] is False

    def test_empty_orch(self):
        result = _verify_system_report_scope_integrity({})
        assert result["intact"] is False


# ============================================================================
# Artifact Generation Tests
# ============================================================================

class TestArtifactGeneration:
    """Test rendering and file writing."""

    def _make_review(self, decision="stable_continue_system_report"):
        criteria = evaluate_system_report_stability(**_all_pass_kwargs())
        if decision == "hold_system_report":
            kwargs = _all_pass_kwargs(total_runs=2)
            criteria = evaluate_system_report_stability(**kwargs)
        elif decision == "rollback_system_report_recommended":
            kwargs = _all_pass_kwargs()
            kwargs["heartbeat_overall"] = "unhealthy"
            criteria = evaluate_system_report_stability(**kwargs)
        dec, next_action = decide_system_report_stability(criteria)
        return SystemReportStabilityReview(
            decision=dec,
            rollout_stage="stage4_research_code_review_code_impl_system_report",
            system_scope="report",
            criteria=criteria,
            system_report_metrics={"total_runs": 5, "completed": 5, "failed": 0,
                                    "verifier_rejected": 0, "failure_rate": 0.0, "rejection_rate": 0.0},
            evidence_summary={"test": True},
            generated_at="2026-03-09T01:00:00Z",
            next_action=next_action,
            observation_window={"activation_at": "2026-03-08T22:52:56Z",
                                "review_at": "2026-03-09T01:00:00Z",
                                "elapsed_hours": 2.1, "elapsed_seconds": 7564},
            scope_integrity=_good_scope_integrity(),
            monitoring_progress={"runs_completed": 5, "runs_target": 5,
                                  "runs_pct": 100, "elapsed_hours": 2.1,
                                  "elapsed_target_hours": 2.0, "elapsed_pct": 100},
        )

    def test_stable_markdown(self):
        review = self._make_review("stable_continue_system_report")
        md = render_system_report_stability_markdown(review)
        assert "STABLE" in md
        assert "No action required" in md

    def test_hold_markdown(self):
        review = self._make_review("hold_system_report")
        md = render_system_report_stability_markdown(review)
        assert "HOLD" in md
        assert "pending more evidence" in md

    def test_rollback_markdown(self):
        review = self._make_review("rollback_system_report_recommended")
        md = render_system_report_stability_markdown(review)
        assert "ROLLBACK RECOMMENDED" in md
        assert "deactivate_system_report_scope" in md

    def test_file_creation(self):
        root = _make_temp_repo()
        review = self._make_review()
        md_path, json_path = write_system_report_stability_review(review, root)
        assert md_path.exists()
        assert json_path.exists()

    def test_json_roundtrip(self):
        root = _make_temp_repo()
        review = self._make_review()
        _, json_path = write_system_report_stability_review(review, root)
        data = json.loads(json_path.read_text())
        assert data["decision"] == "stable_continue_system_report"
        assert len(data["criteria"]) == 10

    def test_file_paths(self):
        root = _make_temp_repo()
        review = self._make_review()
        md_path, json_path = write_system_report_stability_review(review, root)
        assert md_path.name == "phase7_system_report_stability_review.md"
        assert json_path.name == "system_report_stability_review.json"

    def test_scope_integrity_in_markdown(self):
        review = self._make_review()
        md = render_system_report_stability_markdown(review)
        assert "Scope Integrity" in md
        assert "shell-ops allowed" in md

    def test_monitoring_progress_in_markdown(self):
        review = self._make_review()
        md = render_system_report_stability_markdown(review)
        assert "Monitoring Progress" in md


# ============================================================================
# Integration Tests
# ============================================================================

class TestIntegration:
    """End-to-end with temp repo."""

    def test_hold_on_fresh_activation(self):
        root = _make_stable_repo(num_workflows=0)
        review = review_system_report_stability(
            root, now_override="2026-03-08T23:00:00Z"
        )
        assert review.decision == "hold_system_report"

    def test_stable_with_enough_evidence(self):
        root = _make_stable_repo(num_workflows=6)
        review = review_system_report_stability(
            root, now_override="2026-03-09T02:00:00Z"
        )
        assert review.decision == "stable_continue_system_report"

    def test_rollback_on_unhealthy(self):
        root = _make_stable_repo(num_workflows=6)
        _write_heartbeat(root, overall="unhealthy")
        review = review_system_report_stability(
            root, now_override="2026-03-09T02:00:00Z"
        )
        assert review.decision == "rollback_system_report_recommended"

    def test_rollback_on_shell_violation(self):
        root = _make_stable_repo(num_workflows=6)
        (root / "LOGS" / "system_shell_audit.log").write_text(
            "2026-03-09T00:30:00Z [VIOLATION] rm -rf /tmp\n"
        )
        review = review_system_report_stability(
            root, now_override="2026-03-09T02:00:00Z"
        )
        assert review.decision == "rollback_system_report_recommended"

    def test_artifacts_written(self):
        root = _make_stable_repo(num_workflows=6)
        review = review_system_report_stability(
            root, now_override="2026-03-09T02:00:00Z"
        )
        md_path, json_path = write_system_report_stability_review(review, root)
        assert md_path.exists()
        assert json_path.exists()
        data = json.loads(json_path.read_text())
        assert data["decision"] == "stable_continue_system_report"

    def test_scope_integrity_check_in_review(self):
        root = _make_stable_repo(num_workflows=6)
        # Break scope integrity
        _write_feature_flags(root, system_scope="inspect_only")
        review = review_system_report_stability(
            root, now_override="2026-03-09T02:00:00Z"
        )
        assert review.decision == "rollback_system_report_recommended"
        c = next(c for c in review.criteria if c.name == "scope_integrity")
        assert not c.passed


# ============================================================================
# No Scope Expansion Tests
# ============================================================================

class TestNoScopeExpansion:
    """Verify monitoring does not change scope."""

    def test_feature_flags_unchanged(self):
        root = _make_stable_repo(num_workflows=6)
        pre = (root / "STATE" / "config" / "feature_flags.json").read_text()
        review_system_report_stability(root, now_override="2026-03-09T02:00:00Z")
        post = (root / "STATE" / "config" / "feature_flags.json").read_text()
        assert pre == post

    def test_activation_log_unchanged(self):
        root = _make_stable_repo(num_workflows=6)
        pre = (root / "STATE" / "activation_log.jsonl").read_text()
        review_system_report_stability(root, now_override="2026-03-09T02:00:00Z")
        post = (root / "STATE" / "activation_log.jsonl").read_text()
        assert pre == post

    def test_scope_remains_report(self):
        root = _make_stable_repo(num_workflows=6)
        review = review_system_report_stability(
            root, now_override="2026-03-09T02:00:00Z"
        )
        assert review.system_scope == "report"
        assert "diagnose" not in review.next_action.lower() or "future" in review.next_action.lower()
