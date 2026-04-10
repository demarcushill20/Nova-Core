"""Tests for novatrade.validation — evidence capture and reporting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novatrade.models import (
    EvidenceRecord,
    EvidenceType,
    ExecutionOutcome,
    ExecutionResult,
    HealthSnapshot,
    HealthState,
    HealthStatus,
    MismatchType,
    OrderResult,
    OrderStatus,
    PositionMismatch,
    ReconciliationResult,
    RiskCheckResult,
    RiskDecision,
    RiskVerdict,
    ValidationReport,
)
from novatrade.validation.evidence import EvidenceRecorder
from novatrade.validation.reporting import format_report, generate_report

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def evidence_path(tmp_path: Path) -> Path:
    return tmp_path / "test_evidence.jsonl"


@pytest.fixture
def recorder(evidence_path: Path) -> EvidenceRecorder:
    return EvidenceRecorder(evidence_path)


def _filled_execution(ts: float = 1000.0) -> ExecutionResult:
    return ExecutionResult(
        outcome=ExecutionOutcome.FILLED,
        risk_decision=RiskDecision(
            verdict=RiskVerdict.ALLOW,
            checks=[RiskCheckResult(name="kill_switch", passed=True)],
        ),
        order_result=OrderResult(
            ok=True,
            order_id="ORD-1",
            status=OrderStatus.FILLED,
            fill_price=1.08000,
        ),
        elapsed_ms=150.0,
        timestamp=ts,
    )


def _denied_execution(ts: float = 1001.0) -> ExecutionResult:
    return ExecutionResult(
        outcome=ExecutionOutcome.DENIED,
        risk_decision=RiskDecision(
            verdict=RiskVerdict.DENY,
            rule="kill_switch",
            reason="kill switch active",
            checks=[
                RiskCheckResult(name="kill_switch", passed=False, detail="kill switch active"),
            ],
        ),
        elapsed_ms=5.0,
        timestamp=ts,
    )


def _error_execution(ts: float = 1002.0) -> ExecutionResult:
    return ExecutionResult(
        outcome=ExecutionOutcome.ADAPTER_ERROR,
        risk_decision=RiskDecision(verdict=RiskVerdict.ALLOW),
        order_result=OrderResult(ok=False, error="broker timeout"),
        error="broker timeout",
        elapsed_ms=5000.0,
        timestamp=ts,
    )


def _exception_execution(ts: float = 1003.0) -> ExecutionResult:
    return ExecutionResult(
        outcome=ExecutionOutcome.EXCEPTION,
        error="unexpected crash",
        elapsed_ms=10.0,
        timestamp=ts,
    )


def _healthy_snapshot(ts: float = 2000.0) -> HealthSnapshot:
    from novatrade.models import AccountState

    return HealthSnapshot(
        adapter_health=HealthStatus(state=HealthState.OK, connected=True),
        account=AccountState(balance=10000.0, equity=10200.0),
        position_count=2,
        total_unrealized_pnl=50.0,
        timestamp=ts,
    )


def _unhealthy_snapshot(ts: float = 2001.0) -> HealthSnapshot:
    return HealthSnapshot(
        adapter_health=HealthStatus(state=HealthState.DOWN, connected=False),
        error="connection lost",
        timestamp=ts,
    )


def _ok_reconciliation(ts: float = 3000.0) -> ReconciliationResult:
    return ReconciliationResult(ok=True, expected_count=2, actual_count=2, timestamp=ts)


def _bad_reconciliation(ts: float = 3001.0) -> ReconciliationResult:
    return ReconciliationResult(
        ok=False,
        mismatches=[
            PositionMismatch(
                mismatch_type=MismatchType.MISSING,
                symbol="EURUSD",
                detail="expected BUY EURUSD vol=0.1 — not found",
            ),
        ],
        expected_count=1,
        actual_count=0,
        timestamp=ts,
    )


# ===========================================================================
# EvidenceRecord model tests
# ===========================================================================


class TestEvidenceRecord:
    def test_to_dict(self):
        rec = EvidenceRecord(
            event_type=EvidenceType.EXECUTION,
            data={"outcome": "FILLED"},
            timestamp=1234.5,
        )
        d = rec.to_dict()
        assert d["event_type"] == "EXECUTION"
        assert d["data"] == {"outcome": "FILLED"}
        assert d["timestamp"] == 1234.5
        assert d["error"] == ""

    def test_from_dict(self):
        d = {"event_type": "HEALTH_SNAPSHOT", "data": {"ok": True}, "timestamp": 999.0}
        rec = EvidenceRecord.from_dict(d)
        assert rec.event_type == EvidenceType.HEALTH_SNAPSHOT
        assert rec.data == {"ok": True}
        assert rec.timestamp == 999.0

    def test_roundtrip(self):
        original = EvidenceRecord(
            event_type=EvidenceType.ADAPTER_ERROR,
            data={"context": "test"},
            error="something broke",
            timestamp=5555.5,
        )
        restored = EvidenceRecord.from_dict(original.to_dict())
        assert restored.event_type == original.event_type
        assert restored.data == original.data
        assert restored.error == original.error
        assert restored.timestamp == original.timestamp


# ===========================================================================
# EvidenceRecorder tests
# ===========================================================================


class TestEvidenceRecorder:
    def test_record_execution_filled(self, recorder: EvidenceRecorder, evidence_path: Path):
        recorder.record_execution(_filled_execution())
        records = recorder.load()
        assert len(records) == 1
        assert records[0].event_type == EvidenceType.EXECUTION
        assert records[0].data["outcome"] == "FILLED"
        assert records[0].data["order_id"] == "ORD-1"

    def test_record_execution_denied(self, recorder: EvidenceRecorder):
        recorder.record_execution(_denied_execution())
        records = recorder.load()
        assert len(records) == 1
        assert records[0].data["outcome"] == "DENIED"
        assert records[0].data["verdict"] == "DENY"
        assert records[0].data["rule"] == "kill_switch"

    def test_record_execution_error(self, recorder: EvidenceRecorder):
        recorder.record_execution(_error_execution())
        records = recorder.load()
        assert records[0].error == "broker timeout"

    def test_record_health(self, recorder: EvidenceRecorder):
        recorder.record_health(_healthy_snapshot())
        records = recorder.load()
        assert len(records) == 1
        assert records[0].event_type == EvidenceType.HEALTH_SNAPSHOT
        assert records[0].data["ok"] is True
        assert records[0].data["balance"] == 10000.0

    def test_record_health_unhealthy(self, recorder: EvidenceRecorder):
        recorder.record_health(_unhealthy_snapshot())
        records = recorder.load()
        assert records[0].data["ok"] is False
        assert records[0].error == "connection lost"

    def test_record_reconciliation_ok(self, recorder: EvidenceRecorder):
        recorder.record_reconciliation(_ok_reconciliation())
        records = recorder.load()
        assert records[0].event_type == EvidenceType.RECONCILIATION
        assert records[0].data["ok"] is True
        assert records[0].data["mismatch_count"] == 0

    def test_record_reconciliation_mismatch(self, recorder: EvidenceRecorder):
        recorder.record_reconciliation(_bad_reconciliation())
        records = recorder.load()
        assert records[0].data["ok"] is False
        assert records[0].data["mismatch_count"] == 1
        assert records[0].data["mismatches"][0]["type"] == "MISSING"

    def test_record_risk_decision(self, recorder: EvidenceRecorder):
        decision = RiskDecision(
            verdict=RiskVerdict.DENY,
            rule="max_positions",
            reason="too many open",
            checks=[RiskCheckResult(name="max_positions", passed=False)],
        )
        recorder.record_risk_decision(decision, symbol="EURUSD")
        records = recorder.load()
        assert records[0].event_type == EvidenceType.RISK_DECISION
        assert records[0].data["symbol"] == "EURUSD"
        assert records[0].data["verdict"] == "DENY"

    def test_record_error(self, recorder: EvidenceRecorder):
        recorder.record_error("adapter crashed", {"method": "place_order"})
        records = recorder.load()
        assert records[0].event_type == EvidenceType.ADAPTER_ERROR
        assert records[0].error == "adapter crashed"
        assert records[0].data["method"] == "place_order"

    def test_multiple_records_appended(self, recorder: EvidenceRecorder):
        recorder.record_execution(_filled_execution())
        recorder.record_execution(_denied_execution())
        recorder.record_health(_healthy_snapshot())
        records = recorder.load()
        assert len(records) == 3

    def test_load_nonexistent_file(self, tmp_path: Path):
        recorder = EvidenceRecorder(tmp_path / "nonexistent.jsonl")
        records = recorder.load()
        assert records == []

    def test_creates_parent_dirs(self, tmp_path: Path):
        deep_path = tmp_path / "a" / "b" / "c" / "evidence.jsonl"
        recorder = EvidenceRecorder(deep_path)
        recorder.record_error("test")
        assert deep_path.exists()

    def test_malformed_lines_skipped(self, evidence_path: Path):
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        with evidence_path.open("w") as f:
            f.write('{"event_type": "EXECUTION", "data": {"outcome": "FILLED"}, "timestamp": 1.0}\n')
            f.write("this is not json\n")
            f.write('{"bad": "no event_type"}\n')
            f.write('{"event_type": "HEALTH_SNAPSHOT", "data": {"ok": true}, "timestamp": 2.0}\n')
        recorder = EvidenceRecorder(evidence_path)
        records = recorder.load()
        assert len(records) == 2
        assert records[0].event_type == EvidenceType.EXECUTION
        assert records[1].event_type == EvidenceType.HEALTH_SNAPSHOT

    def test_empty_file(self, evidence_path: Path):
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text("")
        recorder = EvidenceRecorder(evidence_path)
        records = recorder.load()
        assert records == []

    def test_jsonl_format(self, recorder: EvidenceRecorder, evidence_path: Path):
        recorder.record_execution(_filled_execution())
        recorder.record_execution(_denied_execution())
        lines = evidence_path.read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            d = json.loads(line)
            assert "event_type" in d
            assert "timestamp" in d


# ===========================================================================
# Report generation tests
# ===========================================================================


class TestReportGeneration:
    def test_empty_records(self):
        report = generate_report([])
        assert report.total_records == 0
        assert report.stable is False

    def test_filled_execution(self):
        records = [
            EvidenceRecord(event_type=EvidenceType.EXECUTION, data={"outcome": "FILLED"}, timestamp=100.0),
        ]
        report = generate_report(records)
        assert report.execution_count == 1
        assert report.filled_count == 1
        assert report.denied_count == 0
        assert report.stable is True

    def test_denied_execution(self):
        records = [
            EvidenceRecord(event_type=EvidenceType.EXECUTION, data={"outcome": "DENIED"}, timestamp=100.0),
        ]
        report = generate_report(records)
        assert report.denied_count == 1
        assert report.stable is True  # denied is not an error

    def test_adapter_error_marks_unstable(self):
        records = [
            EvidenceRecord(
                event_type=EvidenceType.EXECUTION,
                data={"outcome": "ADAPTER_ERROR"},
                error="timeout",
                timestamp=100.0,
            ),
        ]
        report = generate_report(records)
        assert report.adapter_error_count == 1
        assert report.stable is False
        assert len(report.recent_failures) == 1

    def test_exception_marks_unstable(self):
        records = [
            EvidenceRecord(
                event_type=EvidenceType.EXECUTION,
                data={"outcome": "EXCEPTION"},
                error="crash",
                timestamp=100.0,
            ),
        ]
        report = generate_report(records)
        assert report.exception_count == 1
        assert report.stable is False

    def test_health_snapshots(self):
        records = [
            EvidenceRecord(event_type=EvidenceType.HEALTH_SNAPSHOT, data={"ok": True}, timestamp=100.0),
            EvidenceRecord(event_type=EvidenceType.HEALTH_SNAPSHOT, data={"ok": False}, error="down", timestamp=101.0),
        ]
        report = generate_report(records)
        assert report.health_snapshots == 2
        assert report.health_ok_count == 1

    def test_reconciliation(self):
        records = [
            EvidenceRecord(
                event_type=EvidenceType.RECONCILIATION, data={"ok": True, "mismatch_count": 0}, timestamp=100.0
            ),
            EvidenceRecord(
                event_type=EvidenceType.RECONCILIATION,
                data={
                    "ok": False,
                    "mismatch_count": 2,
                    "mismatches": [
                        {"type": "MISSING", "symbol": "EURUSD"},
                        {"type": "VOLUME", "symbol": "GBPUSD"},
                    ],
                },
                timestamp=101.0,
            ),
        ]
        report = generate_report(records)
        assert report.reconciliation_count == 2
        assert report.reconciliation_ok_count == 1
        assert report.mismatch_count == 2
        assert report.stable is False

    def test_mixed_scenario(self):
        records = [
            EvidenceRecord(event_type=EvidenceType.EXECUTION, data={"outcome": "FILLED"}, timestamp=100.0),
            EvidenceRecord(event_type=EvidenceType.EXECUTION, data={"outcome": "DENIED"}, timestamp=101.0),
            EvidenceRecord(event_type=EvidenceType.EXECUTION, data={"outcome": "FILLED"}, timestamp=102.0),
            EvidenceRecord(event_type=EvidenceType.HEALTH_SNAPSHOT, data={"ok": True}, timestamp=103.0),
            EvidenceRecord(
                event_type=EvidenceType.RECONCILIATION, data={"ok": True, "mismatch_count": 0}, timestamp=104.0
            ),
        ]
        report = generate_report(records)
        assert report.total_records == 5
        assert report.execution_count == 3
        assert report.filled_count == 2
        assert report.denied_count == 1
        assert report.health_snapshots == 1
        assert report.reconciliation_count == 1
        assert report.stable is True

    def test_time_range(self):
        records = [
            EvidenceRecord(event_type=EvidenceType.EXECUTION, data={"outcome": "FILLED"}, timestamp=500.0),
            EvidenceRecord(event_type=EvidenceType.EXECUTION, data={"outcome": "FILLED"}, timestamp=900.0),
        ]
        report = generate_report(records)
        assert report.time_range_start == 500.0
        assert report.time_range_end == 900.0

    def test_recent_failures_capped(self):
        records = [
            EvidenceRecord(
                event_type=EvidenceType.ADAPTER_ERROR,
                error=f"error-{i}",
                timestamp=float(i),
            )
            for i in range(20)
        ]
        report = generate_report(records)
        assert len(report.recent_failures) == 10
        # Most recent first
        assert report.recent_failures[0]["error"] == "error-19"

    def test_standalone_adapter_error(self):
        records = [
            EvidenceRecord(
                event_type=EvidenceType.ADAPTER_ERROR,
                data={},
                error="connection reset",
                timestamp=100.0,
            ),
        ]
        report = generate_report(records)
        assert report.adapter_error_count == 1
        assert report.stable is False


# ===========================================================================
# Report formatting tests
# ===========================================================================


class TestReportFormatting:
    def test_empty_report(self):
        text = format_report(ValidationReport())
        assert "No evidence records found" in text

    def test_stable_report(self):
        report = generate_report(
            [
                EvidenceRecord(event_type=EvidenceType.EXECUTION, data={"outcome": "FILLED"}, timestamp=100.0),
            ]
        )
        text = format_report(report)
        assert "STABLE" in text
        assert "Attempts:" in text
        assert "Filled:" in text

    def test_unstable_report(self):
        report = generate_report(
            [
                EvidenceRecord(
                    event_type=EvidenceType.EXECUTION, data={"outcome": "ADAPTER_ERROR"}, error="fail", timestamp=100.0
                ),
            ]
        )
        text = format_report(report)
        assert "UNSTABLE" in text

    def test_contains_sections(self):
        report = generate_report(
            [
                EvidenceRecord(event_type=EvidenceType.EXECUTION, data={"outcome": "FILLED"}, timestamp=100.0),
                EvidenceRecord(event_type=EvidenceType.HEALTH_SNAPSHOT, data={"ok": True}, timestamp=101.0),
                EvidenceRecord(
                    event_type=EvidenceType.RECONCILIATION, data={"ok": True, "mismatch_count": 0}, timestamp=102.0
                ),
            ]
        )
        text = format_report(report)
        assert "Execution Summary" in text
        assert "Health Snapshots" in text
        assert "Reconciliation" in text
        assert "Stability Verdict" in text


# ===========================================================================
# ValidationReport model tests
# ===========================================================================


class TestValidationReportModel:
    def test_stable_true_with_filled(self):
        report = ValidationReport(total_records=1, filled_count=1)
        assert report.stable is True

    def test_stable_false_no_records(self):
        report = ValidationReport()
        assert report.stable is False

    def test_stable_false_adapter_error(self):
        report = ValidationReport(total_records=1, adapter_error_count=1)
        assert report.stable is False

    def test_stable_false_exception(self):
        report = ValidationReport(total_records=1, exception_count=1)
        assert report.stable is False

    def test_stable_false_mismatches(self):
        report = ValidationReport(total_records=1, mismatch_count=1)
        assert report.stable is False

    def test_stable_true_denied_only(self):
        report = ValidationReport(total_records=1, denied_count=1)
        assert report.stable is True


# ===========================================================================
# Integration: recorder → report round-trip
# ===========================================================================


class TestEndToEnd:
    def test_full_round_trip(self, recorder: EvidenceRecorder):
        recorder.record_execution(_filled_execution(ts=100.0))
        recorder.record_execution(_denied_execution(ts=101.0))
        recorder.record_health(_healthy_snapshot(ts=102.0))
        recorder.record_reconciliation(_ok_reconciliation(ts=103.0))

        records = recorder.load()
        report = generate_report(records)

        assert report.total_records == 4
        assert report.execution_count == 2
        assert report.filled_count == 1
        assert report.denied_count == 1
        assert report.health_snapshots == 1
        assert report.health_ok_count == 1
        assert report.reconciliation_count == 1
        assert report.reconciliation_ok_count == 1
        assert report.stable is True

        text = format_report(report)
        assert "STABLE" in text

    def test_unstable_round_trip(self, recorder: EvidenceRecorder):
        recorder.record_execution(_filled_execution(ts=100.0))
        recorder.record_execution(_error_execution(ts=101.0))
        recorder.record_reconciliation(_bad_reconciliation(ts=102.0))

        records = recorder.load()
        report = generate_report(records)

        assert report.stable is False
        assert report.adapter_error_count == 1
        assert report.mismatch_count == 1

        text = format_report(report)
        assert "UNSTABLE" in text
