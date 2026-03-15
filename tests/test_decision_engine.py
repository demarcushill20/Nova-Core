"""Tests for the skill optimizer decision engine.

Tests all hard gates, risk-level overrides, batch halt conditions,
and edge cases.
"""

from tools.skill_optimizer.decision_engine import (
    ACCEPT,
    REJECT_BROADENING,
    REJECT_FPR,
    REJECT_GAIN,
    REJECT_LENGTH,
    REJECT_NEIGHBOR,
    REJECT_POLICY,
    REJECT_PRECISION,
    REJECT_SMOKE,
    BatchHaltMonitor,
    Decision,
    evaluate_gates,
)

# Standard test thresholds (matching configs/thresholds.yaml defaults)
TEST_THRESHOLDS = {
    "scoring": {
        "weights": {
            "f1": 0.30,
            "precision": 0.20,
            "recall": 0.15,
            "specificity": 0.15,
            "paraphrase_consistency": 0.10,
            "neighbor_conflict_rate": 0.10,
        }
    },
    "hard_gates": {
        "precision_floor": {"low": 0.70, "medium": 0.80, "high": 0.90, "critical": 1.00},
        "fpr_regression_limit": 0.05,
        "neighbor_conflict_limit": 0.10,
        "smoke_regression_tolerance": 0,
        "min_score_improvement": 0.02,
        "max_description_length": 1024,
    },
    "risk_overrides": {
        "high": {
            "min_score_improvement": 0.05,
            "fpr_regression_limit": 0.02,
            "neighbor_conflict_limit": 0.05,
        }
    },
    "anti_broadening": {
        "trigger_expansion_limit": 0.20,
        "hard_negative_regression_tolerance": 0,
    },
    "batch_halt": {
        "max_consecutive_failures": 5,
        "min_acceptance_rate": 0.10,
    },
}

# Good baseline and candidate metrics
BASELINE = {"weighted_score": 0.80, "precision": 0.90, "recall": 0.85, "fpr": 0.10, "f1": 0.87}
GOOD_CANDIDATE = {"weighted_score": 0.85, "precision": 0.92, "recall": 0.88, "fpr": 0.08, "f1": 0.90}


class TestAcceptHappyPath:
    """Test that a good candidate passes all gates."""

    def test_accept_low_risk(self):
        d = evaluate_gates(
            skill_name="web-research",
            risk_level="low",
            baseline_metrics=BASELINE,
            candidate_metrics=GOOD_CANDIDATE,
            candidate_description="Fast web research with citations",
            thresholds=TEST_THRESHOLDS,
        )
        assert d.is_accepted
        assert d.reason_code == ACCEPT
        assert d.delta > 0

    def test_accept_medium_risk(self):
        d = evaluate_gates(
            skill_name="browser-automation",
            risk_level="medium",
            baseline_metrics=BASELINE,
            candidate_metrics=GOOD_CANDIDATE,
            candidate_description="Playwright browser automation for web tasks",
            thresholds=TEST_THRESHOLDS,
        )
        assert d.is_accepted


