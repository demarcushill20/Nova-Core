"""Phase 5A — Strategy Durability Validation.

Validates that a strategy has a DURABLE edge across time, not just aggregate
profitability.  Five sub-gates:

  P5A.1  Rolling-window profitability (12-month and 6-month overlapping windows)
  P5A.2  Regime consistency (trending / ranging / volatile / quiet)
  P5A.3  Drawdown survivability (rolling DD, recovery factor, underwater days)
  P5A.4  Year-over-year breakdown
  P5A.5  OOS decay check (OOS profit factor vs IS profit factor)

All gates emit GateResult objects compatible with the existing evaluation ladder.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from novatrade.backtest.metrics import CompletedTrade
from novatrade.evaluation.gates import GateResult
from novatrade.models import Candle

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SECONDS_PER_DAY = 86_400
_DAYS_PER_MONTH = 30.44  # average calendar month
_SECONDS_PER_MONTH = _DAYS_PER_MONTH * _SECONDS_PER_DAY


# ---------------------------------------------------------------------------
# P5A.1 — Rolling-Window Profitability
# ---------------------------------------------------------------------------


@dataclass
class WindowMetrics:
    """Metrics for a single rolling time window."""

    start_ts: float
    end_ts: float
    net_pnl: float
    profit_factor: float
    max_drawdown_pct: float
    trade_count: int
    win_rate: float


def rolling_window_analysis(
    trades: list[CompletedTrade],
    window_months: int = 12,
    step_months: int = 3,
    initial_equity: float = 10_000.0,
) -> list[WindowMetrics]:
    """Slice trades into overlapping time windows and compute per-window metrics.

    Args:
        trades: Completed trades sorted by exit_timestamp (or any order).
        window_months: Width of each rolling window in months.
        step_months: Step size between consecutive window starts.
        initial_equity: Starting equity for drawdown computation.

    Returns:
        List of WindowMetrics, one per window that contains at least one trade.
    """
    if not trades:
        return []

    sorted_trades = sorted(trades, key=lambda t: t.exit_timestamp)
    first_ts = sorted_trades[0].entry_timestamp
    last_ts = sorted_trades[-1].exit_timestamp

    window_seconds = window_months * _SECONDS_PER_MONTH
    step_seconds = step_months * _SECONDS_PER_MONTH

    windows: list[WindowMetrics] = []
    start = first_ts

    while start + window_seconds <= last_ts + step_seconds:
        end = start + window_seconds
        w_trades = [t for t in sorted_trades if t.exit_timestamp >= start and t.exit_timestamp < end]

        if w_trades:
            wm = _compute_window_metrics(w_trades, start, end, initial_equity)
            windows.append(wm)

        start += step_seconds

    return windows


def _compute_window_metrics(
    trades: list[CompletedTrade],
    start_ts: float,
    end_ts: float,
    initial_equity: float,
) -> WindowMetrics:
    """Compute metrics for a single window of trades."""
    net_pnl = sum(t.pnl_usd for t in trades)
    gross_profit = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    gross_loss = abs(sum(t.pnl_usd for t in trades if t.pnl_usd < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    winners = sum(1 for t in trades if t.pnl_usd > 0)
    win_rate = winners / len(trades) if trades else 0.0

    # Compute max drawdown within window
    max_dd_pct = _compute_max_dd_pct(trades, initial_equity)

    return WindowMetrics(
        start_ts=start_ts,
        end_ts=end_ts,
        net_pnl=net_pnl,
        profit_factor=profit_factor,
        max_drawdown_pct=max_dd_pct,
        trade_count=len(trades),
        win_rate=win_rate,
    )


def _compute_max_dd_pct(
    trades: list[CompletedTrade],
    initial_equity: float,
) -> float:
    """Compute maximum drawdown percentage from a sequence of trades."""
    if not trades:
        return 0.0

    equity = initial_equity
    peak = initial_equity
    max_dd = 0.0

    for t in sorted(trades, key=lambda x: x.exit_timestamp):
        equity += t.pnl_usd
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return max_dd


def rolling_profitability_gate(
    trades: list[CompletedTrade],
    initial_equity: float = 10_000.0,
    min_12m_win_pct: float = 0.60,
    min_6m_win_pct: float = 0.55,
    max_concentration_ratio: float = 0.40,
) -> GateResult:
    """Gate: strategy must be profitable across the majority of rolling windows.

    Checks:
      1. Profitable in >= 60% of 12-month rolling windows (step=3mo).
      2. Profitable in >= 55% of 6-month rolling windows (step=3mo).
      3. No single best 12-month window contributes > 40% of total profit.

    Returns:
        GateResult with pass/fail and detailed breakdown.
    """
    if not trades:
        return GateResult(
            gate="D.rolling_profitability",
            passed=False,
            reason="No trades provided",
            details={"trade_count": 0},
        )

    # 12-month windows
    windows_12m = rolling_window_analysis(
        trades,
        window_months=12,
        step_months=3,
        initial_equity=initial_equity,
    )
    # 6-month windows
    windows_6m = rolling_window_analysis(
        trades,
        window_months=6,
        step_months=3,
        initial_equity=initial_equity,
    )

    # Compute profitable-window percentages
    if windows_12m:
        profitable_12m = sum(1 for w in windows_12m if w.net_pnl > 0)
        pct_12m = profitable_12m / len(windows_12m)
    else:
        pct_12m = 0.0

    if windows_6m:
        profitable_6m = sum(1 for w in windows_6m if w.net_pnl > 0)
        pct_6m = profitable_6m / len(windows_6m)
    else:
        pct_6m = 0.0

    # Profit concentration: best 12-month window vs total
    total_pnl = sum(t.pnl_usd for t in trades)
    if windows_12m and total_pnl > 0:
        best_window_pnl = max(w.net_pnl for w in windows_12m)
        concentration = best_window_pnl / total_pnl if total_pnl > 0 else 0.0
    else:
        concentration = 0.0

    # Evaluate gates
    gate_12m = pct_12m >= min_12m_win_pct
    gate_6m = pct_6m >= min_6m_win_pct
    gate_conc = concentration <= max_concentration_ratio

    failures: list[str] = []
    if not gate_12m:
        failures.append(f"12-month profitable windows {pct_12m:.1%} < {min_12m_win_pct:.0%}")
    if not gate_6m:
        failures.append(f"6-month profitable windows {pct_6m:.1%} < {min_6m_win_pct:.0%}")
    if not gate_conc:
        failures.append(f"Best 12-month window concentration {concentration:.1%} > {max_concentration_ratio:.0%}")

    passed = gate_12m and gate_6m and gate_conc
    reason = "All rolling profitability checks passed" if passed else "; ".join(failures)

    return GateResult(
        gate="D.rolling_profitability",
        passed=passed,
        reason=reason,
        details={
            "rolling_12m_win_pct": round(pct_12m, 4),
            "rolling_6m_win_pct": round(pct_6m, 4),
            "profit_concentration_ratio": round(concentration, 4),
            "windows_12m_count": len(windows_12m),
            "windows_6m_count": len(windows_6m),
            "min_12m_win_pct": min_12m_win_pct,
            "min_6m_win_pct": min_6m_win_pct,
            "max_concentration_ratio": max_concentration_ratio,
        },
    )


# ---------------------------------------------------------------------------
# P5A.2 — Regime Consistency Scoring
# ---------------------------------------------------------------------------


@dataclass
class RegimeMetrics:
    """Per-regime performance summary."""

    regime: str  # "trending", "ranging", "volatile", "quiet"
    candle_count: int
    trades_in_regime: int
    profit_factor: float
    win_rate: float
    avg_trade_pnl: float
    max_drawdown_pct: float


def classify_regimes(
    candles: list[Candle],
    atr_period: int = 14,
) -> dict[float, str]:
    """Classify each candle's market regime based on ATR and directional bias.

    Regime rules (applied in priority order):
      1. ATR > 75th percentile -> "volatile"
      2. ATR < 25th percentile -> "quiet"
      3. Directional bias > 0.6 -> "trending"
      4. Otherwise -> "ranging"

    Args:
        candles: OHLCV candles in chronological order.
        atr_period: Look-back period for ATR computation.

    Returns:
        Mapping of candle timestamp -> regime label.
    """
    if len(candles) < atr_period + 1:
        # Not enough data to compute ATR; label everything as "ranging"
        return {c.timestamp: "ranging" for c in candles}

    # Compute True Range for each candle (starting from index 1)
    true_ranges: list[float] = []
    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i - 1].close
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        true_ranges.append(tr)

    # Compute ATR as simple moving average of TR
    atr_values: list[float] = []
    for i in range(len(true_ranges)):
        if i < atr_period - 1:
            atr_values.append(0.0)  # not enough data yet
        else:
            window = true_ranges[i - atr_period + 1 : i + 1]
            atr_values.append(statistics.mean(window))

    # Compute direction bias over atr_period candles
    # bias = abs(close[i] - close[i - atr_period]) / (sum of abs(close[j] - close[j-1]) for j in window)
    direction_bias: list[float] = []
    for i in range(len(true_ranges)):
        if i < atr_period - 1:
            direction_bias.append(0.0)
        else:
            # Net move vs cumulative move over the period
            start_idx = i - atr_period + 1
            # Candle indices are offset by 1 from true_ranges indices
            net_move = abs(candles[i + 1].close - candles[start_idx + 1].open)
            cum_move = sum(abs(candles[j + 1].close - candles[j].close) for j in range(start_idx, i + 1))
            bias = net_move / cum_move if cum_move > 0 else 0.0
            direction_bias.append(bias)

    # Compute ATR percentile bands from valid (non-zero) ATR values
    valid_atrs = [a for a in atr_values if a > 0]
    if valid_atrs:
        sorted_atrs = sorted(valid_atrs)
        p25_idx = max(0, int(len(sorted_atrs) * 0.25) - 1)
        p75_idx = min(len(sorted_atrs) - 1, int(len(sorted_atrs) * 0.75))
        atr_p25 = sorted_atrs[p25_idx]
        atr_p75 = sorted_atrs[p75_idx]
    else:
        atr_p25 = 0.0
        atr_p75 = float("inf")

    # Classify each candle
    regime_map: dict[float, str] = {}

    # First candle has no TR — default to "ranging"
    regime_map[candles[0].timestamp] = "ranging"

    for i in range(len(true_ranges)):
        candle = candles[i + 1]  # offset by 1
        atr = atr_values[i]
        bias = direction_bias[i]

        if atr <= 0:
            regime_map[candle.timestamp] = "ranging"
        elif atr > atr_p75:
            regime_map[candle.timestamp] = "volatile"
        elif atr < atr_p25:
            regime_map[candle.timestamp] = "quiet"
        elif bias > 0.6:
            regime_map[candle.timestamp] = "trending"
        else:
            regime_map[candle.timestamp] = "ranging"

    return regime_map


def _compute_regime_metrics(
    regime: str,
    trades: list[CompletedTrade],
    candle_count: int,
    initial_equity: float,
) -> RegimeMetrics:
    """Compute performance metrics for trades in a single regime."""
    if not trades:
        return RegimeMetrics(
            regime=regime,
            candle_count=candle_count,
            trades_in_regime=0,
            profit_factor=0.0,
            win_rate=0.0,
            avg_trade_pnl=0.0,
            max_drawdown_pct=0.0,
        )

    gross_profit = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    gross_loss = abs(sum(t.pnl_usd for t in trades if t.pnl_usd < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    winners = sum(1 for t in trades if t.pnl_usd > 0)
    wr = winners / len(trades)
    avg_pnl = statistics.mean([t.pnl_usd for t in trades])
    max_dd = _compute_max_dd_pct(trades, initial_equity)

    return RegimeMetrics(
        regime=regime,
        candle_count=candle_count,
        trades_in_regime=len(trades),
        profit_factor=pf,
        win_rate=wr,
        avg_trade_pnl=avg_pnl,
        max_drawdown_pct=max_dd,
    )


def regime_consistency_gate(
    trades: list[CompletedTrade],
    candles: list[Candle],
    max_negative_regime_pct: float = 0.20,
    initial_equity: float = 10_000.0,
) -> GateResult:
    """Gate: strategy must not have large negative-expectancy exposure in any regime.

    Checks:
      - Classify all candles into regimes.
      - Map each trade to the regime active at its entry timestamp.
      - Compute per-regime profit factor.
      - Gate: no regime with negative expectancy (avg PnL < 0) covering > 20%
        of total candle data.
      - Score: harmonic mean of per-regime profit factors (capped regimes with
        trades only).

    Returns:
        GateResult with regime breakdown.
    """
    if not trades or not candles:
        return GateResult(
            gate="D.regime_consistency",
            passed=False,
            reason="No trades or candles provided",
            details={"trade_count": len(trades), "candle_count": len(candles)},
        )

    regime_map = classify_regimes(candles)
    total_candles = len(candles)

    # Build lookup: timestamp -> regime for fast matching
    # For each trade, find the regime of the closest candle to entry_timestamp
    candle_timestamps = sorted(regime_map.keys())

    def _find_regime(ts: float) -> str:
        """Find the regime label for the candle closest to timestamp ts."""
        if not candle_timestamps:
            return "ranging"
        # Binary search for closest timestamp
        lo, hi = 0, len(candle_timestamps) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if candle_timestamps[mid] < ts:
                lo = mid + 1
            else:
                hi = mid
        # Check neighbours for closest
        best = candle_timestamps[lo]
        if lo > 0 and abs(candle_timestamps[lo - 1] - ts) < abs(best - ts):
            best = candle_timestamps[lo - 1]
        return regime_map.get(best, "ranging")

    # Count candles per regime
    regime_candle_counts: dict[str, int] = {}
    for r in regime_map.values():
        regime_candle_counts[r] = regime_candle_counts.get(r, 0) + 1

    # Map trades to regimes
    regime_trades: dict[str, list[CompletedTrade]] = {
        "trending": [],
        "ranging": [],
        "volatile": [],
        "quiet": [],
    }
    for t in trades:
        r = _find_regime(t.entry_timestamp)
        if r not in regime_trades:
            regime_trades[r] = []
        regime_trades[r].append(t)

    # Compute per-regime metrics
    regime_metrics_list: list[RegimeMetrics] = []
    for regime in ["trending", "ranging", "volatile", "quiet"]:
        rm = _compute_regime_metrics(
            regime,
            regime_trades.get(regime, []),
            regime_candle_counts.get(regime, 0),
            initial_equity,
        )
        regime_metrics_list.append(rm)

    # Check: no regime with negative expectancy covering > max_negative_regime_pct of data
    failures: list[str] = []
    for rm in regime_metrics_list:
        regime_fraction = rm.candle_count / total_candles if total_candles > 0 else 0.0
        if rm.trades_in_regime > 0 and rm.avg_trade_pnl < 0 and regime_fraction > max_negative_regime_pct:
            failures.append(
                f"Regime '{rm.regime}' has negative expectancy "
                f"(avg PnL={rm.avg_trade_pnl:.2f}) covering "
                f"{regime_fraction:.1%} of data (> {max_negative_regime_pct:.0%})"
            )

    # Harmonic mean of profit factors for regimes with trades
    pf_values = [
        rm.profit_factor
        for rm in regime_metrics_list
        if rm.trades_in_regime > 0 and math.isfinite(rm.profit_factor) and rm.profit_factor > 0
    ]
    if pf_values:
        harmonic_mean = len(pf_values) / sum(1.0 / pf for pf in pf_values)
    else:
        harmonic_mean = 0.0

    passed = len(failures) == 0
    reason = "All regime consistency checks passed" if passed else "; ".join(failures)

    return GateResult(
        gate="D.regime_consistency",
        passed=passed,
        reason=reason,
        details={
            "regime_consistency_score": round(harmonic_mean, 4),
            "regimes": {
                rm.regime: {
                    "candle_count": rm.candle_count,
                    "trades": rm.trades_in_regime,
                    "profit_factor": round(rm.profit_factor, 4) if math.isfinite(rm.profit_factor) else "inf",
                    "win_rate": round(rm.win_rate, 4),
                    "avg_trade_pnl": round(rm.avg_trade_pnl, 2),
                    "max_drawdown_pct": round(rm.max_drawdown_pct, 2),
                }
                for rm in regime_metrics_list
            },
            "max_negative_regime_pct": max_negative_regime_pct,
        },
    )


# ---------------------------------------------------------------------------
# P5A.3 — Drawdown Survivability
# ---------------------------------------------------------------------------


def drawdown_survivability_gate(
    trades: list[CompletedTrade],
    initial_equity: float = 10_000.0,
    max_rolling_dd_pct: float = 10.0,
    min_recovery_factor: float = 2.0,
    max_underwater_days: int = 90,
) -> GateResult:
    """Gate: drawdown must stay within prop-firm limits and recover quickly.

    Checks:
      1. No rolling 12-month window DD exceeds max_rolling_dd_pct (default 10%).
      2. Recovery factor (net profit / max DD in USD) >= min_recovery_factor.
      3. Maximum underwater duration <= max_underwater_days trading days.

    Returns:
        GateResult with drawdown breakdown.
    """
    if not trades:
        return GateResult(
            gate="D.drawdown_survivability",
            passed=False,
            reason="No trades provided",
            details={"trade_count": 0},
        )

    sorted_trades = sorted(trades, key=lambda t: t.exit_timestamp)

    # Overall drawdown analysis
    equity = initial_equity
    peak = initial_equity
    max_dd_pct = 0.0
    max_dd_usd = 0.0

    # Underwater tracking (in terms of trade count / timestamp gaps)
    max_underwater_seconds = 0.0
    current_underwater_start: float | None = None

    for t in sorted_trades:
        equity += t.pnl_usd
        if equity >= peak:
            peak = equity
            # Ended underwater period
            if current_underwater_start is not None:
                underwater_dur = t.exit_timestamp - current_underwater_start
                if underwater_dur > max_underwater_seconds:
                    max_underwater_seconds = underwater_dur
                current_underwater_start = None
        else:
            dd_usd = peak - equity
            dd_pct = (dd_usd / peak) * 100.0 if peak > 0 else 0.0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                max_dd_usd = dd_usd
            if current_underwater_start is None:
                current_underwater_start = t.exit_timestamp

    # Handle case where we end underwater
    if current_underwater_start is not None:
        final_dur = sorted_trades[-1].exit_timestamp - current_underwater_start
        if final_dur > max_underwater_seconds:
            max_underwater_seconds = final_dur

    # Convert underwater seconds to trading days (~8h per trading day)
    underwater_days = int(max_underwater_seconds / _SECONDS_PER_DAY)

    # Recovery factor: net profit / max drawdown USD
    net_profit = sum(t.pnl_usd for t in trades)
    recovery_factor = net_profit / max_dd_usd if max_dd_usd > 0 else float("inf")

    # Rolling 12-month window max DD check
    windows_12m = rolling_window_analysis(
        trades,
        window_months=12,
        step_months=3,
        initial_equity=initial_equity,
    )
    worst_window_dd = max(
        (w.max_drawdown_pct for w in windows_12m),
        default=0.0,
    )

    # Evaluate gates
    gate_dd = worst_window_dd <= max_rolling_dd_pct
    gate_recovery = recovery_factor >= min_recovery_factor or (math.isinf(recovery_factor) and recovery_factor > 0)
    gate_underwater = underwater_days <= max_underwater_days

    failures: list[str] = []
    if not gate_dd:
        failures.append(f"Worst rolling 12-month DD {worst_window_dd:.2f}% > {max_rolling_dd_pct:.1f}%")
    if not gate_recovery:
        failures.append(f"Recovery factor {recovery_factor:.2f} < {min_recovery_factor:.1f}")
    if not gate_underwater:
        failures.append(f"Max underwater duration {underwater_days} days > {max_underwater_days} days")

    passed = gate_dd and gate_recovery and gate_underwater
    reason = "All drawdown survivability checks passed" if passed else "; ".join(failures)

    return GateResult(
        gate="D.drawdown_survivability",
        passed=passed,
        reason=reason,
        details={
            "max_overall_dd_pct": round(max_dd_pct, 4),
            "max_overall_dd_usd": round(max_dd_usd, 2),
            "worst_rolling_12m_dd_pct": round(worst_window_dd, 4),
            "recovery_factor": round(recovery_factor, 4) if math.isfinite(recovery_factor) else "inf",
            "max_underwater_days": underwater_days,
            "net_profit_usd": round(net_profit, 2),
            "thresholds": {
                "max_rolling_dd_pct": max_rolling_dd_pct,
                "min_recovery_factor": min_recovery_factor,
                "max_underwater_days": max_underwater_days,
            },
        },
    )


# ---------------------------------------------------------------------------
# P5A.4 — Year-Over-Year Breakdown
# ---------------------------------------------------------------------------


@dataclass
class YearBreakdown:
    """Performance summary for a single calendar year."""

    year: int
    net_pnl: float
    profit_factor: float
    max_drawdown_pct: float
    trade_count: int
    win_rate: float


def year_over_year_breakdown(
    trades: list[CompletedTrade],
    initial_equity: float = 10_000.0,
) -> list[YearBreakdown]:
    """Break down trade performance by calendar year.

    Args:
        trades: Completed trades with valid exit_timestamp.
        initial_equity: Starting equity for per-year drawdown computation.

    Returns:
        List of YearBreakdown, one per year with trades, sorted chronologically.
    """
    if not trades:
        return []

    # Group trades by year of exit_timestamp
    year_groups: dict[int, list[CompletedTrade]] = {}
    for t in trades:
        year = datetime.fromtimestamp(t.exit_timestamp, tz=timezone.utc).year
        if year not in year_groups:
            year_groups[year] = []
        year_groups[year].append(t)

    results: list[YearBreakdown] = []
    for year in sorted(year_groups.keys()):
        y_trades = year_groups[year]
        net_pnl = sum(t.pnl_usd for t in y_trades)
        gross_profit = sum(t.pnl_usd for t in y_trades if t.pnl_usd > 0)
        gross_loss = abs(sum(t.pnl_usd for t in y_trades if t.pnl_usd < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        winners = sum(1 for t in y_trades if t.pnl_usd > 0)
        wr = winners / len(y_trades) if y_trades else 0.0
        max_dd = _compute_max_dd_pct(y_trades, initial_equity)

        results.append(
            YearBreakdown(
                year=year,
                net_pnl=net_pnl,
                profit_factor=pf,
                max_drawdown_pct=max_dd,
                trade_count=len(y_trades),
                win_rate=wr,
            )
        )

    return results


# ---------------------------------------------------------------------------
# P5A.5 — OOS Decay Check
# ---------------------------------------------------------------------------


def oos_decay_gate(
    is_profit_factor: float,
    oos_profit_factor: float,
    min_oos_is_ratio: float = 0.50,
) -> GateResult:
    """Gate: out-of-sample performance must not decay excessively vs in-sample.

    Gate passes if OOS PF >= min_oos_is_ratio * IS PF.

    Args:
        is_profit_factor: In-sample profit factor.
        oos_profit_factor: Out-of-sample profit factor.
        min_oos_is_ratio: Minimum acceptable OOS/IS ratio (default 0.50).

    Returns:
        GateResult with OOS/IS ratio detail.
    """
    if is_profit_factor <= 0:
        return GateResult(
            gate="D.oos_decay",
            passed=False,
            reason=f"IS profit factor is non-positive ({is_profit_factor:.4f})",
            details={
                "is_profit_factor": is_profit_factor,
                "oos_profit_factor": oos_profit_factor,
            },
        )

    # Handle infinite IS PF (no losses in-sample)
    if math.isinf(is_profit_factor):
        # If IS is infinite, any positive OOS PF is acceptable
        ratio = 1.0 if oos_profit_factor > 0 else 0.0
    else:
        ratio = oos_profit_factor / is_profit_factor

    passed = ratio >= min_oos_is_ratio
    reason = (
        f"OOS/IS ratio {ratio:.2f} >= {min_oos_is_ratio:.2f}"
        if passed
        else f"OOS/IS ratio {ratio:.2f} < {min_oos_is_ratio:.2f} — excessive decay"
    )

    return GateResult(
        gate="D.oos_decay",
        passed=passed,
        reason=reason,
        details={
            "oos_is_ratio": round(ratio, 4),
            "is_profit_factor": round(is_profit_factor, 4) if math.isfinite(is_profit_factor) else "inf",
            "oos_profit_factor": round(oos_profit_factor, 4) if math.isfinite(oos_profit_factor) else "inf",
            "min_oos_is_ratio": min_oos_is_ratio,
        },
    )


# ---------------------------------------------------------------------------
# Integration — DurabilityConfig, DurabilityReport, evaluate_durability
# ---------------------------------------------------------------------------


@dataclass
class DurabilityConfig:
    """Threshold configuration for all durability gates."""

    min_12m_win_pct: float = 0.60
    min_6m_win_pct: float = 0.55
    max_concentration_ratio: float = 0.40
    max_rolling_dd_pct: float = 10.0
    min_recovery_factor: float = 2.0
    max_underwater_days: int = 90
    min_oos_is_ratio: float = 0.50
    max_negative_regime_pct: float = 0.20


@dataclass
class DurabilityReport:
    """Complete durability validation report."""

    rolling_12m_win_pct: float
    rolling_6m_win_pct: float
    regime_consistency_score: float
    profit_concentration_ratio: float
    max_underwater_days: int
    recovery_factor: float
    oos_is_ratio: float | None
    year_breakdown: list[YearBreakdown]
    durability_grade: str  # A/B/C/D/F
    gates: list[GateResult]
    overall_passed: bool


def _compute_durability_grade(gates: list[GateResult]) -> str:
    """Compute composite durability grade from gate results.

    Grading:
      A — all gates pass
      B — exactly 1 gate fails
      C — exactly 2 gates fail
      D — 3+ gates fail
      F — any critical failure (drawdown survivability or rolling profitability fails)
    """
    failed_gates = [g for g in gates if not g.passed]
    fail_count = len(failed_gates)

    # Check for critical failures (drawdown or rolling profitability)
    critical_ids = {"D.drawdown_survivability", "D.rolling_profitability"}
    has_critical = any(g.gate in critical_ids for g in failed_gates)

    if fail_count == 0:
        return "A"
    if has_critical and fail_count >= 2:
        return "F"
    if fail_count == 1:
        return "B"
    if fail_count == 2:
        return "C"
    return "D"


def evaluate_durability(
    trades: list[CompletedTrade],
    candles: list[Candle],
    initial_equity: float = 10_000.0,
    is_profit_factor: float | None = None,
    oos_profit_factor: float | None = None,
    config: DurabilityConfig | None = None,
) -> DurabilityReport:
    """Run all durability validation gates and produce a composite report.

    Args:
        trades: Completed backtest trades.
        candles: OHLCV candle data covering the same period.
        initial_equity: Starting account equity.
        is_profit_factor: In-sample profit factor (for OOS decay check).
        oos_profit_factor: Out-of-sample profit factor (for OOS decay check).
        config: Threshold overrides (uses defaults if None).

    Returns:
        DurabilityReport with all gates, year breakdown, and composite grade.
    """
    if config is None:
        config = DurabilityConfig()

    gates: list[GateResult] = []

    # P5A.1 — Rolling profitability
    rolling_gate = rolling_profitability_gate(
        trades,
        initial_equity=initial_equity,
        min_12m_win_pct=config.min_12m_win_pct,
        min_6m_win_pct=config.min_6m_win_pct,
        max_concentration_ratio=config.max_concentration_ratio,
    )
    gates.append(rolling_gate)

    rolling_12m_pct = rolling_gate.details.get("rolling_12m_win_pct", 0.0)
    rolling_6m_pct = rolling_gate.details.get("rolling_6m_win_pct", 0.0)
    concentration = rolling_gate.details.get("profit_concentration_ratio", 0.0)

    # P5A.2 — Regime consistency
    regime_gate = regime_consistency_gate(
        trades,
        candles,
        max_negative_regime_pct=config.max_negative_regime_pct,
        initial_equity=initial_equity,
    )
    gates.append(regime_gate)

    regime_score = regime_gate.details.get("regime_consistency_score", 0.0)

    # P5A.3 — Drawdown survivability
    dd_gate = drawdown_survivability_gate(
        trades,
        initial_equity=initial_equity,
        max_rolling_dd_pct=config.max_rolling_dd_pct,
        min_recovery_factor=config.min_recovery_factor,
        max_underwater_days=config.max_underwater_days,
    )
    gates.append(dd_gate)

    underwater = dd_gate.details.get("max_underwater_days", 0)
    rf = dd_gate.details.get("recovery_factor", 0.0)
    if isinstance(rf, str) and rf == "inf":
        rf = float("inf")

    # P5A.4 — Year-over-year breakdown
    yoy = year_over_year_breakdown(trades, initial_equity=initial_equity)

    # P5A.5 — OOS decay (optional)
    oos_ratio: float | None = None
    if is_profit_factor is not None and oos_profit_factor is not None:
        oos_gate = oos_decay_gate(
            is_profit_factor,
            oos_profit_factor,
            min_oos_is_ratio=config.min_oos_is_ratio,
        )
        gates.append(oos_gate)
        oos_ratio = oos_gate.details.get("oos_is_ratio", None)

    # Composite grade
    grade = _compute_durability_grade(gates)
    overall = all(g.passed for g in gates)

    return DurabilityReport(
        rolling_12m_win_pct=rolling_12m_pct,
        rolling_6m_win_pct=rolling_6m_pct,
        regime_consistency_score=regime_score,
        profit_concentration_ratio=concentration,
        max_underwater_days=underwater,
        recovery_factor=rf,
        oos_is_ratio=oos_ratio,
        year_breakdown=yoy,
        durability_grade=grade,
        gates=gates,
        overall_passed=overall,
    )
