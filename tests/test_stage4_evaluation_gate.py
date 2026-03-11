"""Phase 7.14 — Tests for Stage 4 evaluation gate (system class readiness).

Tests cover:
  1. ready_for_stage4_planning — all criteria pass with sufficient evidence
  2. hold_stage4 — insufficient evidence or soft concerns
  3. block_stage4 — hard failures (Stage 3 unstable, policy violations, etc.)
  4. Criteria evaluation correctness
  5. Decision logic
  6. Artifact generation
  7. Threshold tightening vs Stage 3
"""

import json
import time

import pytest

from agents.rollout_gate import (
    MAX_CONTRACT_FAILURE_RATE,
    STAGE3_MAX_CODE_IMPL_FAILURE_RATE,
    STAGE3_MAX_FAILURE_RATE,
    STAGE3_MAX_RECOVERY_ANOMALIES,
    STAGE3_MAX_VERIFIER_REJECTION_RATE,
    STAGE3_MIN_CODE_IMPL_RUNS,
    STAGE4_MAX_CODE_IMPL_FAILURE_RATE,
    STAGE4_MAX_CONTRACT_FAILURE_RATE,
    STAGE4_MAX_FAILURE_RATE,
    STAGE4_MAX_RECOVERY_ANOMALIES,
    STAGE4_MAX_VERIFIER_REJECTION_RATE,
    STAGE4_MIN_CODE_IMPL_RUNS,
    RolloutCriterion,
    decide_stage4_readiness,
    evaluate_stage4,
    render_stage4_evaluation_markdown,
    write_stage4_evaluation,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stage4_root(tmp_path):
    """Set up a minimal Stage 3 environment suitable for Stage 4 evaluation."""
    state = tmp_path / "STATE"
    (state / "config").mkdir(parents=True)
    (state / "workflows").mkdir(parents=True)
    (state / "verifications").mkdir(parents=True)
    (tmp_path / "LOGS").mkdir(parents=True)
    (tmp_path / "WORK").mkdir(parents=True)

    # Feature flags — Stage 3 active (system blocked)
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
        "contract_success": {"_total": 20},
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
    (state / "activation_log.jsonl").write_text(
        json.dumps(activation) + "\n"
    )

    return tmp_path


def _add_workflow(root, stem, status, task_class="research",
                  halt_reason=None):
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


def _populate_healthy_stage4(root):
    """Add enough workflows for a clean ready_for_stage4_planning result."""
    # 5 code_impl (meeting STAGE4_MIN_CODE_IMPL_RUNS)
    for i in range(5):
        _add_workflow(root, f"ci_{i}", "completed", "code_impl")
    # Additional workflows to meet STAGE4_MIN_TOTAL_COMPLETED
    _add_workflow(root, "res_1", "completed", "research")
    _add_workflow(root, "cr_1", "completed", "code_review")


# ===================================================================
# Part 1 — ready_for_stage4_planning outcome
# ===================================================================

class TestReadyForStage4Planning:
    """All criteria pass with sufficient evidence."""

    def test_healthy_system_is_ready(self, stage4_root):
        """Healthy environment with ample evidence → ready_for_stage4_planning."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "ready_for_stage4_planning"
        assert evaluation.code_impl_metrics["completed"] == 5
        assert evaluation.code_impl_metrics["failed"] == 0

    def test_ready_all_criteria_pass(self, stage4_root):
        """Every criterion passes in ready state."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        for c in evaluation.criteria:
            assert c.passed, f"Criterion {c.name} should pass: {c.detail}"

    def test_ready_system_remains_blocked(self, stage4_root):
        """Even when ready, system class is still in enabled_classes as blocked."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "ready_for_stage4_planning"
        assert "system" not in evaluation.enabled_classes

    def test_ready_next_action_mentions_activation_required(self, stage4_root):
        """Ready decision clarifies that activation is a separate step."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        assert "activate" in evaluation.next_action.lower() or \
               "activation" in evaluation.next_action.lower()

    def test_ready_no_remaining_requirements(self, stage4_root):
        """Ready state has empty remaining requirements."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.remaining_requirements == []

    def test_ready_stage3_decision_is_stable(self, stage4_root):
        """Ready state embeds Stage 3 stability decision."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.stage3_stability_decision == "stable_continue"


# ===================================================================
# Part 2 — hold_stage4 outcome
# ===================================================================

class TestHoldStage4:
    """Insufficient evidence or soft concerns → hold."""

    def test_insufficient_code_impl_runs(self, stage4_root):
        """Too few code_impl runs → hold_stage4."""
        # Only 2 code_impl (need 5)
        _add_workflow(stage4_root, "ci1", "completed", "code_impl")
        _add_workflow(stage4_root, "ci2", "completed", "code_impl")
        _add_workflow(stage4_root, "r1", "completed", "research")
        _add_workflow(stage4_root, "r2", "completed", "research")
        _add_workflow(stage4_root, "r3", "completed", "research")
        _add_workflow(stage4_root, "r4", "completed", "research")

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "hold_stage4"

    def test_insufficient_total_completed(self, stage4_root):
        """Not enough total completed workflows → hold_stage4."""
        # 5 code_impl but only 5 total (need 6)
        for i in range(5):
            _add_workflow(stage4_root, f"ci_{i}", "completed", "code_impl")

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "hold_stage4"

    def test_orphaned_agents_cascades_to_block(self, stage4_root):
        """Orphaned agents fail Stage 3 stability → block_stage4 (cascading)."""
        _populate_healthy_stage4(stage4_root)

        # Add an orphaned agent — this fails Stage 3 stability (soft)
        # which causes Stage 4's stage3_stable hard criterion to fail
        runtime_dir = stage4_root / "STATE" / "agents" / "runtime"
        runtime_dir.mkdir(parents=True)
        orphan = {
            "agent_id": "stuck_agent",
            "status": "executing",
            "started_at": time.time() - 3600,  # 1 hour ago
        }
        (runtime_dir / "stuck_agent.json").write_text(json.dumps(orphan))

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "block_stage4"

    def test_recovery_anomalies_causes_hold(self, stage4_root):
        """Too many recovery events → hold_stage4."""
        _populate_healthy_stage4(stage4_root)

        # Add recovery events after activation
        recovery_log = stage4_root / "LOGS" / "recovery.log"
        recovery_log.write_text(
            "--- Recovery at 2026-03-08T09:00:00Z ---\n"
            "requeued 2 workflows\n"
            "--- Recovery at 2026-03-08T09:30:00Z ---\n"
            "requeued 1 workflow\n"
        )

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "hold_stage4"

    def test_high_code_impl_failure_rate_causes_hold(self, stage4_root):
        """High code_impl failure rate → hold_stage4.

        Uses a rate between Stage 3 (50%) and Stage 4 (30%) thresholds
        while keeping overall failure rate below Stage 4's 20% threshold.
        """
        # 3 completed + 2 failed code_impl = 40% code_impl failure (>30%, <50%)
        for i in range(3):
            _add_workflow(stage4_root, f"ci_ok_{i}", "completed", "code_impl")
        for i in range(2):
            _add_workflow(stage4_root, f"ci_fail_{i}", "failed", "code_impl")
        # Add enough completed to keep overall failure rate < 20%
        # 2 failed / 15 total = 13.3%
        for i in range(10):
            _add_workflow(stage4_root, f"res_{i}", "completed", "research")

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "hold_stage4"

    def test_rollback_history_causes_hold(self, stage4_root):
        """Rollback events in activation history → hold_stage4."""
        _populate_healthy_stage4(stage4_root)

        # Add a rollback event to activation log
        rollback_record = {
            "attempted_at": "2026-03-08T07:00:00Z",
            "outcome": "rollback",
            "decision": "rollback_recommended",
            "reason": "test rollback",
        }
        activation_log = stage4_root / "STATE" / "activation_log.jsonl"
        with activation_log.open("a") as f:
            f.write(json.dumps(rollback_record) + "\n")

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "hold_stage4"

    def test_hold_has_remaining_requirements(self, stage4_root):
        """Hold state lists remaining requirements."""
        # Enough for Stage 3 stability (2 code_impl) but not Stage 4 (5)
        _add_workflow(stage4_root, "ci1", "completed", "code_impl")
        _add_workflow(stage4_root, "ci2", "completed", "code_impl")
        _add_workflow(stage4_root, "r1", "completed", "research")
        _add_workflow(stage4_root, "r2", "completed", "research")
        _add_workflow(stage4_root, "r3", "completed", "research")
        _add_workflow(stage4_root, "r4", "completed", "research")

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "hold_stage4"
        assert len(evaluation.remaining_requirements) > 0

    def test_high_verifier_rejection_causes_hold(self, stage4_root):
        """High verifier rejection rate → hold_stage4."""
        _populate_healthy_stage4(stage4_root)

        # Add verifier reports with high rejection rate
        ver_dir = stage4_root / "STATE" / "verifications"
        for i in range(4):
            verdict = "rejected" if i < 2 else "approved"
            (ver_dir / f"ver_{i}.json").write_text(
                json.dumps({"verdict": verdict})
            )
        # 2/4 = 50% > 30%

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "hold_stage4"

    def test_high_contract_failure_causes_hold(self, stage4_root):
        """High contract failure rate → hold_stage4."""
        _populate_healthy_stage4(stage4_root)

        # Update metrics with high contract failure rate
        metrics = {
            "contract_success": {"_total": 3},
            "contract_failure": {"_total": 1},  # 25% > 20%
        }
        (stage4_root / "STATE" / "metrics.json").write_text(
            json.dumps(metrics)
        )

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "hold_stage4"


# ===================================================================
# Part 3 — block_stage4 outcome
# ===================================================================

class TestBlockStage4:
    """Hard failures → block."""

    def test_unhealthy_heartbeat_blocks(self, stage4_root):
        """Unhealthy heartbeat → block_stage4."""
        _populate_healthy_stage4(stage4_root)

        hb = {"overall": "unhealthy", "findings": [
            {"severity": "unhealthy", "message": "test failure"}
        ]}
        (stage4_root / "STATE" / "heartbeat_multiagent.json").write_text(
            json.dumps(hb)
        )

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "block_stage4"

    def test_policy_violations_block(self, stage4_root):
        """Policy violations → block_stage4."""
        _populate_healthy_stage4(stage4_root)

        denials = stage4_root / "STATE" / "policy_denials.jsonl"
        denials.write_text(json.dumps({"type": "denied", "reason": "test"}) + "\n")

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "block_stage4"

    def test_budget_exhaustion_blocks(self, stage4_root):
        """Budget exhaustion → block_stage4."""
        _populate_healthy_stage4(stage4_root)

        _add_workflow(stage4_root, "halted_1", "halted", "code_impl",
                      halt_reason="budget exceeded")

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "block_stage4"

    def test_stage3_not_stable_blocks(self, stage4_root):
        """Stage 3 not stable_continue → block_stage4."""
        # No code_impl runs → Stage 3 will be hold_stage3 → hard failure
        _add_workflow(stage4_root, "r1", "completed", "research")
        _add_workflow(stage4_root, "r2", "completed", "research")

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "block_stage4"
        assert evaluation.stage3_stability_decision != "stable_continue"

    def test_system_already_enabled_blocks(self, stage4_root):
        """system already in supported_classes → block_stage4."""
        _populate_healthy_stage4(stage4_root)

        # Modify flags to include system
        flags_path = stage4_root / "STATE" / "config" / "feature_flags.json"
        flags = json.loads(flags_path.read_text())
        flags["phase7_orchestrator"]["supported_classes"].append("system")
        flags_path.write_text(json.dumps(flags))

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "block_stage4"

    def test_high_overall_failure_rate_blocks(self, stage4_root):
        """High overall failure rate → block_stage4."""
        # 5 code_impl completed, but add many failed workflows
        for i in range(5):
            _add_workflow(stage4_root, f"ci_{i}", "completed", "code_impl")
        # 3 failed out of 8 total = 37.5% > 20%
        for i in range(3):
            _add_workflow(stage4_root, f"fail_{i}", "failed", "research")

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "block_stage4"

    def test_block_next_action_mentions_system_blocked(self, stage4_root):
        """Block decision reminds that system must stay blocked."""
        hb = {"overall": "unhealthy", "findings": [
            {"severity": "unhealthy", "message": "test"}
        ]}
        (stage4_root / "STATE" / "heartbeat_multiagent.json").write_text(
            json.dumps(hb)
        )

        evaluation = evaluate_stage4(stage4_root)
        assert "blocked" in evaluation.next_action.lower() or \
               "system" in evaluation.next_action.lower()

    def test_hard_overrides_soft(self, stage4_root):
        """Hard failure overrides soft concerns → block, not hold."""
        # Few code_impl (soft) + policy violation (hard)
        _add_workflow(stage4_root, "ci1", "completed", "code_impl")

        denials = stage4_root / "STATE" / "policy_denials.jsonl"
        denials.write_text(json.dumps({"type": "denied"}) + "\n")

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "block_stage4"


# ===================================================================
# Part 4 — Criteria evaluation
# ===================================================================

class TestCriteriaEvaluation:
    """Test individual criteria evaluation."""

    def test_criteria_count(self, stage4_root):
        """Stage 4 evaluation has 15 criteria."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        assert len(evaluation.criteria) == 15

    def test_expected_criteria_names(self, stage4_root):
        """All expected criteria are present."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        names = {c.name for c in evaluation.criteria}
        expected = {
            "stage3_stable",
            "heartbeat_healthy",
            "overall_failure_rate",
            "no_policy_violations",
            "no_budget_exhaustions",
            "system_class_blocked",
            "minimum_total_completed",
            "code_impl_minimum_runs",
            "code_impl_failure_rate",
            "verifier_rejection_rate",
            "contract_failure_rate",
            "recovery_anomalies",
            "no_orphaned_agents",
            "no_stale_leases",
            "no_rollback_history",
        }
        assert names == expected

    def test_hard_criteria_identified(self, stage4_root):
        """Hard criteria are correctly classified."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        hard = {c.name for c in evaluation.criteria if c.severity == "hard"}
        expected_hard = {
            "stage3_stable",
            "heartbeat_healthy",
            "overall_failure_rate",
            "no_policy_violations",
            "no_budget_exhaustions",
            "system_class_blocked",
        }
        assert hard == expected_hard

    def test_soft_criteria_identified(self, stage4_root):
        """Soft criteria are correctly classified."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        soft = {c.name for c in evaluation.criteria if c.severity == "soft"}
        expected_soft = {
            "minimum_total_completed",
            "code_impl_minimum_runs",
            "code_impl_failure_rate",
            "verifier_rejection_rate",
            "contract_failure_rate",
            "recovery_anomalies",
            "no_orphaned_agents",
            "no_stale_leases",
            "no_rollback_history",
        }
        assert soft == expected_soft


# ===================================================================
# Part 5 — Decision logic
# ===================================================================

class TestDecisionLogic:
    """Test the decide_stage4_readiness function directly."""

    def _make_criteria(self, overrides=None):
        """Build a set of 15 all-pass criteria, with optional overrides."""
        defaults = {
            "stage3_stable": ("hard", True),
            "heartbeat_healthy": ("hard", True),
            "overall_failure_rate": ("hard", True),
            "no_policy_violations": ("hard", True),
            "no_budget_exhaustions": ("hard", True),
            "system_class_blocked": ("hard", True),
            "minimum_total_completed": ("soft", True),
            "code_impl_minimum_runs": ("soft", True),
            "code_impl_failure_rate": ("soft", True),
            "verifier_rejection_rate": ("soft", True),
            "contract_failure_rate": ("soft", True),
            "recovery_anomalies": ("soft", True),
            "no_orphaned_agents": ("soft", True),
            "no_stale_leases": ("soft", True),
            "no_rollback_history": ("soft", True),
        }
        if overrides:
            for name, passed in overrides.items():
                sev = defaults[name][0]
                defaults[name] = (sev, passed)

        return [
            RolloutCriterion(
                name=name, passed=passed, value="test",
                threshold="test", detail="test", severity=severity,
            )
            for name, (severity, passed) in defaults.items()
        ]

    def test_all_pass_is_ready(self):
        """All criteria pass → ready_for_stage4_planning."""
        criteria = self._make_criteria()
        decision, _ = decide_stage4_readiness(criteria)
        assert decision == "ready_for_stage4_planning"

    def test_hard_failure_is_block(self):
        """Hard failure → block_stage4."""
        criteria = self._make_criteria({"heartbeat_healthy": False})
        decision, _ = decide_stage4_readiness(criteria)
        assert decision == "block_stage4"

    def test_insufficient_evidence_is_hold(self):
        """Insufficient evidence (code_impl_minimum_runs) → hold_stage4."""
        criteria = self._make_criteria({"code_impl_minimum_runs": False})
        decision, _ = decide_stage4_readiness(criteria)
        assert decision == "hold_stage4"

    def test_soft_failure_is_hold(self):
        """Generic soft failure → hold_stage4."""
        criteria = self._make_criteria({"no_orphaned_agents": False})
        decision, _ = decide_stage4_readiness(criteria)
        assert decision == "hold_stage4"

    def test_hard_overrides_soft(self):
        """Hard failure overrides soft concerns → block, not hold."""
        criteria = self._make_criteria({
            "stage3_stable": False,
            "code_impl_minimum_runs": False,
        })
        decision, _ = decide_stage4_readiness(criteria)
        assert decision == "block_stage4"

    def test_multiple_hard_failures_in_block_reason(self):
        """Multiple hard failures listed in next_action."""
        criteria = self._make_criteria({
            "heartbeat_healthy": False,
            "no_policy_violations": False,
        })
        _, next_action = decide_stage4_readiness(criteria)
        assert "heartbeat_healthy" in next_action
        assert "no_policy_violations" in next_action


# ===================================================================
# Part 6 — Artifact generation
# ===================================================================

class TestArtifactGeneration:
    """Test markdown rendering and file writing."""

    def test_markdown_contains_decision(self, stage4_root):
        """Markdown artifact includes the decision."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        md = render_stage4_evaluation_markdown(evaluation)
        assert "ready_for_stage4_planning" in md

    def test_markdown_contains_criteria_table(self, stage4_root):
        """Markdown artifact includes criteria table."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        md = render_stage4_evaluation_markdown(evaluation)
        assert "stage3_stable" in md
        assert "system_class_blocked" in md
        assert "PASS" in md

    def test_markdown_contains_code_impl_metrics(self, stage4_root):
        """Markdown artifact includes code_impl metrics section."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        md = render_stage4_evaluation_markdown(evaluation)
        assert "code_impl Metrics" in md
        assert "Total runs" in md

    def test_markdown_contains_threshold_comparison(self, stage4_root):
        """Markdown includes Stage 3 vs Stage 4 threshold comparison."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        md = render_stage4_evaluation_markdown(evaluation)
        assert "Stage 3 Threshold" in md
        assert "Stage 4 Threshold" in md

    def test_markdown_states_system_blocked(self, stage4_root):
        """Markdown explicitly states system remains blocked."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        md = render_stage4_evaluation_markdown(evaluation)
        assert "system remains BLOCKED" in md

    def test_markdown_block_shows_failures(self, stage4_root):
        """Block decision markdown shows failing criteria."""
        # Trigger block via unhealthy heartbeat
        hb = {"overall": "unhealthy", "findings": [
            {"severity": "unhealthy", "message": "test"}
        ]}
        (stage4_root / "STATE" / "heartbeat_multiagent.json").write_text(
            json.dumps(hb)
        )

        evaluation = evaluate_stage4(stage4_root)
        md = render_stage4_evaluation_markdown(evaluation)
        assert "BLOCKED" in md
        assert "FAIL" in md

    def test_markdown_hold_shows_remaining(self, stage4_root):
        """Hold decision markdown shows remaining requirements."""
        _add_workflow(stage4_root, "ci1", "completed", "code_impl")
        _add_workflow(stage4_root, "ci2", "completed", "code_impl")

        evaluation = evaluate_stage4(stage4_root)
        md = render_stage4_evaluation_markdown(evaluation)
        assert "Remaining Requirements" in md

    def test_write_creates_files(self, stage4_root):
        """write_stage4_evaluation creates md and json files."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        md_path, json_path = write_stage4_evaluation(evaluation, stage4_root)

        assert md_path.exists()
        assert json_path.exists()
        assert md_path.name == "phase7_stage4_evaluation_gate.md"
        assert json_path.name == "stage4_evaluation.json"

    def test_written_json_is_valid(self, stage4_root):
        """Written JSON artifact is valid and contains decision."""
        _populate_healthy_stage4(stage4_root)

        evaluation = evaluate_stage4(stage4_root)
        _, json_path = write_stage4_evaluation(evaluation, stage4_root)

        data = json.loads(json_path.read_text())
        assert data["decision"] == "ready_for_stage4_planning"
        assert "criteria" in data
        assert "code_impl_metrics" in data
        assert "stage3_stability_decision" in data

    def test_written_json_contains_remaining(self, stage4_root):
        """Written JSON includes remaining_requirements field."""
        _add_workflow(stage4_root, "ci1", "completed", "code_impl")

        evaluation = evaluate_stage4(stage4_root)
        _, json_path = write_stage4_evaluation(evaluation, stage4_root)

        data = json.loads(json_path.read_text())
        assert "remaining_requirements" in data
        assert len(data["remaining_requirements"]) > 0


# ===================================================================
# Part 7 — Threshold tightening
# ===================================================================

class TestThresholdTightening:
    """Verify Stage 4 thresholds are tighter than Stage 3."""

    def test_code_impl_min_runs_tighter(self):
        """Stage 4 requires more code_impl runs than Stage 3."""
        assert STAGE4_MIN_CODE_IMPL_RUNS > STAGE3_MIN_CODE_IMPL_RUNS

    def test_code_impl_failure_rate_tighter(self):
        """Stage 4 has lower code_impl failure rate threshold."""
        assert STAGE4_MAX_CODE_IMPL_FAILURE_RATE < STAGE3_MAX_CODE_IMPL_FAILURE_RATE

    def test_overall_failure_rate_tighter(self):
        """Stage 4 has lower overall failure rate threshold."""
        assert STAGE4_MAX_FAILURE_RATE < STAGE3_MAX_FAILURE_RATE

    def test_verifier_rejection_rate_tighter(self):
        """Stage 4 has lower verifier rejection rate threshold."""
        assert STAGE4_MAX_VERIFIER_REJECTION_RATE < STAGE3_MAX_VERIFIER_REJECTION_RATE

    def test_contract_failure_rate_tighter(self):
        """Stage 4 has lower contract failure rate threshold."""
        assert STAGE4_MAX_CONTRACT_FAILURE_RATE < MAX_CONTRACT_FAILURE_RATE

    def test_recovery_anomalies_tighter(self):
        """Stage 4 allows fewer recovery anomalies."""
        assert STAGE4_MAX_RECOVERY_ANOMALIES < STAGE3_MAX_RECOVERY_ANOMALIES


# ===================================================================
# Part 8 — Edge cases
# ===================================================================

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_no_workflows_at_all(self, stage4_root):
        """Zero workflows → hold (insufficient evidence)."""
        evaluation = evaluate_stage4(stage4_root)
        # Stage 3 stability will be hold_stage3 (no code_impl) → hard failure
        assert evaluation.decision == "block_stage4"

    def test_exactly_at_threshold(self, stage4_root):
        """Exactly at threshold values → pass."""
        # Exactly 5 code_impl, exactly 6 total
        for i in range(5):
            _add_workflow(stage4_root, f"ci_{i}", "completed", "code_impl")
        _add_workflow(stage4_root, "r1", "completed", "research")

        evaluation = evaluate_stage4(stage4_root)
        assert evaluation.decision == "ready_for_stage4_planning"

    def test_stale_leases_cascades_to_block(self, stage4_root):
        """Stale leases fail Stage 3 stability → block_stage4 (cascading)."""
        _populate_healthy_stage4(stage4_root)

        leases_dir = stage4_root / "STATE" / "leases"
        leases_dir.mkdir(parents=True)
        (leases_dir / "stale.json").write_text(json.dumps({
            "expires_at": time.time() - 3600,
        }))

        evaluation = evaluate_stage4(stage4_root)
        # Stale leases fail Stage 3 stability (soft) → stage3_stable hard fail
        assert evaluation.decision == "block_stage4"

    def test_evaluation_does_not_modify_state(self, stage4_root):
        """Evaluation is read-only — no state modified."""
        _populate_healthy_stage4(stage4_root)

        flags_before = (stage4_root / "STATE" / "config" / "feature_flags.json").read_text()
        evaluate_stage4(stage4_root)
        flags_after = (stage4_root / "STATE" / "config" / "feature_flags.json").read_text()

        assert flags_before == flags_after

    def test_system_not_in_supported_classes_after_eval(self, stage4_root):
        """Evaluation does not add system to supported_classes."""
        _populate_healthy_stage4(stage4_root)

        evaluate_stage4(stage4_root)

        flags = json.loads(
            (stage4_root / "STATE" / "config" / "feature_flags.json").read_text()
        )
        assert "system" not in flags["phase7_orchestrator"]["supported_classes"]
