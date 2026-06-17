"""MTF-EMA + MACD-divergence FULL strategy (task-1031 spec).

This is the *union* of every filter listed in the task-1031 written review --
the most restrictive reading of the system. It combines the MTF-EMA bias of
task-1029 with the strict zero-line + pivot-gap divergence of task-1030, and
adds the two filters that neither prior runner enforced together: the MACD
**histogram gap/reset** between the divergent pivots, and the **MACD cross**
as the actual entry trigger.

Formal long condition (task-1031):

    long_bias            = ema_15m_50 > ema_1h_50          # 15m EMA OVER 1h EMA
    bullish_divergence   = price_low_2 < price_low_1
                           and macd_low_2 > macd_low_1
    macd_below_zero      = macd_line < 0 and signal_line < 0   (held p1..entry)
    bullish_hist_gap     = histogram resets >0 between the two divergent lows
    macd_cross_up        = prev_macd <= prev_signal and macd > signal  (trigger)

    long_signal = long_bias and bullish_divergence and macd_below_zero
                  and bullish_hist_gap and macd_cross_up

Short is the mirror (ema15 < ema1h, price HH + MACD LH, both lines > 0, hist
gap < 0, MACD cross-down).

Execution: signal forms on candle N close; the shared ``StrategyBacktesterAdapter``
fills at candle N+1 open (1-bar delay, no look-ahead). Stop is just beyond the
confirming swing pivot; target is ``tp_rr`` * stop-distance (default 2R).

The 15m / 1h EMAs are bucketed from the M5 feed and each M5 bar is assigned the
EMA of the *last completed* HTF bucket -> no look-ahead. Pivots are confirmed
``pivot_right`` bars after they form -> no look-ahead. The MACD cross and the
zero-line window are evaluated only on closed bars at-or-before the trigger.
"""

from __future__ import annotations

import bisect
import math
from typing import Any

from novatrade.models import Candle
from novatrade.strategies.base import BaseStrategy, EntrySignal, ExitSignal
from novatrade.strategies.macd_divergence import (
    _swing_highs,
    _swing_lows,
    compute_macd,
)


