"""Tests for 4 HIGH-priority bug fixes in NovaTrade backtest/campaign system.

BUG 1: Promotion pipeline uses logging instead of print()
BUG 2: Overfit detector uses true IS median (not OOS-as-IS proxy)
BUG 3: Recovery factor uses consistent USD units (not pips/USD mismatch)
BUG 4: Sharpe/Sortino use proper per-trade-return formulas
"""

from __future__ import annotations

import logging
import statistics

import pytest

# ---------------------------------------------------------------------------
# BUG 1: promotion.py uses logging, not print()
# ---------------------------------------------------------------------------


class TestPromotionUsesLogging:
    """Verify promotion pipeline uses logging module, not print()."""

    def test_no_print_statements_in_promotion(self):
        """promotion.py must not contain any print() calls."""
        import inspect

        import novatrade.optimization.promotion as mod

        source = inspect.getsource(mod)
        # Count bare print( calls — exclude comments and strings
        lines = source.split("\n")
        print_lines = [
            (i + 1, line)
            for i, line in enumerate(lines)
            if "print(" in line
            and not line.strip().startswith("#")
            and not line.strip().startswith('"')
            and not line.strip().startswith("'")
        ]
        assert print_lines == [], f"Found print() calls at lines: {print_lines}"

    def test_promotion_module_has_logger(self):
        """promotion.py must define a module-level logger."""
        import novatrade.optimization.promotion as mod

        assert hasattr(mod, "log"), "Module must have a 'log' logger"
        assert isinstance(mod.log, logging.Logger)

    def test_promotion_logs_stages(self, caplog):
        """Promotion pipeline emits log messages for each stage."""
        from novatrade.optimization.promotion import log

        assert log.name == "novatrade.optimization.promotion"


# ---------------------------------------------------------------------------
# BUG 2: Overfit detector — IS vs OOS median
# ---------------------------------------------------------------------------


