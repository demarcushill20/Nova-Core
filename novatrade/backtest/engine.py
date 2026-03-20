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
    ) -> None:
        """Process a single H1 bar."""
        # 1. Check if pending order fills on this bar
        if self._pending is not None and self._state in (StrategyState.PENDING_LONG, StrategyState.PENDING_SHORT):
            self._check_pending_fill(i, bar)

        # 2. If in a position, manage trailing stop and check exits
        if self._position is not None and self._state in (StrategyState.LONG, StrategyState.SHORT):
            self._manage_position(i, bar, atr_h1)

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

        # Evaluate IRB signal
        self._evaluate_signal(i, bar, all_bars, ema_h1, atr_h1, adx_h1, ema_h4, h4_map)

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
    ) -> None:
        """Evaluate IRB geometry and all filters on bar close."""
        e = self.env
        rng = bar.high - bar.low

        # Get current indicator values
        if math.isnan(ema_h1[i]) or math.isnan(atr_h1[i]) or math.isnan(adx_h1[i]):
            return
        if atr_h1[i] <= 0:
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
        lookback = min(20, i)
        if lookback < 1:
            self._rejections.trend_filter += 1
            return
        ema_slope = (ema_h1[i] - ema_h1[i - lookback]) / atr_h1[i]

        trend_up = ema_slope >= e.trend_slope_threshold
        trend_dn = ema_slope <= -e.trend_slope_threshold

        # Determine signal direction
        side: TradeSide | None = None
        if is_uptrend_irb and trend_up:
            side = TradeSide.LONG
        elif is_downtrend_irb and trend_dn:
            side = TradeSide.SHORT
        else:
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
        if side == TradeSide.LONG:
            entry_price = bar.high + e.pip_buffer
            stop_loss = bar.low - e.pip_buffer
        else:
            entry_price = bar.low - e.pip_buffer
            stop_loss = bar.high + e.pip_buffer

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
            )
            self._state = StrategyState.LONG if p.side == TradeSide.LONG else StrategyState.SHORT
            self._pending = None

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

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _manage_position(self, i: int, bar: Candle, atr_h1: list[float]) -> None:
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

        # --- Time stop [U3] ---
        if pos.bars_held >= self.env.time_stop_bars:
            self._close_position(i, bar.close, ExitReason.TIME_STOP)
            return

        # --- Update trailing stop [A9][U3] ---
        atr_val = atr_h1[i] if i < len(atr_h1) and not math.isnan(atr_h1[i]) else 0
        if atr_val <= 0:
            return

        if pos.side == TradeSide.LONG:
            pos.best_close = max(pos.best_close, bar.close)
            new_trail = pos.best_close - self.env.trail_atr_multiplier * atr_val
            if new_trail > pos.current_stop:
                old_stop = pos.current_stop
                pos.current_stop = new_trail
                # Check if trailing stop triggers on this bar's low
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

    def _close_position(self, bar_idx: int, exit_price: float, reason: ExitReason) -> None:
        """Close the current position and record the trade."""
        pos = self._position
        if pos is None:
            return

        pip = self.env.pip_value
        lot_val = self.env.pip_value_per_standard_lot

        if pos.side == TradeSide.LONG:
            pnl_pips = (exit_price - pos.entry_price) / pip
        else:
            pnl_pips = (pos.entry_price - exit_price) / pip

        # Deduct transaction costs: spread + slippage (round-trip)
        pnl_pips -= self.env.spread.total_cost_pips

        pnl_usd = pnl_pips * pos.volume * lot_val

        # Deduct commission (round-trip: entry + exit)
        pnl_usd -= self.env.spread.commission_per_lot_usd * pos.volume * 2

        # Risk R-multiple
        stop_distance_pips = abs(pos.entry_price - pos.stop_loss) / pip
        risk_r = pnl_pips / stop_distance_pips if stop_distance_pips > 0 else 0.0

        self._trade_counter += 1
        trade = CompletedTrade(
            trade_id=self._trade_counter,
            side=pos.side,
            entry_bar=pos.entry_bar,
            exit_bar=bar_idx,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            stop_loss=pos.stop_loss,
            volume=pos.volume,
            exit_reason=reason,
            pnl_pips=pnl_pips,
            pnl_usd=pnl_usd,
            risk_r=risk_r,
        )
        self._trades.append(trade)
        self._equity += pnl_usd

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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_h4_map(h1_candles: list[Candle], h4_candles: list[Candle]) -> list[int]:
        """Map each H1 bar index to the most recent H4 bar index by timestamp.

        If H4 candles have no timestamps (0.0), use a simple ratio mapping.
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
            # Simple ratio mapping: every 4 H1 bars = 1 H4 bar
            return [min(i // 4, len(h4_candles) - 1) for i in range(n)]


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
        pnl_usd -= self.env.spread.commission_per_lot_usd * volume * 2

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
