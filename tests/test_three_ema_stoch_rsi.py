"""Tests for the 3 EMA + Stochastic RSI + ATR strategy."""

from __future__ import annotations

import math

import pytest

from novatrade.backtest.engine import StrategyBacktesterAdapter
from novatrade.models import Candle
from novatrade.strategies.base import BaseStrategy, EntrySignal
from novatrade.strategies.registry import get_strategy, list_strategies
from novatrade.strategies.three_ema_stoch_rsi import (
    ThreeEmaStochRsiStrategy,
    compute_rsi,
    compute_stoch_rsi,
)


def _candle(ts: float, o: float, h: float, lo: float, c: float) -> Candle:
    return Candle(timestamp=ts, open=o, high=h, low=lo, close=c, volume=100.0, symbol="EURUSD", timeframe="M15")


def _wave_candles(n: int = 400) -> list[Candle]:
    """Synthetic upward-drifting sine wave -- produces trend + oscillator turns."""
    candles = []
    price = 1.1000
    for i in range(n):
        price = 1.1000 + 0.02 * (i / n) + 0.0030 * math.sin(i / 6.0)
        nxt = 1.1000 + 0.02 * ((i + 1) / n) + 0.0030 * math.sin((i + 1) / 6.0)
        hi = max(price, nxt) + 0.0008
        lo = min(price, nxt) - 0.0008
        candles.append(_candle(float(i * 900), price, hi, lo, nxt))
    return candles


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------


class TestIndicators:
    def test_rsi_length_and_range(self) -> None:
        closes = [1.0 + 0.01 * math.sin(i / 3.0) for i in range(100)]
        rsi = compute_rsi(closes, 14)
        assert len(rsi) == len(closes)
        assert all(math.isnan(v) for v in rsi[:14])
        valid = [v for v in rsi if not math.isnan(v)]
        assert valid, "expected some valid RSI values"
        assert all(0.0 <= v <= 100.0 for v in valid)

    def test_rsi_all_up_is_100(self) -> None:
        closes = [1.0 + 0.001 * i for i in range(50)]
        rsi = compute_rsi(closes, 14)
        assert rsi[-1] == pytest.approx(100.0)

    def test_stoch_rsi_length_and_range(self) -> None:
        closes = [1.0 + 0.01 * math.sin(i / 4.0) for i in range(120)]
        k, d = compute_stoch_rsi(closes, 14, 14, 3, 3)
        assert len(k) == len(closes) == len(d)
        for arr in (k, d):
            valid = [v for v in arr if not math.isnan(v)]
            assert valid
            assert all(-1e-9 <= v <= 100.0 + 1e-9 for v in valid)

    def test_short_series_no_crash(self) -> None:
        assert all(math.isnan(v) for v in compute_rsi([1.0, 1.1], 14))
        k, d = compute_stoch_rsi([1.0, 1.1, 1.2], 14, 14, 3, 3)
        assert all(math.isnan(v) for v in k)
        assert all(math.isnan(v) for v in d)


# ---------------------------------------------------------------------------
# Strategy contract
# ---------------------------------------------------------------------------


class TestStrategyContract:
    def test_is_base_strategy(self) -> None:
        assert issubclass(ThreeEmaStochRsiStrategy, BaseStrategy)
        s = ThreeEmaStochRsiStrategy()
        assert s.name == "three_ema_stoch_rsi"
        assert s.family == "trend_following"

    def test_registered(self) -> None:
        assert "three_ema_stoch_rsi" in list_strategies()
        assert get_strategy("three_ema_stoch_rsi") is ThreeEmaStochRsiStrategy

    def test_indicators_have_all_keys(self) -> None:
        candles = _wave_candles(200)
        ind = ThreeEmaStochRsiStrategy().compute_indicators(candles)
        for key in ("ema_fast", "ema_medium", "ema_slow", "atr", "stoch_k", "stoch_d"):
            assert key in ind
            assert len(ind[key]) == len(candles)

    def test_empty_candles(self) -> None:
        ind = ThreeEmaStochRsiStrategy().compute_indicators([])
        assert all(v == [] for v in ind.values())

    def test_doctrine_and_bounds(self) -> None:
        s = ThreeEmaStochRsiStrategy()
        doc = s.get_default_doctrine("EURUSD", "M15")
        assert doc["setup_type"] == "trend_following"
        bounds = s.get_parameter_bounds()
        assert "sl_atr_mult" in bounds and "tp_rr" in bounds


