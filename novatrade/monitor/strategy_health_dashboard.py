"""Strategy Health Dashboard for NovaTrade.

Unified degradation signal combining Hurst exponent, CUSUM, and Sharpe ratio.
Provides comprehensive strategy monitoring and automatic risk management triggers.

Based on research framework:
- Action: All Green=normal, Any Yellow=reduce risk, Any Red=halt entries, 2+ Red=full pause
"""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novatrade.monitor.degradation_detector import DegradationDetector, DegradationSnapshot, HealthLevel

log = logging.getLogger("novatrade.monitor.strategy_health")

# Health dashboard state persistence
STATE_DIR = Path("/home/nova/nova-core/STATE/novatrade")
HEALTH_FILE = STATE_DIR / "strategy_health.json"


@dataclass
class HealthAlert:
    """Strategy health alert with context."""

    level: HealthLevel
    component: str  # hurst, cusum, half_life, sharpe
    message: str
    value: float | str
    threshold: float | str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "component": self.component,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class HealthScore:
    """Composite strategy health score (0-100)."""

    overall_score: float  # 0-100 composite score
    hurst_score: float  # 0-100 Hurst component score
    cusum_score: float  # 0-100 CUSUM component score
    half_life_score: float  # 0-100 Half-life component score
    sharpe_score: float  # 0-100 Sharpe component score

    @property
    def grade(self) -> str:
        """Health grade: A+ (90-100), A (80-89), B (70-79), C (60-69), D (50-59), F (0-49)."""
        if self.overall_score >= 90:
            return "A+"
        elif self.overall_score >= 80:
            return "A"
        elif self.overall_score >= 70:
            return "B"
        elif self.overall_score >= 60:
            return "C"
        elif self.overall_score >= 50:
            return "D"
        else:
            return "F"


@dataclass
class StrategyHealthDashboard:
    """Complete strategy health assessment with recommendations."""

    snapshot: DegradationSnapshot
    health_score: HealthScore
    alerts: list[HealthAlert]
    position_sizing_multiplier: float  # 0.0-1.0 risk adjustment
    should_halt_entries: bool
    should_reduce_risk: bool
    should_pause_strategy: bool
    next_check_recommended: datetime
    summary_message: str = field(init=False)

    def __post_init__(self) -> None:
        """Generate summary message."""
        grade = self.health_score.grade
        action = self.snapshot.recommended_action

        if self.should_pause_strategy:
            self.summary_message = (
                f"CRITICAL: Strategy health {grade} ({self.health_score.overall_score:.0f}/100) - {action}"
            )
        elif self.should_halt_entries:
            self.summary_message = (
                f"WARNING: Strategy health {grade} ({self.health_score.overall_score:.0f}/100) - {action}"
            )
        elif self.should_reduce_risk:
            self.summary_message = (
                f"CAUTION: Strategy health {grade} ({self.health_score.overall_score:.0f}/100) - {action}"
            )
        else:
            self.summary_message = (
                f"HEALTHY: Strategy health {grade} ({self.health_score.overall_score:.0f}/100) - {action}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.snapshot.timestamp.isoformat(),
            "symbol": self.snapshot.symbol,
            "timeframe": self.snapshot.timeframe,
            "health_score": asdict(self.health_score),
            "degradation_metrics": self.snapshot.to_dict(),
            "alerts": [alert.to_dict() for alert in self.alerts],
            "risk_management": {
                "position_sizing_multiplier": self.position_sizing_multiplier,
                "should_halt_entries": self.should_halt_entries,
                "should_reduce_risk": self.should_reduce_risk,
                "should_pause_strategy": self.should_pause_strategy,
            },
            "recommendations": {
                "action": self.snapshot.recommended_action,
                "next_check": self.next_check_recommended.isoformat(),
                "summary": self.summary_message,
            },
        }


