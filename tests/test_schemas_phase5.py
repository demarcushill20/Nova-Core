"""Tests for Phase 5.1 Pydantic v2 schemas."""

import pytest
from pydantic import ValidationError

from utils.schemas.contract import ContractConfidence, ContractField, ContractOutput
from utils.schemas.heartbeat import CycleOutcome, HeartbeatDecision, WriteStatus
from utils.schemas.task import Confidence, TaskResult, TaskVerification
from utils.schemas.trading import SignalAction, TradeSignal

# ── HeartbeatDecision ────────────────────────────────────────────────────────


class TestHeartbeatDecision:
    def test_valid_construction(self):
        d = HeartbeatDecision(action="research", reason="idle cycle", priority=7, target="NovaTrade")
        assert d.action == "research"
        assert d.reason == "idle cycle"
        assert d.priority == 7
        assert d.target == "NovaTrade"

    def test_defaults(self):
        d = HeartbeatDecision(action="idle")
        assert d.reason == ""
        assert d.priority == 5
        assert d.target == ""
        assert d.estimated_duration_minutes == 30

    def test_priority_lower_bound(self):
        with pytest.raises(ValidationError):
            HeartbeatDecision(action="idle", priority=0)

    def test_priority_upper_bound(self):
        with pytest.raises(ValidationError):
            HeartbeatDecision(action="idle", priority=11)

    def test_estimated_duration_bounds(self):
        d = HeartbeatDecision(action="task", estimated_duration_minutes=1)
        assert d.estimated_duration_minutes == 1
        d2 = HeartbeatDecision(action="task", estimated_duration_minutes=180)
        assert d2.estimated_duration_minutes == 180
        with pytest.raises(ValidationError):
            HeartbeatDecision(action="task", estimated_duration_minutes=0)
        with pytest.raises(ValidationError):
            HeartbeatDecision(action="task", estimated_duration_minutes=181)

    def test_missing_action_raises(self):
        with pytest.raises(ValidationError):
            HeartbeatDecision()


# ── CycleOutcome ─────────────────────────────────────────────────────────────


class TestCycleOutcome:
    def test_valid_construction(self):
        c = CycleOutcome(
            title="Research complete",
            summary="Found 3 relevant papers",
            vault_write=WriteStatus.SUCCESS,
            memory_write=WriteStatus.SUCCESS,
            artifacts=["OUTPUT/research.md"],
            next_steps=["Deep dive on paper 1"],
        )
        assert c.title == "Research complete"
        assert c.vault_write == WriteStatus.SUCCESS
        assert c.memory_write == WriteStatus.SUCCESS
        assert len(c.artifacts) == 1
        assert len(c.next_steps) == 1

    def test_defaults(self):
        c = CycleOutcome(title="Test")
        assert c.summary == ""
        assert c.vault_write == WriteStatus.UNKNOWN
        assert c.memory_write == WriteStatus.UNKNOWN
        assert c.artifacts == []
        assert c.next_steps == []

    def test_write_status_enum(self):
        assert WriteStatus.SUCCESS.value == "success"
        assert WriteStatus.FAILED.value == "failed"
        assert WriteStatus.UNKNOWN.value == "unknown"

    def test_title_max_length(self):
        # Exactly at 200 should be fine
        c = CycleOutcome(title="A" * 200)
        assert len(c.title) == 200
        # Over 200 should fail
        with pytest.raises(ValidationError):
            CycleOutcome(title="A" * 201)

    def test_missing_title_raises(self):
        with pytest.raises(ValidationError):
            CycleOutcome()


# ── TaskVerification ─────────────────────────────────────────────────────────


class TestTaskVerification:
    def test_basic_construction(self):
        v = TaskVerification(description="Tests pass", passed=True, detail="All 42 tests green")
        assert v.description == "Tests pass"
        assert v.passed is True
        assert v.detail == "All 42 tests green"

    def test_defaults(self):
        v = TaskVerification(description="Lint check", passed=False)
        assert v.detail == ""

    def test_missing_fields_raises(self):
        with pytest.raises(ValidationError):
            TaskVerification(description="Missing passed")
        with pytest.raises(ValidationError):
            TaskVerification(passed=True)


# ── TaskResult ───────────────────────────────────────────────────────────────


