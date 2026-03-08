"""Phase 7.18 — Tests for Stage D monitoring and stability review.

Tests cover:
  1. Healthy Stage D evidence -> stable_continue_stageD
  2. Insufficient evidence -> hold_stageD
  3. Poor system_inspect live behavior -> rollback_system_inspect_recommended
  4. Blocked mutation attempts correctly influence Stage D review
  5. Scope integrity verification
  6. system_inspect metrics collection
  7. Artifact output is correct and auditable
  8. Criteria thresholds and severity classification
"""

import json
import pytest
from pathlib import Path

from agents.rollout_gate import (
    StageDStabilityReview,
    RolloutCriterion,
    evaluate_stageD_stability,
    decide_stageD_stability,
    review_stageD_stability,
    render_stageD_stability_markdown,
    write_stageD_stability_review,
    _collect_system_inspect_metrics,
    _count_blocked_mutation_attempts,
    _verify_scope_integrity,
    STAGED_MIN_SYSTEM_INSPECT_RUNS,
    STAGED_MAX_SYSTEM_INSPECT_FAILURE_RATE,
    STAGED_MAX_FAILURE_RATE,
    STAGED_MAX_SYSTEM_VERIFIER_REJECTION_RATE,
    STAGED_MAX_POLICY_VIOLATIONS,
    STAGED_MAX_BUDGET_EXHAUSTIONS,
    STAGED_MAX_RECOVERY_ANOMALIES,
    STAGED_MAX_BLOCKED_MUTATION_ATTEMPTS,
    STAGED_HEARTBEAT_REQUIRED,
    STAGE4_ALLOWED_OPERATIONS,
    STAGE4_BLOCKED_OPERATIONS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_workflow(task_class: str, status: str = "completed", **extra) -> dict:
    return {"task_class": task_class, "status": status, **extra}


def _populate_stageD_env(root: Path) -> None:
    """Populate a test environment that represents a healthy Stage D state."""
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

    metrics = {"contract_success": {"_total": 20}, "contract_failure": {"_total": 0}}
    (state / "metrics.json").write_text(json.dumps(metrics))

    # Activation log with Stage D activation
    log_entries = [
        json.dumps({
            "attempted_at": "2026-03-07T12:00:00Z",
            "outcome": "activated",
            "decision": "ready_to_expand",
            "reason": "Stage 3 expansion applied",
            "pre_config": {},
            "post_config": {},
            "blocking_criteria": [],
        }),
        json.dumps({
            "attempted_at": "2026-03-08T14:00:00Z",
            "outcome": "activated",
            "gate_decision": "ready_to_activate_stage4",
            "reason": "Stage 4 activation applied",
            "activated_scope": "system_inspect",
            "pre_config": {},
            "post_config": {},
            "blocking_criteria": [],
            "plan_validation": {"valid": True, "errors": []},
        }),
    ]
    (state / "activation_log.jsonl").write_text("\n".join(log_entries) + "\n")

    # Existing workflows — mix of classes
    for i in range(5):
        wf = _make_workflow("code_impl", "completed")
        (state / "workflows" / f"code_impl_{i}.json").write_text(json.dumps(wf))
    for i in range(3):
        wf = _make_workflow("research", "completed")
        (state / "workflows" / f"research_{i}.json").write_text(json.dumps(wf))

    # system_inspect workflows (meeting minimum)
    for i in range(STAGED_MIN_SYSTEM_INSPECT_RUNS):
        wf = _make_workflow("system", "completed")
        (state / "workflows" / f"system_{i}.json").write_text(json.dumps(wf))


@pytest.fixture
def stageD_root(tmp_path):
    _populate_stageD_env(tmp_path)
    return tmp_path


def _healthy_evidence() -> dict:
    """Evidence dict representing a fully healthy Stage D state."""
    return {
        "heartbeat_overall": "healthy",
        "heartbeat_exists": True,
        "unhealthy_finding_count": 0,
        "total_workflows": 11,
        "completed_workflows": 11,
        "failed_workflows": 0,
        "halted_workflows": 0,
        "active_workflows": 0,
        "failure_rate": 0.0,
        "verifier_rejections": 0,
        "verifier_approvals": 3,
        "verifier_rejection_rate": 0.0,
        "contract_failures": 0,
        "contract_successes": 20,
        "contract_failure_rate": 0.0,
        "policy_violations": 0,
        "budget_exhaustions": 0,
        "orphaned_agents": 0,
        "stale_leases": 0,
        "recovery_events": 0,
        "rollout_stage": "stage4_research_code_review_code_impl_system_inspect",
        "supported_classes": ["research", "code_review", "code_impl", "system"],
        "enabled": True,
    }


def _healthy_sys_metrics() -> dict:
    return {
        "total_runs": 3,
        "completed": 3,
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


# ===========================================================================
# Test System Inspect Metrics Collection
# ===========================================================================

class TestSystemInspectMetrics:
    """Tests for _collect_system_inspect_metrics()."""

    def test_empty_workflows(self):
        m = _collect_system_inspect_metrics([])
        assert m["total_runs"] == 0
        assert m["completed"] == 0
        assert m["failed"] == 0
        assert m["failure_rate"] is None

    def test_all_completed(self):
        wfs = [
            _make_workflow("system", "completed"),
            _make_workflow("system", "completed"),
            _make_workflow("system", "completed"),
        ]
        m = _collect_system_inspect_metrics(wfs)
        assert m["total_runs"] == 3
        assert m["completed"] == 3
        assert m["failed"] == 0
        assert m["failure_rate"] == 0.0

    def test_some_failed(self):
        wfs = [
            _make_workflow("system", "completed"),
            _make_workflow("system", "failed"),
            _make_workflow("system", "halted"),
        ]
        m = _collect_system_inspect_metrics(wfs)
        assert m["total_runs"] == 3
        assert m["completed"] == 1
        assert m["failed"] == 2
        assert m["failure_rate"] == pytest.approx(0.667, abs=0.001)

    def test_ignores_non_system(self):
        wfs = [
            _make_workflow("research", "completed"),
            _make_workflow("system", "completed"),
            _make_workflow("code_impl", "failed"),
        ]
        m = _collect_system_inspect_metrics(wfs)
        assert m["total_runs"] == 1
        assert m["completed"] == 1

    def test_verifier_rejected(self):
        wfs = [
            _make_workflow("system", "completed"),
            _make_workflow("system", "halted", halt_reason="verifier_rejected"),
        ]
        m = _collect_system_inspect_metrics(wfs)
        assert m["verifier_rejected"] == 1


# ===========================================================================
# Test Blocked Mutation Attempts
# ===========================================================================

class TestBlockedMutationAttempts:
    """Tests for _count_blocked_mutation_attempts()."""

    def test_no_denials(self, tmp_path):
        (tmp_path / "STATE").mkdir(parents=True)
        count = _count_blocked_mutation_attempts(tmp_path)
        assert count == 0

    def test_system_class_denial(self, tmp_path):
        state = tmp_path / "STATE"
        state.mkdir(parents=True)
        denial = json.dumps({
            "task_class": "system",
            "reason": "blocked operation",
            "timestamp": "2026-03-08T15:00:00Z",
        })
        (state / "policy_denials.jsonl").write_text(denial + "\n")
        count = _count_blocked_mutation_attempts(tmp_path)
        assert count == 1

    def test_mutate_reason_denial(self, tmp_path):
        state = tmp_path / "STATE"
        state.mkdir(parents=True)
        denial = json.dumps({
            "task_class": "unknown",
            "reason": "system_mutate pattern detected",
            "timestamp": "2026-03-08T15:00:00Z",
        })
        (state / "policy_denials.jsonl").write_text(denial + "\n")
        count = _count_blocked_mutation_attempts(tmp_path)
        assert count == 1

    def test_filters_by_timestamp(self, tmp_path):
        state = tmp_path / "STATE"
        state.mkdir(parents=True)
        denials = [
            json.dumps({
                "task_class": "system",
                "reason": "blocked",
                "timestamp": "2026-03-07T10:00:00Z",
            }),
            json.dumps({
                "task_class": "system",
                "reason": "blocked",
                "timestamp": "2026-03-08T16:00:00Z",
            }),
        ]
        (state / "policy_denials.jsonl").write_text("\n".join(denials) + "\n")
        count = _count_blocked_mutation_attempts(
            tmp_path, since_ts="2026-03-08T14:00:00Z"
        )
        assert count == 1

    def test_non_system_non_mutate_ignored(self, tmp_path):
        state = tmp_path / "STATE"
        state.mkdir(parents=True)
        denial = json.dumps({
            "task_class": "research",
            "reason": "rate limited",
            "timestamp": "2026-03-08T15:00:00Z",
        })
        (state / "policy_denials.jsonl").write_text(denial + "\n")
        count = _count_blocked_mutation_attempts(tmp_path)
        assert count == 0


# ===========================================================================
# Test Scope Integrity Verification
# ===========================================================================

class TestScopeIntegrity:
    """Tests for _verify_scope_integrity()."""

    def test_intact_scope(self, stageD_root):
        si = _verify_scope_integrity(stageD_root)
        assert si["intact"] is True
        assert si["system_scope"] == "inspect_only"
        assert si["system_in_classes"] is True
        assert si["stage"] == "D"

    def test_missing_feature_flags(self, tmp_path):
        (tmp_path / "STATE" / "config").mkdir(parents=True)
        si = _verify_scope_integrity(tmp_path)
        assert si["intact"] is False
        assert "not found" in si["reason"]

    def test_wrong_scope(self, tmp_path):
        state = tmp_path / "STATE"
        (state / "config").mkdir(parents=True)
        flags = {
            "phase7_orchestrator": {
                "supported_classes": ["research", "code_review", "code_impl", "system"],
                "stage": "D",
                "system_scope": "full_access",
                "system_allowed_operations": sorted(STAGE4_ALLOWED_OPERATIONS),
                "system_blocked_operations": sorted(STAGE4_BLOCKED_OPERATIONS),
            }
        }
        (state / "config" / "feature_flags.json").write_text(json.dumps(flags))
        si = _verify_scope_integrity(tmp_path)
        assert si["intact"] is False
        assert "inspect_only" in si["reason"]

    def test_wrong_stage(self, tmp_path):
        state = tmp_path / "STATE"
        (state / "config").mkdir(parents=True)
        flags = {
            "phase7_orchestrator": {
                "supported_classes": ["research", "code_review", "code_impl", "system"],
                "stage": "C",
                "system_scope": "inspect_only",
                "system_allowed_operations": sorted(STAGE4_ALLOWED_OPERATIONS),
                "system_blocked_operations": sorted(STAGE4_BLOCKED_OPERATIONS),
            }
        }
        (state / "config" / "feature_flags.json").write_text(json.dumps(flags))
        si = _verify_scope_integrity(tmp_path)
        assert si["intact"] is False
        assert "'C'" in si["reason"]

    def test_system_not_in_classes(self, tmp_path):
        state = tmp_path / "STATE"
        (state / "config").mkdir(parents=True)
        flags = {
            "phase7_orchestrator": {
                "supported_classes": ["research", "code_review", "code_impl"],
                "stage": "D",
                "system_scope": "inspect_only",
                "system_allowed_operations": sorted(STAGE4_ALLOWED_OPERATIONS),
                "system_blocked_operations": sorted(STAGE4_BLOCKED_OPERATIONS),
            }
        }
        (state / "config" / "feature_flags.json").write_text(json.dumps(flags))
        si = _verify_scope_integrity(tmp_path)
        assert si["intact"] is False
        assert "not in supported_classes" in si["reason"]

    def test_extra_allowed_operations(self, tmp_path):
        state = tmp_path / "STATE"
        (state / "config").mkdir(parents=True)
        flags = {
            "phase7_orchestrator": {
                "supported_classes": ["research", "code_review", "code_impl", "system"],
                "stage": "D",
                "system_scope": "inspect_only",
                "system_allowed_operations": sorted(STAGE4_ALLOWED_OPERATIONS) + ["deployment"],
                "system_blocked_operations": sorted(STAGE4_BLOCKED_OPERATIONS),
            }
        }
        (state / "config" / "feature_flags.json").write_text(json.dumps(flags))
        si = _verify_scope_integrity(tmp_path)
        assert si["intact"] is False
        assert "unexpected allowed" in si["reason"]

    def test_missing_blocked_operations(self, tmp_path):
        state = tmp_path / "STATE"
        (state / "config").mkdir(parents=True)
        flags = {
            "phase7_orchestrator": {
                "supported_classes": ["research", "code_review", "code_impl", "system"],
                "stage": "D",
                "system_scope": "inspect_only",
                "system_allowed_operations": sorted(STAGE4_ALLOWED_OPERATIONS),
                "system_blocked_operations": ["deployment"],  # missing many
            }
        }
        (state / "config" / "feature_flags.json").write_text(json.dumps(flags))
        si = _verify_scope_integrity(tmp_path)
        assert si["intact"] is False
        assert "missing blocked" in si["reason"]


# ===========================================================================
# Test Stable Continue Decision
# ===========================================================================

class TestStableContinue:
    """Tests for healthy Stage D -> stable_continue_stageD."""

    def test_all_criteria_pass(self):
        evidence = _healthy_evidence()
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        decision, next_action = decide_stageD_stability(criteria)
        assert decision == "stable_continue_stageD"

    def test_all_criteria_pass_count(self):
        evidence = _healthy_evidence()
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        assert len(criteria) == 13

    def test_all_criteria_passed(self):
        evidence = _healthy_evidence()
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        for c in criteria:
            assert c.passed, f"Expected {c.name} to pass, got: {c.detail}"

    def test_hard_criteria_count(self):
        evidence = _healthy_evidence()
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        hard = [c for c in criteria if c.severity == "hard"]
        assert len(hard) == 7

    def test_soft_criteria_count(self):
        evidence = _healthy_evidence()
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        soft = [c for c in criteria if c.severity == "soft"]
        assert len(soft) == 6

    def test_review_integration(self, stageD_root):
        review = review_stageD_stability(stageD_root)
        # With enough system workflows and healthy state, should be stable
        assert review.decision == "stable_continue_stageD"
        assert review.rollout_stage == "stage4_research_code_review_code_impl_system_inspect"
        assert "system" in review.enabled_classes

    def test_scope_integrity_in_review(self, stageD_root):
        review = review_stageD_stability(stageD_root)
        assert review.scope_integrity["intact"] is True

    def test_observation_window_populated(self, stageD_root):
        review = review_stageD_stability(stageD_root)
        ow = review.observation_window
        assert ow["activation_at"] == "2026-03-08T14:00:00Z"
        assert ow["scope"] == "system_inspect (read-only)"
        assert ow["review_at"]  # non-empty

    def test_activation_record_captured(self, stageD_root):
        review = review_stageD_stability(stageD_root)
        assert review.activation_record.get("activated_scope") == "system_inspect"


# ===========================================================================
# Test Hold Decision
# ===========================================================================

class TestHoldStageD:
    """Tests for insufficient evidence -> hold_stageD."""

    def test_insufficient_system_runs(self):
        evidence = _healthy_evidence()
        sys_metrics = {
            "total_runs": 1,
            "completed": 1,
            "failed": 0,
            "verifier_rejected": 0,
            "failure_rate": 0.0,
        }
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "hold_stageD"

    def test_zero_system_runs(self):
        evidence = _healthy_evidence()
        sys_metrics = {
            "total_runs": 0,
            "completed": 0,
            "failed": 0,
            "verifier_rejected": 0,
            "failure_rate": None,
        }
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "hold_stageD"

    def test_hold_on_recovery_anomalies(self):
        evidence = _healthy_evidence()
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics,
            post_activation_recoveries=1,  # > STAGED_MAX_RECOVERY_ANOMALIES (0)
            blocked_mutation_attempts=0,
            scope_integrity=scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "hold_stageD"

    def test_hold_on_too_many_blocked_mutations(self):
        evidence = _healthy_evidence()
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0,
            blocked_mutation_attempts=STAGED_MAX_BLOCKED_MUTATION_ATTEMPTS + 1,
            scope_integrity=scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "hold_stageD"

    def test_hold_on_orphaned_agents(self):
        evidence = _healthy_evidence()
        evidence["orphaned_agents"] = 1
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "hold_stageD"

    def test_hold_on_stale_leases(self):
        evidence = _healthy_evidence()
        evidence["stale_leases"] = 1
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "hold_stageD"

    def test_hold_next_action_mentions_evidence(self):
        evidence = _healthy_evidence()
        sys_metrics = {
            "total_runs": 1,
            "completed": 1,
            "failed": 0,
            "verifier_rejected": 0,
            "failure_rate": 0.0,
        }
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        _, next_action = decide_stageD_stability(criteria)
        assert "Insufficient" in next_action or "evidence" in next_action.lower()

    def test_hold_from_review(self, tmp_path):
        """Stage D env with zero system workflows -> hold."""
        _populate_stageD_env(tmp_path)
        # Remove all system workflows
        state = tmp_path / "STATE"
        for f in state.glob("workflows/system_*.json"):
            f.unlink()

        review = review_stageD_stability(tmp_path)
        assert review.decision == "hold_stageD"


# ===========================================================================
# Test Rollback Decision
# ===========================================================================

class TestRollbackRecommended:
    """Tests for hard failures -> rollback_system_inspect_recommended."""

    def test_unhealthy_heartbeat(self):
        evidence = _healthy_evidence()
        evidence["heartbeat_overall"] = "unhealthy"
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_degraded_heartbeat(self):
        """Strict: even 'degraded' is not 'healthy'."""
        evidence = _healthy_evidence()
        evidence["heartbeat_overall"] = "degraded"
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_policy_violations(self):
        evidence = _healthy_evidence()
        evidence["policy_violations"] = 1
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_budget_exhaustions(self):
        evidence = _healthy_evidence()
        evidence["budget_exhaustions"] = 1
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_high_overall_failure_rate(self):
        evidence = _healthy_evidence()
        evidence["failure_rate"] = 0.50  # > STAGED_MAX_FAILURE_RATE (0.20)
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_scope_integrity_violated(self):
        evidence = _healthy_evidence()
        sys_metrics = _healthy_sys_metrics()
        scope = {
            "intact": False,
            "reason": "system_scope is 'full_access'",
            "system_scope": "full_access",
            "system_in_classes": True,
            "stage": "D",
        }

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_wrong_stage(self):
        evidence = _healthy_evidence()
        evidence["rollout_stage"] = "stage3_research_code_review_code_impl"
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_system_not_in_classes(self):
        evidence = _healthy_evidence()
        evidence["supported_classes"] = ["research", "code_review", "code_impl"]
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_rollback_next_action_mentions_revert(self):
        evidence = _healthy_evidence()
        evidence["heartbeat_overall"] = "unhealthy"
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        _, next_action = decide_stageD_stability(criteria)
        assert "Stage C" in next_action or "revert" in next_action.lower()

    def test_hard_trumps_soft(self):
        """Hard failure trumps soft failure — result is rollback not hold."""
        evidence = _healthy_evidence()
        evidence["heartbeat_overall"] = "unhealthy"
        evidence["orphaned_agents"] = 5
        sys_metrics = {
            "total_runs": 1,
            "completed": 1,
            "failed": 0,
            "verifier_rejected": 0,
            "failure_rate": 0.0,
        }
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "rollback_system_inspect_recommended"


# ===========================================================================
# Test Decision Logic
# ===========================================================================

class TestDecisionLogic:
    """Tests for decide_stageD_stability() decision logic."""

    def test_all_pass(self):
        criteria = [
            RolloutCriterion("c1", True, 0, 0, "ok", "hard"),
            RolloutCriterion("c2", True, 0, 0, "ok", "soft"),
        ]
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "stable_continue_stageD"

    def test_hard_failure_rollback(self):
        criteria = [
            RolloutCriterion("c1", False, 0, 0, "fail", "hard"),
            RolloutCriterion("c2", True, 0, 0, "ok", "soft"),
        ]
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "rollback_system_inspect_recommended"

    def test_soft_failure_hold(self):
        criteria = [
            RolloutCriterion("c1", True, 0, 0, "ok", "hard"),
            RolloutCriterion("c2", False, 0, 0, "fail", "soft"),
        ]
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "hold_stageD"

    def test_multiple_hard(self):
        criteria = [
            RolloutCriterion("c1", False, 0, 0, "fail", "hard"),
            RolloutCriterion("c2", False, 0, 0, "fail", "hard"),
        ]
        decision, next_action = decide_stageD_stability(criteria)
        assert decision == "rollback_system_inspect_recommended"
        assert "c1" in next_action
        assert "c2" in next_action

    def test_multiple_soft(self):
        criteria = [
            RolloutCriterion("c1", True, 0, 0, "ok", "hard"),
            RolloutCriterion("c2", False, 0, 0, "fail", "soft"),
            RolloutCriterion("c3", False, 0, 0, "fail", "soft"),
        ]
        decision, next_action = decide_stageD_stability(criteria)
        assert decision == "hold_stageD"
        assert "c2" in next_action
        assert "c3" in next_action

    def test_insufficient_runs_hold_priority(self):
        """When system_inspect_minimum_runs fails, hold message mentions evidence."""
        criteria = [
            RolloutCriterion("c1", True, 0, 0, "ok", "hard"),
            RolloutCriterion(
                "system_inspect_minimum_runs", False, 1,
                STAGED_MIN_SYSTEM_INSPECT_RUNS, "too few", "soft",
            ),
            RolloutCriterion("c3", False, 0, 0, "fail", "soft"),
        ]
        decision, next_action = decide_stageD_stability(criteria)
        assert decision == "hold_stageD"
        assert "Insufficient" in next_action


# ===========================================================================
# Test Blocked Mutation Influence
# ===========================================================================

class TestBlockedMutationInfluence:
    """Tests that blocked mutation attempts correctly influence Stage D review."""

    def test_few_mutations_accepted(self):
        evidence = _healthy_evidence()
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0,
            blocked_mutation_attempts=2,  # within threshold
            scope_integrity=scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "stable_continue_stageD"

    def test_threshold_boundary(self):
        evidence = _healthy_evidence()
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        # Exactly at threshold — should still pass
        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0,
            blocked_mutation_attempts=STAGED_MAX_BLOCKED_MUTATION_ATTEMPTS,
            scope_integrity=scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "stable_continue_stageD"

    def test_over_threshold_holds(self):
        evidence = _healthy_evidence()
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0,
            blocked_mutation_attempts=STAGED_MAX_BLOCKED_MUTATION_ATTEMPTS + 1,
            scope_integrity=scope,
        )
        decision, _ = decide_stageD_stability(criteria)
        assert decision == "hold_stageD"

    def test_criterion_detail_includes_count(self):
        evidence = _healthy_evidence()
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0,
            blocked_mutation_attempts=3,
            scope_integrity=scope,
        )
        bma = next(c for c in criteria if c.name == "blocked_mutation_attempts")
        assert "3" in bma.detail
        assert str(STAGED_MAX_BLOCKED_MUTATION_ATTEMPTS) in bma.detail


