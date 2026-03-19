"""Scout score and promotion score computation for strategy evaluation.

Scout score: fast heuristic for ranking strategies during AutoResearch
exploration. Combines profit factor, Calmar ratio, expectancy, and
consistency into a single 0.0–1.0 score with penalty deductions.

Promotion score: used after OOS validation to decide if a strategy
graduates from scout pool to live candidate pool.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from novatrade.evaluation.gates import GateResults


@dataclass
class FitnessResult:
    """Combined fitness evaluation result."""

    scout_score: float = 0.0
    """Scout score (0.0–1.0) from in-sample evaluation."""

    promotion_score: float | None = None
    """Promotion score (0.0–1.0) from OOS validation. None if not evaluated."""

    gate_results: GateResults = field(default_factory=GateResults)
    """Gate check results (Stage A/B/C)."""

    details: dict[str, Any] = field(default_factory=dict)
    """Breakdown of score components, penalties, and intermediates."""


def compute_scout_score(metrics: Any) -> float:
    """Compute scout score from backtest metrics.

    Weighted combination of capped performance ratios:
      40% profit factor (capped at 3.0)
      30% Calmar ratio (capped at 5.0)
      20% expectancy in R-multiples
      10% consistency (placeholder)

    Penalties:
      -0.05 for consecutive losses > 8
      -0.10 for max drawdown > 10%
      Boundary parameter penalty: stub (always 0)

    Args:
        metrics: BacktestMetrics instance (typed as Any to avoid circular imports).
            Expected fields: total_completed_trades, profit_factor, expectancy_r,
            max_drawdown_pct, net_result_pips, total_bars, max_consecutive_losses.

    Returns:
        Scout score in range [0.0, 1.0]. Returns 0.0 if fewer than 30 trades.
    """
    total_trades = getattr(metrics, "total_completed_trades", 0)
    if total_trades < 30:
        return 0.0

    # --- Component 1: Profit Factor (40%) ---
    pf = getattr(metrics, "profit_factor", 0.0)
    # Handle inf profit factor (no losses)
    if pf == float("inf"):
        pf = 3.0
    pf_capped = min(pf, 3.0)
    pf_score = pf_capped / 3.0  # normalize to 0–1

    # --- Component 2: Calmar Ratio (30%) ---
    max_dd_pct = getattr(metrics, "max_drawdown_pct", 0.0)
    net_pips = getattr(metrics, "net_result_pips", 0.0)
    total_bars = getattr(metrics, "total_bars", 0)

    # Annualize return: (net_pips / trading_days) * 252
    trading_days = total_bars / 24.0 if total_bars > 0 else 1.0
    annualized_return = (net_pips / trading_days) * 252.0 if trading_days > 0 else 0.0

    if max_dd_pct > 0:
        calmar = annualized_return / max_dd_pct
    else:
        calmar = 5.0 if annualized_return > 0 else 0.0

    calmar_capped = min(max(calmar, 0.0), 5.0)
    calmar_score = calmar_capped / 5.0  # normalize to 0–1

    # --- Component 3: Expectancy R (20%) ---
    exp_r = getattr(metrics, "expectancy_r", 0.0)
    # Clamp to reasonable range: -1 to +2, then normalize
    exp_clamped = min(max(exp_r, -1.0), 2.0)
    exp_score = (exp_clamped + 1.0) / 3.0  # maps [-1, 2] → [0, 1]

    # --- Component 4: Consistency (10%) ---
    # Stub: use win rate as proxy until monthly return series is available
    win_rate = getattr(metrics, "win_rate", 0.0)
    consistency_score = min(win_rate, 1.0)

    # --- Weighted sum ---
    raw_score = 0.40 * pf_score + 0.30 * calmar_score + 0.20 * exp_score + 0.10 * consistency_score

    # --- Penalties ---
    penalty = 0.0

    max_consec_losses = getattr(metrics, "max_consecutive_losses", 0)
    if max_consec_losses > 8:
        penalty += 0.05

    if max_dd_pct > 10.0:
        penalty += 0.10

    # Boundary parameter penalty: stub (requires parameter space analysis)

    final_score = max(raw_score - penalty, 0.0)
    return min(final_score, 1.0)


def compute_promotion_score(
    oos_scores: list[float],
    holdout_score: float,
    is_median: float,
    complexity: int,
) -> float:
    """Compute promotion score for OOS-validated strategies.

    Combines OOS stability with holdout performance and penalizes
    complexity and instability.

    Args:
        oos_scores: Scout scores from each OOS fold.
        holdout_score: Scout score on final holdout set.
        is_median: Median in-sample scout score across folds.
        complexity: Parameter count (higher = more penalty).

    Returns:
        Promotion score in range [0.0, 1.0].
    """
    if not oos_scores:
        return 0.0

    # Base: median OOS score
    median_oos = statistics.median(oos_scores)

    # Holdout bonus: small reward if holdout confirms OOS
    holdout_bonus = 0.0
    if holdout_score >= median_oos * 0.8:
        holdout_bonus = 0.05

    # Complexity penalty: -0.02 per parameter above 5
    complexity_penalty = max(0.0, (complexity - 5) * 0.02)

    # Instability penalty: high variance across OOS folds
    instability_penalty = 0.0
    if len(oos_scores) >= 2:
        oos_std = statistics.stdev(oos_scores)
        if oos_std > 0.15:
            instability_penalty = 0.10
        elif oos_std > 0.10:
            instability_penalty = 0.05

    score = median_oos + holdout_bonus - complexity_penalty - instability_penalty
    return max(min(score, 1.0), 0.0)
