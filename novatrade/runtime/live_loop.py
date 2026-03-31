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
from novatrade.execution.trading_agent import AgentState
from novatrade.monitor.enhanced_health import EnhancedHealthMonitor
from novatrade.monitor.feed_health import FeedHealthSupervisor, FeedState
from novatrade.risk.hard_risk_supervisor import HardRiskSupervisor, SupervisorAction
from novatrade.strategy.live_engine import LiveSignal, LiveStrategyEngine, SignalType

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
        state_store=None,
        adapter=None,
        hard_risk_supervisor: HardRiskSupervisor | None = None,
    ) -> None:
        self._poller = poller
        self._aggregator = aggregator
        self._supervisor = supervisor
        self._strategy_engine = strategy_engine
        self._live_agent = live_agent
        self._health_interval = health_interval
        self._state_store = state_store
        self._adapter = adapter  # MetaApiAdapter for periodic resubscription
        self._hard_supervisor = hard_risk_supervisor
        self._enhanced_health = EnhancedHealthMonitor(adapter) if adapter else None

        self._signal_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_maxsize)
        self._metrics = LiveMetrics()
        self._running = False
        self._stop_event: asyncio.Event | None = None
        self._health_cycle_count = 0

    # -- Public API --------------------------------------------------------

    async def run(self) -> None:
        """Start all three loops via ``asyncio.gather``.

        Runs until ``stop()`` is called or all loops raise.
        """
        self._running = True
        self._stop_event = asyncio.Event()
        self._metrics = LiveMetrics()

        self._restore_queued_signals()

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

    # -- Signal queue persistence -------------------------------------------

    def _restore_queued_signals(self) -> None:
        """Reload persisted signals from a previous crash into the queue."""
        if self._state_store is None:
            return
        try:
            persisted = self._state_store.load_queued_signals()
            for signal_id, signal_dict in persisted:
                signal = LiveSignal(
                    signal_type=SignalType(signal_dict["signal_type"]),
                    side=signal_dict["side"],
                    symbol=signal_dict["symbol"],
                    entry_price=signal_dict.get("entry_price", 0.0),
                    stop_loss=signal_dict.get("stop_loss", 0.0),
                    volume=signal_dict.get("volume", 0.0),
                    exit_price=signal_dict.get("exit_price", 0.0),
                    exit_reason=signal_dict.get("exit_reason", ""),
                    new_stop=signal_dict.get("new_stop", 0.0),
                    timestamp=signal_dict.get("timestamp", 0.0),
                    metadata=signal_dict.get("metadata", {}),
                )
                # Attach the DB signal_id so we can remove it after processing
                signal._db_signal_id = signal_id
                try:
                    self._signal_queue.put_nowait(signal)
                    log.info(
                        "restored persisted signal: %s %s %s (id=%d)",
                        signal.signal_type.value,
                        signal.side,
                        signal.symbol,
                        signal_id,
                    )
                except asyncio.QueueFull:
                    log.warning("queue full during restore — signal %d lost", signal_id)
                    self._state_store.remove_queued_signal(signal_id)
            if persisted:
                log.info("restored %d persisted signals from previous crash", len(persisted))
        except Exception:
            log.exception("failed to restore persisted signals")

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
                            self._log_signal(signal)
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

                            # Persist critical signals for crash safety
                            if _critical and self._state_store is not None:
                                db_id = self._state_store.save_queued_signal(signal)
                                if db_id is not None:
                                    signal._db_signal_id = db_id

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
                        # Roll back strategy engine state on ENTRY veto
                        if signal.signal_type == SignalType.ENTRY:
                            self._strategy_engine.cancel_pending()
                            log.info("rolled back engine state to FLAT after ENTRY veto")
                    elif result.rejected:
                        self._metrics.rejected += 1
                        log.warning(
                            "order rejected: %s — %s",
                            signal.signal_type.value,
                            result.rejected_reason,
                        )
                        # Roll back strategy engine state on ENTRY rejection
                        # to prevent phantom position / EXIT spam.
                        if signal.signal_type == SignalType.ENTRY:
                            self._strategy_engine.cancel_pending()
                            log.info("rolled back engine state to FLAT after ENTRY rejection")
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
                    # Remove from persistence after processing
                    db_id = getattr(signal, "_db_signal_id", None)
                    if db_id is not None and self._state_store is not None:
                        self._state_store.remove_queued_signal(db_id)
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
                # Remove from persistence after processing
                db_id = getattr(signal, "_db_signal_id", None)
                if db_id is not None and self._state_store is not None:
                    self._state_store.remove_queued_signal(db_id)
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

                    # Periodic market data resubscription to prevent feed death
                    if self._adapter is not None and hasattr(self._adapter, "resubscribe_if_stale"):
                        try:
                            resubbed = await self._adapter.resubscribe_if_stale()
                            if resubbed:
                                log.info("health: market data resubscription completed")
                        except Exception:
                            log.warning("health: market data resubscription failed", exc_info=True)

                    # Persist signal metrics for the autonomy collector
                    self._persist_signal_metrics()

                    # Persist equity snapshot for performance_stability scoring
                    self._persist_equity_snapshot()

                    # Broker ↔ Agent state reconciliation
                    await self._reconcile_broker_state()

                    # Hard Risk Supervisor monitoring
                    await self._check_hard_risk_supervisor()

                    # Enhanced broker diagnostics (every 12 cycles = ~1 minute)
                    self._health_cycle_count += 1
                    if self._health_cycle_count % 12 == 0 and self._enhanced_health:
                        await self._run_enhanced_diagnostics()
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

    def _persist_signal_metrics(self) -> None:
        """Write live metrics to STATE/novatrade/live_metrics.json for autonomy collectors."""
        import json as _json
        from pathlib import Path as _Path

        state_dir = _Path(self._get_base_path()) / "STATE" / "novatrade"
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            metrics_path = state_dir / "live_metrics.json"
            metrics_path.write_text(_json.dumps(self._metrics.to_dict(), indent=2))
        except OSError:
            log.debug("Failed to persist signal metrics", exc_info=True)

    def _persist_equity_snapshot(self) -> None:
        """Append an equity snapshot to STATE/novatrade/equity_history.json.

        Called periodically by the health monitor so the performance_stability
        collector has continuous equity-curve data even during periods with no
        trades.  The snapshot records current equity from the adapter (if
        available) or falls back to the live agent's last known equity.
        """
        import json as _json
        from datetime import datetime, timezone
        from pathlib import Path as _Path

        state_dir = _Path(self._get_base_path()) / "STATE" / "novatrade"
        eq_path = state_dir / "equity_history.json"

        # Resolve current equity from adapter or agent
        equity: float | None = None
        if self._adapter is not None and hasattr(self._adapter, "get_equity"):
            try:
                equity = self._adapter.get_equity()
            except Exception:  # noqa: S110
                pass
        if equity is None and hasattr(self._live_agent, "last_equity"):
            equity = self._live_agent.last_equity
        if equity is None:
            return  # no equity source available — skip

        try:
            state_dir.mkdir(parents=True, exist_ok=True)

            # Load existing data
            existing: dict = {"snapshots": [], "initial_equity": 100000.0}
            if eq_path.exists():
                try:
                    raw = _json.loads(eq_path.read_text())
                    if isinstance(raw, dict):
                        existing = raw
                    elif isinstance(raw, list):
                        existing = {"snapshots": raw, "initial_equity": 100000.0}
                except (ValueError, OSError):
                    pass

            snapshots = existing.get("snapshots", [])

            # Deduplicate: skip if the last snapshot has the same equity value
            # and was written less than 30 minutes ago
            now_iso = datetime.now(timezone.utc).isoformat()
            if snapshots:
                last = snapshots[-1]
                if last.get("equity") == equity:
                    return  # no change — skip duplicate

            snapshots.append(
                {
                    "equity": equity,
                    "timestamp": now_iso,
                    "source": "live",
                }
            )

            # Retain last 720 entries (~30 days of hourly snapshots)
            if len(snapshots) > 720:
                snapshots = snapshots[-720:]

            existing["snapshots"] = snapshots
            existing["last_updated"] = now_iso
            eq_path.write_text(_json.dumps(existing, indent=2))
        except OSError:
            log.debug("Failed to persist equity snapshot", exc_info=True)

    def _log_signal(self, signal: Any) -> None:
        """Append a signal event to STATE/novatrade/signal_log.json for autonomy collectors."""
        import json as _json
        from pathlib import Path as _Path

        state_dir = _Path(self._get_base_path()) / "STATE" / "novatrade"
        log_path = state_dir / "signal_log.json"
        try:
            state_dir.mkdir(parents=True, exist_ok=True)

            # Load existing log
            existing: list[dict] = []
            if log_path.exists():
                try:
                    data = _json.loads(log_path.read_text())
                    if isinstance(data, dict):
                        existing = data.get("signals", [])
                    elif isinstance(data, list):
                        existing = data
                except (ValueError, OSError):
                    existing = []

            # Append new signal entry
            entry = {
                "timestamp": time.time(),
                "signal_type": signal.signal_type.value if hasattr(signal, "signal_type") else str(signal),
                "side": getattr(signal, "side", "unknown"),
                "symbol": getattr(signal, "symbol", "unknown"),
            }
            existing.append(entry)

            # Retain only last 500 entries to prevent unbounded growth
            if len(existing) > 500:
                existing = existing[-500:]

            log_path.write_text(_json.dumps({"signals": existing, "last_updated": time.time()}, indent=2))
        except OSError:
            log.debug("Failed to log signal", exc_info=True)

    def _get_base_path(self) -> str:
        """Return the base path for state persistence."""
        # Use state_store's base_path if available, otherwise default
        if self._state_store and hasattr(self._state_store, "base_dir"):
            return str(self._state_store.base_dir.parent.parent)
        return "/home/nova/nova-core"

    async def _check_hard_risk_supervisor(self) -> None:
        """Check HardRiskSupervisor for emergency actions and execute them.

        This method:
        1. Gets current account state and positions
        2. Calls supervisor.check_account()
        3. Executes any supervisor actions (CLOSE_ALL, HALT, etc.)

        Called periodically by the health monitor to ensure continuous
        enforcement of hard risk limits.
        """
        if self._hard_supervisor is None:
            return

        try:
            # Get current account and positions from adapter
            if self._adapter is None:
                log.debug("hard_supervisor: no adapter available for account check")
                return

            account = None
            positions = []

            if hasattr(self._adapter, "get_account"):
                try:
                    account = await self._adapter.get_account()
                except Exception:
                    log.warning("hard_supervisor: failed to get account", exc_info=True)
                    return

            if hasattr(self._adapter, "get_positions"):
                try:
                    positions = await self._adapter.get_positions()
                except Exception:
                    log.warning("hard_supervisor: failed to get positions", exc_info=True)
                    return

            if account is None:
                log.debug("hard_supervisor: no account data for check")
                return

            # Check supervisor for actions
            actions = self._hard_supervisor.check_account(account.equity, positions)

            for action in actions:
                await self._execute_supervisor_action(action)

        except Exception:
            log.exception("hard_supervisor: error during monitoring check")

    async def _execute_supervisor_action(self, action: SupervisorAction) -> None:
        """Execute a supervisor action (CLOSE_ALL, HALT, etc.).

        Args:
            action: The supervisor action to execute
        """
        log.critical("SUPERVISOR ACTION: %s - %s", action.action, action.reason)

        try:
            if action.action == "CLOSE_ALL":
                # Force close all positions
                if hasattr(self._adapter, "close_all_positions"):
                    await self._adapter.close_all_positions()
                    log.critical("supervisor: all positions closed by supervisor order")
                else:
                    log.error("supervisor: close_all requested but adapter doesn't support it")

            elif action.action == "HALT":
                # Halt trading by stopping the loop
                log.critical("supervisor: EMERGENCY HALT - stopping live loop")
                self.stop()

            elif action.action == "CLOSE_POSITION":
                # Close specific positions
                if hasattr(self._adapter, "close_position"):
                    for position_id in action.position_ids:
                        await self._adapter.close_position(position_id)
                        log.critical("supervisor: position %s closed by supervisor order", position_id)
                else:
                    log.error("supervisor: close_position requested but adapter doesn't support it")

            else:
                log.error("supervisor: unknown action type: %s", action.action)

        except Exception:
            log.exception("supervisor: failed to execute action %s", action.action)

    async def _run_enhanced_diagnostics(self) -> None:
        """Run enhanced broker diagnostics for comprehensive health monitoring."""
        if not self._enhanced_health:
            return

        try:
            log.debug("Running enhanced broker diagnostics...")
            snapshot, diagnostics = await self._enhanced_health.take_health_snapshot_with_diagnostics()

            if diagnostics:
                critical_issues = diagnostics.get("summary", {}).get("critical_blocker")
                if critical_issues and "tradeAllowed=false" in critical_issues:
                    log.error("CRITICAL: %s", critical_issues)
                else:
                    log.debug("Enhanced diagnostics completed successfully")
        except Exception:
            log.warning("Enhanced diagnostics failed", exc_info=True)

    async def _reconcile_broker_state(self) -> None:
        """Compare TradingAgent state against broker reality and fix mismatches.

        Detects three critical desync scenarios:
          1. Agent LONG/SHORT but broker has no position → broker closed (SL hit)
          2. Agent PENDING but broker has position → fill happened
          3. Agent PENDING but broker has neither order nor position → stale pending

        Without this, the agent can get stuck in LONG/SHORT after an SL hit
        and reject all future entry signals indefinitely.
        """
        if self._adapter is None:
            return

        agent = self._live_agent.trading_agent
        agent_state = agent.state

        # Only reconcile when agent thinks it has an active order/position
        if agent_state == AgentState.FLAT:
            return

        try:
            positions = await self._adapter.get_positions()
        except Exception:
            log.warning("reconcile: cannot fetch positions", exc_info=True)
            return

        has_position = len(positions) > 0

        # --- Case 1: Agent LONG/SHORT but broker has no position → SL hit ---
        if agent_state in (AgentState.LONG, AgentState.SHORT) and not has_position:
            position_id = agent.position_id or "unknown"
            log.warning(
                "RECONCILE: agent is %s but broker has no position — "
                "broker-side close detected (SL/trailing stop). Syncing to FLAT.",
                agent_state.value,
            )
            self._live_agent.on_broker_close(
                position_id=position_id,
                pnl=0.0,  # P&L unknown — will be resolved from equity delta
                exit_reason="BROKER_CLOSE_RECONCILED",
            )
            log.info(
                "RECONCILE: agent synced to FLAT (was %s, position=%s)",
                agent_state.value,
                position_id,
            )
            return

        # --- Case 2: Agent PENDING but broker has position → fill happened ---
        if agent_state in (AgentState.PENDING_LONG, AgentState.PENDING_SHORT) and has_position:
            pos = positions[0]
            log.warning(
                "RECONCILE: agent is %s but broker has position %s — fill detected. Syncing.",
                agent_state.value,
                pos.position_id,
            )
            self._live_agent.on_fill(
                position_id=pos.position_id,
                fill_price=pos.open_price,
                volume=pos.volume,
                stop_loss=pos.stop_loss or 0.0,
            )
            log.info(
                "RECONCILE: fill synced — position=%s price=%.5f",
                pos.position_id,
                pos.open_price,
            )
            return

        # --- Case 3: Agent PENDING but no order and no position → stale ---
        if agent_state in (AgentState.PENDING_LONG, AgentState.PENDING_SHORT) and not has_position:
            try:
                orders = await self._adapter.get_orders()
            except Exception:
                orders = []

            if not orders:
                pending_id = agent.pending_order_id or "unknown"
                log.warning(
                    "RECONCILE: agent is %s but broker has no order or position — "
                    "stale pending detected. Forcing FLAT.",
                    agent_state.value,
                )
                agent.force_flat(reason=f"reconcile_stale_pending:{pending_id}")
                # Also reset the strategy engine to prevent phantom position
                try:
                    self._live_agent.strategy_engine.cancel_pending()
                except Exception:
                    log.warning("reconcile: strategy_engine.cancel_pending() failed", exc_info=True)
                log.info(
                    "RECONCILE: forced FLAT from %s (stale pending=%s)",
                    agent_state.value,
                    pending_id,
                )
