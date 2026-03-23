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

import time

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


class VectorbtAdapter(BaseEngineAdapter):
    """Adapter for vectorbt."""

    @property
    def engine_id(self) -> EngineId:
        return EngineId.VECTORBT

    @property
    def engine_version(self) -> str:
        try:
            import vectorbt as vbt

            return f"vectorbt {vbt.__version__}"
        except Exception:
            return "vectorbt (unknown)"

    def is_available(self) -> bool:
        try:
            import vectorbt  # noqa: F401

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
            import vectorbt as vbt
            import numpy as np
            import pandas as pd

            from novatrade.backtest.engine import compute_atr, compute_ema

            df = self._candles_to_dataframe(h1_candles)
            closes = df["Close"].values
            highs = df["High"].values
            lows = df["Low"].values
            opens = df["Open"].values

            # Compute indicators
            ema_arr = np.array(compute_ema(closes.tolist(), env.ema_period))
            atr_arr = np.array(compute_atr(h1_candles, env.atr_period))

            # IRB signal detection (vectorized)
            body = np.abs(closes - opens)
            body = np.where(body < 1e-10, 1e-10, body)
            upper_wick = highs - np.maximum(opens, closes)
            lower_wick = np.minimum(opens, closes) - lows

            # Long signals: lower wick dominant + price below EMA
            long_entries = (lower_wick / body > env.irb_threshold) & (closes < ema_arr)
            # Short signals: upper wick dominant + price above EMA
            short_entries = (upper_wick / body > env.irb_threshold) & (closes > ema_arr)

            # Suppress signals during warmup
            warmup = max(env.ema_period, env.atr_period) + 10
            long_entries[:warmup] = False
            short_entries[:warmup] = False

            # Suppress NaN indicator periods
            nan_mask = np.isnan(ema_arr) | np.isnan(atr_arr)
            long_entries[nan_mask] = False
            short_entries[nan_mask] = False

            # Run portfolio simulation
            close_series = pd.Series(closes, index=df.index)

            # Combine into single direction for from_signals
            entries = long_entries | short_entries
            # For simplicity, treat all as long signals (vectorbt limitation)
            # A more accurate approach would use from_orders()

            sl_pct = float(np.nanmean(atr_arr[~np.isnan(atr_arr)])) / float(np.mean(closes)) * 2
            trail_pct = float(np.nanmean(atr_arr[~np.isnan(atr_arr)])) / float(np.mean(closes)) * env.trail_atr_multiplier

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
                    "vectorbt fills at next-bar close, not at stop-order price",
                    "trailing stop uses vectorbt sl_trail parameter",
                    "combined long/short signals may interact differently than event-driven",
                    f"sl_pct={sl_pct:.6f}, trail_pct={trail_pct:.6f}",
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

    def _normalize_trades(self, trade_records) -> list[NormalizedTrade]:
        if trade_records is None:
            return []
        trades: list[NormalizedTrade] = []
        try:
            for i, row in enumerate(trade_records.itertuples()):
                trades.append(
                    NormalizedTrade(
                        trade_id=i + 1,
                        side="LONG" if getattr(row, "Direction", "Long") == "Long" else "SHORT",
                        entry_bar=int(getattr(row, "Entry Idx", 0)),
                        exit_bar=int(getattr(row, "Exit Idx", 0)),
                        entry_price=float(getattr(row, "Avg Entry Price", 0)),
                        exit_price=float(getattr(row, "Avg Exit Price", 0)),
                        stop_loss=0.0,
                        pnl_pips=float(getattr(row, "PnL", 0)) / 10.0,
                        pnl_usd=float(getattr(row, "PnL", 0)),
                        hold_bars=int(getattr(row, "Exit Idx", 0)) - int(getattr(row, "Entry Idx", 0)),
                        exit_reason="trailing_stop",
                    )
                )
        except Exception:
            pass
        return trades

    def _extract_metrics(self, stats, pf) -> NormalizedMetrics:
        def _get(key, default=None):
            try:
                return stats[key]
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
            win_rate=float(_get("Win Rate [%]", 0) or 0),
            net_pnl_usd=float(_get("Total Return [$]", 0) or 0),
            profit_factor=float(_get("Profit Factor", 0) or 0),
            max_drawdown_pct=abs(float(_get("Max Drawdown [%]", 0) or 0)),
            sharpe_ratio=float(_get("Sharpe Ratio", 0) or 0),
            sortino_ratio=float(_get("Sortino Ratio", 0) or 0),
            availability=avail,
        )
