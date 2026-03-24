"""Tests for novatrade.runtime.live_loop — three-loop async orchestrator.

Covers:
    1. Integration: mock ticks -> bar close -> entry signal -> order execution
    2. Exit signal flow
    3. Queue decoupling: slow execution doesn't block ticks
    4. Health gate: blocks signals when feed unhealthy
    5. Duplicate signal suppression
    6. Error handling: adapter exception doesn't crash loop
    7. Stop/shutdown: queue drains, loops stop cleanly
    8. Metrics accumulation
    9. Multiple timeframes: H1 and H4
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from novatrade.data.bar_aggregator import BarAggregator
from novatrade.data.tick import Tick
from novatrade.execution.trading_agent import AgentResult, AgentState
from novatrade.monitor.feed_health import FeedHealthSupervisor, FeedState
from novatrade.runtime.live_loop import LiveLoop, LiveMetrics
from novatrade.strategy.live_engine import LiveSignal, SignalType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A base timestamp on a Thursday so forex market is open.
_BASE_TS = 1_711_036_800.0  # 2024-03-21 16:00:00 UTC (Thursday)
_H1 = 3600
_H4 = 14400


def _tick(symbol: str = "EURUSD", ts: float = 0.0, bid: float = 1.10000, ask: float = 1.10020) -> Tick:
    """Create a test tick."""
    return Tick(symbol=symbol, timestamp=ts or _BASE_TS, bid=bid, ask=ask)


def _permissive_supervisor() -> MagicMock:
    """A mocked FeedHealthSupervisor that allows everything through.

    Use this for tests that need signals to flow through the pipeline
    without being blocked by clock-drift or staleness checks.
    """
    sup = MagicMock(spec=FeedHealthSupervisor)
    sup.on_tick.return_value = FeedState.HEALTHY
    sup.is_tradeable.return_value = True
    sup.is_duplicate_signal.return_value = False
    sup.check_staleness.return_value = {}
    sup.stats = {"tracked_symbols": 1, "healthy": 1, "unhealthy": 0}
    return sup


def _entry_signal(
    symbol: str = "EURUSD",
    side: str = "LONG",
) -> LiveSignal:
    return LiveSignal(
        signal_type=SignalType.ENTRY,
        side=side,
        symbol=symbol,
        entry_price=1.10050,
        stop_loss=1.09900,
        volume=0.10,
    )


def _exit_signal(
    symbol: str = "EURUSD",
    side: str = "LONG",
) -> LiveSignal:
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


def _rejected_result(reason: str = "risk_denied: daily_loss") -> AgentResult:
    return AgentResult(
        success=False,
        state_before=AgentState.FLAT,
        state_after=AgentState.FLAT,
        rejected_reason=reason,
    )


def _vetoed_result() -> AgentResult:
    return AgentResult(
        success=False,
        state_before=AgentState.FLAT,
        state_after=AgentState.FLAT,
        rejected_reason="supervisor_veto: max_positions",
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


# ---------------------------------------------------------------------------
# LiveMetrics tests
# ---------------------------------------------------------------------------


class TestLiveMetrics:
    def test_defaults(self) -> None:
        m = LiveMetrics()
        assert m.ticks == 0
        assert m.bars_h1 == 0
        assert m.signals_entry == 0
        assert m.approved == 0
        assert m.errors == 0

    def test_uptime(self) -> None:
        m = LiveMetrics(started_at=time.time() - 10)
        assert m.uptime_seconds >= 9.9

    def test_to_dict_keys(self) -> None:
        m = LiveMetrics()
        d = m.to_dict()
        expected = {
            "ticks",
            "bars_h1",
            "bars_h4",
            "signals_entry",
            "signals_exit",
            "signals_modify_sl",
            "signals_cancel",
            "approved",
            "rejected",
            "vetoed",
            "errors",
            "queue_depth",
            "uptime_seconds",
        }
        assert set(d.keys()) == expected


# ---------------------------------------------------------------------------
# LiveLoop construction
# ---------------------------------------------------------------------------


class TestLiveLoopInit:
    def test_initial_state(self) -> None:
        poller = MockPoller()
        agg = BarAggregator(timeframes=["H1"])
        sup = _permissive_supervisor()
        engine = MagicMock()
        engine.snapshot.return_value = {}
        agent = MagicMock()

        loop = LiveLoop(
            poller=poller,
            aggregator=agg,
            supervisor=sup,
            strategy_engine=engine,
            live_agent=agent,
            health_interval=5.0,
            queue_maxsize=50,
        )

        assert not loop.running
        assert loop.metrics.ticks == 0

    def test_snapshot_keys(self) -> None:
        poller = MockPoller()
        agg = BarAggregator(timeframes=["H1"])
        sup = _permissive_supervisor()
        engine = MagicMock()
        engine.snapshot.return_value = {}
        agent = MagicMock()

        loop = LiveLoop(
            poller=poller,
            aggregator=agg,
            supervisor=sup,
            strategy_engine=engine,
            live_agent=agent,
        )
        snap = loop.snapshot()
        assert "running" in snap
        assert "metrics" in snap
        assert "strategy_state" in snap
        assert "feed_health" in snap
        assert "poller" in snap
        assert "aggregator" in snap
        assert "queue_depth" in snap


# ---------------------------------------------------------------------------
# Integration: ticks -> bar close -> signal -> execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_entry_signal() -> None:
    """Full pipeline: ticks that cross a bar boundary trigger entry signal."""
    bar_open = (_BASE_TS // _H1) * _H1
    tick1 = _tick(ts=bar_open + 10, bid=1.10000, ask=1.10020)
    tick2 = _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120)

    poller = MockPoller(ticks=[tick1, tick2])
    agg = BarAggregator(timeframes=["H1"])
    sup = _permissive_supervisor()
    engine = MagicMock()
    signal = _entry_signal()
    engine.on_bar.return_value = [signal]
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
    )

    await asyncio.wait_for(loop.run(), timeout=2.0)

    assert loop.metrics.ticks == 2
    assert loop.metrics.bars_h1 == 1
    assert loop.metrics.signals_entry == 1
    agent.execute.assert_called_once_with(signal)
    assert loop.metrics.approved == 1


@pytest.mark.asyncio
async def test_exit_signal_flow() -> None:
    """Exit signal goes through the full pipeline."""
    bar_open = (_BASE_TS // _H1) * _H1
    tick1 = _tick(ts=bar_open + 10, bid=1.10000, ask=1.10020)
    tick2 = _tick(ts=bar_open + _H1 + 10, bid=1.10200, ask=1.10220)

    poller = MockPoller(ticks=[tick1, tick2])
    agg = BarAggregator(timeframes=["H1"])
    sup = _permissive_supervisor()
    engine = MagicMock()
    signal = _exit_signal()
    engine.on_bar.return_value = [signal]
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
    )

    await asyncio.wait_for(loop.run(), timeout=2.0)

    assert loop.metrics.signals_exit == 1
    agent.execute.assert_called_once_with(signal)
    assert loop.metrics.approved == 1


# ---------------------------------------------------------------------------
# Queue decoupling: slow execution doesn't block tick processing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_decoupling_slow_execution() -> None:
    """Slow order execution must not block the tick pipeline."""
    bar_open = (_BASE_TS // _H1) * _H1
    ticks = [
        _tick(ts=bar_open + 10, bid=1.10000, ask=1.10020),
        _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120),
        _tick(ts=bar_open + 2 * _H1 + 10, bid=1.10200, ask=1.10220),
    ]

    poller = MockPoller(ticks=ticks)
    agg = BarAggregator(timeframes=["H1"])
    sup = _permissive_supervisor()
    engine = MagicMock()
    engine.on_bar.return_value = [_entry_signal()]
    engine.snapshot.return_value = {}

    executed: list[LiveSignal] = []

    async def slow_execute(signal: LiveSignal) -> AgentResult:
        await asyncio.sleep(0.05)
        executed.append(signal)
        return _ok_result()

    agent = MagicMock()
    agent.execute = AsyncMock(side_effect=slow_execute)

    loop = LiveLoop(
        poller=poller,
        aggregator=agg,
        supervisor=sup,
        strategy_engine=engine,
        live_agent=agent,
        health_interval=60.0,
    )

    await asyncio.wait_for(loop.run(), timeout=3.0)

    assert loop.metrics.ticks == 3
    assert loop.metrics.signals_entry == 2
    assert len(executed) == 2


# ---------------------------------------------------------------------------
# Health gate: blocks signals when feed unhealthy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_gate_blocks_signals() -> None:
    """Signals are NOT generated when the feed is unhealthy."""
    bar_open = (_BASE_TS // _H1) * _H1
    tick1 = _tick(ts=bar_open + 10, bid=1.10000, ask=1.10020)
    tick2 = _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120)

    poller = MockPoller(ticks=[tick1, tick2])
    agg = BarAggregator(timeframes=["H1"])

    # Supervisor that always reports unhealthy.
    sup = MagicMock(spec=FeedHealthSupervisor)
    sup.on_tick.return_value = FeedState.STALE
    sup.is_tradeable.return_value = False
    sup.is_duplicate_signal.return_value = False
    sup.check_staleness.return_value = {"EURUSD": FeedState.STALE}
    sup.stats = {"tracked_symbols": 1, "healthy": 0, "unhealthy": 1}

    engine = MagicMock()
    engine.on_bar.return_value = [_entry_signal()]
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
    )

    await asyncio.wait_for(loop.run(), timeout=2.0)

    # Tick pipeline processes ticks but signals never reach the agent.
    agent.execute.assert_not_called()
    assert loop.metrics.signals_entry == 0


# ---------------------------------------------------------------------------
# Duplicate signal suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_signal_suppression() -> None:
    """Duplicate signals are suppressed by the supervisor."""
    bar_open = (_BASE_TS // _H1) * _H1
    tick1 = _tick(ts=bar_open + 10)
    tick2 = _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120)

    poller = MockPoller(ticks=[tick1, tick2])
    agg = BarAggregator(timeframes=["H1"])

    sup = MagicMock(spec=FeedHealthSupervisor)
    sup.on_tick.return_value = FeedState.HEALTHY
    sup.is_tradeable.return_value = True
    sup.is_duplicate_signal.return_value = True  # always duplicates
    sup.check_staleness.return_value = {}
    sup.stats = {}

    engine = MagicMock()
    engine.on_bar.return_value = [_entry_signal()]
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
    )

    await asyncio.wait_for(loop.run(), timeout=2.0)

    agent.execute.assert_not_called()
    assert loop.metrics.signals_entry == 0


# ---------------------------------------------------------------------------
# Error handling: exceptions don't crash the loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_pipeline_error_doesnt_crash() -> None:
    """An exception on one tick doesn't crash the whole loop."""
    bar_open = (_BASE_TS // _H1) * _H1
    tick1 = _tick(ts=bar_open + 10)
    tick2 = _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120)

    poller = MockPoller(ticks=[tick1, tick2])
    agg = BarAggregator(timeframes=["H1"])

    sup = MagicMock(spec=FeedHealthSupervisor)
    # First on_tick raises, second succeeds.
    sup.on_tick.side_effect = [RuntimeError("boom"), FeedState.HEALTHY]
    sup.is_tradeable.return_value = True
    sup.is_duplicate_signal.return_value = False
    sup.check_staleness.return_value = {}
    sup.stats = {}

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
    )

    await asyncio.wait_for(loop.run(), timeout=2.0)

    assert loop.metrics.ticks >= 1
    assert loop.metrics.errors >= 1


