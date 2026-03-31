"""Real-time pipeline health dashboard for NovaTrade.

Provides comprehensive monitoring of trading pipeline health including:
- Data feed staleness monitoring
- Strategy signal generation rates
- Order execution success rates
- Risk supervisor interventions
- Live trading loop performance

This addresses recent feed staleness issues and provides critical visibility
for live trading readiness assessment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novatrade.adapter.base import MT5Adapter
from novatrade.config import NovaTradeCfg
from novatrade.execution.trading_agent import TradingAgent
from novatrade.models import AccountState
from novatrade.monitor.feed_health import FeedHealthSupervisor
from novatrade.monitor.health import HealthSnapshot
from novatrade.risk.risk_engine import RiskEngine

log = logging.getLogger("novatrade.monitor.pipeline_dashboard")


@dataclass
class PipelineHealth:
    """Comprehensive pipeline health snapshot."""

    timestamp: float = field(default_factory=time.time)
    overall_status: str = "UNKNOWN"  # HEALTHY, DEGRADED, UNHEALTHY

    # Data Feed Health
    feed_symbols_active: int = 0
    feed_symbols_stale: int = 0
    feed_staleness_max_minutes: float = 0.0
    feed_last_tick_utc: str = ""

    # Strategy Performance
    signals_generated_1h: int = 0
    signals_generated_24h: int = 0
    signal_rate_per_hour: float = 0.0
    strategy_warmup_complete: bool = False

    # Execution Pipeline
    orders_pending: int = 0
    orders_filled_1h: int = 0
    orders_rejected_1h: int = 0
    execution_success_rate: float = 0.0

    # Risk Management
    risk_interventions_1h: int = 0
    equity_drawdown_pct: float = 0.0
    max_risk_utilization_pct: float = 0.0
    kill_switch_active: bool = False

    # Technical Health
    adapter_connected: bool = False
    adapter_type: str = "Unknown"
    live_loop_uptime_seconds: float = 0.0
    errors_1h: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to serializable dict."""
        return {
            "timestamp": self.timestamp,
            "timestamp_utc": datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(),
            "overall_status": self.overall_status,
            "feed": {
                "symbols_active": self.feed_symbols_active,
                "symbols_stale": self.feed_symbols_stale,
                "staleness_max_minutes": self.feed_staleness_max_minutes,
                "last_tick_utc": self.feed_last_tick_utc,
            },
            "strategy": {
                "signals_1h": self.signals_generated_1h,
                "signals_24h": self.signals_generated_24h,
                "rate_per_hour": self.signal_rate_per_hour,
                "warmup_complete": self.strategy_warmup_complete,
            },
            "execution": {
                "orders_pending": self.orders_pending,
                "orders_filled_1h": self.orders_filled_1h,
                "orders_rejected_1h": self.orders_rejected_1h,
                "success_rate": self.execution_success_rate,
            },
            "risk": {
                "interventions_1h": self.risk_interventions_1h,
                "equity_drawdown_pct": self.equity_drawdown_pct,
                "max_risk_utilization_pct": self.max_risk_utilization_pct,
                "kill_switch_active": self.kill_switch_active,
            },
            "technical": {
                "adapter_connected": self.adapter_connected,
                "adapter_type": self.adapter_type,
                "uptime_seconds": self.live_loop_uptime_seconds,
                "errors_1h": self.errors_1h,
            }
        }


