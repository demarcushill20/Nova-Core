"""Phase 7.13 — Tests for Stage 3 post-activation stability review.

Tests cover:
  1. stable_continue — all criteria pass with sufficient code_impl evidence
  2. hold_stage3 — insufficient code_impl evidence or soft concerns
  3. rollback_code_impl_recommended — hard failures detected
  4. Verifier pressure influences Stage 3 review
  5. code_impl-specific metric extraction
  6. Post-activation recovery anomaly counting
  7. Stability review artifact generation
"""

import json
import time

import pytest

from agents.rollout_gate import (
    STAGE3_MAX_CODE_IMPL_FAILURE_RATE,
    STAGE3_MAX_RECOVERY_ANOMALIES,
    RolloutCriterion,
    _collect_code_impl_metrics,
    _count_post_activation_recoveries,
    _get_latest_activation,
    decide_stage3_stability,
    evaluate_stage3_stability,
    render_stability_review_markdown,
    review_stage3_stability,
    write_stability_review,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stage3_root(tmp_path):
    """Set up a minimal Stage 3 test environment."""
    state = tmp_path / "STATE"
    (state / "config").mkdir(parents=True)
    (state / "workflows").mkdir(parents=True)
    (state / "verifications").mkdir(parents=True)
    (tmp_path / "LOGS").mkdir(parents=True)
    (tmp_path / "WORK").mkdir(parents=True)

    # Feature flags — Stage 3 active
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
    hb = {
        "overall": "healthy",
        "findings": [],
        "generated_at": "2026-03-08T10:00:00Z",
    }
    (state / "heartbeat_multiagent.json").write_text(json.dumps(hb))

    # Clean metrics
    metrics = {
        "contract_success": {"_total": 10},
        "contract_failure": {"_total": 0},
    }
    (state / "metrics.json").write_text(json.dumps(metrics))

    # Activation log
    activation = {
        "attempted_at": "2026-03-08T08:11:44Z",
        "outcome": "activated",
        "decision": "ready_to_expand",
        "reason": "Stage 3 expansion applied",
    }
    (state / "activation_log.jsonl").write_text(json.dumps(activation) + "\n")

    return tmp_path


def _add_workflow(root, stem, status, task_class="research", halt_reason=None):
    """Add a workflow record to the test environment."""
    wf = {
        "workflow_id": stem,
        "status": status,
        "task_class": task_class,
        "stage": "C",
        "execution_path": "orchestrator",
        "created_at": time.time(),
        "completed_at": time.time(),
    }
    if halt_reason:
        wf["halt_reason"] = halt_reason
    path = root / "STATE" / "workflows" / f"{stem}.json"
    path.write_text(json.dumps(wf))


# ===================================================================
# Part 1 — stable_continue outcome
# ===================================================================


class TestStableContinue:
    """All criteria pass with sufficient code_impl evidence."""

    def test_healthy_stage3_is_stable(self, stage3_root):
        """Healthy environment with code_impl runs → stable_continue."""
        _add_workflow(stage3_root, "r1", "completed", "research")
        _add_workflow(stage3_root, "r2", "completed", "code_review")
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")

        review = review_stage3_stability(stage3_root)
        assert review.decision == "stable_continue"
        assert "code_impl" in review.enabled_classes
        assert review.code_impl_metrics["completed"] == 2
        assert review.code_impl_metrics["failed"] == 0

    def test_stable_with_mixed_classes(self, stage3_root):
        """Mixed class workflows all succeeded → stable."""
        for i in range(3):
            _add_workflow(stage3_root, f"res_{i}", "completed", "research")
        _add_workflow(stage3_root, "cr_1", "completed", "code_review")
        _add_workflow(stage3_root, "ci_1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci_2", "completed", "code_impl")

        review = review_stage3_stability(stage3_root)
        assert review.decision == "stable_continue"

    def test_stable_criteria_all_pass(self, stage3_root):
        """Every criterion passes in stable state."""
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")

        review = review_stage3_stability(stage3_root)
        for c in review.criteria:
            assert c.passed, f"Criterion {c.name} should pass: {c.detail}"

    def test_stable_next_action_mentions_stage4(self, stage3_root):
        """Stable decision mentions Stage 4 evaluation."""
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")

        review = review_stage3_stability(stage3_root)
        assert "Stage 4" in review.next_action or "stage 4" in review.next_action.lower()


# ===================================================================
# Part 2 — hold_stage3 outcome
# ===================================================================


class TestHoldStage3:
    """Insufficient evidence or soft concerns → hold."""

    def test_no_code_impl_runs_is_hold(self, stage3_root):
        """Zero code_impl runs → hold (insufficient evidence)."""
        _add_workflow(stage3_root, "r1", "completed", "research")
        _add_workflow(stage3_root, "r2", "completed", "research")

        review = review_stage3_stability(stage3_root)
        assert review.decision == "hold_stage3"
        assert review.code_impl_metrics["total_runs"] == 0

    def test_one_code_impl_run_is_hold(self, stage3_root):
        """One code_impl run < minimum threshold → hold."""
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")

        review = review_stage3_stability(stage3_root)
        assert review.decision == "hold_stage3"

    def test_high_code_impl_failure_rate_is_hold(self, stage3_root):
        """code_impl failure rate above threshold → hold."""
        # Add enough successful other-class workflows to keep overall rate
        # below the hard 30% threshold, isolating the soft code_impl concern.
        _add_workflow(stage3_root, "r1", "completed", "research")
        _add_workflow(stage3_root, "r2", "completed", "research")
        _add_workflow(stage3_root, "r3", "completed", "research")
        _add_workflow(stage3_root, "r4", "completed", "research")
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "failed", "code_impl")
        _add_workflow(stage3_root, "ci3", "failed", "code_impl")

        review = review_stage3_stability(stage3_root)
        assert review.decision == "hold_stage3"
        assert review.code_impl_metrics["failure_rate"] > STAGE3_MAX_CODE_IMPL_FAILURE_RATE

    def test_orphaned_agents_is_hold(self, stage3_root):
        """Orphaned agents → hold (soft concern)."""
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")

        # Create an orphaned agent (executing for > 600s)
        runtime_dir = stage3_root / "STATE" / "agents" / "runtime"
        runtime_dir.mkdir(parents=True)
        agent = {
            "agent_id": "orphan_1",
            "status": "executing",
            "started_at": time.time() - 700,
        }
        (runtime_dir / "orphan_1.json").write_text(json.dumps(agent))

        review = review_stage3_stability(stage3_root)
        assert review.decision == "hold_stage3"

    def test_excessive_recoveries_is_hold(self, stage3_root):
        """Too many recovery events since activation → hold."""
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")

        # Write recovery events after activation timestamp
        recovery_log = stage3_root / "LOGS" / "recovery.log"
        lines = []
        for i in range(STAGE3_MAX_RECOVERY_ANOMALIES + 1):
            lines.append(f"--- Recovery at 2026-03-08T09:{i:02d}:00+00:00 ---")
            lines.append(f"  [task_requeued] task_{i} — Requeued\n")
        recovery_log.write_text("\n".join(lines))

        review = review_stage3_stability(stage3_root)
        assert review.decision == "hold_stage3"

    def test_high_verifier_rejection_is_hold(self, stage3_root):
        """High verifier rejection rate → hold."""
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")

        # Create verifier reports with high rejection rate
        verif_dir = stage3_root / "STATE" / "verifications"
        for i in range(3):
            v = {"verdict": "rejected", "step_id": f"step_{i}"}
            (verif_dir / f"rej_{i}.json").write_text(json.dumps(v))
        v = {"verdict": "approved", "step_id": "step_ok"}
        (verif_dir / "app_1.json").write_text(json.dumps(v))

        review = review_stage3_stability(stage3_root)
        assert review.decision == "hold_stage3"


