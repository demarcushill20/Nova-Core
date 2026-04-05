"""Tests for EMA confirm bars filter.

The EMA confirm bars filter requires the last N bars to close above EMA fast
for LONG trades (or below EMA fast for SHORT trades) to ensure trend confirmation.
"""

from __future__ import annotations

import pytest

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle


@pytest.fixture
def test_candles():
    """Create test candles for EMA confirm bars testing."""
    base_price = 1.1000
    candles = []

    for i in range(100):
        # Create a basic uptrend pattern
        price_drift = i * 0.0001
        candle = Candle(
            timestamp=1704067200 + (i * 3600),  # Unix timestamp
            open=base_price + price_drift,
            high=base_price + price_drift + 0.0010,
            low=base_price + price_drift - 0.0005,
            close=base_price + price_drift + 0.0008,
            volume=100,
        )
        candles.append(candle)

    return candles


class TestEmaConfirmBarsConfiguration:
    """Test EMA confirm bars configuration and parameter validation."""

    def test_ema_confirm_bars_enabled_by_default(self):
        """Test that EMA confirm bars filter is enabled by default in v4 enhancement."""
        env = BacktestEnvironment()
        assert env.ema_confirm_bars == 2, "EMA confirm bars should be enabled with 2 bars by default"

    def test_ema_confirm_bars_configuration_options(self):
        """Test that EMA confirm bars can be configured to different values."""
        # Test disabled
        env_disabled = BacktestEnvironment(ema_confirm_bars=0)
        assert env_disabled.ema_confirm_bars == 0

        # Test various confirm bar counts
        for bars in [1, 2, 3, 4, 5]:
            env = BacktestEnvironment(ema_confirm_bars=bars)
            assert env.ema_confirm_bars == bars

    def test_ema_confirm_bars_parameter_validation(self):
        """Test that EMA confirm bars parameters are within valid ranges."""
        env = BacktestEnvironment(ema_confirm_bars=3)

        # Should be within expected bounds (0-10 from schema)
        assert 0 <= env.ema_confirm_bars <= 10
        assert isinstance(env.ema_confirm_bars, int)

    def test_ema_confirm_bars_parameter_access(self):
        """Test that EMA confirm bars parameters are accessible in backtest environment."""
        env = BacktestEnvironment(ema_confirm_bars=2)

        # Verify parameter accessibility
        assert hasattr(env, "ema_confirm_bars")
        assert env.ema_confirm_bars == 2


