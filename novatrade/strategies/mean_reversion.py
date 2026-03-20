"""Bollinger Band mean reversion strategy.

Template strategy: enters when price touches Bollinger Band extremes with RSI
confirmation, exits when price returns to the SMA midline or time stop fires.
"""

from __future__ import annotations

import math
from typing import Any

from novatrade.models import Candle
from novatrade.strategies.base import BaseStrategy, EntrySignal, ExitSignal

# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------


def _sma(values: list[float], period: int) -> list[float]:
    """Simple moving average, NaN-padded for warmup."""
    n = len(values)
    result = [float("nan")] * n
    if n < period:
        return result
    running = sum(values[:period])
    result[period - 1] = running / period
    for i in range(period, n):
        running += values[i] - values[i - period]
        result[i] = running / period
    return result


def _std(values: list[float], period: int) -> list[float]:
    """Rolling standard deviation, NaN-padded for warmup."""
    n = len(values)
    result = [float("nan")] * n
    if n < period:
        return result
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        result[i] = math.sqrt(variance)
    return result


def _rsi(closes: list[float], period: int) -> list[float]:
    """Relative Strength Index (Wilder smoothing), NaN-padded."""
    n = len(closes)
    result = [float("nan")] * n
    if n < period + 1:
        return result

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        delta = closes[i] - closes[i - 1]
        gains[i] = max(delta, 0.0)
        losses[i] = max(-delta, 0.0)

    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period

    if avg_loss == 0:
        result[period] = 100.0
    else:
        result[period] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result[i] = 100.0
        else:
            result[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    return result


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class MeanReversionStrategy(BaseStrategy):
    """Bollinger Band mean reversion with RSI confirmation.

    Entry:
    - Price touches lower band + RSI below oversold threshold -> LONG
    - Price touches upper band + RSI above overbought threshold -> SHORT

    Exit:
    - Price returns to SMA midline
    - Time stop after N bars
    - Stop-loss beyond opposite band
    """

    name = "mean_reversion"
    family = "mean_reversion"

    def __init__(
        self,
        sma_period: int = 20,
        bb_std_mult: float = 2.0,
        rsi_period: int = 14,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
        time_stop_bars: int = 30,
        warmup_bars: int = 25,
        pip_buffer: float = 0.0001,
    ) -> None:
        self.sma_period = sma_period
        self.bb_std_mult = bb_std_mult
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.time_stop_bars = time_stop_bars
        self.warmup_bars = warmup_bars
        self.pip_buffer = pip_buffer

    def compute_indicators(self, candles: list[Candle]) -> dict[str, list[float]]:
        """Compute SMA, Bollinger Bands (upper/lower), and RSI."""
        if not candles:
            return {"sma": [], "upper_band": [], "lower_band": [], "rsi": []}

        closes = [c.close for c in candles]
        n = len(closes)

        sma = _sma(closes, self.sma_period)
        std = _std(closes, self.sma_period)
        rsi = _rsi(closes, self.rsi_period)

        upper_band = [float("nan")] * n
        lower_band = [float("nan")] * n
        for i in range(n):
            if not math.isnan(sma[i]) and not math.isnan(std[i]):
                upper_band[i] = sma[i] + self.bb_std_mult * std[i]
                lower_band[i] = sma[i] - self.bb_std_mult * std[i]

        return {
            "sma": sma,
            "upper_band": upper_band,
            "lower_band": lower_band,
            "rsi": rsi,
        }

    def generate_signals(self, candles: list[Candle], indicators: dict[str, list[float]]) -> list[EntrySignal]:
        """Scan all bars for mean-reversion entry signals."""
        signals: list[EntrySignal] = []
        for i in range(len(candles)):
            sig = self.check_entry(i, candles, indicators)
            if sig is not None:
                signals.append(sig)
        return signals

    def check_entry(
        self,
        bar_index: int,
        candles: list[Candle],
        indicators: dict[str, list[float]],
        h4_candles: list[Candle] | None = None,
    ) -> EntrySignal | None:
        """Check Bollinger Band + RSI entry conditions."""
        i = bar_index
        if i < self.warmup_bars or i >= len(candles):
            return None

        bar = candles[i]
        sma = indicators.get("sma", [])
        upper = indicators.get("upper_band", [])
        lower = indicators.get("lower_band", [])
        rsi = indicators.get("rsi", [])

        if i >= len(sma) or i >= len(upper) or i >= len(lower) or i >= len(rsi):
            return None
        if any(math.isnan(v[i]) for v in [sma, upper, lower, rsi]):
            return None

        # Long: price touches lower band + RSI oversold
        if bar.close <= lower[i] and rsi[i] <= self.rsi_oversold:
            entry_price = bar.close + self.pip_buffer
            stop_loss = lower[i] - (upper[i] - lower[i]) * 0.5  # stop beyond band
            return EntrySignal(
                bar_index=i,
                side="LONG",
                entry_price=entry_price,
                stop_loss=stop_loss,
                metadata={
                    "rsi": rsi[i],
                    "lower_band": lower[i],
                    "sma": sma[i],
                },
            )

        # Short: price touches upper band + RSI overbought
        if bar.close >= upper[i] and rsi[i] >= self.rsi_overbought:
            entry_price = bar.close - self.pip_buffer
            stop_loss = upper[i] + (upper[i] - lower[i]) * 0.5  # stop beyond band
            return EntrySignal(
                bar_index=i,
                side="SHORT",
                entry_price=entry_price,
                stop_loss=stop_loss,
                metadata={
                    "rsi": rsi[i],
                    "upper_band": upper[i],
                    "sma": sma[i],
                },
            )

        return None

    def check_exit(
        self,
        bar_index: int,
        candles: list[Candle],
        indicators: dict[str, list[float]],
        position: dict[str, Any] | None = None,
    ) -> ExitSignal | None:
        """Check mean-reversion exit: return to SMA, stop-loss, or time stop."""
        if position is None or bar_index >= len(candles):
            return None

        bar = candles[bar_index]
        sma = indicators.get("sma", [])
        pos_side = position.get("side", "LONG")
        stop_loss = position.get("stop_loss", 0.0)
        entry_bar = position.get("entry_bar", 0)
        bars_held = bar_index - entry_bar

        # Stop-loss
        if pos_side == "LONG" and bar.low <= stop_loss:
            return ExitSignal(bar_index=bar_index, exit_price=stop_loss, reason="stop_loss")
        if pos_side == "SHORT" and bar.high >= stop_loss:
            return ExitSignal(bar_index=bar_index, exit_price=stop_loss, reason="stop_loss")

        # Time stop
        if bars_held >= self.time_stop_bars:
            return ExitSignal(bar_index=bar_index, exit_price=bar.close, reason="time_stop")

        # Signal exit: price returns to SMA
        if bar_index < len(sma) and not math.isnan(sma[bar_index]):
            if pos_side == "LONG" and bar.close >= sma[bar_index]:
                return ExitSignal(bar_index=bar_index, exit_price=bar.close, reason="signal_exit")
            if pos_side == "SHORT" and bar.close <= sma[bar_index]:
                return ExitSignal(bar_index=bar_index, exit_price=bar.close, reason="signal_exit")

        return None

    def get_default_doctrine(self, pair: str, timeframe: str) -> dict[str, Any]:
        """Return mean-reversion default doctrine."""
        return {
            "name": f"mean_reversion_{pair.lower()}_{timeframe.lower()}",
            "version": "1.0.0",
            "description": f"Bollinger Band mean reversion for {pair} on {timeframe}",
            "concept": (
                "Enters when price touches Bollinger Band extremes with RSI "
                "confirmation. Exits when price returns to the SMA midline. "
                "Works best in ranging/mean-reverting market regimes."
            ),
            "setup_type": "mean_reversion",
            "entry_signal": "bollinger_touch",
            "confirmations": ["rsi_extreme"],
            "exit_method": "sma_return",
            "data": {
                "pair": pair,
                "timeframes": [timeframe],
                "min_bars": 3000,
            },
            "mutable_params": {
                "sma_period": {"default": 20, "min": 10, "max": 50, "step": 1},
                "bb_std_mult": {"default": 2.0, "min": 1.5, "max": 3.0, "step": 0.1},
                "rsi_period": {"default": 14, "min": 7, "max": 21, "step": 1},
                "rsi_oversold": {"default": 30.0, "min": 20.0, "max": 40.0, "step": 1.0},
                "rsi_overbought": {"default": 70.0, "min": 60.0, "max": 80.0, "step": 1.0},
                "time_stop_bars": {"default": 30, "min": 10, "max": 60, "step": 1},
            },
            "immutable_params": {
                "pip_buffer": 0.0001,
            },
            "filters_mandatory": ["bollinger_touch", "rsi_extreme"],
            "filters_optional": [],
            "execution": {
                "spread_pips": 1.0,
                "slippage_pips": 0.0,
                "commission_per_lot": 0.0,
                "leverage": 100,
                "fill_policy": "next_bar_open",
            },
            "validation": {
                "min_trades": 50,
                "min_months": 6,
                "max_drawdown_pct": 12.0,
                "min_profit_factor": 1.2,
                "min_sharpe": 0.4,
            },
        }

    def get_parameter_bounds(self) -> dict[str, tuple[float, float]]:
        """Return mean-reversion parameter bounds."""
        return {
            "sma_period": (10, 50),
            "bb_std_mult": (1.5, 3.0),
            "rsi_period": (7, 21),
            "rsi_oversold": (20.0, 40.0),
            "rsi_overbought": (60.0, 80.0),
            "time_stop_bars": (10, 60),
            "warmup_bars": (15, 55),
        }
