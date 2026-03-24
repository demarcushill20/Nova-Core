"""Tests for novatrade.runtime.shadow_mode — signal parity validation.

Covers:
    1. ShadowSignalRecord serialization round-trip
    2. ShadowSignalLogger writes and reads JSONL correctly
    3. ShadowComparator perfect match, missed, extra, timing, tolerance
    4. ShadowReport serialization and summary output
    5. CutoverGate criteria logic
    6. ShadowModeRunner lifecycle (create, run briefly, stop, verify logs)
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from novatrade.runtime.shadow_mode import (
    CutoverGate,
    ShadowComparator,
    ShadowModeRunner,
    ShadowReport,
    ShadowSignalLogger,
    ShadowSignalRecord,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = time.time()


def _live_record(
    symbol: str = "EURUSD",
    side: str = "LONG",
    action: str = "ENTRY",
    price: float = 1.10050,
    bar_time: float = 0.0,
    ts: float = 0.0,
    metadata: dict | None = None,
) -> ShadowSignalRecord:
    return ShadowSignalRecord(
        source="live",
        timestamp=ts or _NOW,
        symbol=symbol,
        side=side,
        action=action,
        price=price,
        bar_time=bar_time or _NOW,
        metadata=metadata or {},
    )


def _tv_record(
    symbol: str = "EURUSD",
    side: str = "BUY",
    action: str = "ENTRY",
    price: float = 1.10050,
    bar_time: float = 0.0,
    ts: float = 0.0,
) -> ShadowSignalRecord:
    return ShadowSignalRecord(
        source="tv",
        timestamp=ts or _NOW,
        symbol=symbol,
        side=side,
        action=action,
        price=price,
        bar_time=bar_time or _NOW,
    )


# ---------------------------------------------------------------------------
# ShadowSignalRecord
# ---------------------------------------------------------------------------


class TestShadowSignalRecord:
    def test_serialization_round_trip(self) -> None:
        record = _live_record(metadata={"strategy": "IRB"})
        json_str = record.to_json()
        restored = ShadowSignalRecord.from_json(json_str)
        assert restored.source == record.source
        assert restored.symbol == record.symbol
        assert restored.side == record.side
        assert restored.action == record.action
        assert restored.price == record.price
        assert restored.bar_time == record.bar_time
        assert restored.metadata == record.metadata

    def test_from_live_signal(self) -> None:
        from novatrade.strategy.live_engine import LiveSignal, SignalType

        signal = LiveSignal(
            signal_type=SignalType.ENTRY,
            side="LONG",
            symbol="EURUSD",
            entry_price=1.10050,
            stop_loss=1.09900,
            volume=0.10,
        )
        record = ShadowSignalRecord.from_live_signal(signal)
        assert record.source == "live"
        assert record.symbol == "EURUSD"
        assert record.side == "LONG"
        assert record.action == "ENTRY"
        assert record.price == 1.10050

    def test_from_live_signal_uses_bar_close_time(self) -> None:
        """bar_time should prefer metadata['bar_close_time'] over signal.timestamp."""
        from novatrade.strategy.live_engine import LiveSignal, SignalType

        bar_close = 1711036800.0
        signal = LiveSignal(
            signal_type=SignalType.ENTRY,
            side="LONG",
            symbol="EURUSD",
            entry_price=1.10050,
            stop_loss=1.09900,
            volume=0.10,
            metadata={"bar_close_time": bar_close},
        )
        record = ShadowSignalRecord.from_live_signal(signal)
        assert record.bar_time == bar_close

    def test_from_live_signal_falls_back_to_timestamp(self) -> None:
        """When metadata has no bar_close_time, fall back to signal.timestamp."""
        from novatrade.strategy.live_engine import LiveSignal, SignalType

        signal = LiveSignal(
            signal_type=SignalType.ENTRY,
            side="LONG",
            symbol="EURUSD",
            entry_price=1.10050,
            stop_loss=1.09900,
            volume=0.10,
            metadata={},
        )
        record = ShadowSignalRecord.from_live_signal(signal)
        # Should fall back to signal.timestamp (wall-clock)
        assert record.bar_time == signal.timestamp

    def test_from_tv_alert(self) -> None:
        payload = {
            "symbol": "EURUSD",
            "side": "BUY",
            "signal_type": "ENTRY",
            "entry_price": 1.10050,
            "bar_close_time": 1711036800.0,
            "strategy_name": "IRB",
        }
        record = ShadowSignalRecord.from_tv_alert(payload)
        assert record.source == "tv"
        assert record.symbol == "EURUSD"
        assert record.side == "BUY"
        assert record.action == "ENTRY"
        assert record.price == 1.10050
        assert record.bar_time == 1711036800.0
        assert "strategy_name" in record.metadata


# ---------------------------------------------------------------------------
# ShadowSignalLogger
# ---------------------------------------------------------------------------


class TestShadowSignalLogger:
    def test_writes_jsonl(self, tmp_path: Path) -> None:
        logger = ShadowSignalLogger(tmp_path / "shadow")
        live_rec = _live_record()
        tv_rec = _tv_record()

        logger.log_live_signal(live_rec)
        logger.log_tv_signal(tv_rec)

        assert logger.live_path.exists()
        assert logger.tv_path.exists()

        live_lines = logger.live_path.read_text().strip().split("\n")
        tv_lines = logger.tv_path.read_text().strip().split("\n")
        assert len(live_lines) == 1
        assert len(tv_lines) == 1

        restored = json.loads(live_lines[0])
        assert restored["source"] == "live"
        assert restored["symbol"] == "EURUSD"

    def test_read_signals(self, tmp_path: Path) -> None:
        logger = ShadowSignalLogger(tmp_path / "shadow")
        for i in range(3):
            logger.log_live_signal(_live_record(price=1.1 + i * 0.001))

        signals = logger.read_live_signals()
        assert len(signals) == 3
        assert signals[0].price == pytest.approx(1.1)
        assert signals[2].price == pytest.approx(1.102)

    def test_read_empty(self, tmp_path: Path) -> None:
        logger = ShadowSignalLogger(tmp_path / "shadow")
        assert logger.read_live_signals() == []
        assert logger.read_tv_signals() == []

    def test_creates_directory(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "deep" / "nested" / "shadow"
        logger = ShadowSignalLogger(log_dir)
        logger.log_live_signal(_live_record())
        assert log_dir.exists()

    def test_read_signals_with_since_filter(self, tmp_path: Path) -> None:
        """Only signals with timestamp >= since are returned."""
        logger = ShadowSignalLogger(tmp_path / "shadow")
        t_base = 1000000.0
        for i in range(5):
            logger.log_live_signal(_live_record(ts=t_base + i * 100))

        # Read all
        all_sigs = logger.read_live_signals()
        assert len(all_sigs) == 5

        # Read only signals from t_base + 200 onward
        recent = logger.read_live_signals(since=t_base + 200)
        assert len(recent) == 3
        assert recent[0].timestamp == pytest.approx(t_base + 200)

    def test_read_signals_since_none_returns_all(self, tmp_path: Path) -> None:
        """since=None returns all signals (backward compatible)."""
        logger = ShadowSignalLogger(tmp_path / "shadow")
        for i in range(3):
            logger.log_live_signal(_live_record(ts=1000.0 + i))
        assert len(logger.read_live_signals(since=None)) == 3


# ---------------------------------------------------------------------------
# ShadowComparator
# ---------------------------------------------------------------------------


class TestShadowComparator:
    def test_perfect_match(self) -> None:
        """Identical signals from both pipelines -> 100% match rate."""
        bar_time = _NOW
        live = [_live_record(side="LONG", bar_time=bar_time)]
        tv = [_tv_record(side="BUY", bar_time=bar_time)]

        comp = ShadowComparator(tolerance_seconds=300.0)
        report = comp.compare(live, tv)

        assert report.match_rate == pytest.approx(1.0)
        assert len(report.matched) == 1
        assert len(report.missed_by_live) == 0
        assert len(report.extra_by_live) == 0

    def test_missed_by_live(self) -> None:
        """TV signal with no matching live signal -> missed."""
        tv = [_tv_record(side="BUY", bar_time=_NOW)]
        live: list[ShadowSignalRecord] = []

        comp = ShadowComparator()
        report = comp.compare(live, tv)

        assert report.match_rate == pytest.approx(0.0)
        assert len(report.missed_by_live) == 1
        assert len(report.matched) == 0

    def test_extra_by_live(self) -> None:
        """Live signal with no matching TV signal -> extra."""
        live = [_live_record(side="LONG", bar_time=_NOW)]
        tv: list[ShadowSignalRecord] = []

        comp = ShadowComparator()
        report = comp.compare(live, tv)

        # No TV signals means match_rate is 0.0 (no signals to compare = no match)
        assert report.match_rate == pytest.approx(0.0)
        assert len(report.extra_by_live) == 1
        assert len(report.matched) == 0

    def test_timing_delta(self) -> None:
        """Verify timing delta is computed as live.timestamp - tv.timestamp."""
        bar_time = _NOW
        live = [_live_record(side="LONG", bar_time=bar_time, ts=_NOW + 10)]
        tv = [_tv_record(side="BUY", bar_time=bar_time, ts=_NOW)]

        comp = ShadowComparator()
        report = comp.compare(live, tv)

        assert len(report.timing_deltas) == 1
        assert report.timing_deltas[0] == pytest.approx(10.0)

    def test_tolerance_window(self) -> None:
        """Signals outside the tolerance window don't match."""
        live = [_live_record(side="LONG", bar_time=_NOW + 600)]
        tv = [_tv_record(side="BUY", bar_time=_NOW)]

        comp = ShadowComparator(tolerance_seconds=300.0)
        report = comp.compare(live, tv)

        assert report.match_rate == pytest.approx(0.0)
        assert len(report.missed_by_live) == 1
        assert len(report.extra_by_live) == 1

    def test_different_symbols_dont_match(self) -> None:
        """Signals for different symbols are not matched."""
        bar_time = _NOW
        live = [_live_record(symbol="GBPUSD", side="LONG", bar_time=bar_time)]
        tv = [_tv_record(symbol="EURUSD", side="BUY", bar_time=bar_time)]

        comp = ShadowComparator()
        report = comp.compare(live, tv)

        assert report.match_rate == pytest.approx(0.0)
        assert len(report.missed_by_live) == 1
        assert len(report.extra_by_live) == 1

    def test_multiple_signals_matching(self) -> None:
        """Multiple signals from both pipelines are matched correctly."""
        times = [_NOW + i * 3600 for i in range(3)]
        live = [_live_record(side="LONG", bar_time=t, ts=t + 5) for t in times]
        tv = [_tv_record(side="BUY", bar_time=t, ts=t) for t in times]

        comp = ShadowComparator()
        report = comp.compare(live, tv)

        assert report.match_rate == pytest.approx(1.0)
        assert len(report.matched) == 3
        assert len(report.timing_deltas) == 3

    def test_different_actions_dont_match(self) -> None:
        """ENTRY BUY and EXIT BUY signals should not match."""
        bar_time = _NOW
        live = [_live_record(side="LONG", action="EXIT", bar_time=bar_time)]
        tv = [_tv_record(side="BUY", action="ENTRY", bar_time=bar_time)]

        comp = ShadowComparator()
        report = comp.compare(live, tv)

        assert report.match_rate == pytest.approx(0.0)
        assert len(report.matched) == 0
        assert len(report.missed_by_live) == 1
        assert len(report.extra_by_live) == 1

    def test_same_action_matches(self) -> None:
        """Same action + same side + same symbol + same bar_time -> match."""
        bar_time = _NOW
        live = [_live_record(side="LONG", action="ENTRY", bar_time=bar_time)]
        tv = [_tv_record(side="BUY", action="ENTRY", bar_time=bar_time)]

        comp = ShadowComparator()
        report = comp.compare(live, tv)

        assert report.match_rate == pytest.approx(1.0)
        assert len(report.matched) == 1


