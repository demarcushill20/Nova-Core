"""Integration module for strategy degradation detection with NovaTrade monitoring.

Provides unified interface to integrate Hurst exponent, CUSUM, and half-life
monitoring with existing performance analytics and health monitoring systems.

This module bridges the gap between the new degradation detection capabilities
and the established monitoring infrastructure.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from novatrade.config import NovaTradeCfg
from novatrade.monitor.degradation_detector import DegradationDetector
from novatrade.monitor.performance_analyzer import PerformanceAnalyzer, PerformanceMetrics
from novatrade.monitor.strategy_health_dashboard import StrategyHealthDashboard, StrategyHealthMonitor

log = logging.getLogger("novatrade.monitor.degradation_integration")

# State persistence paths
STATE_DIR = Path("/home/nova/nova-core/STATE/novatrade")
DEGRADATION_STATE_FILE = STATE_DIR / "degradation_state.json"


class DegradationMonitoringIntegration:
    """Unified degradation monitoring integration for NovaTrade.

    Combines strategy degradation detection with existing monitoring systems:
    - Performance analyzer for real-time metrics
    - System health for component status
    - Regime detection for market conditions
    - Position tracking for risk management
    """

    def __init__(
        self,
        config: NovaTradeCfg | None = None,
        *,
        enable_degradation_monitoring: bool = True,
        degradation_check_interval_minutes: int = 60,
        position_sizing_enabled: bool = True,
        alert_integration: bool = True,
    ) -> None:
        """Initialize degradation monitoring integration.

        Args:
            config: NovaTrade configuration
            enable_degradation_monitoring: Enable degradation detection
            degradation_check_interval_minutes: Minutes between degradation checks
            position_sizing_enabled: Enable automatic position sizing adjustment
            alert_integration: Enable alert system integration
        """
        self.config = config
        self.enable_degradation_monitoring = enable_degradation_monitoring
        self.check_interval_minutes = degradation_check_interval_minutes
        self.position_sizing_enabled = position_sizing_enabled
        self.alert_integration = alert_integration

        # Initialize monitoring components
        self.degradation_detector = DegradationDetector()
        self.strategy_health_monitor = StrategyHealthMonitor(
            degradation_detector=self.degradation_detector,
            check_interval_minutes=degradation_check_interval_minutes,
        )
        self.performance_analyzer = PerformanceAnalyzer()

        # State tracking
        self.last_degradation_check: datetime | None = None
        self.current_dashboard: StrategyHealthDashboard | None = None
        self.active_alerts: list[dict[str, Any]] = []

        # Data buffers for analysis
        self.price_history: list[float] = []
        self.return_history: list[float] = []
        self.max_history_size = 1000

        log.info("Degradation monitoring integration initialized (enabled=%s)", enable_degradation_monitoring)

    def update_price_data(self, price: float, return_pct: float | None = None) -> None:
        """Update price and return data for degradation analysis.

        Args:
            price: Current price value
            return_pct: Return percentage (calculated if None)
        """
        if not self.enable_degradation_monitoring:
            return

        # Add to price history
        self.price_history.append(price)
        if len(self.price_history) > self.max_history_size:
            self.price_history.pop(0)

        # Calculate return if not provided
        if return_pct is None and len(self.price_history) >= 2:
            prev_price = self.price_history[-2]
            return_pct = ((price - prev_price) / prev_price) * 100

        # Add to return history
        if return_pct is not None:
            self.return_history.append(return_pct)
            if len(self.return_history) > self.max_history_size:
                self.return_history.pop(0)

    def check_degradation_due(self) -> bool:
        """Check if degradation analysis is due based on interval."""
        if not self.enable_degradation_monitoring:
            return False

        if self.last_degradation_check is None:
            return True

        time_since_check = datetime.now(timezone.utc) - self.last_degradation_check
        return time_since_check >= timedelta(minutes=self.check_interval_minutes)

    def analyze_strategy_degradation(
        self,
        symbol: str = "EURUSD",
        timeframe: str = "M5",
        force_check: bool = False,
    ) -> StrategyHealthDashboard | None:
        """Perform strategy degradation analysis if due.

        Args:
            symbol: Trading symbol
            timeframe: Chart timeframe
            force_check: Force check regardless of timing

        Returns:
            Strategy health dashboard or None if not due/disabled
        """
        if not self.enable_degradation_monitoring:
            return None

        if not force_check and not self.check_degradation_due():
            return self.current_dashboard

        # Ensure sufficient data for analysis
        if len(self.price_history) < 50 or len(self.return_history) < 10:
            log.debug(
                "Insufficient data for degradation analysis (prices=%d, returns=%d)",
                len(self.price_history),
                len(self.return_history),
            )
            return self.current_dashboard

        try:
            # Perform degradation analysis
            dashboard = self.strategy_health_monitor.assess_strategy_health(
                price_series=self.price_history,
                returns=self.return_history,
                symbol=symbol,
                timeframe=timeframe,
            )

            self.current_dashboard = dashboard
            self.last_degradation_check = datetime.now(timezone.utc)

            # Update alerts
            if self.alert_integration:
                self._update_active_alerts(dashboard)

            # Log significant health changes
            self._log_health_changes(dashboard)

            return dashboard

        except Exception as e:
            log.error("Degradation analysis failed: %s", e)
            return None

    def get_position_sizing_multiplier(self) -> float:
        """Get current position sizing multiplier based on degradation analysis.

        Returns:
            Position sizing multiplier (0.0-1.0) where 1.0 = full size
        """
        if not self.enable_degradation_monitoring or not self.position_sizing_enabled:
            return 1.0

        if self.current_dashboard is None:
            return 1.0

        return self.current_dashboard.position_sizing_multiplier

    def should_halt_new_entries(self) -> bool:
        """Check if new entries should be halted based on degradation analysis.

        Returns:
            True if new entries should be halted
        """
        if not self.enable_degradation_monitoring:
            return False

        if self.current_dashboard is None:
            return False

        return self.current_dashboard.should_halt_entries

    def should_pause_strategy(self) -> bool:
        """Check if strategy should be paused based on degradation analysis.

        Returns:
            True if strategy should be paused completely
        """
        if not self.enable_degradation_monitoring:
            return False

        if self.current_dashboard is None:
            return False

        return self.current_dashboard.should_pause_strategy

    def get_health_summary(self) -> dict[str, Any]:
        """Get comprehensive health summary for monitoring endpoints.

        Returns:
            Dictionary with health status and recommendations
        """
        if not self.enable_degradation_monitoring or self.current_dashboard is None:
            return {
                "degradation_monitoring": {
                    "enabled": self.enable_degradation_monitoring,
                    "status": "no_data" if self.enable_degradation_monitoring else "disabled",
                    "message": "Degradation monitoring not available",
                },
            }

        dashboard = self.current_dashboard
        return {
            "degradation_monitoring": {
                "enabled": True,
                "status": dashboard.snapshot.overall_health.value.lower(),
                "health_score": dashboard.health_score.overall_score,
                "health_grade": dashboard.health_score.grade,
                "timestamp": dashboard.snapshot.timestamp.isoformat(),
                "symbol": dashboard.snapshot.symbol,
                "timeframe": dashboard.snapshot.timeframe,
                "metrics": {
                    "hurst_exponent": dashboard.snapshot.hurst_metrics.exponent,
                    "hurst_health": dashboard.snapshot.hurst_metrics.health_level.value,
                    "is_mean_reverting": dashboard.snapshot.hurst_metrics.is_mean_reverting,
                    "cusum_pct": dashboard.snapshot.cusum_metrics.cusum_pct,
                    "cusum_health": dashboard.snapshot.cusum_metrics.health_level.value,
                    "rolling_sharpe": dashboard.snapshot.rolling_sharpe,
                },
                "risk_management": {
                    "position_sizing_multiplier": dashboard.position_sizing_multiplier,
                    "halt_entries": dashboard.should_halt_entries,
                    "reduce_risk": dashboard.should_reduce_risk,
                    "pause_strategy": dashboard.should_pause_strategy,
                },
                "recommendations": {
                    "action": dashboard.snapshot.recommended_action,
                    "summary": dashboard.summary_message,
                    "next_check": dashboard.next_check_recommended.isoformat(),
                },
                "alerts_count": len(dashboard.alerts),
                "active_alerts": [alert.to_dict() for alert in dashboard.alerts],
            },
        }

    def integrate_with_performance_metrics(self, performance_metrics: PerformanceMetrics) -> dict[str, Any]:
        """Integrate degradation analysis with performance metrics.

        Args:
            performance_metrics: Current performance metrics from PerformanceAnalyzer

        Returns:
            Combined analysis with performance and degradation metrics
        """
        health_summary = self.get_health_summary()

        # Add performance context to degradation analysis
        combined = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "performance_metrics": {
                "equity_change_pct": performance_metrics.equity_change_pct,
                "approval_rate": performance_metrics.approval_rate,
                "consecutive_losses": performance_metrics.consecutive_losses,
                "performance_level": performance_metrics.performance_level().value,
                "execution_quality": performance_metrics.execution_quality_score(),
            },
            "degradation_analysis": health_summary["degradation_monitoring"],
        }

        # Calculate combined risk assessment
        if self.current_dashboard:
            perf_risk = 1.0 - max(0, min(1, performance_metrics.equity_change_pct / 100 + 1))
            degradation_risk = 1.0 - (self.current_dashboard.health_score.overall_score / 100)

            combined["risk_assessment"] = {
                "performance_risk": round(perf_risk, 3),
                "degradation_risk": round(degradation_risk, 3),
                "combined_risk": round((perf_risk + degradation_risk) / 2, 3),
                "recommended_position_size": self.get_position_sizing_multiplier(),
            }

        return combined

    def _update_active_alerts(self, dashboard: StrategyHealthDashboard) -> None:
        """Update active alerts list."""
        self.active_alerts.clear()

        for alert in dashboard.alerts:
            self.active_alerts.append(alert.to_dict())

        # Add system-level alerts for critical conditions
        if dashboard.should_pause_strategy:
            self.active_alerts.append(
                {
                    "level": "RED",
                    "component": "system",
                    "message": "Strategy degradation detected - full pause recommended",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
        elif dashboard.should_halt_entries:
            self.active_alerts.append(
                {
                    "level": "RED",
                    "component": "system",
                    "message": "Strategy degradation detected - halt new entries",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

    def _log_health_changes(self, dashboard: StrategyHealthDashboard) -> None:
        """Log significant health status changes."""
        if dashboard.should_pause_strategy:
            log.critical("STRATEGY DEGRADATION: Full pause recommended - %s", dashboard.summary_message)
        elif dashboard.should_halt_entries:
            log.error("STRATEGY DEGRADATION: Halt entries recommended - %s", dashboard.summary_message)
        elif dashboard.should_reduce_risk:
            log.warning("STRATEGY DEGRADATION: Risk reduction recommended - %s", dashboard.summary_message)
        else:
            log.info("Strategy health check: %s", dashboard.summary_message)

        # Log detailed metrics for debugging
        snapshot = dashboard.snapshot
        log.debug(
            "Degradation metrics: Hurst=%.3f (%s), CUSUM=%.1f%% (%s), Sharpe=%.2f, Score=%.0f/100",
            snapshot.hurst_metrics.exponent,
            snapshot.hurst_metrics.health_level.value,
            snapshot.cusum_metrics.cusum_pct,
            snapshot.cusum_metrics.health_level.value,
            snapshot.rolling_sharpe,
            dashboard.health_score.overall_score,
        )

    def save_state(self) -> None:
        """Save degradation monitoring state to disk."""
        if not self.enable_degradation_monitoring or self.current_dashboard is None:
            return

        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)

            state = {
                "last_check": self.last_degradation_check.isoformat() if self.last_degradation_check else None,
                "dashboard": self.current_dashboard.to_dict(),
                "active_alerts": self.active_alerts,
                "data_points": {
                    "price_history_size": len(self.price_history),
                    "return_history_size": len(self.return_history),
                },
            }

            with open(DEGRADATION_STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)

        except Exception as e:
            log.error("Failed to save degradation state: %s", e)

    def load_state(self) -> None:
        """Load degradation monitoring state from disk."""
        try:
            if DEGRADATION_STATE_FILE.exists():
                with open(DEGRADATION_STATE_FILE) as f:
                    state = json.load(f)

                if state.get("last_check"):
                    self.last_degradation_check = datetime.fromisoformat(state["last_check"])

                if state.get("active_alerts"):
                    self.active_alerts = state["active_alerts"]

                log.info("Loaded degradation monitoring state from %s", DEGRADATION_STATE_FILE)

        except Exception as e:
            log.warning("Failed to load degradation state: %s", e)

    def get_degradation_status(self) -> dict[str, Any]:
        """Get current degradation monitoring status for health endpoints.

        Returns:
            Status dictionary compatible with existing health checks
        """
        if not self.enable_degradation_monitoring:
            return {
                "status": "disabled",
                "message": "Degradation monitoring disabled",
            }

        if self.current_dashboard is None:
            return {
                "status": "no_data",
                "message": f"No degradation data (prices={len(self.price_history)}, "
                f"returns={len(self.return_history)})",
            }

        dashboard = self.current_dashboard
        health_level = dashboard.snapshot.overall_health

        if health_level.value == "GREEN":
            status = "healthy"
        elif health_level.value == "YELLOW":
            status = "warning"
        else:  # RED
            status = "critical"

        return {
            "status": status,
            "message": dashboard.summary_message,
            "health_score": dashboard.health_score.overall_score,
            "last_check": self.last_degradation_check.isoformat() if self.last_degradation_check else None,
            "alerts_count": len(dashboard.alerts),
        }