class PipelineDashboard:
    """Real-time pipeline health monitoring and dashboard generation."""

    def __init__(
        self,
        cfg: NovaTradeCfg,
        adapter: MT5Adapter | None = None,
        agent: TradingAgent | None = None,
        risk_engine: RiskEngine | None = None,
        feed_supervisor: FeedHealthSupervisor | None = None,
    ):
        self.cfg = cfg
        self.adapter = adapter
        self.agent = agent
        self.risk_engine = risk_engine
        self.feed_supervisor = feed_supervisor

        # Health history for trend analysis
        self.health_history: list[PipelineHealth] = []
        self.max_history_size = 1440  # 24 hours at 1-minute intervals

        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self, interval_seconds: float = 60.0) -> None:
        """Start the dashboard monitoring loop."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop(interval_seconds))
        log.info("pipeline dashboard started (interval=%.1fs)", interval_seconds)

    async def stop(self) -> None:
        """Stop the dashboard monitoring loop."""
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        log.info("pipeline dashboard stopped")

    async def _monitor_loop(self, interval_seconds: float) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                health = await self.collect_health_snapshot()
                self._add_to_history(health)
                await self._persist_health(health)

                # Log critical status changes
                if health.overall_status == "UNHEALTHY":
                    log.error(
                        "PIPELINE UNHEALTHY: feeds_stale=%d max_staleness=%.1fm errors_1h=%d",
                        health.feed_symbols_stale,
                        health.feed_staleness_max_minutes,
                        health.errors_1h,
                    )
                elif health.overall_status == "DEGRADED":
                    log.warning(
                        "PIPELINE DEGRADED: feeds_stale=%d staleness=%.1fm",
                        health.feed_symbols_stale,
                        health.feed_staleness_max_minutes,
                    )

            except Exception:
                log.exception("dashboard monitoring error")

            await asyncio.sleep(interval_seconds)

    def _add_to_history(self, health: PipelineHealth) -> None:
        """Add health snapshot to rolling history."""
        self.health_history.append(health)

        # Trim to max size
        if len(self.health_history) > self.max_history_size:
            self.health_history = self.health_history[-self.max_history_size:]

    async def _persist_health(self, health: PipelineHealth) -> None:
        """Persist health snapshot to disk."""
        try:
            health_dir = self.cfg.data_dir / "pipeline_health"
            health_dir.mkdir(exist_ok=True)

            # Current status (latest)
            current_path = health_dir / "current.json"
            with current_path.open("w") as f:
                json.dump(health.to_dict(), f, indent=2)

            # Historical log (append-only)
            log_path = health_dir / f"health_{datetime.now().strftime('%Y%m%d')}.jsonl"
            with log_path.open("a") as f:
                f.write(json.dumps(health.to_dict()) + "\n")

        except Exception:
            log.debug("failed to persist health snapshot", exc_info=True)

    async def collect_health_snapshot(self) -> PipelineHealth:
        """Collect comprehensive pipeline health snapshot."""
        health = PipelineHealth()

        # Data feed health
        if self.feed_supervisor:
            await self._collect_feed_health(health)

        # Strategy and execution health
        if self.agent:
            await self._collect_execution_health(health)

        # Risk engine health
        if self.risk_engine:
            await self._collect_risk_health(health)

        # Adapter health
        if self.adapter:
            await self._collect_adapter_health(health)

        # Determine overall status
        health.overall_status = self._determine_overall_status(health)

        return health

    async def _collect_feed_health(self, health: PipelineHealth) -> None:
        """Collect data feed health metrics."""
        try:
            # Get feed status for all symbols
            stale_count = 0
            max_staleness = 0.0
            last_tick_time = 0.0

            for symbol in self.cfg.symbols:
                feed_state = await self.feed_supervisor.get_feed_state(symbol)
                if feed_state.is_stale:
                    stale_count += 1
                    staleness_minutes = feed_state.staleness_seconds / 60.0
                    max_staleness = max(max_staleness, staleness_minutes)

                if feed_state.last_tick_time > last_tick_time:
                    last_tick_time = feed_state.last_tick_time

            health.feed_symbols_active = len(self.cfg.symbols) - stale_count
            health.feed_symbols_stale = stale_count
            health.feed_staleness_max_minutes = max_staleness

            if last_tick_time > 0:
                health.feed_last_tick_utc = datetime.fromtimestamp(
                    last_tick_time, tz=timezone.utc
                ).isoformat()

        except Exception:
            log.debug("failed to collect feed health", exc_info=True)

    async def _collect_execution_health(self, health: PipelineHealth) -> None:
        """Collect execution pipeline health metrics."""
        try:
            # Get agent state and metrics
            if hasattr(self.agent, 'get_metrics'):
                metrics = await self.agent.get_metrics()
                health.orders_pending = metrics.get('orders_pending', 0)
                health.orders_filled_1h = metrics.get('orders_filled_1h', 0)
                health.orders_rejected_1h = metrics.get('orders_rejected_1h', 0)

                total_orders = health.orders_filled_1h + health.orders_rejected_1h
                if total_orders > 0:
                    health.execution_success_rate = health.orders_filled_1h / total_orders

        except Exception:
            log.debug("failed to collect execution health", exc_info=True)

    async def _collect_risk_health(self, health: PipelineHealth) -> None:
        """Collect risk engine health metrics."""
        try:
            if hasattr(self.risk_engine, 'get_account_state'):
                account: AccountState = await self.risk_engine.get_account_state()

                # Calculate drawdown
                if account.balance > 0:
                    health.equity_drawdown_pct = max(0, (account.balance - account.equity) / account.balance * 100)

            # Check kill switch status
            kill_switch_path = self.cfg.data_dir.parent / "STATE" / "kill_switch.flag"
            health.kill_switch_active = kill_switch_path.exists()

        except Exception:
            log.debug("failed to collect risk health", exc_info=True)

    async def _collect_adapter_health(self, health: PipelineHealth) -> None:
        """Collect adapter connection health."""
        try:
            health.adapter_type = type(self.adapter).__name__

            # Check connection status
            if hasattr(self.adapter, 'is_connected'):
                health.adapter_connected = await self.adapter.is_connected()
            elif hasattr(self.adapter, '_connected'):
                health.adapter_connected = self.adapter._connected

        except Exception:
            log.debug("failed to collect adapter health", exc_info=True)

    def _determine_overall_status(self, health: PipelineHealth) -> str:
        """Determine overall pipeline health status."""
        # Critical failures
        if health.kill_switch_active:
            return "UNHEALTHY"
        if not health.adapter_connected:
            return "UNHEALTHY"
        if health.errors_1h > 10:
            return "UNHEALTHY"

        # Degraded conditions
        if health.feed_symbols_stale > 0:
            return "DEGRADED"
        if health.feed_staleness_max_minutes > 5.0:
            return "DEGRADED"
        if health.execution_success_rate < 0.8 and health.orders_filled_1h + health.orders_rejected_1h > 0:
            return "DEGRADED"

        return "HEALTHY"

    def get_status_summary(self) -> str:
        """Get a human-readable status summary."""
        if not self.health_history:
            return "No health data available"

        latest = self.health_history[-1]

        lines = [
            f"Pipeline Status: {latest.overall_status}",
            f"Data Feeds: {latest.feed_symbols_active} active, {latest.feed_symbols_stale} stale",
            f"Execution: {latest.orders_filled_1h} filled, {latest.orders_rejected_1h} rejected (1h)",
            f"Adapter: {latest.adapter_type} {'connected' if latest.adapter_connected else 'disconnected'}",
        ]

        if latest.feed_staleness_max_minutes > 0:
            lines.append(f"Max Feed Staleness: {latest.feed_staleness_max_minutes:.1f} minutes")

        if latest.kill_switch_active:
            lines.append("⚠️  KILL SWITCH ACTIVE")

        return "\n".join(lines)