# ===================================================================
# Part 3 — rollback_code_impl_recommended outcome
# ===================================================================


class TestRollbackRecommended:
    """Hard failures → rollback code_impl."""

    def test_unhealthy_heartbeat_triggers_rollback(self, stage3_root):
        """UNHEALTHY heartbeat → rollback recommended."""
        hb = {"overall": "unhealthy", "findings": []}
        (stage3_root / "STATE" / "heartbeat_multiagent.json").write_text(json.dumps(hb))
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")

        review = review_stage3_stability(stage3_root)
        assert review.decision == "rollback_code_impl_recommended"

    def test_high_overall_failure_rate_triggers_rollback(self, stage3_root):
        """Overall failure rate above threshold → rollback."""
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")
        # Add many failures to push overall failure rate above 30%
        _add_workflow(stage3_root, "f1", "failed", "research")
        _add_workflow(stage3_root, "f2", "failed", "code_review")
        _add_workflow(stage3_root, "f3", "failed", "code_impl")

        review = review_stage3_stability(stage3_root)
        assert review.decision == "rollback_code_impl_recommended"

    def test_policy_violations_trigger_rollback(self, stage3_root):
        """Any policy violation → rollback."""
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")

        denials = stage3_root / "STATE" / "policy_denials.jsonl"
        denials.write_text('{"tool": "shell.run", "ok": false}\n')

        review = review_stage3_stability(stage3_root)
        assert review.decision == "rollback_code_impl_recommended"

    def test_budget_exhaustion_triggers_rollback(self, stage3_root):
        """Budget exhaustion → rollback."""
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")
        _add_workflow(
            stage3_root,
            "h1",
            "halted",
            "code_impl",
            halt_reason="budget_exhausted",
        )

        review = review_stage3_stability(stage3_root)
        assert review.decision == "rollback_code_impl_recommended"

    def test_rollback_next_action_mentions_remove(self, stage3_root):
        """Rollback recommendation mentions removing code_impl."""
        hb = {"overall": "unhealthy", "findings": []}
        (stage3_root / "STATE" / "heartbeat_multiagent.json").write_text(json.dumps(hb))
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")

        review = review_stage3_stability(stage3_root)
        assert "code_impl" in review.next_action
        assert "remove" in review.next_action.lower() or "Remove" in review.next_action


