"""MACD divergence reversal strategy (task-1030 spec).

Mechanical implementation of the divergence spec in
``TASKS/1030_Price_low_2_is_lower_than_price_low_1_.md``:

Bullish divergence (LONG):
    price_low_2  < price_low_1      (price makes a lower low)
    macd_low_2   > macd_low_1       (MACD makes a higher low)

Bearish divergence (SHORT):
    price_high_2 > price_high_1     (price makes a higher high)
    macd_high_2  < macd_high_1      (MACD makes a lower high)

Swing pivots (mechanical, spec defaults):
    pivot_left  = 3, pivot_right = 3
    A swing low  is confirmed when the candle low  is <= the lows  of the 3
    candles before AND the 3 candles after it (symmetric for swing highs).
    Because confirmation needs `pivot_right` FUTURE candles, the pivot is only
    actionable `pivot_right` bars later -- so the signal bar is the
    confirmation bar (pivot_index + pivot_right), never the pivot candle. No
    lookahead: the bot is not allowed to "know" the pivot at the low candle.

    minimum_bars_between_pivots = 5   (reject pivots too close together)
    maximum_bars_between_pivots = 60  (reject stale prior pivot)

MACD zero-line rule (strict, spec section 7):
    LONG  : MACD line AND signal line < 0 for every candle from pivot_low_1
            through the entry trigger (never touch / cross 0 during the setup).
    SHORT : MACD line AND signal line > 0 for the symmetric window.

Exit (not pinned by the 1030 chunk -> standard reversal management):
    stop_loss   = just beyond the confirming pivot (low_2 - buffer for LONG,
                  high_2 + buffer for SHORT)
    take_profit = tp_rr * stop-distance (reconstructed in check_exit, like the
                  validated ThreeEmaStochRsi runner)
    optional time stop.
"""

from __future__ import annotations

import math
from typing import Any

from novatrade.backtest.engine import compute_ema
from novatrade.models import Candle
from novatrade.strategies.base import BaseStrategy, EntrySignal, ExitSignal

# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------


def compute_macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float], list[float], list[float]]:
    """Standard MACD (line, signal, histogram), same length as `closes`."""
    fast_ema = compute_ema(closes, fast)
    slow_ema = compute_ema(closes, slow)
    macd_line = [
        (f - s) if not (math.isnan(f) or math.isnan(s)) else float("nan")
        for f, s in zip(fast_ema, slow_ema, strict=False)
    ]
    # Signal line is an EMA of the MACD line; warm it up only on valid values.
    valid = [m for m in macd_line if not math.isnan(m)]
    sig_valid = compute_ema(valid, signal)
    signal_line = [float("nan")] * len(macd_line)
    j = 0
    for i, m in enumerate(macd_line):
        if not math.isnan(m):
            signal_line[i] = sig_valid[j]
            j += 1
    hist = [
        (m - s) if not (math.isnan(m) or math.isnan(s)) else float("nan")
        for m, s in zip(macd_line, signal_line, strict=False)
    ]
    return macd_line, signal_line, hist


def _swing_lows(candles: list[Candle], left: int, right: int) -> list[int]:
    """Indices of confirmed swing-low pivots (low <= `left` before & `right` after)."""
    n = len(candles)
    out: list[int] = []
    for i in range(left, n - right):
        lo = candles[i].low
        if all(lo <= candles[i - k].low for k in range(1, left + 1)) and all(
            lo <= candles[i + k].low for k in range(1, right + 1)
        ):
            out.append(i)
    return out