class TestTaskResult:
    def test_valid_construction(self):
        r = TaskResult(
            summary="Implemented feature X",
            files_changed=["utils/foo.py", "tests/test_foo.py"],
            confidence=Confidence.HIGH,
        )
        assert r.summary == "Implemented feature X"
        assert len(r.files_changed) == 2
        assert r.confidence == Confidence.HIGH

    def test_files_changed_accepts_string(self):
        r = TaskResult(summary="test", files_changed="a.py, b.py, c.py")
        assert r.files_changed == ["a.py", "b.py", "c.py"]

    def test_files_changed_accepts_list(self):
        r = TaskResult(summary="test", files_changed=["x.py"])
        assert r.files_changed == ["x.py"]

    def test_files_changed_string_strips_whitespace(self):
        r = TaskResult(summary="test", files_changed="  a.py ,  b.py  ")
        assert r.files_changed == ["a.py", "b.py"]

    def test_files_changed_empty_string(self):
        r = TaskResult(summary="test", files_changed="")
        assert r.files_changed == []

    def test_defaults(self):
        r = TaskResult(summary="Done")
        assert r.files_changed == []
        assert r.verification == []
        assert r.confidence == Confidence.MEDIUM
        assert r.errors == []
        assert r.warnings == []
        assert r.next_actions == []

    def test_with_verification(self):
        v = TaskVerification(description="Tests pass", passed=True)
        r = TaskResult(summary="Done", verification=[v])
        assert len(r.verification) == 1
        assert r.verification[0].passed is True

    def test_confidence_enum(self):
        assert Confidence.HIGH.value == "high"
        assert Confidence.MEDIUM.value == "medium"
        assert Confidence.LOW.value == "low"

    def test_missing_summary_raises(self):
        with pytest.raises(ValidationError):
            TaskResult()


# ── ContractOutput ───────────────────────────────────────────────────────────


class TestContractOutput:
    def test_valid_construction(self):
        c = ContractOutput(
            summary="Implemented X",
            files_changed=["a.py"],
            verification="Run pytest",
            confidence=ContractConfidence.HIGH,
            tests_passed=True,
            test_count=42,
            lint_clean=True,
        )
        assert c.summary == "Implemented X"
        assert c.files_changed == ["a.py"]
        assert c.confidence == ContractConfidence.HIGH
        assert c.tests_passed is True
        assert c.test_count == 42

    def test_defaults(self):
        c = ContractOutput(summary="Done")
        assert c.files_changed == []
        assert c.verification == ""
        assert c.confidence == ContractConfidence.MEDIUM
        assert c.tests_passed is None
        assert c.test_count is None
        assert c.lint_clean is None
        assert c.extra_fields == []

    def test_files_changed_string_parsing(self):
        c = ContractOutput(summary="Done", files_changed="x.py, y.py")
        assert c.files_changed == ["x.py", "y.py"]

    def test_to_markdown_basic(self):
        c = ContractOutput(
            summary="Built feature",
            files_changed=["a.py", "b.py"],
            verification="Run tests",
            confidence=ContractConfidence.HIGH,
        )
        md = c.to_markdown()
        assert md.startswith("## CONTRACT")
        assert "summary: Built feature" in md
        assert "files_changed: a.py, b.py" in md
        assert "verification: Run tests" in md
        assert "confidence: high" in md

    def test_to_markdown_with_optional_fields(self):
        c = ContractOutput(
            summary="Done",
            confidence=ContractConfidence.MEDIUM,
            tests_passed=True,
            test_count=10,
            lint_clean=False,
        )
        md = c.to_markdown()
        assert "tests_passed: True" in md
        assert "test_count: 10" in md
        assert "lint_clean: False" in md

    def test_to_markdown_with_extra_fields(self):
        c = ContractOutput(
            summary="Done",
            extra_fields=[
                ContractField(key="task_id", value="T-001"),
                ContractField(key="status", value="complete"),
            ],
        )
        md = c.to_markdown()
        assert "task_id: T-001" in md
        assert "status: complete" in md

    def test_to_markdown_without_empty_optional(self):
        c = ContractOutput(summary="Done")
        md = c.to_markdown()
        # files_changed empty -> not in output
        assert "files_changed" not in md
        # verification empty -> not in output
        assert "verification" not in md
        # tests_passed None -> not in output
        assert "tests_passed" not in md

    def test_missing_summary_raises(self):
        with pytest.raises(ValidationError):
            ContractOutput()


# ── TradeSignal ──────────────────────────────────────────────────────────────


