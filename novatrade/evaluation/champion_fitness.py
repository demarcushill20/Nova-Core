"""Champion fitness scorer for multi-horizon strategy evaluation.

Replaces the scout_score heuristic for autoresearch ranking with a
scorer that prioritizes consistency, drawdown control, and regime
robustness over raw profitability.

5 weighted signals (total = 1.0):
  1. % profitable months        (30%)  — consistency that compounds
  2. Max drawdown               (25%)  — FTMO survival
  3. Profit factor stability    (20%)  — real edge vs curve-fit
  4. Worst rolling 1yr return   (15%)  — regime robustness
  5. Calmar ratio               (10%)  — risk-adjusted return

Hard gates (instant 0.0):
  - Fewer than min_trades
  - Overall profit factor ≤ 1.0
  - Max drawdown > 15%
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


def _clamp(val: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, val))


def _group_trades_by_month(trades: list[Any]) -> dict[tuple[int, int], list[Any]]:
    """Group trades by (year, month) using exit_timestamp."""
    groups: dict[tuple[int, int], list[Any]] = defaultdict(list)
    for t in trades:
        ts = getattr(t, "exit_timestamp", 0.0)
        if ts <= 0:
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        groups[(dt.year, dt.month)].append(t)
    return dict(groups)


def _group_trades_by_year(trades: list[Any]) -> dict[int, list[Any]]:
    """Group trades by year using exit_timestamp."""
    groups: dict[int, list[Any]] = defaultdict(list)
    for t in trades:
        ts = getattr(t, "exit_timestamp", 0.0)
        if ts <= 0:
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        groups[dt.year].append(t)
    return dict(groups)


def _equity_curve(trades: list[Any], initial_equity: float) -> list[float]:
    """Build equity curve from trade P&L (sorted by exit time)."""
    sorted_trades = sorted(trades, key=lambda t: getattr(t, "exit_timestamp", 0.0))
    curve = [initial_equity]
    equity = initial_equity
    for t in sorted_trades:
        equity += getattr(t, "pnl_usd", 0.0)
        curve.append(equity)
    return curve


def _max_drawdown_pct(equity_curve: list[float]) -> float:
    """Compute max drawdown as a percentage from equity curve."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            dd = (peak - eq) / peak * 100.0
            max_dd = max(max_dd, dd)
    return max_dd


def _profit_factor(trades: list[Any]) -> float:
    """Compute profit factor from a list of trades."""
    gross_win = sum(getattr(t, "pnl_pips", 0.0) for t in trades if getattr(t, "pnl_pips", 0.0) > 0)
    gross_loss = abs(sum(getattr(t, "pnl_pips", 0.0) for t in trades if getattr(t, "pnl_pips", 0.0) < 0))
    if gross_loss == 0:
        return 3.0 if gross_win > 0 else 0.0
    return gross_win / gross_loss


# ---------------------------------------------------------------------------
# Component scorers — each returns [0.0, 1.0]
# ---------------------------------------------------------------------------


def _score_profitable_months(trades: list[Any]) -> tuple[float, dict]:
    """Score 1: % of months with positive P&L.

    Scale: 45% → 0.0, 70%+ → 1.0 (linear ramp).
    Target: > 58% for a good strategy.
    """
    by_month = _group_trades_by_month(trades)
    if not by_month:
        return 0.0, {"total_months": 0, "profitable_months": 0, "pct": 0.0}

    monthly_pnl = {k: sum(getattr(t, "pnl_usd", 0.0) for t in v) for k, v in by_month.items()}
    profitable = sum(1 for pnl in monthly_pnl.values() if pnl > 0)
    total = len(monthly_pnl)
    pct = profitable / total if total > 0 else 0.0

    # Linear ramp: 0.45 → 0.0, 0.70 → 1.0
    score = _clamp((pct - 0.45) / 0.25)

    return score, {"total_months": total, "profitable_months": profitable, "pct": round(pct * 100, 1)}


def _score_max_drawdown(trades: list[Any], initial_equity: float) -> tuple[float, dict]:
    """Score 2: Max drawdown — lower is better.

    Scale: 0% → 1.0, 15%+ → 0.0 (linear).
    FTMO hard limit: 10% total, so < 8% target.
    """
    curve = _equity_curve(trades, initial_equity)
    dd = _max_drawdown_pct(curve)

    # Linear: 0% → 1.0, 15% → 0.0
    score = _clamp(1.0 - dd / 15.0)

    return score, {"max_dd_pct": round(dd, 2)}