# ---------------------------------------------------------------------------
# Entry / exit logic
# ---------------------------------------------------------------------------


def _fab_indicators(n: int) -> dict[str, list[float]]:
    """Flat, no-signal indicator arrays of length n (override specific bars in tests)."""
    return {
        "ema_fast": [1.1000] * n,
        "ema_medium": [1.1000] * n,
        "ema_slow": [1.1000] * n,
        "atr": [0.0010] * n,
        "stoch_k": [50.0] * n,
        "stoch_d": [50.0] * n,
    }


class TestEntryExit:
    def test_long_entry_on_aligned_trend_and_crossup(self) -> None:
        """Bullish EMA stack + fresh %K-over-%D crossover below overbought -> LONG."""
        s = ThreeEmaStochRsiStrategy(warmup_bars=2, sl_atr_mult=1.5)
        candles = _wave_candles(10)
        candles[5] = _candle(float(5 * 900), 1.1010, 1.1015, 1.1005, 1.1012)
        ind = _fab_indicators(10)
        # bullish stack with close above slow EMA at bar 5
        ind["ema_fast"][5], ind["ema_medium"][5], ind["ema_slow"][5] = 1.1009, 1.1006, 1.1003
        # fresh crossup, %K below overbought
        ind["stoch_k"][4], ind["stoch_d"][4] = 30.0, 35.0
        ind["stoch_k"][5], ind["stoch_d"][5] = 40.0, 36.0
        sig = s.check_entry(5, candles, ind)
        assert sig is not None and sig.side == "LONG"
        assert isinstance(sig, EntrySignal)
        assert sig.entry_price == pytest.approx(1.1012 + s.pip_buffer)
        assert sig.stop_loss == pytest.approx(sig.entry_price - 1.5 * 0.0010)

    def test_short_entry_on_aligned_downtrend_and_crossdown(self) -> None:
        s = ThreeEmaStochRsiStrategy(warmup_bars=2)
        candles = _wave_candles(10)
        candles[5] = _candle(float(5 * 900), 1.0990, 1.0995, 1.0985, 1.0988)
        ind = _fab_indicators(10)
        ind["ema_fast"][5], ind["ema_medium"][5], ind["ema_slow"][5] = 1.0991, 1.0994, 1.0997
        ind["stoch_k"][4], ind["stoch_d"][4] = 70.0, 65.0
        ind["stoch_k"][5], ind["stoch_d"][5] = 60.0, 64.0
        sig = s.check_entry(5, candles, ind)
        assert sig is not None and sig.side == "SHORT"
        assert sig.stop_loss > sig.entry_price

    def test_no_entry_when_overbought(self) -> None:
        """A crossup that is already overbought is suppressed."""
        s = ThreeEmaStochRsiStrategy(warmup_bars=2, stoch_overbought=80.0)
        candles = _wave_candles(10)
        ind = _fab_indicators(10)
        ind["ema_fast"][5], ind["ema_medium"][5], ind["ema_slow"][5] = 1.1009, 1.1006, 1.1003
        candles[5] = _candle(float(5 * 900), 1.1010, 1.1015, 1.1005, 1.1012)
        ind["stoch_k"][4], ind["stoch_d"][4] = 82.0, 83.0
        ind["stoch_k"][5], ind["stoch_d"][5] = 90.0, 85.0
        assert s.check_entry(5, candles, ind) is None

    def test_warmup_blocks_early_entries(self) -> None:
        s = ThreeEmaStochRsiStrategy(warmup_bars=100)
        candles = _wave_candles(200)
        ind = s.compute_indicators(candles)
        assert all(s.check_entry(i, candles, ind) is None for i in range(100))

    def test_long_stop_loss_exit(self) -> None:
        s = ThreeEmaStochRsiStrategy()
        candles = _wave_candles(50)
        pos = {"side": "LONG", "entry_price": 1.1000, "stop_loss": 1.0980, "entry_bar": 0}
        # bar whose low pierces the stop
        candles[10] = _candle(float(10 * 900), 1.0990, 1.0995, 1.0975, 1.0985)
        ex = s.check_exit(10, candles, {}, pos)
        assert ex is not None and ex.reason == "stop_loss"
        assert ex.exit_price == pytest.approx(1.0980)

    def test_long_take_profit_exit(self) -> None:
        s = ThreeEmaStochRsiStrategy(tp_rr=2.0)
        candles = _wave_candles(50)
        pos = {"side": "LONG", "entry_price": 1.1000, "stop_loss": 1.0980, "entry_bar": 0}
        # risk = 0.0020 -> TP at entry + 2.0*0.0020 = 1.1040
        candles[10] = _candle(float(10 * 900), 1.1030, 1.1045, 1.1025, 1.1042)
        ex = s.check_exit(10, candles, {}, pos)
        assert ex is not None and ex.reason == "signal_exit"
        assert ex.exit_price == pytest.approx(1.1040)

    def test_time_stop(self) -> None:
        s = ThreeEmaStochRsiStrategy(time_stop_bars=5)
        candles = _wave_candles(50)
        pos = {"side": "LONG", "entry_price": 1.1000, "stop_loss": 1.0900, "entry_bar": 0}
        ex = s.check_exit(6, candles, {}, pos)
        assert ex is not None and ex.reason == "time_stop"

    def test_no_exit_when_flat(self) -> None:
        s = ThreeEmaStochRsiStrategy()
        candles = _wave_candles(20)
        assert s.check_exit(5, candles, {}, None) is None


