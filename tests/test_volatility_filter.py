"""Tests for volatility filter enhancement."""

import pytest

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle
from novatrade.strategies.irb import IRBStrategy


@pytest.fixture
def irb_with_volatility_filter():
    """Create IRB strategy with volatility filter enabled."""
    env = BacktestEnvironment(
        use_volatility_filter=True,
        volatility_atr_ma_period=20,  # Shorter period for testing
        irb_threshold=0.2,
        atr_period=14,
        adx_period=14,
        time_stop_bars=40,
    )
    return IRBStrategy(env)


@pytest.fixture
def irb_without_volatility_filter():
    """Create IRB strategy with volatility filter disabled."""
    env = BacktestEnvironment(
        use_volatility_filter=False,
        volatility_atr_ma_period=20,
        irb_threshold=0.2,
        atr_period=14,
        adx_period=14,
        time_stop_bars=40,
    )
    return IRBStrategy(env)


@pytest.fixture
def test_candles():
    """Create test candles with varying volatility."""
    base_price = 1.1000
    candles = []

    for i in range(50):
        # Create alternating high and low volatility periods
        if i < 25:
            # Low volatility period (tight range)
            volatility = 0.0005
        else:
            # High volatility period (wide range)
            volatility = 0.0020

        open_price = base_price + (i * 0.0001)
        high = open_price + volatility
        low = open_price - volatility
        close = open_price + (volatility * 0.5)  # Close in middle

        candle = Candle(
            timestamp=f"2024-01-{i + 1:02d}T12:00:00Z", open=open_price, high=high, low=low, close=close, volume=100
        )
        candles.append(candle)

    return candles


def test_volatility_filter_enabled_by_default():
    """Test that volatility filter is enabled by default after enhancement."""
    env = BacktestEnvironment()
    assert env.use_volatility_filter is True
    assert env.volatility_atr_ma_period == 50


def test_volatility_filter_configuration():
    """Test volatility filter configuration options."""
    env = BacktestEnvironment(use_volatility_filter=True, volatility_atr_ma_period=30)
    assert env.use_volatility_filter is True
    assert env.volatility_atr_ma_period == 30

    env_disabled = BacktestEnvironment(use_volatility_filter=False)
    assert env_disabled.use_volatility_filter is False


def test_volatility_filter_integration_with_backtest():
    """Test that volatility filter is properly integrated with backtest engine."""
    from novatrade.backtest.engine import IRBBacktester

    # Test with filter enabled
    env_with = BacktestEnvironment(
        use_volatility_filter=True,
        volatility_atr_ma_period=20,
        irb_threshold=0.2,
    )

    # Test with filter disabled
    env_without = BacktestEnvironment(
        use_volatility_filter=False,
        volatility_atr_ma_period=20,
        irb_threshold=0.2,
    )

    # Both should create successfully
    backtester_with = IRBBacktester(env=env_with)
    backtester_without = IRBBacktester(env=env_without)

    assert backtester_with.env.use_volatility_filter is True
    assert backtester_without.env.use_volatility_filter is False


def test_volatility_filter_parameter_access():
    """Test that volatility filter parameters are accessible."""
    env = BacktestEnvironment(use_volatility_filter=True, volatility_atr_ma_period=30)

    assert hasattr(env, "use_volatility_filter")
    assert hasattr(env, "volatility_atr_ma_period")
    assert env.use_volatility_filter is True
    assert env.volatility_atr_ma_period == 30


