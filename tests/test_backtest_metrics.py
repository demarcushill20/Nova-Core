"""Tests for novatrade.backtest.metrics."""

import math
import statistics

import pytest

from novatrade.backtest.metrics import (
    EVAL_WINDOWS,
    BacktestMetrics,
    CompletedTrade,
    EvaluationWindow,
    ExitReason,
    FilterRejection,
    OrderCancelReason,
    PendingOrderRecord,
    TradeSide,
    _compute_sharpe_sortino,
    compute_metrics,
    format_metrics_report,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_trade(
    trade_id: int,
    side: TradeSide = TradeSide.LONG,
    entry_bar: int = 0,
    exit_bar: int = 10,
    entry_price: float = 1.1000,
    exit_price: float = 1.1050,
    stop_loss: float = 1.0950,
    volume: float = 0.10,
    exit_reason: ExitReason = ExitReason.TRAILING_STOP,
    pnl_pips: float = 50.0,
    pnl_usd: float = 50.0,
    risk_r: float = 1.0,
) -> CompletedTrade:
    return CompletedTrade(
        trade_id=trade_id,
        side=side,
        entry_bar=entry_bar,
        exit_bar=exit_bar,
        entry_price=entry_price,
        exit_price=exit_price,
        stop_loss=stop_loss,
        volume=volume,
        exit_reason=exit_reason,
        pnl_pips=pnl_pips,
        pnl_usd=pnl_usd,
        risk_r=risk_r,
    )


WINDOW_30D = EvaluationWindow("recent", 30, "test")


class TestEvaluationWindows:
    def test_standard_windows_exist(self):
        assert len(EVAL_WINDOWS) == 4
        names = [w.name for w in EVAL_WINDOWS]
        assert "recent" in names
        assert "medium" in names
        assert "broad" in names
        assert "extended" in names

    def test_window_approx_bars(self):
        w30 = EVAL_WINDOWS[0]  # 30 days
        bars = w30.approx_h1_bars
        assert 400 <= bars <= 600  # ~21 trading days * 24


class TestCompletedTrade:
    def test_hold_bars_computed(self):
        t = _make_trade(1, entry_bar=5, exit_bar=15)
        assert t.hold_bars == 10

    def test_negative_pnl(self):
        t = _make_trade(1, pnl_pips=-30.0, pnl_usd=-30.0, risk_r=-0.6)
        assert t.pnl_pips < 0
        assert t.risk_r < 0


class TestComputeMetrics:
    def test_empty_trades(self):
        m = compute_metrics([], [], [], FilterRejection(), WINDOW_30D, 480)
        assert m.total_completed_trades == 0
        assert len(m.unavailable) > 0

    def test_single_winning_trade(self):
        trades = [_make_trade(1, pnl_pips=50, pnl_usd=50, risk_r=1.0)]
        m = compute_metrics(trades, [], [], FilterRejection(), WINDOW_30D, 480)
        assert m.total_completed_trades == 1
        assert m.winning_trades == 1
        assert m.losing_trades == 0
        assert m.win_rate == 1.0
        assert m.net_result_pips == 50.0

    def test_single_losing_trade(self):
        trades = [_make_trade(1, pnl_pips=-30, pnl_usd=-30, risk_r=-0.6)]
        m = compute_metrics(trades, [], [], FilterRejection(), WINDOW_30D, 480)
        assert m.winning_trades == 0
        assert m.losing_trades == 1
        assert m.loss_rate == 1.0

    def test_mixed_trades_profit_factor(self):
        trades = [
            _make_trade(1, pnl_pips=100, pnl_usd=100, risk_r=2.0, exit_reason=ExitReason.TRAILING_STOP),
            _make_trade(2, pnl_pips=-50, pnl_usd=-50, risk_r=-1.0, exit_reason=ExitReason.STOP_LOSS),
            _make_trade(3, pnl_pips=30, pnl_usd=30, risk_r=0.6, exit_reason=ExitReason.TIME_STOP),
        ]
        m = compute_metrics(trades, [], [], FilterRejection(), WINDOW_30D, 480)
        assert m.total_completed_trades == 3
        assert m.winning_trades == 2
        assert m.losing_trades == 1
        assert m.profit_factor == pytest.approx(130.0 / 50.0, rel=0.01)
        assert m.net_result_pips == 80.0

    def test_long_short_split(self):
        trades = [
            _make_trade(1, side=TradeSide.LONG, pnl_pips=50, pnl_usd=50),
            _make_trade(2, side=TradeSide.SHORT, pnl_pips=30, pnl_usd=30),
            _make_trade(3, side=TradeSide.LONG, pnl_pips=-20, pnl_usd=-20),
        ]
        m = compute_metrics(trades, [], [], FilterRejection(), WINDOW_30D, 480)
        assert m.long_trades == 2
        assert m.short_trades == 1

    def test_exit_breakdown(self):
        trades = [
            _make_trade(1, exit_reason=ExitReason.STOP_LOSS, pnl_pips=-30, pnl_usd=-30),
            _make_trade(2, exit_reason=ExitReason.TRAILING_STOP, pnl_pips=80, pnl_usd=80),
            _make_trade(3, exit_reason=ExitReason.TIME_STOP, pnl_pips=5, pnl_usd=5),
            _make_trade(4, exit_reason=ExitReason.STOP_LOSS, pnl_pips=-25, pnl_usd=-25),
        ]
        m = compute_metrics(trades, [], [], FilterRejection(), WINDOW_30D, 480)
        assert m.stop_loss_exits == 2
        assert m.trailing_stop_exits == 1
        assert m.time_stop_exits == 1

    def test_consecutive_wins(self):
        trades = [
            _make_trade(1, pnl_pips=10, pnl_usd=10),
            _make_trade(2, pnl_pips=20, pnl_usd=20),
            _make_trade(3, pnl_pips=15, pnl_usd=15),
            _make_trade(4, pnl_pips=-10, pnl_usd=-10),
            _make_trade(5, pnl_pips=5, pnl_usd=5),
        ]
        m = compute_metrics(trades, [], [], FilterRejection(), WINDOW_30D, 480)
        assert m.max_consecutive_wins == 3
        assert m.max_consecutive_losses == 1

    def test_consecutive_losses(self):
        trades = [
            _make_trade(1, pnl_pips=-10, pnl_usd=-10),
            _make_trade(2, pnl_pips=-20, pnl_usd=-20),
            _make_trade(3, pnl_pips=-15, pnl_usd=-15),
            _make_trade(4, pnl_pips=100, pnl_usd=100),
        ]
        m = compute_metrics(trades, [], [], FilterRejection(), WINDOW_30D, 480)
        assert m.max_consecutive_losses == 3
        assert m.max_consecutive_wins == 1

    def test_drawdown_computation(self):
        trades = [
            _make_trade(1, exit_bar=10, pnl_pips=50, pnl_usd=500),
            _make_trade(2, exit_bar=20, pnl_pips=-80, pnl_usd=-800),
            _make_trade(3, exit_bar=30, pnl_pips=-40, pnl_usd=-400),
            _make_trade(4, exit_bar=40, pnl_pips=100, pnl_usd=1000),
        ]
        m = compute_metrics(
            trades,
            [],
            [],
            FilterRejection(),
            WINDOW_30D,
            480,
            initial_equity=100_000,
        )
        # Peak at 100500, trough at 99300 → dd = 1200/100500 ≈ 1.19%
        assert m.max_drawdown_pct > 0
        assert m.max_drawdown_usd > 0

    def test_pending_orders_counting(self):
        orders = [
            PendingOrderRecord(
                bar_placed=10,
                side=TradeSide.LONG,
                entry_price=1.1,
                stop_loss=1.09,
                filled=True,
                fill_bar=12,
                bars_alive=2,
            ),
            PendingOrderRecord(
                bar_placed=30,
                side=TradeSide.SHORT,
                entry_price=1.08,
                stop_loss=1.09,
                cancel_reason=OrderCancelReason.TRIGGER_WINDOW_EXPIRED,
                bars_alive=20,
            ),
            PendingOrderRecord(
                bar_placed=60,
                side=TradeSide.LONG,
                entry_price=1.12,
                stop_loss=1.11,
                cancel_reason=OrderCancelReason.IRB_REPLACEMENT,
                bars_alive=5,
            ),
        ]
        m = compute_metrics(
            [_make_trade(1, pnl_pips=10, pnl_usd=10)],
            orders,
            [],
            FilterRejection(),
            WINDOW_30D,
            480,
        )
        assert m.total_pending_orders_placed == 3
        assert m.pending_orders_expired == 1
        assert m.pending_orders_replaced == 1

    def test_filter_rejections(self):
        rej = FilterRejection(
            irb_geometry=100,
            trend_filter=30,
            mtf_alignment=15,
            sideways_filter=10,
            overextension_filter=5,
        )
        m = compute_metrics(
            [_make_trade(1, pnl_pips=10, pnl_usd=10)],
            [],
            [],
            rej,
            WINDOW_30D,
            480,
        )
        assert m.filter_rejections.irb_geometry == 100
        assert m.filter_rejections.trend_filter == 30

    def test_exposure_pct(self):
        trades = [
            _make_trade(1, entry_bar=10, exit_bar=30, pnl_pips=10, pnl_usd=10),
            _make_trade(2, entry_bar=50, exit_bar=60, pnl_pips=5, pnl_usd=5),
        ]
        m = compute_metrics(trades, [], [], FilterRejection(), WINDOW_30D, 480)
        # 20 + 10 = 30 bars in position out of 480
        assert m.exposure_pct == pytest.approx(30 / 480 * 100, rel=0.01)

    def test_trade_frequency(self):
        trades = [_make_trade(i, pnl_pips=10, pnl_usd=10) for i in range(10)]
        m = compute_metrics(trades, [], [], FilterRejection(), WINDOW_30D, 480)
        # 480 bars / 24 = 20 trading days → 10/20 = 0.5 trades/day
        assert m.trade_frequency_per_day == pytest.approx(0.5, rel=0.01)


class TestFormatMetricsReport:
    def test_report_contains_window_name(self):
        m = BacktestMetrics(window=WINDOW_30D, total_bars=480)
        report = format_metrics_report(m)
        assert "Recent" in report
        assert "30 days" in report

    def test_report_contains_key_metrics(self):
        trades = [
            _make_trade(1, pnl_pips=50, pnl_usd=50),
            _make_trade(2, pnl_pips=-30, pnl_usd=-30),
        ]
        m = compute_metrics(trades, [], [], FilterRejection(), WINDOW_30D, 480)
        report = format_metrics_report(m)
        assert "Win rate" in report
        assert "Profit factor" in report
        assert "Max drawdown" in report


class TestTopNConcentration:
    def test_all_winners(self):
        trades = [
            _make_trade(1, pnl_pips=100, pnl_usd=100),
            _make_trade(2, pnl_pips=50, pnl_usd=50),
            _make_trade(3, pnl_pips=30, pnl_usd=30),
            _make_trade(4, pnl_pips=20, pnl_usd=20),
        ]
        m = compute_metrics(trades, [], [], FilterRejection(), WINDOW_30D, 480)
        # Top 3 = 100+50+30=180 out of 200 total profit = 90%
        assert m.top_3_trades_pct_of_profit == pytest.approx(90.0, rel=0.01)


class TestMetricsToDict:
    def test_serialisation(self):
        m = BacktestMetrics(window=WINDOW_30D, total_bars=100)
        d = m.to_dict()
        assert d["window"]["name"] == "recent"
        assert d["total_bars"] == 100
        assert "win_rate" in d
        assert "profit_factor" in d
        assert "filter_rejections" in d
        assert "sharpe_ratio" in d
        assert "sortino_ratio" in d


# ---------------------------------------------------------------------------
# H3: Recovery factor uses consistent units (USD/USD)
# ---------------------------------------------------------------------------


class TestRecoveryFactorUnits:
    """Bug H3: Recovery factor must use net_result_usd / max_drawdown_usd,
    NOT net_result_pips / max_drawdown_usd (unit mismatch)."""

    def test_recovery_factor_consistent_usd_units(self):
        """Verify evaluation_ladder computes recovery_factor as net_usd / max_dd_usd."""
        from novatrade.evaluation.evaluation_ladder import compute_dashboard_metrics

        class MockMetrics:
            total_completed_trades = 100
            profit_factor = 1.8
            max_drawdown_pct = 5.0
            max_drawdown_usd = 2000.0
            net_result_pips = 500.0  # pips -- should NOT be used
            net_result_usd = 4000.0  # USD -- should be used
            total_bars = 24 * 22 * 6
            expectancy_r = 0.5
            win_rate = 0.55
            max_consecutive_losses = 4
            top_3_trades_pct_of_profit = 25.0
            average_winner_pips = 30.0
            average_loser_pips = -15.0
            sharpe_ratio = 0.0
            sortino_ratio = 0.0

        row = compute_dashboard_metrics(MockMetrics(), experiment_id="rf-test")
        # recovery_factor = net_usd / max_dd_usd = 4000 / 2000 = 2.0
        assert row.recovery_factor == pytest.approx(2.0, rel=0.01)

    def test_recovery_factor_not_pips_over_usd(self):
        """Ensure net_pips is NOT divided by max_dd_usd (the original bug)."""
        from novatrade.evaluation.evaluation_ladder import compute_dashboard_metrics

        class MockMetrics:
            total_completed_trades = 100
            profit_factor = 1.8
            max_drawdown_pct = 5.0
            max_drawdown_usd = 500.0
            net_result_pips = 200.0  # would give 0.4 if divided by max_dd_usd
            net_result_usd = 2000.0  # gives 4.0 (correct)
            total_bars = 24 * 22 * 6
            expectancy_r = 0.5
            win_rate = 0.55
            max_consecutive_losses = 4
            top_3_trades_pct_of_profit = 25.0
            average_winner_pips = 30.0
            average_loser_pips = -15.0
            sharpe_ratio = 0.0
            sortino_ratio = 0.0

        row = compute_dashboard_metrics(MockMetrics(), experiment_id="rf-test2")
        # Should be 4.0 (USD/USD), NOT 0.4 (pips/USD)
        assert row.recovery_factor == pytest.approx(4.0, rel=0.01)
        assert row.recovery_factor != pytest.approx(0.4, abs=0.1)

    def test_recovery_factor_zero_drawdown(self):
        """When drawdown is zero, recovery_factor should be 5.0 cap (positive net) or 0.0."""
        from novatrade.evaluation.evaluation_ladder import compute_dashboard_metrics

        class MockMetrics:
            total_completed_trades = 100
            profit_factor = 2.0
            max_drawdown_pct = 0.0
            max_drawdown_usd = 0.0
            net_result_pips = 300.0
            net_result_usd = 3000.0
            total_bars = 24 * 22 * 6
            expectancy_r = 0.5
            win_rate = 0.55
            max_consecutive_losses = 3
            top_3_trades_pct_of_profit = 25.0
            average_winner_pips = 30.0
            average_loser_pips = -15.0
            sharpe_ratio = 0.0
            sortino_ratio = 0.0

        row = compute_dashboard_metrics(MockMetrics(), experiment_id="rf-zero")
        assert row.recovery_factor == 5.0


# ---------------------------------------------------------------------------
# H4: Sharpe and Sortino — proper annualized formulas
# ---------------------------------------------------------------------------


class TestSharpeSortinoFormulas:
    """Bug H4: Sharpe and Sortino must use proper formulas with annualization,
    not rough proxies based on drawdown."""

    def test_sharpe_known_values(self):
        """Verify Sharpe ratio against hand-computed known values.

        Setup: 10 trades over 20 trading days, equity=100000
          P&L: [100, -50, 80, -30, 120, -40, 90, -20, 110, -60]
          Returns: [0.001, -0.0005, 0.0008, -0.0003, 0.0012, -0.0004, 0.0009, -0.0002, 0.0011, -0.0006]
          Mean return: 0.0004 (hand-verified)
          Std return: stdev([...])
          trades_per_year = (10 / 20) * 252 = 126
          annualization = sqrt(126)
          Sharpe = (mean / std) * sqrt(126)
        """
        pnl = [100.0, -50.0, 80.0, -30.0, 120.0, -40.0, 90.0, -20.0, 110.0, -60.0]
        equity = 100_000.0
        trading_days = 20.0

        sharpe, sortino = _compute_sharpe_sortino(pnl, equity, trading_days)

        # Hand-compute expected
        returns = [p / equity for p in pnl]
        mean_r = statistics.mean(returns)
        std_r = statistics.stdev(returns)
        tpy = (len(returns) / trading_days) * 252.0
        ann = math.sqrt(tpy)
        expected_sharpe = (mean_r / std_r) * ann

        assert sharpe == pytest.approx(expected_sharpe, rel=1e-6)
        assert sharpe > 0  # net positive returns

    def test_sortino_known_values(self):
        """Verify Sortino uses proper downside deviation formula.

        Downside deviation = sqrt(1/N * sum(min(r, 0)^2 for ALL r))
        """
        pnl = [100.0, -50.0, 80.0, -30.0, 120.0, -40.0, 90.0, -20.0, 110.0, -60.0]
        equity = 100_000.0
        trading_days = 20.0

        sharpe, sortino = _compute_sharpe_sortino(pnl, equity, trading_days)

        # Hand-compute expected Sortino
        returns = [p / equity for p in pnl]
        mean_r = statistics.mean(returns)
        tpy = (len(returns) / trading_days) * 252.0
        ann = math.sqrt(tpy)

        # Downside deviation: sqrt(1/N * sum(min(r, 0)^2))
        dd_squares = [min(r, 0.0) ** 2 for r in returns]
        dd = (sum(dd_squares) / len(returns)) ** 0.5

        expected_sortino = (mean_r / dd) * ann

        assert sortino == pytest.approx(expected_sortino, rel=1e-6)
        assert sortino > 0

    def test_sortino_greater_than_sharpe_mixed_returns(self):
        """With mixed returns, Sortino should generally be >= Sharpe
        because downside deviation <= total std deviation."""
        pnl = [100.0, -50.0, 80.0, -30.0, 120.0, -40.0, 90.0, -20.0, 110.0, -60.0]
        equity = 100_000.0
        trading_days = 20.0

        sharpe, sortino = _compute_sharpe_sortino(pnl, equity, trading_days)
        assert sortino >= sharpe

    def test_sharpe_all_positive_returns(self):
        """All positive returns: Sortino should be 0 (no downside)."""
        pnl = [100.0, 200.0, 150.0, 80.0, 120.0]
        equity = 100_000.0
        trading_days = 10.0

        sharpe, sortino = _compute_sharpe_sortino(pnl, equity, trading_days)
        assert sharpe > 0
        # No negative returns => downside_dev = 0 => sortino = 0
        assert sortino == 0.0

    def test_sharpe_all_negative_returns(self):
        """All negative returns: both should be negative (or zero)."""
        pnl = [-100.0, -200.0, -150.0, -80.0, -120.0]
        equity = 100_000.0
        trading_days = 10.0

        sharpe, sortino = _compute_sharpe_sortino(pnl, equity, trading_days)
        assert sharpe < 0
        assert sortino < 0

    def test_sharpe_too_few_trades(self):
        """Less than 2 trades should return (0, 0)."""
        sharpe, sortino = _compute_sharpe_sortino([100.0], 100_000.0, 10.0)
        assert sharpe == 0.0
        assert sortino == 0.0

    def test_sharpe_zero_equity(self):
        """Zero initial equity should return (0, 0)."""
        sharpe, sortino = _compute_sharpe_sortino([100.0, 200.0], 0.0, 10.0)
        assert sharpe == 0.0
        assert sortino == 0.0

    def test_sharpe_with_risk_free_rate(self):
        """Risk-free rate should reduce Sharpe/Sortino ratios."""
        pnl = [100.0, -50.0, 80.0, -30.0, 120.0, -40.0, 90.0, -20.0, 110.0, -60.0]
        equity = 100_000.0
        trading_days = 20.0

        sharpe_0, _ = _compute_sharpe_sortino(pnl, equity, trading_days, risk_free_annual=0.0)
        sharpe_rf, _ = _compute_sharpe_sortino(pnl, equity, trading_days, risk_free_annual=0.05)
        # With positive risk-free rate, Sharpe should be lower
        assert sharpe_rf < sharpe_0

    def test_compute_metrics_populates_sharpe_sortino(self):
        """compute_metrics should populate sharpe_ratio and sortino_ratio fields."""
        trades = [
            _make_trade(i, pnl_pips=float(pnl), pnl_usd=float(pnl * 10), exit_bar=i * 10 + 10)
            for i, pnl in enumerate([50, -30, 40, -20, 60, -25, 35, -15, 55, -35], start=1)
        ]
        m = compute_metrics(trades, [], [], FilterRejection(), WINDOW_30D, 480)
        # With mixed trades, both ratios should be non-zero
        assert m.sharpe_ratio != 0.0
        assert m.sortino_ratio != 0.0

    def test_annualization_scales_correctly(self):
        """Same returns but different trading_days should produce different Sharpe."""
        pnl = [100.0, -50.0, 80.0, -30.0, 120.0]
        equity = 100_000.0

        sharpe_20, _ = _compute_sharpe_sortino(pnl, equity, 20.0)
        sharpe_100, _ = _compute_sharpe_sortino(pnl, equity, 100.0)
        # More trades per day (20 days) = higher annualization = higher Sharpe
        assert abs(sharpe_20) > abs(sharpe_100)