# ---------------------------------------------------------------------------
# ShadowReport
# ---------------------------------------------------------------------------


class TestShadowReport:
    def test_to_dict(self) -> None:
        report = ShadowReport(
            matched=[({}, {})],
            missed_by_live=[{}],
            extra_by_live=[],
            timing_deltas=[5.0, 10.0],
            match_rate=0.5,
            report_time=_NOW,
        )
        d = report.to_dict()
        assert d["matched_count"] == 1
        assert d["missed_by_live_count"] == 1
        assert d["extra_by_live_count"] == 0
        assert d["match_rate"] == 0.5
        assert d["avg_timing_delta"] == 7.5

    def test_to_summary(self) -> None:
        report = ShadowReport(
            matched=[({}, {})],
            missed_by_live=[],
            extra_by_live=[],
            timing_deltas=[3.0],
            match_rate=1.0,
            report_time=_NOW,
        )
        summary = report.to_summary()
        assert "Match rate:" in summary
        assert "100.0%" in summary
        assert "PASS" in summary

    def test_to_summary_fail(self) -> None:
        report = ShadowReport(
            matched=[],
            missed_by_live=[{}],
            extra_by_live=[],
            timing_deltas=[],
            match_rate=0.0,
            report_time=_NOW,
        )
        summary = report.to_summary()
        assert "FAIL" in summary


