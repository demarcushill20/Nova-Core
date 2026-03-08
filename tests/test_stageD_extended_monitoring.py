"""Phase 7.19 — Tests for extended Stage D monitoring window.

Tests cover:
  1. Elapsed time computation
  2. Contract failure rate computation
  3. Sustained-stable outcome (all 15 criteria pass)
  4. Continue-monitoring outcome (insufficient time, runs, or soft concerns)
  5. Rollback outcome (hard failures)
  6. Decision priority logic
  7. Monitoring progress tracking
  8. Artifact output (markdown + JSON)
  9. No scope expansion
  10. Threshold tightening vs initial review
"""

import json
import pytest
from pathlib import Path

from agents.rollout_gate import (
    StageDExtendedMonitoring,
    RolloutCriterion,
    evaluate_stageD_extended,
    decide_stageD_extended,
    review_stageD_extended,
    render_stageD_extended_markdown,
    write_stageD_extended_monitoring,
    _compute_elapsed_seconds,
    _compute_contract_failure_rate,
    _collect_system_inspect_metrics,
    _count_blocked_mutation_attempts,
    _verify_scope_integrity,
    EXTENDED_MIN_ELAPSED_SECONDS,
    EXTENDED_MIN_SYSTEM_INSPECT_RUNS,
    EXTENDED_MAX_SYSTEM_INSPECT_FAILURE_RATE,
    EXTENDED_MAX_FAILURE_RATE,
    EXTENDED_MAX_SYSTEM_VERIFIER_REJECTION_RATE,
    EXTENDED_MAX_POLICY_VIOLATIONS,
    EXTENDED_MAX_BUDGET_EXHAUSTIONS,
    EXTENDED_MAX_RECOVERY_ANOMALIES,
    EXTENDED_MAX_BLOCKED_MUTATION_ATTEMPTS,
    EXTENDED_MAX_CONTRACT_FAILURE_RATE,
    EXTENDED_HEARTBEAT_REQUIRED,
    STAGED_MIN_SYSTEM_INSPECT_RUNS,
    STAGED_MAX_FAILURE_RATE,
    STAGED_MAX_SYSTEM_INSPECT_FAILURE_RATE,
    STAGED_MAX_SYSTEM_VERIFIER_REJECTION_RATE,
    STAGED_MAX_BLOCKED_MUTATION_ATTEMPTS,
    STAGE4_ALLOWED_OPERATIONS,
    STAGE4_BLOCKED_OPERATIONS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_workflow(task_class: str, status: str = "completed", **extra) -> dict:
    return {"task_class": task_class, "status": status, **extra}


def _populate_extended_env(root: Path, num_system_runs: int = 10) -> None:
    """Populate a test environment for extended Stage D monitoring."""
    state = root / "STATE"
    (state / "config").mkdir(parents=True, exist_ok=True)
    (state / "workflows").mkdir(parents=True, exist_ok=True)
    (state / "verifications").mkdir(parents=True, exist_ok=True)
    (root / "LOGS").mkdir(parents=True, exist_ok=True)
    (root / "WORK").mkdir(parents=True, exist_ok=True)

    flags = {
        "phase7_orchestrator": {
            "enabled": True,
            "supported_classes": ["research", "code_review", "code_impl", "system"],
            "stage": "D",
            "rollout_stage": "stage4_research_code_review_code_impl_system_inspect",
            "verifier_required": True,
            "system_scope": "inspect_only",
            "system_allowed_operations": sorted(STAGE4_ALLOWED_OPERATIONS),
            "system_blocked_operations": sorted(STAGE4_BLOCKED_OPERATIONS),
            "system_allowed_skills": ["file-ops", "http-fetch", "reading-obsidian-memory", "self-verification", "web-research"],
            "system_blocked_skills": ["git-ops", "shell-ops", "task-execution"],
        },
        "version": 8,
    }
    (state / "config" / "feature_flags.json").write_text(json.dumps(flags))

    hb = {"overall": "healthy", "findings": [], "generated_at": "2026-03-08T10:00:00Z"}
    (state / "heartbeat_multiagent.json").write_text(json.dumps(hb))

    metrics = {"contract_success": {"_total": 30}, "contract_failure": {"_total": 0}}
    (state / "metrics.json").write_text(json.dumps(metrics))

    # Activation log — Stage D activated 5 hours ago
    log_entries = [
        json.dumps({
            "attempted_at": "2026-03-07T12:00:00Z",
            "outcome": "activated",
            "decision": "ready_to_expand",
            "reason": "Stage 3 expansion",
            "pre_config": {}, "post_config": {},
            "blocking_criteria": [],
        }),
        json.dumps({
            "attempted_at": "2026-03-08T10:00:00Z",
            "outcome": "activated",
            "gate_decision": "ready_to_activate_stage4",
            "reason": "Stage 4 activation",
            "activated_scope": "system_inspect",
            "pre_config": {}, "post_config": {},
            "blocking_criteria": [],
            "plan_validation": {"valid": True, "errors": []},
        }),
    ]
    (state / "activation_log.jsonl").write_text("\n".join(log_entries) + "\n")

    # Non-system workflows
    for i in range(5):
        wf = _make_workflow("code_impl", "completed")
        (state / "workflows" / f"code_impl_{i}.json").write_text(json.dumps(wf))
    for i in range(3):
        wf = _make_workflow("research", "completed")
        (state / "workflows" / f"research_{i}.json").write_text(json.dumps(wf))

    # system_inspect workflows
    for i in range(num_system_runs):
        wf = _make_workflow("system", "completed", steps=[
            {"name": "inspect", "contract_status": "valid"},
            {"name": "analyze", "contract_status": "valid"},
            {"name": "verify", "contract_status": "valid"},
        ])
        (state / "workflows" / f"system_{i}.json").write_text(json.dumps(wf))


def _healthy_evidence() -> dict:
    return {
        "heartbeat_overall": "healthy",
        "policy_violations": 0,
        "budget_exhaustions": 0,
        "failure_rate": 0.0,
        "rollout_stage": "stage4_research_code_review_code_impl_system_inspect",
        "supported_classes": ["research", "code_review", "code_impl", "system"],
        "orphaned_agents": 0,
        "stale_leases": 0,
        "recovery_events": 0,
    }


def _healthy_sys_metrics(total: int = 10) -> dict:
    return {
        "total_runs": total,
        "completed": total,
        "failed": 0,
        "verifier_rejected": 0,
        "failure_rate": 0.0,
    }


def _intact_scope() -> dict:
    return {
        "intact": True,
        "reason": "scope intact",
        "system_scope": "inspect_only",
        "system_in_classes": True,
        "stage": "D",
    }


# ---------------------------------------------------------------------------
# TestComputeElapsedSeconds
# ---------------------------------------------------------------------------

class TestComputeElapsedSeconds:
    def test_normal_elapsed(self):
        result = _compute_elapsed_seconds(
            "2026-03-08T10:00:00Z", "2026-03-08T14:00:00Z"
        )
        assert result == 4 * 3600

    def test_empty_activation(self):
        assert _compute_elapsed_seconds("", "2026-03-08T14:00:00Z") == 0.0

    def test_empty_now(self):
        assert _compute_elapsed_seconds("2026-03-08T10:00:00Z", "") == 0.0

    def test_same_time(self):
        assert _compute_elapsed_seconds(
            "2026-03-08T10:00:00Z", "2026-03-08T10:00:00Z"
        ) == 0.0

    def test_negative_clamps_to_zero(self):
        result = _compute_elapsed_seconds(
            "2026-03-08T14:00:00Z", "2026-03-08T10:00:00Z"
        )
        assert result == 0.0

    def test_invalid_format(self):
        assert _compute_elapsed_seconds("not-a-date", "2026-03-08T14:00:00Z") == 0.0

    def test_fractional_hours(self):
        result = _compute_elapsed_seconds(
            "2026-03-08T10:00:00Z", "2026-03-08T10:30:00Z"
        )
        assert result == 1800


# ---------------------------------------------------------------------------
# TestComputeContractFailureRate
# ---------------------------------------------------------------------------

class TestComputeContractFailureRate:
    def test_no_system_workflows(self):
        workflows = [_make_workflow("research", "completed")]
        assert _compute_contract_failure_rate(workflows) is None

    def test_all_valid_contracts(self):
        workflows = [
            _make_workflow("system", "completed", steps=[
                {"contract_status": "valid"},
                {"contract_status": "valid"},
            ]),
        ]
        assert _compute_contract_failure_rate(workflows) == 0.0

    def test_some_invalid_contracts(self):
        workflows = [
            _make_workflow("system", "completed", steps=[
                {"contract_status": "valid"},
                {"contract_status": "invalid"},
            ]),
        ]
        assert _compute_contract_failure_rate(workflows) == 0.5

    def test_no_contract_data(self):
        workflows = [_make_workflow("system", "completed", steps=[{"name": "inspect"}])]
        assert _compute_contract_failure_rate(workflows) is None

    def test_mixed_system_and_other(self):
        workflows = [
            _make_workflow("system", "completed", steps=[{"contract_status": "valid"}]),
            _make_workflow("research", "completed", steps=[{"contract_status": "invalid"}]),
        ]
        # Only system workflows count
        assert _compute_contract_failure_rate(workflows) == 0.0


# ---------------------------------------------------------------------------
# TestSustainedStable
# ---------------------------------------------------------------------------

class TestSustainedStable:
    def test_all_criteria_pass(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(12),
            post_activation_recoveries=0,
            blocked_mutation_attempts=0,
            scope_integrity=_intact_scope(),
            elapsed_seconds=5 * 3600,  # 5h > 4h required
            contract_failure_rate=0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "stageD_sustained_stable"

    def test_criteria_count(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        assert len(criteria) == 15

    def test_hard_count(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        hard = [c for c in criteria if c.severity == "hard"]
        assert len(hard) == 8

    def test_soft_count(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        soft = [c for c in criteria if c.severity == "soft"]
        assert len(soft) == 7

    def test_all_passed(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        for c in criteria:
            assert c.passed, f"{c.name} should pass"

    def test_next_action_mentions_sustained(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        _, next_action = decide_stageD_extended(criteria)
        assert "sustained-stable" in next_action

    def test_integration_with_review(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        # now_override = 5h after activation (10:00 -> 15:00)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        assert result.decision == "stageD_sustained_stable"

    def test_observation_window_has_elapsed(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        assert result.observation_window["elapsed_hours"] == 5.0
        assert result.observation_window["elapsed_seconds"] == 18000.0


# ---------------------------------------------------------------------------
# TestContinueMonitoring
# ---------------------------------------------------------------------------

class TestContinueMonitoring:
    def test_insufficient_elapsed_time(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(12),
            0, 0, _intact_scope(),
            elapsed_seconds=2 * 3600,  # 2h < 4h required
            contract_failure_rate=0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "stageD_continue_monitoring"

    def test_insufficient_runs(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(5),  # 5 < 10 required
            0, 0, _intact_scope(),
            elapsed_seconds=5 * 3600,
            contract_failure_rate=0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "stageD_continue_monitoring"

    def test_zero_elapsed_time(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(12),
            0, 0, _intact_scope(),
            elapsed_seconds=0,
            contract_failure_rate=0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "stageD_continue_monitoring"

    def test_zero_runs(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(0),
            0, 0, _intact_scope(),
            elapsed_seconds=5 * 3600,
            contract_failure_rate=None,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "stageD_continue_monitoring"

    def test_elapsed_priority_over_runs(self):
        """Elapsed time checked before runs."""
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(5),  # also insufficient
            0, 0, _intact_scope(),
            elapsed_seconds=1 * 3600,  # insufficient
            contract_failure_rate=0.0,
        )
        decision, next_action = decide_stageD_extended(criteria)
        assert decision == "stageD_continue_monitoring"
        assert "elapsed time" in next_action.lower()

    def test_orphaned_agents_continue(self):
        ev = _healthy_evidence()
        ev["orphaned_agents"] = 1
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "stageD_continue_monitoring"

    def test_high_contract_failure_rate(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600,
            contract_failure_rate=0.15,  # > 10%
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "stageD_continue_monitoring"

    def test_too_many_blocked_mutations(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(12),
            0,
            blocked_mutation_attempts=4,  # > 3
            scope_integrity=_intact_scope(),
            elapsed_seconds=5 * 3600,
            contract_failure_rate=0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "stageD_continue_monitoring"

    def test_next_action_says_continue(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(5),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        _, next_action = decide_stageD_extended(criteria)
        assert "continue" in next_action.lower()

    def test_integration_insufficient_time(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        # Only 2h after activation
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T12:00:00Z"
        )
        assert result.decision == "stageD_continue_monitoring"

    def test_integration_insufficient_runs(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=5)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        assert result.decision == "stageD_continue_monitoring"


# ---------------------------------------------------------------------------
# TestRollbackRecommended
# ---------------------------------------------------------------------------

class TestRollbackRecommended:
    def test_unhealthy_heartbeat(self):
        ev = _healthy_evidence()
        ev["heartbeat_overall"] = "degraded"
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_policy_violation(self):
        ev = _healthy_evidence()
        ev["policy_violations"] = 1
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_budget_exhaustion(self):
        ev = _healthy_evidence()
        ev["budget_exhaustions"] = 1
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_high_overall_failure_rate(self):
        ev = _healthy_evidence()
        ev["failure_rate"] = 0.25  # > 15%
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_scope_integrity_violated(self):
        broken_scope = {
            "intact": False,
            "reason": "system_scope is 'mutate', expected 'inspect_only'",
            "system_scope": "mutate",
            "system_in_classes": True,
            "stage": "D",
        }
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(12),
            0, 0, broken_scope, 5 * 3600, 0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_wrong_stage(self):
        ev = _healthy_evidence()
        ev["rollout_stage"] = "stage3_research_code_review_code_impl"
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_system_not_in_classes(self):
        ev = _healthy_evidence()
        ev["supported_classes"] = ["research", "code_review", "code_impl"]
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_recovery_anomalies_now_hard(self):
        """Recovery anomalies promoted from soft to hard in extended."""
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(12),
            post_activation_recoveries=1,
            blocked_mutation_attempts=0,
            scope_integrity=_intact_scope(),
            elapsed_seconds=5 * 3600,
            contract_failure_rate=0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_hard_trumps_soft(self):
        ev = _healthy_evidence()
        ev["heartbeat_overall"] = "unhealthy"  # hard fail
        criteria = evaluate_stageD_extended(
            ev,
            _healthy_sys_metrics(5),  # soft fail (insufficient runs)
            0, 0, _intact_scope(),
            elapsed_seconds=1 * 3600,  # soft fail (insufficient time)
            contract_failure_rate=0.0,
        )
        decision, _ = decide_stageD_extended(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_next_action_says_rollback(self):
        ev = _healthy_evidence()
        ev["policy_violations"] = 2
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        _, next_action = decide_stageD_extended(criteria)
        assert "revert to Stage C" in next_action


# ---------------------------------------------------------------------------
# TestDecisionLogic
# ---------------------------------------------------------------------------

class TestDecisionLogic:
    def test_all_pass(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(), _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        d, _ = decide_stageD_extended(criteria)
        assert d == "stageD_sustained_stable"

    def test_hard_failure(self):
        ev = _healthy_evidence()
        ev["heartbeat_overall"] = "degraded"
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        d, _ = decide_stageD_extended(criteria)
        assert d == "rollback_system_inspect_recommended"

    def test_soft_failure(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(), _healthy_sys_metrics(5),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        d, _ = decide_stageD_extended(criteria)
        assert d == "stageD_continue_monitoring"

    def test_multiple_hard(self):
        ev = _healthy_evidence()
        ev["heartbeat_overall"] = "unhealthy"
        ev["policy_violations"] = 1
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        d, _ = decide_stageD_extended(criteria)
        assert d == "rollback_system_inspect_recommended"

    def test_multiple_soft(self):
        ev = _healthy_evidence()
        ev["orphaned_agents"] = 1
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(5),
            0, 0, _intact_scope(), 1 * 3600, 0.5,
        )
        d, _ = decide_stageD_extended(criteria)
        assert d == "stageD_continue_monitoring"

    def test_boundary_elapsed_exactly_at_threshold(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(), _healthy_sys_metrics(12),
            0, 0, _intact_scope(),
            elapsed_seconds=EXTENDED_MIN_ELAPSED_SECONDS,  # exactly at threshold
            contract_failure_rate=0.0,
        )
        d, _ = decide_stageD_extended(criteria)
        assert d == "stageD_sustained_stable"

    def test_boundary_runs_exactly_at_threshold(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(EXTENDED_MIN_SYSTEM_INSPECT_RUNS),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        d, _ = decide_stageD_extended(criteria)
        assert d == "stageD_sustained_stable"

    def test_boundary_runs_one_below_threshold(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(),
            _healthy_sys_metrics(EXTENDED_MIN_SYSTEM_INSPECT_RUNS - 1),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        d, _ = decide_stageD_extended(criteria)
        assert d == "stageD_continue_monitoring"


# ---------------------------------------------------------------------------
# TestMonitoringProgress
# ---------------------------------------------------------------------------

class TestMonitoringProgress:
    def test_full_progress(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        mp = result.monitoring_progress
        assert mp["runs_completed"] == 12
        assert mp["runs_required"] == EXTENDED_MIN_SYSTEM_INSPECT_RUNS
        assert mp["runs_progress_pct"] == 100.0
        assert mp["elapsed_hours"] == 5.0
        assert mp["elapsed_progress_pct"] == 100.0

    def test_partial_progress_runs(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=5)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        mp = result.monitoring_progress
        assert mp["runs_completed"] == 5
        assert mp["runs_progress_pct"] == 50.0

    def test_partial_progress_time(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        # Only 2h after activation (50% of 4h)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T12:00:00Z"
        )
        mp = result.monitoring_progress
        assert mp["elapsed_hours"] == 2.0
        assert mp["elapsed_progress_pct"] == 50.0

    def test_hard_soft_counts(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        mp = result.monitoring_progress
        assert mp["hard_criteria_passed"] == 8
        assert mp["hard_criteria_total"] == 8
        assert mp["soft_criteria_passed"] == 7
        assert mp["soft_criteria_total"] == 7

    def test_progress_caps_at_100(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=20)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T20:00:00Z"
        )
        mp = result.monitoring_progress
        assert mp["runs_progress_pct"] == 100.0
        assert mp["elapsed_progress_pct"] == 100.0


# ---------------------------------------------------------------------------
# TestThresholdTightening
# ---------------------------------------------------------------------------

class TestThresholdTightening:
    """Verify extended thresholds are tighter than initial."""

    def test_runs_threshold_higher(self):
        assert EXTENDED_MIN_SYSTEM_INSPECT_RUNS > STAGED_MIN_SYSTEM_INSPECT_RUNS

    def test_failure_rate_tighter(self):
        assert EXTENDED_MAX_FAILURE_RATE < STAGED_MAX_FAILURE_RATE

    def test_system_inspect_failure_rate_tighter(self):
        assert EXTENDED_MAX_SYSTEM_INSPECT_FAILURE_RATE < STAGED_MAX_SYSTEM_INSPECT_FAILURE_RATE

    def test_verifier_rejection_rate_tighter(self):
        assert EXTENDED_MAX_SYSTEM_VERIFIER_REJECTION_RATE < STAGED_MAX_SYSTEM_VERIFIER_REJECTION_RATE

    def test_blocked_mutations_tighter(self):
        assert EXTENDED_MAX_BLOCKED_MUTATION_ATTEMPTS < STAGED_MAX_BLOCKED_MUTATION_ATTEMPTS

    def test_elapsed_time_is_new(self):
        assert EXTENDED_MIN_ELAPSED_SECONDS > 0

    def test_contract_failure_rate_is_new(self):
        assert EXTENDED_MAX_CONTRACT_FAILURE_RATE > 0


# ---------------------------------------------------------------------------
# TestCriteriaEvaluation
# ---------------------------------------------------------------------------

class TestCriteriaEvaluation:
    def test_strict_healthy(self):
        ev = _healthy_evidence()
        ev["heartbeat_overall"] = "degraded"
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        hb = next(c for c in criteria if c.name == "heartbeat_healthy")
        assert not hb.passed
        assert hb.severity == "hard"

    def test_no_data_heartbeat_fails(self):
        ev = _healthy_evidence()
        ev["heartbeat_overall"] = ""
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        hb = next(c for c in criteria if c.name == "heartbeat_healthy")
        assert not hb.passed

    def test_no_failure_rate_passes_soft(self):
        ev = _healthy_evidence()
        ev["failure_rate"] = None
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        fr = next(c for c in criteria if c.name == "overall_failure_rate")
        assert fr.passed
        assert fr.severity == "soft"

    def test_failure_rate_boundary(self):
        ev = _healthy_evidence()
        ev["failure_rate"] = EXTENDED_MAX_FAILURE_RATE
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        fr = next(c for c in criteria if c.name == "overall_failure_rate")
        assert fr.passed

    def test_failure_rate_over(self):
        ev = _healthy_evidence()
        ev["failure_rate"] = EXTENDED_MAX_FAILURE_RATE + 0.01
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        fr = next(c for c in criteria if c.name == "overall_failure_rate")
        assert not fr.passed

    def test_all_criteria_names(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(), _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        names = {c.name for c in criteria}
        expected = {
            "heartbeat_healthy", "no_policy_violations", "no_budget_exhaustions",
            "overall_failure_rate", "scope_integrity", "stage_is_D",
            "system_class_active", "no_recovery_anomalies",
            "minimum_elapsed_time", "extended_minimum_runs",
            "system_inspect_failure_rate", "system_verifier_rejection_rate",
            "blocked_mutation_attempts", "contract_failure_rate",
            "no_workflow_anomalies",
        }
        assert names == expected

    def test_severity_classification(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(), _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        hard_names = {c.name for c in criteria if c.severity == "hard"}
        soft_names = {c.name for c in criteria if c.severity == "soft"}
        expected_hard = {
            "heartbeat_healthy", "no_policy_violations", "no_budget_exhaustions",
            "overall_failure_rate", "scope_integrity", "stage_is_D",
            "system_class_active", "no_recovery_anomalies",
        }
        expected_soft = {
            "minimum_elapsed_time", "extended_minimum_runs",
            "system_inspect_failure_rate", "system_verifier_rejection_rate",
            "blocked_mutation_attempts", "contract_failure_rate",
            "no_workflow_anomalies",
        }
        assert hard_names == expected_hard
        assert soft_names == expected_soft

    def test_system_inspect_failure_rate_tighter(self):
        # 12% fails in extended (max 10%) but passes in initial (max 20%)
        metrics = _healthy_sys_metrics(10)
        metrics["failure_rate"] = 0.12
        criteria = evaluate_stageD_extended(
            _healthy_evidence(), metrics,
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        fr = next(c for c in criteria if c.name == "system_inspect_failure_rate")
        assert not fr.passed


# ---------------------------------------------------------------------------
# TestArtifactGeneration
# ---------------------------------------------------------------------------

class TestArtifactGeneration:
    def test_sustained_stable_markdown(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(), _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        review = StageDExtendedMonitoring(
            decision="stageD_sustained_stable",
            rollout_stage="stage4_research_code_review_code_impl_system_inspect",
            enabled_classes=["research", "code_review", "code_impl", "system"],
            criteria=criteria,
            system_inspect_metrics=_healthy_sys_metrics(12),
            evidence_summary={},
            observation_window={"activation_at": "T1", "review_at": "T2", "elapsed_hours": 5.0, "elapsed_seconds": 18000.0, "scope": "system_inspect (read-only)"},
            scope_integrity=_intact_scope(),
            monitoring_progress={"runs_completed": 12, "runs_required": 10, "runs_progress_pct": 100.0, "elapsed_hours": 5.0, "elapsed_required_hours": 4.0, "elapsed_progress_pct": 100.0, "hard_criteria_passed": 8, "hard_criteria_total": 8, "soft_criteria_passed": 7, "soft_criteria_total": 7},
        )
        md = render_stageD_extended_markdown(review)
        assert "SUSTAINED STABLE" in md
        assert "Phase 7.19" in md

    def test_continue_monitoring_markdown(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(), _healthy_sys_metrics(5),
            0, 0, _intact_scope(), 2 * 3600, 0.0,
        )
        review = StageDExtendedMonitoring(
            decision="stageD_continue_monitoring",
            rollout_stage="stage4_research_code_review_code_impl_system_inspect",
            enabled_classes=["research", "code_review", "code_impl", "system"],
            criteria=criteria,
            system_inspect_metrics=_healthy_sys_metrics(5),
            evidence_summary={},
            observation_window={"activation_at": "T1", "review_at": "T2", "elapsed_hours": 2.0, "elapsed_seconds": 7200.0, "scope": "system_inspect (read-only)"},
            scope_integrity=_intact_scope(),
            monitoring_progress={"runs_completed": 5, "runs_required": 10, "runs_progress_pct": 50.0, "elapsed_hours": 2.0, "elapsed_required_hours": 4.0, "elapsed_progress_pct": 50.0, "hard_criteria_passed": 8, "hard_criteria_total": 8, "soft_criteria_passed": 5, "soft_criteria_total": 7},
        )
        md = render_stageD_extended_markdown(review)
        assert "CONTINUE MONITORING" in md

    def test_rollback_markdown(self):
        ev = _healthy_evidence()
        ev["heartbeat_overall"] = "unhealthy"
        criteria = evaluate_stageD_extended(
            ev, _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        review = StageDExtendedMonitoring(
            decision="rollback_system_inspect_recommended",
            rollout_stage="stage4_research_code_review_code_impl_system_inspect",
            enabled_classes=["research", "code_review", "code_impl", "system"],
            criteria=criteria,
            system_inspect_metrics=_healthy_sys_metrics(12),
            evidence_summary={},
            observation_window={"activation_at": "T1", "review_at": "T2", "elapsed_hours": 5.0, "elapsed_seconds": 18000.0, "scope": "system_inspect (read-only)"},
            scope_integrity=_intact_scope(),
            monitoring_progress={},
        )
        md = render_stageD_extended_markdown(review)
        assert "ROLLBACK" in md
        assert "Revert to Stage C" in md

    def test_threshold_comparison_in_markdown(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(), _healthy_sys_metrics(12),
            0, 0, _intact_scope(), 5 * 3600, 0.0,
        )
        review = StageDExtendedMonitoring(
            decision="stageD_sustained_stable",
            rollout_stage="stage4",
            criteria=criteria,
            system_inspect_metrics=_healthy_sys_metrics(12),
            evidence_summary={},
            observation_window={"activation_at": "T1", "review_at": "T2", "elapsed_hours": 5.0, "elapsed_seconds": 18000.0, "scope": "test"},
            scope_integrity=_intact_scope(),
            monitoring_progress={"runs_completed": 12, "runs_required": 10, "runs_progress_pct": 100.0, "elapsed_hours": 5.0, "elapsed_required_hours": 4.0, "elapsed_progress_pct": 100.0, "hard_criteria_passed": 8, "hard_criteria_total": 8, "soft_criteria_passed": 7, "soft_criteria_total": 7},
        )
        md = render_stageD_extended_markdown(review)
        assert "Threshold Comparison" in md
        assert "Initial (7.18)" in md
        assert "Extended (7.19)" in md

    def test_monitoring_progress_in_markdown(self):
        criteria = evaluate_stageD_extended(
            _healthy_evidence(), _healthy_sys_metrics(5),
            0, 0, _intact_scope(), 2 * 3600, 0.0,
        )
        review = StageDExtendedMonitoring(
            decision="stageD_continue_monitoring",
            rollout_stage="stage4",
            criteria=criteria,
            system_inspect_metrics=_healthy_sys_metrics(5),
            evidence_summary={},
            observation_window={"activation_at": "T1", "review_at": "T2", "elapsed_hours": 2.0, "elapsed_seconds": 7200.0, "scope": "test"},
            scope_integrity=_intact_scope(),
            monitoring_progress={"runs_completed": 5, "runs_required": 10, "runs_progress_pct": 50.0, "elapsed_hours": 2.0, "elapsed_required_hours": 4.0, "elapsed_progress_pct": 50.0, "hard_criteria_passed": 8, "hard_criteria_total": 8, "soft_criteria_passed": 5, "soft_criteria_total": 7},
        )
        md = render_stageD_extended_markdown(review)
        assert "Monitoring Progress" in md
        assert "50%" in md

    def test_file_creation(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        md_path, json_path = write_stageD_extended_monitoring(result, tmp_path)
        assert md_path.exists()
        assert json_path.exists()

    def test_json_valid(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        _, json_path = write_stageD_extended_monitoring(result, tmp_path)
        data = json.loads(json_path.read_text())
        assert data["decision"] == "stageD_sustained_stable"

    def test_json_roundtrip(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        d = result.to_dict()
        assert d["decision"] == "stageD_sustained_stable"
        assert len(d["criteria"]) == 15
        assert "monitoring_progress" in d

    def test_file_paths(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        md_path, json_path = write_stageD_extended_monitoring(result, tmp_path)
        assert md_path.name == "phase7_stageD_extended_monitoring.md"
        assert json_path.name == "stageD_extended_monitoring.json"


# ---------------------------------------------------------------------------
# TestNoScopeExpansion
# ---------------------------------------------------------------------------

class TestNoScopeExpansion:
    def test_feature_flags_unchanged(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        flags_before = json.loads(
            (tmp_path / "STATE" / "config" / "feature_flags.json").read_text()
        )
        review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        flags_after = json.loads(
            (tmp_path / "STATE" / "config" / "feature_flags.json").read_text()
        )
        assert flags_before == flags_after

    def test_activation_log_unchanged(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        log_before = (tmp_path / "STATE" / "activation_log.jsonl").read_text()
        review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        log_after = (tmp_path / "STATE" / "activation_log.jsonl").read_text()
        assert log_before == log_after

    def test_classes_not_expanded(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        assert result.enabled_classes == ["research", "code_review", "code_impl", "system"]

    def test_no_auto_expand_in_next_action(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        assert "expand" not in result.next_action.lower() or "may be evaluated" in result.next_action.lower()

    def test_scope_remains_inspect_only(self, tmp_path):
        _populate_extended_env(tmp_path, num_system_runs=12)
        result = review_stageD_extended(
            tmp_path, now_override="2026-03-08T15:00:00Z"
        )
        assert result.scope_integrity["system_scope"] == "inspect_only"