# ---------------------------------------------------------------------------
# End-to-end through the generic adapter
# ---------------------------------------------------------------------------


def test_runs_through_adapter_no_crash() -> None:
    """The strategy plugs into the generic adapter and returns a valid result."""
    s = ThreeEmaStochRsiStrategy(warmup_bars=60)
    candles = _wave_candles(600)
    result = StrategyBacktesterAdapter(s).run(candles)
    assert result.total_bars == 600
    assert isinstance(result.trades, list)
    for t in result.trades:
        assert t.entry_bar <= t.exit_bar


def test_adapter_produces_trade_with_injected_signal() -> None:
    """A guaranteed LONG entry flows through adapter entry -> fill -> TP exit."""

    class _Canned(ThreeEmaStochRsiStrategy):
        def compute_indicators(self, candles):  # type: ignore[override]
            n = len(candles)
            ind = _fab_indicators(n)
            # Force a LONG signal at bar 3.
            ind["ema_fast"][3], ind["ema_medium"][3], ind["ema_slow"][3] = 1.1009, 1.1006, 1.1003
            ind["stoch_k"][2], ind["stoch_d"][2] = 30.0, 35.0
            ind["stoch_k"][3], ind["stoch_d"][3] = 40.0, 36.0
            return ind

    s = _Canned(warmup_bars=2, sl_atr_mult=1.0, tp_rr=1.0)
    candles = [_candle(float(i * 900), 1.1010, 1.1015, 1.1006, 1.1012) for i in range(8)]
    # entry fills at bar 4 (close[3]+buffer ~1.1013, stop ~1.1003); bar 5 spikes to hit TP.
    candles[5] = _candle(float(5 * 900), 1.1013, 1.1030, 1.1012, 1.1025)
    result = StrategyBacktesterAdapter(s).run(candles)
    assert len(result.trades) == 1
    assert result.trades[0].side.name == "LONG"