def _swing_highs(candles: list[Candle], left: int, right: int) -> list[int]:
    """Indices of confirmed swing-high pivots (high >= `left` before & `right` after)."""
    n = len(candles)
    out: list[int] = []
    for i in range(left, n - right):
        hi = candles[i].high
        if all(hi >= candles[i - k].high for k in range(1, left + 1)) and all(
            hi >= candles[i + k].high for k in range(1, right + 1)
        ):
            out.append(i)
    return out


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class MacdDivergenceStrategy(BaseStrategy):
    """MACD bullish/bearish divergence reversal (task-1030)."""

    name = "macd_divergence"
    family = "reversal"

    def __init__(
        self,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        pivot_left: int = 3,
        pivot_right: int = 3,
        min_bars_between: int = 5,
        max_bars_between: int = 60,
        tp_rr: float = 2.0,
        time_stop_bars: int = 0,
        pip_buffer: float = 0.0002,
        require_zero_line: bool = True,
    ) -> None:
        self.fast = fast
        self.slow = slow
        self.signal = signal
        self.pivot_left = pivot_left
        self.pivot_right = pivot_right
        self.min_bars_between = min_bars_between
        self.max_bars_between = max_bars_between
        self.tp_rr = tp_rr
        self.time_stop_bars = time_stop_bars
        self.pip_buffer = pip_buffer
        self.require_zero_line = require_zero_line
        # Caches populated by compute_indicators.
        self._low_pivots: list[int] = []
        self._high_pivots: list[int] = []
        # Map: confirmation_bar -> pivot_index, for O(1) per-bar entry checks.
        self._low_conf: dict[int, int] = {}
        self._high_conf: dict[int, int] = {}

    def compute_indicators(self, candles: list[Candle]) -> dict[str, list[float]]:
        if not candles:
            return {"macd": [], "signal": [], "hist": []}
        closes = [c.close for c in candles]
        macd_line, signal_line, hist = compute_macd(closes, self.fast, self.slow, self.signal)
        self._low_pivots = _swing_lows(candles, self.pivot_left, self.pivot_right)
        self._high_pivots = _swing_highs(candles, self.pivot_left, self.pivot_right)
        self._low_conf = {p + self.pivot_right: p for p in self._low_pivots}
        self._high_conf = {p + self.pivot_right: p for p in self._high_pivots}
        return {"macd": macd_line, "signal": signal_line, "hist": hist}

    def generate_signals(self, candles: list[Candle], indicators: dict[str, list[float]]) -> list[EntrySignal]:
        signals: list[EntrySignal] = []
        for i in range(len(candles)):
            sig = self.check_entry(i, candles, indicators)
            if sig is not None:
                signals.append(sig)
        return signals

    def _zero_line_ok(self, side: str, start: int, end: int, macd: list[float], signal: list[float]) -> bool:
        """True if MACD AND signal stay strictly on the correct side of 0 over [start, end]."""
        for k in range(start, end + 1):
            m, s = macd[k], signal[k]
            if math.isnan(m) or math.isnan(s):
                return False
            if side == "LONG" and not (m < 0.0 and s < 0.0):
                return False
            if side == "SHORT" and not (m > 0.0 and s > 0.0):
                return False
        return True

    def check_entry(
        self,
        bar_index: int,
        candles: list[Candle],
        indicators: dict[str, list[float]],
        h4_candles: list[Candle] | None = None,
    ) -> EntrySignal | None:
        i = bar_index
        macd = indicators.get("macd", [])
        signal = indicators.get("signal", [])
        if i >= len(candles) or i >= len(macd):
            return None

        # --- Bullish divergence (LONG): a swing low confirms on this bar -----
        p2 = self._low_conf.get(i)
        if p2 is not None:
            # Most recent prior confirmed low pivot within the bar-gap window.
            p1 = self._prior_pivot(self._low_pivots, p2)
            if p1 is not None:
                gap = p2 - p1
                if self.min_bars_between <= gap <= self.max_bars_between:
                    price_low_1, price_low_2 = candles[p1].low, candles[p2].low
                    macd_low_1, macd_low_2 = macd[p1], macd[p2]
                    if (
                        not math.isnan(macd_low_1)
                        and not math.isnan(macd_low_2)
                        and price_low_2 < price_low_1
                        and macd_low_2 > macd_low_1
                        and (not self.require_zero_line or self._zero_line_ok("LONG", p1, i, macd, signal))
                    ):
                        bar = candles[i]
                        stop_loss = price_low_2 - self.pip_buffer
                        if stop_loss < bar.close:
                            return EntrySignal(
                                bar_index=i,
                                side="LONG",
                                entry_price=bar.close,
                                stop_loss=stop_loss,
                                metadata={
                                    "pivot_low_1": p1,
                                    "pivot_low_2": p2,
                                    "macd_low_1": macd_low_1,
                                    "macd_low_2": macd_low_2,
                                },
                            )

        # --- Bearish divergence (SHORT): a swing high confirms on this bar ---
        p2h = self._high_conf.get(i)
        if p2h is not None:
            p1h = self._prior_pivot(self._high_pivots, p2h)
            if p1h is not None:
                gap = p2h - p1h
                if self.min_bars_between <= gap <= self.max_bars_between:
                    price_high_1, price_high_2 = candles[p1h].high, candles[p2h].high
                    macd_high_1, macd_high_2 = macd[p1h], macd[p2h]
                    if (
                        not math.isnan(macd_high_1)
                        and not math.isnan(macd_high_2)
                        and price_high_2 > price_high_1
                        and macd_high_2 < macd_high_1
                        and (not self.require_zero_line or self._zero_line_ok("SHORT", p1h, i, macd, signal))
                    ):
                        bar = candles[i]
                        stop_loss = price_high_2 + self.pip_buffer
                        if stop_loss > bar.close:
                            return EntrySignal(
                                bar_index=i,
                                side="SHORT",
                                entry_price=bar.close,
                                stop_loss=stop_loss,
                                metadata={
                                    "pivot_high_1": p1h,
                                    "pivot_high_2": p2h,
                                    "macd_high_1": macd_high_1,
                                    "macd_high_2": macd_high_2,
                                },
                            )
        return None

    @staticmethod
    def _prior_pivot(pivots: list[int], current: int) -> int | None:
        """Return the pivot index immediately preceding `current` in the sorted list."""
        prev = None
        for p in pivots:
            if p >= current:
                break
            prev = p
        return prev

    def check_exit(
        self,
        bar_index: int,
        candles: list[Candle],
        indicators: dict[str, list[float]],
        position: dict[str, Any] | None = None,
    ) -> ExitSignal | None:
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

    def get_default_doctrine(self, pair: str, timeframe: str) -> dict[str, Any]:
        return {
            "name": f"macd_divergence_{pair.lower()}_{timeframe.lower()}",
            "version": "1.0.0",
            "description": f"MACD divergence reversal for {pair} on {timeframe}",
            "setup_type": "reversal",
            "entry_signal": "macd_divergence",
            "exit_method": "pivot_stop_and_rr_target",
        }

    def get_parameter_bounds(self) -> dict[str, tuple[float, float]]:
        return {
            "pivot_left": (2, 6),
            "pivot_right": (2, 6),
            "min_bars_between": (3, 15),
            "max_bars_between": (30, 120),
            "tp_rr": (1.0, 4.0),
        }