def _score_pf_stability(trades: list[Any]) -> tuple[float, dict]:
    """Score 3: Profit factor stability across years.

    Coefficient of variation (std/mean) of yearly PF.
    Scale: CV=0 → 1.0, CV≥0.6 → 0.0.
    Requires ≥ 2 years; returns 0.5 (neutral) if insufficient data.
    """
    by_year = _group_trades_by_year(trades)

    # Need at least 2 years for meaningful stability measure
    if len(by_year) < 2:
        return 0.5, {"years": len(by_year), "reason": "insufficient_years"}

    yearly_pfs = []
    year_details = {}
    for year in sorted(by_year):
        pf = min(_profit_factor(by_year[year]), 3.0)  # cap at 3.0
        yearly_pfs.append(pf)
        year_details[year] = round(pf, 3)

    mean_pf = sum(yearly_pfs) / len(yearly_pfs)
    if mean_pf <= 0:
        return 0.0, {"years": len(by_year), "mean_pf": 0.0, "cv": float("inf"), "year_pfs": year_details}

    variance = sum((x - mean_pf) ** 2 for x in yearly_pfs) / len(yearly_pfs)
    std_pf = math.sqrt(variance)
    cv = std_pf / mean_pf

    # Linear: CV=0 → 1.0, CV=0.6 → 0.0
    score = _clamp((0.6 - cv) / 0.6)

    return score, {
        "years": len(by_year),
        "mean_pf": round(mean_pf, 3),
        "std_pf": round(std_pf, 3),
        "cv": round(cv, 3),
        "year_pfs": year_details,
    }


def _score_worst_rolling_1yr(trades: list[Any], initial_equity: float) -> tuple[float, dict]:
    """Score 4: Worst rolling 12-month return.

    Computes monthly returns (%), then finds worst 12-month rolling sum.
    Scale: ≥5% → 1.0, ≤-10% → 0.0 (linear).
    Requires ≥ 12 months; returns 0.5 (neutral) if insufficient.
    """
    by_month = _group_trades_by_month(trades)
    if len(by_month) < 12:
        return 0.5, {"months": len(by_month), "reason": "insufficient_months"}

    # Monthly returns as % of initial equity
    sorted_months = sorted(by_month.keys())
    monthly_returns = []
    for key in sorted_months:
        month_pnl = sum(getattr(t, "pnl_usd", 0.0) for t in by_month[key])
        pct = (month_pnl / initial_equity) * 100.0 if initial_equity > 0 else 0.0
        monthly_returns.append(pct)

    # Rolling 12-month return (simple sum of monthly %)
    rolling_1yr = []
    for i in range(len(monthly_returns) - 11):
        window_return = sum(monthly_returns[i : i + 12])
        rolling_1yr.append(window_return)

    worst = min(rolling_1yr) if rolling_1yr else 0.0

    # Linear: -10% → 0.0, +5% → 1.0
    score = _clamp((worst + 10.0) / 15.0)

    return score, {
        "worst_rolling_1yr_pct": round(worst, 2),
        "best_rolling_1yr_pct": round(max(rolling_1yr), 2) if rolling_1yr else 0.0,
        "n_windows": len(rolling_1yr),
    }


def _score_calmar(trades: list[Any], initial_equity: float) -> tuple[float, dict]:
    """Score 5: Calmar ratio (annualized return / max drawdown).

    Scale: ≥3.0 → 1.0, ≤0 → 0.0 (linear).
    """
    if not trades:
        return 0.0, {"calmar": 0.0}

    # Total return
    total_pnl = sum(getattr(t, "pnl_usd", 0.0) for t in trades)
    total_return_pct = (total_pnl / initial_equity) * 100.0 if initial_equity > 0 else 0.0

    # Time span in years
    sorted_trades = sorted(trades, key=lambda t: getattr(t, "exit_timestamp", 0.0))
    first_ts = getattr(sorted_trades[0], "entry_timestamp", 0.0)
    last_ts = getattr(sorted_trades[-1], "exit_timestamp", 0.0)
    span_years = (last_ts - first_ts) / (365.25 * 86400) if last_ts > first_ts else 1.0
    span_years = max(span_years, 0.25)  # floor at 3 months to avoid division explosion

    annualized_return_pct = total_return_pct / span_years

    # Max drawdown
    curve = _equity_curve(trades, initial_equity)
    dd = _max_drawdown_pct(curve)

    if dd > 0:
        calmar = annualized_return_pct / dd
    else:
        calmar = 3.0 if annualized_return_pct > 0 else 0.0

    # Linear: 0 → 0.0, 3.0 → 1.0
    score = _clamp(calmar / 3.0)

    return score, {
        "calmar": round(calmar, 3),
        "annualized_return_pct": round(annualized_return_pct, 2),
        "max_dd_pct": round(dd, 2),
        "span_years": round(span_years, 2),
    }


