"""Tests for strategy degradation detection system.

Tests cover:
- Hurst exponent calculation with various market regimes
- CUSUM performance monitoring
- Half-life estimation for mean reversion
- Strategy health dashboard integration
- Alert generation and risk adjustments
"""

import math
from datetime import datetime, timezone

import numpy as np
import pytest

from novatrade.monitor.degradation_detector import (
    CusumMetrics,
    DegradationDetector,
    DegradationSnapshot,
    HalfLifeMetrics,
    HealthLevel,
    HurstMetrics,
)
from novatrade.monitor.strategy_health_dashboard import (
    HealthScore,
    StrategyHealthDashboard,
    StrategyHealthMonitor,
)


class TestHurstExponentCalculation:
    """Test Hurst exponent calculation for different market conditions."""

    def test_mean_reverting_series(self):
        """Test Hurst calculation on mean-reverting price series."""
        detector = DegradationDetector()

        # Generate mean-reverting series using Ornstein-Uhlenbeck
        np.random.seed(42)
        n_points = 500
        theta = 0.5  # Mean reversion speed
        mu = 100.0  # Long-term mean
        sigma = 0.02  # Volatility
        dt = 1.0

        prices = [mu]
        for _i in range(n_points - 1):
            dx = theta * (mu - prices[-1]) * dt + sigma * np.random.normal() * math.sqrt(dt)
            prices.append(prices[-1] + dx)

        metrics = detector.calculate_hurst_exponent(prices)

        # Mean-reverting series should have H < 0.5
        assert metrics.exponent < 0.5
        assert metrics.is_mean_reverting
        assert metrics.sample_size == n_points
        assert metrics.std_error > 0
        assert len(metrics.confidence_interval) == 2

    def test_trending_series(self):
        """Test Hurst calculation on trending price series."""
        detector = DegradationDetector()

        # Generate trending series with persistent moves
        np.random.seed(123)
        n_points = 500
        trend = 0.001  # Small upward trend
        vol = 0.01

        prices = [100.0]
        for _i in range(n_points - 1):
            # Persistent random walk with trend
            change = trend + vol * np.random.normal()
            if np.random.random() > 0.3:  # 70% chance to continue direction
                if len(prices) > 1 and prices[-1] > prices[-2]:
                    change = abs(change)  # Continue upward
                elif len(prices) > 1 and prices[-1] < prices[-2]:
                    change = -abs(change)  # Continue downward
            prices.append(prices[-1] * (1 + change))

        metrics = detector.calculate_hurst_exponent(prices)

        # Trending series should have H > 0.5
        assert metrics.exponent > 0.5
        assert not metrics.is_mean_reverting

    def test_insufficient_data(self):
        """Test Hurst calculation with insufficient data."""
        detector = DegradationDetector(hurst_window=100)
        prices = [100.0, 100.1, 99.9]  # Only 3 points

        metrics = detector.calculate_hurst_exponent(prices)

        # Should return neutral values for insufficient data
        assert metrics.exponent == 0.5
        assert metrics.std_error == 0.0
        assert metrics.sample_size == 3

    def test_health_level_classification(self):
        """Test health level classification based on Hurst values."""
        # Test different Hurst values and their health classifications
        test_cases = [
            (0.3, HealthLevel.GREEN),  # Strong mean reversion
            (0.44, HealthLevel.GREEN),  # Good mean reversion
            (0.5, HealthLevel.YELLOW),  # Transition zone
            (0.6, HealthLevel.RED),  # Trending behavior
        ]

        for hurst_value, expected_level in test_cases:
            metrics = HurstMetrics(
                exponent=hurst_value,
                std_error=0.05,
                sample_size=300,
                confidence_interval=(hurst_value - 0.1, hurst_value + 0.1),
            )
            assert metrics.health_level == expected_level