@pytest.mark.asyncio
async def test_order_execution_error_doesnt_crash() -> None:
    """An exception in order execution doesn't crash Loop 2."""
    bar_open = (_BASE_TS // _H1) * _H1
    tick1 = _tick(ts=bar_open + 10)
    tick2 = _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120)

    poller = MockPoller(ticks=[tick1, tick2])
    agg = BarAggregator(timeframes=["H1"])
    sup = _permissive_supervisor()

    engine = MagicMock()
    engine.on_bar.return_value = [_entry_signal()]
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
    )

    await asyncio.wait_for(loop.run(), timeout=2.0)

    assert loop.metrics.errors >= 1


# ---------------------------------------------------------------------------
# Stop/shutdown: queue drains on shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graceful_shutdown() -> None:
    """stop() causes all loops to exit cleanly."""
    bar_open = (_BASE_TS // _H1) * _H1
    ticks = [_tick(ts=bar_open + i * 10) for i in range(5)]

    poller = MockPoller(ticks=ticks, delay=0.05)
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
    )

    async def stop_after_delay() -> None:
        await asyncio.sleep(0.1)
        loop.stop()

    await asyncio.gather(
        asyncio.wait_for(loop.run(), timeout=2.0),
        stop_after_delay(),
    )

    assert not loop.running
    assert not poller.running