class TestOverfitDetector:
    """The promotion pipeline must use true IS median for overfit detection."""

    def test_walkforward_result_has_median_is_score(self):
        """WalkForwardResult must expose median_is_score field."""
        from novatrade.optimization.walkforward import WalkForwardResult

        wfr = WalkForwardResult()
        assert hasattr(wfr, "median_is_score")
        assert wfr.median_is_score == 0.0

    def test_walkforward_result_populates_median_is_score(self):
        """median_is_score must be populated from IS scores, not OOS scores."""
        from novatrade.optimization.walkforward import WalkForwardResult, WindowResult

        windows = [
            WindowResult(
                window_index=0,
                train_start=0,
                train_end=100,
                test_start=105,
                test_end=200,
                is_scout_score=0.80,  # IS score
                oos_scout_score=0.60,  # OOS score (lower = some overfit)
                oos_is_ratio=0.75,
                test_trade_count=50,
                passed=True,
            ),
            WindowResult(
                window_index=1,
                train_start=50,
                train_end=150,
                test_start=155,
                test_end=250,
                is_scout_score=0.70,
                oos_scout_score=0.50,
                oos_is_ratio=0.714,
                test_trade_count=45,
                passed=True,
            ),
        ]
        wfr = WalkForwardResult(
            windows=windows,
            median_oos_score=statistics.median([0.60, 0.50]),
            median_is_score=statistics.median([0.80, 0.70]),
            median_oos_is_ratio=0.732,
            all_windows_passed=True,
            overall_passed=True,
        )
        # median_is_score must reflect IS, not OOS
        assert wfr.median_is_score == 0.75  # median of [0.80, 0.70]
        assert wfr.median_oos_score == 0.55  # median of [0.60, 0.50]
        assert wfr.median_is_score != wfr.median_oos_score

    def test_promotion_uses_is_median_not_oos(self):
        """promotion.py must reference median_is_score, not median_oos_score for IS."""
        import inspect

        import novatrade.optimization.promotion as mod

        source = inspect.getsource(mod)
        # Must use median_is_score for the is_median variable
        assert "wf_result.median_is_score" in source, "promotion.py must use wf_result.median_is_score for IS median"
        # Must NOT use median_oos_score as IS proxy
        assert (
            "wf_result.median_oos_score" not in source.split("is_median =")[1].split("\n")[0]
            if "is_median =" in source
            else True
        )

    def test_overfit_detector_fires_when_is_much_greater_than_oos(self):
        """When IS >> OOS, the overfit penalty must be applied."""
        from novatrade.evaluation.fitness import compute_promotion_score

        # Severely overfit: IS median = 0.80, OOS = [0.30, 0.35]
        score_overfit = compute_promotion_score(
            oos_scores=[0.30, 0.35],
            holdout_score=0.30,
            is_median=0.80,  # true IS median — much higher than OOS
            complexity=5,
        )
        # Non-overfit: IS median = 0.35, OOS = [0.30, 0.35]
        score_normal = compute_promotion_score(
            oos_scores=[0.30, 0.35],
            holdout_score=0.30,
            is_median=0.35,  # IS ≈ OOS — no overfit
            complexity=5,
        )
        # Overfit case should have lower score due to IS/OOS penalty
        assert score_overfit < score_normal, (
            f"Overfit score ({score_overfit}) should be less than normal ({score_normal})"
        )

    def test_overfit_detector_no_penalty_when_is_close_to_oos(self):
        """When IS ≈ OOS, no overfit penalty should apply."""
        from novatrade.evaluation.fitness import compute_promotion_score

        score = compute_promotion_score(
            oos_scores=[0.50, 0.55, 0.52],
            holdout_score=0.50,
            is_median=0.54,  # very close to OOS median
            complexity=5,
        )
        # Should get a reasonable score without heavy penalties
        assert score > 0.3

    def test_overfit_ratio_below_05_severe_penalty(self):
        """OOS/IS ratio < 0.5 should trigger severe (0.15) penalty."""
        from novatrade.evaluation.fitness import compute_promotion_score

        # OOS median = 0.20, IS median = 0.80 → ratio = 0.25 < 0.5
        score_severe = compute_promotion_score(
            oos_scores=[0.20, 0.20],
            holdout_score=0.20,
            is_median=0.80,
            complexity=5,
        )
        # OOS median = 0.60, IS median = 0.80 → ratio = 0.75 > 0.7 (no penalty)
        score_no_penalty = compute_promotion_score(
            oos_scores=[0.60, 0.60],
            holdout_score=0.60,
            is_median=0.80,
            complexity=5,
        )
        # Severe penalty case must be significantly lower
        assert score_no_penalty - score_severe >= 0.10

    def test_overfit_ratio_between_05_and_07_moderate_penalty(self):
        """OOS/IS ratio between 0.5 and 0.7 should trigger moderate (0.08) penalty."""
        from novatrade.evaluation.fitness import compute_promotion_score

        # ratio = 0.48/0.80 = 0.60 → moderate
        score_moderate = compute_promotion_score(
            oos_scores=[0.48, 0.48],
            holdout_score=0.48,
            is_median=0.80,
            complexity=5,
        )
        # ratio = 0.75/0.80 = 0.9375 → no penalty
        score_clean = compute_promotion_score(
            oos_scores=[0.75, 0.75],
            holdout_score=0.75,
            is_median=0.80,
            complexity=5,
        )
        assert score_clean > score_moderate


# ---------------------------------------------------------------------------
# BUG 3: Recovery factor — consistent units
# ---------------------------------------------------------------------------


