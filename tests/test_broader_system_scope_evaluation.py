"""Phase 7.20 — Tests for broader system scope evaluation gate.

Tests cover:
  1. Ready outcome — all criteria pass
  2. Hold outcome — soft criteria failures
  3. Block outcome — hard criteria failures
  4. Decision priority logic
  5. Criteria counts and severity classification
  6. Stage D rollback history detection
  7. Threshold tightening vs extended monitoring
  8. Artifact output (markdown + JSON)
  9. No scope expansion
  10. Integration with review_broader_system_scope
"""

import json
from pathlib import Path

from agents.rollout_gate import (
    BROADER_MAX_BLOCKED_MUTATION_ATTEMPTS,
    BROADER_MAX_CONTRACT_FAILURE_RATE,
    BROADER_MAX_FAILURE_RATE,
    BROADER_MAX_SYSTEM_INSPECT_FAILURE_RATE,
    BROADER_MAX_SYSTEM_VERIFIER_REJECTION_RATE,
    BROADER_MIN_ELAPSED_SECONDS,
    BROADER_MIN_SYSTEM_INSPECT_RUNS,
    BROADER_REQUIRED_EXTENDED_DECISION,
    EXTENDED_MAX_BLOCKED_MUTATION_ATTEMPTS,
    EXTENDED_MAX_CONTRACT_FAILURE_RATE,
    EXTENDED_MAX_FAILURE_RATE,
    EXTENDED_MAX_SYSTEM_INSPECT_FAILURE_RATE,
    EXTENDED_MAX_SYSTEM_VERIFIER_REJECTION_RATE,
    EXTENDED_MIN_ELAPSED_SECONDS,
    EXTENDED_MIN_SYSTEM_INSPECT_RUNS,
    STAGE4_ALLOWED_OPERATIONS,
    STAGE4_BLOCKED_OPERATIONS,
    BroaderSystemScopeEvaluation,
    _count_stageD_rollbacks,
    decide_broader_system_scope,
    evaluate_broader_system_scope,
    render_broader_system_scope_markdown,
    review_broader_system_scope,
    write_broader_system_scope_evaluation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workflow(task_class: str, status: str = "completed", **extra) -> dict:
    return {"task_class": task_class, "status": status, **extra}


def _populate_broader_env(root: Path, num_system_runs: int = 15) -> None:
    """Populate a test environment for broader scope evaluation."""
    state = root / "STATE"
    (state / "config").mkdir(parents=True, exist_ok=True)
    (state / "workflows").mkdir(parents=True, exist_ok=True)
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
            "system_allowed_skills": [
                "file-ops",
                "http-fetch",
                "reading-obsidian-memory",
                "self-verification",
                "web-research",
            ],
            "system_blocked_skills": ["git-ops", "shell-ops", "task-execution"],
        },
        "version": 9,
    }
    (state / "config" / "feature_flags.json").write_text(json.dumps(flags))

    hb = {"overall": "healthy", "findings": [], "generated_at": "2026-03-08T10:00:00Z"}
    (state / "heartbeat_multiagent.json").write_text(json.dumps(hb))

    metrics = {"contract_success": {"_total": 50}, "contract_failure": {"_total": 0}}
    (state / "metrics.json").write_text(json.dumps(metrics))

    # Extended monitoring result: sustained-stable
    ext = {"decision": "stageD_sustained_stable", "generated_at": "2026-03-08T16:00:00Z"}
    (state / "stageD_extended_monitoring.json").write_text(json.dumps(ext))

    # Activation log — Stage D activated 10 hours ago
    log_entries = [
        json.dumps(
            {
                "attempted_at": "2026-03-07T12:00:00Z",
                "outcome": "activated",
                "decision": "ready_to_expand",
                "reason": "Stage 3 expansion",
                "pre_config": {},
                "post_config": {},
                "blocking_criteria": [],
            }
        ),
        json.dumps(
            {
                "attempted_at": "2026-03-08T08:00:00Z",
                "outcome": "activated",
                "gate_decision": "ready_to_activate_stage4",
                "reason": "Stage 4 activation",
                "activated_scope": "system_inspect",
                "pre_config": {},
                "post_config": {},
                "blocking_criteria": [],
                "plan_validation": {"valid": True, "errors": []},
            }
        ),
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
        wf = _make_workflow("system", "completed")
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


def _healthy_sys_metrics(total: int = 15) -> dict:
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


def _no_recoveries() -> dict:
    return {"total": 0, "resolved": 0, "unresolved": 0, "details": []}


def _all_pass_criteria():
    return evaluate_broader_system_scope(
        _healthy_evidence(),
        _healthy_sys_metrics(20),
        BROADER_REQUIRED_EXTENDED_DECISION,
        _no_recoveries(),
        0,
        _intact_scope(),
        10 * 3600,  # 10h > 8h required
        0.0,
        0,
    )


# ---------------------------------------------------------------------------
# TestReadyForPlanning
# ---------------------------------------------------------------------------


class TestReadyForPlanning:
    def test_all_criteria_pass(self):
        criteria = _all_pass_criteria()
        decision, _ = decide_broader_system_scope(criteria)
        assert decision == "ready_for_broader_system_scope_planning"

    def test_criteria_count(self):
        criteria = _all_pass_criteria()
        assert len(criteria) == 12

    def test_hard_count(self):
        criteria = _all_pass_criteria()
        hard = [c for c in criteria if c.severity == "hard"]
        assert len(hard) == 9

    def test_soft_count(self):
        criteria = _all_pass_criteria()
        soft = [c for c in criteria if c.severity == "soft"]
        assert len(soft) == 3

    def test_all_passed(self):
        criteria = _all_pass_criteria()
        for c in criteria:
            assert c.passed, f"{c.name} should pass"

    def test_next_action_mentions_planning(self):
        criteria = _all_pass_criteria()
        _, next_action = decide_broader_system_scope(criteria)
        assert "plan" in next_action.lower()

    def test_next_action_mentions_inspect_only(self):
        criteria = _all_pass_criteria()
        _, next_action = decide_broader_system_scope(criteria)
        assert "inspect_only" in next_action

    def test_integration_with_review(self, tmp_path):
        _populate_broader_env(tmp_path, num_system_runs=20)
        result = review_broader_system_scope(
            tmp_path,
            now_override="2026-03-08T18:00:00Z",  # 10h after activation
        )
        assert result.decision == "ready_for_broader_system_scope_planning"

    def test_remaining_requirements_empty(self, tmp_path):
        _populate_broader_env(tmp_path, num_system_runs=20)
        result = review_broader_system_scope(tmp_path, now_override="2026-03-08T18:00:00Z")
        assert result.remaining_requirements == []


# ---------------------------------------------------------------------------
# TestHoldBroaderScope
# ---------------------------------------------------------------------------


class TestHoldBroaderScope:
    def test_insufficient_runs(self):
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(10),  # 10 < 15 required
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        decision, _ = decide_broader_system_scope(criteria)
        assert decision == "hold_broader_system_scope"

    def test_insufficient_elapsed(self):
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            3 * 3600,  # 3h < 8h required
            0.0,
            0,
        )
        decision, _ = decide_broader_system_scope(criteria)
        assert decision == "hold_broader_system_scope"

    def test_too_many_blocked_mutations(self):
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            blocked_mutation_attempts=3,  # > 2
            scope_integrity=_intact_scope(),
            elapsed_seconds=10 * 3600,
            contract_failure_rate=0.0,
            stageD_rollback_count=0,
        )
        decision, _ = decide_broader_system_scope(criteria)
        assert decision == "hold_broader_system_scope"

    def test_next_action_says_hold(self):
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(10),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        _, next_action = decide_broader_system_scope(criteria)
        assert "held" in next_action.lower() or "hold" in next_action.lower()
        assert "inspect_only" in next_action

    def test_integration_insufficient_runs(self, tmp_path):
        _populate_broader_env(tmp_path, num_system_runs=10)
        result = review_broader_system_scope(tmp_path, now_override="2026-03-08T18:00:00Z")
        assert result.decision == "hold_broader_system_scope"

    def test_integration_insufficient_time(self, tmp_path):
        _populate_broader_env(tmp_path, num_system_runs=20)
        # Only 3h after activation (08:00 + 3h = 11:00)
        result = review_broader_system_scope(tmp_path, now_override="2026-03-08T11:00:00Z")
        assert result.decision == "hold_broader_system_scope"

    def test_remaining_requirements_populated(self, tmp_path):
        _populate_broader_env(tmp_path, num_system_runs=10)
        result = review_broader_system_scope(tmp_path, now_override="2026-03-08T18:00:00Z")
        assert len(result.remaining_requirements) > 0