@pytest.mark.asyncio
async def test_shutdown_drains_queue() -> None:
    """Signals already in the queue are drained on shutdown."""
    bar_open = (_BASE_TS // _H1) * _H1
    tick1 = _tick(ts=bar_open + 10)
    tick2 = _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120)

    poller = MockPoller(ticks=[tick1, tick2])
    agg = BarAggregator(timeframes=["H1"])
    sup = _permissive_supervisor()

    engine = MagicMock()
    engine.on_bar.return_value = [_entry_signal()]
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
    )

    await asyncio.wait_for(loop.run(), timeout=2.0)

    assert agent.execute.call_count >= 1
    assert loop.metrics.approved >= 1


# ---------------------------------------------------------------------------
# Metrics accumulation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_accumulation() -> None:
    """Verify ticks, bars, signals, and queue_depth all increment correctly."""
    bar_open = (_BASE_TS // _H1) * _H1
    ticks = [
        _tick(ts=bar_open + 10, bid=1.10000, ask=1.10020),
        _tick(ts=bar_open + 20, bid=1.10010, ask=1.10030),
        _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120),
        _tick(ts=bar_open + _H1 + 20, bid=1.10110, ask=1.10130),
        _tick(ts=bar_open + 2 * _H1 + 10, bid=1.10200, ask=1.10220),
    ]

    poller = MockPoller(ticks=ticks)
    agg = BarAggregator(timeframes=["H1"])
    sup = _permissive_supervisor()

    call_count = 0
    signals_sequence: list[list[LiveSignal]] = [
        [_entry_signal()],  # first bar close: entry
        [_modify_signal()],  # second bar close: modify SL
    ]

    def on_bar_side_effect(bar: Any, timeframe: str) -> list[LiveSignal]:
        nonlocal call_count
        if call_count < len(signals_sequence):
            result = signals_sequence[call_count]
            call_count += 1
            return result
        return []

    engine = MagicMock()
    engine.on_bar.side_effect = on_bar_side_effect
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
    )

    await asyncio.wait_for(loop.run(), timeout=2.0)

    m = loop.metrics
    assert m.ticks == 5
    assert m.bars_h1 == 2
    assert m.signals_entry == 1
    assert m.signals_modify_sl == 1
    assert m.approved == 2


