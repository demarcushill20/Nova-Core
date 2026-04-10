"""Tests for novatrade.validation.readiness — MVP readiness/remediation package."""

from __future__ import annotations

from novatrade.models import (
    MvpVerdict,
    ReadinessPackage,
    RemediationItem,
    RemediationSeverity,
    ValidationReport,
)
from novatrade.validation.decision import evaluate_mvp
from novatrade.validation.readiness import build_readiness_package, format_readiness

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _report(**overrides) -> ValidationReport:
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


def _build(**overrides) -> ReadinessPackage:
    report = _report(**overrides)
    decision = evaluate_mvp(report)
    return build_readiness_package(report, decision)


# ===========================================================================
# READY
# ===========================================================================


class TestReady:
    def test_clean_report_is_ready(self):
        pkg = _build()
        assert pkg.ready is True
        assert pkg.verdict == MvpVerdict.READY
        assert pkg.blocker_count == 0

    def test_next_step_mentions_continue(self):
        pkg = _build()
        assert "continue" in pkg.next_step.lower() or "stable" in pkg.next_step.lower()

    def test_no_do_not_proceed(self):
        pkg = _build()
        assert len(pkg.do_not_proceed) == 0


# ===========================================================================
# INSUFFICIENT_DATA
# ===========================================================================


class TestInsufficientData:
    def test_empty_report(self):
        report = ValidationReport()
        decision = evaluate_mvp(report)
        pkg = build_readiness_package(report, decision)
        assert pkg.ready is False
        assert pkg.verdict == MvpVerdict.INSUFFICIENT_DATA

    def test_few_records_has_evidence_gap(self):
        report = _report(total_records=5)
        decision = evaluate_mvp(report)
        pkg = build_readiness_package(report, decision)
        assert any("evidence" in g.lower() or "records" in g.lower() for g in pkg.evidence_gaps)

    def test_no_health_snapshots_gap(self):
        pkg = _build(health_snapshots=0, health_ok_count=0)
        assert any("health" in g.lower() for g in pkg.evidence_gaps)

    def test_no_reconciliation_gap(self):
        pkg = _build(reconciliation_count=0, reconciliation_ok_count=0)
        assert any("reconciliation" in g.lower() for g in pkg.evidence_gaps)

    def test_no_fills_gap(self):
        pkg = _build(filled_count=0, denied_count=5)
        assert any("fill" in g.lower() for g in pkg.evidence_gaps)


# ===========================================================================
# NOT_READY (exceptions)
# ===========================================================================


class TestNotReady:
    def test_exceptions_are_blockers(self):
        pkg = _build(exception_count=3)
        assert pkg.ready is False
        assert pkg.verdict == MvpVerdict.NOT_READY
        assert pkg.blocker_count >= 1
        exception_items = [i for i in pkg.items if i.category == "exceptions"]
        assert len(exception_items) == 1
        assert exception_items[0].severity == RemediationSeverity.BLOCKER

    def test_exceptions_have_action(self):
        pkg = _build(exception_count=1)
        item = next(i for i in pkg.items if i.category == "exceptions")
        assert len(item.action) > 0
        assert "log" in item.action.lower() or "stack" in item.action.lower()

    def test_do_not_proceed_on_exceptions(self):
        pkg = _build(exception_count=1)
        assert len(pkg.do_not_proceed) > 0
        assert any("exception" in c.lower() for c in pkg.do_not_proceed)


# ===========================================================================
# NEEDS_REMEDIATION
# ===========================================================================


class TestNeedsRemediation:
    def test_adapter_errors(self):
        pkg = _build(adapter_error_count=2)
        assert pkg.ready is False
        assert pkg.verdict == MvpVerdict.NEEDS_REMEDIATION
        adapter_items = [i for i in pkg.items if i.category == "adapter_errors"]
        assert len(adapter_items) == 1
        assert adapter_items[0].severity == RemediationSeverity.BLOCKER
        assert "2" in adapter_items[0].description

    def test_adapter_errors_have_action(self):
        pkg = _build(adapter_error_count=1)
        item = next(i for i in pkg.items if i.category == "adapter_errors")
        assert "metaapi" in item.action.lower() or "symbol" in item.action.lower()

    def test_adapter_errors_do_not_proceed(self):
        pkg = _build(adapter_error_count=1)
        assert len(pkg.do_not_proceed) > 0

    def test_reconciliation_mismatches(self):
        pkg = _build(mismatch_count=3)
        assert pkg.ready is False
        recon_items = [i for i in pkg.items if i.category == "reconciliation"]
        assert len(recon_items) == 1
        assert recon_items[0].severity == RemediationSeverity.BLOCKER

    def test_reconciliation_has_action(self):
        pkg = _build(mismatch_count=1)
        item = next(i for i in pkg.items if i.category == "reconciliation")
        assert "mismatch" in item.action.lower() or "tracker" in item.action.lower()

    def test_health_failure_blocker(self):
        """>=50% health failure rate is a blocker."""
        pkg = _build(health_snapshots=4, health_ok_count=1)
        health_items = [i for i in pkg.items if i.category == "health"]
        assert len(health_items) == 1
        assert health_items[0].severity == RemediationSeverity.BLOCKER

    def test_multiple_blockers(self):
        pkg = _build(adapter_error_count=1, mismatch_count=1, exception_count=1)
        assert pkg.blocker_count >= 3