class TestCusumMonitoring:
    """Test CUSUM performance monitoring."""

    def test_cusum_normal_performance(self):
        """Test CUSUM with normal performance (no degradation)."""
        detector = DegradationDetector(cusum_threshold=5.0)

        # Simulate normal returns around 0%
        returns = [0.1, -0.1, 0.2, -0.05, 0.15, -0.2, 0.1]

        for ret in returns:
            metrics = detector.update_cusum(ret)

        assert abs(metrics.cusum_score) < 1.0  # Should stay near zero
        assert not metrics.drift_detected
        assert metrics.consecutive_negative < 5

    def test_cusum_degradation_detection(self):
        """Test CUSUM detection of performance degradation."""
        detector = DegradationDetector(cusum_threshold=2.0)  # Lower threshold

        # Simulate degrading performance
        degrading_returns = [-0.5, -0.3, -0.4, -0.6, -0.2, -0.7, -0.3]

        metrics = None
        for ret in degrading_returns:
            metrics = detector.update_cusum(ret)

        assert metrics.cusum_score < -2.0
        assert metrics.drift_detected
        assert metrics.consecutive_negative > 5

    def test_cusum_health_levels(self):
        """Test CUSUM health level classification."""
        # Test different CUSUM values
        test_cases = [
            (-1.0, HealthLevel.GREEN),  # Minor negative
            (-3.5, HealthLevel.YELLOW),  # Warning zone
            (-7.0, HealthLevel.RED),  # Critical zone
        ]

        for cusum_pct, expected_level in test_cases:
            metrics = CusumMetrics(
                cusum_score=cusum_pct,
                cusum_pct=cusum_pct,
                drift_detected=cusum_pct < -5.0,
                detection_threshold=5.0,
                consecutive_negative=int(abs(cusum_pct)),
            )
            assert metrics.health_level == expected_level


class TestHalfLifeCalculation:
    """Test half-life calculation for mean reversion."""

    def test_mean_reverting_half_life(self):
        """Test half-life calculation on mean-reverting series."""
        detector = DegradationDetector()

        # Generate OU process with known parameters
        np.random.seed(42)
        theta = 0.1  # Known mean reversion speed
        expected_half_life = math.log(2) / theta  # Theoretical half-life

        mu = 100.0
        sigma = 0.02
        n_points = 200
        dt = 1.0

        prices = [mu]
        for _i in range(n_points - 1):
            dx = theta * (mu - prices[-1]) * dt + sigma * np.random.normal() * math.sqrt(dt)
            prices.append(max(prices[-1] + dx, 1.0))  # Prevent negative prices

        metrics = detector.calculate_half_life(prices)

        assert metrics is not None
        # Half-life should be reasonable (within order of magnitude of theoretical value)
        assert 0.5 * expected_half_life < metrics.half_life_bars < 2 * expected_half_life
        assert metrics.r_squared > 0.1  # Some explanatory power
        assert metrics.sample_size > 50

    def test_trending_series_half_life(self):
        """Test half-life on trending (non-mean-reverting) series."""
        detector = DegradationDetector()

        # Generate trending series
        prices = [100.0]
        for _i in range(150):
            prices.append(prices[-1] * (1 + 0.001))  # Steady uptrend

        metrics = detector.calculate_half_life(prices)

        # Trending series should have very long half-life or infinite
        assert metrics is not None
        assert metrics.half_life_bars > 100 or metrics.half_life_bars == 1000.0

    def test_half_life_health_levels(self):
        """Test half-life health level classification."""
        test_cases = [
            (25.0, HealthLevel.GREEN),  # Optimal range
            (60.0, HealthLevel.YELLOW),  # Slower but acceptable
            (150.0, HealthLevel.RED),  # Too slow
        ]

        for half_life, expected_level in test_cases:
            metrics = HalfLifeMetrics(
                half_life_bars=half_life,
                mean_reversion_speed=math.log(2) / half_life,
                r_squared=0.5,
                sample_size=100,
            )
            assert metrics.health_level == expected_level


