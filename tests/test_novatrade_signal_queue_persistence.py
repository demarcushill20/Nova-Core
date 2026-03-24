"""Tests for signal queue persistence in StateStore and LiveLoop.

Covers:
    1. StateStore signal_queue CRUD operations
    2. LiveLoop persists critical signals (EXIT, MODIFY_SL) to StateStore
    3. LiveLoop restores persisted signals on restart
    4. LiveLoop removes signals after successful execution
    5. Non-critical signals (ENTRY) are NOT persisted
    6. Crash recovery scenario
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from novatrade.data.bar_aggregator import BarAggregator
from novatrade.data.tick import Tick
from novatrade.execution.trading_agent import AgentResult, AgentState
from novatrade.monitor.feed_health import FeedHealthSupervisor, FeedState
from novatrade.runtime.live_loop import LiveLoop
from novatrade.storage.state_store import StateStore
from novatrade.strategy.live_engine import LiveSignal, SignalType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = 1_711_036_800.0  # 2024-03-21 16:00:00 UTC (Thursday)
_H1 = 3600


def _tick(
    symbol: str = "EURUSD",
    ts: float = 0.0,
    bid: float = 1.10000,
    ask: float = 1.10020,
) -> Tick:
    return Tick(symbol=symbol, timestamp=ts or _BASE_TS, bid=bid, ask=ask)


def _permissive_supervisor() -> MagicMock:
    sup = MagicMock(spec=FeedHealthSupervisor)
    sup.on_tick.return_value = FeedState.HEALTHY
    sup.is_tradeable.return_value = True
    sup.is_duplicate_signal.return_value = False
    sup.check_staleness.return_value = {}
    sup.stats = {"tracked_symbols": 1, "healthy": 1, "unhealthy": 0}
    return sup


def _entry_signal(symbol: str = "EURUSD", side: str = "LONG") -> LiveSignal:
    return LiveSignal(
        signal_type=SignalType.ENTRY,
        side=side,
        symbol=symbol,
        entry_price=1.10050,
        stop_loss=1.09900,
        volume=0.10,
    )


def _exit_signal(symbol: str = "EURUSD", side: str = "LONG") -> LiveSignal:
    return LiveSignal(
        signal_type=SignalType.EXIT,
        side=side,
        symbol=symbol,
        exit_price=1.10200,
        exit_reason="TIME_STOP",
    )


def _modify_signal(symbol: str = "EURUSD", side: str = "LONG") -> LiveSignal:
    return LiveSignal(
        signal_type=SignalType.MODIFY_SL,
        side=side,
        symbol=symbol,
        new_stop=1.10000,
        metadata={"old_stop": 1.09900, "atr": 0.00120},
    )


def _ok_result() -> AgentResult:
    return AgentResult(
        success=True,
        state_before=AgentState.FLAT,
        state_after=AgentState.PENDING_LONG,
    )


class MockPoller:
    """Mock TickBatchPoller that yields a controlled set of ticks then stops."""

    def __init__(self, ticks: list[Tick] | None = None, *, delay: float = 0.0) -> None:
        self._ticks = ticks or []
        self._running = False
        self._delay = delay
        self.ticks_yielded = 0
        self.polls = 0
        self.errors = 0

    def stop(self) -> None:
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def stream(self) -> AsyncIterator[Tick]:
        self._running = True
        try:
            for tick in self._ticks:
                if not self._running:
                    break
                if self._delay > 0:
                    await asyncio.sleep(self._delay)
                self.ticks_yielded += 1
                yield tick
        finally:
            self._running = False

    @property
    def stats(self) -> dict:
        return {
            "running": self._running,
            "symbols": [],
            "interval": 0.5,
            "ticks_yielded": self.ticks_yielded,
            "polls": self.polls,
            "errors": self.errors,
        }


# ===================================================================
# TestSignalQueueStore — StateStore CRUD for signal_queue
# ===================================================================


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "test_signal.db")


class TestSignalQueueStore:
    """Test StateStore's signal_queue CRUD operations in isolation."""

    def test_signal_queue_table_exists(self, store):
        conn = sqlite3.connect(str(store._db_path))
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert "signal_queue" in tables

    def test_save_and_load_signal(self, store):
        signal = _exit_signal()
        signal_id = store.save_queued_signal(signal)
        assert signal_id is not None
        assert signal_id > 0

        loaded = store.load_queued_signals()
        assert len(loaded) == 1
        sid, d = loaded[0]
        assert sid == signal_id
        assert d["signal_type"] == "EXIT"
        assert d["side"] == "LONG"
        assert d["symbol"] == "EURUSD"
        assert d["exit_price"] == 1.10200
        assert d["exit_reason"] == "TIME_STOP"

    def test_save_multiple_signals_ordered(self, store):
        s1 = _exit_signal(side="LONG")
        s2 = _modify_signal(side="SHORT")
        s3 = _exit_signal(side="SHORT")

        id1 = store.save_queued_signal(s1)
        id2 = store.save_queued_signal(s2)
        id3 = store.save_queued_signal(s3)

        loaded = store.load_queued_signals()
        assert len(loaded) == 3
        # Ordered by signal_id ascending
        assert loaded[0][0] == id1
        assert loaded[1][0] == id2
        assert loaded[2][0] == id3
        assert loaded[0][1]["side"] == "LONG"
        assert loaded[1][1]["signal_type"] == "MODIFY_SL"
        assert loaded[2][1]["side"] == "SHORT"

    def test_remove_signal(self, store):
        signal = _exit_signal()
        signal_id = store.save_queued_signal(signal)
        assert signal_id is not None

        store.remove_queued_signal(signal_id)
        loaded = store.load_queued_signals()
        assert len(loaded) == 0

    def test_clear_signal_queue(self, store):
        for _ in range(3):
            store.save_queued_signal(_exit_signal())
        assert len(store.load_queued_signals()) == 3

        store.clear_signal_queue()
        assert len(store.load_queued_signals()) == 0

    def test_load_empty_queue(self, store):
        loaded = store.load_queued_signals()
        assert loaded == []

    def test_metadata_round_trip(self, store):
        signal = _modify_signal()
        store.save_queued_signal(signal)
        loaded = store.load_queued_signals()
        assert len(loaded) == 1
        _, d = loaded[0]
        assert d["metadata"] == {"old_stop": 1.09900, "atr": 0.00120}