class TestTradeSignal:
    def test_valid_construction(self):
        s = TradeSignal(
            action=SignalAction.SIGNAL,
            symbol="EURUSD",
            side="buy",
            entry_price=1.0850,
            stop_loss=1.0800,
            take_profit=1.0950,
            lot_size=0.1,
            timeframe="4H",
            strategy="breakout_v1",
            confidence=0.85,
        )
        assert s.action == SignalAction.SIGNAL
        assert s.symbol == "EURUSD"
        assert s.side == "buy"
        assert s.entry_price == 1.0850
        assert s.confidence == 0.85

    def test_symbol_normalization_uppercase(self):
        s = TradeSignal(action=SignalAction.SIGNAL, symbol="eurusd")
        assert s.symbol == "EURUSD"

    def test_symbol_normalization_strips(self):
        s = TradeSignal(action=SignalAction.SIGNAL, symbol="  gbpusd  ")
        assert s.symbol == "GBPUSD"

    def test_side_normalization_lowercase(self):
        s = TradeSignal(action=SignalAction.SIGNAL, symbol="EURUSD", side="BUY")
        assert s.side == "buy"

    def test_side_normalization_strips(self):
        s = TradeSignal(action=SignalAction.SIGNAL, symbol="EURUSD", side="  Sell  ")
        assert s.side == "sell"

    def test_defaults(self):
        s = TradeSignal(action=SignalAction.CLOSE, symbol="USDJPY")
        assert s.side == ""
        assert s.entry_price is None
        assert s.stop_loss is None
        assert s.take_profit is None
        assert s.lot_size is None
        assert s.timeframe == ""
        assert s.strategy == ""
        assert s.confidence == 0.5
        assert s.metadata == {}

    def test_confidence_lower_bound(self):
        s = TradeSignal(action=SignalAction.SIGNAL, symbol="X", confidence=0.0)
        assert s.confidence == 0.0

    def test_confidence_upper_bound(self):
        s = TradeSignal(action=SignalAction.SIGNAL, symbol="X", confidence=1.0)
        assert s.confidence == 1.0

    def test_confidence_too_high_raises(self):
        with pytest.raises(ValidationError):
            TradeSignal(action=SignalAction.SIGNAL, symbol="X", confidence=2.0)

    def test_confidence_negative_raises(self):
        with pytest.raises(ValidationError):
            TradeSignal(action=SignalAction.SIGNAL, symbol="X", confidence=-0.1)

    def test_signal_action_enum(self):
        assert SignalAction.SIGNAL.value == "signal"
        assert SignalAction.TRAIL.value == "trail"
        assert SignalAction.CANCEL.value == "cancel"
        assert SignalAction.CLOSE.value == "close"

    def test_invalid_action_raises(self):
        with pytest.raises(ValidationError):
            TradeSignal(action="invalid_action", symbol="EURUSD")

    def test_empty_symbol_raises(self):
        with pytest.raises(ValidationError):
            TradeSignal(action=SignalAction.SIGNAL, symbol="")

    def test_metadata_dict(self):
        s = TradeSignal(
            action=SignalAction.SIGNAL,
            symbol="EURUSD",
            metadata={"source": "tradingview", "alert_id": 123},
        )
        assert s.metadata["source"] == "tradingview"
        assert s.metadata["alert_id"] == 123


# ── Cross-model validation errors ───────────────────────────────────────────


class TestValidationErrors:
    def test_heartbeat_priority_zero(self):
        with pytest.raises(ValidationError) as exc_info:
            HeartbeatDecision(action="idle", priority=0)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("priority",) for e in errors)

    def test_trade_signal_confidence_out_of_range(self):
        with pytest.raises(ValidationError) as exc_info:
            TradeSignal(action=SignalAction.SIGNAL, symbol="EURUSD", confidence=2.0)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("confidence",) for e in errors)

    def test_task_result_missing_summary(self):
        with pytest.raises(ValidationError) as exc_info:
            TaskResult()
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("summary",) for e in errors)


# ── Import from __init__ ────────────────────────────────────────────────────


class TestImports:
    def test_all_exports_importable(self):
        from utils.schemas import (
            ContractField,
            ContractOutput,
            CycleOutcome,
            HeartbeatDecision,
            SignalAction,
            TaskResult,
            TaskVerification,
            TradeSignal,
        )

        # Verify they are the correct classes
        assert HeartbeatDecision.__name__ == "HeartbeatDecision"
        assert CycleOutcome.__name__ == "CycleOutcome"
        assert TaskResult.__name__ == "TaskResult"
        assert TaskVerification.__name__ == "TaskVerification"
        assert ContractOutput.__name__ == "ContractOutput"
        assert ContractField.__name__ == "ContractField"
        assert TradeSignal.__name__ == "TradeSignal"
        assert SignalAction.__name__ == "SignalAction"
