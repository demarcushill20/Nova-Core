"""Donchian Channel breakout strategy.

Template strategy: enters on price breakout above/below Donchian Channel
(highest high / lowest low over N bars), exits via ATR-based trailing stop
or opposite channel touch.
"""

from __future__ import annotations

import math
from typing import Any

from novatrade.backtest.engine import compute_atr
from novatrade.models import Candle
from novatrade.strategies.base import BaseStrategy, EntrySignal, ExitSignal

# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------


def _donchian_upper(candles: list[Candle], period: int) -> list[float]:
    """Highest high over the last `period` bars. NaN-padded for warmup."""
    n = len(candles)
    result = [float("nan")] * n
    for i in range(period - 1, n):
        result[i] = max(candles[j].high for j in range(i - period + 1, i + 1))
    return result


def _donchian_lower(candles: list[Candle], period: int) -> list[float]:
    """Lowest low over the last `period` bars. NaN-padded for warmup."""
    n = len(candles)
    result = [float("nan")] * n
    for i in range(period - 1, n):
        result[i] = min(candles[j].low for j in range(i - period + 1, i + 1))
    return result


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class BreakoutStrategy(BaseStrategy):
    """Donchian Channel breakout strategy.

    Entry:
    - Price breaks above the upper channel (highest high of N bars) -> LONG
    - Price breaks below the lower channel (lowest low of N bars) -> SHORT

    Exit:
    - ATR-based trailing stop
    - Opposite channel touch
    - Time stop after N bars
    """

    name = "breakout"
    family = "breakout"

    def __init__(
        self,
        channel_period: int = 20,
        atr_period: int = 14,
        trail_atr_mult: float = 2.0,
        time_stop_bars: int = 50,
        warmup_bars: int = 25,
        pip_buffer: float = 0.0001,
    ) -> None:
        self.channel_period = channel_period
        self.atr_period = atr_period
        self.trail_atr_mult = trail_atr_mult
        self.time_stop_bars = time_stop_bars
        self.warmup_bars = warmup_bars
        self.pip_buffer = pip_buffer

    def compute_indicators(self, candles: list[Candle]) -> dict[str, list[float]]:
        """Compute Donchian Channel upper/lower and ATR."""
        if not candles:
            return {"upper_channel": [], "lower_channel": [], "atr": []}

        return {
            "upper_channel": _donchian_upper(candles, self.channel_period),
            "lower_channel": _donchian_lower(candles, self.channel_period),
            "atr": compute_atr(candles, self.atr_period),
        }

    def generate_signals(self, candles: list[Candle], indicators: dict[str, list[float]]) -> list[EntrySignal]:
        """Scan all bars for breakout entry signals."""
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
        """Check Donchian Channel breakout at bar_index.

        A breakout is detected when the current bar's close exceeds the
        *previous* bar's channel boundary. This avoids look-ahead bias --
        the channel value at bar_index includes the current bar's data, so
        we compare against bar_index - 1.
        """
        i = bar_index
        if i < self.warmup_bars or i >= len(candles) or i < 1:
            return None

        bar = candles[i]
        upper = indicators.get("upper_channel", [])
        lower = indicators.get("lower_channel", [])
        atr = indicators.get("atr", [])

        prev = i - 1
        if prev >= len(upper) or prev >= len(lower) or i >= len(atr):
            return None
        if math.isnan(upper[prev]) or math.isnan(lower[prev]) or math.isnan(atr[i]):
            return None
        if atr[i] <= 0:
            return None

        # Long breakout: close breaks above previous upper channel
        if bar.close > upper[prev]:
            entry_price = bar.close + self.pip_buffer
            stop_loss = bar.close - self.trail_atr_mult * atr[i]
            return EntrySignal(
                bar_index=i,
                side="LONG",
                entry_price=entry_price,
                stop_loss=stop_loss,
                metadata={
                    "upper_channel": upper[prev],
                    "atr": atr[i],
                    "breakout_distance": bar.close - upper[prev],
                },
            )

        # Short breakout: close breaks below previous lower channel
        if bar.close < lower[prev]:
            entry_price = bar.close - self.pip_buffer
            stop_loss = bar.close + self.trail_atr_mult * atr[i]
            return EntrySignal(
                bar_index=i,
                side="SHORT",
                entry_price=entry_price,
                stop_loss=stop_loss,
                metadata={
                    "lower_channel": lower[prev],
                    "atr": atr[i],
                    "breakout_distance": lower[prev] - bar.close,
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
        """Check breakout exit: trailing ATR stop, opposite channel, or time stop."""
        if position is None or bar_index >= len(candles):
            return None

        bar = candles[bar_index]
        atr = indicators.get("atr", [])
        upper = indicators.get("upper_channel", [])
        lower = indicators.get("lower_channel", [])

        pos_side = position.get("side", "LONG")
        current_stop = position.get("current_stop", position.get("stop_loss", 0.0))
        best_close = position.get("best_close", bar.close)
        entry_bar = position.get("entry_bar", 0)
        bars_held = bar_index - entry_bar

        # Stop-loss / trailing stop
        if pos_side == "LONG" and bar.low <= current_stop:
            return ExitSignal(bar_index=bar_index, exit_price=current_stop, reason="stop_loss")
        if pos_side == "SHORT" and bar.high >= current_stop:
            return ExitSignal(bar_index=bar_index, exit_price=current_stop, reason="stop_loss")

        # Time stop
        if bars_held >= self.time_stop_bars:
            return ExitSignal(bar_index=bar_index, exit_price=bar.close, reason="time_stop")

        # Opposite channel touch
        channels_valid = (
            bar_index < len(lower)
            and bar_index < len(upper)
            and not math.isnan(lower[bar_index])
            and not math.isnan(upper[bar_index])
        )
        if channels_valid:
            if pos_side == "LONG" and bar.close <= lower[bar_index]:
                return ExitSignal(bar_index=bar_index, exit_price=bar.close, reason="signal_exit")
            if pos_side == "SHORT" and bar.close >= upper[bar_index]:
                return ExitSignal(bar_index=bar_index, exit_price=bar.close, reason="signal_exit")

        # Trailing stop update
        if bar_index < len(atr) and not math.isnan(atr[bar_index]) and atr[bar_index] > 0:
            atr_val = atr[bar_index]
            if pos_side == "LONG":
                new_best = max(best_close, bar.close)
                new_trail = new_best - self.trail_atr_mult * atr_val
                if new_trail > current_stop and bar.low <= new_trail:
                    return ExitSignal(bar_index=bar_index, exit_price=new_trail, reason="trailing_stop")
            else:
                new_best = min(best_close, bar.close)
                new_trail = new_best + self.trail_atr_mult * atr_val
                if new_trail < current_stop and bar.high >= new_trail:
                    return ExitSignal(bar_index=bar_index, exit_price=new_trail, reason="trailing_stop")

        return None

    def get_default_doctrine(self, pair: str, timeframe: str) -> dict[str, Any]:
        """Return breakout default doctrine."""
        return {
            "name": f"breakout_{pair.lower()}_{timeframe.lower()}",
            "version": "1.0.0",
            "description": f"Donchian Channel breakout for {pair} on {timeframe}",
            "concept": (
                "Enters on Donchian Channel breakout (highest high / lowest low "
                "over N bars). Exits via ATR trailing stop or opposite channel "
                "touch. Captures momentum continuation in trending markets."
            ),
            "setup_type": "breakout",
            "entry_signal": "donchian_breakout",
            "confirmations": ["atr_filter"],
            "exit_method": "atr_trailing",
            "data": {
                "pair": pair,
                "timeframes": [timeframe],
                "min_bars": 3000,
            },
            "mutable_params": {
                "channel_period": {"default": 20, "min": 10, "max": 55, "step": 1},
                "atr_period": {"default": 14, "min": 7, "max": 21, "step": 1},
                "trail_atr_mult": {"default": 2.0, "min": 1.0, "max": 4.0, "step": 0.1},
                "time_stop_bars": {"default": 50, "min": 20, "max": 100, "step": 1},
            },
            "immutable_params": {
                "pip_buffer": 0.0001,
            },
            "filters_mandatory": ["donchian_breakout"],
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
                "max_drawdown_pct": 18.0,
                "min_profit_factor": 1.2,
                "min_sharpe": 0.4,
            },
        }

    def get_parameter_bounds(self) -> dict[str, tuple[float, float]]:
        """Return breakout parameter bounds."""
        return {
            "channel_period": (10, 55),
            "atr_period": (7, 21),
            "trail_atr_mult": (1.0, 4.0),
            "time_stop_bars": (20, 100),
            "warmup_bars": (15, 60),
        }