# ===========================================================================
# Test Criteria Evaluation Details
# ===========================================================================

class TestCriteriaEvaluation:
    """Tests for specific criteria evaluation behavior."""

    def test_heartbeat_strict_healthy(self):
        evidence = _healthy_evidence()
        evidence["heartbeat_overall"] = "healthy"
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        hb = next(c for c in criteria if c.name == "heartbeat_healthy")
        assert hb.passed is True
        assert hb.severity == "hard"

    def test_heartbeat_no_data_fails(self):
        evidence = _healthy_evidence()
        evidence["heartbeat_overall"] = ""
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        hb = next(c for c in criteria if c.name == "heartbeat_healthy")
        assert hb.passed is False

    def test_no_failure_rate_passes(self):
        """When no terminal workflows exist, failure rate is None -> pass."""
        evidence = _healthy_evidence()
        evidence["failure_rate"] = None
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        fr = next(c for c in criteria if c.name == "overall_failure_rate")
        assert fr.passed is True

    def test_system_inspect_failure_rate_criterion(self):
        evidence = _healthy_evidence()
        sys_metrics = {
            "total_runs": 5,
            "completed": 4,
            "failed": 1,
            "verifier_rejected": 0,
            "failure_rate": 0.2,
        }
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        sifr = next(c for c in criteria if c.name == "system_inspect_failure_rate")
        assert sifr.passed is True  # 0.2 == threshold

    def test_system_inspect_failure_rate_over(self):
        evidence = _healthy_evidence()
        sys_metrics = {
            "total_runs": 5,
            "completed": 3,
            "failed": 2,
            "verifier_rejected": 0,
            "failure_rate": 0.4,
        }
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        sifr = next(c for c in criteria if c.name == "system_inspect_failure_rate")
        assert sifr.passed is False

    def test_severity_classification(self):
        evidence = _healthy_evidence()
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        hard_names = {c.name for c in criteria if c.severity == "hard"}
        expected_hard = {
            "heartbeat_healthy",
            "no_policy_violations",
            "no_budget_exhaustions",
            "overall_failure_rate",
            "scope_integrity",
            "stage_is_D",
            "system_class_active",
        }
        assert hard_names == expected_hard

    def test_all_criteria_names(self):
        evidence = _healthy_evidence()
        sys_metrics = _healthy_sys_metrics()
        scope = _intact_scope()

        criteria = evaluate_stageD_stability(
            evidence, sys_metrics, 0, 0, scope,
        )
        names = [c.name for c in criteria]
        expected = [
            "heartbeat_healthy",
            "no_policy_violations",
            "no_budget_exhaustions",
            "overall_failure_rate",
            "scope_integrity",
            "stage_is_D",
            "system_class_active",
            "system_inspect_minimum_runs",
            "system_inspect_failure_rate",
            "system_verifier_rejection_rate",
            "recovery_anomalies",
            "blocked_mutation_attempts",
            "no_workflow_anomalies",
        ]
        assert names == expected


