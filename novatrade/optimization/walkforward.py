"""Rolling walk-forward validation engine.

Splits candle data into overlapping train/test windows with a final holdout
set.  Each window runs a fresh backtest and computes an OOS/IS scout-score
ratio to detect overfitting.  The holdout set provides a final reality check.

Walk-forward layout (bar indices)::

    |-- train --|-- embargo --|-- test --|-- step -->|
    |-- train ----|-- embargo --|-- test --|-- step -->|
    ...
    holdout = last N bars, excluded from all windows
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field, replace

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import DEFAULT_ENVIRONMENT
from novatrade.backtest.metrics import EVAL_WINDOWS, compute_metrics
from novatrade.cli.config_schema import StrategyConfig
from novatrade.evaluation.fitness import compute_scout_score
from novatrade.models import Candle

log = logging.getLogger("novatrade.optimization.walkforward")

# ---------------------------------------------------------------------------
# Config and result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkForwardConfig:
    """Tuning knobs for walk-forward validation."""

    train_bars: int = 4380  # 6 months of H1 (24 * 182.5)
    test_bars: int = 1460  # 2 months of H1 (24 * 61)
    step_bars: int = 730  # 1 month rolling step (24 * 30.4)
    embargo_bars: int = 5  # gap between train and test
    holdout_bars: int = 2190  # 3 months final holdout (24 * 91.25)
    min_oos_is_ratio: float = 0.6
    min_holdout_ratio: float = 0.7


@dataclass
class WindowResult:
    """Outcome of a single train/test window."""

    window_index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    is_scout_score: float
    oos_scout_score: float
    oos_is_ratio: float
    test_trade_count: int
    passed: bool


@dataclass
class WalkForwardResult:
    """Aggregated walk-forward validation outcome."""

    windows: list[WindowResult] = field(default_factory=list)
    holdout_score: float | None = None
    holdout_is_ratio: float | None = None
    median_oos_score: float = 0.0
    median_oos_is_ratio: float = 0.0
    all_windows_passed: bool = False
    holdout_passed: bool | None = None
    overall_passed: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slice_h4(h1_start: int, h1_end: int, h4_len: int) -> tuple[int, int]:
    """Map H1 bar range to corresponding H4 bar range (4:1 ratio)."""
    h4_start = h1_start // 4
    h4_end = min(h1_end // 4, h4_len)
    return h4_start, h4_end


def _run_single_backtest(
    config: StrategyConfig,
    h1_slice: list[Candle],
    h4_slice: list[Candle],
) -> float:
    """Run a backtest on a candle slice and return the scout score."""
    if not h1_slice or not h4_slice:
        return 0.0

    env_kwargs = config.to_environment_kwargs()
    try:
        environment = replace(DEFAULT_ENVIRONMENT, **env_kwargs)
    except TypeError:
        return 0.0

    bt = IRBBacktester(env=environment)
    result = bt.run(h1_slice, h4_slice)

    window_idx = min(2, len(EVAL_WINDOWS) - 1)
    metrics = compute_metrics(
        trades=result.trades,
        pending_orders=result.pending_orders,
        signals=result.signals,
        filter_rejections=result.filter_rejections,
        window=EVAL_WINDOWS[window_idx],
        total_bars=result.total_bars,
        initial_equity=environment.initial_equity,
    )
    return compute_scout_score(metrics)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_walk_forward(
    config: StrategyConfig,
    h1_candles: list[Candle],
    h4_candles: list[Candle],
    wf_config: WalkForwardConfig | None = None,
) -> WalkForwardResult:
    """Run rolling walk-forward validation on *config* against candle data.

    Args:
        config: Strategy configuration to validate.
        h1_candles: Full H1 candle series.
        h4_candles: Full H4 candle series.
        wf_config: Walk-forward parameters (defaults to ``WalkForwardConfig()``).

    Returns:
        ``WalkForwardResult`` with per-window and aggregate outcomes.
    """
    wf = wf_config or WalkForwardConfig()
    n_h1 = len(h1_candles)
    n_h4 = len(h4_candles)
    result = WalkForwardResult()

    # Reserve holdout (last holdout_bars of the data)
    usable_h1 = n_h1 - wf.holdout_bars
    min_window = wf.train_bars + wf.embargo_bars + wf.test_bars

    if usable_h1 < min_window:
        log.warning(
            "Not enough data for walk-forward: %d usable bars, need %d",
            usable_h1,
            min_window,
        )
        return result

    # Generate windows
    windows: list[WindowResult] = []
    win_idx = 0
    start = 0

    while start + min_window <= usable_h1:
        train_start = start
        train_end = start + wf.train_bars
        test_start = train_end + wf.embargo_bars
        test_end = min(test_start + wf.test_bars, usable_h1)

        if test_end <= test_start:
            break

        # Slice H1
        h1_train = h1_candles[train_start:train_end]
        h1_test = h1_candles[test_start:test_end]

        # Slice H4 (aligned via 4:1 ratio)
        h4_ts, h4_te = _slice_h4(train_start, train_end, n_h4)
        h4_train = h4_candles[h4_ts:h4_te]

        h4_ts2, h4_te2 = _slice_h4(test_start, test_end, n_h4)
        h4_test = h4_candles[h4_ts2:h4_te2]

        # Run IS and OOS backtests
        is_score = _run_single_backtest(config, h1_train, h4_train)
        oos_score = _run_single_backtest(config, h1_test, h4_test)

        ratio = oos_score / is_score if is_score > 0 else (1.0 if oos_score > 0 else 0.0)
        passed = ratio >= wf.min_oos_is_ratio

        w = WindowResult(
            window_index=win_idx,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            is_scout_score=is_score,
            oos_scout_score=oos_score,
            oos_is_ratio=round(ratio, 4),
            test_trade_count=0,  # populated below if needed
            passed=passed,
        )
        windows.append(w)
        log.info(
            "Window %d: IS=%.4f OOS=%.4f ratio=%.4f %s",
            win_idx,
            is_score,
            oos_score,
            ratio,
            "PASS" if passed else "FAIL",
        )

        win_idx += 1
        start += wf.step_bars

    result.windows = windows

    if not windows:
        return result

    # Aggregate window stats
    is_scores = [w.is_scout_score for w in windows]
    oos_scores = [w.oos_scout_score for w in windows]
    ratios = [w.oos_is_ratio for w in windows]

    result.median_oos_score = statistics.median(oos_scores)
    result.median_oos_is_ratio = statistics.median(ratios)
    result.all_windows_passed = all(w.passed for w in windows)

    # Holdout evaluation
    if wf.holdout_bars > 0 and n_h1 > wf.holdout_bars:
        holdout_h1 = h1_candles[-wf.holdout_bars :]
        h4_hs, h4_he = _slice_h4(n_h1 - wf.holdout_bars, n_h1, n_h4)
        holdout_h4 = h4_candles[h4_hs:h4_he]

        holdout_score = _run_single_backtest(config, holdout_h1, holdout_h4)
        result.holdout_score = holdout_score

        median_is = statistics.median(is_scores) if is_scores else 0.0
        holdout_ratio = holdout_score / median_is if median_is > 0 else (1.0 if holdout_score > 0 else 0.0)
        result.holdout_is_ratio = round(holdout_ratio, 4)
        result.holdout_passed = holdout_ratio >= wf.min_holdout_ratio

    # Overall verdict
    result.overall_passed = result.all_windows_passed and (
        result.holdout_passed is True or result.holdout_passed is None
    )
    return result