class TestPrecisionFloor:
    """Test Gate 3: precision floor by risk level."""

    def test_reject_low_risk_below_floor(self):
        candidate = {**GOOD_CANDIDATE, "precision": 0.65}
        d = evaluate_gates(
            skill_name="test",
            risk_level="low",
            baseline_metrics=BASELINE,
            candidate_metrics=candidate,
            candidate_description="test desc",
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code == REJECT_PRECISION

    def test_reject_medium_risk_below_floor(self):
        candidate = {**GOOD_CANDIDATE, "precision": 0.75}
        d = evaluate_gates(
            skill_name="test",
            risk_level="medium",
            baseline_metrics=BASELINE,
            candidate_metrics=candidate,
            candidate_description="test desc",
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code == REJECT_PRECISION

    def test_reject_high_risk_below_floor(self):
        candidate = {**GOOD_CANDIDATE, "precision": 0.85}
        d = evaluate_gates(
            skill_name="test",
            risk_level="high",
            baseline_metrics=BASELINE,
            candidate_metrics=candidate,
            candidate_description="test desc",
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code == REJECT_PRECISION

    def test_pass_at_floor(self):
        candidate = {**GOOD_CANDIDATE, "precision": 0.80}
        d = evaluate_gates(
            skill_name="test",
            risk_level="medium",
            baseline_metrics=BASELINE,
            candidate_metrics=candidate,
            candidate_description="test desc",
            thresholds=TEST_THRESHOLDS,
        )
        # Should pass precision gate (might fail others)
        assert d.reason_code != REJECT_PRECISION


class TestFPRRegression:
    """Test Gate 4: FPR regression limit."""

    def test_reject_fpr_increase(self):
        baseline = {**BASELINE, "fpr": 0.05}
        candidate = {**GOOD_CANDIDATE, "fpr": 0.15}  # +0.10 > 0.05 limit
        d = evaluate_gates(
            skill_name="test",
            risk_level="medium",
            baseline_metrics=baseline,
            candidate_metrics=candidate,
            candidate_description="test desc",
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code == REJECT_FPR

    def test_pass_small_fpr_increase(self):
        baseline = {**BASELINE, "fpr": 0.05}
        candidate = {**GOOD_CANDIDATE, "fpr": 0.08}  # +0.03 < 0.05 limit
        d = evaluate_gates(
            skill_name="test",
            risk_level="medium",
            baseline_metrics=baseline,
            candidate_metrics=candidate,
            candidate_description="test desc",
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code != REJECT_FPR

    def test_high_risk_stricter_fpr(self):
        baseline = {**BASELINE, "fpr": 0.05}
        candidate = {**GOOD_CANDIDATE, "precision": 0.95, "fpr": 0.08}  # +0.03 > 0.02 high-risk limit
        d = evaluate_gates(
            skill_name="test",
            risk_level="high",
            baseline_metrics=baseline,
            candidate_metrics=candidate,
            candidate_description="test desc",
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code == REJECT_FPR


class TestNeighborConflict:
    """Test Gate 5: neighbor conflict rate."""

    def test_reject_high_conflict(self):
        d = evaluate_gates(
            skill_name="test",
            risk_level="medium",
            baseline_metrics=BASELINE,
            candidate_metrics=GOOD_CANDIDATE,
            candidate_description="test desc",
            neighbor_conflict_rate=0.15,  # > 0.10 limit
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code == REJECT_NEIGHBOR

    def test_pass_low_conflict(self):
        d = evaluate_gates(
            skill_name="test",
            risk_level="medium",
            baseline_metrics=BASELINE,
            candidate_metrics=GOOD_CANDIDATE,
            candidate_description="test desc",
            neighbor_conflict_rate=0.05,  # < 0.10 limit
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code != REJECT_NEIGHBOR


class TestSmokeRegression:
    """Test Gate 6: global smoke zero tolerance."""

    def test_reject_any_smoke_regression(self):
        d = evaluate_gates(
            skill_name="test",
            risk_level="low",
            baseline_metrics=BASELINE,
            candidate_metrics=GOOD_CANDIDATE,
            candidate_description="test desc",
            smoke_regressions=1,
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code == REJECT_SMOKE

    def test_pass_no_smoke_regression(self):
        d = evaluate_gates(
            skill_name="test",
            risk_level="low",
            baseline_metrics=BASELINE,
            candidate_metrics=GOOD_CANDIDATE,
            candidate_description="test desc",
            smoke_regressions=0,
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code != REJECT_SMOKE


class TestAntiBroadening:
    """Test Gate 7: anti-broadening guard."""

    def test_reject_hard_negative_regression(self):
        d = evaluate_gates(
            skill_name="test",
            risk_level="medium",
            baseline_metrics=BASELINE,
            candidate_metrics=GOOD_CANDIDATE,
            candidate_description="test desc",
            hard_negative_regression=True,
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code == REJECT_BROADENING


class TestMinimumGain:
    """Test Gate 8: minimum score improvement."""

    def test_reject_insufficient_gain(self):
        candidate = {**BASELINE, "weighted_score": BASELINE["weighted_score"] + 0.01}
        d = evaluate_gates(
            skill_name="test",
            risk_level="medium",
            baseline_metrics=BASELINE,
            candidate_metrics=candidate,
            candidate_description="test desc",
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code == REJECT_GAIN

    def test_reject_regression(self):
        candidate = {**BASELINE, "weighted_score": BASELINE["weighted_score"] - 0.05}
        d = evaluate_gates(
            skill_name="test",
            risk_level="medium",
            baseline_metrics=BASELINE,
            candidate_metrics=candidate,
            candidate_description="test desc",
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code == REJECT_GAIN

    def test_high_risk_needs_more_gain(self):
        candidate = {**GOOD_CANDIDATE, "precision": 0.95, "weighted_score": BASELINE["weighted_score"] + 0.03}
        baseline_low_fpr = {**BASELINE, "fpr": 0.05}
        candidate_low_fpr = {**candidate, "fpr": 0.05}
        d = evaluate_gates(
            skill_name="test",
            risk_level="high",
            baseline_metrics=baseline_low_fpr,
            candidate_metrics=candidate_low_fpr,
            candidate_description="test desc",
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code == REJECT_GAIN  # +0.03 < 0.05 high-risk minimum


class TestPolicyViolation:
    """Test Gate 1: policy violations."""

    def test_reject_policy_violation(self):
        d = evaluate_gates(
            skill_name="test",
            risk_level="medium",
            baseline_metrics=BASELINE,
            candidate_metrics=GOOD_CANDIDATE,
            candidate_description="test desc",
            policy_violations=["disallowed_pattern: 'any task'"],
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code == REJECT_POLICY


class TestDescriptionLength:
    """Test Gate 2: description length."""

    def test_reject_too_long(self):
        d = evaluate_gates(
            skill_name="test",
            risk_level="medium",
            baseline_metrics=BASELINE,
            candidate_metrics=GOOD_CANDIDATE,
            candidate_description="x" * 1025,
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code == REJECT_LENGTH


class TestGateOrdering:
    """Test that gates short-circuit in correct order."""

    def test_policy_checked_before_precision(self):
        """Policy violation should reject before precision check."""
        low_precision = {**GOOD_CANDIDATE, "precision": 0.50}
        d = evaluate_gates(
            skill_name="test",
            risk_level="medium",
            baseline_metrics=BASELINE,
            candidate_metrics=low_precision,
            candidate_description="test desc",
            policy_violations=["bad pattern"],
            thresholds=TEST_THRESHOLDS,
        )
        assert d.reason_code == REJECT_POLICY  # not REJECT_PRECISION


class TestBatchHaltMonitor:
    """Test batch-level halt conditions."""

    def test_halt_on_consecutive_failures(self):
        monitor = BatchHaltMonitor(TEST_THRESHOLDS)
        # Start with an accept so acceptance rate stays above threshold
        d_ok = Decision("test", ACCEPT, "ok", ACCEPT)
        monitor.record_decision(d_ok)
        for _ in range(5):
            d = Decision("test", "REJECT_GAIN", "fail", REJECT_GAIN)
            monitor.record_decision(d)
        assert monitor.is_halted
        assert "consecutive" in monitor.halt_reason.lower()

    def test_no_halt_with_mixed_results(self):
        monitor = BatchHaltMonitor(TEST_THRESHOLDS)
        for i in range(10):
            if i % 3 == 0:
                d = Decision("test", ACCEPT, "ok", ACCEPT)
            else:
                d = Decision("test", "REJECT_GAIN", "fail", REJECT_GAIN)
            monitor.record_decision(d)
        assert not monitor.is_halted

    def test_no_halt_at_exact_threshold(self):
        monitor = BatchHaltMonitor(TEST_THRESHOLDS)
        # Interleave accepts to avoid consecutive failure halt
        # Pattern: A R R R R A R R R R = 2/10 = 20% > 10% threshold
        for i in range(10):
            if i % 5 == 0:
                d = Decision("test", ACCEPT, "ok", ACCEPT)
            else:
                d = Decision("test", "REJECT_GAIN", "fail", REJECT_GAIN)
            monitor.record_decision(d)
        assert not monitor.is_halted

    def test_halt_below_acceptance_rate(self):
        monitor = BatchHaltMonitor(TEST_THRESHOLDS)
        # 0 accepts, 5 rejects
        for _ in range(5):
            d_fail = Decision("test", "REJECT_GAIN", "fail", REJECT_GAIN)
            monitor.record_decision(d_fail)
        # 0/5 = 0.0 < 0.10, should halt (also consecutive failures)
        assert monitor.is_halted

    def test_accept_resets_consecutive(self):
        monitor = BatchHaltMonitor(TEST_THRESHOLDS)
        for _ in range(4):
            d = Decision("test", "REJECT_GAIN", "fail", REJECT_GAIN)
            monitor.record_decision(d)
        # Not halted yet (4 < 5)
        assert not monitor.is_halted
        # Accept resets counter
        d_ok = Decision("test", ACCEPT, "ok", ACCEPT)
        monitor.record_decision(d_ok)
        assert monitor._consecutive_failures == 0
        # 4 more failures
        for _ in range(4):
            d = Decision("test", "REJECT_GAIN", "fail", REJECT_GAIN)
            monitor.record_decision(d)
        # Still not halted (4 < 5 consecutive)
        assert not monitor.is_halted


class TestDecisionSerializable:
    """Test that decisions serialize cleanly."""

    def test_to_dict(self):
        d = evaluate_gates(
            skill_name="web-research",
            risk_level="medium",
            baseline_metrics=BASELINE,
            candidate_metrics=GOOD_CANDIDATE,
            candidate_description="test desc",
            thresholds=TEST_THRESHOLDS,
        )
        result = d.to_dict()
        assert isinstance(result, dict)
        assert "skill_name" in result
        assert "decision" in result
        assert "gate_details" in result
