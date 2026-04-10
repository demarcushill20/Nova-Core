"""Tests for novatrade.validation.decision — MVP go/no-go verdict."""

from __future__ import annotations

from novatrade.models import MvpDecision, MvpVerdict, ValidationReport
from novatrade.validation.decision import (
    HEALTH_FAILURE_BLOCK_RATIO,
    HEALTH_FAILURE_WARN_RATIO,
    MAX_ADAPTER_ERRORS,
    MAX_EXCEPTIONS,
    MAX_MISMATCHES,
    MIN_EXECUTIONS,
    MIN_RECORDS,
    evaluate_mvp,
    format_decision,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _report(**overrides) -> ValidationReport:
    """Build a ValidationReport with sensible defaults for testing."""
    defaults = {
        "total_records": 10,
        "execution_count": 5,
        "filled_count": 3,
        "denied_count": 2,
        "adapter_error_count": 0,
        "exception_count": 0,
        "health_snapshots": 3,
        "health_ok_count": 3,
        "reconciliation_count": 2,
        "reconciliation_ok_count": 2,
        "mismatch_count": 0,
    }
    defaults.update(overrides)
    return ValidationReport(**defaults)  # type: ignore[arg-type]


# ===========================================================================
# INSUFFICIENT_DATA
# ===========================================================================


class TestInsufficientData:
    def test_empty_report(self):
        decision = evaluate_mvp(ValidationReport())
        assert decision.verdict == MvpVerdict.INSUFFICIENT_DATA
        assert len(decision.reasons) > 0

    def test_too_few_records(self):
        decision = evaluate_mvp(_report(total_records=MIN_RECORDS - 1))
        assert decision.verdict == MvpVerdict.INSUFFICIENT_DATA
        assert "record" in decision.reasons[0].lower()

    def test_no_executions(self):
        decision = evaluate_mvp(_report(execution_count=0))
        assert decision.verdict == MvpVerdict.INSUFFICIENT_DATA
        assert "execution" in decision.reasons[0].lower()

    def test_exactly_min_records_passes(self):
        decision = evaluate_mvp(_report(total_records=MIN_RECORDS))
        assert decision.verdict != MvpVerdict.INSUFFICIENT_DATA

    def test_exactly_min_executions_passes(self):
        decision = evaluate_mvp(_report(execution_count=MIN_EXECUTIONS))
        assert decision.verdict != MvpVerdict.INSUFFICIENT_DATA


# ===========================================================================
# READY
# ===========================================================================


class TestReady:
    def test_clean_report(self):
        decision = evaluate_mvp(_report())
        assert decision.verdict == MvpVerdict.READY
        assert len(decision.blockers) == 0
        assert "stable" in decision.reasons[-1].lower()

    def test_all_denied_still_ready(self):
        """All denied is not an error — READY with warnings."""
        decision = evaluate_mvp(
            _report(
                filled_count=0,
                denied_count=5,
            )
        )
        assert decision.verdict == MvpVerdict.READY
        assert any("denied" in w.lower() for w in decision.warnings)

    def test_next_action_mentions_continue(self):
        decision = evaluate_mvp(_report())
        assert "continue" in decision.next_action.lower()


# ===========================================================================
# NOT_READY
# ===========================================================================


class TestNotReady:
    def test_exceptions_trigger_not_ready(self):
        decision = evaluate_mvp(_report(exception_count=1))
        assert decision.verdict == MvpVerdict.NOT_READY
        assert len(decision.blockers) > 0
        assert any("exception" in b.lower() for b in decision.blockers)

    def test_multiple_exceptions(self):
        decision = evaluate_mvp(_report(exception_count=5))
        assert decision.verdict == MvpVerdict.NOT_READY
        assert "5" in decision.blockers[0]

    def test_next_action_mentions_investigate(self):
        decision = evaluate_mvp(_report(exception_count=1))
        assert "investigate" in decision.next_action.lower()


# ===========================================================================
# NEEDS_REMEDIATION
# ===========================================================================


class TestNeedsRemediation:
    def test_adapter_errors(self):
        decision = evaluate_mvp(_report(adapter_error_count=2))
        assert decision.verdict == MvpVerdict.NEEDS_REMEDIATION
        assert any("adapter" in b.lower() for b in decision.blockers)

    def test_reconciliation_mismatches(self):
        decision = evaluate_mvp(_report(mismatch_count=1))
        assert decision.verdict == MvpVerdict.NEEDS_REMEDIATION
        assert any("mismatch" in b.lower() for b in decision.blockers)

    def test_health_failure_above_block_ratio(self):
        # 3/4 = 75% failure rate, above 50% threshold
        decision = evaluate_mvp(_report(health_snapshots=4, health_ok_count=1))
        assert decision.verdict == MvpVerdict.NEEDS_REMEDIATION
        assert any("health" in b.lower() for b in decision.blockers)

    def test_next_action_mentions_address(self):
        decision = evaluate_mvp(_report(adapter_error_count=1))
        assert "address" in decision.next_action.lower()

    def test_multiple_blockers(self):
        decision = evaluate_mvp(
            _report(
                adapter_error_count=1,
                mismatch_count=2,
            )
        )
        assert decision.verdict == MvpVerdict.NEEDS_REMEDIATION
        assert len(decision.blockers) >= 2


# ===========================================================================
# Warnings
# ===========================================================================


class TestWarnings:
    def test_health_warning_below_block_threshold(self):
        # 1/3 = 33% failure, above 30% warn but below 50% block
        decision = evaluate_mvp(_report(health_snapshots=3, health_ok_count=2))
        assert decision.verdict == MvpVerdict.READY
        assert any("health" in w.lower() for w in decision.warnings)

    def test_no_fills_warning(self):
        decision = evaluate_mvp(_report(filled_count=0, denied_count=5))
        assert any("filled" in w.lower() for w in decision.warnings)

    def test_no_warnings_on_clean_report(self):
        decision = evaluate_mvp(_report())
        assert len(decision.warnings) == 0


# ===========================================================================
# Priority / escalation
# ===========================================================================


class TestVerdictPriority:
    def test_exception_overrides_adapter_error(self):
        """NOT_READY (exceptions) takes priority over NEEDS_REMEDIATION."""
        decision = evaluate_mvp(
            _report(
                exception_count=1,
                adapter_error_count=2,
            )
        )
        assert decision.verdict == MvpVerdict.NOT_READY

    def test_adapter_error_overrides_ready(self):
        decision = evaluate_mvp(_report(adapter_error_count=1))
        assert decision.verdict == MvpVerdict.NEEDS_REMEDIATION

    def test_insufficient_data_overrides_all(self):
        """INSUFFICIENT_DATA is checked first regardless of other issues."""
        decision = evaluate_mvp(
            _report(
                total_records=1,
                exception_count=5,
                adapter_error_count=10,
            )
        )
        assert decision.verdict == MvpVerdict.INSUFFICIENT_DATA


# ===========================================================================
# Threshold constants
# ===========================================================================


class TestThresholdValues:
    """Verify thresholds are conservative and documented."""

    def test_min_records(self):
        assert MIN_RECORDS >= 3

    def test_min_executions(self):
        assert MIN_EXECUTIONS >= 1

    def test_max_adapter_errors(self):
        assert MAX_ADAPTER_ERRORS == 0

    def test_max_exceptions(self):
        assert MAX_EXCEPTIONS == 0

    def test_max_mismatches(self):
        assert MAX_MISMATCHES == 0

    def test_health_warn_below_block(self):
        assert HEALTH_FAILURE_WARN_RATIO < HEALTH_FAILURE_BLOCK_RATIO


# ===========================================================================
# MvpDecision model
# ===========================================================================


class TestMvpDecisionModel:
    def test_defaults(self):
        d = MvpDecision(verdict=MvpVerdict.READY)
        assert d.reasons == []
        assert d.blockers == []
        assert d.warnings == []
        assert d.next_action == ""
        assert d.timestamp > 0

    def test_verdict_values(self):
        assert MvpVerdict.READY.value == "READY"
        assert MvpVerdict.NEEDS_REMEDIATION.value == "NEEDS_REMEDIATION"
        assert MvpVerdict.NOT_READY.value == "NOT_READY"
        assert MvpVerdict.INSUFFICIENT_DATA.value == "INSUFFICIENT_DATA"


# ===========================================================================
# Formatting
# ===========================================================================


class TestFormatDecision:
    def test_ready_format(self):
        decision = evaluate_mvp(_report())
        text = format_decision(decision)
        assert "READY" in text
        assert "Verdict:" in text

    def test_not_ready_format(self):
        decision = evaluate_mvp(_report(exception_count=1))
        text = format_decision(decision)
        assert "NOT_READY" in text
        assert "Blockers" in text

    def test_remediation_format(self):
        decision = evaluate_mvp(_report(adapter_error_count=1))
        text = format_decision(decision)
        assert "NEEDS_REMEDIATION" in text

    def test_insufficient_data_format(self):
        decision = evaluate_mvp(ValidationReport())
        text = format_decision(decision)
        assert "INSUFFICIENT_DATA" in text

    def test_warnings_section_shown(self):
        decision = evaluate_mvp(_report(filled_count=0, denied_count=5))
        text = format_decision(decision)
        assert "Warnings" in text

    def test_next_action_always_shown(self):
        for report in [ValidationReport(), _report(), _report(exception_count=1)]:
            decision = evaluate_mvp(report)
            text = format_decision(decision)
            assert "Next Action" in text
