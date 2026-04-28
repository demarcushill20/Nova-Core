"""IRB strategy backtesting engine.

Implements the complete IRB strategy state machine from strategy_spec.yaml v2.0.0
as a bar-by-bar Python simulation.  Consumes H1 + H4 OHLCV candle arrays and
produces CompletedTrade / PendingOrderRecord / SignalRecord lists suitable for
metrics computation.

Design:
- Pure Python, no external dependencies beyond stdlib
- Deterministic: same candle input → same output
- No side effects: does not touch the filesystem or network
- Mirrors the Pine Script logic verified in Phase 3 (134 rules)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from novatrade.backtest.environment import DEFAULT_ENVIRONMENT, BacktestEnvironment
from novatrade.backtest.metrics import (
    CompletedTrade,
    ExitReason,
    FilterRejection,
    OrderCancelReason,
    PendingOrderRecord,
    SignalRecord,
    TradeSide,
)
from novatrade.models import Candle

log = logging.getLogger("novatrade.backtest.engine")


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class StrategyState(Enum):
    FLAT = "FLAT"
    PENDING_LONG = "PENDING_LONG"
    PENDING_SHORT = "PENDING_SHORT"
    LONG = "LONG"
    SHORT = "SHORT"


# ---------------------------------------------------------------------------
# Internal tracking
# ---------------------------------------------------------------------------


@dataclass
class _PendingOrder:
    """Active pending stop order."""

    side: TradeSide
    entry_price: float
    stop_loss: float
    volume: float
    bar_placed: int
    irb_bar: int


@dataclass
class _OpenPosition:
    """Currently open position."""

    side: TradeSide
    entry_price: float
    stop_loss: float
    volume: float
    entry_bar: int
    current_stop: float = 0.0
    best_close: float = 0.0
    bars_held: int = 0
    breakeven_hit: bool = False  # v4: tracks if BE level was reached
    partial_taken: bool = False  # v5: tracks if partial profit was taken
    initial_stop: float = 0.0  # v5: original stop loss for R calculation
    initial_volume: float = 0.0  # v5: original volume before partial
    # v5 partial bookkeeping: cash already realised from a partial exit, folded
    # into the runner's CompletedTrade at _close_position time so each entry
    # produces exactly one trade record (matches Pine v5 output cardinality).
    partial_pnl_pips: float = 0.0
    partial_pnl_usd: float = 0.0
    # v5 stagnation guard: ATR at entry, peak favourable excursion in price units
    entry_atr: float = 0.0
    peak_fav: float = 0.0


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------


def compute_ema(closes: list[float], period: int) -> list[float]:
    """Compute EMA over a list of close prices.  Returns list of same length (NaN-padded)."""
    if not closes:
        return []
    ema = [float("nan")] * len(closes)
    k = 2.0 / (period + 1)
    ema[0] = closes[0]
    for i in range(1, len(closes)):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema


def compute_atr(candles: list[Candle], period: int) -> list[float]:
    """Compute ATR (Wilder smoothing) over candle list. Returns same-length list."""
    if not candles:
        return []
    n = len(candles)
    tr = [0.0] * n
    tr[0] = candles[0].high - candles[0].low
    for i in range(1, n):
        c = candles[i]
        prev_close = candles[i - 1].close
        tr[i] = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))

    atr = [float("nan")] * n
    if n >= period:
        atr[period - 1] = sum(tr[:period]) / period
        for i in range(period, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def compute_adx(candles: list[Candle], period: int) -> list[float]:
    """Compute ADX over candle list. Returns same-length list (NaN-padded)."""
    n = len(candles)
    if n < period + 1:
        return [float("nan")] * n

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    tr[0] = candles[0].high - candles[0].low

    for i in range(1, n):
        c = candles[i]
        prev = candles[i - 1]
        high_diff = c.high - prev.high
        low_diff = prev.low - c.low
        plus_dm[i] = max(high_diff, 0) if high_diff > low_diff else 0.0
        minus_dm[i] = max(low_diff, 0) if low_diff > high_diff else 0.0
        tr[i] = max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))

    # Wilder smoothing
    def smooth(vals: list[float], p: int) -> list[float]:
        s = [float("nan")] * len(vals)
        s[p] = sum(vals[1 : p + 1])
        for i in range(p + 1, len(vals)):
            s[i] = s[i - 1] - s[i - 1] / p + vals[i]
        return s

    smooth_tr = smooth(tr, period)
    smooth_plus = smooth(plus_dm, period)
    smooth_minus = smooth(minus_dm, period)

    dx = [float("nan")] * n
    for i in range(period, n):
        st = smooth_tr[i]
        if st == 0 or math.isnan(st):
            continue
        pdi = 100 * smooth_plus[i] / st
        mdi = 100 * smooth_minus[i] / st
        denom = pdi + mdi
        if denom > 0:
            dx[i] = 100 * abs(pdi - mdi) / denom

    # ADX = EMA of DX (Wilder smoothing)
    adx = [float("nan")] * n
    start = 2 * period - 1
    if start < n:
        valid_dx = [dx[i] for i in range(period, start + 1) if not math.isnan(dx[i])]
        if valid_dx:
            adx[start] = sum(valid_dx) / len(valid_dx)
            for i in range(start + 1, n):
                if not math.isnan(dx[i]) and not math.isnan(adx[i - 1]):
                    adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    return adx


def compute_bbw(closes: list[float], period: int = 20) -> list[float]:
    """Compute Bollinger Band Width (BBW) as (upper - lower) / middle.

    Low BBW indicates a ranging/compressed market (squeeze).
    Returns same-length list, NaN-padded for warmup.
    """
    n = len(closes)
    bbw = [float("nan")] * n
    if n < period:
        return bbw
    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        sma = sum(window) / period
        if sma <= 0:
            continue
        variance = sum((x - sma) ** 2 for x in window) / period
        std = variance**0.5
        bbw[i] = (2 * std) / sma  # (upper - lower) / middle = 4*std/sma simplified
    return bbw


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class BacktestResult:
    """Complete output of a backtest run."""

    trades: list[CompletedTrade] = field(default_factory=list)
    pending_orders: list[PendingOrderRecord] = field(default_factory=list)
    signals: list[SignalRecord] = field(default_factory=list)
    filter_rejections: FilterRejection = field(default_factory=FilterRejection)
    total_bars: int = 0
    final_equity: float = 0.0
    environment: BacktestEnvironment = field(default_factory=lambda: DEFAULT_ENVIRONMENT)


class IRBBacktester:
    """Bar-by-bar IRB strategy simulator.

    Usage::

        bt = IRBBacktester(env=BacktestEnvironment())
        result = bt.run(h1_candles, h4_candles)
        metrics = compute_metrics(result.trades, ...)
    """

    def __init__(self, env: BacktestEnvironment | None = None) -> None:
        self.env = env or DEFAULT_ENVIRONMENT
        self._state = StrategyState.FLAT
        self._pending: _PendingOrder | None = None
        self._position: _OpenPosition | None = None
        self._equity = self.env.initial_equity
        self._trade_counter = 0
        self._consecutive_losses = 0  # circuit breaker tracking
        self._trades_today = 0  # v5: daily trade counter
        self._last_flat_bar = -999  # v5: bar index when last went flat
        self._current_day_key: int = -1  # v5: day boundary tracker

        # Result accumulators
        self._trades: list[CompletedTrade] = []
        self._pending_orders: list[PendingOrderRecord] = []
        self._signals: list[SignalRecord] = []
        self._rejections = FilterRejection()

    def run(
        self,
        h1_candles: list[Candle],
        h4_candles: list[Candle],
    ) -> BacktestResult:
        """Run the backtest over H1 candles with H4 for MTF alignment.

        Args:
            h1_candles: List of H1 OHLCV candles in chronological order.
            h4_candles: List of H4 OHLCV candles in chronological order.
                The engine maps H1 bars to corresponding H4 bars by timestamp.

        Returns:
            BacktestResult with all trades, orders, signals.
        """
        n = len(h1_candles)
        if n == 0:
            return BacktestResult(total_bars=0, final_equity=self._equity, environment=self.env)

        # Pre-compute H1 indicators
        h1_closes = [c.close for c in h1_candles]
        ema_h1 = compute_ema(h1_closes, self.env.ema_period)
        atr_h1 = compute_atr(h1_candles, self.env.atr_period)
        adx_h1 = compute_adx(h1_candles, self.env.adx_period)

        # EMA fast/slow for stack filter and confirmation
        ema_fast = compute_ema(h1_closes, self.env.ema_fast_period) if self.env.ema_fast_period > 0 else ema_h1
        ema_slow = compute_ema(h1_closes, self.env.ema_slow_period) if self.env.ema_slow_period > 0 else ema_h1

        # EMA for trailing stop (if enabled)
        trail_ema = compute_ema(h1_closes, self.env.trail_ema_period) if self.env.trail_ema_period > 0 else []

        # ATR simple moving average for volatility filter (v4)
        atr_sma: list[float] = []
        if self.env.use_volatility_filter:
            ma_period = self.env.volatility_atr_ma_period
            atr_sma = [float("nan")] * n
            for j in range(ma_period - 1, n):
                vals = [atr_h1[k] for k in range(j - ma_period + 1, j + 1) if not math.isnan(atr_h1[k])]
                if len(vals) == ma_period:
                    atr_sma[j] = sum(vals) / ma_period

        # Pre-compute H4 EMA
        h4_closes = [c.close for c in h4_candles]
        ema_h4 = compute_ema(h4_closes, self.env.ema_period) if h4_candles else []

        # Build H1 → H4 index mapping (each H1 bar maps to the most recent H4 bar)
        h4_map = self._build_h4_map(h1_candles, h4_candles)

        # Bar-by-bar simulation
        for i in range(n):
            self._process_bar(
                i,
                h1_candles[i],
                h1_candles,
                ema_h1,
                atr_h1,
                adx_h1,
                ema_h4,
                h4_map,
                ema_fast,
                ema_slow,
                trail_ema,
                atr_sma,
            )

        # Close any remaining position at last bar close
        if self._position is not None:
            self._close_position(n - 1, h1_candles[-1].close, ExitReason.TIME_STOP)

        # Cancel any remaining pending order
        if self._pending is not None:
            self._cancel_pending(n - 1, OrderCancelReason.TRIGGER_WINDOW_EXPIRED)

        return BacktestResult(
            trades=self._trades,
            pending_orders=self._pending_orders,
            signals=self._signals,
            filter_rejections=self._rejections,
            total_bars=n,
            final_equity=self._equity,
            environment=self.env,
        )

    # ------------------------------------------------------------------
    # Bar processing
    # ------------------------------------------------------------------

    def _process_bar(
        self,
        i: int,
        bar: Candle,
        all_bars: list[Candle],
        ema_h1: list[float],
        atr_h1: list[float],
        adx_h1: list[float],
        ema_h4: list[float],
        h4_map: list[int],
        ema_fast: list[float] | None = None,
        ema_slow: list[float] | None = None,
        trail_ema: list[float] | None = None,
        atr_sma: list[float] | None = None,
    ) -> None:
        """Process a single H1 bar."""
        # Stash current-bar indicators so _check_pending_fill can stamp them
        # on the new position without changing the call signature.
        self._cur_atr = atr_h1[i] if i < len(atr_h1) and not math.isnan(atr_h1[i]) else 0.0
        self._cur_trail_ema = (
            trail_ema[i]
            if (
                trail_ema is not None
                and self.env.trail_ema_period > 0
                and i < len(trail_ema)
                and not math.isnan(trail_ema[i])
            )
            else float("nan")
        )
        self._detect_day_boundary(i, bar)

        # v5: re-validate pending orders against current conditions (BEFORE fill check —
        # Pine cancels invalid orders before they can fill on the same bar)
        if (
            self.env.revalidate_pending
            and self._pending is not None
            and self._state in (StrategyState.PENDING_LONG, StrategyState.PENDING_SHORT)
        ):
            self._revalidate_pending(i, bar, ema_h1, ema_fast, ema_slow, ema_h4, h4_map)

        # 1. Check if pending order fills on this bar
        if self._pending is not None and self._state in (StrategyState.PENDING_LONG, StrategyState.PENDING_SHORT):
            self._check_pending_fill(i, bar)

        # 2. If in a position, manage trailing stop and check exits
        if self._position is not None and self._state in (StrategyState.LONG, StrategyState.SHORT):
            self._manage_position(i, bar, atr_h1, trail_ema)

        # 3. Evaluate IRB signal on bar close (only if warmup is satisfied)
        if i < self.env.warmup_bars:
            self._rejections.warmup += 1
            return

        # Check pending order expiry
        if self._pending is not None:
            bars_since_placed = i - self._pending.bar_placed
            if bars_since_placed >= self.env.trigger_window_bars:
                self._cancel_pending(i, OrderCancelReason.TRIGGER_WINDOW_EXPIRED)

        # Don't generate signals if in a position
        if self._state in (StrategyState.LONG, StrategyState.SHORT):
            self._rejections.existing_position += 1
            return

        # Circuit breaker: skip signal evaluation after N consecutive losses
        if self.env.max_consecutive_losses > 0 and self._consecutive_losses >= self.env.max_consecutive_losses:
            self._rejections.circuit_breaker += 1
            return

        # Evaluate IRB signal
        self._evaluate_signal(i, bar, all_bars, ema_h1, atr_h1, adx_h1, ema_h4, h4_map, ema_fast, ema_slow, atr_sma)

    def _evaluate_signal(
        self,
        i: int,
        bar: Candle,
        all_bars: list[Candle],
        ema_h1: list[float],
        atr_h1: list[float],
        adx_h1: list[float],
        ema_h4: list[float],
        h4_map: list[int],
        ema_fast: list[float] | None = None,
        ema_slow: list[float] | None = None,
        atr_sma: list[float] | None = None,
    ) -> None:
        """Evaluate IRB geometry and all filters on bar close."""
        e = self.env
        rng = bar.high - bar.low

        # Get current indicator values
        if math.isnan(ema_h1[i]) or math.isnan(atr_h1[i]) or math.isnan(adx_h1[i]):
            return
        if atr_h1[i] <= 0:
            return

        # --- v5: Daily trade limit ---
        if e.max_trades_per_day > 0 and self._trades_today >= e.max_trades_per_day:
            self._rejections.circuit_breaker += 1
            return

        # --- v5: Cooldown bars ---
        if e.cooldown_bars > 0 and (i - self._last_flat_bar) <= e.cooldown_bars:
            self._rejections.circuit_breaker += 1
            return

        # --- Session filter [v4] ---
        if e.session_filter is not None and bar.timestamp > 0:
            from datetime import datetime, timezone

            bar_hour = datetime.fromtimestamp(bar.timestamp, tz=timezone.utc).hour
            if (
                (e.session_filter == "london" and not (7 <= bar_hour < 16))
                or (e.session_filter == "newyork" and not (13 <= bar_hour < 22))
                or (e.session_filter == "london_ny_overlap" and not (13 <= bar_hour < 16))
            ):
                self._rejections.session_filter += 1
                return

        # --- Volatility filter [v4]: ATR must be above its SMA ---
        if (
            e.use_volatility_filter
            and atr_sma
            and i < len(atr_sma)
            and not math.isnan(atr_sma[i])
            and atr_h1[i] < atr_sma[i]
        ):
            self._rejections.volatility_filter += 1
            return

        # --- IRB geometry detection [A1] ---
        is_uptrend_irb = False
        is_downtrend_irb = False

        if rng > 0:
            up_threshold = bar.high - (e.irb_threshold * rng)
            dn_threshold = bar.low + (e.irb_threshold * rng)
            is_uptrend_irb = bar.open <= up_threshold and bar.close <= up_threshold
            is_downtrend_irb = bar.open >= dn_threshold and bar.close >= dn_threshold

        if not is_uptrend_irb and not is_downtrend_irb:
            self._rejections.irb_geometry += 1
            return

        # --- Trend filter [A7][U1] ---
        side: TradeSide | None = None
        ema_slope: float = 0.0

        if e.use_simple_trend_filter:
            # v5 simple trend: ema_fast > ema_slow + ema_fast rising
            if ema_fast is None or ema_slow is None:
                self._rejections.trend_filter += 1
                return
            ef = ema_fast[i] if i < len(ema_fast) else float("nan")
            es = ema_slow[i] if i < len(ema_slow) else float("nan")
            lb = min(e.ema_slope_lookback, i)
            ef_prev = ema_fast[i - lb] if lb > 0 and (i - lb) < len(ema_fast) else float("nan")
            if math.isnan(ef) or math.isnan(es) or math.isnan(ef_prev):
                self._rejections.trend_filter += 1
                return

            bull_trend = ef > es and ef > ef_prev
            bear_trend = ef < es and ef < ef_prev

            if is_uptrend_irb and bull_trend:
                side = TradeSide.LONG
            elif is_downtrend_irb and bear_trend:
                side = TradeSide.SHORT
            else:
                self._rejections.trend_filter += 1
                return
        else:
            # Original normalized slope trend filter
            lookback = min(20, i)
            if lookback < 1:
                self._rejections.trend_filter += 1
                return
            ema_slope = (ema_h1[i] - ema_h1[i - lookback]) / atr_h1[i]

            trend_up = ema_slope >= e.trend_slope_threshold
            trend_dn = ema_slope <= -e.trend_slope_threshold

            if is_uptrend_irb and trend_up:
                side = TradeSide.LONG
            elif is_downtrend_irb and trend_dn:
                side = TradeSide.SHORT
            else:
                self._rejections.trend_filter += 1
                return

        # v5 simple trend mode skips EMA stack and ADX filters
        if not e.use_simple_trend_filter:
            # --- EMA stack filter (EMA fast > EMA mid > EMA slow for longs) ---
            if e.use_ema_stack_filter and ema_fast is not None and ema_slow is not None:
                ef = ema_fast[i] if i < len(ema_fast) else float("nan")
                em = ema_h1[i]
                es = ema_slow[i] if i < len(ema_slow) else float("nan")
                if math.isnan(ef) or math.isnan(es):
                    self._rejections.trend_filter += 1
                    return
                if side == TradeSide.LONG and not (ef > em > es):
                    self._rejections.trend_filter += 1
                    return
                if side == TradeSide.SHORT and not (ef < em < es):
                    self._rejections.trend_filter += 1
                    return

            # --- EMA close confirmation (N bars closing above/below EMA fast) ---
            if e.ema_confirm_bars > 0 and ema_fast is not None:
                confirm_ok = True
                for j in range(e.ema_confirm_bars):
                    idx = i - j
                    if idx < 0 or idx >= len(ema_fast) or math.isnan(ema_fast[idx]):
                        confirm_ok = False
                        break
                    if side == TradeSide.LONG and all_bars[idx].close <= ema_fast[idx]:
                        confirm_ok = False
                        break
                    if side == TradeSide.SHORT and all_bars[idx].close >= ema_fast[idx]:
                        confirm_ok = False
                        break
                if not confirm_ok:
                    self._rejections.trend_filter += 1
                    return

        # --- MTF alignment [A8] ---
        h4_idx = h4_map[i] if i < len(h4_map) else -1
        if h4_idx < 0 or h4_idx >= len(ema_h4):
            self._rejections.mtf_alignment += 1
            return
        h4_lookback = min(e.mtf_lookback, h4_idx)
        if h4_lookback < 1:
            self._rejections.mtf_alignment += 1
            return

        h4_ema_current = ema_h4[h4_idx]
        h4_ema_prev = ema_h4[h4_idx - h4_lookback]
        if math.isnan(h4_ema_current) or math.isnan(h4_ema_prev):
            self._rejections.mtf_alignment += 1
            return

        h4_rising = h4_ema_current > h4_ema_prev
        h4_falling = h4_ema_current < h4_ema_prev

        if (side == TradeSide.LONG and not h4_rising) or (side == TradeSide.SHORT and not h4_falling):
            self._rejections.mtf_alignment += 1
            return

        # --- Sideways filter [A6][U4] ---
        adx_val = adx_h1[i]
        if math.isnan(adx_val) or adx_val < e.adx_threshold:
            self._rejections.sideways_filter += 1
            return

        # --- Overextension filter [A10][U2] ---
        overext_ratio = rng / atr_h1[i]
        if overext_ratio > e.overextension_threshold:
            self._rejections.overextension_filter += 1
            return

        # --- Minimum signal size filter [v5] ---
        if e.min_signal_atr_mult > 0 and overext_ratio < e.min_signal_atr_mult:
            self._rejections.overextension_filter += 1
            return

        # --- All filters passed: record signal ---
        self._signals.append(
            SignalRecord(
                bar_index=i,
                side=side,
                irb_range=rng,
                ema_slope=ema_slope,
                adx_value=adx_val,
                overextension_ratio=overext_ratio,
            )
        )

        # --- Compute order levels ---
        # Spread cushion: widen SL by the configured spread buffer so the
        # broker's spread-adjusted trigger doesn't stop the trade out inside
        # the intended wick-plus-buffer distance.
        spread_cushion = max(0.0, e.sl_spread_buffer_pips) * e.pip_value
        if side == TradeSide.LONG:
            entry_price = bar.high + e.pip_buffer
            stop_loss = bar.low - e.pip_buffer - spread_cushion
            # ATR-adaptive SL floor: widen SL if candle geometry is too tight
            if e.atr_sl_floor_multiplier > 0:
                min_sl_dist = atr_h1[i] * e.atr_sl_floor_multiplier
                if (entry_price - stop_loss) < min_sl_dist:
                    stop_loss = entry_price - min_sl_dist
        else:
            entry_price = bar.low - e.pip_buffer
            stop_loss = bar.high + e.pip_buffer + spread_cushion
            # ATR-adaptive SL floor: widen SL if candle geometry is too tight
            if e.atr_sl_floor_multiplier > 0:
                min_sl_dist = atr_h1[i] * e.atr_sl_floor_multiplier
                if (stop_loss - entry_price) < min_sl_dist:
                    stop_loss = entry_price + min_sl_dist

        # --- Position sizing [A5][U7] ---
        stop_distance_pips = abs(entry_price - stop_loss) / e.pip_value
        if stop_distance_pips <= 0:
            return
        risk_dollars = self._equity * e.risk_fraction
        volume = risk_dollars / (stop_distance_pips * e.pip_value_per_standard_lot)
        volume = max(e.min_volume, min(e.max_volume, round(volume, 2)))

        # --- Handle existing pending order ---
        if self._pending is not None:
            if self._pending.side == side:
                # IRB replacement [A4]
                self._cancel_pending(i, OrderCancelReason.IRB_REPLACEMENT)
            else:
                # Opposite direction — ignore
                self._rejections.existing_pending += 1
                return

        # --- Place pending stop order ---
        self._pending = _PendingOrder(
            side=side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            volume=volume,
            bar_placed=i,
            irb_bar=i,
        )
        self._state = StrategyState.PENDING_LONG if side == TradeSide.LONG else StrategyState.PENDING_SHORT

        self._pending_orders.append(
            PendingOrderRecord(
                bar_placed=i,
                side=side,
                entry_price=entry_price,
                stop_loss=stop_loss,
            )
        )

        log.debug(
            "bar %d: %s signal — entry=%.5f sl=%.5f vol=%.2f",
            i,
            side.value,
            entry_price,
            stop_loss,
            volume,
        )

    # ------------------------------------------------------------------
    # Pending order management
    # ------------------------------------------------------------------

    def _check_pending_fill(self, i: int, bar: Candle) -> None:
        """Check if the pending stop order fills on this bar."""
        p = self._pending
        if p is None:
            return

        filled = False
        fill_price = p.entry_price

        if p.side == TradeSide.LONG:
            # Buy-stop fills when high >= entry price
            if bar.high >= p.entry_price:
                filled = True
                # Gap fill: if bar opens above entry, fill at open
                if bar.open >= p.entry_price:
                    fill_price = bar.open
        else:
            # Sell-stop fills when low <= entry price
            if bar.low <= p.entry_price:
                filled = True
                if bar.open <= p.entry_price:
                    fill_price = bar.open

        if filled:
            # Update the pending order record
            for rec in reversed(self._pending_orders):
                if rec.bar_placed == p.bar_placed and rec.side == p.side:
                    rec.filled = True
                    rec.fill_bar = i
                    rec.bars_alive = i - p.bar_placed
                    break

            # Open position
            self._position = _OpenPosition(
                side=p.side,
                entry_price=fill_price,
                stop_loss=p.stop_loss,
                volume=p.volume,
                entry_bar=i,
                current_stop=p.stop_loss,
                best_close=bar.close,
                initial_stop=p.stop_loss,  # v5
                initial_volume=p.volume,  # v5
                entry_atr=getattr(self, "_cur_atr", 0.0),  # v5 stagnation guard
            )
            # Pine v5 parity: when EMA-trail is the trail mode, the live
            # trigger stop is initialized from trail_ema (Pine cur_stop), not
            # the IRB wick. stop_loss/initial_stop stay at the wick because
            # they drive sizing and R-multiple math (Pine f_qty(ep, sp)).
            ema_stop = getattr(self, "_cur_trail_ema", float("nan"))
            if not math.isnan(ema_stop):
                self._position.current_stop = ema_stop
                if (p.side == TradeSide.LONG and ema_stop >= fill_price) or (
                    p.side == TradeSide.SHORT and ema_stop <= fill_price
                ):
                    log.debug(
                        "bar %d: EMA wrong-sided at entry (%s fill=%.5f ema=%.5f) — instant SL likely",
                        i,
                        p.side.value,
                        fill_price,
                        ema_stop,
                    )
            self._state = StrategyState.LONG if p.side == TradeSide.LONG else StrategyState.SHORT
            self._pending = None
            self._trades_today += 1

            log.debug("bar %d: %s fill at %.5f", i, p.side.value, fill_price)

    def _cancel_pending(self, i: int, reason: OrderCancelReason) -> None:
        """Cancel the current pending order."""
        if self._pending is None:
            return

        for rec in reversed(self._pending_orders):
            if rec.bar_placed == self._pending.bar_placed and rec.side == self._pending.side:
                rec.cancel_reason = reason
                rec.bars_alive = i - self._pending.bar_placed
                break

        self._pending = None
        self._state = StrategyState.FLAT
        log.debug("bar %d: pending cancelled — %s", i, reason.value)

    def _revalidate_pending(
        self,
        i: int,
        bar: Candle,
        ema_h1: list[float],
        ema_fast: list[float] | None,
        ema_slow: list[float] | None,
        ema_h4: list[float],
        h4_map: list[int],
    ) -> None:
        """v5: Cancel pending order if trend or HTF conditions have changed."""
        if self._pending is None:
            return
        e = self.env

        # Check simple trend (v5 style)
        if e.use_simple_trend_filter and ema_fast is not None and ema_slow is not None:
            ef = ema_fast[i] if i < len(ema_fast) else float("nan")
            es = ema_slow[i] if i < len(ema_slow) else float("nan")
            lb = min(e.ema_slope_lookback, i)
            ef_prev = ema_fast[i - lb] if lb > 0 and (i - lb) < len(ema_fast) else float("nan")

            if not math.isnan(ef) and not math.isnan(es) and not math.isnan(ef_prev):
                if self._pending.side == TradeSide.LONG:
                    if not (ef > es and ef > ef_prev):
                        self._cancel_pending(i, OrderCancelReason.TRIGGER_WINDOW_EXPIRED)
                        return
                else:
                    if not (ef < es and ef < ef_prev):
                        self._cancel_pending(i, OrderCancelReason.TRIGGER_WINDOW_EXPIRED)
                        return

        # Check HTF alignment (use same lookback as signal path)
        h4_idx = h4_map[i] if i < len(h4_map) else -1
        if h4_idx >= 1 and h4_idx < len(ema_h4):
            h4_lookback = min(self.env.mtf_lookback, h4_idx)
            if h4_lookback < 1:
                return  # not enough data
            h4_ema_current = ema_h4[h4_idx]
            h4_ema_prev = ema_h4[h4_idx - h4_lookback]
            if not math.isnan(h4_ema_current) and not math.isnan(h4_ema_prev):
                if self._pending.side == TradeSide.LONG and not (h4_ema_current > h4_ema_prev):
                    self._cancel_pending(i, OrderCancelReason.TRIGGER_WINDOW_EXPIRED)
                    return
                if self._pending.side == TradeSide.SHORT and not (h4_ema_current < h4_ema_prev):
                    self._cancel_pending(i, OrderCancelReason.TRIGGER_WINDOW_EXPIRED)
                    return

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _manage_position(
        self,
        i: int,
        bar: Candle,
        atr_h1: list[float],
        trail_ema: list[float] | None = None,
    ) -> None:
        """Manage trailing stop and check exit conditions."""
        pos = self._position
        if pos is None:
            return

        pos.bars_held = i - pos.entry_bar

        # --- Check stop-loss hit intra-bar ---
        if pos.side == TradeSide.LONG:
            if bar.low <= pos.current_stop:
                self._close_position(i, pos.current_stop, ExitReason.STOP_LOSS)
                return
        else:
            if bar.high >= pos.current_stop:
                self._close_position(i, pos.current_stop, ExitReason.STOP_LOSS)
                return

        # --- Track peak favourable excursion (for stagnation guard) ---
        if pos.side == TradeSide.LONG:
            fav = bar.high - pos.entry_price
        else:
            fav = pos.entry_price - bar.low
        if fav > pos.peak_fav:
            pos.peak_fav = fav

        # --- Stagnation guard [v5 Pine] ---
        # At exactly bars_held == stag_bars, if peak favourable excursion is
        # below stag_atr_mult * entry_atr AND the close is still adverse to
        # the entry, close at market with STAG_EXIT.
        if self.env.use_stagnation_guard and pos.bars_held == self.env.stag_bars and pos.entry_atr > 0:
            stag_threshold = self.env.stag_atr_mult * pos.entry_atr
            adverse = bar.close < pos.entry_price if pos.side == TradeSide.LONG else bar.close > pos.entry_price
            if pos.peak_fav < stag_threshold and adverse:
                self._close_position(i, bar.close, ExitReason.STAG_EXIT)
                return

        # --- Partial exit [v5] ---
        if self.env.partial_exit_enabled and not pos.partial_taken:
            stop_distance = abs(pos.entry_price - pos.initial_stop)
            if stop_distance > 0:
                target_distance = stop_distance * self.env.partial_r_target
                if pos.side == TradeSide.LONG:
                    # For longs: check if bar HIGH reached the partial target
                    partial_target_price = pos.entry_price + target_distance
                    if bar.high >= partial_target_price:
                        self._partial_close_position(i, partial_target_price)
                else:
                    # For shorts: check if bar LOW reached the partial target
                    partial_target_price = pos.entry_price - target_distance
                    if bar.low <= partial_target_price:
                        self._partial_close_position(i, partial_target_price)

        # --- Time stop [U3] ---
        if pos.bars_held >= self.env.time_stop_bars:
            self._close_position(i, bar.close, ExitReason.TIME_STOP)
            return

        # --- Breakeven mechanism [v4]: move SL to entry after N*R profit ---
        if self.env.breakeven_r > 0 and not pos.breakeven_hit:
            pip = self.env.pip_value
            stop_distance = abs(pos.entry_price - pos.stop_loss)
            target_distance = stop_distance * self.env.breakeven_r

            if pos.side == TradeSide.LONG:
                unrealized = bar.close - pos.entry_price
                if unrealized >= target_distance:
                    pos.current_stop = max(pos.current_stop, pos.entry_price + pip)
                    pos.breakeven_hit = True
                    log.debug("bar %d: breakeven hit for LONG, stop → %.5f", i, pos.current_stop)
            else:
                unrealized = pos.entry_price - bar.close
                if unrealized >= target_distance:
                    pos.current_stop = min(pos.current_stop, pos.entry_price - pip)
                    pos.breakeven_hit = True
                    log.debug("bar %d: breakeven hit for SHORT, stop → %.5f", i, pos.current_stop)

        # --- Trail delay [v4]: don't start trailing for first N bars ---
        if self.env.trail_delay_bars > 0 and pos.bars_held < self.env.trail_delay_bars:
            return

        # --- Update trailing stop ---
        use_ema_trail = self.env.trail_ema_period > 0 and trail_ema is not None and i < len(trail_ema)

        if use_ema_trail and trail_ema is not None:
            # EMA-based trailing stop (Pine Script v2.0.0 style)
            ema_val = trail_ema[i]
            if math.isnan(ema_val):
                return

            if pos.side == TradeSide.LONG:
                pos.best_close = max(pos.best_close, bar.close)
                # Trailing stop = max(current_stop, trail_ema)
                new_trail = ema_val
                if new_trail > pos.current_stop:
                    old_stop = pos.current_stop
                    pos.current_stop = new_trail
                    if bar.low <= pos.current_stop and bar.low > old_stop:
                        self._close_position(i, pos.current_stop, ExitReason.TRAILING_STOP)
                        return
            else:
                pos.best_close = min(pos.best_close, bar.close)
                new_trail = ema_val
                if new_trail < pos.current_stop:
                    old_stop = pos.current_stop
                    pos.current_stop = new_trail
                    if bar.high >= pos.current_stop and bar.high < old_stop:
                        self._close_position(i, pos.current_stop, ExitReason.TRAILING_STOP)
                        return
        else:
            # ATR-based trailing stop (original)
            atr_val = atr_h1[i] if i < len(atr_h1) and not math.isnan(atr_h1[i]) else 0
            if atr_val <= 0:
                return

            if pos.side == TradeSide.LONG:
                pos.best_close = max(pos.best_close, bar.close)
                new_trail = pos.best_close - self.env.trail_atr_multiplier * atr_val
                if new_trail > pos.current_stop:
                    old_stop = pos.current_stop
                    pos.current_stop = new_trail
                    if bar.low <= pos.current_stop and bar.low > old_stop:
                        self._close_position(i, pos.current_stop, ExitReason.TRAILING_STOP)
                        return
            else:
                pos.best_close = min(pos.best_close, bar.close)
                new_trail = pos.best_close + self.env.trail_atr_multiplier * atr_val
                if new_trail < pos.current_stop:
                    old_stop = pos.current_stop
                    pos.current_stop = new_trail
                    if bar.high >= pos.current_stop and bar.high < old_stop:
                        self._close_position(i, pos.current_stop, ExitReason.TRAILING_STOP)
                        return

    def _partial_close_position(self, bar_idx: int, exit_price: float) -> None:
        """Realise a partial exit. Books the cash on equity and stashes the
        leg pnl on the position; does NOT emit a separate CompletedTrade —
        the runner's _close_position folds it in. This matches Pine v5
        output cardinality (one row per round-trip)."""
        pos = self._position
        if pos is None or pos.partial_taken:
            return

        pip = self.env.pip_value
        lot_val = self.env.pip_value_per_standard_lot
        partial_pct = self.env.partial_exit_pct / 100.0
        partial_volume = round(pos.initial_volume * partial_pct, 2)
        remaining_volume = round(pos.initial_volume - partial_volume, 2)

        if partial_volume <= 0 or remaining_volume <= 0:
            return

        if pos.side == TradeSide.LONG:
            pnl_pips = (exit_price - pos.entry_price) / pip
        else:
            pnl_pips = (pos.entry_price - exit_price) / pip

        pnl_pips -= self.env.spread.total_cost_pips
        pnl_usd = pnl_pips * partial_volume * lot_val
        pnl_usd -= self.env.spread.commission_per_lot_usd * partial_volume

        # Stash the leg pnl for fold-in at _close_position
        pos.partial_pnl_pips = pnl_pips
        pos.partial_pnl_usd = pnl_usd
        self._equity += pnl_usd

        # Update position
        pos.partial_taken = True
        pos.volume = remaining_volume
        # Move stop to breakeven after partial
        if pos.side == TradeSide.LONG:
            pos.current_stop = max(pos.current_stop, pos.entry_price + pip)
        else:
            pos.current_stop = min(pos.current_stop, pos.entry_price - pip)
        pos.breakeven_hit = True  # breakeven is implicit after partial

        log.debug(
            "bar %d: PARTIAL %s closed %.2f lots at %.5f, pnl=%.1f pips $%.2f, runner=%.2f lots",
            bar_idx,
            pos.side.value,
            partial_volume,
            exit_price,
            pnl_pips,
            pnl_usd,
            remaining_volume,
        )

    def _close_position(self, bar_idx: int, exit_price: float, reason: ExitReason) -> None:
        """Close the current position and record the trade."""
        pos = self._position
        if pos is None:
            return

        pip = self.env.pip_value
        lot_val = self.env.pip_value_per_standard_lot

        if pos.side == TradeSide.LONG:
            runner_pnl_pips = (exit_price - pos.entry_price) / pip
        else:
            runner_pnl_pips = (pos.entry_price - exit_price) / pip

        # Deduct transaction costs: spread + slippage (round-trip)
        runner_pnl_pips -= self.env.spread.total_cost_pips

        runner_pnl_usd = runner_pnl_pips * pos.volume * lot_val

        # Deduct commission (round-trip per lot)
        runner_pnl_usd -= self.env.spread.commission_per_lot_usd * pos.volume

        # v5: fold any prior partial-leg pnl into the round-trip total so
        # this CompletedTrade represents the full entry → all-out cash result
        # (matches Pine v5 export cardinality: one row per round-trip).
        pnl_pips = runner_pnl_pips + pos.partial_pnl_pips
        pnl_usd = runner_pnl_usd + pos.partial_pnl_usd

        # Risk R-multiple computed on initial stop (before partial moved BE),
        # so a +1R partial / 0R runner combo nets ~+0.5R, not the truncated
        # runner-only R that the prior code reported.
        stop_distance_pips = abs(pos.entry_price - pos.initial_stop) / pip
        risk_r = pnl_pips / stop_distance_pips if stop_distance_pips > 0 else 0.0

        self._trade_counter += 1
        trade = CompletedTrade(
            trade_id=self._trade_counter,
            side=pos.side,
            entry_bar=pos.entry_bar,
            exit_bar=bar_idx,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            stop_loss=pos.initial_stop,
            volume=pos.initial_volume or pos.volume,
            exit_reason=reason,
            pnl_pips=pnl_pips,
            pnl_usd=pnl_usd,
            risk_r=risk_r,
        )
        self._trades.append(trade)
        # Only add the runner-leg pnl to equity here — the partial leg (if any)
        # was already added to equity in _partial_close_position.
        self._equity += runner_pnl_usd

        # Update consecutive loss tracker for circuit breaker on round-trip pnl
        if pnl_pips > 0:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1

        log.debug(
            "bar %d: %s closed (%s) pnl=%.1f pips $%.2f (%.2fR) equity=$%.2f",
            bar_idx,
            pos.side.value,
            reason.value,
            pnl_pips,
            pnl_usd,
            risk_r,
            self._equity,
        )

        self._position = None
        self._state = StrategyState.FLAT
        self._last_flat_bar = bar_idx

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_h4_map(self, h1_candles: list[Candle], h4_candles: list[Candle]) -> list[int]:
        """Map each primary-TF bar index to the most recent higher-TF bar index by timestamp.

        If higher-TF candles have no timestamps (0.0), use the ratio from
        ``self.env.h1_to_h4_ratio`` for positional mapping.
        """
        n = len(h1_candles)
        if not h4_candles:
            return [-1] * n

        # Check if timestamps are populated
        has_timestamps = h4_candles[0].timestamp > 0 and h1_candles[0].timestamp > 0

        if has_timestamps:
            h4_ts = [c.timestamp for c in h4_candles]
            mapping = []
            h4_idx = 0
            for h1_bar in h1_candles:
                while h4_idx < len(h4_ts) - 1 and h4_ts[h4_idx + 1] <= h1_bar.timestamp:
                    h4_idx += 1
                mapping.append(h4_idx)
            return mapping
        else:
            # Ratio mapping using env-configured ratio (default 4 for H1:H4)
            ratio = self.env.h1_to_h4_ratio
            return [min(i // ratio, len(h4_candles) - 1) for i in range(n)]

    def _detect_day_boundary(self, i: int, bar: Candle) -> bool:
        """Detect if bar i starts a new trading day. Resets daily counters."""
        if bar.timestamp > 0:
            from datetime import datetime, timezone

            day_key = int(datetime.fromtimestamp(bar.timestamp, tz=timezone.utc).toordinal())
        else:
            day_key = i // self.env.h1_bars_per_day
        if day_key != self._current_day_key:
            self._current_day_key = day_key
            self._trades_today = 0
            return True
        return False


# ---------------------------------------------------------------------------
# Strategy-agnostic backtester adapter
# ---------------------------------------------------------------------------


class StrategyBacktesterAdapter:
    """Generic backtester that delegates signal logic to a BaseStrategy.

    This adapter wraps any ``BaseStrategy`` implementation into a backtester
    with the same ``run(h1_candles, h4_candles) -> BacktestResult`` interface
    as ``IRBBacktester``.  It uses the strategy's ``check_entry`` and
    ``check_exit`` methods for bar-by-bar simulation.
    """

    def __init__(self, strategy: object, env: BacktestEnvironment | None = None) -> None:
        from novatrade.strategies.base import BaseStrategy as _BS

        if not isinstance(strategy, _BS):
            raise TypeError(f"Expected BaseStrategy, got {type(strategy).__name__}")
        self.strategy = strategy
        self.env = env or DEFAULT_ENVIRONMENT
        self._equity = self.env.initial_equity
        self._trade_counter = 0

    def run(
        self,
        h1_candles: list[Candle],
        h4_candles: list[Candle] | None = None,
    ) -> BacktestResult:
        """Run bar-by-bar simulation using the pluggable strategy.

        Args:
            h1_candles: Primary timeframe candles.
            h4_candles: Higher timeframe candles (optional, passed to strategy).

        Returns:
            BacktestResult with completed trades.
        """
        n = len(h1_candles)
        if n == 0:
            return BacktestResult(total_bars=0, final_equity=self._equity, environment=self.env)

        indicators = self.strategy.compute_indicators(h1_candles)

        trades: list[CompletedTrade] = []
        signals: list[SignalRecord] = []
        position: dict | None = None
        pending_entry: dict | None = None  # simple 1-bar delay model

        for i in range(n):
            # Check exit for open position
            if position is not None:
                exit_sig = self.strategy.check_exit(i, h1_candles, indicators, position)
                if exit_sig is not None:
                    trade = self._close_trade(position, exit_sig, h1_candles[i])
                    trades.append(trade)
                    position = None

            # Fill pending entry on this bar (1-bar delay)
            if pending_entry is not None and position is None:
                position = {
                    "side": pending_entry["side"],
                    "entry_price": pending_entry["entry_price"],
                    "stop_loss": pending_entry["stop_loss"],
                    "entry_bar": i,
                    "current_stop": pending_entry["stop_loss"],
                    "best_close": h1_candles[i].close,
                }
                pending_entry = None

            # Check for new entry signal (only if flat)
            if position is None and pending_entry is None:
                entry_sig = self.strategy.check_entry(i, h1_candles, indicators, h4_candles)
                if entry_sig is not None:
                    signals.append(
                        SignalRecord(
                            bar_index=entry_sig.bar_index,
                            side=TradeSide.LONG if entry_sig.side == "LONG" else TradeSide.SHORT,
                        )
                    )
                    pending_entry = {
                        "side": entry_sig.side,
                        "entry_price": entry_sig.entry_price,
                        "stop_loss": entry_sig.stop_loss,
                    }

            # Update position tracking
            if position is not None:
                bar = h1_candles[i]
                if position["side"] == "LONG":
                    position["best_close"] = max(position["best_close"], bar.close)
                else:
                    position["best_close"] = min(position["best_close"], bar.close)

        # Close any remaining position at last bar
        if position is not None:
            from novatrade.strategies.base import ExitSignal

            exit_sig_final = ExitSignal(bar_index=n - 1, exit_price=h1_candles[-1].close, reason="time_stop")
            trade = self._close_trade(position, exit_sig_final, h1_candles[-1])
            trades.append(trade)

        return BacktestResult(
            trades=trades,
            signals=signals,
            total_bars=n,
            final_equity=self._equity,
            environment=self.env,
        )

    def _close_trade(self, position: dict, exit_sig: Any, bar: Candle) -> CompletedTrade:
        pip = self.env.pip_value
        lot_val = self.env.pip_value_per_standard_lot

        side = TradeSide.LONG if position["side"] == "LONG" else TradeSide.SHORT
        entry_price = position["entry_price"]
        exit_price = exit_sig.exit_price

        if side == TradeSide.LONG:
            pnl_pips = (exit_price - entry_price) / pip
        else:
            pnl_pips = (entry_price - exit_price) / pip

        pnl_pips -= self.env.spread.total_cost_pips

        risk_dollars = self._equity * self.env.risk_fraction
        stop_distance_pips = abs(entry_price - position["stop_loss"]) / pip
        if stop_distance_pips > 0:
            volume = risk_dollars / (stop_distance_pips * lot_val)
            volume = max(self.env.min_volume, min(self.env.max_volume, round(volume, 2)))
        else:
            volume = self.env.min_volume

        pnl_usd = pnl_pips * volume * lot_val
        pnl_usd -= self.env.spread.commission_per_lot_usd * volume

        risk_r = pnl_pips / stop_distance_pips if stop_distance_pips > 0 else 0.0

        # Map exit reason
        reason_map = {
            "stop_loss": ExitReason.STOP_LOSS,
            "trailing_stop": ExitReason.TRAILING_STOP,
            "time_stop": ExitReason.TIME_STOP,
            "signal_exit": ExitReason.TIME_STOP,  # closest enum match
        }
        exit_reason = reason_map.get(exit_sig.reason, ExitReason.TIME_STOP)

        self._trade_counter += 1
        trade = CompletedTrade(
            trade_id=self._trade_counter,
            side=side,
            entry_bar=position["entry_bar"],
            exit_bar=exit_sig.bar_index,
            entry_price=entry_price,
            exit_price=exit_price,
            stop_loss=position["stop_loss"],
            volume=volume,
            exit_reason=exit_reason,
            pnl_pips=pnl_pips,
            pnl_usd=pnl_usd,
            risk_r=risk_r,
        )
        self._equity += pnl_usd
        return trade


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_backtester(
    strategy_type: str,
    env: BacktestEnvironment | None = None,
) -> IRBBacktester | StrategyBacktesterAdapter:
    """Create a backtester for the given strategy type.

    For "irb", returns the battle-tested IRBBacktester (zero regression risk).
    For other registered strategies, returns a StrategyBacktesterAdapter that
    delegates to the corresponding BaseStrategy implementation.

    Args:
        strategy_type: Registered strategy name (e.g. "irb", "mean_reversion").
        env: Optional BacktestEnvironment override.

    Returns:
        A backtester with a ``run(h1_candles, h4_candles)`` method.

    Raises:
        KeyError: If strategy_type is not registered.
    """
    if strategy_type == "irb":
        return IRBBacktester(env=env)

    from novatrade.strategies.registry import get_strategy

    strategy_cls = get_strategy(strategy_type)
    strategy = strategy_cls()
    return StrategyBacktesterAdapter(strategy=strategy, env=env)
