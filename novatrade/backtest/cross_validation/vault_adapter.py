"""Vault-reference engine adapter.

Wraps vault Python's validated IRB strategy core
(``novatrade.strategies.vault_irb_state``) — the independent reference
implementation that flagged the historical nova engine-drift — as a
cross-validation engine. Unlike the nova/vectorbt/backtesting.py/nautilus
adapters (which all replay nova's H1 IRB filter chain), this runs the vault's
OWN bar-by-bar state machine, so it is a genuinely independent strategy engine.

Ledger note: the vault core is zero-cost (no spread/slippage/commission), so its
PnL is gross. Nova-chain engines model cost; expect that systematic difference.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

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


class VaultEngineAdapter(BaseEngineAdapter):
    """Adapter wrapping vault Python's IRBStrategyState as a reference engine."""

    @property
    def engine_id(self) -> EngineId:
        return EngineId.VAULT

    @property
    def engine_version(self) -> str:
        return "vault-irb (IRBStrategyState)"

    def is_available(self) -> bool:
        try:
            from novatrade.strategies.vault_irb_state import run_vault_strategy_batch  # noqa: F401

            return True
        except Exception:
            return False

    def run(
        self,
        h1_candles: list[Candle],
        h4_candles: list[Candle],
        env: BacktestEnvironment,
    ) -> EngineResult:
        t0 = time.monotonic()
        try:
            import pandas as pd

            from novatrade.strategies.vault_irb_state import run_vault_strategy_batch
            from novatrade.strategy.vault_engine import env_to_irb_config

            raw = pd.DataFrame(
                {
                    "time": [datetime.fromtimestamp(c.timestamp, tz=timezone.utc) for c in h1_candles],
                    "open": [c.open for c in h1_candles],
                    "high": [c.high for c in h1_candles],
                    "low": [c.low for c in h1_candles],
                    "close": [c.close for c in h1_candles],
                }
            )
            cfg = env_to_irb_config(env)
            res = run_vault_strategy_batch(raw, cfg)

            ts_to_bar = {datetime.fromtimestamp(c.timestamp, tz=timezone.utc): i for i, c in enumerate(h1_candles)}
            trades = self._normalize_trades(res["trades"], ts_to_bar, env.pip_value)
            metrics = self._compute_metrics(res, trades, env.initial_equity)

            return EngineResult(
                engine=self.engine_id,
                engine_version=self.engine_version,
                trades=trades,
                metrics=metrics,
                elapsed_seconds=time.monotonic() - t0,
                raw_result=res,
                config_notes=[
                    "vault Python IRBStrategyState — independent strategy core",
                    f"HTF={cfg.htf_timeframe}, risk_pct={cfg.risk_pct}, irb_percent={cfg.irb_percent}",
                    "zero-cost ledger (no spread/slippage/commission) — PnL is gross",
                ],
            )
        except Exception as exc:
            return EngineResult(
                engine=self.engine_id,
                engine_version=self.engine_version,
                elapsed_seconds=time.monotonic() - t0,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_trades(trades_df, ts_to_bar: dict, pip_value: float) -> list[NormalizedTrade]:
        """Pair the vault's entry/exit events into completed positions.

        A position opens on an ``entry`` event and closes on the first non-partial
        ``exit`` (partial_tp legs accumulate into the same position's PnL).
        """
        out: list[NormalizedTrade] = []
        if trades_df is None or len(trades_df) == 0:
            return out

        def _bar(t) -> int:
            try:
                if hasattr(t, "to_pydatetime"):
                    t = t.to_pydatetime()
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                return int(ts_to_bar.get(t, -1))
            except Exception:
                return -1

        cur: dict | None = None
        tid = 0
        for _, ev in trades_df.iterrows():
            etype = ev.get("type")
            if etype == "entry":
                cur = {
                    "side": str(ev.get("side", "long")).upper(),
                    "entry_bar": _bar(ev.get("time")),
                    "entry_price": float(ev.get("price", 0.0)),
                    "qty": float(ev.get("qty", 0.0)),
                    "pnl": 0.0,
                    "exit_bar": -1,
                    "exit_price": 0.0,
                    "reason": "",
                }
            elif etype == "exit" and cur is not None:
                pnl = ev.get("pnl")
                cur["pnl"] += float(pnl) if pnl is not None else 0.0
                cur["exit_bar"] = _bar(ev.get("time"))
                cur["exit_price"] = float(ev.get("price", 0.0))
                cur["reason"] = str(ev.get("reason") or "")
                if cur["reason"] != "partial_tp":
                    tid += 1
                    # pnl in pips: per-unit price move / pip; qty is in units
                    pnl_pips = (cur["pnl"] / cur["qty"] / pip_value) if cur["qty"] else 0.0
                    out.append(
                        NormalizedTrade(
                            trade_id=tid,
                            side=cur["side"],
                            entry_bar=cur["entry_bar"],
                            exit_bar=cur["exit_bar"],
                            entry_price=cur["entry_price"],
                            exit_price=cur["exit_price"],
                            stop_loss=0.0,
                            pnl_pips=pnl_pips,
                            pnl_usd=cur["pnl"],
                            hold_bars=max(0, cur["exit_bar"] - cur["entry_bar"]),
                            exit_reason=cur["reason"] or "runner_stop",
                        )
                    )
                    cur = None
        return out

    @staticmethod
    def _compute_metrics(res, trades: list[NormalizedTrade], initial_equity: float) -> NormalizedMetrics:
        total = len(trades)
        longs = sum(1 for t in trades if t.side == "LONG")
        shorts = total - longs
        wins = sum(1 for t in trades if t.pnl_usd > 0)
        win_rate = (wins / total) if total else 0.0
        net_pnl_usd = float(res.get("net_profit", sum(t.pnl_usd for t in trades)))
        gross_profit = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
        gross_loss = abs(sum(t.pnl_usd for t in trades if t.pnl_usd < 0))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        # Max drawdown % from the realised equity curve (initial + running PnL).
        eq = initial_equity
        peak = initial_equity
        max_dd = 0.0
        for t in trades:
            eq += t.pnl_usd
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)
        max_drawdown_pct = max_dd * 100.0

        avail = {
            k: MetricAvailability.AVAILABLE
            for k in ["total_trades", "win_rate", "net_pnl_usd", "profit_factor", "max_drawdown_pct"]
        }
        avail["sharpe_ratio"] = MetricAvailability.NOT_COMPUTED
        avail["sortino_ratio"] = MetricAvailability.NOT_COMPUTED

        return NormalizedMetrics(
            total_trades=total,
            long_trades=longs,
            short_trades=shorts,
            win_rate=win_rate,
            net_pnl_usd=net_pnl_usd,
            profit_factor=min(profit_factor, 999.0),
            max_drawdown_pct=max_drawdown_pct,
            avg_hold_bars=(sum(t.hold_bars for t in trades) / total) if total else 0.0,
            availability=avail,
        )