# ===================================================================
# Part 4 — code_impl metric extraction
# ===================================================================


class TestCodeImplMetrics:
    """Verify code_impl-specific metric extraction."""

    def test_empty_workflows(self):
        """No workflows → zero metrics."""
        m = _collect_code_impl_metrics([])
        assert m["total_runs"] == 0
        assert m["completed"] == 0
        assert m["failed"] == 0
        assert m["failure_rate"] is None

    def test_only_other_classes(self):
        """Non-code_impl workflows ignored."""
        wfs = [
            {"task_class": "research", "status": "completed"},
            {"task_class": "code_review", "status": "failed"},
        ]
        m = _collect_code_impl_metrics(wfs)
        assert m["total_runs"] == 0

    def test_mixed_code_impl_statuses(self):
        """Correctly counts code_impl completed/failed/rejected."""
        wfs = [
            {"task_class": "code_impl", "status": "completed"},
            {"task_class": "code_impl", "status": "completed"},
            {"task_class": "code_impl", "status": "failed"},
            {"task_class": "code_impl", "status": "failed", "halt_reason": "verifier_rejected"},
            {"task_class": "research", "status": "completed"},
        ]
        m = _collect_code_impl_metrics(wfs)
        assert m["total_runs"] == 4
        assert m["completed"] == 2
        assert m["failed"] == 2
        assert m["verifier_rejected"] == 1
        assert m["failure_rate"] == 0.5

    def test_all_completed(self):
        """All completed → 0% failure rate."""
        wfs = [
            {"task_class": "code_impl", "status": "completed"},
            {"task_class": "code_impl", "status": "completed"},
        ]
        m = _collect_code_impl_metrics(wfs)
        assert m["failure_rate"] == 0.0

    def test_all_failed(self):
        """All failed → 100% failure rate."""
        wfs = [
            {"task_class": "code_impl", "status": "failed"},
            {"task_class": "code_impl", "status": "failed"},
        ]
        m = _collect_code_impl_metrics(wfs)
        assert m["failure_rate"] == 1.0