# ===========================================================================
# Test Artifact Generation
# ===========================================================================

class TestArtifactGeneration:
    """Tests for markdown rendering and file writing."""

    def test_stable_markdown_content(self):
        review = StageDStabilityReview(
            decision="stable_continue_stageD",
            rollout_stage="stage4_research_code_review_code_impl_system_inspect",
            enabled_classes=["research", "code_review", "code_impl", "system"],
            system_inspect_metrics=_healthy_sys_metrics(),
            evidence_summary={"heartbeat_overall": "healthy", "blocked_mutation_attempts": 0},
            observation_window={"activation_at": "2026-03-08T14:00:00Z", "review_at": "2026-03-08T16:00:00Z", "scope": "system_inspect (read-only)"},
            scope_integrity=_intact_scope(),
            next_action="Stage D is stable.",
        )
        md = render_stageD_stability_markdown(review)
        assert "Phase 7.18" in md
        assert "STABLE" in md
        assert "stable_continue_stageD" in md
        assert "inspect_only" in md

    def test_hold_markdown_content(self):
        review = StageDStabilityReview(
            decision="hold_stageD",
            rollout_stage="stage4_research_code_review_code_impl_system_inspect",
            enabled_classes=["research", "code_review", "code_impl", "system"],
            system_inspect_metrics={"total_runs": 0, "completed": 0, "failed": 0, "verifier_rejected": 0, "failure_rate": None},
            evidence_summary={"heartbeat_overall": "healthy", "blocked_mutation_attempts": 0},
            observation_window={"activation_at": "2026-03-08T14:00:00Z", "review_at": "2026-03-08T16:00:00Z", "scope": "system_inspect (read-only)"},
            scope_integrity=_intact_scope(),
            next_action="Insufficient evidence.",
        )
        md = render_stageD_stability_markdown(review)
        assert "HOLD" in md
        assert "Do NOT expand" in md

    def test_rollback_markdown_content(self):
        review = StageDStabilityReview(
            decision="rollback_system_inspect_recommended",
            rollout_stage="stage4_research_code_review_code_impl_system_inspect",
            enabled_classes=["research", "code_review", "code_impl", "system"],
            system_inspect_metrics=_healthy_sys_metrics(),
            evidence_summary={"heartbeat_overall": "unhealthy", "blocked_mutation_attempts": 0},
            observation_window={"activation_at": "2026-03-08T14:00:00Z", "review_at": "2026-03-08T16:00:00Z", "scope": "system_inspect (read-only)"},
            scope_integrity=_intact_scope(),
            next_action="Rollback to Stage C.",
        )
        md = render_stageD_stability_markdown(review)
        assert "ROLLBACK" in md
        assert "Stage C" in md
        assert "ROLLBACK RECOMMENDED" in md

    def test_markdown_has_observation_window(self):
        review = StageDStabilityReview(
            decision="stable_continue_stageD",
            rollout_stage="stage4_test",
            enabled_classes=["research"],
            observation_window={"activation_at": "2026-03-08T14:00:00Z", "review_at": "2026-03-08T16:00:00Z", "scope": "system_inspect (read-only)"},
            scope_integrity=_intact_scope(),
        )
        md = render_stageD_stability_markdown(review)
        assert "Observation Window" in md
        assert "2026-03-08T14:00:00Z" in md

    def test_markdown_has_scope_integrity(self):
        review = StageDStabilityReview(
            decision="stable_continue_stageD",
            rollout_stage="stage4_test",
            enabled_classes=["research"],
            observation_window={},
            scope_integrity=_intact_scope(),
        )
        md = render_stageD_stability_markdown(review)
        assert "Scope Integrity" in md
        assert "INTACT" in md

    def test_markdown_has_criteria_table(self):
        review = StageDStabilityReview(
            decision="stable_continue_stageD",
            rollout_stage="stage4_test",
            enabled_classes=["research"],
            criteria=[
                RolloutCriterion("test_crit", True, "ok", "ok", "all good", "hard"),
            ],
            observation_window={},
            scope_integrity=_intact_scope(),
        )
        md = render_stageD_stability_markdown(review)
        assert "Stability Criteria" in md
        assert "test_crit" in md
        assert "PASS" in md

    def test_write_creates_files(self, tmp_path):
        (tmp_path / "WORK").mkdir()
        (tmp_path / "STATE").mkdir()

        review = StageDStabilityReview(
            decision="stable_continue_stageD",
            rollout_stage="stage4_test",
            enabled_classes=["research"],
            observation_window={},
            scope_integrity=_intact_scope(),
        )
        md_path, json_path = write_stageD_stability_review(review, tmp_path)
        assert md_path.exists()
        assert json_path.exists()

    def test_write_json_valid(self, tmp_path):
        (tmp_path / "WORK").mkdir()
        (tmp_path / "STATE").mkdir()

        review = StageDStabilityReview(
            decision="stable_continue_stageD",
            rollout_stage="stage4_test",
            enabled_classes=["research"],
            observation_window={},
            scope_integrity=_intact_scope(),
        )
        _, json_path = write_stageD_stability_review(review, tmp_path)
        data = json.loads(json_path.read_text())
        assert data["decision"] == "stable_continue_stageD"

    def test_write_paths(self, tmp_path):
        (tmp_path / "WORK").mkdir()
        (tmp_path / "STATE").mkdir()

        review = StageDStabilityReview(
            decision="stable_continue_stageD",
            rollout_stage="stage4_test",
            enabled_classes=["research"],
            observation_window={},
            scope_integrity=_intact_scope(),
        )
        md_path, json_path = write_stageD_stability_review(review, tmp_path)
        assert md_path.name == "phase7_stageD_stability_review.md"
        assert json_path.name == "stageD_stability_review.json"

    def test_to_dict_roundtrip(self):
        review = StageDStabilityReview(
            decision="hold_stageD",
            rollout_stage="stage4_test",
            enabled_classes=["research", "system"],
            criteria=[
                RolloutCriterion("c1", True, 0, 0, "ok", "hard"),
            ],
            system_inspect_metrics=_healthy_sys_metrics(),
            observation_window={"activation_at": "2026-03-08T14:00:00Z"},
            scope_integrity=_intact_scope(),
        )
        d = review.to_dict()
        assert d["decision"] == "hold_stageD"
        assert d["observation_window"]["activation_at"] == "2026-03-08T14:00:00Z"
        assert len(d["criteria"]) == 1
        # Ensure it's JSON-serializable
        json.dumps(d, default=str)

    def test_blocked_mutation_section_in_md(self):
        review = StageDStabilityReview(
            decision="stable_continue_stageD",
            rollout_stage="stage4_test",
            enabled_classes=["research"],
            evidence_summary={"blocked_mutation_attempts": 3},
            observation_window={},
            scope_integrity=_intact_scope(),
        )
        md = render_stageD_stability_markdown(review)
        assert "Blocked Mutation Attempts" in md
        assert "3" in md


