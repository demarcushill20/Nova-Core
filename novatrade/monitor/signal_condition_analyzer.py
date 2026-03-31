"""Enhanced signal condition analyzer for NovaTrade strategies.

Provides detailed analysis of why entry conditions are not being met,
tracks proximity to signals, and generates actionable insights for
strategy optimization. This module addresses the gap where strategies
are running but not triggering trades by providing transparency into
the decision-making process.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

log = logging.getLogger("novatrade.monitor.signal_condition")


class ConditionStatus(Enum):
    """Status of individual entry conditions."""
    PASSED = "PASSED"
    FAILED = "FAILED"
    NEAR_THRESHOLD = "NEAR_THRESHOLD"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ProximityLevel(Enum):
    """How close conditions are to triggering."""
    VERY_CLOSE = "VERY_CLOSE"  # Within 5% of threshold
    CLOSE = "CLOSE"  # Within 15% of threshold
    MODERATE = "MODERATE"  # Within 30% of threshold
    FAR = "FAR"  # More than 30% away


@dataclass
class ConditionResult:
    """Result of checking a single entry condition."""
    name: str
    status: ConditionStatus
    actual_value: float
    threshold_value: float
    proximity_pct: float
    proximity_level: ProximityLevel
    message: str


@dataclass
class SignalAnalysis:
    """Complete analysis of signal conditions for a bar."""
    timestamp: float
    bar_index: int
    strategy_name: str
    symbol: str
    timeframe: str
    overall_status: str  # "NO_SIGNAL", "NEAR_SIGNAL", "SIGNAL_TRIGGERED"
    conditions: List[ConditionResult] = field(default_factory=list)
    proximity_score: float = 0.0  # 0-100, higher means closer to signal
    blocking_conditions: List[str] = field(default_factory=list)
    near_conditions: List[str] = field(default_factory=list)


class SignalConditionAnalyzer:
    """Analyzes signal conditions and tracks proximity to triggers."""

    def __init__(self):
        self.history: List[SignalAnalysis] = []
        self.max_history = 100

    def analyze_irb_conditions(
        self,
        bar_index: int,
        candles: List[Any],
        indicators: Dict[str, List[float]],
        env: Any,
        symbol: str = "EURUSD",
        timeframe: str = "H1"
    ) -> SignalAnalysis:
        """Analyze IRB strategy entry conditions in detail."""

        analysis = SignalAnalysis(
            timestamp=time.time(),
            bar_index=bar_index,
            strategy_name="IRB",
            symbol=symbol,
            timeframe=timeframe,
            overall_status="NO_SIGNAL"
        )

        if bar_index < env.warmup_bars or bar_index >= len(candles):
            analysis.overall_status = "INSUFFICIENT_DATA"
            return analysis

        bar = candles[bar_index]
        ema = indicators.get("ema", [])
        atr = indicators.get("atr", [])
        adx = indicators.get("adx", [])

        # Check data availability
        if (bar_index >= len(ema) or bar_index >= len(atr) or
            bar_index >= len(adx)):
            analysis.overall_status = "MISSING_INDICATORS"
            return analysis

        # Analyze each condition
        conditions = []

        # 1. IRB Geometry Check
        rng = bar.high - bar.low
        if rng > 0:
            up_threshold = bar.high - (env.irb_threshold * rng)
            down_threshold = bar.low + (env.irb_threshold * rng)

            # Check if close is in reversal zones
            up_zone_hit = bar.close >= up_threshold
            down_zone_hit = bar.close <= down_threshold

            if up_zone_hit or down_zone_hit:
                conditions.append(ConditionResult(
                    name="IRB_geometry",
                    status=ConditionStatus.PASSED,
                    actual_value=bar.close,
                    threshold_value=up_threshold if up_zone_hit else down_threshold,
                    proximity_pct=0.0,
                    proximity_level=ProximityLevel.VERY_CLOSE,
                    message=f"Close {bar.close:.5f} in {'upper' if up_zone_hit else 'lower'} reversal zone"
                ))
            else:
                # Calculate proximity to either zone
                up_dist = abs(bar.close - up_threshold) / rng * 100
                down_dist = abs(bar.close - down_threshold) / rng * 100
                min_dist = min(up_dist, down_dist)

                proximity_level = self._calculate_proximity_level(min_dist)

                conditions.append(ConditionResult(
                    name="IRB_geometry",
                    status=ConditionStatus.FAILED,
                    actual_value=bar.close,
                    threshold_value=up_threshold if up_dist < down_dist else down_threshold,
                    proximity_pct=min_dist,
                    proximity_level=proximity_level,
                    message=f"Close {bar.close:.5f} not in reversal zones (closest: {min_dist:.1f}% away)"
                ))

        # 2. EMA Trend Filter
        if bar_index < len(ema) and ema[bar_index]:
            ema_val = ema[bar_index]
            price_ema_diff_pct = abs(bar.close - ema_val) / ema_val * 100

            trend_up = bar.close > ema_val
            trend_down = bar.close < ema_val

            conditions.append(ConditionResult(
                name="EMA_trend",
                status=ConditionStatus.PASSED if (trend_up or trend_down) else ConditionStatus.FAILED,
                actual_value=bar.close,
                threshold_value=ema_val,
                proximity_pct=price_ema_diff_pct,
                proximity_level=self._calculate_proximity_level(price_ema_diff_pct),
                message=f"Price {'above' if trend_up else 'below'} EMA ({price_ema_diff_pct:.2f}% diff)"
            ))

        # 3. ADX Filter
        if bar_index < len(adx) and adx[bar_index]:
            adx_val = adx[bar_index]
            adx_passes = adx_val >= env.adx_threshold
            adx_proximity = abs(adx_val - env.adx_threshold) / env.adx_threshold * 100

            conditions.append(ConditionResult(
                name="ADX_filter",
                status=ConditionStatus.PASSED if adx_passes else ConditionStatus.FAILED,
                actual_value=adx_val,
                threshold_value=env.adx_threshold,
                proximity_pct=adx_proximity if not adx_passes else 0.0,
                proximity_level=self._calculate_proximity_level(adx_proximity),
                message=f"ADX {adx_val:.2f} {'≥' if adx_passes else '<'} {env.adx_threshold} threshold"
            ))

        # 4. ATR Volatility Check
        if bar_index < len(atr) and atr[bar_index]:
            atr_val = atr[bar_index]
            conditions.append(ConditionResult(
                name="ATR_volatility",
                status=ConditionStatus.PASSED if atr_val > 0 else ConditionStatus.FAILED,
                actual_value=atr_val,
                threshold_value=0.0,
                proximity_pct=0.0,
                proximity_level=ProximityLevel.VERY_CLOSE,
                message=f"ATR {atr_val:.5f} ({'valid' if atr_val > 0 else 'invalid'})"
            ))

        analysis.conditions = conditions

        # Calculate overall status and proximity
        passed_conditions = [c for c in conditions if c.status == ConditionStatus.PASSED]
        near_conditions = [c for c in conditions if c.proximity_level in [ProximityLevel.VERY_CLOSE, ProximityLevel.CLOSE]]
        blocking_conditions = [c for c in conditions if c.status == ConditionStatus.FAILED]

        analysis.blocking_conditions = [c.name for c in blocking_conditions]
        analysis.near_conditions = [c.name for c in near_conditions if c.status != ConditionStatus.PASSED]

        # Calculate proximity score (higher = closer to signal)
        if conditions:
            proximity_scores = []
            for condition in conditions:
                if condition.status == ConditionStatus.PASSED:
                    proximity_scores.append(100.0)
                elif condition.proximity_level == ProximityLevel.VERY_CLOSE:
                    proximity_scores.append(90.0)
                elif condition.proximity_level == ProximityLevel.CLOSE:
                    proximity_scores.append(75.0)
                elif condition.proximity_level == ProximityLevel.MODERATE:
                    proximity_scores.append(50.0)
                else:
                    proximity_scores.append(25.0)

            analysis.proximity_score = sum(proximity_scores) / len(proximity_scores)

        # Determine overall status
        if len(passed_conditions) == len(conditions):
            analysis.overall_status = "SIGNAL_TRIGGERED"
        elif analysis.proximity_score >= 80:
            analysis.overall_status = "NEAR_SIGNAL"
        else:
            analysis.overall_status = "NO_SIGNAL"

        # Store in history
        self.history.append(analysis)
        if len(self.history) > self.max_history:
            self.history.pop(0)

        return analysis

    def _calculate_proximity_level(self, distance_pct: float) -> ProximityLevel:
        """Calculate proximity level based on percentage distance."""
        if distance_pct <= 5.0:
            return ProximityLevel.VERY_CLOSE
        elif distance_pct <= 15.0:
            return ProximityLevel.CLOSE
        elif distance_pct <= 30.0:
            return ProximityLevel.MODERATE
        else:
            return ProximityLevel.FAR

    def get_summary_stats(self, lookback_bars: int = 20) -> Dict[str, Any]:
        """Get summary statistics for recent signal analysis."""
        recent = self.history[-lookback_bars:] if self.history else []

        if not recent:
            return {"error": "No analysis history available"}

        total_bars = len(recent)
        signals_triggered = len([a for a in recent if a.overall_status == "SIGNAL_TRIGGERED"])
        near_signals = len([a for a in recent if a.overall_status == "NEAR_SIGNAL"])

        # Most common blocking conditions
        blocking_counts = {}
        for analysis in recent:
            for condition in analysis.blocking_conditions:
                blocking_counts[condition] = blocking_counts.get(condition, 0) + 1

        # Average proximity score
        avg_proximity = sum(a.proximity_score for a in recent) / total_bars

        return {
            "lookback_bars": total_bars,
            "signals_triggered": signals_triggered,
            "near_signals": near_signals,
            "signal_rate_pct": (signals_triggered / total_bars) * 100,
            "avg_proximity_score": round(avg_proximity, 2),
            "most_common_blockers": dict(sorted(blocking_counts.items(), key=lambda x: x[1], reverse=True)),
            "recent_proximity_trend": [round(a.proximity_score, 1) for a in recent[-5:]]
        }

    def get_current_status_message(self) -> str:
        """Get human-readable status message for current conditions."""
        if not self.history:
            return "No signal analysis available yet"

        latest = self.history[-1]

        if latest.overall_status == "SIGNAL_TRIGGERED":
            return f"✅ Signal triggered at bar {latest.bar_index}"
        elif latest.overall_status == "NEAR_SIGNAL":
            return f"⚡ Near signal (score: {latest.proximity_score:.1f}/100) - blocked by: {', '.join(latest.blocking_conditions)}"
        else:
            return f"⏳ No signal (score: {latest.proximity_score:.1f}/100) - main blockers: {', '.join(latest.blocking_conditions[:2])}"