def _htf_ema(candles: list[Candle], bucket_seconds: int, period: int) -> list[float]:
    """EMA(period) of higher-timeframe bucket closes, mapped onto each base bar.

    Each base bar receives the EMA of the LAST COMPLETED bucket (strictly before
    the current, still-forming bucket) -> no look-ahead into the live HTF candle.
    """
    n = len(candles)
    out = [float("nan")] * n
    k = 2.0 / (period + 1.0)
    ema: float | None = None
    cur_bucket: int | None = None
    cur_close = 0.0
    last_completed = float("nan")
    for i, c in enumerate(candles):
        b = int(c.timestamp // bucket_seconds)
        if cur_bucket is None:
            cur_bucket, cur_close = b, c.close
        elif b != cur_bucket:
            ema = cur_close if ema is None else cur_close * k + ema * (1.0 - k)
            last_completed = ema
            cur_bucket, cur_close = b, c.close
        else:
            cur_close = c.close
        out[i] = last_completed
    return out


class MacdDivergenceMtfFullStrategy(BaseStrategy):
    """Full task-1031 union: MTF-EMA bias + divergence + zero-line + hist gap + cross."""

    name = "macd_divergence_mtf_full"
    family = "reversal"

    def __init__(
        self,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        ema_period: int = 50,
        pivot_left: int = 3,
        pivot_right: int = 3,
        min_bars_between: int = 5,
        max_bars_between: int = 60,
        m15_seconds: int = 15 * 60,
        h1_seconds: int = 60 * 60,
        tp_rr: float = 2.0,
        time_stop_bars: int = 0,
        pip_buffer: float = 0.0002,
        require_zero_line: bool = True,
        require_bias: bool = True,
        require_hist_gap: bool = True,
        require_cross: bool = True,
    ) -> None:
        self.fast = fast
        self.slow = slow
        self.signal = signal
        self.ema_period = ema_period
        self.pivot_left = pivot_left
        self.pivot_right = pivot_right
        self.min_bars_between = min_bars_between
        self.max_bars_between = max_bars_between
        self.m15_seconds = m15_seconds
        self.h1_seconds = h1_seconds
        self.tp_rr = tp_rr
        self.time_stop_bars = time_stop_bars
        self.pip_buffer = pip_buffer
        self.require_zero_line = require_zero_line
        self.require_bias = require_bias
        self.require_hist_gap = require_hist_gap
        self.require_cross = require_cross
        self._low_pivots: list[int] = []
        self._high_pivots: list[int] = []

    # -- indicators --------------------------------------------------------
    def compute_indicators(self, candles: list[Candle]) -> dict[str, list[float]]:
        if not candles:
            return {"macd": [], "signal": [], "hist": [], "ema15": [], "ema1h": []}
        closes = [c.close for c in candles]
        macd_line, signal_line, hist = compute_macd(closes, self.fast, self.slow, self.signal)
        ema15 = _htf_ema(candles, self.m15_seconds, self.ema_period)
        ema1h = _htf_ema(candles, self.h1_seconds, self.ema_period)
        self._low_pivots = _swing_lows(candles, self.pivot_left, self.pivot_right)
        self._high_pivots = _swing_highs(candles, self.pivot_left, self.pivot_right)
        return {
            "macd": macd_line,
            "signal": signal_line,
            "hist": hist,
            "ema15": ema15,
            "ema1h": ema1h,
        }

    def generate_signals(self, candles: list[Candle], indicators: dict[str, list[float]]) -> list[EntrySignal]:
        out: list[EntrySignal] = []
        for i in range(len(candles)):
            sig = self.check_entry(i, candles, indicators)
            if sig is not None:
                out.append(sig)
        return out

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _last_two_confirmed(pivots: list[int], confirm_offset: int, i: int) -> tuple[int, int] | None:
        """Return (p1, p2): the two most recent pivots CONFIRMED at or before bar i.

        ``pivots`` is sorted ascending, so the count of confirmed pivots (those
        with ``p + confirm_offset <= i``) is found in O(log n) via bisect. No
        look-ahead: a pivot is only usable once its confirmation bar has closed.
        """
        cnt = bisect.bisect_right(pivots, i - confirm_offset)
        if cnt < 2:
            return None
        return pivots[cnt - 2], pivots[cnt - 1]

    def _zero_line_ok(self, side: str, start: int, end: int, macd: list[float], signal: list[float]) -> bool:
        for k in range(start, end + 1):
            m, s = macd[k], signal[k]
            if math.isnan(m) or math.isnan(s):
                return False
            if side == "LONG" and not (m < 0.0 and s < 0.0):
                return False
            if side == "SHORT" and not (m > 0.0 and s > 0.0):
                return False
        return True

    @staticmethod
    def _cross_up(i: int, macd: list[float], signal: list[float]) -> bool:
        if i < 1:
            return False
        pm, ps, m, s = macd[i - 1], signal[i - 1], macd[i], signal[i]
        if any(math.isnan(x) for x in (pm, ps, m, s)):
            return False
        return pm <= ps and m > s

    @staticmethod
    def _cross_down(i: int, macd: list[float], signal: list[float]) -> bool:
        if i < 1:
            return False
        pm, ps, m, s = macd[i - 1], signal[i - 1], macd[i], signal[i]
        if any(math.isnan(x) for x in (pm, ps, m, s)):
            return False
        return pm >= ps and m < s

    # -- entry -------------------------------------------------------------
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
        hist = indicators.get("hist", [])
        ema15 = indicators.get("ema15", [])
        ema1h = indicators.get("ema1h", [])
        if i >= len(candles) or i >= len(macd) or i < 1:
            return None

        # --- LONG: trigger on MACD cross-up ------------------------------
        if not self.require_cross or self._cross_up(i, macd, signal):
            two = self._last_two_confirmed(self._low_pivots, self.pivot_right, i)
            bias_ok = (not self.require_bias) or (
                not math.isnan(ema15[i]) and not math.isnan(ema1h[i]) and ema15[i] > ema1h[i]
            )
            if two is not None and bias_ok:
                p1, p2 = two
                gap = p2 - p1
                if self.min_bars_between <= gap <= self.max_bars_between:
                    price_low_1, price_low_2 = candles[p1].low, candles[p2].low
                    macd_low_1, macd_low_2 = macd[p1], macd[p2]
                    diverge = (
                        not math.isnan(macd_low_1)
                        and not math.isnan(macd_low_2)
                        and price_low_2 < price_low_1
                        and macd_low_2 > macd_low_1
                    )
                    zero_ok = (not self.require_zero_line) or self._zero_line_ok("LONG", p1, i, macd, signal)
                    gap_vals = [hist[k] for k in range(p1 + 1, p2) if not math.isnan(hist[k])]
                    hist_ok = (not self.require_hist_gap) or (bool(gap_vals) and max(gap_vals) > 0.0)
                    if diverge and zero_ok and hist_ok:
                        bar = candles[i]
                        stop_loss = price_low_2 - self.pip_buffer
                        if stop_loss < bar.close:
                            return EntrySignal(
                                bar_index=i,
                                side="LONG",
                                entry_price=bar.close,
                                stop_loss=stop_loss,
                                metadata={"pivot_low_1": p1, "pivot_low_2": p2},
                            )

        # --- SHORT: trigger on MACD cross-down ---------------------------
        if not self.require_cross or self._cross_down(i, macd, signal):
            two = self._last_two_confirmed(self._high_pivots, self.pivot_right, i)
            bias_ok = (not self.require_bias) or (
                not math.isnan(ema15[i]) and not math.isnan(ema1h[i]) and ema15[i] < ema1h[i]
            )
            if two is not None and bias_ok:
                p1, p2 = two
                gap = p2 - p1
                if self.min_bars_between <= gap <= self.max_bars_between:
                    price_high_1, price_high_2 = candles[p1].high, candles[p2].high
                    macd_high_1, macd_high_2 = macd[p1], macd[p2]
                    diverge = (
                        not math.isnan(macd_high_1)
                        and not math.isnan(macd_high_2)
                        and price_high_2 > price_high_1
                        and macd_high_2 < macd_high_1
                    )
                    zero_ok = (not self.require_zero_line) or self._zero_line_ok("SHORT", p1, i, macd, signal)
                    gap_vals = [hist[k] for k in range(p1 + 1, p2) if not math.isnan(hist[k])]
                    hist_ok = (not self.require_hist_gap) or (bool(gap_vals) and min(gap_vals) < 0.0)
                    if diverge and zero_ok and hist_ok:
                        bar = candles[i]
                        stop_loss = price_high_2 + self.pip_buffer
                        if stop_loss > bar.close:
                            return EntrySignal(
                                bar_index=i,
                                side="SHORT",
                                entry_price=bar.close,
                                stop_loss=stop_loss,
                                metadata={"pivot_high_1": p1, "pivot_high_2": p2},
                            )
        return None

    # -- exit (pivot stop + RR target, same as task-1030) -----------------
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
            "name": f"macd_divergence_mtf_full_{pair.lower()}_{timeframe.lower()}",
            "version": "1.0.0",
            "description": f"Task-1031 full MTF-EMA + MACD divergence union for {pair} {timeframe}",
            "setup_type": "reversal",
            "entry_signal": "macd_divergence_mtf_full",
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
