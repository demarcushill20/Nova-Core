"""Dual EMA crossover with ADX filter trend-following strategy.

Template strategy: enters on fast/slow EMA crossover confirmed by ADX above
a threshold, exits on reverse crossover or ADX dropping below threshold.
"""

from __future__ import annotations

import math
from typing import Any

from novatrade.backtest.engine import compute_adx, compute_atr, compute_ema
from novatrade.models import Candle
from novatrade.strategies.base import BaseStrategy, EntrySignal, ExitSignal


class TrendFollowingStrategy(BaseStrategy):
    """Dual EMA crossover with ADX filter.

    Entry:
    - Fast EMA crosses above slow EMA + ADX above threshold -> LONG
    - Fast EMA crosses below slow EMA + ADX above threshold -> SHORT

    Exit:
    - Reverse crossover (fast EMA crosses opposite direction)
    - ADX drops below threshold (trend exhaustion)
    - Time stop after N bars
    - ATR-based trailing stop
    """

    name = "trend_following"
    family = "trend_following"

    def __init__(
        self,
        fast_ema_period: int = 12,
        slow_ema_period: int = 26,
        adx_period: int = 14,
        adx_threshold: float = 25.0,
        atr_period: int = 14,
        trail_atr_mult: float = 2.0,
        time_stop_bars: int = 60,
        warmup_bars: int = 30,
        pip_buffer: float = 0.0001,
    ) -> None:
        self.fast_ema_period = fast_ema_period
        self.slow_ema_period = slow_ema_period
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.atr_period = atr_period
        self.trail_atr_mult = trail_atr_mult
        self.time_stop_bars = time_stop_bars
        self.warmup_bars = warmup_bars
        self.pip_buffer = pip_buffer

    def compute_indicators(self, candles: list[Candle]) -> dict[str, list[float]]:
        """Compute fast EMA, slow EMA, ADX, and ATR."""
        if not candles:
            return {"fast_ema": [], "slow_ema": [], "adx": [], "atr": []}

        closes = [c.close for c in candles]
        return {
            "fast_ema": compute_ema(closes, self.fast_ema_period),
            "slow_ema": compute_ema(closes, self.slow_ema_period),
            "adx": compute_adx(candles, self.adx_period),
            "atr": compute_atr(candles, self.atr_period),
        }

    def generate_signals(self, candles: list[Candle], indicators: dict[str, list[float]]) -> list[EntrySignal]:
        """Scan all bars for EMA crossover entry signals."""
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
        """Check EMA crossover + ADX filter at bar_index.

        A crossover is detected when fast EMA is above (below) slow EMA on
        the current bar but was below (above) on the previous bar.
        """
        i = bar_index
        if i < self.warmup_bars or i < 1 or i >= len(candles):
            return None

        bar = candles[i]
        fast = indicators.get("fast_ema", [])
        slow = indicators.get("slow_ema", [])
        adx = indicators.get("adx", [])
        atr = indicators.get("atr", [])

        if i >= len(fast) or i >= len(slow) or i >= len(adx) or i >= len(atr):
            return None
        if any(math.isnan(v) for v in [fast[i], slow[i], fast[i - 1], slow[i - 1], adx[i], atr[i]]):
            return None
        if atr[i] <= 0:
            return None

        # ADX filter: trend must be strong enough
        if adx[i] < self.adx_threshold:
            return None

        # Bullish crossover: fast crosses above slow
        if fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]:
            entry_price = bar.close + self.pip_buffer
            stop_loss = bar.close - self.trail_atr_mult * atr[i]
            return EntrySignal(
                bar_index=i,
                side="LONG",
                entry_price=entry_price,
                stop_loss=stop_loss,
                metadata={
                    "fast_ema": fast[i],
                    "slow_ema": slow[i],
                    "adx": adx[i],
                    "atr": atr[i],
                },
            )

        # Bearish crossover: fast crosses below slow
        if fast[i] < slow[i] and fast[i - 1] >= slow[i - 1]:
            entry_price = bar.close - self.pip_buffer
            stop_loss = bar.close + self.trail_atr_mult * atr[i]
            return EntrySignal(
                bar_index=i,
                side="SHORT",
                entry_price=entry_price,
                stop_loss=stop_loss,
                metadata={
                    "fast_ema": fast[i],
                    "slow_ema": slow[i],
                    "adx": adx[i],
                    "atr": atr[i],
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
        """Check trend-following exit: reverse crossover, ADX drop, trailing, time stop."""
        if position is None or bar_index >= len(candles) or bar_index < 1:
            return None

        bar = candles[bar_index]
        fast = indicators.get("fast_ema", [])
        slow = indicators.get("slow_ema", [])
        adx = indicators.get("adx", [])
        atr = indicators.get("atr", [])

        pos_side = position.get("side", "LONG")
        current_stop = position.get("current_stop", position.get("stop_loss", 0.0))
        best_close = position.get("best_close", bar.close)
        entry_bar = position.get("entry_bar", 0)
        bars_held = bar_index - entry_bar

        # Stop-loss / trailing stop hit
        if pos_side == "LONG" and bar.low <= current_stop:
            return ExitSignal(bar_index=bar_index, exit_price=current_stop, reason="stop_loss")
        if pos_side == "SHORT" and bar.high >= current_stop:
            return ExitSignal(bar_index=bar_index, exit_price=current_stop, reason="stop_loss")

        # Time stop
        if bars_held >= self.time_stop_bars:
            return ExitSignal(bar_index=bar_index, exit_price=bar.close, reason="time_stop")

        # Signal-based exits (require valid indicators)
        indicators_valid = (
            bar_index < len(fast)
            and bar_index < len(slow)
            and bar_index < len(adx)
            and not any(
                math.isnan(v)
                for v in [
                    fast[bar_index],
                    slow[bar_index],
                    fast[bar_index - 1],
                    slow[bar_index - 1],
                    adx[bar_index],
                ]
            )
        )
        if indicators_valid:
            # ADX drops below threshold -- trend exhaustion
            if adx[bar_index] < self.adx_threshold:
                return ExitSignal(
                    bar_index=bar_index,
                    exit_price=bar.close,
                    reason="signal_exit",
                    metadata={"reason_detail": "adx_exhaustion", "adx": adx[bar_index]},
                )

            # Reverse crossover
            bi = bar_index
            if pos_side == "LONG" and fast[bi] < slow[bi] and fast[bi - 1] >= slow[bi - 1]:
                return ExitSignal(
                    bar_index=bi,
                    exit_price=bar.close,
                    reason="signal_exit",
                    metadata={"reason_detail": "reverse_crossover"},
                )
            if pos_side == "SHORT" and fast[bi] > slow[bi] and fast[bi - 1] <= slow[bi - 1]:
                return ExitSignal(
                    bar_index=bi,
                    exit_price=bar.close,
                    reason="signal_exit",
                    metadata={"reason_detail": "reverse_crossover"},
                )

        # Trailing stop update and check
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
        """Return trend-following default doctrine."""
        return {
            "name": f"trend_following_{pair.lower()}_{timeframe.lower()}",
            "version": "1.0.0",
            "description": f"Dual EMA crossover with ADX filter for {pair} on {timeframe}",
            "concept": (
                "Enters on fast/slow EMA crossover confirmed by ADX above threshold. "
                "Captures sustained directional moves. Exits on reverse crossover, "
                "ADX exhaustion, or ATR trailing stop."
            ),
            "setup_type": "trend_following",
            "entry_signal": "ema_crossover",
            "confirmations": ["adx_filter"],
            "exit_method": "reverse_crossover_or_atr_trail",
            "data": {
                "pair": pair,
                "timeframes": [timeframe],
                "min_bars": 3000,
            },
            "mutable_params": {
                "fast_ema_period": {"default": 12, "min": 5, "max": 20, "step": 1},
                "slow_ema_period": {"default": 26, "min": 15, "max": 50, "step": 1},
                "adx_period": {"default": 14, "min": 7, "max": 21, "step": 1},
                "adx_threshold": {"default": 25.0, "min": 15.0, "max": 35.0, "step": 0.5},
                "atr_period": {"default": 14, "min": 7, "max": 21, "step": 1},
                "trail_atr_mult": {"default": 2.0, "min": 1.0, "max": 4.0, "step": 0.1},
                "time_stop_bars": {"default": 60, "min": 20, "max": 120, "step": 1},
            },
            "immutable_params": {
                "pip_buffer": 0.0001,
            },
            "filters_mandatory": ["ema_crossover", "adx_filter"],
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
        """Return trend-following parameter bounds."""
        return {
            "fast_ema_period": (5, 20),
            "slow_ema_period": (15, 50),
            "adx_period": (7, 21),
            "adx_threshold": (15.0, 35.0),
            "atr_period": (7, 21),
            "trail_atr_mult": (1.0, 4.0),
            "time_stop_bars": (20, 120),
            "warmup_bars": (20, 55),
        }