class StrategyHealthMonitor:
    """Real-time strategy health monitoring and alerting system."""

    def __init__(
        self,
        *,
        degradation_detector: DegradationDetector | None = None,
        sharpe_warning_threshold: float = 0.0,
        sharpe_critical_threshold: float = -0.5,
        check_interval_minutes: int = 60,
    ) -> None:
        """Initialize strategy health monitor.

        Args:
            degradation_detector: Custom detector instance (creates default if None)
            sharpe_warning_threshold: Sharpe ratio warning level
            sharpe_critical_threshold: Sharpe ratio critical level
            check_interval_minutes: Minutes between health checks
        """
        self.detector = degradation_detector or DegradationDetector()
        self.sharpe_warning = sharpe_warning_threshold
        self.sharpe_critical = sharpe_critical_threshold
        self.check_interval_minutes = check_interval_minutes

        # Health history for trend analysis
        self.health_history: list[StrategyHealthDashboard] = []
        self.max_history = 1000

    def calculate_health_score(self, snapshot: DegradationSnapshot) -> HealthScore:
        """Calculate composite health score from degradation metrics.

        Args:
            snapshot: Latest degradation analysis

        Returns:
            Composite health score (0-100 scale)
        """
        # Hurst score: optimal around 0.3-0.4 for mean reversion
        hurst = snapshot.hurst_metrics.exponent
        if hurst < 0.3:
            hurst_score = 85.0 + (0.3 - hurst) * 50  # Bonus for strong mean reversion
        elif hurst <= 0.45:
            hurst_score = 85.0  # Green zone
        elif hurst <= 0.55:
            hurst_score = 60.0  # Yellow zone
        else:
            hurst_score = max(0.0, 40.0 - (hurst - 0.55) * 100)  # Red zone

        # CUSUM score: closer to 0% is better
        cusum_pct = snapshot.cusum_metrics.cusum_pct
        if cusum_pct > -1.0:
            cusum_score = 90.0
        elif cusum_pct >= -2.0:
            cusum_score = 80.0  # Green zone
        elif cusum_pct >= -5.0:
            cusum_score = 50.0  # Yellow zone
        else:
            cusum_score = max(0.0, 25.0 + cusum_pct * 5)  # Red zone

        # Half-life score: 10-40 bars is optimal
        if snapshot.half_life_metrics:
            half_life = snapshot.half_life_metrics.half_life_bars
            if 10 <= half_life <= 40:
                half_life_score = 85.0
            elif half_life <= 80:
                half_life_score = 60.0
            else:
                half_life_score = max(0.0, 40.0 - (half_life - 80) * 0.5)
        else:
            half_life_score = 50.0  # Neutral if unavailable

        # Sharpe score: >0.5 is good, 0-0.5 is acceptable, <0 is bad
        sharpe = snapshot.rolling_sharpe
        if sharpe > 1.0:
            sharpe_score = 90.0
        elif sharpe >= 0.5:
            sharpe_score = 80.0
        elif sharpe >= 0.0:
            sharpe_score = 60.0
        elif sharpe >= -0.5:
            sharpe_score = 30.0
        else:
            sharpe_score = max(0.0, 10.0 + sharpe * 20)

        # Weighted composite score
        # Hurst and CUSUM are most important for edge decay detection
        overall_score = hurst_score * 0.35 + cusum_score * 0.30 + sharpe_score * 0.20 + half_life_score * 0.15

        return HealthScore(
            overall_score=round(overall_score, 1),
            hurst_score=round(hurst_score, 1),
            cusum_score=round(cusum_score, 1),
            half_life_score=round(half_life_score, 1),
            sharpe_score=round(sharpe_score, 1),
        )

    def generate_alerts(self, snapshot: DegradationSnapshot) -> list[HealthAlert]:
        """Generate health alerts based on degradation metrics.

        Args:
            snapshot: Latest degradation analysis

        Returns:
            List of active health alerts
        """
        alerts = []

        # Hurst exponent alerts
        hurst = snapshot.hurst_metrics
        if hurst.health_level == HealthLevel.RED:
            alerts.append(
                HealthAlert(
                    level=HealthLevel.RED,
                    component="hurst",
                    message=f"Hurst exponent {hurst.exponent:.3f} indicates strong trending (not mean-reverting)",
                    value=hurst.exponent,
                    threshold=0.55,
                )
            )
        elif hurst.health_level == HealthLevel.YELLOW:
            alerts.append(
                HealthAlert(
                    level=HealthLevel.YELLOW,
                    component="hurst",
                    message=f"Hurst exponent {hurst.exponent:.3f} in transition zone - reduced mean reversion",
                    value=hurst.exponent,
                    threshold="0.45-0.55",
                )
            )

        # CUSUM alerts
        cusum = snapshot.cusum_metrics
        if cusum.health_level == HealthLevel.RED:
            alerts.append(
                HealthAlert(
                    level=HealthLevel.RED,
                    component="cusum",
                    message=f"CUSUM {cusum.cusum_pct:.1f}% indicates significant performance degradation",
                    value=cusum.cusum_pct,
                    threshold=-5.0,
                )
            )
        elif cusum.health_level == HealthLevel.YELLOW:
            alerts.append(
                HealthAlert(
                    level=HealthLevel.YELLOW,
                    component="cusum",
                    message=f"CUSUM {cusum.cusum_pct:.1f}% shows early warning of performance decline",
                    value=cusum.cusum_pct,
                    threshold="-2% to -5%",
                )
            )

        # Half-life alerts
        if snapshot.half_life_metrics:
            half_life = snapshot.half_life_metrics
            if half_life.health_level == HealthLevel.RED:
                alerts.append(
                    HealthAlert(
                        level=HealthLevel.RED,
                        component="half_life",
                        message=f"Mean reversion half-life {half_life.half_life_bars:.1f} bars too slow",
                        value=half_life.half_life_bars,
                        threshold=80.0,
                    )
                )
            elif half_life.health_level == HealthLevel.YELLOW:
                alerts.append(
                    HealthAlert(
                        level=HealthLevel.YELLOW,
                        component="half_life",
                        message=f"Mean reversion half-life {half_life.half_life_bars:.1f} bars slower than optimal",
                        value=half_life.half_life_bars,
                        threshold="40-80 bars",
                    )
                )

        # Sharpe ratio alerts
        sharpe = snapshot.rolling_sharpe
        if sharpe <= self.sharpe_critical:
            alerts.append(
                HealthAlert(
                    level=HealthLevel.RED,
                    component="sharpe",
                    message=f"Rolling Sharpe ratio {sharpe:.2f} critically low",
                    value=sharpe,
                    threshold=self.sharpe_critical,
                )
            )
        elif sharpe <= self.sharpe_warning:
            alerts.append(
                HealthAlert(
                    level=HealthLevel.YELLOW,
                    component="sharpe",
                    message=f"Rolling Sharpe ratio {sharpe:.2f} below warning threshold",
                    value=sharpe,
                    threshold=self.sharpe_warning,
                )
            )

        return alerts

    def calculate_risk_adjustments(
        self, snapshot: DegradationSnapshot, health_score: HealthScore
    ) -> tuple[float, bool, bool, bool]:
        """Calculate position sizing and trading restrictions.

        Args:
            snapshot: Latest degradation analysis
            health_score: Composite health score

        Returns:
            Tuple of (position_multiplier, halt_entries, reduce_risk, pause_strategy)
        """
        action = snapshot.recommended_action

        if action == "FULL_PAUSE":
            return 0.0, True, True, True
        elif action == "HALT_ENTRIES":
            return 0.0, True, True, False
        elif action == "REDUCE_RISK":
            # Gradual risk reduction based on health score
            if health_score.overall_score >= 70:
                multiplier = 0.75  # 25% reduction
            elif health_score.overall_score >= 60:
                multiplier = 0.50  # 50% reduction
            else:
                multiplier = 0.25  # 75% reduction
            return multiplier, False, True, False
        else:
            return 1.0, False, False, False

    def assess_strategy_health(
        self,
        price_series: list[float],
        returns: list[float],
        symbol: str = "EURUSD",
        timeframe: str = "M5",
    ) -> StrategyHealthDashboard:
        """Perform comprehensive strategy health assessment.

        Args:
            price_series: List of price values (close prices)
            returns: List of return percentages
            symbol: Trading symbol
            timeframe: Chart timeframe

        Returns:
            Complete strategy health dashboard
        """
        # Get degradation analysis
        snapshot = self.detector.analyze_degradation(price_series, returns, symbol, timeframe)

        # Calculate health scores
        health_score = self.calculate_health_score(snapshot)

        # Generate alerts
        alerts = self.generate_alerts(snapshot)

        # Calculate risk adjustments
        pos_mult, halt_entries, reduce_risk, pause_strategy = self.calculate_risk_adjustments(snapshot, health_score)

        # Next check recommendation based on health level
        from datetime import timedelta

        if pause_strategy:
            next_check = datetime.now(timezone.utc) + timedelta(hours=6)  # Check every 6 hours when paused
        elif halt_entries:
            next_check = datetime.now(timezone.utc) + timedelta(hours=2)  # Check every 2 hours when halted
        elif reduce_risk:
            next_check = datetime.now(timezone.utc) + timedelta(hours=1)  # Check hourly when reducing risk
        else:
            next_check = datetime.now(timezone.utc) + timedelta(minutes=self.check_interval_minutes)

        dashboard = StrategyHealthDashboard(
            snapshot=snapshot,
            health_score=health_score,
            alerts=alerts,
            position_sizing_multiplier=pos_mult,
            should_halt_entries=halt_entries,
            should_reduce_risk=reduce_risk,
            should_pause_strategy=pause_strategy,
            next_check_recommended=next_check,
        )

        # Add to history
        self.health_history.append(dashboard)
        if len(self.health_history) > self.max_history:
            self.health_history.pop(0)

        # Persist to disk
        self._persist_health_state(dashboard)

        return dashboard

    def get_health_trend(self, window: int = 10) -> str:
        """Analyze health trend over recent assessments.

        Args:
            window: Number of recent assessments to analyze

        Returns:
            Trend description: IMPROVING, STABLE, DECLINING, INSUFFICIENT_DATA
        """
        if len(self.health_history) < 2:
            return "INSUFFICIENT_DATA"

        recent = self.health_history[-min(window, len(self.health_history)) :]
        scores = [h.health_score.overall_score for h in recent]

        if len(scores) < 2:
            return "STABLE"

        # Simple trend analysis
        trend = scores[-1] - scores[0]

        if trend > 5.0:
            return "IMPROVING"
        elif trend < -5.0:
            return "DECLINING"
        else:
            return "STABLE"

    def load_health_state(self) -> StrategyHealthDashboard | None:
        """Load the last persisted health state."""
        try:
            if HEALTH_FILE.exists():
                # Note: This is a simplified load - full reconstruction would require
                # rebuilding the dataclass hierarchy from the JSON data
                json.loads(HEALTH_FILE.read_text())  # Validate JSON format
                return None  # Return None for now, implement full deserialization if needed
        except (json.JSONDecodeError, TypeError, OSError):
            pass
        return None

    def _persist_health_state(self, dashboard: StrategyHealthDashboard) -> None:
        """Atomically write health state to disk."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)

        fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
        try:
            with open(fd, "w") as f:
                json.dump(dashboard.to_dict(), f, indent=2)
            import os

            os.replace(tmp, str(HEALTH_FILE))
        except BaseException:
            try:
                import os

                os.unlink(tmp)
            except OSError:
                pass
            raise