# ===========================================================================
# Warnings
# ===========================================================================


class TestWarnings:
    def test_health_warning(self):
        """<50% but >0 health failures is a warning."""
        pkg = _build(health_snapshots=4, health_ok_count=3)
        health_items = [i for i in pkg.items if i.category == "health"]
        assert len(health_items) == 1
        assert health_items[0].severity == RemediationSeverity.WARNING

    def test_all_denied_warning(self):
        pkg = _build(filled_count=0, denied_count=5)
        denied_items = [i for i in pkg.items if i.category == "all_denied"]
        assert len(denied_items) == 1
        assert denied_items[0].severity == RemediationSeverity.WARNING
        assert "demo mode" in denied_items[0].action.lower()

    def test_no_fills_no_denials_warning(self):
        pkg = _build(filled_count=0, denied_count=0, execution_count=1)
        no_fill_items = [i for i in pkg.items if i.category in ("no_fills", "all_denied")]
        assert len(no_fill_items) == 1

    def test_warnings_do_not_block_ready(self):
        """Warnings alone should not prevent READY verdict."""
        pkg = _build(health_snapshots=4, health_ok_count=3)
        assert pkg.ready is True
        assert pkg.warning_count > 0


# ===========================================================================
# Mixed scenarios
# ===========================================================================


class TestMixedScenarios:
    def test_adapter_error_plus_warning(self):
        pkg = _build(
            adapter_error_count=1,
            health_snapshots=4,
            health_ok_count=3,
        )
        assert pkg.ready is False
        assert pkg.blocker_count >= 1
        assert pkg.warning_count >= 1

    def test_ready_with_evidence_gaps(self):
        """READY verdict can still have evidence gaps."""
        pkg = _build(
            total_records=5,
            health_snapshots=0,
            health_ok_count=0,
            reconciliation_count=0,
            reconciliation_ok_count=0,
        )
        assert pkg.ready is True
        assert len(pkg.evidence_gaps) > 0

    def test_not_ready_with_everything_wrong(self):
        pkg = _build(
            exception_count=2,
            adapter_error_count=3,
            mismatch_count=1,
            health_snapshots=4,
            health_ok_count=1,
        )
        assert pkg.ready is False
        assert pkg.verdict == MvpVerdict.NOT_READY
        assert pkg.blocker_count >= 3
        assert len(pkg.do_not_proceed) >= 2


# ===========================================================================
# ReadinessPackage model
# ===========================================================================


class TestReadinessPackageModel:
    def test_defaults(self):
        pkg = ReadinessPackage(verdict=MvpVerdict.READY, ready=True)
        assert pkg.items == []
        assert pkg.do_not_proceed == []
        assert pkg.evidence_gaps == []
        assert pkg.blocker_count == 0
        assert pkg.warning_count == 0
        assert pkg.timestamp > 0

    def test_blocker_count(self):
        pkg = ReadinessPackage(
            verdict=MvpVerdict.NOT_READY,
            ready=False,
            items=[
                RemediationItem("a", "x", "y", RemediationSeverity.BLOCKER),
                RemediationItem("b", "x", "y", RemediationSeverity.WARNING),
                RemediationItem("c", "x", "y", RemediationSeverity.BLOCKER),
            ],
        )
        assert pkg.blocker_count == 2
        assert pkg.warning_count == 1


# ===========================================================================
# Formatting
# ===========================================================================


class TestFormatReadiness:
    def test_ready_format(self):
        pkg = _build()
        text = format_readiness(pkg)
        assert "READY" in text
        assert "YES" in text
        assert "Next Step" in text

    def test_not_ready_format(self):
        pkg = _build(exception_count=1)
        text = format_readiness(pkg)
        assert "NOT_READY" in text
        assert "NO" in text
        assert "Blockers" in text
        assert "Do Not Proceed" in text

    def test_remediation_format(self):
        pkg = _build(adapter_error_count=1)
        text = format_readiness(pkg)
        assert "NEEDS_REMEDIATION" in text
        assert "adapter_errors" in text
        assert "Action:" in text

    def test_warnings_section(self):
        pkg = _build(filled_count=0, denied_count=5)
        text = format_readiness(pkg)
        assert "Warnings" in text
        assert "all_denied" in text

    def test_evidence_gaps_section(self):
        pkg = _build(health_snapshots=0, health_ok_count=0)
        text = format_readiness(pkg)
        assert "Evidence Gaps" in text

    def test_empty_report_format(self):
        report = ValidationReport()
        decision = evaluate_mvp(report)
        pkg = build_readiness_package(report, decision)
        text = format_readiness(pkg)
        assert "INSUFFICIENT_DATA" in text