class TestEmaConfirmBarsIntegration:
    """Test EMA confirm bars filter integration with backtest engine."""

    def test_ema_confirm_bars_integration_with_backtest(self, test_candles):
        """Test that EMA confirm bars filter integrates properly with IRB backtest engine."""
        # Create H4 candles (simplified - aggregate every 4 H1 candles)
        h4_candles = []
        for i in range(0, len(test_candles), 4):
            if i + 3 < len(test_candles):
                h4_candle = Candle(
                    timestamp=test_candles[i].timestamp,
                    open=test_candles[i].open,
                    high=max(test_candles[j].high for j in range(i, i + 4)),
                    low=min(test_candles[j].low for j in range(i, i + 4)),
                    close=test_candles[i + 3].close,
                    volume=sum(test_candles[j].volume for j in range(i, i + 4)),
                )
                h4_candles.append(h4_candle)

        # Test with EMA confirm bars enabled
        env_enabled = BacktestEnvironment(
            ema_confirm_bars=2,
            use_volatility_filter=False,  # Isolate EMA confirm bars filter
            use_regime_gate=False,  # Isolate EMA confirm bars filter
            irb_threshold=0.1,  # Lower threshold to get potential signals
            time_stop_bars=100,  # Longer time stop
        )

        bt_enabled = IRBBacktester(env_enabled)
        results_enabled = bt_enabled.run(test_candles, h4_candles)

        # Test with EMA confirm bars disabled
        env_disabled = BacktestEnvironment(
            ema_confirm_bars=0,
            use_volatility_filter=False,  # Isolate EMA confirm bars filter
            use_regime_gate=False,  # Isolate EMA confirm bars filter
            irb_threshold=0.1,
            time_stop_bars=100,
        )

        bt_disabled = IRBBacktester(env_disabled)
        results_disabled = bt_disabled.run(test_candles, h4_candles)

        # Both should complete successfully
        assert results_enabled is not None
        assert results_disabled is not None
        assert isinstance(results_enabled.trades, list)
        assert isinstance(results_disabled.trades, list)

        # EMA confirm bars should reduce or maintain signal count (more restrictive)
        enabled_signals = len(results_enabled.trades)
        disabled_signals = len(results_disabled.trades)
        assert enabled_signals <= disabled_signals, "EMA confirm bars should reduce or maintain signal count"

    def test_ema_confirm_bars_affects_signal_generation(self, test_candles):
        """Test that EMA confirm bars filter affects signal generation in backtest."""
        # Create H4 candles
        h4_candles = []
        for i in range(0, len(test_candles), 4):
            if i + 3 < len(test_candles):
                h4_candle = Candle(
                    timestamp=test_candles[i].timestamp,
                    open=test_candles[i].open,
                    high=max(test_candles[j].high for j in range(i, i + 4)),
                    low=min(test_candles[j].low for j in range(i, i + 4)),
                    close=test_candles[i + 3].close,
                    volume=sum(test_candles[j].volume for j in range(i, i + 4)),
                )
                h4_candles.append(h4_candle)

        # Test with different confirm bar counts
        env_1bar = BacktestEnvironment(
            ema_confirm_bars=1,
            use_volatility_filter=False,
            use_regime_gate=False,
            irb_threshold=0.1,
            time_stop_bars=100,
        )
        results_1bar = IRBBacktester(env_1bar).run(test_candles, h4_candles)

        env_3bar = BacktestEnvironment(
            ema_confirm_bars=3,
            use_volatility_filter=False,
            use_regime_gate=False,
            irb_threshold=0.1,
            time_stop_bars=100,
        )
        results_3bar = IRBBacktester(env_3bar).run(test_candles, h4_candles)

        # More confirm bars should be more restrictive
        signals_1bar = len(results_1bar.trades)
        signals_3bar = len(results_3bar.trades)
        assert signals_3bar <= signals_1bar, "Higher confirm bar count should reduce signal count"

    def test_ema_confirm_bars_integration_with_other_filters(self, test_candles):
        """Test that EMA confirm bars works alongside other filters."""
        # Create H4 candles
        h4_candles = []
        for i in range(0, len(test_candles), 4):
            if i + 3 < len(test_candles):
                h4_candle = Candle(
                    timestamp=test_candles[i].timestamp,
                    open=test_candles[i].open,
                    high=max(test_candles[j].high for j in range(i, i + 4)),
                    low=min(test_candles[j].low for j in range(i, i + 4)),
                    close=test_candles[i + 3].close,
                    volume=sum(test_candles[j].volume for j in range(i, i + 4)),
                )
                h4_candles.append(h4_candle)

        # Test with all filters enabled
        env_all_filters = BacktestEnvironment(
            ema_confirm_bars=2, use_volatility_filter=True, use_regime_gate=True, irb_threshold=0.1, time_stop_bars=100
        )
        bt_all_filters = IRBBacktester(env_all_filters)
        results_all = bt_all_filters.run(test_candles, h4_candles)

        # Should work without errors
        assert results_all is not None
        assert isinstance(results_all.trades, list)

        # Test with only EMA confirm bars
        env_only_ema = BacktestEnvironment(
            ema_confirm_bars=2,
            use_volatility_filter=False,
            use_regime_gate=False,
            irb_threshold=0.1,
            time_stop_bars=100,
        )
        bt_only_ema = IRBBacktester(env_only_ema)
        results_ema_only = bt_only_ema.run(test_candles, h4_candles)

        # All filters should be more restrictive than EMA alone
        all_signals = len(results_all.trades)
        ema_only_signals = len(results_ema_only.trades)
        assert all_signals <= ema_only_signals, "Multiple filters should be more restrictive"


class TestEmaConfirmBarsEdgeCases:
    """Test edge cases for EMA confirm bars filter."""

    def test_ema_confirm_bars_with_zero_setting(self, test_candles):
        """Test that setting ema_confirm_bars=0 disables the filter."""
        # Create H4 candles
        h4_candles = []
        for i in range(0, len(test_candles), 4):
            if i + 3 < len(test_candles):
                h4_candle = Candle(
                    timestamp=test_candles[i].timestamp,
                    open=test_candles[i].open,
                    high=max(test_candles[j].high for j in range(i, i + 4)),
                    low=min(test_candles[j].low for j in range(i, i + 4)),
                    close=test_candles[i + 3].close,
                    volume=sum(test_candles[j].volume for j in range(i, i + 4)),
                )
                h4_candles.append(h4_candle)

        env = BacktestEnvironment(
            ema_confirm_bars=0,  # Explicitly disabled
            use_volatility_filter=False,
            use_regime_gate=False,
            irb_threshold=0.1,
            time_stop_bars=100,
        )

        bt = IRBBacktester(env)
        results = bt.run(test_candles, h4_candles)

        # Should work normally (filter bypassed)
        assert results is not None
        assert isinstance(results.trades, list)

    def test_ema_confirm_bars_parameter_bounds(self, test_candles):
        """Test EMA confirm bars parameter boundary conditions."""
        # Create minimal H4 candles for testing
        h4_candles = test_candles[::4][:10]  # Use every 4th candle as H4

        # Test minimum valid value
        env_min = BacktestEnvironment(ema_confirm_bars=1)
        bt_min = IRBBacktester(env_min)
        results_min = bt_min.run(test_candles[:20], h4_candles[:5])
        assert results_min is not None
        assert isinstance(results_min.trades, list)

        # Test maximum reasonable value
        env_max = BacktestEnvironment(ema_confirm_bars=5)
        bt_max = IRBBacktester(env_max)
        results_max = bt_max.run(test_candles[:20], h4_candles[:5])
        assert results_max is not None
        assert isinstance(results_max.trades, list)


if __name__ == "__main__":
    pytest.main([__file__])