class TestDegradationSnapshot:
    """Test complete degradation analysis integration."""

    def test_healthy_strategy_snapshot(self):
        """Test degradation analysis for healthy strategy."""
        detector = DegradationDetector()

        # Generate healthy mean-reverting price series
        np.random.seed(42)
        prices = self._generate_mean_reverting_prices(300, theta=0.3)
        returns = [0.1, 0.05, -0.02, 0.08, -0.03, 0.12, 0.01]

        # Update CUSUM with positive returns
        for ret in returns:
            detector.update_cusum(ret)

        snapshot = detector.analyze_degradation(prices, returns, "EURUSD", "H1")

        assert snapshot.symbol == "EURUSD"
        assert snapshot.timeframe == "H1"
        assert snapshot.hurst_metrics.is_mean_reverting
        assert snapshot.cusum_metrics.health_level in [HealthLevel.GREEN, HealthLevel.YELLOW]
        assert snapshot.overall_health in [HealthLevel.GREEN, HealthLevel.YELLOW]

    def test_degraded_strategy_snapshot(self):
        """Test degradation analysis for degraded strategy."""
        detector = DegradationDetector()

        # Generate trending (non-mean-reverting) price series
        prices = []
        start_price = 100.0
        for i in range(300):
            # Strong trend with momentum
            trend = 0.002 if i < 150 else -0.001
            noise = 0.001 * np.random.normal()
            start_price *= 1 + trend + noise
            prices.append(start_price)

        # Negative returns (severe performance degradation for RED zone)
        bad_returns = [-0.8, -0.6, -0.7, -0.5, -0.9, -0.4, -0.6, -0.8, -0.3]

        # Update CUSUM with negative returns
        for ret in bad_returns:
            detector.update_cusum(ret)

        snapshot = detector.analyze_degradation(prices, bad_returns, "EURUSD", "M5")

        # Should detect problems
        assert not snapshot.hurst_metrics.is_mean_reverting  # Should be trending
        assert snapshot.cusum_metrics.health_level == HealthLevel.RED
        assert snapshot.overall_health == HealthLevel.RED
        assert snapshot.recommended_action in ["HALT_ENTRIES", "FULL_PAUSE"]

    def _generate_mean_reverting_prices(self, n_points: int, theta: float = 0.2) -> list[float]:
        """Generate synthetic mean-reverting price series."""
        np.random.seed(42)
        mu = 100.0
        sigma = 0.02
        dt = 1.0

        prices = [mu]
        for _i in range(n_points - 1):
            dx = theta * (mu - prices[-1]) * dt + sigma * np.random.normal() * math.sqrt(dt)
            prices.append(max(prices[-1] + dx, 1.0))

        return prices


