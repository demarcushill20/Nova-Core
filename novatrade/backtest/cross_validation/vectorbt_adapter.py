"""Adapter for vectorbt (polakowo/vectorbt).

pip install vectorbt

Translation approach:
- Compute all indicators as numpy arrays (vectorized)
- Build entry/exit boolean signal arrays from IRB logic
- Use vbt.Portfolio.from_signals() with stop-loss parameters

Known limitations:
- vectorbt fills at next-bar close, NOT at stop-order price
- No native stop-order entry model — signals fire, fill happens at close
- Trailing stop via trail_stop parameter
- Open-source edition (v0.28.x) is frozen; PRO has more features
"""

from __future__ import annotations

import os
import sys
import time

# Prevent SIGABRT from OpenBLAS multi-threaded allocation on constrained VPS.
# Must be set before numpy is imported (numpy reads this at import time).
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from novatrade.backtest.cross_validation.base_adapter import BaseEngineAdapter
from novatrade.backtest.cross_validation.types import (
    EngineId,
    EngineResult,
    MetricAvailability,
    NormalizedMetrics,
    NormalizedTrade,
)
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle


def _import_vectorbt():
    """Import vectorbt with sys.path sanitized to avoid telegram module shadowing.

    nova-core's ``telegram/`` package shadows ``python-telegram-bot`` which
    vectorbt.messaging.telegram tries to import.  We temporarily remove any
    path entry whose resolved directory contains a local ``telegram/__init__.py``
    (i.e. not from site-packages).
    """
    from pathlib import Path

    poison: list[tuple[int, str]] = []
    for i, p in enumerate(sys.path):
        resolved = Path(p).resolve() if p else Path.cwd()
        candidate = resolved / "telegram" / "__init__.py"
        if candidate.exists() and "site-packages" not in str(resolved):
            poison.append((i, p))

    for _, p in reversed(poison):
        sys.path.remove(p)
    try:
        import vectorbt as vbt

        return vbt
    finally:
        for idx, p in poison:
            if p not in sys.path:
                sys.path.insert(idx, p)