# ---------------------------------------------------------------------------
# CutoverGate
# ---------------------------------------------------------------------------


class TestCutoverGate:
    def test_not_ready_defaults(self) -> None:
        gate = CutoverGate()
        assert not gate.ready

    def test_ready_when_all_criteria_met(self) -> None:
        gate = CutoverGate(
            shadow_days=7,
            match_rate=0.96,
            matched_count=15,
            missed_bar_closes=0,
            false_positive_blocks=0,
            failure_domains_observed=8,
            operator_approved=True,
        )
        assert gate.ready

    def test_not_ready_insufficient_signals(self) -> None:
        """Gate is not ready if matched_count < min_signals, even with high match_rate."""
        gate = CutoverGate(
            shadow_days=7,
            match_rate=1.0,
            matched_count=3,
            min_signals=10,
            missed_bar_closes=0,
            false_positive_blocks=0,
            failure_domains_observed=8,
            operator_approved=True,
        )
        assert not gate.ready

    def test_not_ready_low_match_rate(self) -> None:
        gate = CutoverGate(
            shadow_days=7,
            match_rate=0.90,
            matched_count=20,
            missed_bar_closes=0,
            false_positive_blocks=0,
            failure_domains_observed=8,
            operator_approved=True,
        )
        assert not gate.ready

    def test_not_ready_without_operator_approval(self) -> None:
        gate = CutoverGate(
            shadow_days=10,
            match_rate=0.99,
            matched_count=50,
            missed_bar_closes=0,
            false_positive_blocks=0,
            failure_domains_observed=8,
            operator_approved=False,
        )
        assert not gate.ready

    def test_to_dict(self) -> None:
        gate = CutoverGate(shadow_days=5, match_rate=0.88)
        d = gate.to_dict()
        assert d["shadow_days"] == 5
        assert d["match_rate"] == 0.88
        assert d["ready"] is False

    def test_summary(self) -> None:
        gate = CutoverGate(
            shadow_days=7,
            match_rate=0.96,
            matched_count=20,
            missed_bar_closes=0,
            false_positive_blocks=0,
            failure_domains_observed=8,
            operator_approved=True,
        )
        s = gate.summary()
        assert "READY FOR CUTOVER" in s
        assert "[x]" in s

    def test_summary_not_ready(self) -> None:
        gate = CutoverGate(shadow_days=3)
        s = gate.summary()
        assert "NOT READY" in s
        assert "[ ]" in s


