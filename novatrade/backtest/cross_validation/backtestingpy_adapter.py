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

import time

from novatrade.backtest.cross_validation.base_adapter import BaseEngineAdapter
from novatrade.backtest.cross_validation.types import (
    EngineId,
    EngineResult,
    MetricAvailability,
    NormalizedMetrics,
    NormalizedTrade,
)
from novatrade.backtest.engine import compute_atr, compute_ema
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

            # Build strategy class dynamically with env parameters baked in
            StrategyClass = self._build_strategy_class(env)

            # Commission: convert fixed cost to approximate fraction
            # ECN: commission_per_lot_usd / (avg_price * lot_size * pip_value)
            avg_price = df["Close"].mean()
            spread_cost = env.spread.avg_spread_pips * env.pip_value
            slippage_cost = env.spread.slippage_pips * env.pip_value
            commission_frac = (spread_cost + slippage_cost) / avg_price

            bt = Backtest(
                df,
                StrategyClass,
                cash=env.initial_equity,
                commission=commission_frac,
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
                    "commission modelled as fraction of trade value",
                    "slippage added to spread (no native slippage model)",
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
        """Build DataFrame with OHLCV + pre-computed indicator columns."""

        df = self._candles_to_dataframe(h1_candles)

        # Pre-compute H1 indicators
        closes = [c.close for c in h1_candles]
        df["EMA_fast"] = compute_ema(closes, env.ema_period)
        df["ATR"] = compute_atr(h1_candles, env.atr_period)

        # Pre-compute H4 EMA alignment as extra column
        if h4_candles:
            h4_closes = [c.close for c in h4_candles]
            h4_ema = compute_ema(h4_closes, env.ema_period)
            h4_ts = [c.timestamp for c in h4_candles]

            # Map H4 EMA to H1 bars by timestamp alignment
            h4_aligned = []
            h4_idx = 0
            for c in h1_candles:
                while h4_idx < len(h4_ts) - 1 and h4_ts[h4_idx + 1] <= c.timestamp:
                    h4_idx += 1
                h4_aligned.append(h4_ema[h4_idx] if h4_idx < len(h4_ema) else float("nan"))
            df["H4_EMA"] = h4_aligned
        else:
            df["H4_EMA"] = float("nan")

        return df

    def _build_strategy_class(self, env: BacktestEnvironment):
        """Dynamically create a backtesting.Strategy subclass for IRB."""
        from backtesting import Strategy

        # Capture env params via closure
        _env = env

        class IRBCrossValidation(Strategy):
            """IRB strategy reimplemented for backtesting.py."""

            # Parameters exposed for optimization
            ema_period = _env.ema_period
            atr_period = _env.atr_period
            irb_threshold = _env.irb_threshold
            trail_atr_mult = _env.trail_atr_multiplier
            trigger_window = _env.trigger_window_bars
            time_stop_bars = _env.time_stop_bars

            def init(self):
                # Indicators are pre-computed in DataFrame columns
                self.ema = self.I(lambda: self.data.EMA_fast, name="EMA")
                self.atr = self.I(lambda: self.data.ATR, name="ATR")
                self.h4_ema = self.I(lambda: self.data.H4_EMA, name="H4_EMA")
                self._bars_in_trade = 0
                self._trail_stop = 0.0

            def next(self):
                price = self.data.Close[-1]
                atr_val = self.atr[-1]
                ema_val = self.ema[-1]

                if not self.position:
                    self._bars_in_trade = 0
                    # Skip if indicators not ready
                    if np.isnan(atr_val) or np.isnan(ema_val):
                        return

                    bar = self.data
                    o, h, low, c = bar.Open[-1], bar.High[-1], bar.Low[-1], bar.Close[-1]
                    body = abs(c - o) or 1e-10
                    upper_wick = h - max(o, c)
                    lower_wick = min(o, c) - low

                    # IRB geometry: detect imbalance
                    if upper_wick / body > self.irb_threshold and c > ema_val:
                        # Bearish IRB (short signal) — upper wick dominant
                        entry = low - _env.pip_value
                        sl = h + atr_val * 0.5
                        if sl > entry:
                            self.sell(stop=entry, sl=sl)

                    elif lower_wick / body > self.irb_threshold and c < ema_val:
                        # Bullish IRB (long signal) — lower wick dominant
                        entry = h + _env.pip_value
                        sl = low - atr_val * 0.5
                        if entry > sl:
                            self.buy(stop=entry, sl=sl)
                else:
                    self._bars_in_trade += 1

                    # Time stop
                    if self._bars_in_trade >= self.time_stop_bars:
                        self.position.close()
                        return

                    # Simple ATR trailing stop
                    if not np.isnan(atr_val):
                        if self.position.is_long:
                            trail = price - atr_val * self.trail_atr_mult
                            if trail > self._trail_stop:
                                self._trail_stop = trail
                            if price <= self._trail_stop:
                                self.position.close()
                        else:
                            trail = price + atr_val * self.trail_atr_mult
                            if self._trail_stop == 0 or trail < self._trail_stop:
                                self._trail_stop = trail
                            if price >= self._trail_stop:
                                self.position.close()

        import numpy as np  # ensure available in closure

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
            win_rate=float(win_rate_raw) if win_rate_raw else 0.0,
            net_pnl_usd=_get("Equity Final [$]", 0.0) - 100_000.0,
            profit_factor=_get("Profit Factor", 0.0) or 0.0,
            max_drawdown_pct=abs(_get("Max. Drawdown [%]", 0.0) or 0.0),
            sharpe_ratio=_get("Sharpe Ratio", 0.0) or 0.0,
            avg_trade_pips=(_get("Avg. Trade [%]", 0.0) or 0.0) * 100,  # approx
            exposure_pct=_get("Exposure Time [%]", 0.0) or 0.0,
            availability=avail,
        )