class VectorbtAdapter(BaseEngineAdapter):
    """Adapter for vectorbt."""

    @property
    def engine_id(self) -> EngineId:
        return EngineId.VECTORBT

    @property
    def engine_version(self) -> str:
        try:
            vbt = _import_vectorbt()
            return f"vectorbt {vbt.__version__}"
        except Exception:
            return "vectorbt (unknown)"

    def is_available(self) -> bool:
        try:
            _import_vectorbt()
            return True
        except ImportError:
            return False

    def run(
        self,
        h1_candles: list[Candle],
        h4_candles: list[Candle],
        env: BacktestEnvironment,
    ) -> EngineResult:
        t0 = time.monotonic()
        try:
            import math

            import numpy as np
            import pandas as pd

            vbt = _import_vectorbt()
            from novatrade.backtest.engine import compute_adx, compute_atr, compute_ema

            df = self._candles_to_dataframe(h1_candles)
            n = len(h1_candles)
            closes = df["Close"].values

            ema = compute_ema(closes.tolist(), env.ema_period)
            ema_fast = compute_ema(closes.tolist(), env.ema_fast_period)
            ema_slow = compute_ema(closes.tolist(), env.ema_slow_period)
            atr = compute_atr(h1_candles, env.atr_period)
            adx = compute_adx(h1_candles, env.adx_period)

            atr_sma: list[float] = []
            if env.use_volatility_filter:
                ma_period = env.volatility_atr_ma_period
                atr_sma = [float("nan")] * n
                for j in range(ma_period - 1, n):
                    vals = [atr[k] for k in range(j - ma_period + 1, j + 1) if not math.isnan(atr[k])]
                    if len(vals) == ma_period:
                        atr_sma[j] = sum(vals) / ma_period

            h4_ema: list[float] = []
            h4_map: list[int] = []
            if h4_candles:
                h4_closes = [c.close for c in h4_candles]
                h4_ema = compute_ema(h4_closes, env.ema_period)
                h4_ts = [c.timestamp for c in h4_candles]
                h4_idx = 0
                for c in h1_candles:
                    while h4_idx < len(h4_ts) - 1 and h4_ts[h4_idx + 1] <= c.timestamp:
                        h4_idx += 1
                    h4_map.append(h4_idx)

            long_entries = np.zeros(n, dtype=bool)
            short_entries = np.zeros(n, dtype=bool)
            entry_prices = np.zeros(n)
            stop_losses = np.zeros(n)

            for i in range(n):
                if i < env.warmup_bars:
                    continue
                bar = h1_candles[i]
                if math.isnan(ema[i]) or math.isnan(atr[i]) or math.isnan(adx[i]) or atr[i] <= 0:
                    continue

                rng = bar.high - bar.low
                if rng <= 0:
                    continue

                if env.session_filter is not None and bar.timestamp > 0:
                    from datetime import datetime, timezone

                    bar_hour = datetime.fromtimestamp(bar.timestamp, tz=timezone.utc).hour
                    if (
                        (env.session_filter == "london" and not (7 <= bar_hour < 16))
                        or (env.session_filter == "newyork" and not (13 <= bar_hour < 22))
                        or (env.session_filter == "london_ny_overlap" and not (13 <= bar_hour < 16))
                    ):
                        continue

                if (
                    env.use_volatility_filter
                    and atr_sma
                    and i < len(atr_sma)
                    and not math.isnan(atr_sma[i])
                    and atr[i] < atr_sma[i]
                ):
                    continue

                up_th = bar.high - env.irb_threshold * rng
                dn_th = bar.low + env.irb_threshold * rng
                is_up_irb = bar.open <= up_th and bar.close <= up_th
                is_dn_irb = bar.open >= dn_th and bar.close >= dn_th
                if not is_up_irb and not is_dn_irb:
                    continue

                side = None
                if env.use_simple_trend_filter:
                    ef, es = ema_fast[i], ema_slow[i]
                    lb = min(env.ema_slope_lookback, i)
                    if lb < 1:
                        continue
                    ef_prev = ema_fast[i - lb]
                    if math.isnan(ef) or math.isnan(es) or math.isnan(ef_prev):
                        continue
                    if is_up_irb and ef > es and ef > ef_prev:
                        side = "LONG"
                    elif is_dn_irb and ef < es and ef < ef_prev:
                        side = "SHORT"
                    else:
                        continue
                else:
                    lookback = min(20, i)
                    if lookback < 1:
                        continue
                    slope = (ema[i] - ema[i - lookback]) / atr[i]
                    if is_up_irb and slope >= env.trend_slope_threshold:
                        side = "LONG"
                    elif is_dn_irb and slope <= -env.trend_slope_threshold:
                        side = "SHORT"
                    else:
                        continue

                    if env.use_ema_stack_filter:
                        ef, es = ema_fast[i], ema_slow[i]
                        if math.isnan(ef) or math.isnan(es):
                            continue
                        if side == "LONG" and not (ef > ema[i] > es):
                            continue
                        if side == "SHORT" and not (ef < ema[i] < es):
                            continue

                    if env.ema_confirm_bars > 0:
                        confirm_ok = True
                        for j in range(env.ema_confirm_bars):
                            idx = i - j
                            if idx < 0 or idx >= len(ema_fast) or math.isnan(ema_fast[idx]):
                                confirm_ok = False
                                break
                            if side == "LONG" and h1_candles[idx].close <= ema_fast[idx]:
                                confirm_ok = False
                                break
                            if side == "SHORT" and h1_candles[idx].close >= ema_fast[idx]:
                                confirm_ok = False
                                break
                        if not confirm_ok:
                            continue

                if h4_map and h4_ema:
                    h4_i = h4_map[i] if i < len(h4_map) else -1
                    if h4_i < 0 or h4_i >= len(h4_ema):
                        continue
                    h4_lb = min(env.mtf_lookback, h4_i)
                    if h4_lb < 1:
                        continue
                    h4_cur = h4_ema[h4_i]
                    h4_prev = h4_ema[h4_i - h4_lb]
                    if math.isnan(h4_cur) or math.isnan(h4_prev):
                        continue
                    if side == "LONG" and not (h4_cur > h4_prev):
                        continue
                    if side == "SHORT" and not (h4_cur < h4_prev):
                        continue

                if math.isnan(adx[i]) or adx[i] < env.adx_threshold:
                    continue

                overext = rng / atr[i]
                if overext > env.overextension_threshold:
                    continue
                if env.min_signal_atr_mult > 0 and overext < env.min_signal_atr_mult:
                    continue

                if side == "LONG":
                    long_entries[i] = True
                    entry = bar.high + env.pip_buffer
                    sl = bar.low - env.pip_buffer - max(0.0, env.sl_spread_buffer_pips) * env.pip_value
                    if env.atr_sl_floor_multiplier > 0:
                        min_sl = atr[i] * env.atr_sl_floor_multiplier
                        if (entry - sl) < min_sl:
                            sl = entry - min_sl
                else:
                    short_entries[i] = True
                    entry = bar.low - env.pip_buffer
                    sl = bar.high + env.pip_buffer + max(0.0, env.sl_spread_buffer_pips) * env.pip_value
                    if env.atr_sl_floor_multiplier > 0:
                        min_sl = atr[i] * env.atr_sl_floor_multiplier
                        if (sl - entry) < min_sl:
                            sl = entry + min_sl
                entry_prices[i] = entry
                stop_losses[i] = sl

            # Simulate Nova's pending-stop cadence AND its position release (stop / time
            # stop), then drive vectorbt with explicit entry + exit signals and the exact
            # fill prices. This replaces the old single global sl_pct trailing stop, whose
            # independent exit model disagreed with the entry filter and silently dropped
            # ~19 entries while "in position" under its own unrelated stop.
            sim = self._simulate_execution(
                long_entries,
                short_entries,
                h1_candles,
                env,
                entry_prices,
                stop_losses,
                atr=atr,
            )
            long_entries = sim["long_entries"]
            short_entries = sim["short_entries"]

            close_series = pd.Series(closes, index=df.index)
            price_series = pd.Series(sim["price"], index=df.index)
            size_arr = sim["size"]
            price_arr = sim["price"]

            # vectorbt OSS (0.28) has no leverage, so a cash-secured account cannot
            # hold a risk-sized FX position. Fund it large enough to afford the
            # biggest single position; absolute PnL is leverage-invariant, and
            # drawdown %/return are recomputed against initial_equity in metrics.
            max_notional = float((size_arr * price_arr).max()) if size_arr.size else 0.0
            init_cash = max(env.initial_equity, max_notional * 2.0)

            pf = vbt.Portfolio.from_signals(
                close=close_series,
                entries=pd.Series(sim["long_entries"], index=df.index),
                exits=pd.Series(sim["long_exits"], index=df.index),
                short_entries=pd.Series(sim["short_entries"], index=df.index),
                short_exits=pd.Series(sim["short_exits"], index=df.index),
                price=price_series,
                size=pd.Series(size_arr, index=df.index),
                size_type="amount",
                init_cash=init_cash,
                accumulate=False,
                freq="1h",
            )
            self._init_cash = init_cash
            self._initial_equity = env.initial_equity

            stats = pf.stats()
            trade_records = pf.trades.records_readable if hasattr(pf.trades, "records_readable") else None

            ts_to_bar = {ts: i for i, ts in enumerate(df.index)}
            trades = self._normalize_trades(trade_records, ts_to_bar)
            metrics = self._extract_metrics(stats, pf)

            return EngineResult(
                engine=self.engine_id,
                engine_version=self.engine_version,
                trades=trades,
                metrics=metrics,
                elapsed_seconds=time.monotonic() - t0,
                raw_result=pf,
                config_notes=[
                    "signals pre-computed using full Nova filter chain"
                    " (range-zone IRB, EMA trend, H4 MTF, ADX, overextension)",
                    "explicit entry+exit signals replay Nova's pending-stop cadence",
                    "fills at exact pending-stop / stop-loss prices via price= (not bar close)",
                    "single exit model (no global sl_pct); occupancy = one position at a time",
                ],
            )
        except ImportError:
            return EngineResult(
                engine=self.engine_id,
                engine_version=self.engine_version,
                elapsed_seconds=time.monotonic() - t0,
                error="vectorbt not installed (pip install vectorbt)",
            )
        except Exception as exc:
            return EngineResult(
                engine=self.engine_id,
                engine_version=self.engine_version,
                elapsed_seconds=time.monotonic() - t0,
                error=str(exc),
            )

    @staticmethod
    def _apply_execution_filter(
        long_entries,
        short_entries,
        candles: list[Candle],
        env: BacktestEnvironment,
        entry_prices=None,
        stop_losses=None,
    ):
        """Simulate Nova's pending-stop cadence on pre-computed signal arrays.

        Signals place pending stop orders on bar close. A trade entry is emitted
        only on a later bar whose high/low crosses that stop level; unfilled
        orders expire after trigger_window_bars. If entry_prices are omitted the
        legacy time-stop-only approximation is used for unit tests.
        """
        import numpy as np

        n = len(candles)
        filtered_long = np.zeros(n, dtype=bool)
        filtered_short = np.zeros(n, dtype=bool)

        if entry_prices is None or stop_losses is None:
            in_position = False
            entry_bar = -1
            last_flat_bar = -9999
            trades_today = 0
            last_day_ord = -1
            for i in range(n):
                if env.max_trades_per_day > 0:
                    ts = candles[i].timestamp
                    if ts > 0:
                        from datetime import datetime, timezone

                        day_ord = datetime.fromtimestamp(ts, tz=timezone.utc).toordinal()
                    else:
                        day_ord = i // 24
                    if day_ord != last_day_ord:
                        last_day_ord = day_ord
                        trades_today = 0
                if in_position:
                    if i - entry_bar >= env.time_stop_bars:
                        in_position = False
                        last_flat_bar = i
                    continue
                if env.cooldown_bars > 0 and (i - last_flat_bar) <= env.cooldown_bars:
                    continue
                if env.max_trades_per_day > 0 and trades_today >= env.max_trades_per_day:
                    continue
                if long_entries[i] or short_entries[i]:
                    filtered_long[i] = bool(long_entries[i])
                    filtered_short[i] = bool(short_entries[i]) and not bool(long_entries[i])
                    in_position = True
                    entry_bar = i
                    trades_today += 1
            return filtered_long, filtered_short

        sim = VectorbtAdapter._simulate_execution(long_entries, short_entries, candles, env, entry_prices, stop_losses)
        return sim["long_entries"], sim["short_entries"]

    @staticmethod
    def _simulate_execution(
        long_entries,
        short_entries,
        candles: list[Candle],
        env: BacktestEnvironment,
        entry_prices,
        stop_losses,
        atr=None,
    ) -> dict:
        """Full one-position-at-a-time pending-stop simulation.

        Mirrors Nova's execution state machine closely enough to drive vectorbt
        with EXPLICIT entry + exit signals (no global sl_pct). Returns boolean
        entry/exit arrays per side plus a per-bar fill-price array:

        * entries fill at the pending-stop ``entry_price`` on the first bar (within
          ``trigger_window_bars``) whose high/low crosses it; a same-side signal
          re-arms the pending order, an opposite-side signal is ignored while armed.
        * a position releases on the first of: stop-loss crossing (fill at the stop)
          or the ``time_stop_bars`` ceiling (fill at close).
        * ``price[i]`` carries the exact fill price for whichever event fired on bar
          ``i`` (entry or exit); other bars keep the close so vectorbt values them.

        The entry arrays are identical to the legacy ``_apply_execution_filter``
        prices-path; this method additionally surfaces the exit timing/price the
        filter was already computing internally but discarding.
        """
        import math

        import numpy as np

        n = len(candles)
        f_long = np.zeros(n, dtype=bool)
        f_short = np.zeros(n, dtype=bool)
        x_long = np.zeros(n, dtype=bool)
        x_short = np.zeros(n, dtype=bool)
        price = np.array([c.close for c in candles], dtype=float)
        size = np.zeros(n, dtype=float)  # units at each entry bar (volume * 100k)

        # Cost model — replicate Nova exactly so vectorbt's portfolio reproduces
        # net USD. Spread+slippage is a per-trade pip cost; the $/lot commission
        # is ALSO a constant per-trade pip cost (commission_per_lot / lot_val),
        # independent of volume. Both fold into one exit-price haircut.
        pip = env.pip_value
        lot_val = env.pip_value_per_standard_lot
        cost_pips = env.spread.total_cost_pips + (env.spread.commission_per_lot_usd / lot_val if lot_val else 0.0)

        equity = env.initial_equity  # compounding base for risk-fraction sizing
        position_volume = 0.0

        in_position = False
        position_entry_bar = -1
        position_side: str | None = None
        position_stop = 0.0
        position_entry_price = 0.0
        position_initial_stop = 0.0
        position_best = 0.0
        breakeven_hit = False
        last_flat_bar = -9999
        trades_today = 0
        last_day_ord = -1
        pending_side: str | None = None
        pending_entry = 0.0
        pending_stop = 0.0
        pending_bar = -1

        def _emit_exit(bar_i, exit_price, is_long, entry_price, volume):
            # Bake the full per-trade cost (spread + slippage + commission) into the
            # fill price as a pip haircut so vectorbt's portfolio PnL == Nova's net;
            # return the realised USD so the caller can compound it into equity for
            # the next trade's risk-fraction size.
            gross_pips = (exit_price - entry_price) / pip if is_long else (entry_price - exit_price) / pip
            if is_long:
                x_long[bar_i] = True
            else:
                x_short[bar_i] = True
            price[bar_i] = exit_price - cost_pips * pip if is_long else exit_price + cost_pips * pip
            return (gross_pips - cost_pips) * volume * lot_val

        for i, candle in enumerate(candles):
            if env.max_trades_per_day > 0:
                ts = candle.timestamp
                if ts > 0:
                    from datetime import datetime, timezone

                    day_ord = datetime.fromtimestamp(ts, tz=timezone.utc).toordinal()
                else:
                    day_ord = i // 24
                if day_ord != last_day_ord:
                    last_day_ord = day_ord
                    trades_today = 0

            if in_position:
                # Replicate Nova _manage_position intra-bar order so the slot
                # frees on the SAME bar Nova would close: (1) stop-loss against
                # the stop carried from the prior bar, (2) time-stop ceiling,
                # (3) breakeven ratchet, (4) ATR trailing ratchet. Steps 3-4 only
                # move the stop for subsequent bars unless the trail jumps past an
                # intra-bar wick. With ``atr=None`` only the fixed stop / time-stop
                # fire (legacy behaviour, used by unit tests).
                bars_held = i - position_entry_bar
                is_long = position_side == "LONG"

                # (1) stop-loss — stop value as of the previous bar's ratchet
                if (is_long and candle.low <= position_stop) or (not is_long and candle.high >= position_stop):
                    equity += _emit_exit(i, position_stop, is_long, position_entry_price, position_volume)
                    in_position = False
                    last_flat_bar = i
                    continue

                # (2) time stop — fill at close
                if bars_held >= env.time_stop_bars:
                    equity += _emit_exit(i, candle.close, is_long, position_entry_price, position_volume)
                    in_position = False
                    last_flat_bar = i
                    continue

                if atr is not None:
                    # (3) breakeven: move stop to entry+/-pip after breakeven_r * R
                    if env.breakeven_r > 0 and not breakeven_hit:
                        sd = abs(position_entry_price - position_initial_stop)
                        target = sd * env.breakeven_r
                        if is_long and (candle.close - position_entry_price) >= target:
                            position_stop = max(position_stop, position_entry_price + env.pip_value)
                            breakeven_hit = True
                        elif not is_long and (position_entry_price - candle.close) >= target:
                            position_stop = min(position_stop, position_entry_price - env.pip_value)
                            breakeven_hit = True

                    # (4) ATR trailing (close anchor); trail_delay default 0
                    a = atr[i] if i < len(atr) and not math.isnan(atr[i]) else 0.0
                    if a > 0 and (env.trail_delay_bars <= 0 or bars_held >= env.trail_delay_bars):
                        if is_long:
                            position_best = max(position_best, candle.close)
                            new_trail = position_best - env.trail_atr_multiplier * a
                            if new_trail > position_stop:
                                old_stop = position_stop
                                position_stop = new_trail
                                if candle.low <= position_stop and candle.low > old_stop:
                                    equity += _emit_exit(
                                        i, position_stop, is_long, position_entry_price, position_volume
                                    )
                                    in_position = False
                                    last_flat_bar = i
                                    continue
                        else:
                            position_best = min(position_best, candle.close)
                            new_trail = position_best + env.trail_atr_multiplier * a
                            if new_trail < position_stop:
                                old_stop = position_stop
                                position_stop = new_trail
                                if candle.high >= position_stop and candle.high < old_stop:
                                    equity += _emit_exit(
                                        i, position_stop, is_long, position_entry_price, position_volume
                                    )
                                    in_position = False
                                    last_flat_bar = i
                                    continue
                continue

            if pending_side is not None:
                filled = (pending_side == "LONG" and candle.high >= pending_entry) or (
                    pending_side == "SHORT" and candle.low <= pending_entry
                )
                if filled:
                    if env.max_trades_per_day <= 0 or trades_today < env.max_trades_per_day:
                        # Gap fill: if the bar opens through the pending stop, Nova
                        # fills at bar.open, not the stop level (engine.py _check_pending_fill).
                        if pending_side == "LONG":
                            f_long[i] = True
                            fill_px = candle.open if candle.open >= pending_entry else pending_entry
                        else:
                            f_short[i] = True
                            fill_px = candle.open if candle.open <= pending_entry else pending_entry
                        price[i] = fill_px
                        in_position = True
                        position_entry_bar = i
                        position_side = pending_side
                        position_stop = pending_stop
                        # entry_price tracks the actual fill; the initial stop stays
                        # anchored at the IRB wick (used for the breakeven R unit).
                        position_entry_price = fill_px
                        position_initial_stop = pending_stop
                        position_best = candle.close
                        breakeven_hit = False
                        # Risk-fraction position sizing (Nova engine.py:816-821):
                        # volume = equity*risk_fraction / (stop_pips * lot_val),
                        # sized off the signal-time stop distance, clamped + rounded.
                        stop_dist_pips = abs(pending_entry - pending_stop) / pip
                        if stop_dist_pips > 0:
                            volume = (equity * env.risk_fraction) / (stop_dist_pips * lot_val)
                            volume = max(env.min_volume, min(env.max_volume, round(volume, 2)))
                        else:
                            volume = env.min_volume
                        position_volume = volume
                        size[i] = volume * 100_000.0
                        trades_today += 1
                    pending_side = None
                    continue
                if i - pending_bar >= env.trigger_window_bars:
                    pending_side = None

            if env.cooldown_bars > 0 and (i - last_flat_bar) <= env.cooldown_bars:
                continue
            if env.max_trades_per_day > 0 and trades_today >= env.max_trades_per_day:
                continue

            signal_side = "LONG" if long_entries[i] else "SHORT" if short_entries[i] else None
            if signal_side is None:
                continue
            if pending_side is not None and pending_side != signal_side:
                continue
            pending_side = signal_side
            pending_entry = float(entry_prices[i])
            pending_stop = float(stop_losses[i])
            pending_bar = i

        # End-of-data: force-close any still-open position at the final bar, the
        # way Nova's run() closes the trailing position at the last close. Without
        # this, vectorbt auto-closes the dangling position at the raw close with no
        # cost haircut — the source of a small terminal PnL/sign discrepancy.
        if in_position and n > 0:
            last = n - 1
            equity += _emit_exit(
                last,
                candles[last].close,
                position_side == "LONG",
                position_entry_price,
                position_volume,
            )

        return {
            "long_entries": f_long,
            "short_entries": f_short,
            "long_exits": x_long,
            "short_exits": x_short,
            "price": price,
            "size": size,
        }

    def _normalize_trades(self, trade_records, ts_to_bar=None) -> list[NormalizedTrade]:
        if trade_records is None:
            return []
        trades: list[NormalizedTrade] = []
        try:
            for i in range(len(trade_records)):
                row = trade_records.iloc[i]
                direction = row.get("Direction", "Long")
                entry_ts = row.get("Entry Timestamp", 0)
                exit_ts = row.get("Exit Timestamp", 0)
                if ts_to_bar is not None:
                    # records carry pandas Timestamps; map back to integer bar index
                    entry_idx = int(ts_to_bar.get(entry_ts, 0))
                    exit_idx = int(ts_to_bar.get(exit_ts, 0))
                else:
                    entry_idx = int(entry_ts) if not hasattr(entry_ts, "timestamp") else 0
                    exit_idx = int(exit_ts) if not hasattr(exit_ts, "timestamp") else 0
                trades.append(
                    NormalizedTrade(
                        trade_id=i + 1,
                        side="LONG" if direction == "Long" else "SHORT",
                        entry_bar=entry_idx,
                        exit_bar=exit_idx,
                        entry_price=float(row.get("Avg Entry Price", 0)),
                        exit_price=float(row.get("Avg Exit Price", 0)),
                        stop_loss=0.0,
                        pnl_pips=float(row.get("PnL", 0)) / 10.0,
                        pnl_usd=float(row.get("PnL", 0)),
                        hold_bars=max(0, exit_idx - entry_idx),
                        exit_reason="trailing_stop",
                    )
                )
        except Exception:  # noqa: S110
            pass
        return trades

    def _extract_metrics(self, stats, pf) -> NormalizedMetrics:
        import math

        def _get(key, default=None):
            try:
                v = stats[key]
                if isinstance(v, float) and math.isnan(v):
                    return default
                return v
            except (KeyError, IndexError):
                return default

        avail = {
            "total_trades": MetricAvailability.AVAILABLE,
            "win_rate": MetricAvailability.AVAILABLE,
            "net_pnl_usd": MetricAvailability.AVAILABLE,
            "max_drawdown_pct": MetricAvailability.AVAILABLE,
            "sharpe_ratio": MetricAvailability.AVAILABLE,
            "sortino_ratio": MetricAvailability.AVAILABLE,
            "net_pnl_pips": MetricAvailability.NOT_COMPUTED,
        }

        # vectorbt computes Max Drawdown % against init_cash, which we inflate to
        # fund unleveraged FX positions. Recompute against initial_equity by
        # rebasing the equity curve (absolute $ drawdown is leverage-invariant) so
        # the % is comparable to Nova's compounding-equity drawdown.
        max_dd_pct = abs(float(_get("Max Drawdown [%]", 0) or 0))
        init_cash = getattr(self, "_init_cash", None)
        base = getattr(self, "_initial_equity", None)
        if pf is not None and init_cash and base:
            try:
                import numpy as np

                eq = np.asarray(pf.value().values, dtype=float) - float(init_cash) + float(base)
                peak = np.maximum.accumulate(eq)
                dd = np.where(peak > 0, (peak - eq) / peak, 0.0)
                max_dd_pct = float(np.max(dd) * 100.0)
            except Exception:  # noqa: S110
                pass

        return NormalizedMetrics(
            total_trades=int(_get("Total Trades", 0) or 0),
            win_rate=float(_get("Win Rate [%]", 0) or 0) / 100.0,  # Convert % to fraction
            net_pnl_usd=float(pf.total_profit()) if pf is not None else 0.0,
            profit_factor=min(float(_get("Profit Factor", 0) or 0), 999.0),
            max_drawdown_pct=max_dd_pct,
            sharpe_ratio=float(_get("Sharpe Ratio", 0) or 0),
            sortino_ratio=float(_get("Sortino Ratio", 0) or 0),
            availability=avail,
        )