# ---------------------------------------------------------------------------
# Main scorer
# ---------------------------------------------------------------------------

# Component weights — must sum to 1.0
WEIGHTS = {
    "profitable_months": 0.30,
    "max_drawdown": 0.25,
    "pf_stability": 0.20,
    "worst_rolling_1yr": 0.15,
    "calmar": 0.10,
}


def compute_champion_fitness(
    trades: list[Any],
    total_bars: int = 0,
    initial_equity: float = 100_000.0,
    *,
    min_trades: int = 100,
) -> float:
    """Compute champion fitness score for strategy ranking.

    Multi-horizon consistency scorer that prioritizes monthly profitability,
    drawdown control, profit factor stability, worst-case rolling returns,
    and risk-adjusted performance over raw profit.

    Args:
        trades: List of CompletedTrade objects from backtest.
        total_bars: Total bars in backtest (unused, kept for interface compat).
        initial_equity: Starting equity for return calculations.
        min_trades: Minimum trades to produce a non-zero score.

    Returns:
        Champion fitness score in [0.0, 1.0].
    """
    # --- Hard gates ---
    if len(trades) < min_trades:
        return 0.0

    pf = _profit_factor(trades)
    if pf <= 1.0:
        return 0.0

    curve = _equity_curve(trades, initial_equity)
    dd = _max_drawdown_pct(curve)
    if dd > 15.0:
        return 0.0

    # --- Component scores ---
    s1, _ = _score_profitable_months(trades)
    s2, _ = _score_max_drawdown(trades, initial_equity)
    s3, _ = _score_pf_stability(trades)
    s4, _ = _score_worst_rolling_1yr(trades, initial_equity)
    s5, _ = _score_calmar(trades, initial_equity)

    score = (
        WEIGHTS["profitable_months"] * s1
        + WEIGHTS["max_drawdown"] * s2
        + WEIGHTS["pf_stability"] * s3
        + WEIGHTS["worst_rolling_1yr"] * s4
        + WEIGHTS["calmar"] * s5
    )

    return _clamp(score)


def compute_champion_fitness_detailed(
    trades: list[Any],
    total_bars: int = 0,
    initial_equity: float = 100_000.0,
    *,
    min_trades: int = 100,
) -> tuple[float, dict]:
    """Same as compute_champion_fitness but also returns component breakdown.

    Returns:
        (score, details) where details contains per-component scores and metadata.
    """
    details: dict[str, Any] = {"min_trades": min_trades, "trade_count": len(trades)}

    # --- Hard gates ---
    if len(trades) < min_trades:
        details["gate_failed"] = "min_trades"
        return 0.0, details

    pf = _profit_factor(trades)
    details["overall_pf"] = round(pf, 3)
    if pf <= 1.0:
        details["gate_failed"] = "profit_factor"
        return 0.0, details

    curve = _equity_curve(trades, initial_equity)
    dd = _max_drawdown_pct(curve)
    details["overall_max_dd_pct"] = round(dd, 2)
    if dd > 15.0:
        details["gate_failed"] = "max_drawdown"
        return 0.0, details

    # --- Component scores ---
    s1, d1 = _score_profitable_months(trades)
    s2, d2 = _score_max_drawdown(trades, initial_equity)
    s3, d3 = _score_pf_stability(trades)
    s4, d4 = _score_worst_rolling_1yr(trades, initial_equity)
    s5, d5 = _score_calmar(trades, initial_equity)

    score = (
        WEIGHTS["profitable_months"] * s1
        + WEIGHTS["max_drawdown"] * s2
        + WEIGHTS["pf_stability"] * s3
        + WEIGHTS["worst_rolling_1yr"] * s4
        + WEIGHTS["calmar"] * s5
    )
    score = _clamp(score)

    details["components"] = {
        "profitable_months": {"score": round(s1, 4), "weight": WEIGHTS["profitable_months"], **d1},
        "max_drawdown": {"score": round(s2, 4), "weight": WEIGHTS["max_drawdown"], **d2},
        "pf_stability": {"score": round(s3, 4), "weight": WEIGHTS["pf_stability"], **d3},
        "worst_rolling_1yr": {"score": round(s4, 4), "weight": WEIGHTS["worst_rolling_1yr"], **d4},
        "calmar": {"score": round(s5, 4), "weight": WEIGHTS["calmar"], **d5},
    }
    details["final_score"] = round(score, 4)

    return score, details