# ===================================================================
# TestLiveLoopSignalPersistence — LiveLoop integration with StateStore
# ===================================================================


def _make_loop(
    ticks,
    signals_per_bar,
    tmp_path,
    *,
    with_store=True,
    queue_maxsize=100,
    agent_result=None,
):
    """Build a LiveLoop wired for testing signal persistence."""
    poller = MockPoller(ticks=ticks)
    agg = BarAggregator(timeframes=["H1"])
    sup = _permissive_supervisor()

    engine = MagicMock()
    engine.on_bar.return_value = signals_per_bar
    engine.snapshot.return_value = {}

    agent = MagicMock()
    agent.execute = AsyncMock(return_value=agent_result or _ok_result())

    state_store = StateStore(tmp_path / "test_loop.db") if with_store else None

    loop = LiveLoop(
        poller=poller,
        aggregator=agg,
        supervisor=sup,
        strategy_engine=engine,
        live_agent=agent,
        health_interval=60.0,
        queue_maxsize=queue_maxsize,
        state_store=state_store,
    )
    return loop, state_store, agent


class TestLiveLoopSignalPersistence:
    """Test LiveLoop persists critical signals and removes them after execution."""

    @pytest.mark.asyncio
    async def test_exit_signal_persisted(self, tmp_path):
        """EXIT signal should be persisted to DB during enqueue and removed after execution."""
        bar_open = (_BASE_TS // _H1) * _H1
        ticks = [
            _tick(ts=bar_open + 10, bid=1.10000, ask=1.10020),
            _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120),
        ]
        loop, store, agent = _make_loop(ticks, [_exit_signal()], tmp_path)

        await asyncio.wait_for(loop.run(), timeout=2.0)

        # After execution completes, signal should be removed from DB
        remaining = store.load_queued_signals()
        assert len(remaining) == 0
        # But the signal was executed
        assert agent.execute.call_count == 1
        assert loop.metrics.signals_exit == 1

    @pytest.mark.asyncio
    async def test_modify_sl_signal_persisted(self, tmp_path):
        """MODIFY_SL signal should also be persisted (it is critical)."""
        bar_open = (_BASE_TS // _H1) * _H1
        ticks = [
            _tick(ts=bar_open + 10, bid=1.10000, ask=1.10020),
            _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120),
        ]
        loop, store, agent = _make_loop(ticks, [_modify_signal()], tmp_path)

        await asyncio.wait_for(loop.run(), timeout=2.0)

        remaining = store.load_queued_signals()
        assert len(remaining) == 0
        assert agent.execute.call_count == 1
        assert loop.metrics.signals_modify_sl == 1

    @pytest.mark.asyncio
    async def test_entry_signal_not_persisted(self, tmp_path):
        """ENTRY signals are NOT critical and should NOT be persisted."""
        bar_open = (_BASE_TS // _H1) * _H1
        ticks = [
            _tick(ts=bar_open + 10, bid=1.10000, ask=1.10020),
            _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120),
        ]
        loop, store, agent = _make_loop(ticks, [_entry_signal()], tmp_path)

        await asyncio.wait_for(loop.run(), timeout=2.0)

        # ENTRY is not critical — nothing persisted
        remaining = store.load_queued_signals()
        assert len(remaining) == 0
        # But signal was still executed normally
        assert agent.execute.call_count == 1
        assert loop.metrics.signals_entry == 1

    @pytest.mark.asyncio
    async def test_restore_signals_on_startup(self, tmp_path):
        """Pre-populated DB signals should be restored into the queue on startup."""
        db_path = tmp_path / "restore.db"
        store = StateStore(db_path)

        # Pre-populate with an EXIT signal (simulating crash recovery)
        exit_sig = _exit_signal()
        signal_id = store.save_queued_signal(exit_sig)
        assert signal_id is not None

        # Verify signal is in DB
        assert len(store.load_queued_signals()) == 1

        # Now create a LiveLoop with the same store — it should restore signals
        poller = MockPoller(ticks=[])  # No new ticks
        agg = BarAggregator(timeframes=["H1"])
        sup = _permissive_supervisor()
        engine = MagicMock()
        engine.on_bar.return_value = []
        engine.snapshot.return_value = {}

        agent = MagicMock()
        agent.execute = AsyncMock(return_value=_ok_result())

        loop = LiveLoop(
            poller=poller,
            aggregator=agg,
            supervisor=sup,
            strategy_engine=engine,
            live_agent=agent,
            health_interval=60.0,
            state_store=store,
        )

        await asyncio.wait_for(loop.run(), timeout=2.0)

        # The restored signal should have been executed
        assert agent.execute.call_count == 1
        executed_signal = agent.execute.call_args[0][0]
        assert executed_signal.signal_type == SignalType.EXIT
        assert executed_signal.side == "LONG"
        assert executed_signal.symbol == "EURUSD"

        # And removed from DB after execution
        assert len(store.load_queued_signals()) == 0

    @pytest.mark.asyncio
    async def test_loop_without_state_store(self):
        """LiveLoop works fine with state_store=None (backward compat)."""
        bar_open = (_BASE_TS // _H1) * _H1
        ticks = [
            _tick(ts=bar_open + 10, bid=1.10000, ask=1.10020),
            _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120),
        ]
        poller = MockPoller(ticks=ticks)
        agg = BarAggregator(timeframes=["H1"])
        sup = _permissive_supervisor()
        engine = MagicMock()
        engine.on_bar.return_value = [_exit_signal()]
        engine.snapshot.return_value = {}

        agent = MagicMock()
        agent.execute = AsyncMock(return_value=_ok_result())

        loop = LiveLoop(
            poller=poller,
            aggregator=agg,
            supervisor=sup,
            strategy_engine=engine,
            live_agent=agent,
            health_interval=60.0,
            state_store=None,  # explicitly None
        )

        await asyncio.wait_for(loop.run(), timeout=2.0)

        # Should work fine — no errors, signal executed
        assert agent.execute.call_count == 1
        assert loop.metrics.signals_exit == 1
        assert loop.metrics.errors == 0

    @pytest.mark.asyncio
    async def test_signal_removed_after_execution(self, tmp_path):
        """After execution, the signal should be gone from the DB."""
        bar_open = (_BASE_TS // _H1) * _H1
        ticks = [
            _tick(ts=bar_open + 10, bid=1.10000, ask=1.10020),
            _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120),
        ]
        loop, store, agent = _make_loop(ticks, [_exit_signal()], tmp_path)

        # We need to verify the signal exists in DB during execution.
        # Use a side effect that checks DB state before returning.
        db_had_signal_during_exec = []

        async def check_db_and_execute(signal):
            queued = store.load_queued_signals()
            db_had_signal_during_exec.append(len(queued))
            return _ok_result()

        agent.execute = AsyncMock(side_effect=check_db_and_execute)

        await asyncio.wait_for(loop.run(), timeout=2.0)

        # During execution, the signal should have been in DB
        assert db_had_signal_during_exec[0] == 1
        # After execution, it should be removed
        assert len(store.load_queued_signals()) == 0

    @pytest.mark.asyncio
    async def test_crash_recovery_multiple_signals(self, tmp_path):
        """Multiple persisted signals are all restored and executed in order."""
        db_path = tmp_path / "multi_restore.db"
        store = StateStore(db_path)

        # Pre-populate with multiple critical signals
        store.save_queued_signal(_exit_signal(side="LONG"))
        store.save_queued_signal(_modify_signal(side="SHORT"))
        store.save_queued_signal(_exit_signal(side="SHORT"))

        assert len(store.load_queued_signals()) == 3

        poller = MockPoller(ticks=[])
        agg = BarAggregator(timeframes=["H1"])
        sup = _permissive_supervisor()
        engine = MagicMock()
        engine.on_bar.return_value = []
        engine.snapshot.return_value = {}

        executed_signals = []

        async def track_execute(signal):
            executed_signals.append(signal)
            return _ok_result()

        agent = MagicMock()
        agent.execute = AsyncMock(side_effect=track_execute)

        loop = LiveLoop(
            poller=poller,
            aggregator=agg,
            supervisor=sup,
            strategy_engine=engine,
            live_agent=agent,
            health_interval=60.0,
            state_store=store,
        )

        await asyncio.wait_for(loop.run(), timeout=2.0)

        # All 3 signals should be executed
        assert len(executed_signals) == 3
        assert executed_signals[0].signal_type == SignalType.EXIT
        assert executed_signals[0].side == "LONG"
        assert executed_signals[1].signal_type == SignalType.MODIFY_SL
        assert executed_signals[1].side == "SHORT"
        assert executed_signals[2].signal_type == SignalType.EXIT
        assert executed_signals[2].side == "SHORT"

        # DB should be empty after execution
        assert len(store.load_queued_signals()) == 0

    @pytest.mark.asyncio
    async def test_signal_removed_even_on_execution_error(self, tmp_path):
        """Signal is removed from DB even when execution raises an exception."""
        db_path = tmp_path / "error_remove.db"
        store = StateStore(db_path)

        # Pre-populate
        store.save_queued_signal(_exit_signal())
        assert len(store.load_queued_signals()) == 1

        poller = MockPoller(ticks=[])
        agg = BarAggregator(timeframes=["H1"])
        sup = _permissive_supervisor()
        engine = MagicMock()
        engine.on_bar.return_value = []
        engine.snapshot.return_value = {}

        agent = MagicMock()
        agent.execute = AsyncMock(side_effect=RuntimeError("execution boom"))

        loop = LiveLoop(
            poller=poller,
            aggregator=agg,
            supervisor=sup,
            strategy_engine=engine,
            live_agent=agent,
            health_interval=60.0,
            state_store=store,
        )

        await asyncio.wait_for(loop.run(), timeout=2.0)

        # Signal should still be removed from DB (cleanup in finally block)
        assert len(store.load_queued_signals()) == 0
        assert loop.metrics.errors >= 1
