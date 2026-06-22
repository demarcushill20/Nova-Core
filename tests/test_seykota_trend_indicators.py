"""Regression tests for the four 'core indicators' of the Seykota trend system.

The reverse-engineered system (task-1072) is, per its public rules, four
*simple mathematic calculations*:

  1. Trend filter   -- EMA(N): long-only bias above it, short-only below.
  2. Long breakout  -- close breaks above the prior 20-bar high  (Donchian upper).
  3. Short breakout -- close breaks below the prior 20-bar low    (Donchian lower).
  4. ATR            -- Wilder average true range (sizing the trailing stop).

These were shipped (commit 4dd9ab1) without a dedicated unit test. This module
pins each calculation to a hand-checkable fixture so a refactor that silently
changes the indicator math (e.g. swapping SMA for Donchian, or a look-ahead in
the breakout comparison) fails loudly. No strategy edge is asserted here --
only that the indicator core computes what the rules say it does.
"""

from __future__ import annotations

import math

from novatrade.backtest.engine import compute_atr, compute_ema
from novatrade.models import Candle
from novatrade.strategies.breakout import _donchian_lower, _donchian_upper
from novatrade.strategies.seykota_trend import SeykotaTrendStrategy


def _c(high: float, low: float, close: float, *, open_: float | None = None) -> Candle:
    """Minimal OHLC candle; open defaults to close when irrelevant to the test."""
    return Candle(
        timestamp=0.0,
        open=close if open_ is None else open_,
        high=high,
        low=low,
        close=close,
        volume=0.0,
    )


# ---------------------------------------------------------------------------
# 1. Trend filter -- EMA
# ---------------------------------------------------------------------------


def test_ema_is_nan_free_and_seeded_to_first_close() -> None:
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    ema = compute_ema(closes, period=3)
    assert len(ema) == len(closes)
    assert ema[0] == closes[0]  # seeded, not NaN -- whole series is usable
    assert all(not math.isnan(v) for v in ema)


def test_ema_recurrence_matches_closed_form() -> None:
    closes = [10.0, 11.0, 12.0]
    period = 4
    k = 2.0 / (period + 1)
    ema = compute_ema(closes, period)
    expected1 = closes[1] * k + closes[0] * (1 - k)
    expected2 = closes[2] * k + expected1 * (1 - k)
    assert math.isclose(ema[1], expected1)
    assert math.isclose(ema[2], expected2)


# ---------------------------------------------------------------------------
# 2 & 3. Long / short breakout -- Donchian channels
# ---------------------------------------------------------------------------


def test_donchian_upper_is_prior_window_high_and_nan_padded() -> None:
    candles = [_c(high=h, low=h - 1.0, close=h - 0.5) for h in [5, 7, 6, 9, 8]]
    upper = _donchian_upper(candles, period=3)
    # First (period-1) bars cannot form a full window -> NaN.
    assert math.isnan(upper[0]) and math.isnan(upper[1])
    assert upper[2] == max(5, 7, 6)
    assert upper[3] == max(7, 6, 9)
    assert upper[4] == max(6, 9, 8)


def test_donchian_lower_is_prior_window_low_and_nan_padded() -> None:
    candles = [_c(high=l_ + 1.0, low=l_, close=l_ + 0.5) for l_ in [5, 3, 4, 1, 2]]
    lower = _donchian_lower(candles, period=3)
    assert math.isnan(lower[0]) and math.isnan(lower[1])
    assert lower[2] == min(5, 3, 4)
    assert lower[3] == min(3, 4, 1)
    assert lower[4] == min(4, 1, 2)


# ---------------------------------------------------------------------------
# 4. ATR -- Wilder smoothing
# ---------------------------------------------------------------------------


def test_atr_seed_is_mean_true_range_then_wilder_smoothed() -> None:
    # Constant 2.0 high-low range, no gaps -> every TR == 2.0 -> ATR == 2.0.
    candles = [_c(high=12.0, low=10.0, close=11.0) for _ in range(6)]
    atr = compute_atr(candles, period=3)
    assert math.isnan(atr[0]) and math.isnan(atr[1])  # warmup
    assert math.isclose(atr[2], 2.0)  # seed = mean of first 3 TRs
    assert all(math.isclose(atr[i], 2.0) for i in range(2, len(candles)))


def test_atr_true_range_accounts_for_gaps() -> None:
    # A gap-up: prev close 10, next bar entirely above it -> TR uses the gap.
    candles = [_c(high=10.5, low=9.5, close=10.0), _c(high=14.0, low=13.0, close=13.5)]
    atr = compute_atr(candles, period=1)
    # period=1: ATR seeds at index 0 with TR[0], then index 1 == TR[1].
    # TR[1] = max(high-low=1.0, |high-prevclose|=4.0, |low-prevclose|=3.0) = 4.0
    assert math.isclose(atr[1], 4.0)


# ---------------------------------------------------------------------------
# Integration: the four indicators wired into the strategy's entry gate
# ---------------------------------------------------------------------------


def test_trend_filter_vetoes_breakout_against_the_ema() -> None:
    """A valid long breakout below the EMA must be rejected by the trend filter."""
    strat = SeykotaTrendStrategy(channel_period=3, atr_period=3, ema_period=3, warmup_bars=4)
    # Sharp drop then a bounce: the slow EMA lags the drop and stays elevated,
    # while the bounce makes a new 3-bar high (a genuine long breakout) that is
    # still *below* the EMA -- the trend filter must veto the long.
    closes = [40.0, 12.0, 11.0, 12.0, 13.0]
    candles = [_c(high=c + 0.1, low=c - 0.1, close=c) for c in closes]
    ind = strat.compute_indicators(candles)

    i = len(candles) - 1
    # Sanity: it *is* a raw breakout (close above prior upper channel)...
    assert candles[i].close > ind["upper_channel"][i - 1]
    # ...but price is below the EMA, so the filtered entry is None.
    assert candles[i].close < ind["ema"][i]
    assert strat.check_entry(i, candles, ind) is None