class TestStrategyHealthDashboard:
    """Test strategy health dashboard and monitoring."""

    def test_health_score_calculation(self):
        """Test composite health score calculation."""
        monitor = StrategyHealthMonitor()

        # Create a degradation snapshot with known metrics
        hurst_metrics = HurstMetrics(
            exponent=0.4,  # Good mean reversion
            std_error=0.05,
            sample_size=300,
            confidence_interval=(0.35, 0.45),
        )
        cusum_metrics = CusumMetrics(
            cusum_score=-1.5,
            cusum_pct=-1.5,  # Good performance
            drift_detected=False,
            detection_threshold=5.0,
            consecutive_negative=1,
        )
        half_life_metrics = HalfLifeMetrics(
            half_life_bars=25.0,  # Optimal range
            mean_reversion_speed=0.03,
            r_squared=0.7,
            sample_size=100,
        )

        snapshot = DegradationSnapshot(
            timestamp=datetime.now(timezone.utc),
            symbol="EURUSD",
            timeframe="H1",
            hurst_metrics=hurst_metrics,
            cusum_metrics=cusum_metrics,
            half_life_metrics=half_life_metrics,
            rolling_sharpe=0.8,  # Good Sharpe
        )

        health_score = monitor.calculate_health_score(snapshot)

        # All metrics are good, so score should be high
        assert health_score.overall_score > 80
        assert health_score.hurst_score >= 80
        assert health_score.cusum_score >= 80
        assert health_score.sharpe_score >= 70
        assert health_score.grade in ["A+", "A"]

    def test_alert_generation(self):
        """Test health alert generation."""
        monitor = StrategyHealthMonitor()

        # Create snapshot with problems
        hurst_metrics = HurstMetrics(
            exponent=0.7,  # Trending (bad for mean reversion)
            std_error=0.05,
            sample_size=300,
            confidence_interval=(0.65, 0.75),
        )
        cusum_metrics = CusumMetrics(
            cusum_score=-6.0,
            cusum_pct=-6.0,  # Poor performance
            drift_detected=True,
            detection_threshold=5.0,
            consecutive_negative=8,
        )

        snapshot = DegradationSnapshot(
            timestamp=datetime.now(timezone.utc),
            symbol="EURUSD",
            timeframe="M5",
            hurst_metrics=hurst_metrics,
            cusum_metrics=cusum_metrics,
            half_life_metrics=None,
            rolling_sharpe=-0.3,  # Poor Sharpe
        )

        alerts = monitor.generate_alerts(snapshot)

        # Should have alerts for all problematic metrics
        alert_components = [alert.component for alert in alerts]
        assert "hurst" in alert_components
        assert "cusum" in alert_components
        assert "sharpe" in alert_components

        # Check alert levels
        hurst_alert = next(a for a in alerts if a.component == "hurst")
        assert hurst_alert.level == HealthLevel.RED

    def test_risk_adjustment_calculations(self):
        """Test position sizing — degradation gating disabled, always full size."""
        monitor = StrategyHealthMonitor()

        # All actions should return full sizing — drawdown limits handle risk
        for action, score in [
            ("NORMAL_OPERATION", 85),
            ("REDUCE_RISK", 75),
            ("REDUCE_RISK", 55),
            ("HALT_ENTRIES", 45),
            ("FULL_PAUSE", 25),
        ]:
            snapshot = type("MockSnapshot", (), {"recommended_action": action})()
            health_score = HealthScore(
                overall_score=score,
                hurst_score=score,
                cusum_score=score,
                half_life_score=score,
                sharpe_score=score,
            )

            mult, halt, reduce, pause = monitor.calculate_risk_adjustments(snapshot, health_score)

            assert mult == 1.0, f"Expected full sizing for {action}/{score}, got {mult}"
            assert halt is False
            assert reduce is False
            assert pause is False

    def test_full_health_assessment(self):
        """Test complete strategy health assessment."""
        monitor = StrategyHealthMonitor()

        # Generate test data
        np.random.seed(42)
        prices = self._generate_test_prices(250)
        returns = [0.1, 0.05, -0.02, 0.08, -0.03, 0.12, 0.01]

        dashboard = monitor.assess_strategy_health(prices, returns, "EURUSD", "H1")

        assert isinstance(dashboard, StrategyHealthDashboard)
        assert dashboard.snapshot.symbol == "EURUSD"
        assert dashboard.snapshot.timeframe == "H1"
        assert isinstance(dashboard.health_score, HealthScore)
        assert isinstance(dashboard.alerts, list)
        assert 0.0 <= dashboard.position_sizing_multiplier <= 1.0
        assert isinstance(dashboard.summary_message, str)
        assert dashboard.next_check_recommended > datetime.now(timezone.utc)

        # Test serialization
        dashboard_dict = dashboard.to_dict()
        assert "health_score" in dashboard_dict
        assert "degradation_metrics" in dashboard_dict
        assert "risk_management" in dashboard_dict
        assert "recommendations" in dashboard_dict

    def test_health_trend_analysis(self):
        """Test health trend analysis over time."""
        monitor = StrategyHealthMonitor()

        # Generate declining health scenarios
        prices = self._generate_test_prices(200)

        # First assessment (good performance)
        returns1 = [0.1, 0.05, 0.08, 0.12, 0.01]
        dashboard1 = monitor.assess_strategy_health(prices, returns1)
        assert dashboard1 is not None

        # Second assessment (declining performance)
        returns2 = [-0.05, -0.02, -0.03, 0.01, -0.01]
        dashboard2 = monitor.assess_strategy_health(prices, returns2)
        assert dashboard2 is not None

        # Third assessment (poor performance)
        returns3 = [-0.15, -0.12, -0.08, -0.05, -0.10]
        dashboard3 = monitor.assess_strategy_health(prices, returns3)
        assert dashboard3 is not None

        # Check trend analysis
        trend = monitor.get_health_trend(window=3)
        assert trend in ["IMPROVING", "STABLE", "DECLINING"]

        # Should have history
        assert len(monitor.health_history) == 3

    def _generate_test_prices(self, n_points: int) -> list[float]:
        """Generate test price series."""
        np.random.seed(42)
        prices = [100.0]
        for _i in range(n_points - 1):
            change = 0.001 * np.random.normal()
            prices.append(prices[-1] * (1 + change))
        return prices