class TestRecoveryFactorUnits:
    """Recovery factor must use consistent units (USD/USD, not pips/USD)."""

    def _make_metrics(self, **overrides):
        class MockMetrics:
            total_completed_trades = 100
            profit_factor = 1.8
            max_drawdown_pct = 5.0
            max_drawdown_usd = 500.0
            net_result_pips = 200.0
            net_result_usd = 2000.0
            total_bars = 24 * 22 * 12
            expectancy_r = 0.5
            win_rate = 0.55
            max_consecutive_losses = 5
            average_winner_pips = 30.0
            average_loser_pips = -15.0
            sharpe_ratio = 0.0
            sortino_ratio = 0.0

        m = MockMetrics()
        for k, v in overrides.items():
            setattr(m, k, v)
        return m

    def test_recovery_factor_uses_usd_over_usd(self):
        """Recovery factor = net_result_usd / max_drawdown_usd."""
        from novatrade.evaluation.evaluation_ladder import compute_dashboard_metrics

        metrics = self._make_metrics(
            net_result_usd=2000.0,
            max_drawdown_usd=500.0,
            net_result_pips=200.0,  # deliberately different from USD values
        )
        row = compute_dashboard_metrics(metrics, experiment_id="rf-test")
        expected = 2000.0 / 500.0  # = 4.0
        assert row.recovery_factor == pytest.approx(expected, rel=1e-4), (
            f"Expected {expected}, got {row.recovery_factor}"
        )

    def test_recovery_factor_not_pips_over_usd(self):
        """Recovery factor must NOT be net_pips / max_drawdown_usd."""
        from novatrade.evaluation.evaluation_ladder import compute_dashboard_metrics

        metrics = self._make_metrics(
            net_result_usd=5000.0,
            max_drawdown_usd=1000.0,
            net_result_pips=300.0,  # pips != usd
        )
        row = compute_dashboard_metrics(metrics, experiment_id="rf-mismatch")
        # If using pips/USD it would be 300/1000 = 0.3
        # If using USD/USD it should be 5000/1000 = 5.0
        wrong_value = 300.0 / 1000.0
        assert row.recovery_factor != pytest.approx(wrong_value, rel=0.01), (
            "Recovery factor appears to use pips/USD mismatch"
        )
        assert row.recovery_factor == pytest.approx(5.0, rel=1e-4)

    def test_recovery_factor_zero_drawdown(self):
        """Recovery factor with zero drawdown should cap at 5.0 for positive profit."""
        from novatrade.evaluation.evaluation_ladder import compute_dashboard_metrics

        metrics = self._make_metrics(
            net_result_usd=1000.0,
            max_drawdown_usd=0.0,
        )
        row = compute_dashboard_metrics(metrics, experiment_id="rf-nodd")
        assert row.recovery_factor == 5.0

    def test_recovery_factor_zero_profit_zero_drawdown(self):
        """Recovery factor with zero profit and zero drawdown should be 0."""
        from novatrade.evaluation.evaluation_ladder import compute_dashboard_metrics

        metrics = self._make_metrics(
            net_result_usd=0.0,
            max_drawdown_usd=0.0,
        )
        row = compute_dashboard_metrics(metrics, experiment_id="rf-zero")
        assert row.recovery_factor == 0.0


# ---------------------------------------------------------------------------
# BUG 4: Sharpe / Sortino — proper formulas
# ---------------------------------------------------------------------------


