"""Cost stress testing — verify strategies survive inflated transaction costs.

Doubles the spread and adds extra slippage to simulate adverse market
conditions (low liquidity, fast-moving news).  A robust strategy should
remain profitable under stressed costs; if not, the edge is too thin.

``delayed_entry_bars`` is declared but not yet wired — it will require
engine-level changes to shift fill bars forward.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import (
    DEFAULT_ENVIRONMENT,
    BacktestEnvironment,
)
from novatrade.backtest.metrics import EVAL_WINDOWS, compute_metrics
from novatrade.cli.config_schema import StrategyConfig
from novatrade.evaluation.fitness import compute_scout_score
from novatrade.models import Candle

log = logging.getLogger("novatrade.optimization.stress")

# ---------------------------------------------------------------------------
# Config and result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StressConfig:
    """Cost stress test parameters.

    ``delayed_entry_bars`` is a stub — noted here for future implementation.
    Delaying entry by N bars requires engine modifications to shift the fill
    bar forward, which is out of scope for Phase 3.
    """

    spread_multiplier: float = 2.0  # 2x normal spread
    extra_slippage_pips: float = 1.0  # additional slippage on top of base
    delayed_entry_bars: int = 1  # stub: delay entry by 1 bar (not wired)


@dataclass
class StressResult:
    """Outcome of a cost stress test."""

    normal_score: float
    stress_score: float
    still_profitable: bool
    score_degradation_pct: float
    passed: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_stressed_environment(
    config: StrategyConfig,
    stress: StressConfig,
) -> BacktestEnvironment:
    """Create a BacktestEnvironment with inflated costs."""
    env_kwargs = config.to_environment_kwargs()
    base_env = replace(DEFAULT_ENVIRONMENT, **env_kwargs)

    stressed_spread = replace(
        base_env.spread,
        avg_spread_pips=base_env.spread.avg_spread_pips * stress.spread_multiplier,
        slippage_pips=base_env.spread.slippage_pips + stress.extra_slippage_pips,
    )
    return replace(base_env, spread=stressed_spread)


def _run_backtest_score(
    env: BacktestEnvironment,
    h1_candles: list[Candle],
    h4_candles: list[Candle],
) -> float:
    """Run a backtest with the given environment and return scout score."""
    bt = IRBBacktester(env=env)
    result = bt.run(h1_candles, h4_candles)

    window_idx = min(2, len(EVAL_WINDOWS) - 1)
    metrics = compute_metrics(
        trades=result.trades,
        pending_orders=result.pending_orders,
        signals=result.signals,
        filter_rejections=result.filter_rejections,
        window=EVAL_WINDOWS[window_idx],
        total_bars=result.total_bars,
        initial_equity=env.initial_equity,
    )
    return compute_scout_score(metrics)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_stress_test(
    config: StrategyConfig,
    h1_candles: list[Candle],
    h4_candles: list[Candle],
    normal_score: float,
    stress_config: StressConfig | None = None,
) -> StressResult:
    """Run a cost stress test on *config*.

    Inflates spread and slippage per ``stress_config``, runs the backtest,
    and compares the stressed scout score against *normal_score*.

    Args:
        config: Strategy configuration under test.
        h1_candles: H1 candle series.
        h4_candles: H4 candle series.
        normal_score: Scout score from a standard-cost backtest.
        stress_config: Stress parameters (defaults to ``StressConfig()``).

    Returns:
        ``StressResult`` with degradation metrics and pass/fail.
    """
    sc = stress_config or StressConfig()
    stressed_env = _build_stressed_environment(config, sc)

    stress_score = _run_backtest_score(stressed_env, h1_candles, h4_candles)
    still_profitable = stress_score > 0

    if normal_score > 0:
        degradation = (normal_score - stress_score) / normal_score
    else:
        degradation = 0.0 if stress_score >= 0 else 1.0

    degradation_pct = max(degradation, 0.0)
    passed = still_profitable

    log.info(
        "Stress test: normal=%.4f stress=%.4f degradation=%.1f%% profitable=%s %s",
        normal_score,
        stress_score,
        degradation_pct * 100,
        still_profitable,
        "PASS" if passed else "FAIL",
    )

    return StressResult(
        normal_score=normal_score,
        stress_score=stress_score,
        still_profitable=still_profitable,
        score_degradation_pct=round(degradation_pct, 4),
        passed=passed,
    )
