"""Real-time strategy performance analyzer for NovaTrade.

Provides advanced analytics on strategy performance, trade execution quality,
and system health metrics to identify optimization opportunities and alert
on critical performance degradation.

This module enhances the existing monitoring infrastructure with:
- Performance trend analysis
- Trade execution quality scoring
- Risk-adjusted return metrics
- Alert generation for performance thresholds
- Daily performance summaries
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("novatrade.monitor.performance")


class PerformanceLevel(Enum):
    """Performance level classification."""

    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    AVERAGE = "AVERAGE"
    POOR = "POOR"
    CRITICAL = "CRITICAL"


class AlertSeverity(Enum):
    """Performance alert severity levels."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class PerformanceMetrics:
    """Consolidated performance metrics snapshot."""

    # Core metrics
    equity: float
    equity_change: float
    equity_change_pct: float
    total_signals: int
    approval_rate: float

    # Execution quality
    avg_latency_ms: float | None = None
    spread_cost_pips: float | None = None
    slippage_pips: float | None = None

    # Strategy metrics
    consecutive_losses: int = 0
    win_rate: float | None = None
    profit_factor: float | None = None
    sharpe_ratio: float | None = None

    # S7: Partial exit metrics (v5 features)
    partial_exits_count: int = 0
    partial_exit_rate: float = 0.0  # percentage of trades with partial exits
    avg_partial_profit: float = 0.0  # average profit from partial exits
    partial_to_breakeven_rate: float = 0.0  # percentage reaching breakeven after partial

    # S7: Multi-timeframe metrics
    timeframe: str = "H1"  # current timeframe being monitored
    m5_signal_rate: float = 0.0  # signals per hour on M5
    h1_signal_rate: float = 0.0  # signals per hour on H1
    mtf_alignment_rate: float = 0.0  # H1/H4 or M5/H1 alignment success rate

    # S7: Enhanced strategy metrics (v5)
    trades_per_day: float = 0.0
    avg_trade_duration_hours: float = 0.0
    volume_efficiency: float = 0.0  # actual vs target position sizes
    sl_modification_count: int = 0
    trail_trigger_rate: float = 0.0  # percentage of trades where trailing activated

    # System health
    feed_health_score: float = 0.0
    uptime_seconds: float = 0.0
    error_rate: float = 0.0

    timestamp: float = field(default_factory=time.time)

    def performance_level(self) -> PerformanceLevel:
        """Classify overall performance level."""
        if self.equity_change_pct >= 0.5:
            return PerformanceLevel.EXCELLENT
        elif self.equity_change_pct >= 0.1:
            return PerformanceLevel.GOOD
        elif self.equity_change_pct >= -0.1:
            return PerformanceLevel.AVERAGE
        elif self.equity_change_pct >= -0.5:
            return PerformanceLevel.POOR
        else:
            return PerformanceLevel.CRITICAL

    def execution_quality_score(self) -> float:
        """Calculate execution quality score (0-100)."""
        score = 100.0

        # Penalize high error rate
        if self.error_rate > 0:
            score -= min(self.error_rate * 50, 30)

        # Penalize low approval rate
        if self.approval_rate < 0.9:
            score -= (0.9 - self.approval_rate) * 100

        # Penalize poor feed health
        score *= self.feed_health_score

        return max(0.0, score)


@dataclass
class PerformanceAlert:
    """Performance alert with severity and context."""

    severity: AlertSeverity
    message: str
    metric_name: str
    current_value: float | str
    threshold: float | str | None = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "severity": self.severity.value,
            "message": self.message,
            "metric_name": self.metric_name,
            "current_value": self.current_value,
            "threshold": self.threshold,
            "timestamp": self.timestamp,
        }