# ===========================================================================
# Test No Scope Expansion
# ===========================================================================

class TestNoScopeExpansion:
    """Tests that Stage D review never changes live state."""

    def test_feature_flags_unchanged(self, stageD_root):
        pre = json.loads(
            (stageD_root / "STATE" / "config" / "feature_flags.json").read_text()
        )
        review_stageD_stability(stageD_root)
        post = json.loads(
            (stageD_root / "STATE" / "config" / "feature_flags.json").read_text()
        )
        assert pre == post

    def test_activation_log_unchanged(self, stageD_root):
        pre = (stageD_root / "STATE" / "activation_log.jsonl").read_text()
        review_stageD_stability(stageD_root)
        post = (stageD_root / "STATE" / "activation_log.jsonl").read_text()
        assert pre == post

    def test_no_new_state_files(self, stageD_root):
        pre_files = set(f.name for f in (stageD_root / "STATE").rglob("*") if f.is_file())
        review_stageD_stability(stageD_root)
        post_files = set(f.name for f in (stageD_root / "STATE").rglob("*") if f.is_file())
        assert pre_files == post_files

    def test_classes_not_expanded(self, stageD_root):
        review = review_stageD_stability(stageD_root)
        # Even with stable decision, should not add new classes
        assert "system" in review.enabled_classes
        assert len(review.enabled_classes) == 4  # no expansion

    def test_next_action_does_not_auto_expand(self, stageD_root):
        review = review_stageD_stability(stageD_root)
        assert "auto" not in review.next_action.lower() or "evaluated" in review.next_action.lower()
