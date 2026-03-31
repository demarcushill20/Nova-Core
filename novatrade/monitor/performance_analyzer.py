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
        }

        if alert_thresholds:
            self.thresholds.update(alert_thresholds)

    def analyze_status(self, status_data: dict[str, Any]) -> PerformanceMetrics:
        """Analyze performance from NovaTrade status endpoint data."""
        metrics = status_data.get("metrics", {})
        strategy = status_data.get("strategy_state", {})
        feed_health = status_data.get("feed_health", {})

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
        }
