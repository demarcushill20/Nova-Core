"""3 EMA + Stochastic RSI + ATR strategy.

Mechanical spec (SignalCave write-up of the referenced YouTube strategy):

- Trend filter: three EMAs -- EMA(8) fast, EMA(14) medium, EMA(50) slow.
  * Long bias  when EMA8 > EMA14 > EMA50 and close > EMA50.
  * Short bias when EMA8 < EMA14 < EMA50 and close < EMA50.
- Entry trigger: Stochastic RSI (settings 3, 3, 14, 14) %K / %D crossover.
  * Long  when %K crosses *above* %D (and %K is not already overbought).
  * Short when %K crosses *below* %D (and %K is not already oversold).
- Risk: ATR(14) sets a volatility-scaled stop. Take-profit is a fixed
  reward-to-risk multiple of that stop distance.

All signals are confirmed on the *closed* candle; the generic
``StrategyBacktesterAdapter`` fills the entry on the next bar (1-bar delay),
so there is no lookahead / repaint.
"""

from __future__ import annotations

import math
from typing import Any

from novatrade.backtest.engine import compute_atr, compute_ema
from novatrade.models import Candle
from novatrade.strategies.base import BaseStrategy, EntrySignal, ExitSignal

NAN = float("nan")


# ---------------------------------------------------------------------------
# Indicator helpers (RSI + Stochastic RSI -- not in the shared engine module)
# ---------------------------------------------------------------------------


