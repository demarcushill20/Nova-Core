"""Tests for edge cases and uncovered paths in backtest engine."""

import math

from novatrade.backtest.engine import compute_atr, compute_ema
from novatrade.models import Candle


class TestComputeEMA:
    """Test edge cases for EMA computation."""

    def test_empty_list(self):
        """Test EMA with empty price list."""
        result = compute_ema([], 14)
        assert result == []

    def test_single_price(self):
        """Test EMA with single price."""
        result = compute_ema([100.0], 14)
        assert len(result) == 1
        assert result[0] == 100.0

    def test_two_prices(self):
        """Test EMA with two prices."""
        prices = [100.0, 102.0]
        result = compute_ema(prices, 14)
        assert len(result) == 2
        assert result[0] == 100.0
        # k = 2/(14+1) = 0.1333...
        k = 2.0 / (14 + 1)
        expected = 102.0 * k + 100.0 * (1 - k)
        assert abs(result[1] - expected) < 1e-10

    def test_period_edge_cases(self):
        """Test EMA with edge case periods."""
        prices = [100.0, 101.0, 102.0]

        # Period = 1 (maximum smoothing factor)
        result = compute_ema(prices, 1)
        assert result[0] == 100.0
        assert result[1] == 101.0  # k=1, so EMA follows price exactly
        assert result[2] == 102.0

    def test_nan_handling(self):
        """Test EMA initialization with NaN."""
        prices = [100.0, 101.0, 102.0, 103.0, 104.0]
        result = compute_ema(prices, 20)

        # First value should not be NaN
        assert not math.isnan(result[0])
        assert result[0] == 100.0

        # Subsequent values should be computed
        for i in range(1, len(result)):
            assert not math.isnan(result[i])


class TestComputeATR:
    """Test edge cases for ATR computation."""

    def test_empty_candles(self):
        """Test ATR with empty candle list."""
        result = compute_atr([], 14)
        assert result == []

    def test_single_candle(self):
        """Test ATR with single candle."""
        candle = Candle(
            timestamp=1640995200,
            open=1.1000,
            high=1.1020,
            low=1.0980,
            close=1.1010,
            volume=100.0,
            symbol="EURUSD",
            timeframe="1H",
        )
        # Test with period=1 to get actual ATR value
        result = compute_atr([candle], 1)
        assert len(result) == 1
        expected_atr = 1.1020 - 1.0980  # high - low for first candle
        assert abs(result[0] - expected_atr) < 1e-10

        # Test with larger period returns NaN for insufficient data
        result_large = compute_atr([candle], 14)
        assert len(result_large) == 1
        assert math.isnan(result_large[0])

    def test_two_candles_gap_up(self):
        """Test ATR with gap up between candles."""
        candles = [
            Candle(
                timestamp=1640995200,
                open=1.1000,
                high=1.1020,
                low=1.0980,
                close=1.1010,
                volume=100.0,
                symbol="EURUSD",
                timeframe="1H",
            ),
            Candle(
                timestamp=1640998800,
                open=1.1050,  # Gap up
                high=1.1080,
                low=1.1040,
                close=1.1070,
                volume=100.0,
                symbol="EURUSD",
                timeframe="1H",
            ),
        ]

        result = compute_atr(candles, 2)
        assert len(result) == 2

        # First TR = high - low (NaN for period 2, only ATR[1] is valid)
        first_tr = 1.1020 - 1.0980
        assert math.isnan(result[0])  # Not enough data for ATR[0] with period=2

        # Second TR should consider gap
        # TR = max(high-low, high-prev_close, prev_close-low)
        second_tr = max(
            1.1080 - 1.1040,  # high - low
            1.1080 - 1.1010,  # high - prev_close
            1.1010 - 1.1040,  # prev_close - low (negative, so abs)
        )
        # For period=2, ATR[1] = average of first 2 TRs
        expected_atr = (first_tr + second_tr) / 2
        assert abs(result[1] - expected_atr) < 1e-10

    def test_gap_down_scenario(self):
        """Test ATR with gap down scenario."""
        candles = [
            Candle(
                timestamp=1640995200,
                open=1.1000,
                high=1.1020,
                low=1.0980,
                close=1.1010,
                volume=100.0,
                symbol="EURUSD",
                timeframe="1H",
            ),
            Candle(
                timestamp=1640998800,
                open=1.0950,  # Gap down
                high=1.0970,
                low=1.0930,
                close=1.0960,
                volume=100.0,
                symbol="EURUSD",
                timeframe="1H",
            ),
        ]

        result = compute_atr(candles, 2)
        assert len(result) == 2

        # Second TR should capture gap down
        second_tr = max(
            1.0970 - 1.0930,  # high - low
            abs(1.0970 - 1.1010),  # abs(high - prev_close)
            abs(1.1010 - 1.0930),  # abs(prev_close - low) (captures gap)
        )
        assert second_tr == abs(1.1010 - 1.0930)  # Should be the gap

        # ATR[1] should be valid
        assert not math.isnan(result[1])
        assert result[1] > 0

    def test_period_validation(self):
        """Test ATR with various periods."""
        candle = Candle(
            timestamp=1640995200,
            open=1.1000,
            high=1.1020,
            low=1.0980,
            close=1.1010,
            volume=100.0,
            symbol="EURUSD",
            timeframe="1H",
        )

        # Test different periods
        for period in [1, 5, 14, 21, 50]:
            result = compute_atr([candle], period)
            assert len(result) == 1
            if period == 1:
                assert result[0] > 0  # ATR should be positive for period=1
            else:
                assert math.isnan(result[0])  # NaN for period > 1 with single candle


class TestBacktestEngineErrorPaths:
    """Test error paths and edge cases in backtest engine."""

    def test_zero_prices_ema(self):
        """Test EMA with zero prices."""
        prices = [0.0, 0.0, 0.0]
        result = compute_ema(prices, 14)
        assert all(x == 0.0 for x in result)

    def test_negative_prices_ema(self):
        """Test EMA with negative prices (should work)."""
        prices = [-10.0, -5.0, -2.0]
        result = compute_ema(prices, 14)
        assert len(result) == 3
        assert result[0] == -10.0

    def test_large_period_ema(self):
        """Test EMA with period larger than price list."""
        prices = [100.0, 101.0]
        result = compute_ema(prices, 100)  # Large period
        assert len(result) == 2
        # k = 2/(100+1) ≈ 0.0198
        k = 2.0 / (100 + 1)
        expected = 101.0 * k + 100.0 * (1 - k)
        assert abs(result[1] - expected) < 1e-10

    def test_extremely_high_low_candles(self):
        """Test ATR with extreme high/low values."""
        candle = Candle(
            timestamp=1640995200,
            open=1.0,
            high=1000000.0,  # Extreme high
            low=0.0001,  # Extreme low
            close=500.0,
            volume=100.0,
            symbol="EURUSD",
            timeframe="1H",
        )

        result = compute_atr([candle], 1)
        assert len(result) == 1
        assert result[0] == 1000000.0 - 0.0001  # Should handle extreme ranges