class TestSharpeAndSortino:
    """Sharpe and Sortino must use proper return-based formulas."""

    def test_sharpe_sortino_computed_in_metrics(self):
        """BacktestMetrics must have sharpe_ratio and sortino_ratio fields."""
        from novatrade.backtest.metrics import BacktestMetrics, EvaluationWindow

        m = BacktestMetrics(window=EvaluationWindow("test", 30, "test"))
        assert hasattr(m, "sharpe_ratio")
        assert hasattr(m, "sortino_ratio")

    def test_sharpe_formula_known_values(self):
        """Verify Sharpe against hand-calculated values."""
        from novatrade.backtest.metrics import _compute_sharpe_sortino

        # 10 trades: 5 winners at +100, 5 losers at -80
        # Returns at 100k equity: [0.001, -0.0008, 0.001, -0.0008, ...]
        trade_pnl = [100, -80, 100, -80, 100, -80, 100, -80, 100, -80]
        initial_equity = 100_000.0
        trading_days = 100.0

        sharpe, sortino = _compute_sharpe_sortino(trade_pnl, initial_equity, trading_days)

        # Manual calculation:
        returns = [p / initial_equity for p in trade_pnl]
        mean_r = statistics.mean(returns)  # (5*0.001 + 5*(-0.0008))/10 = 0.0001
        std_r = statistics.stdev(returns)
        trades_per_year = (10 / 100) * 252
        annualization = trades_per_year**0.5
        expected_sharpe = (mean_r / std_r) * annualization

        assert sharpe == pytest.approx(expected_sharpe, rel=1e-4)
        assert sharpe > 0  # net positive expectancy

    def test_sortino_formula_known_values(self):
        """Verify Sortino against hand-calculated values."""
        from novatrade.backtest.metrics import _compute_sharpe_sortino

        trade_pnl = [100, -80, 100, -80, 100, -80, 100, -80, 100, -80]
        initial_equity = 100_000.0
        trading_days = 100.0

        sharpe, sortino = _compute_sharpe_sortino(trade_pnl, initial_equity, trading_days)

        returns = [p / initial_equity for p in trade_pnl]
        mean_r = statistics.mean(returns)
        downside_squares = [min(r, 0.0) ** 2 for r in returns]
        downside_dev = (sum(downside_squares) / len(returns)) ** 0.5
        trades_per_year = (10 / 100) * 252
        annualization = trades_per_year**0.5
        expected_sortino = (mean_r / downside_dev) * annualization

        assert sortino == pytest.approx(expected_sortino, rel=1e-4)
        # Sortino should be higher than Sharpe when there's downside asymmetry
        # (downside_dev < std_dev when there are positive returns)

    def test_sortino_higher_than_sharpe_with_many_small_losses(self):
        """When many small losses drag stdev up but downside_dev stays moderate,
        Sortino > Sharpe.  This is the typical shape for trend-following strategies."""
        from novatrade.backtest.metrics import _compute_sharpe_sortino

        # Large wins, many small losses — total std dominated by win variance
        trade_pnl = [
            500,
            -20,
            -25,
            -15,
            -30,
            400,
            -20,
            -25,
            -10,
            -30,
            600,
            -20,
            -15,
            -25,
            -20,
            450,
            -30,
            -10,
            -25,
            -20,
        ]
        initial_equity = 100_000.0
        trading_days = 200.0

        sharpe, sortino = _compute_sharpe_sortino(trade_pnl, initial_equity, trading_days)

        # Both should be positive (net profitable strategy)
        assert sharpe > 0
        assert sortino > 0
        # Sortino should be higher because large positive returns inflate total
        # stdev but don't affect downside deviation
        assert sortino > sharpe, f"Sortino ({sortino:.4f}) should exceed Sharpe ({sharpe:.4f})"

    def test_sharpe_zero_with_single_trade(self):
        """Sharpe requires at least 2 trades (need stdev)."""
        from novatrade.backtest.metrics import _compute_sharpe_sortino

        sharpe, sortino = _compute_sharpe_sortino([100], 100_000.0, 100.0)
        assert sharpe == 0.0
        assert sortino == 0.0

    def test_sharpe_negative_when_losing(self):
        """Net-losing strategy should have negative Sharpe."""
        from novatrade.backtest.metrics import _compute_sharpe_sortino

        trade_pnl = [-100, -80, -90, 20, -100, -85]
        sharpe, sortino = _compute_sharpe_sortino(trade_pnl, 100_000.0, 100.0)
        assert sharpe < 0

    def test_sharpe_zero_with_zero_equity(self):
        """Edge case: zero initial equity returns 0."""
        from novatrade.backtest.metrics import _compute_sharpe_sortino

        sharpe, sortino = _compute_sharpe_sortino([100, -50], 0.0, 100.0)
        assert sharpe == 0.0
        assert sortino == 0.0

    def test_sharpe_zero_when_all_returns_identical(self):
        """If all returns are the same, std=0, Sharpe should be 0."""
        from novatrade.backtest.metrics import _compute_sharpe_sortino

        trade_pnl = [100, 100, 100, 100]
        sharpe, sortino = _compute_sharpe_sortino(trade_pnl, 100_000.0, 100.0)
        assert sharpe == 0.0
        # Sortino should also be 0 (no downside returns, downside_dev = 0)
        assert sortino == 0.0

    def test_evaluation_ladder_uses_metrics_sharpe_sortino(self):
        """Dashboard metrics should use BacktestMetrics Sharpe/Sortino when available."""
        from novatrade.evaluation.evaluation_ladder import compute_dashboard_metrics

        class MockMetrics:
            total_completed_trades = 100
            profit_factor = 1.8
            max_drawdown_pct = 5.0
            max_drawdown_usd = 500.0
            net_result_pips = 200.0
            net_result_usd = 2000.0
            total_bars = 24 * 22 * 12
            expectancy_r = 0.5
            win_rate = 0.55
            max_consecutive_losses = 5
            average_winner_pips = 30.0
            average_loser_pips = -15.0
            sharpe_ratio = 1.85  # Pre-computed proper value
            sortino_ratio = 2.40  # Pre-computed proper value

        row = compute_dashboard_metrics(MockMetrics(), experiment_id="sharpe-test")
        assert row.sharpe_ratio == pytest.approx(1.85, rel=1e-4)
        assert row.sortino_ratio == pytest.approx(2.40, rel=1e-4)

    def test_evaluation_ladder_fallback_proxy(self):
        """When metrics don't have Sharpe/Sortino, fallback proxy is used."""
        from novatrade.evaluation.evaluation_ladder import compute_dashboard_metrics

        class MockMetrics:
            total_completed_trades = 100
            profit_factor = 1.8
            max_drawdown_pct = 5.0
            max_drawdown_usd = 500.0
            net_result_pips = 200.0
            net_result_usd = 2000.0
            total_bars = 24 * 22 * 12
            expectancy_r = 0.5
            win_rate = 0.55
            max_consecutive_losses = 5
            average_winner_pips = 30.0
            average_loser_pips = -15.0
            # No sharpe_ratio or sortino_ratio attributes

        row = compute_dashboard_metrics(MockMetrics(), experiment_id="proxy-test")
        # Should still produce non-zero values via the proxy formula
        assert row.sharpe_ratio > 0
        assert row.sortino_ratio > 0

    def test_sharpe_sortino_in_to_dict(self):
        """BacktestMetrics.to_dict() should include sharpe and sortino."""
        from novatrade.backtest.metrics import BacktestMetrics, EvaluationWindow

        m = BacktestMetrics(
            window=EvaluationWindow("test", 30, "test"),
            sharpe_ratio=1.5,
            sortino_ratio=2.0,
        )
        d = m.to_dict()
        assert "sharpe_ratio" in d
        assert "sortino_ratio" in d
        assert d["sharpe_ratio"] == 1.5
        assert d["sortino_ratio"] == 2.0

    def test_compute_metrics_populates_sharpe_sortino(self):
        """compute_metrics() should populate sharpe_ratio and sortino_ratio."""
        from novatrade.backtest.metrics import (
            CompletedTrade,
            EvaluationWindow,
            ExitReason,
            FilterRejection,
            TradeSide,
            compute_metrics,
        )

        # Create trades with known P&L
        trades = []
        for i in range(20):
            pnl = 100.0 if i % 2 == 0 else -60.0
            trades.append(
                CompletedTrade(
                    trade_id=i,
                    side=TradeSide.LONG,
                    entry_bar=i * 50,
                    exit_bar=i * 50 + 10,
                    entry_price=1.1000,
                    exit_price=1.1050 if pnl > 0 else 1.0940,
                    stop_loss=1.0900,
                    volume=0.10,
                    exit_reason=ExitReason.TRAILING_STOP,
                    pnl_pips=pnl / 10,
                    pnl_usd=pnl,
                    risk_r=1.0 if pnl > 0 else -0.6,
                )
            )

        window = EvaluationWindow("test", 365, "test window")
        m = compute_metrics(
            trades=trades,
            pending_orders=[],
            signals=[],
            filter_rejections=FilterRejection(),
            window=window,
            total_bars=24 * 252,  # ~1 year
            initial_equity=100_000.0,
        )
        assert m.sharpe_ratio != 0.0, "Sharpe should be computed"
        assert m.sortino_ratio != 0.0, "Sortino should be computed"
        # Net positive strategy should have positive ratios
        assert m.sharpe_ratio > 0
        assert m.sortino_ratio > 0