@pytest.mark.asyncio
async def test_metrics_rejected_and_vetoed() -> None:
    """Rejected and vetoed signals are counted correctly."""
    bar_open = (_BASE_TS // _H1) * _H1
    ticks = [
        _tick(ts=bar_open + 10),
        _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120),
        _tick(ts=bar_open + _H1 + 20, bid=1.10110, ask=1.10130),
        _tick(ts=bar_open + 2 * _H1 + 10, bid=1.10200, ask=1.10220),
    ]

    poller = MockPoller(ticks=ticks)
    agg = BarAggregator(timeframes=["H1"])
    sup = _permissive_supervisor()

    engine = MagicMock()
    engine.on_bar.return_value = [_entry_signal()]
    engine.snapshot.return_value = {}

    agent = MagicMock()
    agent.execute = AsyncMock(
        side_effect=[_rejected_result(), _vetoed_result()],
    )

    loop = LiveLoop(
        poller=poller,
        aggregator=agg,
        supervisor=sup,
        strategy_engine=engine,
        live_agent=agent,
        health_interval=60.0,
    )

    await asyncio.wait_for(loop.run(), timeout=2.0)

    m = loop.metrics
    assert m.rejected == 1
    assert m.vetoed == 1
    assert m.approved == 0


# ---------------------------------------------------------------------------
# Multiple timeframes: H1 and H4
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_timeframes_h1_h4() -> None:
    """Both H1 and H4 bars pass through correctly."""
    h4_open = (_BASE_TS // _H4) * _H4
    ticks = [
        _tick(ts=h4_open + 10, bid=1.10000, ask=1.10020),
        _tick(ts=h4_open + _H1 + 10, bid=1.10100, ask=1.10120),
        _tick(ts=h4_open + 2 * _H1 + 10, bid=1.10200, ask=1.10220),
        _tick(ts=h4_open + 3 * _H1 + 10, bid=1.10300, ask=1.10320),
        _tick(ts=h4_open + _H4 + 10, bid=1.10400, ask=1.10420),
    ]

    poller = MockPoller(ticks=ticks)
    agg = BarAggregator(timeframes=["H1", "H4"])
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
    )

    await asyncio.wait_for(loop.run(), timeout=2.0)

    assert loop.metrics.ticks == 5
    assert loop.metrics.bars_h1 == 4
    assert loop.metrics.bars_h4 == 1