# ---------------------------------------------------------------------------
# ShadowModeRunner lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_mode_runner_lifecycle(tmp_path: Path) -> None:
    """Create runner, run briefly, stop, verify log files exist."""
    # Mock LiveLoop
    live_loop = MagicMock()
    live_loop._live_agent = MagicMock()
    live_loop._live_agent.execute = AsyncMock()

    run_called = asyncio.Event()

    stop_event = asyncio.Event()

    async def mock_run() -> None:
        run_called.set()
        # Simulate running until stopped
        await stop_event.wait()

    live_loop._running_flag = True
    live_loop.run = mock_run
    live_loop.stop = MagicMock(side_effect=lambda: stop_event.set())

    # Mock WebhookState
    ws = MagicMock()
    ws.agent = MagicMock()
    ws.agent.process_alert = AsyncMock()

    log_dir = tmp_path / "shadow_logs"
    runner = ShadowModeRunner(
        live_loop=live_loop,
        webhook_state=ws,
        log_dir=log_dir,
        report_interval=0.1,  # fast reports for testing
    )

    # Write some test signals directly
    runner.signal_logger.log_live_signal(_live_record())
    runner.signal_logger.log_tv_signal(_tv_record())

    # Run briefly then stop
    async def stop_after_delay() -> None:
        await run_called.wait()
        await asyncio.sleep(0.25)
        runner.stop()

    await asyncio.gather(
        asyncio.wait_for(runner.run(), timeout=3.0),
        stop_after_delay(),
    )

    # Verify log files were created
    assert runner.signal_logger.live_path.exists()
    assert runner.signal_logger.tv_path.exists()

    # Verify at least one report was generated
    report = runner.get_latest_report()
    assert report is not None
    assert report.match_rate >= 0.0