def compute_rsi(closes: list[float], period: int = 14) -> list[float]:
    """Wilder-smoothed RSI. Returns a NaN-padded list the length of ``closes``."""
    n = len(closes)
    rsi: list[float] = [NAN] * n
    if n <= period:
        return rsi

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains[i] = change if change > 0 else 0.0
        losses[i] = -change if change < 0 else 0.0

    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    rsi[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return rsi


def _sma_nan(values: list[float], window: int) -> list[float]:
    """Simple moving average that only emits a value when the full window is valid."""
    n = len(values)
    out: list[float] = [NAN] * n
    if window <= 0:
        return out
    for i in range(n):
        if i + 1 < window:
            continue
        chunk = values[i - window + 1 : i + 1]
        if any(math.isnan(v) for v in chunk):
            continue
        out[i] = sum(chunk) / window
    return out


def compute_stoch_rsi(
    closes: list[float],
    rsi_period: int = 14,
    stoch_period: int = 14,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> tuple[list[float], list[float]]:
    """Stochastic RSI %K and %D (each scaled 0..100), NaN-padded.

    Pipeline: RSI(rsi_period) -> stochastic over stoch_period -> SMA(k_smooth)
    for %K -> SMA(d_smooth) of %K for %D. This matches the common 3/3/14/14
    Stoch RSI configuration.
    """
    rsi = compute_rsi(closes, rsi_period)
    n = len(rsi)
    raw_stoch: list[float] = [NAN] * n
    for i in range(n):
        if i + 1 < stoch_period:
            continue
        window = rsi[i - stoch_period + 1 : i + 1]
        if any(math.isnan(v) for v in window):
            continue
        lo = min(window)
        hi = max(window)
        raw_stoch[i] = 0.0 if hi == lo else (rsi[i] - lo) / (hi - lo) * 100.0
    k = _sma_nan(raw_stoch, k_smooth)
    d = _sma_nan(k, d_smooth)
    return k, d


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class ThreeEmaStochRsiStrategy(BaseStrategy):
    """3 EMA trend filter + Stochastic RSI crossover trigger + ATR exits.

    Entry:
    - LONG  : EMA8 > EMA14 > EMA50, close > EMA50, %K crosses above %D, %K < overbought.
    - SHORT : EMA8 < EMA14 < EMA50, close < EMA50, %K crosses below %D, %K > oversold.

    Exit (handled in ``check_exit``, reconstructed from the position's fixed stop):
    - Fixed ATR-based stop loss (set at entry).
    - Fixed take-profit at ``tp_rr`` x stop distance.
    - Optional time stop after ``time_stop_bars``.
    """

    name = "three_ema_stoch_rsi"
    family = "trend_following"

    def __init__(
        self,
        ema_fast_period: int = 8,
        ema_medium_period: int = 14,
        ema_slow_period: int = 50,
        rsi_period: int = 14,
        stoch_period: int = 14,
        stoch_k_smooth: int = 3,
        stoch_d_smooth: int = 3,
        stoch_overbought: float = 80.0,
        stoch_oversold: float = 20.0,
        atr_period: int = 14,
        sl_atr_mult: float = 1.5,
        tp_rr: float = 1.5,
        time_stop_bars: int = 0,
        warmup_bars: int = 60,
        pip_buffer: float = 0.0001,
    ) -> None:
        self.ema_fast_period = ema_fast_period
        self.ema_medium_period = ema_medium_period
        self.ema_slow_period = ema_slow_period
        self.rsi_period = rsi_period
        self.stoch_period = stoch_period
        self.stoch_k_smooth = stoch_k_smooth
        self.stoch_d_smooth = stoch_d_smooth
        self.stoch_overbought = stoch_overbought
        self.stoch_oversold = stoch_oversold
        self.atr_period = atr_period
        self.sl_atr_mult = sl_atr_mult
        self.tp_rr = tp_rr
        self.time_stop_bars = time_stop_bars
        self.warmup_bars = warmup_bars
        self.pip_buffer = pip_buffer

    # -- indicators --------------------------------------------------------

    def compute_indicators(self, candles: list[Candle]) -> dict[str, list[float]]:
        """Compute the three EMAs, ATR, and Stochastic RSI %K/%D."""
        if not candles:
            return {"ema_fast": [], "ema_medium": [], "ema_slow": [], "atr": [], "stoch_k": [], "stoch_d": []}

        closes = [c.close for c in candles]
        stoch_k, stoch_d = compute_stoch_rsi(
            closes,
            rsi_period=self.rsi_period,
            stoch_period=self.stoch_period,
            k_smooth=self.stoch_k_smooth,
            d_smooth=self.stoch_d_smooth,
        )
        return {
            "ema_fast": compute_ema(closes, self.ema_fast_period),
            "ema_medium": compute_ema(closes, self.ema_medium_period),
            "ema_slow": compute_ema(closes, self.ema_slow_period),
            "atr": compute_atr(candles, self.atr_period),
            "stoch_k": stoch_k,
            "stoch_d": stoch_d,
        }

    def generate_signals(self, candles: list[Candle], indicators: dict[str, list[float]]) -> list[EntrySignal]:
        """Scan every bar for entry signals (used by optimizer/inspection)."""
        signals: list[EntrySignal] = []
        for i in range(len(candles)):
            sig = self.check_entry(i, candles, indicators)
            if sig is not None:
                signals.append(sig)
        return signals

    # -- entry -------------------------------------------------------------

    def check_entry(
        self,
        bar_index: int,
        candles: list[Candle],
        indicators: dict[str, list[float]],
        h4_candles: list[Candle] | None = None,
    ) -> EntrySignal | None:
        """3-EMA trend alignment + Stoch RSI %K/%D crossover on the closed bar."""
        i = bar_index
        if i < self.warmup_bars or i < 1 or i >= len(candles):
            return None

        bar = candles[i]
        ema_f = indicators.get("ema_fast", [])
        ema_m = indicators.get("ema_medium", [])
        ema_s = indicators.get("ema_slow", [])
        atr = indicators.get("atr", [])
        k = indicators.get("stoch_k", [])
        d = indicators.get("stoch_d", [])

        if any(i >= len(arr) for arr in (ema_f, ema_m, ema_s, atr, k, d)):
            return None
        vals = [ema_f[i], ema_m[i], ema_s[i], atr[i], k[i], d[i], k[i - 1], d[i - 1]]
        if any(math.isnan(v) for v in vals):
            return None
        if atr[i] <= 0:
            return None

        stop_distance = self.sl_atr_mult * atr[i]
        if stop_distance <= 0:
            return None

        cross_up = k[i] > d[i] and k[i - 1] <= d[i - 1]
        cross_down = k[i] < d[i] and k[i - 1] >= d[i - 1]

        long_trend = ema_f[i] > ema_m[i] > ema_s[i] and bar.close > ema_s[i]
        short_trend = ema_f[i] < ema_m[i] < ema_s[i] and bar.close < ema_s[i]

        if long_trend and cross_up and k[i] < self.stoch_overbought:
            entry_price = bar.close + self.pip_buffer
            return EntrySignal(
                bar_index=i,
                side="LONG",
                entry_price=entry_price,
                stop_loss=entry_price - stop_distance,
                metadata={"k": k[i], "d": d[i], "atr": atr[i], "ema_slow": ema_s[i]},
            )

        if short_trend and cross_down and k[i] > self.stoch_oversold:
            entry_price = bar.close - self.pip_buffer
            return EntrySignal(
                bar_index=i,
                side="SHORT",
                entry_price=entry_price,
                stop_loss=entry_price + stop_distance,
                metadata={"k": k[i], "d": d[i], "atr": atr[i], "ema_slow": ema_s[i]},
            )

        return None

    # -- exit --------------------------------------------------------------

    def check_exit(
        self,
        bar_index: int,
        candles: list[Candle],
        indicators: dict[str, list[float]],
        position: dict[str, Any] | None = None,
    ) -> ExitSignal | None:
        """Fixed ATR stop + R-multiple take-profit (+ optional time stop).

        The take-profit is reconstructed from the position's fixed stop so it
        works with the generic adapter (which only carries entry_price /
        stop_loss / entry_bar in the position dict). Stop is checked first so a
        same-bar stop-and-target ambiguity resolves conservatively.
        """
        if position is None or bar_index >= len(candles) or bar_index < 1:
            return None

        bar = candles[bar_index]
        side = position.get("side", "LONG")
        entry_price = position.get("entry_price", bar.close)
        stop_loss = position.get("stop_loss", 0.0)
        entry_bar = position.get("entry_bar", 0)

        risk = abs(entry_price - stop_loss)

        if side == "LONG":
            if bar.low <= stop_loss:
                return ExitSignal(bar_index=bar_index, exit_price=stop_loss, reason="stop_loss")
            take_profit = entry_price + self.tp_rr * risk
            if bar.high >= take_profit:
                return ExitSignal(bar_index=bar_index, exit_price=take_profit, reason="signal_exit")
        else:
            if bar.high >= stop_loss:
                return ExitSignal(bar_index=bar_index, exit_price=stop_loss, reason="stop_loss")
            take_profit = entry_price - self.tp_rr * risk
            if bar.low <= take_profit:
                return ExitSignal(bar_index=bar_index, exit_price=take_profit, reason="signal_exit")

        if self.time_stop_bars > 0 and (bar_index - entry_bar) >= self.time_stop_bars:
            return ExitSignal(bar_index=bar_index, exit_price=bar.close, reason="time_stop")

        return None

    # -- doctrine / bounds -------------------------------------------------

    def get_default_doctrine(self, pair: str, timeframe: str) -> dict[str, Any]:
        """Return the default doctrine descriptor for this strategy type."""
        return {
            "name": f"three_ema_stoch_rsi_{pair.lower()}_{timeframe.lower()}",
            "version": "1.0.0",
            "description": f"3 EMA + Stochastic RSI + ATR for {pair} on {timeframe}",
            "concept": (
                "Three EMAs (8/14/50) define trend direction; a Stochastic RSI "
                "(3/3/14/14) %K/%D crossover triggers entries in the trend "
                "direction; ATR(14) sizes the stop and an R-multiple take-profit."
            ),
            "setup_type": "trend_following",
            "entry_signal": "stoch_rsi_crossover",
            "confirmations": ["three_ema_alignment"],
            "exit_method": "atr_stop_and_rr_target",
            "data": {"pair": pair, "timeframes": [timeframe], "min_bars": 3000},
            "mutable_params": {
                "ema_fast_period": {"default": 8, "min": 3, "max": 20, "step": 1},
                "ema_medium_period": {"default": 14, "min": 8, "max": 30, "step": 1},
                "ema_slow_period": {"default": 50, "min": 30, "max": 100, "step": 1},
                "sl_atr_mult": {"default": 1.5, "min": 0.5, "max": 4.0, "step": 0.1},
                "tp_rr": {"default": 1.5, "min": 0.5, "max": 4.0, "step": 0.1},
            },
            "immutable_params": {"pip_buffer": 0.0001},
            "filters_mandatory": ["three_ema_alignment", "stoch_rsi_crossover"],
            "filters_optional": [],
            "execution": {
                "spread_pips": 1.0,
                "slippage_pips": 0.0,
                "commission_per_lot": 0.0,
                "leverage": 100,
                "fill_policy": "next_bar_open",
            },
            "validation": {
                "min_trades": 40,
                "min_months": 6,
                "max_drawdown_pct": 20.0,
                "min_profit_factor": 1.2,
                "min_sharpe": 0.4,
            },
        }

    def get_parameter_bounds(self) -> dict[str, tuple[float, float]]:
        """Return optimizer parameter bounds."""
        return {
            "ema_fast_period": (3, 20),
            "ema_medium_period": (8, 30),
            "ema_slow_period": (30, 100),
            "rsi_period": (7, 21),
            "stoch_period": (7, 21),
            "sl_atr_mult": (0.5, 4.0),
            "tp_rr": (0.5, 4.0),
            "time_stop_bars": (0, 200),
            "warmup_bars": (50, 120),
        }