# ---------------------------------------------------------------------------
# Queue full: signal dropped with warning, tick pipeline continues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_full_drops_signal() -> None:
    """When the queue is full, signals are dropped instead of blocking."""
    bar_open = (_BASE_TS // _H1) * _H1
    ticks = []
    for i in range(5):
        ticks.append(_tick(ts=bar_open + i * _H1 + 10, bid=1.10000 + i * 0.001, ask=1.10020 + i * 0.001))

    poller = MockPoller(ticks=ticks)
    agg = BarAggregator(timeframes=["H1"])
    sup = _permissive_supervisor()

    engine = MagicMock()
    engine.on_bar.return_value = [_entry_signal()]
    engine.snapshot.return_value = {}

    # Agent that never completes (to fill the queue).
    execute_event = asyncio.Event()

    async def blocking_execute(signal: LiveSignal) -> AgentResult:
        await execute_event.wait()
        return _ok_result()

    agent = MagicMock()
    agent.execute = AsyncMock(side_effect=blocking_execute)

    loop = LiveLoop(
        poller=poller,
        aggregator=agg,
        supervisor=sup,
        strategy_engine=engine,
        live_agent=agent,
        health_interval=60.0,
        queue_maxsize=2,
    )

    async def run_and_stop() -> None:
        task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.3)
        execute_event.set()
        loop.stop()
        await asyncio.wait_for(task, timeout=2.0)

    await run_and_stop()

    # All ticks processed even though queue was full.
    assert loop.metrics.ticks == 5


# ---------------------------------------------------------------------------
# Running property tracks state correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_property() -> None:
    """The running property reflects the loop's state."""
    poller = MockPoller(ticks=[])
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
    )

    assert not loop.running
    await asyncio.wait_for(loop.run(), timeout=1.0)
    assert not loop.running


# ---------------------------------------------------------------------------
# Health monitor loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_monitor_logs_unhealthy() -> None:
    """Health monitor detects and warns about unhealthy symbols."""
    ticks = [_tick(ts=_BASE_TS + i) for i in range(10)]
    poller = MockPoller(ticks=ticks, delay=0.05)
    agg = BarAggregator(timeframes=["H1"])

    sup = MagicMock(spec=FeedHealthSupervisor)
    sup.on_tick.return_value = FeedState.HEALTHY
    sup.is_tradeable.return_value = True
    sup.is_duplicate_signal.return_value = False
    sup.check_staleness.return_value = {"EURUSD": FeedState.STALE}
    sup.stats = {}

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
        health_interval=0.05,
    )

    await asyncio.wait_for(loop.run(), timeout=2.0)

    assert sup.check_staleness.call_count >= 1


