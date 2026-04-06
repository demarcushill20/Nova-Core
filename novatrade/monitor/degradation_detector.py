"""Strategy degradation detection for NovaTrade.

Implements early warning system for strategy edge decay using:
1. Hurst exponent - measures mean reversion vs trending behavior
2. CUSUM monitoring - detects performance degradation 3-6 months early
3. Half-life tracker - monitors reversion speed changes

Based on research findings from OUTPUT/shift_20260405_13_novatrade_research__20260406-042500.md

Degradation Detection Framework:
- Hurst: Green H<0.45, Yellow 0.45-0.55, Red H>0.55
- CUSUM: Green S>-2%, Yellow -2% to -5%, Red S<-5%
- Action: All Green=normal, Any Yellow=reduce risk, Any Red=halt entries, 2+ Red=full pause
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import numpy as np
from scipy import stats

log = logging.getLogger("novatrade.monitor.degradation")


class HealthLevel(Enum):
    """Strategy health level classification."""

    GREEN = "GREEN"  # All systems normal
    YELLOW = "YELLOW"  # Caution - reduce risk
    RED = "RED"  # Critical - halt entries or full pause


@dataclass
class HurstMetrics:
    """Hurst exponent analysis results."""

    exponent: float  # H<0.5=mean-reverting, H>0.5=trending
    std_error: float
    sample_size: int
    confidence_interval: tuple[float, float]
    is_mean_reverting: bool = field(init=False)
    health_level: HealthLevel = field(init=False)

    def __post_init__(self) -> None:
        self.is_mean_reverting = self.exponent < 0.5
        # Research thresholds: Green H<0.45, Yellow 0.45-0.55, Red H>0.55
        if self.exponent < 0.45:
            self.health_level = HealthLevel.GREEN
        elif self.exponent <= 0.55:
            self.health_level = HealthLevel.YELLOW
        else:
            self.health_level = HealthLevel.RED


@dataclass
class CusumMetrics:
    """CUSUM performance monitoring results."""

    cusum_score: float  # Cumulative sum of performance deviations
    cusum_pct: float  # CUSUM as percentage of initial capital
    drift_detected: bool
    detection_threshold: float
    consecutive_negative: int
    health_level: HealthLevel = field(init=False)

    def __post_init__(self) -> None:
        # Research thresholds: Green S>-2%, Yellow -2% to -5%, Red S<-5%
        if self.cusum_pct > -2.0:
            self.health_level = HealthLevel.GREEN
        elif self.cusum_pct >= -5.0:
            self.health_level = HealthLevel.YELLOW
        else:
            self.health_level = HealthLevel.RED


@dataclass
class HalfLifeMetrics:
    """Half-life analysis for mean reversion speed."""

    half_life_bars: float  # Time for prices to revert halfway to mean
    mean_reversion_speed: float  # Ornstein-Uhlenbeck theta parameter
    r_squared: float  # Quality of half-life estimation
    sample_size: int
    health_level: HealthLevel = field(init=False)

    def __post_init__(self) -> None:
        # Research thresholds: Green 10-40 bars, Yellow 40-80, Red >80
        if 10 <= self.half_life_bars <= 40:
            self.health_level = HealthLevel.GREEN
        elif self.half_life_bars <= 80:
            self.health_level = HealthLevel.YELLOW
        else:
            self.health_level = HealthLevel.RED


@dataclass
class DegradationSnapshot:
    """Complete strategy degradation assessment."""

    timestamp: datetime
    symbol: str
    timeframe: str
    hurst_metrics: HurstMetrics
    cusum_metrics: CusumMetrics
    half_life_metrics: HalfLifeMetrics | None
    rolling_sharpe: float
    overall_health: HealthLevel = field(init=False)
    recommended_action: str = field(init=False)
    warning_message: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        """Determine overall health and recommended action."""
        levels = [self.hurst_metrics.health_level, self.cusum_metrics.health_level]
        if self.half_life_metrics:
            levels.append(self.half_life_metrics.health_level)

        red_count = sum(1 for level in levels if level == HealthLevel.RED)
        yellow_count = sum(1 for level in levels if level == HealthLevel.YELLOW)

        # Research action rules
        if red_count >= 2:
            self.overall_health = HealthLevel.RED
            self.recommended_action = "FULL_PAUSE"
            self.warning_message = f"Multiple RED signals detected ({red_count}/3) - full strategy pause recommended"
        elif red_count >= 1:
            self.overall_health = HealthLevel.RED
            self.recommended_action = "HALT_ENTRIES"
            self.warning_message = "RED signal detected - halt new entries, monitor existing positions"
        elif yellow_count >= 1:
            self.overall_health = HealthLevel.YELLOW
            self.recommended_action = "REDUCE_RISK"
            self.warning_message = "YELLOW signal detected - reduce position sizing and increase monitoring"
        else:
            self.overall_health = HealthLevel.GREEN
            self.recommended_action = "NORMAL_OPERATION"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "hurst_exponent": self.hurst_metrics.exponent,
            "hurst_health": self.hurst_metrics.health_level.value,
            "is_mean_reverting": self.hurst_metrics.is_mean_reverting,
            "cusum_pct": self.cusum_metrics.cusum_pct,
            "cusum_health": self.cusum_metrics.health_level.value,
            "half_life_bars": self.half_life_metrics.half_life_bars if self.half_life_metrics else None,
            "half_life_health": self.half_life_metrics.health_level.value if self.half_life_metrics else None,
            "rolling_sharpe": self.rolling_sharpe,
            "overall_health": self.overall_health.value,
            "recommended_action": self.recommended_action,
            "warning_message": self.warning_message,
        }


class DegradationDetector:
    """Strategy degradation monitoring using Hurst exponent, CUSUM, and half-life analysis."""

    def __init__(
        self,
        *,
        cusum_threshold: float = 5.0,
        hurst_window: int = 252,  # ~1 year of daily data
        half_life_window: int = 100,
        sharpe_window: int = 60,
    ) -> None:
        """Initialize degradation detector.

        Args:
            cusum_threshold: CUSUM detection threshold (% of capital)
            hurst_window: Minimum data points for Hurst calculation
            half_life_window: Window for half-life estimation
            sharpe_window: Window for rolling Sharpe ratio
        """
        self.cusum_threshold = cusum_threshold
        self.hurst_window = hurst_window
        self.half_life_window = half_life_window
        self.sharpe_window = sharpe_window

        # CUSUM state
        self.cusum_sum = 0.0
        self.cusum_history: list[float] = []
        self.returns_history: list[float] = []

    def calculate_hurst_exponent(self, price_series: list[float]) -> HurstMetrics:
        """Calculate Hurst exponent using rescaled range (R/S) analysis.

        Args:
            price_series: List of price values (close prices)

        Returns:
            HurstMetrics with exponent and confidence intervals
        """
        if len(price_series) < self.hurst_window:
            # Insufficient data - return neutral Hurst
            return HurstMetrics(
                exponent=0.5,
                std_error=0.0,
                sample_size=len(price_series),
                confidence_interval=(0.45, 0.55),
            )

        prices = np.array(price_series)
        log_prices = np.log(prices)
        returns = np.diff(log_prices)

        # R/S analysis with multiple time lags
        lags = np.logspace(1, np.log10(len(returns) // 4), num=20, dtype=int)
        lags = np.unique(lags)

        rs_values = []
        for lag in lags:
            if lag >= len(returns):
                continue

            # Split returns into non-overlapping chunks
            n_chunks = len(returns) // lag
            if n_chunks < 2:
                continue

            rs_chunk_values = []
            for i in range(n_chunks):
                chunk = returns[i * lag : (i + 1) * lag]
                if len(chunk) != lag:
                    continue

                # Mean-adjusted cumulative sum
                mean_return = np.mean(chunk)
                cumulative_devs = np.cumsum(chunk - mean_return)

                # Range of cumulative deviations
                range_val = np.max(cumulative_devs) - np.min(cumulative_devs)

                # Standard deviation
                std_val = np.std(chunk, ddof=1)

                # R/S ratio (avoid division by zero)
                if std_val > 1e-8:
                    rs_chunk_values.append(range_val / std_val)

            if rs_chunk_values:
                rs_values.append(np.mean(rs_chunk_values))

        if len(rs_values) < 5:
            # Insufficient valid R/S values
            return HurstMetrics(
                exponent=0.5,
                std_error=0.0,
                sample_size=len(price_series),
                confidence_interval=(0.45, 0.55),
            )

        # Linear regression: log(R/S) = H * log(lag) + constant
        log_lags = np.log(lags[: len(rs_values)])
        log_rs = np.log(rs_values)

        # Remove any infinite or NaN values
        valid_mask = np.isfinite(log_lags) & np.isfinite(log_rs)
        if np.sum(valid_mask) < 3:
            return HurstMetrics(
                exponent=0.5,
                std_error=0.0,
                sample_size=len(price_series),
                confidence_interval=(0.45, 0.55),
            )

        log_lags = log_lags[valid_mask]
        log_rs = log_rs[valid_mask]

        # Linear regression
        slope, intercept, r_value, p_value, std_err = stats.linregress(log_lags, log_rs)

        # Hurst exponent is the slope
        hurst = slope

        # 95% confidence interval
        t_stat = stats.t.ppf(0.975, len(log_lags) - 2)  # 95% CI
        margin = t_stat * std_err
        ci_lower = hurst - margin
        ci_upper = hurst + margin

        return HurstMetrics(
            exponent=hurst,
            std_error=std_err,
            sample_size=len(price_series),
            confidence_interval=(ci_lower, ci_upper),
        )

    def update_cusum(self, return_pct: float, target_return: float = 0.0) -> CusumMetrics:
        """Update CUSUM monitoring with new return.

        Args:
            return_pct: New return as percentage
            target_return: Expected return (default: 0)

        Returns:
            Updated CUSUM metrics
        """
        # CUSUM of performance deviations
        deviation = return_pct - target_return
        self.cusum_sum += deviation
        self.cusum_history.append(self.cusum_sum)
        self.returns_history.append(return_pct)

        # Keep rolling window
        max_history = 1000
        if len(self.cusum_history) > max_history:
            self.cusum_history.pop(0)
            self.returns_history.pop(0)

        # Check for drift detection (sustained negative performance)
        consecutive_negative = 0
        for ret in reversed(self.returns_history[-20:]):  # Last 20 observations
            if ret < target_return:
                consecutive_negative += 1
            else:
                break

        drift_detected = abs(self.cusum_sum) > self.cusum_threshold or consecutive_negative >= 10

        return CusumMetrics(
            cusum_score=self.cusum_sum,
            cusum_pct=self.cusum_sum,  # Assuming returns are already in %
            drift_detected=drift_detected,
            detection_threshold=self.cusum_threshold,
            consecutive_negative=consecutive_negative,
        )

    def calculate_half_life(self, price_series: list[float]) -> HalfLifeMetrics | None:
        """Calculate half-life of mean reversion using Ornstein-Uhlenbeck estimation.

        Args:
            price_series: List of price values

        Returns:
            HalfLifeMetrics or None if insufficient data
        """
        if len(price_series) < self.half_life_window:
            return None

        prices = np.array(price_series[-self.half_life_window :])
        log_prices = np.log(prices)

        # Ornstein-Uhlenbeck: dX_t = theta * (mu - X_t) * dt + sigma * dW_t
        # Discrete: X_t+1 = alpha + beta * X_t + epsilon_t
        # where beta = exp(-theta * dt), so theta = -ln(beta) / dt

        try:
            # Linear regression: log_price[t+1] = alpha + beta * log_price[t] + error
            x = log_prices[:-1]
            y = log_prices[1:]

            slope, intercept, r_value, p_value, std_err = stats.linregress(x, y)
            beta = slope

            # Half-life calculation
            if 0 < beta < 1:
                theta = -np.log(beta)  # Assuming dt = 1 (one time period)
                half_life = np.log(2) / theta
            else:
                # Not mean-reverting or unstable
                half_life = float("inf")

            r_squared = r_value**2

            return HalfLifeMetrics(
                half_life_bars=half_life if np.isfinite(half_life) else 1000.0,
                mean_reversion_speed=theta if "theta" in locals() else 0.0,
                r_squared=r_squared,
                sample_size=len(x),
            )

        except Exception as e:
            log.warning("Half-life calculation failed: %s", e)
            return None

    def calculate_rolling_sharpe(self, returns: list[float], risk_free_rate: float = 0.0) -> float:
        """Calculate rolling Sharpe ratio.

        Args:
            returns: List of return percentages
            risk_free_rate: Risk-free rate (annualized %)

        Returns:
            Rolling Sharpe ratio
        """
        if len(returns) < self.sharpe_window:
            return 0.0

        recent_returns = returns[-self.sharpe_window :]
        mean_return = np.mean(recent_returns)
        std_return = np.std(recent_returns, ddof=1)

        if std_return < 1e-8:
            return 0.0

        # Annualize assuming daily returns
        sharpe = (mean_return * 252 - risk_free_rate) / (std_return * np.sqrt(252))
        return sharpe

    def analyze_degradation(
        self,
        price_series: list[float],
        returns: list[float],
        symbol: str = "EURUSD",
        timeframe: str = "M5",
    ) -> DegradationSnapshot:
        """Perform complete degradation analysis.

        Args:
            price_series: List of price values (close prices)
            returns: List of return percentages
            symbol: Trading symbol
            timeframe: Chart timeframe

        Returns:
            Complete degradation snapshot
        """
        # Calculate all metrics
        hurst_metrics = self.calculate_hurst_exponent(price_series)

        # Update CUSUM with most recent return
        if returns:
            cusum_metrics = self.update_cusum(returns[-1])
        else:
            cusum_metrics = CusumMetrics(
                cusum_score=0.0,
                cusum_pct=0.0,
                drift_detected=False,
                detection_threshold=self.cusum_threshold,
                consecutive_negative=0,
            )

        half_life_metrics = self.calculate_half_life(price_series)
        rolling_sharpe = self.calculate_rolling_sharpe(returns)

        return DegradationSnapshot(
            timestamp=datetime.now(timezone.utc),
            symbol=symbol,
            timeframe=timeframe,
            hurst_metrics=hurst_metrics,
            cusum_metrics=cusum_metrics,
            half_life_metrics=half_life_metrics,
            rolling_sharpe=rolling_sharpe,
        )