# ===================================================================
# Part 5 — Post-activation recovery counting
# ===================================================================


class TestPostActivationRecoveries:
    """Verify recovery event counting relative to activation timestamp."""

    def test_no_recovery_log(self, tmp_path):
        """No recovery log → 0 recoveries."""
        count = _count_post_activation_recoveries(tmp_path, "2026-03-08T08:00:00Z")
        assert count == 0

    def test_all_before_activation(self, tmp_path):
        """All recovery events before activation → 0."""
        log = tmp_path / "LOGS" / "recovery.log"
        log.parent.mkdir(parents=True)
        log.write_text("--- Recovery at 2026-03-08T07:00:00+00:00 ---\n  [task_requeued] old_task\n")
        count = _count_post_activation_recoveries(tmp_path, "2026-03-08T08:00:00Z")
        assert count == 0

    def test_all_after_activation(self, tmp_path):
        """All recovery events after activation → counted."""
        log = tmp_path / "LOGS" / "recovery.log"
        log.parent.mkdir(parents=True)
        log.write_text(
            "--- Recovery at 2026-03-08T09:00:00+00:00 ---\n"
            "  [task_requeued] new_task_1\n"
            "--- Recovery at 2026-03-08T10:00:00+00:00 ---\n"
            "  [task_requeued] new_task_2\n"
        )
        count = _count_post_activation_recoveries(tmp_path, "2026-03-08T08:00:00Z")
        assert count == 2

    def test_mixed_before_and_after(self, tmp_path):
        """Only post-activation events counted."""
        log = tmp_path / "LOGS" / "recovery.log"
        log.parent.mkdir(parents=True)
        log.write_text(
            "--- Recovery at 2026-03-07T12:00:00+00:00 ---\n"
            "  [task_requeued] before_1\n"
            "--- Recovery at 2026-03-08T07:00:00+00:00 ---\n"
            "  [task_requeued] before_2\n"
            "--- Recovery at 2026-03-08T09:00:00+00:00 ---\n"
            "  [task_requeued] after_1\n"
        )
        count = _count_post_activation_recoveries(tmp_path, "2026-03-08T08:00:00Z")
        assert count == 1


# ===================================================================
# Part 6 — Decision logic
# ===================================================================


class TestDecisionLogic:
    """Verify decide_stage3_stability produces correct decisions."""

    def _passing_criteria(self):
        """Build a set of all-passing criteria."""
        return [
            RolloutCriterion("heartbeat_healthy", True, "healthy", "not unhealthy", "OK", "hard"),
            RolloutCriterion("overall_failure_rate", True, 0.0, 0.3, "OK", "hard"),
            RolloutCriterion("no_policy_violations", True, 0, 0, "OK", "hard"),
            RolloutCriterion("no_budget_exhaustions", True, 0, 0, "OK", "hard"),
            RolloutCriterion("code_impl_minimum_runs", True, 2, 2, "OK", "soft"),
            RolloutCriterion("code_impl_failure_rate", True, 0.0, 0.5, "OK", "soft"),
            RolloutCriterion("verifier_rejection_rate", True, None, 0.5, "OK", "soft"),
            RolloutCriterion("contract_failure_rate", True, 0.0, 0.3, "OK", "soft"),
            RolloutCriterion("recovery_anomalies", True, 0, 3, "OK", "soft"),
            RolloutCriterion("no_orphaned_agents", True, 0, 0, "OK", "soft"),
            RolloutCriterion("no_stale_leases", True, 0, 0, "OK", "soft"),
        ]

    def test_all_pass_is_stable(self):
        """All criteria pass → stable_continue."""
        criteria = self._passing_criteria()
        decision, _ = decide_stage3_stability(criteria)
        assert decision == "stable_continue"

    def test_hard_failure_is_rollback(self):
        """Any hard failure → rollback."""
        criteria = self._passing_criteria()
        criteria[0] = RolloutCriterion("heartbeat_healthy", False, "unhealthy", "not unhealthy", "FAIL", "hard")
        decision, _ = decide_stage3_stability(criteria)
        assert decision == "rollback_code_impl_recommended"

    def test_insufficient_impl_runs_is_hold(self):
        """Insufficient code_impl runs → hold."""
        criteria = self._passing_criteria()
        criteria[4] = RolloutCriterion("code_impl_minimum_runs", False, 1, 2, "FAIL", "soft")
        decision, _ = decide_stage3_stability(criteria)
        assert decision == "hold_stage3"

    def test_soft_failure_is_hold(self):
        """Soft failure (not impl_runs) → hold."""
        criteria = self._passing_criteria()
        criteria[9] = RolloutCriterion("no_orphaned_agents", False, 1, 0, "FAIL", "soft")
        decision, _ = decide_stage3_stability(criteria)
        assert decision == "hold_stage3"

    def test_hard_overrides_soft(self):
        """Hard failure takes precedence over soft failure."""
        criteria = self._passing_criteria()
        criteria[0] = RolloutCriterion("heartbeat_healthy", False, "unhealthy", "not unhealthy", "FAIL", "hard")
        criteria[4] = RolloutCriterion("code_impl_minimum_runs", False, 0, 2, "FAIL", "soft")
        decision, _ = decide_stage3_stability(criteria)
        assert decision == "rollback_code_impl_recommended"