# ---------------------------------------------------------------------------
# Modify SL signal type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modify_sl_signal() -> None:
    """MODIFY_SL signals are counted and executed correctly."""
    bar_open = (_BASE_TS // _H1) * _H1
    tick1 = _tick(ts=bar_open + 10)
    tick2 = _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120)

    poller = MockPoller(ticks=[tick1, tick2])
    agg = BarAggregator(timeframes=["H1"])
    sup = _permissive_supervisor()

    engine = MagicMock()
    engine.on_bar.return_value = [_modify_signal()]
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
    )

    await asyncio.wait_for(loop.run(), timeout=2.0)

    assert loop.metrics.signals_modify_sl == 1
    assert loop.metrics.approved == 1


# ---------------------------------------------------------------------------
# No signals when strategy returns empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_signals_when_strategy_returns_empty() -> None:
    """When strategy returns no signals, nothing reaches the agent."""
    bar_open = (_BASE_TS // _H1) * _H1
    tick1 = _tick(ts=bar_open + 10)
    tick2 = _tick(ts=bar_open + _H1 + 10, bid=1.10100, ask=1.10120)

    poller = MockPoller(ticks=[tick1, tick2])
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
    )

    await asyncio.wait_for(loop.run(), timeout=2.0)

    agent.execute.assert_not_called()
    assert loop.metrics.signals_entry == 0
    assert loop.metrics.approved == 0


# ---------------------------------------------------------------------------
# HIGH #3: Critical signals (EXIT, MODIFY_SL) force-enqueue when queue full
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_full_exit_signal_force_enqueued() -> None:
    """EXIT signals must be force-enqueued (blocking) when queue is full,
    not dropped like ENTRY signals."""
    bar_open = (_BASE_TS // _H1) * _H1
    # Generate enough ticks for multiple bar closes
    ticks = []
    for i in range(4):
        ticks.append(
            _tick(
                ts=bar_open + i * _H1 + 10,
                bid=1.10000 + i * 0.001,
                ask=1.10020 + i * 0.001,
            )
        )

    poller = MockPoller(ticks=ticks)
    agg = BarAggregator(timeframes=["H1"])
    sup = _permissive_supervisor()

    # Return alternating entry/exit signals
    call_count = 0

    def on_bar_returns(bar, timeframe):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [_entry_signal()]
        elif call_count == 2:
            return [_exit_signal()]
        return []

    engine = MagicMock()
    engine.on_bar.side_effect = on_bar_returns
    engine.snapshot.return_value = {}

    # Use a very slow agent so queue fills up
    execute_calls = []

    async def slow_execute(signal):
        execute_calls.append(signal)
        await asyncio.sleep(0.01)
        return _ok_result()

    agent = MagicMock()
    agent.execute = AsyncMock(side_effect=slow_execute)

    # Very small queue to trigger full condition
    loop = LiveLoop(
        poller=poller,
        aggregator=agg,
        supervisor=sup,
        strategy_engine=engine,
        live_agent=agent,
        health_interval=60.0,
        queue_maxsize=1,
    )

    await asyncio.wait_for(loop.run(), timeout=3.0)

    # The EXIT signal should have been processed (not dropped).
    # Check that at least one EXIT signal made it through.
    exit_signals = [s for s in execute_calls if s.signal_type == SignalType.EXIT]
    assert len(exit_signals) >= 1, (
        f"EXIT signal should be force-enqueued, not dropped. Executed: {[s.signal_type.value for s in execute_calls]}"
    )


# ---------------------------------------------------------------------------
# HIGH #2: Order execution loop handles shutdown race correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_execution_exits_on_sentinel() -> None:
    """Order execution loop properly exits when sentinel (None) is received."""
    poller = MockPoller(ticks=[])
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
    )

    # Run and stop — should exit cleanly without hanging
    async def stop_soon():
        await asyncio.sleep(0.1)
        loop.stop()

    await asyncio.gather(
        asyncio.wait_for(loop.run(), timeout=2.0),
        stop_soon(),
    )

    assert not loop.running
