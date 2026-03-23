"""Native engine adapter — wraps the existing IRBBacktester as the control engine."""

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
from novatrade.backtest.engine import BacktestResult, IRBBacktester
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.backtest.metrics import (
    BacktestMetrics,
    CompletedTrade,
    EvaluationWindow,
    compute_metrics,
)
from novatrade.models import Candle


class NovaEngineAdapter(BaseEngineAdapter):
    """Adapter wrapping the existing IRBBacktester as the control engine."""

    @property
    def engine_id(self) -> EngineId:
        return EngineId.NOVA

    @property
    def engine_version(self) -> str:
        return "IRBBacktester 2.0.0"

    def is_available(self) -> bool:
        return True  # always available — it's our own engine

    def run(
        self,
        h1_candles: list[Candle],
        h4_candles: list[Candle],
        env: BacktestEnvironment,
    ) -> EngineResult:
        t0 = time.monotonic()
        try:
            bt = IRBBacktester(env=env)
            result = bt.run(h1_candles, h4_candles)

            trades = self._normalize_trades(result.trades)
            metrics = self._compute_normalized_metrics(result, env)

            return EngineResult(
                engine=self.engine_id,
                engine_version=self.engine_version,
                trades=trades,
                metrics=metrics,
                elapsed_seconds=time.monotonic() - t0,
                raw_result=result,
                config_notes=["control engine — bar-by-bar state machine"],
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
    def _normalize_trades(trades: list[CompletedTrade]) -> list[NormalizedTrade]:
        out: list[NormalizedTrade] = []
        for t in trades:
            out.append(
                NormalizedTrade(
                    trade_id=t.trade_id,
                    side=t.side.value,
                    entry_bar=t.entry_bar,
                    exit_bar=t.exit_bar,
                    entry_price=t.entry_price,
                    exit_price=t.exit_price,
                    stop_loss=t.stop_loss,
                    pnl_pips=t.pnl_pips,
                    pnl_usd=t.pnl_usd,
                    hold_bars=t.hold_bars,
                    exit_reason=t.exit_reason.value.lower(),
                )
            )
        return out

    @staticmethod
    def _compute_normalized_metrics(
        result: BacktestResult, env: BacktestEnvironment
    ) -> NormalizedMetrics:
        window = EvaluationWindow("full", 9999, "Full dataset")
        bm: BacktestMetrics = compute_metrics(
            trades=result.trades,
            pending_orders=result.pending_orders,
            signals=result.signals,
            filter_rejections=result.filter_rejections,
            window=window,
            total_bars=result.total_bars,
            initial_equity=env.initial_equity,
        )
        return NormalizedMetrics(
            total_trades=bm.total_completed_trades,
            long_trades=bm.long_trades,
            short_trades=bm.short_trades,
            win_rate=bm.win_rate,
            net_pnl_pips=bm.net_result_pips,
            net_pnl_usd=bm.net_result_usd,
            profit_factor=bm.profit_factor,
            max_drawdown_pct=bm.max_drawdown_pct,
            max_drawdown_usd=bm.max_drawdown_usd,
            sharpe_ratio=bm.sharpe_ratio,
            sortino_ratio=bm.sortino_ratio,
            avg_trade_pips=bm.average_trade_pips,
            avg_hold_bars=bm.avg_hold_bars,
            exposure_pct=bm.exposure_pct,
            max_consecutive_wins=bm.max_consecutive_wins,
            max_consecutive_losses=bm.max_consecutive_losses,
            availability={
                k: MetricAvailability.AVAILABLE
                for k in [
                    "total_trades",
                    "win_rate",
                    "net_pnl_pips",
                    "net_pnl_usd",
                    "profit_factor",
                    "max_drawdown_pct",
                    "sharpe_ratio",
                    "sortino_ratio",
                ]
            },
        )