# ===================================================================
# Part 7 — Artifact generation
# ===================================================================


class TestArtifactGeneration:
    """Verify stability review artifacts are correct and auditable."""

    def test_markdown_contains_decision(self, stage3_root):
        """Markdown report contains the decision."""
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")

        review = review_stage3_stability(stage3_root)
        md = render_stability_review_markdown(review)
        assert "stable_continue" in md
        assert "STABLE" in md

    def test_markdown_contains_code_impl_metrics(self, stage3_root):
        """Markdown report contains code_impl metrics table."""
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "failed", "code_impl")

        review = review_stage3_stability(stage3_root)
        md = render_stability_review_markdown(review)
        assert "code_impl Metrics" in md
        assert "| Completed | 1 |" in md
        assert "| Failed | 1 |" in md

    def test_markdown_contains_rollback_instructions(self, stage3_root):
        """Markdown report contains rollback instructions."""
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")

        review = review_stage3_stability(stage3_root)
        md = render_stability_review_markdown(review)
        assert "Rollback Instructions" in md
        assert "supported_classes" in md

    def test_write_creates_files(self, stage3_root):
        """write_stability_review creates md and json files."""
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")

        review = review_stage3_stability(stage3_root)
        md_path, json_path = write_stability_review(review, stage3_root)

        assert md_path.exists()
        assert json_path.exists()

        # JSON is valid and contains expected fields
        data = json.loads(json_path.read_text())
        assert data["decision"] == "stable_continue"
        assert "code_impl_metrics" in data
        assert "criteria" in data

    def test_json_contains_activation_record(self, stage3_root):
        """JSON output includes activation record for audit trail."""
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")

        review = review_stage3_stability(stage3_root)
        data = review.to_dict()
        assert "activation_record" in data
        assert data["activation_record"]["outcome"] == "activated"

    def test_rollback_markdown_mentions_remove_code_impl(self, stage3_root):
        """Rollback recommendation markdown mentions code_impl removal."""
        hb = {"overall": "unhealthy", "findings": []}
        (stage3_root / "STATE" / "heartbeat_multiagent.json").write_text(json.dumps(hb))
        _add_workflow(stage3_root, "ci1", "completed", "code_impl")
        _add_workflow(stage3_root, "ci2", "completed", "code_impl")

        review = review_stage3_stability(stage3_root)
        md = render_stability_review_markdown(review)
        assert "ROLLBACK" in md
        assert "rollback_code_impl_recommended" in md


# ===================================================================
# Part 8 — Activation record retrieval
# ===================================================================


