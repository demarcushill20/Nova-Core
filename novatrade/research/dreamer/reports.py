"""Task-1061 — performance reporting for the DreamerV3 gold bot.

Turns a list of closed :class:`~novatrade.research.dreamer.execution.Trade`
objects into every metric the spec's "Reports" section asks for:

    * training / validation / test return (call the builder per window),
    * annualized return + maximum drawdown,
    * monthly returns,
    * trade counts (total / long / short),
    * exit-reason breakdown (stop-loss / take-profit / flip / manual),
    * stop-loss-multiplier and take-profit-R distributions,
    * weekday and hour-of-day performance,
    * rolling Sharpe ratio,
    * a baseline comparison against a simple EMA model.

All returns are expressed in **R** (risk multiples) — the engine's native PnL
unit — so they are broker-cost-agnostic and comparable across folds. Metrics
that need a wall-clock (monthly / weekday / hour / annualized) require a
``timestamps`` map from decision-bar index to ``pd.Timestamp``; without it those
fields are reported as empty/None rather than fabricated.

This is a pure, deterministic layer (no GPU, no data files) so it is implemented
and unit-tested now, ahead of the training loop.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

import pandas as pd

from .execution import BUY, HOLD, LONG, SELL, SHORT, Decision, Trade

# Map the engine's internal exit reasons onto the spec's report labels.
EXIT_LABELS = {
    "stop": "stop_loss",
    "target": "take_profit",
    "flip": "flip_close",
    "manual": "manual_close",
}

_DAYS_PER_YEAR = 365.25


def _closed(trades: Sequence[Trade]) -> list[Trade]:
    """Only fully-closed trades contribute to performance metrics."""
    return [t for t in trades if t.exit_reason is not None]


# --------------------------------------------------------------------------- #
# Returns, drawdown, Sharpe
# --------------------------------------------------------------------------- #
def returns_in_r(trades: Sequence[Trade]) -> list[float]:
    """Per-trade R multiples, in close (exit) order."""
    return [t.r_multiple() for t in _closed(trades)]


def total_return(trades: Sequence[Trade]) -> float:
    """Sum of realized R over all closed trades (the window's return)."""
    return float(sum(returns_in_r(trades)))


def equity_curve(trades: Sequence[Trade]) -> list[float]:
    """Cumulative R after each closed trade."""
    curve, running = [], 0.0
    for r in returns_in_r(trades):
        running += r
        curve.append(running)
    return curve


def max_drawdown(trades: Sequence[Trade]) -> float:
    """Largest peak-to-trough decline of the cumulative-R curve (>= 0).

    Returned as a non-negative magnitude so it feeds straight into
    :func:`selection.drawdown_penalty`. The peak starts at 0 (flat equity), so a
    curve that only ever loses still reports a real drawdown.
    """
    peak, worst = 0.0, 0.0
    for equity in equity_curve(trades):
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def annualized_return(
    trades: Sequence[Trade],
    timestamps: Sequence[pd.Timestamp] | None,
) -> float | None:
    """Total R scaled to a per-year rate using the live span of the trades.

    Span = first entry timestamp -> last exit timestamp. Returns ``None`` when
    timestamps are unavailable or the span is degenerate (can't annualize one
    instant honestly).
    """
    closed = _closed(trades)
    if not closed or timestamps is None:
        return None
    last_exit = closed[-1].exit_index
    assert last_exit is not None  # _closed guarantees an exit index is set
    start = timestamps[closed[0].entry_index]
    end = timestamps[last_exit]
    years = (end - start).total_seconds() / (_DAYS_PER_YEAR * 24 * 3600)
    if years <= 0:
        return None
    return total_return(trades) / years


def rolling_sharpe(trades: Sequence[Trade], window: int = 20) -> list[float]:
    """Rolling Sharpe of the per-trade R series (mean/std over ``window`` trades).

    Per-trade (not annualized) by design — it answers "how steady is the edge
    over the last N trades?". Windows with zero dispersion yield 0.0 (no
    information rather than a divide-by-zero spike). Fewer than ``window`` closed
    trades -> empty list.
    """
    if window <= 1:
        raise ValueError("window must be > 1 for a Sharpe ratio")
    r = pd.Series(returns_in_r(trades), dtype="float64")
    if len(r) < window:
        return []
    mean = r.rolling(window).mean()
    std = r.rolling(window).std(ddof=1)
    sharpe = (mean / std).where(std > 0, 0.0)
    return [float(x) for x in sharpe.dropna().tolist()]


# --------------------------------------------------------------------------- #
# Counts and distributions
# --------------------------------------------------------------------------- #
def trade_counts(trades: Sequence[Trade]) -> dict[str, int]:
    """Total, long and short closed-trade counts."""
    closed = _closed(trades)
    return {
        "total": len(closed),
        "long": sum(1 for t in closed if t.side == LONG),
        "short": sum(1 for t in closed if t.side == SHORT),
    }


def exit_reason_breakdown(trades: Sequence[Trade]) -> dict[str, int]:
    """Counts per spec exit label; every label present (0 when none occurred)."""
    counts = {label: 0 for label in EXIT_LABELS.values()}
    for t in _closed(trades):
        assert t.exit_reason is not None  # _closed filters to exited trades
        counts[EXIT_LABELS[t.exit_reason]] += 1
    return counts


def sl_mult_distribution(trades: Sequence[Trade]) -> dict[float, int]:
    """Histogram of the ATR stop-loss multipliers chosen at entry."""
    return dict(sorted(Counter(t.sl_atr_mult for t in _closed(trades)).items()))


def tp_rr_distribution(trades: Sequence[Trade]) -> dict[float, int]:
    """Histogram of the take-profit risk-multiples chosen at entry."""
    return dict(sorted(Counter(t.tp_rr for t in _closed(trades)).items()))


# --------------------------------------------------------------------------- #
# Time-bucketed performance (need timestamps)
# --------------------------------------------------------------------------- #
def _exit_frame(
    trades: Sequence[Trade],
    timestamps: Sequence[pd.Timestamp],
) -> pd.DataFrame:
    closed = _closed(trades)
    rows = [
        {"ts": timestamps[t.exit_index], "r": t.r_multiple()}
        for t in closed
        if t.exit_index is not None  # always true post-_closed; narrows for mypy
    ]
    return pd.DataFrame(rows, columns=["ts", "r"])


def monthly_returns(
    trades: Sequence[Trade],
    timestamps: Sequence[pd.Timestamp] | None,
) -> dict[str, float]:
    """Sum of R per calendar month, keyed ``"YYYY-MM"`` (by exit time)."""
    if timestamps is None:
        return {}
    df = _exit_frame(trades, timestamps)
    if df.empty:
        return {}
    keys = df["ts"].dt.strftime("%Y-%m")
    return {k: float(v) for k, v in df["r"].groupby(keys).sum().items()}


def weekday_performance(
    trades: Sequence[Trade],
    timestamps: Sequence[pd.Timestamp] | None,
) -> dict[int, float]:
    """Sum of R per weekday (Mon=0 .. Sun=6), by exit time."""
    if timestamps is None:
        return {}
    df = _exit_frame(trades, timestamps)
    if df.empty:
        return {}
    return {int(k): float(v) for k, v in df["r"].groupby(df["ts"].dt.weekday).sum().items()}


def hour_performance(
    trades: Sequence[Trade],
    timestamps: Sequence[pd.Timestamp] | None,
) -> dict[int, float]:
    """Sum of R per hour-of-day (0..23), by exit time."""
    if timestamps is None:
        return {}
    df = _exit_frame(trades, timestamps)
    if df.empty:
        return {}
    return {int(k): float(v) for k, v in df["r"].groupby(df["ts"].dt.hour).sum().items()}


# --------------------------------------------------------------------------- #
# Simple EMA baseline (section: "Baseline comparison against a simple EMA model")
# --------------------------------------------------------------------------- #
def ema_baseline_policy(
    decision_close: Sequence[float],
    fast: int = 10,
    slow: int = 30,
    sl_atr_mult: float | None = None,
    tp_rr: float | None = None,
):
    """A plain fast/slow-EMA crossover policy for use with ``run_backtest``.

    Long while the fast EMA is above the slow EMA, short while below, hold until
    enough bars exist to define the slow EMA. It is intentionally dumb — the RL
    agent has to *beat* this to justify its complexity. The same ATR-stop / RR
    target machinery is reused so the comparison is apples-to-apples.
    """
    if fast >= slow:
        raise ValueError("fast EMA span must be shorter than slow")
    sl = config_default(sl_atr_mult, "atr_stop_mult")
    tp = config_default(tp_rr, "take_profit_rr")
    closes = pd.Series(list(decision_close), dtype="float64")
    fast_ema = closes.ewm(span=fast, adjust=False).mean()
    slow_ema = closes.ewm(span=slow, adjust=False).mean()

    def policy(i: int) -> Decision:
        if i < slow:  # slow EMA not yet meaningful
            return Decision(HOLD)
        if fast_ema.iloc[i] > slow_ema.iloc[i]:
            return Decision(BUY, sl, tp)
        if fast_ema.iloc[i] < slow_ema.iloc[i]:
            return Decision(SELL, sl, tp)
        return Decision(HOLD)

    return policy


def config_default(value, attr: str):
    """Return ``value`` if given, else the config default for ``attr``."""
    if value is not None:
        return value
    from .config import DEFAULT

    return getattr(DEFAULT, attr)


def baseline_comparison(
    strategy_trades: Sequence[Trade],
    baseline_trades: Sequence[Trade],
) -> dict[str, float | bool]:
    """Compare strategy return to the EMA baseline (both in R)."""
    strat = total_return(strategy_trades)
    base = total_return(baseline_trades)
    return {
        "strategy_return": strat,
        "baseline_return": base,
        "delta": strat - base,
        "strategy_outperforms": strat > base,
    }


# --------------------------------------------------------------------------- #
# Assembled report
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PerformanceReport:
    """Every metric the spec's Reports section names, for one window of trades."""

    total_return: float
    annualized_return: float | None
    max_drawdown: float
    monthly_returns: dict[str, float]
    trade_counts: dict[str, int]
    exit_reason_breakdown: dict[str, int]
    sl_mult_distribution: dict[float, int]
    tp_rr_distribution: dict[float, int]
    weekday_performance: dict[int, float]
    hour_performance: dict[int, float]
    rolling_sharpe: list[float] = field(default_factory=list)


def build_report(
    trades: Sequence[Trade],
    timestamps: Sequence[pd.Timestamp] | None = None,
    *,
    rolling_window: int = 20,
) -> PerformanceReport:
    """Assemble a full :class:`PerformanceReport` from closed trades.

    Pass each window's trades separately to get training / validation / test
    returns; the orchestrator reports all three plus the test-only champion read.
    """
    return PerformanceReport(
        total_return=total_return(trades),
        annualized_return=annualized_return(trades, timestamps),
        max_drawdown=max_drawdown(trades),
        monthly_returns=monthly_returns(trades, timestamps),
        trade_counts=trade_counts(trades),
        exit_reason_breakdown=exit_reason_breakdown(trades),
        sl_mult_distribution=sl_mult_distribution(trades),
        tp_rr_distribution=tp_rr_distribution(trades),
        weekday_performance=weekday_performance(trades, timestamps),
        hour_performance=hour_performance(trades, timestamps),
        rolling_sharpe=rolling_sharpe(trades, rolling_window) if len(_closed(trades)) >= rolling_window else [],
    )