class TestIntegrationScenarios:
    """Test realistic integration scenarios."""

    def test_forex_mean_reversion_scenario(self):
        """Test EUR/USD mean reversion monitoring scenario."""
        monitor = StrategyHealthMonitor()

        # Simulate EUR/USD ranging market (good for mean reversion)
        np.random.seed(123)
        prices = []
        base_price = 1.1000

        for i in range(300):
            # Simulate ranging behavior around 1.1000
            if i < 100:
                # Initial ranging
                change = 0.0002 * np.random.normal()
            elif i < 200:
                # Trend break (should trigger alerts)
                change = 0.0005 * (1 if i % 20 < 15 else -1) + 0.0001 * np.random.normal()
            else:
                # Return to ranging
                revert = -0.3 * (base_price - 1.1000) if abs(base_price - 1.1000) > 0.005 else 0
                change = revert + 0.0001 * np.random.normal()

            base_price += change
            prices.append(base_price)

        # Simulate strategy returns during different periods
        good_returns = [0.08, 0.12, 0.05, 0.15, 0.03, 0.10]  # Good ranging period
        bad_returns = [-0.05, -0.08, -0.03, -0.10, -0.02]  # Trend break period

        # Test good period
        dashboard1 = monitor.assess_strategy_health(prices[:100], good_returns, "EURUSD", "M5")
        assert dashboard1.health_score.overall_score > 60
        assert not dashboard1.should_pause_strategy

        # Test bad period (trend break) — degradation gating disabled,
        # so should_reduce_risk is always False (drawdown limits handle risk)
        dashboard2 = monitor.assess_strategy_health(prices[100:200], bad_returns, "EURUSD", "M5")
        assert not dashboard2.should_reduce_risk
        assert not dashboard2.should_halt_entries

    def test_strategy_recovery_scenario(self):
        """Test strategy recovery detection."""
        detector = DegradationDetector()

        # Simulate initial severe degradation (for RED zone < -5%)
        bad_returns = [-0.8, -0.7, -0.9, -0.6, -1.0, -0.5, -0.8]
        for ret in bad_returns:
            detector.update_cusum(ret)

        # Check degradation detected
        cusum1 = detector.update_cusum(-0.1)
        assert cusum1.health_level == HealthLevel.RED

        # Simulate recovery
        good_returns = [0.8, 0.6, 0.4, 0.5, 0.3, 0.7, 0.2]
        for ret in good_returns:
            detector.update_cusum(ret)

        # Check recovery
        cusum2 = detector.update_cusum(0.1)
        assert cusum2.health_level in [HealthLevel.GREEN, HealthLevel.YELLOW]


if __name__ == "__main__":
    pytest.main([__file__])
