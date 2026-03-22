"""Backtest evaluation metrics for the IRB strategy.

THIS MODULE IS SACRED. The AutoResearch agent must NOT modify the metric
definitions or computation logic. Any changes invalidate all prior backtest
results and require full re-evaluation. Version hash tracks identity.

Computes all metrics required by the task specification:
- trade counts, win/loss rates, profit factor, expectancy
- drawdown, consecutive wins/losses, trade frequency
- exit type breakdown, filter rejection counts
- multi-window evaluation (30d, 90d, 1y, 5y)

All metrics are computed from a list of CompletedTrade records produced
by the backtesting engine.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass, field
from enum import Enum

VERSION: str = "1.2.0"


def metrics_version_hash() -> str:
    """SHA-256 hash of the metrics module version for identity tracking.

    Bump VERSION whenever computation logic changes (new fields, formula
    corrections, rounding changes). This hash participates in the
    reproducibility manifest so any metric drift is detectable.
    """
    return hashlib.sha256(f"metrics:{VERSION}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------


class TradeSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ExitReason(Enum):
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_STOP = "TIME_STOP"


class OrderCancelReason(Enum):
    TRIGGER_WINDOW_EXPIRED = "TRIGGER_WINDOW_EXPIRED"
    IRB_REPLACEMENT = "IRB_REPLACEMENT"


@dataclass
class CompletedTrade:
    """A single completed trade with full audit trail."""

    trade_id: int
    side: TradeSide
    entry_bar: int
    exit_bar: int
    entry_price: float
    exit_price: float
    stop_loss: float
    volume: float
    exit_reason: ExitReason
    pnl_pips: float = 0.0
    pnl_usd: float = 0.0
    risk_r: float = 0.0  # result in R-multiples
    hold_bars: int = 0
    max_favorable_excursion_pips: float = 0.0
    max_adverse_excursion_pips: float = 0.0
    entry_timestamp: float = 0.0
    exit_timestamp: float = 0.0

    def __post_init__(self) -> None:
        self.hold_bars = self.exit_bar - self.entry_bar


@dataclass
class PendingOrderRecord:
    """Record of a pending order (filled or expired)."""

    bar_placed: int
    side: TradeSide
    entry_price: float
    stop_loss: float
    filled: bool = False
    fill_bar: int | None = None
    cancel_reason: OrderCancelReason | None = None
    bars_alive: int = 0


@dataclass
class SignalRecord:
    """Record of an IRB signal detection (may or may not lead to a trade)."""

    bar_index: int
    side: TradeSide
    irb_range: float = 0.0
    ema_slope: float = 0.0
    adx_value: float = 0.0
    overextension_ratio: float = 0.0
    filters_passed: bool = True


@dataclass
class FilterRejection:
    """Counts of signal rejections by each filter."""

    irb_geometry: int = 0
    trend_filter: int = 0
    mtf_alignment: int = 0
    sideways_filter: int = 0
    overextension_filter: int = 0
    existing_position: int = 0
    existing_pending: int = 0
    warmup: int = 0
    session_filter: int = 0  # v4: rejected by session hours
    volatility_filter: int = 0  # v4: rejected by low-volatility regime
    circuit_breaker: int = 0  # v4: rejected by consecutive-loss breaker
    regime_gate: int = 0  # Tier 1: rejected by BBW ranging regime gate


# ---------------------------------------------------------------------------
# Evaluation window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationWindow:
    """A named time window for multi-regime evaluation."""

    name: str
    days: int
    description: str

    @property
    def approx_h1_bars(self) -> int:
        """Approximate H1 bars in this window (24 bars/trading day, ~5 days/week)."""
        trading_days = int(self.days * 5 / 7)
        return trading_days * 24


# Standard evaluation windows per task requirements
EVAL_WINDOWS = [
    EvaluationWindow("recent", 30, "Last 30 days — most recent market regime"),
    EvaluationWindow("medium", 90, "Last 90 days — multiple regime transitions"),
    EvaluationWindow("broad", 365, "Last 1 year — full seasonal cycle"),
    EvaluationWindow("extended", 1825, "Last 5 years — long-term structural validation"),
]


# ---------------------------------------------------------------------------
# Metrics report
# ---------------------------------------------------------------------------


@dataclass
class BacktestMetrics:
    """Complete evaluation metrics for one backtest window.

    Every metric listed in the task specification is present.
    Unavailable metrics are set to None with an explanation.
    """

    window: EvaluationWindow
    total_bars: int = 0

    # --- Trade counts ---
    total_setups_detected: int = 0
    total_pending_orders_placed: int = 0
    pending_orders_expired: int = 0
    pending_orders_replaced: int = 0
    total_completed_trades: int = 0
    long_trades: int = 0
    short_trades: int = 0

    # --- Win/loss ---
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0
    win_rate: float = 0.0
    loss_rate: float = 0.0

    # --- Profitability ---
    profit_factor: float = 0.0
    expectancy_r: float = 0.0
    expectancy_pips: float = 0.0
    net_result_pips: float = 0.0
    net_result_usd: float = 0.0
    average_trade_pips: float = 0.0
    average_trade_usd: float = 0.0
    average_winner_pips: float = 0.0
    average_loser_pips: float = 0.0

    # --- Drawdown ---
    max_drawdown_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    max_drawdown_duration_bars: int = 0

    # --- Extremes ---
    largest_win_pips: float = 0.0
    largest_win_usd: float = 0.0
    largest_loss_pips: float = 0.0
    largest_loss_usd: float = 0.0

    # --- Streaks ---
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0

    # --- Frequency / exposure ---
    trade_frequency_per_day: float = 0.0
    avg_hold_bars: float = 0.0
    exposure_pct: float = 0.0  # % of bars in a position

    # --- Concentration ---
    top_3_trades_pct_of_profit: float = 0.0

    # --- Risk-adjusted ratios (per-trade-return based) ---
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0

    # --- Exit breakdown ---
    stop_loss_exits: int = 0
    trailing_stop_exits: int = 0
    time_stop_exits: int = 0

    # --- Order lifecycle ---
    order_expiry_count: int = 0
    irb_replacement_count: int = 0

    # --- Filter rejections ---
    filter_rejections: FilterRejection = field(default_factory=FilterRejection)

    # --- Unavailable metrics ---
    unavailable: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialise all metrics to a dictionary."""
        return {
            "window": {"name": self.window.name, "days": self.window.days},
            "total_bars": self.total_bars,
            "total_setups_detected": self.total_setups_detected,
            "total_pending_orders_placed": self.total_pending_orders_placed,
            "pending_orders_expired": self.pending_orders_expired,
            "pending_orders_replaced": self.pending_orders_replaced,
            "total_completed_trades": self.total_completed_trades,
            "long_trades": self.long_trades,
            "short_trades": self.short_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "breakeven_trades": self.breakeven_trades,
            "win_rate": round(self.win_rate, 4),
            "loss_rate": round(self.loss_rate, 4),
            "profit_factor": round(self.profit_factor, 4),
            "expectancy_r": round(self.expectancy_r, 4),
            "expectancy_pips": round(self.expectancy_pips, 4),
            "net_result_pips": round(self.net_result_pips, 2),
            "net_result_usd": round(self.net_result_usd, 2),
            "average_trade_pips": round(self.average_trade_pips, 2),
            "average_trade_usd": round(self.average_trade_usd, 2),
            "average_winner_pips": round(self.average_winner_pips, 2),
            "average_loser_pips": round(self.average_loser_pips, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "max_drawdown_usd": round(self.max_drawdown_usd, 2),
            "max_drawdown_duration_bars": self.max_drawdown_duration_bars,
            "largest_win_pips": round(self.largest_win_pips, 2),
            "largest_win_usd": round(self.largest_win_usd, 2),
            "largest_loss_pips": round(self.largest_loss_pips, 2),
            "largest_loss_usd": round(self.largest_loss_usd, 2),
            "max_consecutive_wins": self.max_consecutive_wins,
            "max_consecutive_losses": self.max_consecutive_losses,
            "trade_frequency_per_day": round(self.trade_frequency_per_day, 4),
            "avg_hold_bars": round(self.avg_hold_bars, 2),
            "exposure_pct": round(self.exposure_pct, 4),
            "top_3_trades_pct_of_profit": round(self.top_3_trades_pct_of_profit, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "sortino_ratio": round(self.sortino_ratio, 4),
            "stop_loss_exits": self.stop_loss_exits,
            "trailing_stop_exits": self.trailing_stop_exits,
            "time_stop_exits": self.time_stop_exits,
            "order_expiry_count": self.order_expiry_count,
            "irb_replacement_count": self.irb_replacement_count,
            "filter_rejections": {
                "irb_geometry": self.filter_rejections.irb_geometry,
                "trend_filter": self.filter_rejections.trend_filter,
                "mtf_alignment": self.filter_rejections.mtf_alignment,
                "sideways_filter": self.filter_rejections.sideways_filter,
                "overextension_filter": self.filter_rejections.overextension_filter,
                "existing_position": self.filter_rejections.existing_position,
                "existing_pending": self.filter_rejections.existing_pending,
                "warmup": self.filter_rejections.warmup,
            },
            "unavailable": self.unavailable,
        }


# ---------------------------------------------------------------------------
# Metrics calculator
# ---------------------------------------------------------------------------


def compute_metrics(
    trades: list[CompletedTrade],
    pending_orders: list[PendingOrderRecord],
    signals: list[SignalRecord],
    filter_rejections: FilterRejection,
    window: EvaluationWindow,
    total_bars: int,
    initial_equity: float = 100_000.0,
) -> BacktestMetrics:
    """Compute all evaluation metrics from trade records.

    Args:
        trades: Completed trades in this window.
        pending_orders: All pending orders placed.
        signals: All IRB signals detected (passed all filters).
        filter_rejections: Counts of rejections per filter.
        window: The evaluation window being computed.
        total_bars: Total H1 bars in this window.
        initial_equity: Starting equity for drawdown computation.

    Returns:
        BacktestMetrics with every field populated.
    """
    m = BacktestMetrics(window=window, total_bars=total_bars)
    m.filter_rejections = filter_rejections

    # --- Trade counts ---
    m.total_setups_detected = len(signals)
    m.total_pending_orders_placed = len(pending_orders)
    m.pending_orders_expired = sum(
        1 for o in pending_orders if o.cancel_reason == OrderCancelReason.TRIGGER_WINDOW_EXPIRED
    )
    m.pending_orders_replaced = sum(1 for o in pending_orders if o.cancel_reason == OrderCancelReason.IRB_REPLACEMENT)
    m.order_expiry_count = m.pending_orders_expired
    m.irb_replacement_count = m.pending_orders_replaced

    m.total_completed_trades = len(trades)
    m.long_trades = sum(1 for t in trades if t.side == TradeSide.LONG)
    m.short_trades = sum(1 for t in trades if t.side == TradeSide.SHORT)

    if not trades:
        m.unavailable.append("No completed trades — most metrics are zero/not applicable")
        return m

    # --- Win/loss ---
    winners = [t for t in trades if t.pnl_pips > 0]
    losers = [t for t in trades if t.pnl_pips < 0]
    breakevens = [t for t in trades if t.pnl_pips == 0]

    m.winning_trades = len(winners)
    m.losing_trades = len(losers)
    m.breakeven_trades = len(breakevens)
    m.win_rate = m.winning_trades / m.total_completed_trades
    m.loss_rate = m.losing_trades / m.total_completed_trades

    # --- Profitability ---
    gross_profit_pips = sum(t.pnl_pips for t in winners)
    gross_loss_pips = abs(sum(t.pnl_pips for t in losers))
    m.profit_factor = gross_profit_pips / gross_loss_pips if gross_loss_pips > 0 else float("inf")

    all_pnl_pips = [t.pnl_pips for t in trades]
    all_pnl_usd = [t.pnl_usd for t in trades]
    all_r = [t.risk_r for t in trades]

    m.net_result_pips = sum(all_pnl_pips)
    m.net_result_usd = sum(all_pnl_usd)
    m.average_trade_pips = statistics.mean(all_pnl_pips)
    m.average_trade_usd = statistics.mean(all_pnl_usd)
    m.expectancy_pips = m.average_trade_pips
    m.expectancy_r = statistics.mean(all_r) if all_r else 0.0

    if winners:
        m.average_winner_pips = statistics.mean([t.pnl_pips for t in winners])
    if losers:
        m.average_loser_pips = statistics.mean([t.pnl_pips for t in losers])

    # --- Drawdown ---
    equity_curve = _compute_equity_curve(trades, initial_equity)
    dd_pct, dd_usd, dd_bars = _compute_max_drawdown(equity_curve, initial_equity)
    m.max_drawdown_pct = dd_pct
    m.max_drawdown_usd = dd_usd
    m.max_drawdown_duration_bars = dd_bars

    # --- Frequency / exposure ---
    trading_days = max(total_bars / 24, 1)

    # --- Risk-adjusted ratios (per-trade-return based, annualized) ---
    m.sharpe_ratio, m.sortino_ratio = _compute_sharpe_sortino(
        all_pnl_usd,
        initial_equity,
        trading_days,
    )

    # --- Extremes ---
    m.largest_win_pips = max((t.pnl_pips for t in winners), default=0.0)
    m.largest_win_usd = max((t.pnl_usd for t in winners), default=0.0)
    m.largest_loss_pips = min((t.pnl_pips for t in losers), default=0.0)
    m.largest_loss_usd = min((t.pnl_usd for t in losers), default=0.0)

    # --- Streaks ---
    m.max_consecutive_wins = _max_streak(trades, winning=True)
    m.max_consecutive_losses = _max_streak(trades, winning=False)
    m.trade_frequency_per_day = m.total_completed_trades / trading_days
    m.avg_hold_bars = statistics.mean([t.hold_bars for t in trades])

    bars_in_position = sum(t.hold_bars for t in trades)
    m.exposure_pct = (bars_in_position / total_bars * 100) if total_bars > 0 else 0.0

    # --- Concentration ---
    m.top_3_trades_pct_of_profit = _top_n_concentration(trades, n=3)

    # --- Exit breakdown ---
    m.stop_loss_exits = sum(1 for t in trades if t.exit_reason == ExitReason.STOP_LOSS)
    m.trailing_stop_exits = sum(1 for t in trades if t.exit_reason == ExitReason.TRAILING_STOP)
    m.time_stop_exits = sum(1 for t in trades if t.exit_reason == ExitReason.TIME_STOP)

    return m


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_equity_curve(trades: list[CompletedTrade], initial_equity: float) -> list[tuple[int, float]]:
    """Build (bar_index, equity) pairs from trade P&L sequence."""
    curve = [(0, initial_equity)]
    equity = initial_equity
    for t in sorted(trades, key=lambda x: x.exit_bar):
        equity += t.pnl_usd
        curve.append((t.exit_bar, equity))
    return curve


def _compute_max_drawdown(equity_curve: list[tuple[int, float]], initial_equity: float) -> tuple[float, float, int]:
    """Return (max_dd_pct, max_dd_usd, max_dd_duration_bars)."""
    if len(equity_curve) < 2:
        return 0.0, 0.0, 0

    peak = equity_curve[0][1]
    peak_bar = equity_curve[0][0]
    max_dd_pct = 0.0
    max_dd_usd = 0.0
    max_dd_bars = 0

    for bar, eq in equity_curve[1:]:
        if eq >= peak:
            peak = eq
            peak_bar = bar
        else:
            dd_usd = peak - eq
            dd_pct = (dd_usd / peak) * 100 if peak > 0 else 0.0
            dd_bars = bar - peak_bar
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                max_dd_usd = dd_usd
                max_dd_bars = dd_bars

    return max_dd_pct, max_dd_usd, max_dd_bars


def _max_streak(trades: list[CompletedTrade], winning: bool) -> int:
    """Count max consecutive wins or losses."""
    max_streak = 0
    current = 0
    for t in trades:
        if (winning and t.pnl_pips > 0) or (not winning and t.pnl_pips < 0):
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


def _compute_sharpe_sortino(
    trade_pnl_usd: list[float],
    initial_equity: float,
    trading_days: float,
    risk_free_annual: float = 0.0,
) -> tuple[float, float]:
    """Compute annualized Sharpe and Sortino ratios from per-trade returns.

    Per-trade returns are computed as pnl_usd / initial_equity.  Annualization
    uses ``sqrt(trades_per_year)`` scaling where trades_per_year is estimated
    from trade count and trading days.

    These are per-trade-return-based approximations.  True daily-return-based
    ratios require a daily equity series which is not available at this level.

    Args:
        trade_pnl_usd: List of per-trade P&L in USD.
        initial_equity: Starting account equity (for return computation).
        trading_days: Number of trading days in the backtest window.
        risk_free_annual: Annual risk-free rate (default 0.0).

    Returns:
        (sharpe_ratio, sortino_ratio) tuple, both annualized.
    """
    if len(trade_pnl_usd) < 2 or initial_equity <= 0:
        return 0.0, 0.0

    # Per-trade fractional returns
    returns = [pnl / initial_equity for pnl in trade_pnl_usd]

    mean_return = statistics.mean(returns)
    std_return = statistics.stdev(returns)

    # Annualization factor: sqrt(trades per year)
    # trades_per_year = (n_trades / trading_days) * 252
    n_trades = len(returns)
    trades_per_year = (n_trades / max(trading_days, 1.0)) * 252.0
    annualization = trades_per_year**0.5

    # Per-trade risk-free rate
    rf_per_trade = risk_free_annual / max(trades_per_year, 1.0)

    # Sharpe ratio: (mean_return - rf) / std * sqrt(trades_per_year)
    if std_return > 0:
        sharpe = ((mean_return - rf_per_trade) / std_return) * annualization
    else:
        sharpe = 0.0

    # Sortino ratio: (mean_return - rf) / downside_deviation * sqrt(trades_per_year)
    # Downside deviation uses ALL returns: sqrt(1/N * sum(min(r, 0)^2))
    # Target = 0 (industry standard MAR for trade-return Sortino).
    downside_squares = [min(r, 0.0) ** 2 for r in returns]
    downside_dev = (sum(downside_squares) / len(returns)) ** 0.5

    if downside_dev > 0:
        sortino = ((mean_return - rf_per_trade) / downside_dev) * annualization
    else:
        sortino = 0.0

    return sharpe, sortino


def _top_n_concentration(trades: list[CompletedTrade], n: int = 3) -> float:
    """What fraction of total profit comes from the top N trades."""
    if not trades:
        return 0.0
    total_profit = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    if total_profit <= 0:
        return 0.0
    sorted_by_profit = sorted(trades, key=lambda t: t.pnl_usd, reverse=True)
    top_n_profit = sum(t.pnl_usd for t in sorted_by_profit[:n] if t.pnl_usd > 0)
    return (top_n_profit / total_profit) * 100


def format_metrics_report(metrics: BacktestMetrics) -> str:
    """Render metrics as a human-readable markdown section."""
    m = metrics
    lines = [
        f"### {m.window.name.title()} Window ({m.window.days} days)",
        f"_{m.window.description}_\n",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total bars | {m.total_bars} |",
        f"| Setups detected | {m.total_setups_detected} |",
        f"| Pending orders placed | {m.total_pending_orders_placed} |",
        f"| Orders expired | {m.pending_orders_expired} |",
        f"| Orders replaced (IRB) | {m.pending_orders_replaced} |",
        f"| Completed trades | {m.total_completed_trades} |",
        f"| Long trades | {m.long_trades} |",
        f"| Short trades | {m.short_trades} |",
        f"| Winning trades | {m.winning_trades} |",
        f"| Losing trades | {m.losing_trades} |",
        f"| Win rate | {m.win_rate:.1%} |",
        f"| Loss rate | {m.loss_rate:.1%} |",
        f"| Profit factor | {m.profit_factor:.2f} |",
        f"| Expectancy (R) | {m.expectancy_r:.2f}R |",
        f"| Expectancy (pips) | {m.expectancy_pips:.1f} |",
        f"| Net result (pips) | {m.net_result_pips:.1f} |",
        f"| Net result (USD) | ${m.net_result_usd:.2f} |",
        f"| Average trade (pips) | {m.average_trade_pips:.1f} |",
        f"| Average winner (pips) | {m.average_winner_pips:.1f} |",
        f"| Average loser (pips) | {m.average_loser_pips:.1f} |",
        f"| Max drawdown (%) | {m.max_drawdown_pct:.2f}% |",
        f"| Max drawdown (USD) | ${m.max_drawdown_usd:.2f} |",
        f"| Max DD duration (bars) | {m.max_drawdown_duration_bars} |",
        f"| Largest win (pips) | {m.largest_win_pips:.1f} |",
        f"| Largest loss (pips) | {m.largest_loss_pips:.1f} |",
        f"| Max consecutive wins | {m.max_consecutive_wins} |",
        f"| Max consecutive losses | {m.max_consecutive_losses} |",
        f"| Trade frequency (/day) | {m.trade_frequency_per_day:.3f} |",
        f"| Avg hold (bars) | {m.avg_hold_bars:.1f} |",
        f"| Exposure (%) | {m.exposure_pct:.1f}% |",
        f"| Top 3 trades % of profit | {m.top_3_trades_pct_of_profit:.1f}% |",
        f"| SL exits | {m.stop_loss_exits} |",
        f"| Trail exits | {m.trailing_stop_exits} |",
        f"| Time stop exits | {m.time_stop_exits} |",
    ]

    if m.unavailable:
        lines.append(f"\n**Unavailable metrics:** {', '.join(m.unavailable)}")

    return "\n".join(lines)
