"""Filter ablation testing — measure individual filter contribution.

Removes each filter one at a time and re-runs the backtest to determine
whether that filter helps or hurts the scout score.  Dead-weight filters
(those that hurt when present) are surfaced first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import DEFAULT_ENVIRONMENT
from novatrade.backtest.metrics import EVAL_WINDOWS, compute_metrics
from novatrade.cli.config_schema import StrategyConfig
from novatrade.evaluation.fitness import compute_scout_score
from novatrade.models import Candle

log = logging.getLogger("novatrade.optimization.ablation")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class AblationResult:
    """Outcome of removing a single filter from the strategy."""

    filter_name: str
    baseline_score: float
    ablated_score: float
    score_change: float  # positive = filter was hurting, negative = filter was helping
    filter_contributes: bool  # True if removing filter reduces score


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_ablation(
    config: StrategyConfig,
    h1_candles: list[Candle],
    h4_candles: list[Candle],
    baseline_score: float,
) -> list[AblationResult]:
    """Run ablation test for each enabled filter.

    For each filter in ``config.filters_enabled``, creates a copy of the
    config with that filter removed, runs a full backtest, and compares the
    resulting scout score to the baseline.

    When no Level 2 filters are enabled (Level 1 only), returns an empty
    list since there is nothing to ablate.

    Args:
        config: Strategy configuration with filters_enabled populated.
        h1_candles: H1 OHLCV candle series.
        h4_candles: H4 OHLCV candle series.
        baseline_score: Scout score of the unmodified config.

    Returns:
        List of AblationResult sorted by score_change descending
        (dead-weight filters first).
    """
    if not config.filters_enabled:
        log.info("No filters enabled — ablation skipped (Level 1 only)")
        return []

    # Evaluation window — broadest available
    window_idx = min(2, len(EVAL_WINDOWS) - 1)
    eval_window = EVAL_WINDOWS[window_idx]

    results: list[AblationResult] = []

    for filter_name in config.filters_enabled:
        ablated_filters = [f for f in config.filters_enabled if f != filter_name]
        ablated_config = config.model_copy(update={"filters_enabled": ablated_filters})

        try:
            env = replace(DEFAULT_ENVIRONMENT, **ablated_config.to_environment_kwargs())
            bt = IRBBacktester(env=env)
            bt_result = bt.run(h1_candles, h4_candles)

            metrics = compute_metrics(
                trades=bt_result.trades,
                pending_orders=bt_result.pending_orders,
                signals=bt_result.signals,
                filter_rejections=bt_result.filter_rejections,
                window=eval_window,
                total_bars=bt_result.total_bars,
                initial_equity=env.initial_equity,
            )

            ablated_score = compute_scout_score(metrics)
        except Exception as exc:
            log.warning("Ablation for filter '%s' crashed: %s", filter_name, exc)
            ablated_score = 0.0

        change = ablated_score - baseline_score
        contributes = ablated_score < baseline_score

        results.append(
            AblationResult(
                filter_name=filter_name,
                baseline_score=baseline_score,
                ablated_score=ablated_score,
                score_change=round(change, 6),
                filter_contributes=contributes,
            )
        )

        log.info(
            "Ablation '%s': baseline=%.4f ablated=%.4f change=%+.4f %s",
            filter_name,
            baseline_score,
            ablated_score,
            change,
            "HELPS" if contributes else "DEAD WEIGHT",
        )

    # Sort: dead-weight first (positive change = filter was hurting)
    results.sort(key=lambda r: r.score_change, reverse=True)
    return results