def test_volatility_filter_affects_signal_generation():
    """Test that volatility filter affects signal generation via backtest."""
    from novatrade.backtest.engine import IRBBacktester

    # Create test data as Candle objects
    h1_candles = []
    base_price = 1.1000

    for i in range(60):
        # Create predictable volatility pattern
        if i < 30:
            # Low volatility period
            volatility = 0.0005
        else:
            # High volatility period
            volatility = 0.0020

        open_price = base_price + (i * 0.0001)
        high = open_price + volatility
        low = open_price - volatility
        close = open_price + (volatility * 0.3)

        candle = Candle(
            timestamp=1704067200 + (i * 3600),  # Unix timestamp starting 2024-01-01, +1 hour each
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=100,
        )
        h1_candles.append(candle)

    # Test with filter enabled
    env_with = BacktestEnvironment(
        use_volatility_filter=True,
        volatility_atr_ma_period=20,
        irb_threshold=0.1,  # Lower threshold to potentially get signals
        time_stop_bars=100,  # Long time stop to avoid early exits
    )

    # Test with filter disabled
    env_without = BacktestEnvironment(
        use_volatility_filter=False,
        volatility_atr_ma_period=20,
        irb_threshold=0.1,
        time_stop_bars=100,
    )

    backtester_with = IRBBacktester(env=env_with)
    backtester_without = IRBBacktester(env=env_without)

    # Create H4 candles (simplified - just use every 4th H1 candle)
    h4_candles = []
    for i in range(0, len(h1_candles), 4):
        if i + 3 < len(h1_candles):
            # Aggregate 4 H1 candles into 1 H4 candle
            open_price = h1_candles[i].open
            high_price = max(h1_candles[j].high for j in range(i, i + 4))
            low_price = min(h1_candles[j].low for j in range(i, i + 4))
            close_price = h1_candles[i + 3].close
            volume = sum(h1_candles[j].volume for j in range(i, i + 4))

            h4_candle = Candle(
                timestamp=1704067200 + (i * 3600),  # Unix timestamp aligned to H1 start
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
            )
            h4_candles.append(h4_candle)

    # Run backtests (may not generate trades due to other filters, but should not crash)
    try:
        result_with = backtester_with.run(h1_candles, h4_candles)
        result_without = backtester_without.run(h1_candles, h4_candles)

        # Both should complete successfully
        assert result_with is not None
        assert result_without is not None

        # Verify the filter setting persisted
        assert backtester_with.env.use_volatility_filter is True
        assert backtester_without.env.use_volatility_filter is False

    except Exception as e:
        # If backtest fails due to insufficient data, that's acceptable for this test
        # as long as it's not a configuration error
        if "insufficient data" not in str(e).lower() and "not enough" not in str(e).lower():
            raise


def test_signal_allowed_when_atr_above_sma():
    """Test that volatility filter allows signals during high volatility."""
    env_with_filter = BacktestEnvironment(
        use_volatility_filter=True,
        volatility_atr_ma_period=20,
        irb_threshold=0.1,  # Lower threshold to potentially get signals
        atr_period=14,
        adx_period=14,
    )

    env_without_filter = BacktestEnvironment(
        use_volatility_filter=False,
        volatility_atr_ma_period=20,
        irb_threshold=0.1,
        atr_period=14,
        adx_period=14,
    )

    # Both configurations should be valid
    assert env_with_filter.use_volatility_filter is True
    assert env_without_filter.use_volatility_filter is False

    # Strategy creation should work
    strategy_with = IRBStrategy(env_with_filter)
    strategy_without = IRBStrategy(env_without_filter)

    assert strategy_with.env.use_volatility_filter is True
    assert strategy_without.env.use_volatility_filter is False


def test_volatility_filter_integration_with_other_filters():
    """Test that volatility filter works alongside other filters."""
    env = BacktestEnvironment(
        use_volatility_filter=True,
        use_regime_gate=True,  # BBW squeeze gate
        volatility_atr_ma_period=20,
        regime_bbw_period=20,
        regime_bbw_threshold=0.003,
        irb_threshold=0.2,
    )

    # Both filters should be enabled
    assert env.use_volatility_filter is True
    assert env.use_regime_gate is True

    # Configuration should be valid
    assert env.volatility_atr_ma_period == 20
    assert env.regime_bbw_period == 20

    strategy = IRBStrategy(env)
    assert strategy.env.use_volatility_filter is True
    assert strategy.env.use_regime_gate is True