# ---------------------------------------------------------------------------
# TestBlockBroaderScope
# ---------------------------------------------------------------------------


class TestBlockBroaderScope:
    def test_extended_not_sustained(self):
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(20),
            "stageD_continue_monitoring",  # not sustained
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        decision, _ = decide_broader_system_scope(criteria)
        assert decision == "block_broader_system_scope"

    def test_unhealthy_heartbeat(self):
        ev = _healthy_evidence()
        ev["heartbeat_overall"] = "degraded"
        criteria = evaluate_broader_system_scope(
            ev,
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        decision, _ = decide_broader_system_scope(criteria)
        assert decision == "block_broader_system_scope"

    def test_policy_violation(self):
        ev = _healthy_evidence()
        ev["policy_violations"] = 1
        criteria = evaluate_broader_system_scope(
            ev,
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        decision, _ = decide_broader_system_scope(criteria)
        assert decision == "block_broader_system_scope"

    def test_budget_exhaustion(self):
        ev = _healthy_evidence()
        ev["budget_exhaustions"] = 1
        criteria = evaluate_broader_system_scope(
            ev,
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        decision, _ = decide_broader_system_scope(criteria)
        assert decision == "block_broader_system_scope"

    def test_scope_violated(self):
        broken = {
            "intact": False,
            "reason": "scope tampered",
            "system_scope": "mutate",
            "system_in_classes": True,
            "stage": "D",
        }
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            broken,
            10 * 3600,
            0.0,
            0,
        )
        decision, _ = decide_broader_system_scope(criteria)
        assert decision == "block_broader_system_scope"

    def test_wrong_stage(self):
        ev = _healthy_evidence()
        ev["rollout_stage"] = "stage3_research_code_review_code_impl"
        criteria = evaluate_broader_system_scope(
            ev,
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        decision, _ = decide_broader_system_scope(criteria)
        assert decision == "block_broader_system_scope"

    def test_system_not_in_classes(self):
        ev = _healthy_evidence()
        ev["supported_classes"] = ["research", "code_review", "code_impl"]
        criteria = evaluate_broader_system_scope(
            ev,
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        decision, _ = decide_broader_system_scope(criteria)
        assert decision == "block_broader_system_scope"

    def test_unresolved_recovery(self):
        rc = {"total": 1, "resolved": 0, "unresolved": 1, "details": []}
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            rc,
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        decision, _ = decide_broader_system_scope(criteria)
        assert decision == "block_broader_system_scope"

    def test_stageD_rollback_history(self):
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            stageD_rollback_count=1,  # had a prior rollback
        )
        decision, _ = decide_broader_system_scope(criteria)
        assert decision == "block_broader_system_scope"

    def test_hard_trumps_soft(self):
        ev = _healthy_evidence()
        ev["heartbeat_overall"] = "unhealthy"  # hard fail
        criteria = evaluate_broader_system_scope(
            ev,
            _healthy_sys_metrics(5),  # soft fail too
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            1 * 3600,
            0.0,
            0,  # soft fail too
        )
        decision, _ = decide_broader_system_scope(criteria)
        assert decision == "block_broader_system_scope"

    def test_next_action_says_blocked(self):
        ev = _healthy_evidence()
        ev["policy_violations"] = 2
        criteria = evaluate_broader_system_scope(
            ev,
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        _, next_action = decide_broader_system_scope(criteria)
        assert "blocked" in next_action.lower()
        assert "inspect_only" in next_action


# ---------------------------------------------------------------------------
# TestDecisionLogic
# ---------------------------------------------------------------------------


class TestDecisionLogic:
    def test_all_pass_ready(self):
        d, _ = decide_broader_system_scope(_all_pass_criteria())
        assert d == "ready_for_broader_system_scope_planning"

    def test_single_hard_fail_blocks(self):
        ev = _healthy_evidence()
        ev["heartbeat_overall"] = "degraded"
        criteria = evaluate_broader_system_scope(
            ev,
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        d, _ = decide_broader_system_scope(criteria)
        assert d == "block_broader_system_scope"

    def test_single_soft_fail_holds(self):
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(10),  # insufficient
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        d, _ = decide_broader_system_scope(criteria)
        assert d == "hold_broader_system_scope"

    def test_boundary_runs_exactly_at_threshold(self):
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(BROADER_MIN_SYSTEM_INSPECT_RUNS),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        d, _ = decide_broader_system_scope(criteria)
        assert d == "ready_for_broader_system_scope_planning"

    def test_boundary_runs_one_below(self):
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(BROADER_MIN_SYSTEM_INSPECT_RUNS - 1),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        d, _ = decide_broader_system_scope(criteria)
        assert d == "hold_broader_system_scope"

    def test_boundary_elapsed_exactly_at_threshold(self):
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            BROADER_MIN_ELAPSED_SECONDS,
            0.0,
            0,
        )
        d, _ = decide_broader_system_scope(criteria)
        assert d == "ready_for_broader_system_scope_planning"

    def test_boundary_elapsed_one_second_below(self):
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            BROADER_MIN_ELAPSED_SECONDS - 1,
            0.0,
            0,
        )
        d, _ = decide_broader_system_scope(criteria)
        assert d == "hold_broader_system_scope"


# ---------------------------------------------------------------------------
# TestCriteriaClassification
# ---------------------------------------------------------------------------


class TestCriteriaClassification:
    def test_all_criteria_names(self):
        criteria = _all_pass_criteria()
        names = {c.name for c in criteria}
        expected = {
            "extended_monitoring_sustained_stable",
            "heartbeat_healthy",
            "no_policy_violations",
            "no_budget_exhaustions",
            "scope_integrity",
            "stage_is_D",
            "system_class_active",
            "no_recovery_anomalies",
            "no_stageD_rollback_history",
            "minimum_system_inspect_runs",
            "minimum_elapsed_time",
            "blocked_mutation_attempts",
        }
        assert names == expected

    def test_severity_classification(self):
        criteria = _all_pass_criteria()
        hard_names = {c.name for c in criteria if c.severity == "hard"}
        soft_names = {c.name for c in criteria if c.severity == "soft"}
        expected_hard = {
            "extended_monitoring_sustained_stable",
            "heartbeat_healthy",
            "no_policy_violations",
            "no_budget_exhaustions",
            "scope_integrity",
            "stage_is_D",
            "system_class_active",
            "no_recovery_anomalies",
            "no_stageD_rollback_history",
        }
        expected_soft = {
            "minimum_system_inspect_runs",
            "minimum_elapsed_time",
            "blocked_mutation_attempts",
        }
        assert hard_names == expected_hard
        assert soft_names == expected_soft

    def test_resolved_recovery_passes(self):
        rc = {"total": 2, "resolved": 2, "unresolved": 0, "details": []}
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            rc,
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        rec = next(c for c in criteria if c.name == "no_recovery_anomalies")
        assert rec.passed
        assert "benign" in rec.detail


# ---------------------------------------------------------------------------
# TestStageDRollbackHistory
# ---------------------------------------------------------------------------


class TestStageDRollbackHistory:
    def test_no_rollbacks(self, tmp_path):
        state = tmp_path / "STATE"
        state.mkdir(parents=True)
        log_entries = [
            json.dumps(
                {
                    "pre_config": {"stage": "C"},
                    "post_config": {"stage": "D"},
                }
            ),
        ]
        (state / "activation_log.jsonl").write_text("\n".join(log_entries) + "\n")
        assert _count_stageD_rollbacks(tmp_path) == 0

    def test_one_rollback(self, tmp_path):
        state = tmp_path / "STATE"
        state.mkdir(parents=True)
        log_entries = [
            json.dumps(
                {
                    "pre_config": {"stage": "C"},
                    "post_config": {"stage": "D"},
                }
            ),
            json.dumps(
                {
                    "pre_config": {"stage": "D"},
                    "post_config": {"stage": "C"},  # rollback!
                }
            ),
        ]
        (state / "activation_log.jsonl").write_text("\n".join(log_entries) + "\n")
        assert _count_stageD_rollbacks(tmp_path) == 1

    def test_no_log_file(self, tmp_path):
        state = tmp_path / "STATE"
        state.mkdir(parents=True)
        assert _count_stageD_rollbacks(tmp_path) == 0

    def test_non_D_stage_change_ignored(self, tmp_path):
        state = tmp_path / "STATE"
        state.mkdir(parents=True)
        log_entries = [
            json.dumps(
                {
                    "pre_config": {"stage": "B"},
                    "post_config": {"stage": "C"},
                }
            ),
        ]
        (state / "activation_log.jsonl").write_text("\n".join(log_entries) + "\n")
        assert _count_stageD_rollbacks(tmp_path) == 0


# ---------------------------------------------------------------------------
# TestThresholdTightening
# ---------------------------------------------------------------------------


class TestThresholdTightening:
    """Verify broader-scope thresholds are tighter than extended."""

    def test_runs_threshold_higher(self):
        assert BROADER_MIN_SYSTEM_INSPECT_RUNS > EXTENDED_MIN_SYSTEM_INSPECT_RUNS

    def test_elapsed_threshold_higher(self):
        assert BROADER_MIN_ELAPSED_SECONDS > EXTENDED_MIN_ELAPSED_SECONDS

    def test_failure_rate_tighter(self):
        assert BROADER_MAX_FAILURE_RATE < EXTENDED_MAX_FAILURE_RATE

    def test_system_inspect_failure_rate_tighter(self):
        assert BROADER_MAX_SYSTEM_INSPECT_FAILURE_RATE < EXTENDED_MAX_SYSTEM_INSPECT_FAILURE_RATE

    def test_verifier_rejection_rate_tighter(self):
        assert BROADER_MAX_SYSTEM_VERIFIER_REJECTION_RATE < EXTENDED_MAX_SYSTEM_VERIFIER_REJECTION_RATE

    def test_blocked_mutations_tighter(self):
        assert BROADER_MAX_BLOCKED_MUTATION_ATTEMPTS < EXTENDED_MAX_BLOCKED_MUTATION_ATTEMPTS

    def test_contract_failure_rate_tighter(self):
        assert BROADER_MAX_CONTRACT_FAILURE_RATE < EXTENDED_MAX_CONTRACT_FAILURE_RATE

    def test_extended_decision_prerequisite(self):
        assert BROADER_REQUIRED_EXTENDED_DECISION == "stageD_sustained_stable"


# ---------------------------------------------------------------------------
# TestArtifactGeneration
# ---------------------------------------------------------------------------


class TestArtifactGeneration:
    def test_ready_markdown(self):
        criteria = _all_pass_criteria()
        review = BroaderSystemScopeEvaluation(
            decision="ready_for_broader_system_scope_planning",
            rollout_stage="stage4_research_code_review_code_impl_system_inspect",
            enabled_classes=["research", "code_review", "code_impl", "system"],
            criteria=criteria,
            system_inspect_metrics=_healthy_sys_metrics(20),
            evidence_summary={},
            extended_monitoring_decision="stageD_sustained_stable",
            observation_window={
                "activation_at": "T1",
                "review_at": "T2",
                "elapsed_hours": 10.0,
                "elapsed_seconds": 36000.0,
                "scope": "system_inspect (read-only)",
            },
            scope_integrity=_intact_scope(),
        )
        md = render_broader_system_scope_markdown(review)
        assert "READY FOR PLANNING" in md
        assert "Phase 7.20" in md
        assert "inspect_only" in md

    def test_hold_markdown(self):
        criteria = evaluate_broader_system_scope(
            _healthy_evidence(),
            _healthy_sys_metrics(10),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            3 * 3600,
            0.0,
            0,
        )
        review = BroaderSystemScopeEvaluation(
            decision="hold_broader_system_scope",
            rollout_stage="stage4",
            criteria=criteria,
            evidence_summary={},
            extended_monitoring_decision="stageD_sustained_stable",
            observation_window={
                "activation_at": "T1",
                "review_at": "T2",
                "elapsed_hours": 3.0,
                "elapsed_seconds": 10800.0,
                "scope": "test",
            },
            scope_integrity=_intact_scope(),
        )
        md = render_broader_system_scope_markdown(review)
        assert "HOLD" in md

    def test_block_markdown(self):
        ev = _healthy_evidence()
        ev["heartbeat_overall"] = "unhealthy"
        criteria = evaluate_broader_system_scope(
            ev,
            _healthy_sys_metrics(20),
            BROADER_REQUIRED_EXTENDED_DECISION,
            _no_recoveries(),
            0,
            _intact_scope(),
            10 * 3600,
            0.0,
            0,
        )
        review = BroaderSystemScopeEvaluation(
            decision="block_broader_system_scope",
            rollout_stage="stage4",
            criteria=criteria,
            evidence_summary={},
            extended_monitoring_decision="stageD_sustained_stable",
            observation_window={
                "activation_at": "T1",
                "review_at": "T2",
                "elapsed_hours": 10.0,
                "elapsed_seconds": 36000.0,
                "scope": "test",
            },
            scope_integrity=_intact_scope(),
        )
        md = render_broader_system_scope_markdown(review)
        assert "BLOCKED" in md
        assert "Do NOT proceed" in md

    def test_threshold_progression_in_markdown(self):
        criteria = _all_pass_criteria()
        review = BroaderSystemScopeEvaluation(
            decision="ready_for_broader_system_scope_planning",
            rollout_stage="stage4",
            criteria=criteria,
            evidence_summary={},
            extended_monitoring_decision="stageD_sustained_stable",
            observation_window={
                "activation_at": "T1",
                "review_at": "T2",
                "elapsed_hours": 10.0,
                "elapsed_seconds": 36000.0,
                "scope": "test",
            },
            scope_integrity=_intact_scope(),
        )
        md = render_broader_system_scope_markdown(review)
        assert "Threshold Progression" in md
        assert "Extended (7.19)" in md
        assert "Broader (7.20)" in md

    def test_file_creation(self, tmp_path):
        _populate_broader_env(tmp_path, num_system_runs=20)
        result = review_broader_system_scope(tmp_path, now_override="2026-03-08T18:00:00Z")
        md_path, json_path = write_broader_system_scope_evaluation(result, tmp_path)
        assert md_path.exists()
        assert json_path.exists()

    def test_json_valid(self, tmp_path):
        _populate_broader_env(tmp_path, num_system_runs=20)
        result = review_broader_system_scope(tmp_path, now_override="2026-03-08T18:00:00Z")
        _, json_path = write_broader_system_scope_evaluation(result, tmp_path)
        data = json.loads(json_path.read_text())
        assert data["decision"] == "ready_for_broader_system_scope_planning"

    def test_json_roundtrip(self, tmp_path):
        _populate_broader_env(tmp_path, num_system_runs=20)
        result = review_broader_system_scope(tmp_path, now_override="2026-03-08T18:00:00Z")
        d = result.to_dict()
        assert d["decision"] == "ready_for_broader_system_scope_planning"
        assert len(d["criteria"]) == 12

    def test_file_paths(self, tmp_path):
        _populate_broader_env(tmp_path, num_system_runs=20)
        result = review_broader_system_scope(tmp_path, now_override="2026-03-08T18:00:00Z")
        md_path, json_path = write_broader_system_scope_evaluation(result, tmp_path)
        assert md_path.name == "phase7_broader_system_scope_evaluation.md"
        assert json_path.name == "broader_system_scope_evaluation.json"


# ---------------------------------------------------------------------------
# TestNoScopeExpansion
# ---------------------------------------------------------------------------


class TestNoScopeExpansion:
    def test_feature_flags_unchanged(self, tmp_path):
        _populate_broader_env(tmp_path, num_system_runs=20)
        flags_before = json.loads((tmp_path / "STATE" / "config" / "feature_flags.json").read_text())
        review_broader_system_scope(tmp_path, now_override="2026-03-08T18:00:00Z")
        flags_after = json.loads((tmp_path / "STATE" / "config" / "feature_flags.json").read_text())
        assert flags_before == flags_after

    def test_activation_log_unchanged(self, tmp_path):
        _populate_broader_env(tmp_path, num_system_runs=20)
        log_before = (tmp_path / "STATE" / "activation_log.jsonl").read_text()
        review_broader_system_scope(tmp_path, now_override="2026-03-08T18:00:00Z")
        log_after = (tmp_path / "STATE" / "activation_log.jsonl").read_text()
        assert log_before == log_after

    def test_classes_not_expanded(self, tmp_path):
        _populate_broader_env(tmp_path, num_system_runs=20)
        result = review_broader_system_scope(tmp_path, now_override="2026-03-08T18:00:00Z")
        assert result.enabled_classes == ["research", "code_review", "code_impl", "system"]

    def test_scope_remains_inspect_only(self, tmp_path):
        _populate_broader_env(tmp_path, num_system_runs=20)
        result = review_broader_system_scope(tmp_path, now_override="2026-03-08T18:00:00Z")
        assert result.scope_integrity["system_scope"] == "inspect_only"

    def test_extended_monitoring_file_unchanged(self, tmp_path):
        _populate_broader_env(tmp_path, num_system_runs=20)
        ext_before = (tmp_path / "STATE" / "stageD_extended_monitoring.json").read_text()
        review_broader_system_scope(tmp_path, now_override="2026-03-08T18:00:00Z")
        ext_after = (tmp_path / "STATE" / "stageD_extended_monitoring.json").read_text()
        assert ext_before == ext_after
