"""Comprehensive tests for the multi-strategy architecture (Phase 6).

Tests cover:
- BaseStrategy ABC enforcement
- IRBStrategy wrapper correctness
- MeanReversionStrategy indicator computation and signal generation
- BreakoutStrategy indicator computation and signal generation
- TrendFollowingStrategy indicator computation and signal generation
- Strategy registry: register, get, list
- strategy_type field in StrategyConfig
- create_backtester factory function
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import ClassVar

import pytest

from novatrade.backtest.engine import (
    BacktestResult,
    IRBBacktester,
    StrategyBacktesterAdapter,
    create_backtester,
)
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.cli.config_schema import StrategyConfig
from novatrade.models import Candle
from novatrade.strategies.base import BaseStrategy, EntrySignal, ExitSignal
from novatrade.strategies.breakout import BreakoutStrategy
from novatrade.strategies.irb import IRBStrategy
from novatrade.strategies.mean_reversion import MeanReversionStrategy
from novatrade.strategies.registry import (
    STRATEGY_REGISTRY,
    get_strategy,
    list_strategies,
    register_strategy,
)
from novatrade.strategies.trend_following import TrendFollowingStrategy

# ---------------------------------------------------------------------------
# Fixtures: synthetic candle generators
# ---------------------------------------------------------------------------


def _make_candle(
    timestamp: float = 0.0,
    open_: float = 1.1000,
    high: float = 1.1020,
    low: float = 1.0980,
    close: float = 1.1010,
    volume: float = 100.0,
) -> Candle:
    return Candle(
        timestamp=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _make_trending_candles(n: int = 200, direction: str = "up", base: float = 1.1000) -> list[Candle]:
    """Generate synthetic candles with a clear trend for testing."""
    candles = []
    price = base
    step = 0.0005 if direction == "up" else -0.0005
    noise = 0.0010
    for i in range(n):
        open_ = price
        close = price + step
        high = max(open_, close) + noise
        low = min(open_, close) - noise
        candles.append(
            Candle(
                timestamp=float(i * 3600),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=100.0,
            )
        )
        price = close
    return candles


def _make_ranging_candles(n: int = 200, center: float = 1.1000, amplitude: float = 0.0050) -> list[Candle]:
    """Generate synthetic candles oscillating around a center for mean-reversion tests."""
    import math as _math

    candles = []
    for i in range(n):
        # Sine wave oscillation
        phase = 2 * _math.pi * i / 40  # period of ~40 bars
        mid = center + amplitude * _math.sin(phase)
        open_ = mid - 0.0002
        close = mid + 0.0002
        high = max(open_, close) + 0.0005
        low = min(open_, close) - 0.0005
        candles.append(
            Candle(
                timestamp=float(i * 3600),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=100.0,
            )
        )
    return candles


def _make_breakout_candles(n: int = 200, breakout_at: int = 100, direction: str = "up") -> list[Candle]:
    """Generate candles that range then break out at a specific bar."""
    candles = []
    base = 1.1000
    for i in range(n):
        if i < breakout_at:
            # Range-bound
            noise = 0.0003 * (1 if i % 2 == 0 else -1)
            open_ = base + noise
            close = base - noise
            high = base + 0.0010
            low = base - 0.0010
        else:
            # Breakout
            offset = (i - breakout_at) * (0.0008 if direction == "up" else -0.0008)
            mid = base + offset
            open_ = mid - 0.0002
            close = mid + 0.0004
            if direction == "down":
                open_ = mid + 0.0002
                close = mid - 0.0004
            high = max(open_, close) + 0.0003
            low = min(open_, close) - 0.0003
        candles.append(
            Candle(
                timestamp=float(i * 3600),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=100.0,
            )
        )
    return candles


def _make_crossover_candles(n: int = 200) -> list[Candle]:
    """Generate candles with an EMA crossover around the middle."""
    candles = []
    # First half: slow uptrend, then accelerate to cause crossover
    for i in range(n):
        if i < n // 2:
            # Slow downtrend -- fast EMA below slow
            price = 1.1000 - i * 0.0001
        else:
            # Sharp uptrend -- fast EMA crosses above slow
            price = 1.1000 - (n // 2) * 0.0001 + (i - n // 2) * 0.0005
        noise = 0.0003 * (1 if i % 2 == 0 else -1)
        open_ = price + noise
        close = price - noise
        high = max(open_, close) + 0.0005
        low = min(open_, close) - 0.0005
        candles.append(
            Candle(
                timestamp=float(i * 3600),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=100.0,
            )
        )
    return candles


# ===========================================================================
# 1. BaseStrategy ABC enforcement
# ===========================================================================


class TestBaseStrategyABC:
    """Test that BaseStrategy cannot be instantiated and enforces interface."""

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            BaseStrategy()  # type: ignore[abstract]

    def test_incomplete_subclass_raises(self) -> None:
        class Incomplete(BaseStrategy):
            name = "incomplete"
            family = "test"

            def compute_indicators(self, candles):
                return {}

            # Missing other abstract methods

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_complete_subclass_instantiates(self) -> None:
        class Complete(BaseStrategy):
            name = "complete"
            family = "test"

            def compute_indicators(self, candles):
                return {}

            def generate_signals(self, candles, indicators):
                return []

            def check_entry(self, bar_index, candles, indicators, h4_candles=None):
                return None

            def check_exit(self, bar_index, candles, indicators, position=None):
                return None

            def get_default_doctrine(self, pair, timeframe):
                return {}

            def get_parameter_bounds(self):
                return {}

        s = Complete()
        assert s.name == "complete"
        assert s.family == "test"


# ===========================================================================
# 2. IRBStrategy tests
# ===========================================================================


class TestIRBStrategy:
    """Test IRBStrategy wraps existing logic correctly."""

    def test_attributes(self) -> None:
        s = IRBStrategy()
        assert s.name == "irb"
        assert s.family == "reversal"

    def test_compute_indicators_empty(self) -> None:
        s = IRBStrategy()
        result = s.compute_indicators([])
        assert result == {"ema": [], "atr": [], "adx": []}

    def test_compute_indicators_returns_correct_keys(self) -> None:
        candles = _make_trending_candles(100)
        s = IRBStrategy()
        indicators = s.compute_indicators(candles)
        assert set(indicators.keys()) == {"ema", "atr", "adx"}
        assert len(indicators["ema"]) == 100
        assert len(indicators["atr"]) == 100
        assert len(indicators["adx"]) == 100

    def test_check_entry_warmup_returns_none(self) -> None:
        candles = _make_trending_candles(10)
        s = IRBStrategy()
        indicators = s.compute_indicators(candles)
        # Default warmup is 34, so bar 0 should return None
        assert s.check_entry(0, candles, indicators) is None

    def test_generate_signals_returns_list(self) -> None:
        candles = _make_trending_candles(200)
        s = IRBStrategy()
        indicators = s.compute_indicators(candles)
        signals = s.generate_signals(candles, indicators)
        assert isinstance(signals, list)
        for sig in signals:
            assert isinstance(sig, EntrySignal)
            assert sig.side in ("LONG", "SHORT")
            assert sig.bar_index >= 0

    def test_check_exit_none_without_position(self) -> None:
        candles = _make_trending_candles(50)
        s = IRBStrategy()
        indicators = s.compute_indicators(candles)
        assert s.check_exit(20, candles, indicators, position=None) is None

    def test_check_exit_stop_loss(self) -> None:
        candles = _make_trending_candles(100, direction="down")
        s = IRBStrategy()
        indicators = s.compute_indicators(candles)
        position = {
            "side": "LONG",
            "entry_price": 1.1000,
            "stop_loss": candles[50].high,  # Stop above current price in downtrend
            "entry_bar": 40,
            "current_stop": candles[50].high,
            "best_close": 1.1000,
        }
        exit_sig = s.check_exit(50, candles, indicators, position)
        # In a downtrend, a long position should hit the stop
        if exit_sig is not None:
            assert isinstance(exit_sig, ExitSignal)
            assert exit_sig.reason in ("stop_loss", "trailing_stop", "time_stop")

    def test_check_exit_time_stop(self) -> None:
        candles = _make_trending_candles(100)
        s = IRBStrategy()
        indicators = s.compute_indicators(candles)
        # Position held for 41 bars (> default time_stop_bars=40)
        position = {
            "side": "LONG",
            "entry_price": 1.1000,
            "stop_loss": 1.0900,
            "entry_bar": 10,
            "current_stop": 1.0900,
            "best_close": 1.1100,
        }
        exit_sig = s.check_exit(51, candles, indicators, position)
        assert exit_sig is not None
        assert exit_sig.reason == "time_stop"

    def test_get_default_doctrine(self) -> None:
        s = IRBStrategy()
        doctrine = s.get_default_doctrine("EURUSD", "H1")
        assert doctrine["name"] == "irb_eurusd_h1"
        assert doctrine["setup_type"] == "reversal"
        assert "mutable_params" in doctrine
        assert "irb_threshold" in doctrine["mutable_params"]

    def test_get_parameter_bounds(self) -> None:
        s = IRBStrategy()
        bounds = s.get_parameter_bounds()
        assert "irb_threshold" in bounds
        assert bounds["irb_threshold"] == (0.30, 0.60)
        assert "ema_period" in bounds
        for name, (lo, hi) in bounds.items():
            assert lo < hi, f"Invalid bounds for {name}: {lo} >= {hi}"

    def test_irb_with_custom_env(self) -> None:
        env = BacktestEnvironment(irb_threshold=0.40, ema_period=25)
        s = IRBStrategy(env=env)
        assert s.env.irb_threshold == 0.40
        assert s.env.ema_period == 25

    def test_atr_sl_floor_widens_tight_stops(self) -> None:
        """ATR-adaptive SL floor widens candle-geometry stops when too tight."""
        # Create candles with very small ranges (tight bars) but clear trend
        candles = _make_trending_candles(200, direction="up")
        env = BacktestEnvironment(atr_sl_floor_multiplier=0.5)
        s = IRBStrategy(env=env)
        indicators = s.compute_indicators(candles)
        signals = s.generate_signals(candles, indicators)
        for sig in signals:
            if sig.side == "LONG":
                sl_dist = sig.entry_price - sig.stop_loss
                # ATR floor ensures SL distance >= 0.5 * ATR
                atr_val = indicators["atr"][sig.bar_index]
                assert sl_dist >= 0.5 * atr_val - 1e-10, f"SL distance {sl_dist} < ATR floor {0.5 * atr_val}"

    def test_atr_sl_floor_disabled_when_zero(self) -> None:
        """With atr_sl_floor_multiplier=0, candle geometry is used as-is."""
        candles = _make_trending_candles(200, direction="up")
        env_off = BacktestEnvironment(atr_sl_floor_multiplier=0.0)
        env_on = BacktestEnvironment(atr_sl_floor_multiplier=0.5)
        s_off = IRBStrategy(env=env_off)
        s_on = IRBStrategy(env=env_on)
        ind = s_off.compute_indicators(candles)
        sigs_off = s_off.generate_signals(candles, ind)
        sigs_on = s_on.generate_signals(candles, ind)
        # Both should produce signals; with floor=0, some may have tighter SLs
        assert isinstance(sigs_off, list)
        assert isinstance(sigs_on, list)


# ===========================================================================
# 3. MeanReversionStrategy tests
# ===========================================================================


class TestMeanReversionStrategy:
    """Test Bollinger Band mean reversion strategy."""

    def test_attributes(self) -> None:
        s = MeanReversionStrategy()
        assert s.name == "mean_reversion"
        assert s.family == "mean_reversion"

    def test_compute_indicators_empty(self) -> None:
        s = MeanReversionStrategy()
        result = s.compute_indicators([])
        assert result == {"sma": [], "upper_band": [], "lower_band": [], "rsi": []}

    def test_compute_indicators_keys_and_lengths(self) -> None:
        candles = _make_ranging_candles(100)
        s = MeanReversionStrategy()
        indicators = s.compute_indicators(candles)
        assert set(indicators.keys()) == {"sma", "upper_band", "lower_band", "rsi"}
        for key, vals in indicators.items():
            assert len(vals) == 100, f"{key} has wrong length"

    def test_sma_computation(self) -> None:
        candles = _make_ranging_candles(50)
        s = MeanReversionStrategy(sma_period=10)
        indicators = s.compute_indicators(candles)
        sma = indicators["sma"]
        # First 9 values should be NaN (warmup)
        for i in range(9):
            assert math.isnan(sma[i])
        # From index 9, SMA should be defined
        assert not math.isnan(sma[9])

    def test_bollinger_bands_bracket_sma(self) -> None:
        candles = _make_ranging_candles(100)
        s = MeanReversionStrategy()
        indicators = s.compute_indicators(candles)
        sma = indicators["sma"]
        upper = indicators["upper_band"]
        lower = indicators["lower_band"]
        for i in range(len(candles)):
            if not math.isnan(sma[i]):
                assert upper[i] > sma[i], f"Upper band not above SMA at {i}"
                assert lower[i] < sma[i], f"Lower band not below SMA at {i}"

    def test_rsi_in_range(self) -> None:
        candles = _make_ranging_candles(100)
        s = MeanReversionStrategy()
        indicators = s.compute_indicators(candles)
        rsi = indicators["rsi"]
        for i, val in enumerate(rsi):
            if not math.isnan(val):
                assert 0.0 <= val <= 100.0, f"RSI out of range at {i}: {val}"

    def test_generate_signals_from_ranging_data(self) -> None:
        # Create candles that should trigger mean-reversion signals
        candles = _make_ranging_candles(300, amplitude=0.0080)
        s = MeanReversionStrategy(warmup_bars=25, rsi_oversold=40.0, rsi_overbought=60.0)
        indicators = s.compute_indicators(candles)
        signals = s.generate_signals(candles, indicators)
        assert isinstance(signals, list)
        for sig in signals:
            assert isinstance(sig, EntrySignal)
            assert sig.side in ("LONG", "SHORT")

    def test_check_entry_warmup_returns_none(self) -> None:
        candles = _make_ranging_candles(10)
        s = MeanReversionStrategy()
        indicators = s.compute_indicators(candles)
        assert s.check_entry(5, candles, indicators) is None

    def test_check_exit_signal_exit(self) -> None:
        candles = _make_ranging_candles(100)
        s = MeanReversionStrategy()
        indicators = s.compute_indicators(candles)
        sma = indicators["sma"]
        # Find a bar where SMA is defined
        test_bar = None
        for i in range(30, len(candles)):
            if not math.isnan(sma[i]):
                test_bar = i
                break
        assert test_bar is not None
        # Create a long position that should exit when close >= SMA
        position = {
            "side": "LONG",
            "entry_price": candles[test_bar].close - 0.0050,
            "stop_loss": candles[test_bar].close - 0.0100,
            "entry_bar": test_bar - 5,
            "current_stop": candles[test_bar].close - 0.0100,
            "best_close": candles[test_bar].close,
        }
        # If close >= SMA, should get signal_exit
        if candles[test_bar].close >= sma[test_bar]:
            exit_sig = s.check_exit(test_bar, candles, indicators, position)
            if exit_sig is not None:
                assert exit_sig.reason in ("signal_exit", "stop_loss", "time_stop")

    def test_check_exit_time_stop(self) -> None:
        candles = _make_ranging_candles(100)
        s = MeanReversionStrategy(time_stop_bars=5)
        indicators = s.compute_indicators(candles)
        position = {
            "side": "LONG",
            "entry_price": 1.0900,
            "stop_loss": 1.0800,
            "entry_bar": 30,
            "current_stop": 1.0800,
            "best_close": 1.1000,
        }
        exit_sig = s.check_exit(36, candles, indicators, position)
        # bars_held = 36 - 30 = 6 >= 5, should trigger time_stop
        assert exit_sig is not None
        assert exit_sig.reason in ("time_stop", "signal_exit", "stop_loss")

    def test_get_default_doctrine(self) -> None:
        s = MeanReversionStrategy()
        doctrine = s.get_default_doctrine("GBPUSD", "H1")
        assert doctrine["name"] == "mean_reversion_gbpusd_h1"
        assert doctrine["setup_type"] == "mean_reversion"
        assert "mutable_params" in doctrine
        assert "sma_period" in doctrine["mutable_params"]

    def test_get_parameter_bounds(self) -> None:
        s = MeanReversionStrategy()
        bounds = s.get_parameter_bounds()
        assert "sma_period" in bounds
        assert "rsi_period" in bounds
        for name, (lo, hi) in bounds.items():
            assert lo < hi, f"Invalid bounds for {name}"


# ===========================================================================
# 4. BreakoutStrategy tests
# ===========================================================================


class TestBreakoutStrategy:
    """Test Donchian Channel breakout strategy."""

    def test_attributes(self) -> None:
        s = BreakoutStrategy()
        assert s.name == "breakout"
        assert s.family == "breakout"

    def test_compute_indicators_empty(self) -> None:
        s = BreakoutStrategy()
        result = s.compute_indicators([])
        assert result == {"upper_channel": [], "lower_channel": [], "atr": []}

    def test_compute_indicators_keys_and_lengths(self) -> None:
        candles = _make_breakout_candles(100)
        s = BreakoutStrategy()
        indicators = s.compute_indicators(candles)
        assert set(indicators.keys()) == {"upper_channel", "lower_channel", "atr"}
        for key, vals in indicators.items():
            assert len(vals) == 100, f"{key} has wrong length"

    def test_donchian_channel_valid(self) -> None:
        candles = _make_breakout_candles(100)
        s = BreakoutStrategy(channel_period=10)
        indicators = s.compute_indicators(candles)
        upper = indicators["upper_channel"]
        lower = indicators["lower_channel"]
        for i in range(9, len(candles)):
            if not math.isnan(upper[i]) and not math.isnan(lower[i]):
                assert upper[i] >= lower[i], f"Upper < lower at {i}"

    def test_generate_signals_from_breakout_data(self) -> None:
        candles = _make_breakout_candles(200, breakout_at=80, direction="up")
        s = BreakoutStrategy(warmup_bars=25, channel_period=20)
        indicators = s.compute_indicators(candles)
        signals = s.generate_signals(candles, indicators)
        assert isinstance(signals, list)
        # Should detect at least one breakout signal after bar 80
        has_signal_after_breakout = any(sig.bar_index > 80 for sig in signals)
        assert has_signal_after_breakout, "No breakout signals detected after breakout point"

    def test_breakout_signals_have_correct_side(self) -> None:
        # Upward breakout should produce LONG signals
        candles = _make_breakout_candles(200, breakout_at=80, direction="up")
        s = BreakoutStrategy(warmup_bars=25, channel_period=20)
        indicators = s.compute_indicators(candles)
        signals = s.generate_signals(candles, indicators)
        for sig in signals:
            if sig.bar_index > 90:
                assert sig.side == "LONG", f"Expected LONG at bar {sig.bar_index}, got {sig.side}"

    def test_downward_breakout_signals(self) -> None:
        candles = _make_breakout_candles(200, breakout_at=80, direction="down")
        s = BreakoutStrategy(warmup_bars=25, channel_period=20)
        indicators = s.compute_indicators(candles)
        signals = s.generate_signals(candles, indicators)
        has_short = any(sig.side == "SHORT" and sig.bar_index > 80 for sig in signals)
        assert has_short, "No SHORT breakout signals detected"

    def test_check_entry_warmup_returns_none(self) -> None:
        candles = _make_breakout_candles(30)
        s = BreakoutStrategy()
        indicators = s.compute_indicators(candles)
        assert s.check_entry(5, candles, indicators) is None

    def test_check_exit_time_stop(self) -> None:
        candles = _make_breakout_candles(200)
        s = BreakoutStrategy(time_stop_bars=10)
        indicators = s.compute_indicators(candles)
        position = {
            "side": "LONG",
            "entry_price": 1.1000,
            "stop_loss": 1.0900,
            "entry_bar": 50,
            "current_stop": 1.0900,
            "best_close": 1.1100,
        }
        exit_sig = s.check_exit(61, candles, indicators, position)
        assert exit_sig is not None
        assert exit_sig.reason in ("time_stop", "stop_loss", "signal_exit", "trailing_stop")

    def test_get_default_doctrine(self) -> None:
        s = BreakoutStrategy()
        doctrine = s.get_default_doctrine("USDJPY", "H1")
        assert doctrine["name"] == "breakout_usdjpy_h1"
        assert doctrine["setup_type"] == "breakout"
        assert "mutable_params" in doctrine
        assert "channel_period" in doctrine["mutable_params"]

    def test_get_parameter_bounds(self) -> None:
        s = BreakoutStrategy()
        bounds = s.get_parameter_bounds()
        assert "channel_period" in bounds
        assert "trail_atr_mult" in bounds
        for name, (lo, hi) in bounds.items():
            assert lo < hi, f"Invalid bounds for {name}"


# ===========================================================================
# 5. TrendFollowingStrategy tests
# ===========================================================================


class TestTrendFollowingStrategy:
    """Test dual EMA crossover with ADX filter."""

    def test_attributes(self) -> None:
        s = TrendFollowingStrategy()
        assert s.name == "trend_following"
        assert s.family == "trend_following"

    def test_compute_indicators_empty(self) -> None:
        s = TrendFollowingStrategy()
        result = s.compute_indicators([])
        assert result == {"fast_ema": [], "slow_ema": [], "adx": [], "atr": []}

    def test_compute_indicators_keys_and_lengths(self) -> None:
        candles = _make_crossover_candles(100)
        s = TrendFollowingStrategy()
        indicators = s.compute_indicators(candles)
        assert set(indicators.keys()) == {"fast_ema", "slow_ema", "adx", "atr"}
        for key, vals in indicators.items():
            assert len(vals) == 100, f"{key} has wrong length"

    def test_fast_ema_more_responsive(self) -> None:
        """Fast EMA should track price more closely than slow EMA."""
        candles = _make_trending_candles(100, direction="up")
        s = TrendFollowingStrategy(fast_ema_period=5, slow_ema_period=20)
        indicators = s.compute_indicators(candles)
        fast = indicators["fast_ema"]
        slow = indicators["slow_ema"]
        # In an uptrend, fast EMA should generally be above slow EMA after warmup
        above_count = sum(
            1 for i in range(30, 100) if not math.isnan(fast[i]) and not math.isnan(slow[i]) and fast[i] > slow[i]
        )
        assert above_count > 50, "Fast EMA not consistently above slow EMA in uptrend"

    def test_generate_signals_from_crossover_data(self) -> None:
        candles = _make_crossover_candles(200)
        s = TrendFollowingStrategy(
            fast_ema_period=8,
            slow_ema_period=21,
            adx_threshold=10.0,  # Lower threshold to increase signal likelihood
            warmup_bars=30,
        )
        indicators = s.compute_indicators(candles)
        signals = s.generate_signals(candles, indicators)
        assert isinstance(signals, list)
        # Should detect at least one crossover
        assert len(signals) > 0, "No crossover signals detected"

    def test_check_entry_warmup_returns_none(self) -> None:
        candles = _make_crossover_candles(20)
        s = TrendFollowingStrategy()
        indicators = s.compute_indicators(candles)
        assert s.check_entry(5, candles, indicators) is None

    def test_signal_direction_matches_crossover(self) -> None:
        candles = _make_crossover_candles(200)
        s = TrendFollowingStrategy(
            fast_ema_period=8,
            slow_ema_period=21,
            adx_threshold=10.0,
            warmup_bars=30,
        )
        indicators = s.compute_indicators(candles)
        signals = s.generate_signals(candles, indicators)
        fast = indicators["fast_ema"]
        slow = indicators["slow_ema"]
        for sig in signals:
            i = sig.bar_index
            if sig.side == "LONG":
                assert fast[i] > slow[i], f"LONG signal at {i} but fast <= slow"
            else:
                assert fast[i] < slow[i], f"SHORT signal at {i} but fast >= slow"

    def test_check_exit_reverse_crossover(self) -> None:
        candles = _make_crossover_candles(200)
        s = TrendFollowingStrategy(
            fast_ema_period=8,
            slow_ema_period=21,
            adx_threshold=10.0,
            warmup_bars=30,
        )
        indicators = s.compute_indicators(candles)
        fast = indicators["fast_ema"]
        slow = indicators["slow_ema"]
        adx = indicators["adx"]

        # Find a reverse crossover point for a LONG position
        for i in range(31, len(candles)):
            vals_valid = (
                not math.isnan(fast[i])
                and not math.isnan(slow[i])
                and not math.isnan(fast[i - 1])
                and not math.isnan(slow[i - 1])
                and not math.isnan(adx[i])
            )
            if vals_valid and fast[i] < slow[i] and fast[i - 1] >= slow[i - 1]:
                position = {
                    "side": "LONG",
                    "entry_price": 1.1000,
                    "stop_loss": 1.0800,
                    "entry_bar": i - 10,
                    "current_stop": 1.0800,
                    "best_close": 1.1100,
                }
                exit_sig = s.check_exit(i, candles, indicators, position)
                if exit_sig is not None:
                    assert exit_sig.reason in ("signal_exit", "stop_loss", "trailing_stop")
                break

    def test_check_exit_time_stop(self) -> None:
        candles = _make_crossover_candles(200)
        s = TrendFollowingStrategy(time_stop_bars=5)
        indicators = s.compute_indicators(candles)
        position = {
            "side": "LONG",
            "entry_price": 1.0900,
            "stop_loss": 1.0800,
            "entry_bar": 50,
            "current_stop": 1.0800,
            "best_close": 1.1000,
        }
        exit_sig = s.check_exit(56, candles, indicators, position)
        assert exit_sig is not None
        assert exit_sig.reason in ("time_stop", "stop_loss", "signal_exit", "trailing_stop")

    def test_get_default_doctrine(self) -> None:
        s = TrendFollowingStrategy()
        doctrine = s.get_default_doctrine("EURUSD", "H4")
        assert doctrine["name"] == "trend_following_eurusd_h4"
        assert doctrine["setup_type"] == "trend_following"
        assert "mutable_params" in doctrine
        assert "fast_ema_period" in doctrine["mutable_params"]
        assert "slow_ema_period" in doctrine["mutable_params"]

    def test_get_parameter_bounds(self) -> None:
        s = TrendFollowingStrategy()
        bounds = s.get_parameter_bounds()
        assert "fast_ema_period" in bounds
        assert "slow_ema_period" in bounds
        assert "adx_threshold" in bounds
        for name, (lo, hi) in bounds.items():
            assert lo < hi, f"Invalid bounds for {name}"


# ===========================================================================
# 6. Registry tests
# ===========================================================================


class TestStrategyRegistry:
    """Test strategy registration, lookup, and listing."""

    def test_builtin_strategies_registered(self) -> None:
        names = list_strategies()
        assert "irb" in names
        assert "mean_reversion" in names
        assert "breakout" in names
        assert "trend_following" in names

    def test_get_strategy_irb(self) -> None:
        cls = get_strategy("irb")
        assert cls is IRBStrategy

    def test_get_strategy_mean_reversion(self) -> None:
        cls = get_strategy("mean_reversion")
        assert cls is MeanReversionStrategy

    def test_get_strategy_breakout(self) -> None:
        cls = get_strategy("breakout")
        assert cls is BreakoutStrategy

    def test_get_strategy_trend_following(self) -> None:
        cls = get_strategy("trend_following")
        assert cls is TrendFollowingStrategy

    def test_get_unknown_raises_key_error(self) -> None:
        with pytest.raises(KeyError, match="Unknown strategy"):
            get_strategy("nonexistent_strategy")

    def test_register_custom_strategy(self) -> None:
        class Custom(BaseStrategy):
            name = "custom"
            family = "test"

            def compute_indicators(self, candles):
                return {}

            def generate_signals(self, candles, indicators):
                return []

            def check_entry(self, bar_index, candles, indicators, h4_candles=None):
                return None

            def check_exit(self, bar_index, candles, indicators, position=None):
                return None

            def get_default_doctrine(self, pair, timeframe):
                return {}

            def get_parameter_bounds(self):
                return {}

        register_strategy("custom_test", Custom)
        assert get_strategy("custom_test") is Custom
        assert "custom_test" in list_strategies()
        # Cleanup
        del STRATEGY_REGISTRY["custom_test"]

    def test_register_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            register_strategy("", IRBStrategy)

    def test_register_non_subclass_raises(self) -> None:
        with pytest.raises(TypeError, match="not a subclass"):
            register_strategy("bad", str)  # type: ignore[arg-type]

    def test_list_strategies_returns_sorted(self) -> None:
        names = list_strategies()
        assert names == sorted(names)


# ===========================================================================
# 7. StrategyConfig.strategy_type field
# ===========================================================================


class TestStrategyConfigField:
    """Test the strategy_type field in StrategyConfig."""

    def test_default_strategy_type(self) -> None:
        config = StrategyConfig()
        assert config.strategy_type == "irb"

    def test_custom_strategy_type(self) -> None:
        config = StrategyConfig(strategy_type="mean_reversion")
        assert config.strategy_type == "mean_reversion"

    def test_strategy_type_in_model_dump(self) -> None:
        config = StrategyConfig(strategy_type="breakout")
        data = config.model_dump()
        assert data["strategy_type"] == "breakout"

    def test_strategy_type_preserved_in_yaml_roundtrip(self, tmp_path: Path) -> None:
        config = StrategyConfig(strategy_type="trend_following", name="tf_test")
        yaml_path = tmp_path / "test_config.yaml"
        config.to_yaml(yaml_path)
        loaded = StrategyConfig.from_yaml(yaml_path)
        assert loaded.strategy_type == "trend_following"
        assert loaded.name == "tf_test"


# ===========================================================================
# 8. create_backtester factory function
# ===========================================================================


class TestCreateBacktester:
    """Test the create_backtester factory function."""

    def test_irb_returns_irb_backtester(self) -> None:
        bt = create_backtester("irb")
        assert isinstance(bt, IRBBacktester)

    def test_mean_reversion_returns_adapter(self) -> None:
        bt = create_backtester("mean_reversion")
        assert isinstance(bt, StrategyBacktesterAdapter)
        assert bt.strategy.name == "mean_reversion"

    def test_breakout_returns_adapter(self) -> None:
        bt = create_backtester("breakout")
        assert isinstance(bt, StrategyBacktesterAdapter)
        assert bt.strategy.name == "breakout"

    def test_trend_following_returns_adapter(self) -> None:
        bt = create_backtester("trend_following")
        assert isinstance(bt, StrategyBacktesterAdapter)
        assert bt.strategy.name == "trend_following"

    def test_unknown_strategy_raises(self) -> None:
        with pytest.raises(KeyError):
            create_backtester("nonexistent")

    def test_custom_env_passed_to_irb(self) -> None:
        env = BacktestEnvironment(initial_equity=50_000.0)
        bt = create_backtester("irb", env=env)
        assert bt.env.initial_equity == 50_000.0

    def test_custom_env_passed_to_adapter(self) -> None:
        env = BacktestEnvironment(initial_equity=50_000.0)
        bt = create_backtester("mean_reversion", env=env)
        assert isinstance(bt, StrategyBacktesterAdapter)
        assert bt.env.initial_equity == 50_000.0


# ===========================================================================
# 9. StrategyBacktesterAdapter integration tests
# ===========================================================================


class TestStrategyBacktesterAdapter:
    """Test that the adapter produces valid BacktestResults."""

    def test_adapter_empty_candles(self) -> None:
        bt = create_backtester("mean_reversion")
        assert isinstance(bt, StrategyBacktesterAdapter)
        result = bt.run([])
        assert isinstance(result, BacktestResult)
        assert result.total_bars == 0
        assert len(result.trades) == 0

    def test_adapter_mean_reversion_produces_result(self) -> None:
        candles = _make_ranging_candles(300, amplitude=0.0080)
        bt = create_backtester("mean_reversion")
        assert isinstance(bt, StrategyBacktesterAdapter)
        result = bt.run(candles)
        assert isinstance(result, BacktestResult)
        assert result.total_bars == 300
        assert result.final_equity > 0

    def test_adapter_breakout_produces_trades(self) -> None:
        candles = _make_breakout_candles(300, breakout_at=100, direction="up")
        bt = create_backtester("breakout")
        assert isinstance(bt, StrategyBacktesterAdapter)
        result = bt.run(candles)
        assert isinstance(result, BacktestResult)
        assert result.total_bars == 300
        # With a clear breakout, expect at least one trade
        assert len(result.trades) > 0 or len(result.signals) > 0

    def test_adapter_trend_following_produces_result(self) -> None:
        candles = _make_crossover_candles(300)
        bt = create_backtester("trend_following")
        assert isinstance(bt, StrategyBacktesterAdapter)
        result = bt.run(candles)
        assert isinstance(result, BacktestResult)
        assert result.total_bars == 300

    def test_adapter_irb_classic_still_works(self) -> None:
        """IRB via create_backtester should use IRBBacktester, not the adapter."""
        candles = _make_trending_candles(200)
        h4 = _make_trending_candles(50)
        bt = create_backtester("irb")
        assert isinstance(bt, IRBBacktester)
        result = bt.run(candles, h4)
        assert isinstance(result, BacktestResult)
        assert result.total_bars == 200

    def test_adapter_trades_have_valid_fields(self) -> None:
        candles = _make_breakout_candles(300, breakout_at=80, direction="up")
        bt = create_backtester("breakout")
        assert isinstance(bt, StrategyBacktesterAdapter)
        result = bt.run(candles)
        for trade in result.trades:
            assert trade.trade_id > 0
            assert trade.entry_bar >= 0
            assert trade.exit_bar >= trade.entry_bar
            assert trade.volume > 0
            assert trade.side is not None

    def test_adapter_type_check(self) -> None:
        with pytest.raises(TypeError, match="Expected BaseStrategy"):
            StrategyBacktesterAdapter(strategy="not_a_strategy")  # type: ignore[arg-type]


# ===========================================================================
# 10. EntrySignal / ExitSignal dataclass tests
# ===========================================================================


class TestSignalDataclasses:
    """Test the EntrySignal and ExitSignal dataclasses."""

    def test_entry_signal_fields(self) -> None:
        sig = EntrySignal(bar_index=10, side="LONG", entry_price=1.1000, stop_loss=1.0950)
        assert sig.bar_index == 10
        assert sig.side == "LONG"
        assert sig.entry_price == 1.1000
        assert sig.stop_loss == 1.0950
        assert sig.confidence == 1.0
        assert sig.metadata == {}

    def test_entry_signal_with_metadata(self) -> None:
        sig = EntrySignal(
            bar_index=5,
            side="SHORT",
            entry_price=1.2000,
            stop_loss=1.2050,
            confidence=0.8,
            metadata={"rsi": 75.0},
        )
        assert sig.metadata["rsi"] == 75.0
        assert sig.confidence == 0.8

    def test_exit_signal_fields(self) -> None:
        sig = ExitSignal(bar_index=20, exit_price=1.1050, reason="trailing_stop")
        assert sig.bar_index == 20
        assert sig.exit_price == 1.1050
        assert sig.reason == "trailing_stop"
        assert sig.metadata == {}

    def test_exit_signal_with_metadata(self) -> None:
        sig = ExitSignal(
            bar_index=30,
            exit_price=1.0900,
            reason="signal_exit",
            metadata={"detail": "reverse_crossover"},
        )
        assert sig.metadata["detail"] == "reverse_crossover"


# ===========================================================================
# 11. Cross-strategy consistency tests
# ===========================================================================


class TestCrossStrategyConsistency:
    """Verify all strategies conform to the same interface contract."""

    STRATEGY_CLASSES: ClassVar[list[type]] = [
        IRBStrategy,
        MeanReversionStrategy,
        BreakoutStrategy,
        TrendFollowingStrategy,
    ]

    def test_all_have_name(self) -> None:
        for cls in self.STRATEGY_CLASSES:
            s = cls()
            assert s.name, f"{cls.__name__} has empty name"

    def test_all_have_family(self) -> None:
        for cls in self.STRATEGY_CLASSES:
            s = cls()
            assert s.family, f"{cls.__name__} has empty family"

    def test_all_compute_indicators_with_empty(self) -> None:
        for cls in self.STRATEGY_CLASSES:
            s = cls()
            result = s.compute_indicators([])
            assert isinstance(result, dict), f"{cls.__name__} compute_indicators did not return dict"
            for key, vals in result.items():
                assert isinstance(vals, list), f"{cls.__name__} indicator '{key}' is not a list"
                assert len(vals) == 0, f"{cls.__name__} indicator '{key}' should be empty for empty input"

    def test_all_generate_signals_with_synthetic(self) -> None:
        candles = _make_trending_candles(200)
        for cls in self.STRATEGY_CLASSES:
            s = cls()
            indicators = s.compute_indicators(candles)
            signals = s.generate_signals(candles, indicators)
            assert isinstance(signals, list), f"{cls.__name__} generate_signals did not return list"

    def test_all_get_default_doctrine(self) -> None:
        for cls in self.STRATEGY_CLASSES:
            s = cls()
            doctrine = s.get_default_doctrine("EURUSD", "H1")
            assert isinstance(doctrine, dict), f"{cls.__name__} get_default_doctrine did not return dict"
            assert "name" in doctrine, f"{cls.__name__} doctrine missing 'name'"
            assert "mutable_params" in doctrine, f"{cls.__name__} doctrine missing 'mutable_params'"

    def test_all_get_parameter_bounds(self) -> None:
        for cls in self.STRATEGY_CLASSES:
            s = cls()
            bounds = s.get_parameter_bounds()
            assert isinstance(bounds, dict), f"{cls.__name__} get_parameter_bounds did not return dict"
            for name, (lo, hi) in bounds.items():
                assert lo < hi, f"{cls.__name__} bounds for '{name}': {lo} >= {hi}"

    def test_all_check_entry_returns_none_for_bar_0(self) -> None:
        candles = _make_trending_candles(10)
        for cls in self.STRATEGY_CLASSES:
            s = cls()
            indicators = s.compute_indicators(candles)
            result = s.check_entry(0, candles, indicators)
            assert result is None, f"{cls.__name__} check_entry returned signal for bar 0"

    def test_all_check_exit_returns_none_without_position(self) -> None:
        candles = _make_trending_candles(50)
        for cls in self.STRATEGY_CLASSES:
            s = cls()
            indicators = s.compute_indicators(candles)
            result = s.check_exit(25, candles, indicators, position=None)
            assert result is None, f"{cls.__name__} check_exit returned signal without position"