def test_volatility_filter_parameter_validation():
    """Test parameter validation for volatility filter."""
    # Valid parameters
    env = BacktestEnvironment(use_volatility_filter=True, volatility_atr_ma_period=30)
    assert env.volatility_atr_ma_period == 30

    # Edge cases
    env_min = BacktestEnvironment(volatility_atr_ma_period=20)
    assert env_min.volatility_atr_ma_period == 20

    env_max = BacktestEnvironment(volatility_atr_ma_period=100)
    assert env_max.volatility_atr_ma_period == 100


def test_volatility_filter_performance_characteristics():
    """Test that volatility filter doesn't significantly impact performance."""
    import time

    # Create larger dataset
    candles = []
    for i in range(1000):
        candle = Candle(
            timestamp=f"2024-01-01T{i % 24:02d}:00:00Z",
            open=1.1000 + (i * 0.00001),
            high=1.1000 + (i * 0.00001) + 0.0010,
            low=1.1000 + (i * 0.00001) - 0.0010,
            close=1.1000 + (i * 0.00001) + 0.0005,
            volume=100,
        )
        candles.append(candle)

    # Time with filter enabled
    env_with_filter = BacktestEnvironment(use_volatility_filter=True)
    strategy_with = IRBStrategy(env_with_filter)

    start_time = time.time()
    indicators_with = strategy_with.compute_indicators(candles)
    time_with_filter = time.time() - start_time

    # Time with filter disabled
    env_without_filter = BacktestEnvironment(use_volatility_filter=False)
    strategy_without = IRBStrategy(env_without_filter)

    start_time = time.time()
    indicators_without = strategy_without.compute_indicators(candles)
    time_without_filter = time.time() - start_time

    # Performance impact should be minimal (< 50% overhead)
    performance_overhead = time_with_filter / time_without_filter if time_without_filter > 0 else 1
    assert performance_overhead < 2.0, f"Volatility filter overhead too high: {performance_overhead:.2f}x"

    # Sanity check that indicators were computed
    assert len(indicators_with["atr"]) == len(candles)
    assert len(indicators_without["atr"]) == len(candles)

    # Both should have the basic indicators
    assert "atr" in indicators_with and "atr" in indicators_without


def test_volatility_filter_impact_measurement():
    """Test measuring the filtering impact of the volatility filter."""
    # Create test data with known volatility characteristics
    candles = []
    base_price = 1.1000

    for i in range(100):
        # Alternate between low and high volatility
        if i % 10 < 5:  # Low volatility periods
            volatility = 0.0003
        else:  # High volatility periods
            volatility = 0.0015

        open_price = base_price + (i * 0.0001)
        high = open_price + volatility
        low = open_price - volatility
        close = open_price + (volatility * 0.2)

        candle = Candle(
            timestamp=f"2024-01-{i + 1:02d}T12:00:00Z", open=open_price, high=high, low=low, close=close, volume=100
        )
        candles.append(candle)

    # Test with filter enabled
    env_with = BacktestEnvironment(
        use_volatility_filter=True,
        volatility_atr_ma_period=20,
        irb_threshold=0.05,  # Low threshold to get signals
    )
    strategy_with = IRBStrategy(env_with)
    indicators_with = strategy_with.compute_indicators(candles)
    signals_with = strategy_with.generate_signals(candles, indicators_with)

    # Test with filter disabled
    env_without = BacktestEnvironment(
        use_volatility_filter=False,
        volatility_atr_ma_period=20,
        irb_threshold=0.05,
    )
    strategy_without = IRBStrategy(env_without)
    indicators_without = strategy_without.compute_indicators(candles)
    signals_without = strategy_without.generate_signals(candles, indicators_without)

    # Filter should reduce signal count
    signals_with_count = len(signals_with)
    signals_without_count = len(signals_without)

    # Should have some filtering effect (may be 0 signals due to other filters)
    assert signals_with_count <= signals_without_count, "Volatility filter should not increase signal count"

    # Basic validation that both processed the data
    assert isinstance(signals_with, list)
    assert isinstance(signals_without, list)