class PerformanceAnalyzer:
    """Real-time performance analyzer for NovaTrade strategies."""

    def __init__(
        self,
        *,
        initial_equity: float = 100_000.0,
        alert_thresholds: dict[str, float] | None = None,
        history_window: int = 100,
    ) -> None:
        """Initialize performance analyzer.

        Args:
            initial_equity: Starting account equity for change calculations.
            alert_thresholds: Custom alert thresholds. Defaults to conservative values.
            history_window: Number of metrics snapshots to keep in memory.
        """
        self.initial_equity = initial_equity
        self.history: list[PerformanceMetrics] = []
        self.history_window = history_window

        # Default alert thresholds
        self.thresholds = {
            "max_drawdown_pct": -2.0,  # Alert if drawdown > 2%
            "min_approval_rate": 0.8,  # Alert if approval rate < 80%
            "max_consecutive_losses": 5,  # Alert if 5+ consecutive losses
            "min_feed_health": 0.9,  # Alert if feed health < 90%
            "max_error_rate": 0.05,  # Alert if error rate > 5%
            "min_execution_quality": 75.0,  # Alert if execution quality < 75
            # S7: Enhanced thresholds for v5 features
            "min_partial_exit_rate": 30.0,  # Alert if < 30% partial exit rate when enabled
            "max_trades_per_day": 8.0,  # Alert if > 8 trades/day (overtrading)
            "min_trades_per_day": 0.5,  # Alert if < 0.5 trades/day (undertrading)
            "min_mtf_alignment": 60.0,  # Alert if MTF alignment < 60%
            "max_avg_trade_duration": 48.0,  # Alert if avg trade > 48h (holding too long)
            "min_volume_efficiency": 80.0,  # Alert if volume efficiency < 80%
            "max_sl_modifications_per_day": 20.0,  # Alert if excessive SL modifications
        }

        if alert_thresholds:
            self.thresholds.update(alert_thresholds)

    def analyze_status(self, status_data: dict[str, Any]) -> PerformanceMetrics:
        """Analyze performance from NovaTrade status endpoint data."""
        metrics = status_data.get("metrics", {})
        strategy = status_data.get("strategy_state", {})
        feed_health = status_data.get("feed_health", {})
        config = status_data.get("config", {})

        # Extract core metrics
        equity = strategy.get("equity", self.initial_equity)
        equity_change = equity - self.initial_equity
        equity_change_pct = (equity_change / self.initial_equity) * 100

        total_signals = (
            metrics.get("signals_entry", 0) + metrics.get("signals_exit", 0) + metrics.get("signals_modify_sl", 0)
        )

        approval_rate = 1.0
        if total_signals > 0:
            approved = metrics.get("approved", 0)
            approval_rate = approved / total_signals

        # Feed health score
        healthy_symbols = feed_health.get("healthy", 0)
        total_symbols = feed_health.get("tracked_symbols", 1)
        feed_health_score = healthy_symbols / total_symbols if total_symbols > 0 else 1.0

        # Error rate
        total_ops = total_signals + metrics.get("ticks", 0)
        error_rate = 0.0
        if total_ops > 0:
            errors = metrics.get("errors", 0)
            error_rate = errors / total_ops

        # S7: Extract v5 feature metrics
        # Partial exit metrics
        partial_exits = metrics.get("partial_exits", 0)
        total_trades = metrics.get("total_trades", 1)
        partial_exit_rate = (partial_exits / total_trades * 100) if total_trades > 0 else 0.0
        avg_partial_profit = metrics.get("avg_partial_profit", 0.0)
        partial_to_breakeven = metrics.get("partial_to_breakeven", 0)
        partial_to_breakeven_rate = (partial_to_breakeven / max(partial_exits, 1) * 100) if partial_exits > 0 else 0.0

        # Multi-timeframe metrics
        timeframe = config.get("timeframe", "H1")
        m5_signals = metrics.get("m5_signals_1h", 0)
        h1_signals = metrics.get("h1_signals_1h", 0)
        mtf_aligned = metrics.get("mtf_aligned_signals", 0)
        mtf_total = metrics.get("mtf_checked_signals", 1)
        mtf_alignment_rate = (mtf_aligned / mtf_total * 100) if mtf_total > 0 else 0.0

        # Enhanced strategy metrics
        uptime_hours = status_data.get("uptime_seconds", 0) / 3600.0
        trades_per_day = (total_trades / max(uptime_hours / 24.0, 0.001)) if uptime_hours > 0 else 0.0

        avg_duration = metrics.get("avg_trade_duration_hours", 0.0)
        target_volume = metrics.get("target_volume_sum", 1.0)
        actual_volume = metrics.get("actual_volume_sum", 1.0)
        volume_efficiency = (actual_volume / target_volume * 100) if target_volume > 0 else 100.0

        sl_modifications = metrics.get("sl_modifications", 0)
        trail_triggers = metrics.get("trail_triggers", 0)
        trail_trigger_rate = (trail_triggers / max(total_trades, 1) * 100) if total_trades > 0 else 0.0

        perf_metrics = PerformanceMetrics(
            equity=equity,
            equity_change=equity_change,
            equity_change_pct=equity_change_pct,
            total_signals=total_signals,
            approval_rate=approval_rate,
            consecutive_losses=strategy.get("consecutive_losses", 0),
            feed_health_score=feed_health_score,
            uptime_seconds=status_data.get("uptime_seconds", 0),
            error_rate=error_rate,
            # S7: New v5 metrics
            partial_exits_count=partial_exits,
            partial_exit_rate=partial_exit_rate,
            avg_partial_profit=avg_partial_profit,
            partial_to_breakeven_rate=partial_to_breakeven_rate,
            timeframe=timeframe,
            m5_signal_rate=float(m5_signals),
            h1_signal_rate=float(h1_signals),
            mtf_alignment_rate=mtf_alignment_rate,
            trades_per_day=trades_per_day,
            avg_trade_duration_hours=avg_duration,
            volume_efficiency=volume_efficiency,
            sl_modification_count=sl_modifications,
            trail_trigger_rate=trail_trigger_rate,
        )

        # Add to history
        self.history.append(perf_metrics)
        if len(self.history) > self.history_window:
            self.history.pop(0)

        return perf_metrics

    def generate_alerts(self, metrics: PerformanceMetrics) -> list[PerformanceAlert]:
        """Generate performance alerts based on current metrics."""
        alerts: list[PerformanceAlert] = []

        # Drawdown alert
        if metrics.equity_change_pct <= self.thresholds["max_drawdown_pct"]:
            alerts.append(
                PerformanceAlert(
                    severity=AlertSeverity.WARNING if metrics.equity_change_pct > -5.0 else AlertSeverity.CRITICAL,
                    message=f"Account drawdown {metrics.equity_change_pct:.2f}% exceeds threshold",
                    metric_name="drawdown_pct",
                    current_value=metrics.equity_change_pct,
                    threshold=self.thresholds["max_drawdown_pct"],
                )
            )

        # Approval rate alert
        if metrics.approval_rate < self.thresholds["min_approval_rate"]:
            alerts.append(
                PerformanceAlert(
                    severity=AlertSeverity.WARNING,
                    message=f"Signal approval rate {metrics.approval_rate:.1%} below threshold",
                    metric_name="approval_rate",
                    current_value=metrics.approval_rate,
                    threshold=self.thresholds["min_approval_rate"],
                )
            )

        # Consecutive losses alert
        if metrics.consecutive_losses >= self.thresholds["max_consecutive_losses"]:
            alerts.append(
                PerformanceAlert(
                    severity=AlertSeverity.WARNING,
                    message=f"Consecutive losses ({metrics.consecutive_losses}) exceed threshold",
                    metric_name="consecutive_losses",
                    current_value=metrics.consecutive_losses,
                    threshold=self.thresholds["max_consecutive_losses"],
                )
            )

        # Feed health alert
        if metrics.feed_health_score < self.thresholds["min_feed_health"]:
            alerts.append(
                PerformanceAlert(
                    severity=AlertSeverity.CRITICAL,
                    message=f"Feed health score {metrics.feed_health_score:.1%} below threshold",
                    metric_name="feed_health_score",
                    current_value=metrics.feed_health_score,
                    threshold=self.thresholds["min_feed_health"],
                )
            )

        # Error rate alert
        if metrics.error_rate > self.thresholds["max_error_rate"]:
            alerts.append(
                PerformanceAlert(
                    severity=AlertSeverity.WARNING,
                    message=f"Error rate {metrics.error_rate:.1%} exceeds threshold",
                    metric_name="error_rate",
                    current_value=metrics.error_rate,
                    threshold=self.thresholds["max_error_rate"],
                )
            )

        # Execution quality alert
        exec_quality = metrics.execution_quality_score()
        if exec_quality < self.thresholds["min_execution_quality"]:
            alerts.append(
                PerformanceAlert(
                    severity=AlertSeverity.WARNING,
                    message=f"Execution quality score {exec_quality:.1f} below threshold",
                    metric_name="execution_quality",
                    current_value=exec_quality,
                    threshold=self.thresholds["min_execution_quality"],
                )
            )

        # S7: Enhanced v5 feature alerts
        # Partial exit rate alert (only when partial exits are enabled)
        if metrics.partial_exits_count > 0 and metrics.partial_exit_rate < self.thresholds["min_partial_exit_rate"]:
            alerts.append(
                PerformanceAlert(
                    severity=AlertSeverity.WARNING,
                    message=f"Partial exit rate {metrics.partial_exit_rate:.1f}% below expected threshold",
                    metric_name="partial_exit_rate",
                    current_value=metrics.partial_exit_rate,
                    threshold=self.thresholds["min_partial_exit_rate"],
                )
            )

        # Trading frequency alerts
        if metrics.trades_per_day > self.thresholds["max_trades_per_day"]:
            alerts.append(
                PerformanceAlert(
                    severity=AlertSeverity.WARNING,
                    message=(
                        f"Trading frequency {metrics.trades_per_day:.1f} trades/day exceeds threshold (overtrading)"
                    ),
                    metric_name="trades_per_day",
                    current_value=metrics.trades_per_day,
                    threshold=self.thresholds["max_trades_per_day"],
                )
            )

        if metrics.trades_per_day < self.thresholds["min_trades_per_day"]:
            alerts.append(
                PerformanceAlert(
                    severity=AlertSeverity.WARNING,
                    message=f"Trading frequency {metrics.trades_per_day:.1f} trades/day below threshold (undertrading)",
                    metric_name="trades_per_day",
                    current_value=metrics.trades_per_day,
                    threshold=self.thresholds["min_trades_per_day"],
                )
            )

        # Multi-timeframe alignment alert
        if metrics.mtf_alignment_rate < self.thresholds["min_mtf_alignment"]:
            alerts.append(
                PerformanceAlert(
                    severity=AlertSeverity.WARNING,
                    message=f"MTF alignment rate {metrics.mtf_alignment_rate:.1f}% below threshold",
                    metric_name="mtf_alignment_rate",
                    current_value=metrics.mtf_alignment_rate,
                    threshold=self.thresholds["min_mtf_alignment"],
                )
            )

        # Trade duration alert
        if metrics.avg_trade_duration_hours > self.thresholds["max_avg_trade_duration"]:
            alerts.append(
                PerformanceAlert(
                    severity=AlertSeverity.WARNING,
                    message=f"Average trade duration {metrics.avg_trade_duration_hours:.1f}h exceeds threshold",
                    metric_name="avg_trade_duration",
                    current_value=metrics.avg_trade_duration_hours,
                    threshold=self.thresholds["max_avg_trade_duration"],
                )
            )

        # Volume efficiency alert
        if metrics.volume_efficiency < self.thresholds["min_volume_efficiency"]:
            alerts.append(
                PerformanceAlert(
                    severity=AlertSeverity.WARNING,
                    message=f"Volume efficiency {metrics.volume_efficiency:.1f}% below threshold",
                    metric_name="volume_efficiency",
                    current_value=metrics.volume_efficiency,
                    threshold=self.thresholds["min_volume_efficiency"],
                )
            )

        # Stop-loss modification alert
        daily_sl_modifications = metrics.sl_modification_count * (24.0 / max(metrics.uptime_seconds / 3600.0, 1.0))
        if daily_sl_modifications > self.thresholds["max_sl_modifications_per_day"]:
            alerts.append(
                PerformanceAlert(
                    severity=AlertSeverity.WARNING,
                    message=f"SL modifications {daily_sl_modifications:.1f}/day exceed threshold",
                    metric_name="sl_modifications_per_day",
                    current_value=daily_sl_modifications,
                    threshold=self.thresholds["max_sl_modifications_per_day"],
                )
            )

        return alerts

    def performance_trend(self, window: int = 10) -> str:
        """Analyze performance trend over recent history."""
        if len(self.history) < 2:
            return "INSUFFICIENT_DATA"

        recent = self.history[-min(window, len(self.history)) :]

        # Calculate trend in equity change percentage
        equity_changes = [m.equity_change_pct for m in recent]
        if len(equity_changes) < 2:
            return "STABLE"

        # Simple trend analysis
        start_value = equity_changes[0]
        end_value = equity_changes[-1]
        trend = end_value - start_value

        if trend > 0.1:
            return "IMPROVING"
        elif trend < -0.1:
            return "DECLINING"
        else:
            return "STABLE"

    def generate_summary(self, metrics: PerformanceMetrics) -> dict[str, Any]:
        """Generate performance summary report."""
        trend = self.performance_trend()
        alerts = self.generate_alerts(metrics)
        execution_quality = metrics.execution_quality_score()

        return {
            "performance_level": metrics.performance_level().value,
            "equity": metrics.equity,
            "equity_change": metrics.equity_change,
            "equity_change_pct": round(metrics.equity_change_pct, 2),
            "total_signals": metrics.total_signals,
            "approval_rate": round(metrics.approval_rate, 3),
            "execution_quality_score": round(execution_quality, 1),
            "feed_health_score": round(metrics.feed_health_score, 3),
            "consecutive_losses": metrics.consecutive_losses,
            "uptime_hours": round(metrics.uptime_seconds / 3600, 1),
            "trend": trend,
            "alert_count": len(alerts),
            "alerts": [alert.to_dict() for alert in alerts],
            "timestamp": metrics.timestamp,
            # S7: Enhanced v5 metrics summary
            "partial_exit_metrics": {
                "partial_exits_count": metrics.partial_exits_count,
                "partial_exit_rate": round(metrics.partial_exit_rate, 1),
                "avg_partial_profit": round(metrics.avg_partial_profit, 2),
                "partial_to_breakeven_rate": round(metrics.partial_to_breakeven_rate, 1),
            },
            "timeframe_metrics": {
                "primary_timeframe": metrics.timeframe,
                "m5_signal_rate": round(metrics.m5_signal_rate, 1),
                "h1_signal_rate": round(metrics.h1_signal_rate, 1),
                "mtf_alignment_rate": round(metrics.mtf_alignment_rate, 1),
            },
            "trading_efficiency": {
                "trades_per_day": round(metrics.trades_per_day, 1),
                "avg_trade_duration_hours": round(metrics.avg_trade_duration_hours, 1),
                "volume_efficiency": round(metrics.volume_efficiency, 1),
                "sl_modification_count": metrics.sl_modification_count,
                "trail_trigger_rate": round(metrics.trail_trigger_rate, 1),
            },
        }
