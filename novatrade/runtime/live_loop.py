"""Three-loop async orchestrator for the live trading pipeline.

Phase 5.1 of the Full-Python NovaTrade implementation.  Wires the entire
live pipeline end-to-end: tick ingestion -> bar aggregation -> strategy
signals -> order execution, with a decoupled signal queue and periodic
health monitoring.

Architecture (three concurrent loops via ``asyncio.gather``):

    Loop 1 — Tick Pipeline (non-blocking):
        Polls ticks, feeds health supervisor, aggregates bars, runs strategy,
        and enqueues signals for execution.

    Loop 2 — Order Execution (async consumer):
        Drains the signal queue and delegates each signal to the
        LiveTradingAgent for execution through the full governed pipeline.

    Loop 3 — Health Monitor (periodic):
        Periodically checks feed staleness and logs unhealthy symbols.

Public API:
    - LiveMetrics: runtime counters dataclass
    - LiveLoop: the three-loop orchestrator
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from novatrade.data.bar_aggregator import BarAggregator
from novatrade.data.price_feed import TickBatchPoller
from novatrade.execution.live_trading_agent import LiveTradingAgent
from novatrade.monitor.feed_health import FeedHealthSupervisor, FeedState
from novatrade.strategy.live_engine import LiveStrategyEngine, SignalType

log = logging.getLogger("novatrade.runtime.live_loop")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class LiveMetrics:
    """Counters for the live trading loop."""

    ticks: int = 0
    bars_h1: int = 0
    bars_h4: int = 0
    signals_entry: int = 0
    signals_exit: int = 0
    signals_modify_sl: int = 0
    signals_cancel: int = 0
    approved: int = 0
    rejected: int = 0
    vetoed: int = 0
    errors: int = 0
    queue_depth: int = 0
    started_at: float = field(default_factory=time.time)

    @property
    def uptime_seconds(self) -> float:
        """Seconds since the loop started."""
        return time.time() - self.started_at

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "ticks": self.ticks,
            "bars_h1": self.bars_h1,
            "bars_h4": self.bars_h4,
            "signals_entry": self.signals_entry,
            "signals_exit": self.signals_exit,
            "signals_modify_sl": self.signals_modify_sl,
            "signals_cancel": self.signals_cancel,
            "approved": self.approved,
            "rejected": self.rejected,
            "vetoed": self.vetoed,
            "errors": self.errors,
            "queue_depth": self.queue_depth,
            "uptime_seconds": round(self.uptime_seconds, 1),
        }


# Mapping from SignalType enum to the metrics counter attribute name.
_SIGNAL_COUNTER: dict[SignalType, str] = {
    SignalType.ENTRY: "signals_entry",
    SignalType.EXIT: "signals_exit",
    SignalType.MODIFY_SL: "signals_modify_sl",
    SignalType.CANCEL_PENDING: "signals_cancel",
    SignalType.PENDING_FILL: "signals_entry",  # fills count as entries
}


# ---------------------------------------------------------------------------
# LiveLoop
# ---------------------------------------------------------------------------


class LiveLoop:
    """Three-loop async orchestrator for live trading.

    Wires ``TickBatchPoller`` -> ``BarAggregator`` -> ``LiveStrategyEngine``
    -> signal queue -> ``LiveTradingAgent``, with a parallel health monitor.

    Usage::

        loop = LiveLoop(poller, aggregator, supervisor, engine, agent)
        await loop.run()   # runs until stop() or external cancel
        loop.stop()        # signal graceful shutdown
    """

    def __init__(
        self,
        poller: TickBatchPoller,
        aggregator: BarAggregator,
        supervisor: FeedHealthSupervisor,
        strategy_engine: LiveStrategyEngine,
        live_agent: LiveTradingAgent,
        *,
        health_interval: float = 5.0,
        queue_maxsize: int = 100,
    ) -> None:
        self._poller = poller
        self._aggregator = aggregator
        self._supervisor = supervisor
        self._strategy_engine = strategy_engine
        self._live_agent = live_agent
        self._health_interval = health_interval

        self._signal_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_maxsize)
        self._metrics = LiveMetrics()
        self._running = False
        self._stop_event: asyncio.Event | None = None

    # -- Public API --------------------------------------------------------

    async def run(self) -> None:
        """Start all three loops via ``asyncio.gather``.

        Runs until ``stop()`` is called or all loops raise.
        """
        self._running = True
        self._stop_event = asyncio.Event()
        self._metrics = LiveMetrics()

        log.info(
            "LiveLoop starting: health_interval=%.1fs queue_maxsize=%d",
            self._health_interval,
            self._signal_queue.maxsize,
        )

        try:
            await asyncio.gather(
                self._tick_pipeline(),
                self._order_execution(),
                self._health_monitor(),
            )
        finally:
            self._running = False
            log.info("LiveLoop stopped: %s", self._metrics.to_dict())

    def stop(self) -> None:
        """Signal all loops to stop gracefully.

        Stops the poller (ends tick generation) and places a ``None``
        sentinel on the signal queue so Loop 2 can unblock and drain.
        Sets the stop event to wake up the health monitor immediately.
        """
        self._running = False
        self._poller.stop()

        # Wake the health monitor from its sleep immediately.
        if self._stop_event is not None:
            self._stop_event.set()

        # Non-blocking sentinel — if queue is full we still proceed.
        try:
            self._signal_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

        log.info("LiveLoop stop requested")

    @property
    def metrics(self) -> LiveMetrics:
        """Current runtime metrics."""
        return self._metrics

    @property
    def running(self) -> bool:
        """Whether the loop is currently running."""
        return self._running

    def snapshot(self) -> dict[str, Any]:
        """Point-in-time snapshot of the entire live stack."""
        return {
            "running": self._running,
            "metrics": self._metrics.to_dict(),
            "strategy_state": self._strategy_engine.snapshot(),
            "feed_health": self._supervisor.stats,
            "poller": self._poller.stats,
            "aggregator": self._aggregator.stats,
            "queue_depth": self._signal_queue.qsize(),
        }

    # -- Loop 1: Tick Pipeline ---------------------------------------------

    async def _tick_pipeline(self) -> None:
        """Consume ticks, aggregate bars, generate signals, enqueue."""
        log.info("tick pipeline started")
        try:
            async for tick in self._poller.stream():
                if not self._running:
                    break

                try:
                    self._metrics.ticks += 1
                    log.debug(
                        "tick: %s bid=%.5f ask=%.5f",
                        tick.symbol,
                        tick.bid,
                        tick.ask,
                    )

                    # Feed health assessment
                    self._supervisor.on_tick(tick)

                    # Bar aggregation
                    completed_bars = self._aggregator.on_tick(tick)
                    for timeframe, candle in completed_bars:
                        self._increment_bar_counter(timeframe)
                        log.info(
                            "bar closed: %s %s O=%.5f H=%.5f L=%.5f C=%.5f",
                            candle.symbol,
                            timeframe,
                            candle.open,
                            candle.high,
                            candle.low,
                            candle.close,
                        )

                        # Feed health gate
                        if not self._supervisor.is_tradeable(tick.symbol):
                            log.warning(
                                "feed unhealthy for %s — skipping signal generation",
                                tick.symbol,
                            )
                            continue

                        # Strategy evaluation
                        signals = self._strategy_engine.on_bar(candle, timeframe)
                        for signal in signals:
                            # Duplicate signal suppression
                            if self._supervisor.is_duplicate_signal(
                                tick.symbol,
                                signal.side,
                            ):
                                log.info(
                                    "duplicate signal suppressed: %s %s %s",
                                    tick.symbol,
                                    signal.signal_type.value,
                                    signal.side,
                                )
                                continue

                            self._increment_signal_counter(signal.signal_type)
                            log.info(
                                "signal generated: %s %s %s",
                                signal.symbol,
                                signal.signal_type.value,
                                signal.side,
                            )

                            # Enqueue — non-blocking for ENTRY/CANCEL (acceptable
                            # to drop), blocking for EXIT/MODIFY_SL (critical —
                            # dropping these means an open position has no management).
                            _critical = signal.signal_type in (
                                SignalType.EXIT,
                                SignalType.MODIFY_SL,
                            )
                            try:
                                self._signal_queue.put_nowait(signal)
                                self._metrics.queue_depth = self._signal_queue.qsize()
                            except asyncio.QueueFull:
                                if _critical:
                                    log.error(
                                        "signal queue full (%d) — force-enqueuing critical %s %s signal (blocking)",
                                        self._signal_queue.maxsize,
                                        signal.signal_type.value,
                                        signal.side,
                                    )
                                    await self._signal_queue.put(signal)
                                    self._metrics.queue_depth = self._signal_queue.qsize()
                                else:
                                    log.warning(
                                        "signal queue full (%d) — dropping signal %s %s",
                                        self._signal_queue.maxsize,
                                        signal.signal_type.value,
                                        signal.side,
                                    )

                except Exception:
                    log.exception("tick pipeline error — continuing")
                    self._metrics.errors += 1

        finally:
            log.info(
                "tick pipeline stopped: %d ticks processed",
                self._metrics.ticks,
            )
            # Signal shutdown so the other loops exit too.
            if self._running:
                self.stop()

    # -- Loop 2: Order Execution -------------------------------------------

    async def _order_execution(self) -> None:
        """Drain the signal queue and execute each signal.

        Uses ``while True`` with explicit break on sentinel (None) to avoid
        a race where the loop exits before the sentinel is processed.
        """
        log.info("order execution loop started")
        try:
            while True:
                try:
                    signal = await asyncio.wait_for(
                        self._signal_queue.get(),
                        timeout=0.5,
                    )
                except asyncio.TimeoutError:
                    # If we're no longer running AND the queue is empty,
                    # the tick pipeline has stopped and won't produce more.
                    if not self._running and self._signal_queue.empty():
                        break
                    continue

                # Sentinel means shutdown — drain remaining items then exit.
                if signal is None:
                    await self._drain_remaining()
                    break

                try:
                    result = await self._live_agent.execute(signal)
                    self._metrics.queue_depth = self._signal_queue.qsize()

                    if result.success:
                        self._metrics.approved += 1
                        log.info(
                            "order executed: %s %s -> %s",
                            signal.signal_type.value,
                            signal.side,
                            result.state_after.value if result.state_after else "OK",
                        )
                    elif result.rejected_reason and "supervisor" in result.rejected_reason.lower():
                        self._metrics.vetoed += 1
                        log.warning(
                            "order vetoed: %s — %s",
                            signal.signal_type.value,
                            result.rejected_reason,
                        )
                    elif result.rejected:
                        self._metrics.rejected += 1
                        log.warning(
                            "order rejected: %s — %s",
                            signal.signal_type.value,
                            result.rejected_reason,
                        )
                    else:
                        self._metrics.errors += 1
                        log.warning(
                            "order failed: %s — %s",
                            signal.signal_type.value,
                            result.error,
                        )
                except Exception:
                    log.exception("order execution error — continuing")
                    self._metrics.errors += 1
                finally:
                    self._signal_queue.task_done()

        finally:
            log.info(
                "order execution loop stopped: approved=%d rejected=%d vetoed=%d errors=%d",
                self._metrics.approved,
                self._metrics.rejected,
                self._metrics.vetoed,
                self._metrics.errors,
            )

    async def _drain_remaining(self) -> None:
        """Drain any signals remaining in the queue after sentinel received."""
        while not self._signal_queue.empty():
            try:
                signal = self._signal_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            if signal is None:
                self._signal_queue.task_done()
                continue

            try:
                result = await self._live_agent.execute(signal)
                if result.success:
                    self._metrics.approved += 1
                elif result.rejected:
                    self._metrics.rejected += 1
                else:
                    self._metrics.errors += 1
            except Exception:
                log.exception("drain execution error")
                self._metrics.errors += 1
            finally:
                self._signal_queue.task_done()

    # -- Loop 3: Health Monitor --------------------------------------------

    async def _health_monitor(self) -> None:
        """Periodically check feed staleness and log unhealthy symbols."""
        log.info("health monitor started (interval=%.1fs)", self._health_interval)
        try:
            while self._running:
                try:
                    staleness = self._supervisor.check_staleness()
                    for symbol, state in staleness.items():
                        if state != FeedState.HEALTHY:
                            log.warning(
                                "health: %s is %s",
                                symbol,
                                state.value,
                            )
                except Exception:
                    log.exception("health monitor error — continuing")

                # Use the stop event to allow immediate wake-up on shutdown
                # instead of sleeping for the full interval.
                stop_evt = self._stop_event
                if stop_evt is None:  # pragma: no cover — should never happen in run()
                    break
                try:
                    await asyncio.wait_for(
                        stop_evt.wait(),
                        timeout=self._health_interval,
                    )
                    # Event was set — time to exit.
                    break
                except asyncio.TimeoutError:
                    # Normal timeout — loop again.
                    pass
        finally:
            log.info("health monitor stopped")

    # -- Helpers -----------------------------------------------------------

    def _increment_bar_counter(self, timeframe: str) -> None:
        """Increment the appropriate bar counter."""
        if timeframe == "H1":
            self._metrics.bars_h1 += 1
        elif timeframe == "H4":
            self._metrics.bars_h4 += 1

    def _increment_signal_counter(self, signal_type: SignalType) -> None:
        """Increment the appropriate signal counter."""
        attr = _SIGNAL_COUNTER.get(signal_type)
        if attr is not None:
            current = getattr(self._metrics, attr)
            setattr(self._metrics, attr, current + 1)
