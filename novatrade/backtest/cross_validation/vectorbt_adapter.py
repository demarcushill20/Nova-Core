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

            # Apply Nova-like pending-stop execution cadence before handing fill bars to vectorbt.
            long_entries, short_entries = self._apply_execution_filter(
                long_entries,
                short_entries,
                h1_candles,
                env,
                entry_prices,
                stop_losses,
            )

            close_series = pd.Series(closes, index=df.index)
            atr_arr = np.array(atr)
            valid_atr = atr_arr[~np.isnan(atr_arr)]
            sl_pct = (
                float(np.mean(valid_atr)) / float(np.mean(closes)) * env.trail_atr_multiplier
                if len(valid_atr)
                else 0.02
            )

            pf = vbt.Portfolio.from_signals(
                close=close_series,
                entries=pd.Series(long_entries, index=df.index),
                short_entries=pd.Series(short_entries, index=df.index),
                init_cash=env.initial_equity,
                sl_stop=sl_pct,
                sl_trail=True,
                freq="1h",
            )

            stats = pf.stats()
            trade_records = pf.trades.records_readable if hasattr(pf.trades, "records_readable") else None

            trades = self._normalize_trades(trade_records)
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
                    "vectorbt fills at next-bar close, not at stop-order price",
                    "trailing stop uses vectorbt sl_trail parameter",
                    f"sl_pct={sl_pct:.6f}",
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

        in_position = False
        position_entry_bar = -1
        last_flat_bar = -9999
        trades_today = 0
        last_day_ord = -1
        pending_side: str | None = None
        pending_entry = 0.0
        pending_bar = -1

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
                if i - position_entry_bar >= env.time_stop_bars:
                    in_position = False
                    last_flat_bar = i
                continue

            if pending_side is not None:
                filled = (pending_side == "LONG" and candle.high >= pending_entry) or (
                    pending_side == "SHORT" and candle.low <= pending_entry
                )
                if filled:
                    if env.max_trades_per_day <= 0 or trades_today < env.max_trades_per_day:
                        if pending_side == "LONG":
                            filtered_long[i] = True
                        else:
                            filtered_short[i] = True
                        in_position = True
                        position_entry_bar = i
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
            pending_bar = i

        return filtered_long, filtered_short

    def _normalize_trades(self, trade_records) -> list[NormalizedTrade]:
        if trade_records is None:
            return []
        trades: list[NormalizedTrade] = []
        try:
            for i in range(len(trade_records)):
                row = trade_records.iloc[i]
                direction = row.get("Direction", "Long")
                entry_ts = row.get("Entry Timestamp", 0)
                exit_ts = row.get("Exit Timestamp", 0)
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

        return NormalizedMetrics(
            total_trades=int(_get("Total Trades", 0) or 0),
            win_rate=float(_get("Win Rate [%]", 0) or 0) / 100.0,  # Convert % to fraction
            net_pnl_usd=float(pf.total_profit()) if pf is not None else 0.0,
            profit_factor=min(float(_get("Profit Factor", 0) or 0), 999.0),
            max_drawdown_pct=abs(float(_get("Max Drawdown [%]", 0) or 0)),
            sharpe_ratio=float(_get("Sharpe Ratio", 0) or 0),
            sortino_ratio=float(_get("Sortino Ratio", 0) or 0),
            availability=avail,
        )
