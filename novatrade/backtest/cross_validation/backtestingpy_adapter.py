"""Adapter for kernc/backtesting.py.

pip install backtesting

Translation approach:
- Dynamically builds a backtesting.Strategy subclass that reimplements IRB logic
- Pre-computes H4 alignment as extra columns in the H1 DataFrame
- Uses buy(stop=..., sl=...) for stop-order entries
- Trailing stop: managed in next() by tracking position and calling position.close()

Fill model notes:
- backtesting.py checks stop orders on next bar, using high/low for intra-bar fills
- Commission set as fraction via Backtest(commission=...)
- No native slippage — added to effective spread
"""

from __future__ import annotations

import math
import time

from novatrade.backtest.cross_validation.base_adapter import BaseEngineAdapter
from novatrade.backtest.cross_validation.types import (
    EngineId,
    EngineResult,
    MetricAvailability,
    NormalizedMetrics,
    NormalizedTrade,
)
from novatrade.backtest.engine import compute_adx, compute_atr, compute_ema
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle


class BacktestingPyAdapter(BaseEngineAdapter):
    """Adapter for kernc/backtesting.py."""

    @property
    def engine_id(self) -> EngineId:
        return EngineId.BACKTESTING_PY

    @property
    def engine_version(self) -> str:
        try:
            import backtesting

            return f"backtesting.py {backtesting.__version__}"
        except Exception:
            return "backtesting.py (unknown)"

    def is_available(self) -> bool:
        try:
            import backtesting  # noqa: F401

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
            import numpy as np  # noqa: F401 – used in _build_strategy_class closure
            from backtesting import Backtest, Strategy  # noqa: F401
            from backtesting.lib import crossover  # noqa: F401

            df = self._prepare_dataframe(h1_candles, h4_candles, env)

            # Cost model — match Nova's per-trade pip cost (spread + slippage +
            # the $/lot commission expressed as a constant pip cost). backtesting.py
            # charges `commission` per SIDE as a fraction of trade value, so a
            # round-turn 2·f·notional must equal cost_pips·pip·units → solve for f.
            avg_price = float(df["Close"].mean())
            lot_val = env.pip_value_per_standard_lot
            cost_pips = env.spread.total_cost_pips + (env.spread.commission_per_lot_usd / lot_val if lot_val else 0.0)
            commission_frac = (cost_pips * env.pip_value) / (2.0 * avg_price) if avg_price else 0.0

            # backtesting.py is a genuinely independent EVENT-DRIVEN engine: it runs
            # Nova's strategy with its own fill engine (risk-fraction sizing, breakeven
            # + ATR-trail stop ratcheting). Two independent intra-bar fill engines
            # phase-drift slightly on exit timing, so the trade count is close but not
            # identical to Nova — by design, this surfaces execution-model sensitivity.
            StrategyClass = self._build_strategy_class(env)

            bt = Backtest(
                df,
                StrategyClass,
                cash=env.initial_equity,
                commission=commission_frac,
                # Native margin → high leverage so risk-sized FX positions are
                # affordable while equity/drawdown stay relative to initial_equity.
                margin=0.001,
                exclusive_orders=True,
                trade_on_close=False,
            )
            stats = bt.run()

            trades = self._normalize_stats(stats)
            metrics = self._extract_metrics(stats)

            return EngineResult(
                engine=self.engine_id,
                engine_version=self.engine_version,
                trades=trades,
                metrics=metrics,
                elapsed_seconds=time.monotonic() - t0,
                raw_result=stats,
                config_notes=[
                    "independent event-driven engine: own fills, risk-fraction sizing,"
                    " breakeven + ATR-trail stop ratcheting",
                    "trade count phase-drifts vs Nova (independent intra-bar fills)",
                    "per-trade spread+commission cost via commission fraction",
                    f"effective commission fraction: {commission_frac:.6f}",
                ],
            )
        except ImportError:
            return EngineResult(
                engine=self.engine_id,
                engine_version=self.engine_version,
                elapsed_seconds=time.monotonic() - t0,
                error="backtesting.py not installed (pip install backtesting)",
            )
        except Exception as exc:
            return EngineResult(
                engine=self.engine_id,
                engine_version=self.engine_version,
                elapsed_seconds=time.monotonic() - t0,
                error=str(exc),
            )

    def _prepare_dataframe(
        self,
        h1_candles: list[Candle],
        h4_candles: list[Candle],
        env: BacktestEnvironment,
    ):
        """Build DataFrame with OHLCV + pre-computed IRB signal columns.

        Pre-computes signals using the full Nova engine filter chain:
        IRB range-zone geometry, EMA trend, H4 MTF, ADX gate, overextension.
        """
        import numpy as np

        df = self._candles_to_dataframe(h1_candles)
        n = len(h1_candles)
        closes = [c.close for c in h1_candles]

        ema = compute_ema(closes, env.ema_period)
        ema_fast = compute_ema(closes, env.ema_fast_period)
        ema_slow = compute_ema(closes, env.ema_slow_period)
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

        sig_long = np.zeros(n, dtype=bool)
        sig_short = np.zeros(n, dtype=bool)
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

            spread_cushion = max(0.0, env.sl_spread_buffer_pips) * env.pip_value
            if side == "LONG":
                entry = bar.high + env.pip_buffer
                sl = bar.low - env.pip_buffer - spread_cushion
                if env.atr_sl_floor_multiplier > 0:
                    min_sl = atr[i] * env.atr_sl_floor_multiplier
                    if (entry - sl) < min_sl:
                        sl = entry - min_sl
                sig_long[i] = True
            else:
                entry = bar.low - env.pip_buffer
                sl = bar.high + env.pip_buffer + spread_cushion
                if env.atr_sl_floor_multiplier > 0:
                    min_sl = atr[i] * env.atr_sl_floor_multiplier
                    if (sl - entry) < min_sl:
                        sl = entry + min_sl
                sig_short[i] = True
            entry_prices[i] = entry
            stop_losses[i] = sl

        df["Signal_Long"] = sig_long
        df["Signal_Short"] = sig_short
        df["Entry_Price"] = entry_prices
        df["Stop_Loss"] = stop_losses
        df["ATR"] = atr

        return df

    def _build_strategy_class(self, env: BacktestEnvironment):
        """Dynamically create a backtesting.Strategy subclass for IRB.

        Signal logic is pre-computed in _prepare_dataframe using the full Nova
        engine filter chain. Execution mirrors Nova's state machine so backtesting.py
        reproduces the same trades AND money via its OWN independent fill engine:
        risk-fraction sizing off live equity, one position at a time, and exits by
        RATCHETING the live trade's stop (breakeven at 1R, then ATR close-anchor
        trail) so backtesting.py fills intra-bar at the ratcheted level like Nova.
        """
        from backtesting import Strategy

        _env = env
        _pip = env.pip_value
        _lot_val = env.pip_value_per_standard_lot

        class IRBCrossValidation(Strategy):
            """IRB strategy using pre-computed Nova-equivalent signals."""

            trail_atr_mult = _env.trail_atr_multiplier
            time_stop_bars = _env.time_stop_bars
            cooldown_bars = _env.cooldown_bars
            max_trades_per_day = _env.max_trades_per_day
            breakeven_r = _env.breakeven_r
            trail_delay_bars = _env.trail_delay_bars
            risk_fraction = _env.risk_fraction
            min_volume = _env.min_volume
            max_volume = _env.max_volume

            def init(self):
                self.atr = self.I(lambda: self.data.ATR, name="ATR")
                self._bars_in_trade = 0
                self._bar_index = -1
                self._last_exit_bar = -9999
                self._had_position = False
                self._trades_today = 0
                self._last_day_ord = -1
                self._entry_price = 0.0
                self._initial_sl = 0.0
                self._best_close = 0.0
                self._be_hit = False

            def _set_sl(self, trade, value, is_long):
                # backtesting.py rejects a stop on the wrong side of the last price;
                # only ratchet in the protective direction and stay valid.
                price = self.data.Close[-1]
                if is_long and value >= price:
                    return
                if (not is_long) and value <= price:
                    return
                try:
                    cur = trade.sl
                    if cur is None or (is_long and value > cur) or ((not is_long) and value < cur):
                        trade.sl = value
                except Exception:  # noqa: S110
                    pass

            def next(self):
                import numpy as np

                self._bar_index += 1

                # Day-boundary reset for daily trade counter
                if self.max_trades_per_day > 0:
                    try:
                        day_ord = self.data.index[-1].toordinal()
                    except Exception:
                        day_ord = self._bar_index // 24
                    if day_ord != self._last_day_ord:
                        self._last_day_ord = day_ord
                        self._trades_today = 0

                # Detect position exit (framework SL or our explicit close)
                if self._had_position and not self.position:
                    self._last_exit_bar = self._bar_index
                self._had_position = bool(self.position)

                if not self.position:
                    self._bars_in_trade = 0

                    # v5: Cooldown bars — match Nova engine.py:605-612
                    if self.cooldown_bars > 0 and (self._bar_index - self._last_exit_bar) <= self.cooldown_bars:
                        return

                    # v5: Daily trade limit — match Nova engine.py:600-603
                    if self.max_trades_per_day > 0 and self._trades_today >= self.max_trades_per_day:
                        return

                    long_sig = bool(self.data.Signal_Long[-1])
                    short_sig = bool(self.data.Signal_Short[-1])
                    if not (long_sig or short_sig):
                        return

                    entry = float(self.data.Entry_Price[-1])
                    sl = float(self.data.Stop_Loss[-1])
                    stop_dist = abs(entry - sl)
                    if stop_dist <= 0:
                        return

                    # Risk-fraction sizing off live equity (Nova engine.py:816-821)
                    stop_pips = stop_dist / _pip
                    volume = (self.equity * self.risk_fraction) / (stop_pips * _lot_val)
                    volume = max(self.min_volume, min(self.max_volume, round(volume, 2)))
                    units = round(volume * 100_000)
                    if units < 1:
                        return

                    self._entry_price = entry
                    self._initial_sl = sl
                    self._best_close = float(self.data.Close[-1])
                    self._be_hit = False

                    if long_sig and entry > sl:
                        self.buy(stop=entry, sl=sl, size=units)
                        self._trades_today += 1
                    elif short_sig and sl > entry:
                        self.sell(stop=entry, sl=sl, size=units)
                        self._trades_today += 1
                else:
                    self._bars_in_trade += 1

                    # Time stop — Nova closes at market on the ceiling bar
                    if self._bars_in_trade >= self.time_stop_bars:
                        self.position.close()
                        return

                    trade = self.trades[-1]
                    is_long = self.position.is_long
                    close = float(self.data.Close[-1])

                    # Breakeven: move stop to entry +/- pip after breakeven_r * R
                    if self.breakeven_r > 0 and not self._be_hit:
                        target = abs(self._entry_price - self._initial_sl) * self.breakeven_r
                        if is_long and (close - self._entry_price) >= target:
                            self._set_sl(trade, self._entry_price + _pip, True)
                            self._be_hit = True
                        elif (not is_long) and (self._entry_price - close) >= target:
                            self._set_sl(trade, self._entry_price - _pip, False)
                            self._be_hit = True

                    # ATR trailing (close anchor); ratchet the live stop so the
                    # framework fills intra-bar at the trailed level.
                    atr_val = self.atr[-1]
                    if (
                        not np.isnan(atr_val)
                        and atr_val > 0
                        and (self.trail_delay_bars <= 0 or self._bars_in_trade >= self.trail_delay_bars)
                    ):
                        if is_long:
                            self._best_close = max(self._best_close, close)
                            self._set_sl(trade, self._best_close - atr_val * self.trail_atr_mult, True)
                        else:
                            self._best_close = min(self._best_close, close)
                            self._set_sl(trade, self._best_close + atr_val * self.trail_atr_mult, False)

        return IRBCrossValidation

    def _normalize_stats(self, stats) -> list[NormalizedTrade]:
        """Convert backtesting.py trade list to NormalizedTrade."""
        trades: list[NormalizedTrade] = []
        trade_list = stats._trades if hasattr(stats, "_trades") else []
        for i, t in enumerate(trade_list.itertuples() if hasattr(trade_list, "itertuples") else []):
            trades.append(
                NormalizedTrade(
                    trade_id=i + 1,
                    side="LONG" if getattr(t, "Size", 0) > 0 else "SHORT",
                    entry_bar=getattr(t, "EntryBar", 0),
                    exit_bar=getattr(t, "ExitBar", 0),
                    entry_price=getattr(t, "EntryPrice", 0.0),
                    exit_price=getattr(t, "ExitPrice", 0.0),
                    stop_loss=getattr(t, "SL", 0.0),
                    pnl_pips=getattr(t, "PnL", 0.0) / 10.0,  # approximate USD→pips
                    pnl_usd=getattr(t, "PnL", 0.0),
                    hold_bars=getattr(t, "ExitBar", 0) - getattr(t, "EntryBar", 0),
                    exit_reason="stop_loss",  # backtesting.py doesn't distinguish
                )
            )
        return trades

    def _extract_metrics(self, stats) -> NormalizedMetrics:
        """Extract normalised metrics from backtesting.py stats."""
        avail = {
            "total_trades": MetricAvailability.AVAILABLE,
            "win_rate": MetricAvailability.AVAILABLE,
            "net_pnl_usd": MetricAvailability.AVAILABLE,
            "max_drawdown_pct": MetricAvailability.AVAILABLE,
            "sharpe_ratio": MetricAvailability.AVAILABLE,
            "profit_factor": MetricAvailability.AVAILABLE,
            "sortino_ratio": MetricAvailability.NOT_COMPUTED,
        }

        # Safe getattr for stats which is a pd.Series
        def _get(key, default=None):
            try:
                return stats[key]
            except (KeyError, IndexError):
                return default

        total_trades = _get("# Trades", 0)
        win_rate_raw = _get("Win Rate [%]", 0.0)

        return NormalizedMetrics(
            total_trades=int(total_trades) if total_trades else 0,
            win_rate=float(win_rate_raw) / 100.0 if win_rate_raw else 0.0,  # Convert % to fraction
            net_pnl_usd=_get("Return [$]", _get("Equity Final [$]", 100_000.0) - 100_000.0),
            profit_factor=_get("Profit Factor", 0.0) or 0.0,
            max_drawdown_pct=abs(_get("Max. Drawdown [%]", 0.0) or 0.0),
            sharpe_ratio=_get("Sharpe Ratio", 0.0) or 0.0,
            avg_trade_pips=(_get("Avg. Trade [%]", 0.0) or 0.0) * 100,  # approx
            exposure_pct=_get("Exposure Time [%]", 0.0) or 0.0,
            availability=avail,
        )