class TestActivationRecord:
    """Verify activation record retrieval from audit log."""

    def test_reads_latest_activation(self, stage3_root):
        """Returns the most recent activated entry."""
        record = _get_latest_activation(stage3_root)
        assert record["outcome"] == "activated"
        assert record["decision"] == "ready_to_expand"

    def test_prefers_activated_over_blocked(self, stage3_root):
        """If multiple entries, returns last 'activated' one."""
        log = stage3_root / "STATE" / "activation_log.jsonl"
        blocked = {"attempted_at": "2026-03-08T09:00:00Z", "outcome": "blocked", "decision": "hold"}
        activated = {"attempted_at": "2026-03-08T10:00:00Z", "outcome": "activated", "decision": "ready_to_expand"}
        log.write_text(json.dumps(blocked) + "\n" + json.dumps(activated) + "\n")
        record = _get_latest_activation(stage3_root)
        assert record["outcome"] == "activated"
        assert record["attempted_at"] == "2026-03-08T10:00:00Z"

    def test_no_log_returns_empty(self, tmp_path):
        """No activation log → empty dict."""
        record = _get_latest_activation(tmp_path)
        assert record == {}


# ===================================================================
# Part 9 — Criteria evaluation integration
# ===================================================================


class TestCriteriaEvaluation:
    """Verify evaluate_stage3_stability produces correct criteria."""

    def test_returns_11_criteria(self):
        """Stage 3 review evaluates exactly 11 criteria."""
        evidence = {
            "heartbeat_overall": "healthy",
            "failure_rate": 0.0,
            "policy_violations": 0,
            "budget_exhaustions": 0,
            "verifier_rejection_rate": None,
            "contract_failure_rate": 0.0,
            "orphaned_agents": 0,
            "stale_leases": 0,
        }
        code_impl_metrics = {
            "total_runs": 3,
            "completed": 3,
            "failed": 0,
            "verifier_rejected": 0,
            "failure_rate": 0.0,
        }
        criteria = evaluate_stage3_stability(evidence, code_impl_metrics, 0)
        assert len(criteria) == 11

    def test_criteria_names(self):
        """All expected criteria names are present."""
        evidence = {
            "heartbeat_overall": "healthy",
            "failure_rate": 0.0,
            "policy_violations": 0,
            "budget_exhaustions": 0,
            "verifier_rejection_rate": None,
            "contract_failure_rate": 0.0,
            "orphaned_agents": 0,
            "stale_leases": 0,
        }
        code_impl_metrics = {
            "total_runs": 2,
            "completed": 2,
            "failed": 0,
            "verifier_rejected": 0,
            "failure_rate": 0.0,
        }
        criteria = evaluate_stage3_stability(evidence, code_impl_metrics, 0)
        names = {c.name for c in criteria}
        expected = {
            "heartbeat_healthy",
            "overall_failure_rate",
            "no_policy_violations",
            "no_budget_exhaustions",
            "code_impl_minimum_runs",
            "code_impl_failure_rate",
            "verifier_rejection_rate",
            "contract_failure_rate",
            "recovery_anomalies",
            "no_orphaned_agents",
            "no_stale_leases",
        }
        assert names == expected

    def test_hard_criteria_identified(self):
        """Hard criteria have severity='hard'."""
        evidence = {
            "heartbeat_overall": "healthy",
            "failure_rate": 0.0,
            "policy_violations": 0,
            "budget_exhaustions": 0,
            "verifier_rejection_rate": None,
            "contract_failure_rate": 0.0,
            "orphaned_agents": 0,
            "stale_leases": 0,
        }
        code_impl_metrics = {
            "total_runs": 2,
            "completed": 2,
            "failed": 0,
            "verifier_rejected": 0,
            "failure_rate": 0.0,
        }
        criteria = evaluate_stage3_stability(evidence, code_impl_metrics, 0)
        hard = {c.name for c in criteria if c.severity == "hard"}
        assert hard == {
            "heartbeat_healthy",
            "overall_failure_rate",
            "no_policy_violations",
            "no_budget_exhaustions",
        }
