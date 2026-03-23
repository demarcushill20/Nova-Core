"""Tests for TradingView backtest validation and drift comparison."""

from __future__ import annotations

import json

from novatrade.data.tv_backtest_validator import (
    DriftSeverity,
    DriftThresholds,
    TVSnapshotMetrics,
    compare_backtests,
    extract_metrics_from_js_result,
    extract_metrics_from_snapshot,
    metrics_from_tv_cli_report,
)

# ---------------------------------------------------------------------------
# Fixtures: realistic TradingView Strategy Tester snapshot text
# ---------------------------------------------------------------------------

STRATEGY_TESTER_SNAPSHOT = """
[role="tabpanel" Strategy Tester]
  [heading] Performance Summary
  Net Profit: $1,149.00
  Net Profit  1.15%
  Total Closed Trades: 148
  Percent Profitable: 52.03%
  Profit Factor: 1.07
  Sharpe Ratio: 0.28
  Sortino Ratio: 0.41
  Max Drawdown: $892.50
  Max Drawdown  0.89%
  Avg Trade: $7.76
  Avg Winning Trade: $56.23
  Avg Losing Trade: $-45.81
  Largest Winning Trade: $234.00
  Largest Losing Trade: $-178.50
  Commission Paid: $1,036.00
  Long Trades: 81
  Short Trades: 67
  Winning Trades: 77
  Losing Trades: 71
"""

MINIMAL_SNAPSHOT = """
Net Profit: $500.00
Total Trades: 50
Win Rate: 55.0%
Profit Factor: 1.25
"""


# ---------------------------------------------------------------------------
# Snapshot extraction tests
# ---------------------------------------------------------------------------


class TestExtractMetricsFromSnapshot:
    def test_extracts_full_metrics(self):
        m = extract_metrics_from_snapshot(STRATEGY_TESTER_SNAPSHOT)
        assert m.net_profit == 1149.00
        assert m.total_trades == 148
        assert m.win_rate == 52.03
        assert m.profit_factor == 1.07
        assert m.sharpe_ratio == 0.28
        assert m.sortino_ratio == 0.41
        assert m.max_drawdown == 892.50
        assert m.avg_trade == 7.76
        assert m.avg_win == 56.23
        assert m.avg_loss == -45.81
        assert m.largest_win == 234.00
        assert m.largest_loss == -178.50
        assert m.commission_paid == 1036.00
        assert m.total_long_trades == 81
        assert m.total_short_trades == 67
        assert m.winning_trades == 77
        assert m.losing_trades == 71

    def test_extracts_minimal_metrics(self):
        m = extract_metrics_from_snapshot(MINIMAL_SNAPSHOT)
        assert m.net_profit == 500.00
        assert m.total_trades == 50
        assert m.win_rate == 55.0
        assert m.profit_factor == 1.25
        # Missing metrics should be None
        assert m.sharpe_ratio is None
        assert m.max_drawdown is None

    def test_preserves_raw_text(self):
        m = extract_metrics_from_snapshot(STRATEGY_TESTER_SNAPSHOT)
        assert "Net Profit" in m.raw_text

    def test_empty_snapshot_returns_all_none(self):
        m = extract_metrics_from_snapshot("")
        assert m.net_profit is None
        assert m.total_trades is None
        assert m.win_rate is None

    def test_handles_negative_values(self):
        text = "Net Profit: $-250.00\nAvg Trade: $-5.50"
        m = extract_metrics_from_snapshot(text)
        assert m.net_profit == -250.00
        assert m.avg_trade == -5.50

    def test_handles_no_currency_symbol(self):
        text = "Net Profit: 1500.00\nTotal Trades: 100"
        m = extract_metrics_from_snapshot(text)
        assert m.net_profit == 1500.00
        assert m.total_trades == 100

    def test_handles_euro_symbol(self):
        text = "Net Profit: €2,500.00"
        m = extract_metrics_from_snapshot(text)
        assert m.net_profit == 2500.00

    def test_extracts_timestamp(self):
        m = extract_metrics_from_snapshot(STRATEGY_TESTER_SNAPSHOT)
        assert m.extracted_at  # ISO format string
        assert "T" in m.extracted_at


# ---------------------------------------------------------------------------
# JS result extraction tests
# ---------------------------------------------------------------------------


class TestExtractMetricsFromJsResult:
    def test_extracts_from_camelcase_keys(self):
        data = {
            "netProfit": 1149.0,
            "totalTrades": 148,
            "percentProfitable": 52.03,
            "profitFactor": 1.07,
            "sharpeRatio": 0.28,
            "maxDrawDown": 892.5,
        }
        m = extract_metrics_from_js_result(data)
        assert m.net_profit == 1149.0
        assert m.total_trades == 148
        assert m.win_rate == 52.03
        assert m.profit_factor == 1.07
        assert m.sharpe_ratio == 0.28
        assert m.max_drawdown == 892.5

    def test_handles_alternate_key_names(self):
        data = {"totalClosedTrades": 50, "winRate": 60.0, "maxDrawdown": 500.0}
        m = extract_metrics_from_js_result(data)
        assert m.total_trades == 50
        assert m.win_rate == 60.0
        assert m.max_drawdown == 500.0

    def test_empty_dict_returns_all_none(self):
        m = extract_metrics_from_js_result({})
        assert m.net_profit is None
        assert m.total_trades is None

    def test_ignores_invalid_values(self):
        data = {"netProfit": "not_a_number", "totalTrades": 100}
        m = extract_metrics_from_js_result(data)
        assert m.net_profit is None  # invalid value ignored
        assert m.total_trades == 100


# ---------------------------------------------------------------------------
# Drift comparison tests
# ---------------------------------------------------------------------------


class TestCompareBacktests:
    def _make_metrics(self, **kwargs) -> TVSnapshotMetrics:
        return TVSnapshotMetrics(**kwargs)

    def test_identical_metrics_pass(self):
        m = self._make_metrics(
            net_profit=1000.0,
            total_trades=100,
            win_rate=55.0,
            profit_factor=1.2,
            max_drawdown=500.0,
            sharpe_ratio=0.5,
        )
        report = compare_backtests(m, m)
        assert report.overall == DriftSeverity.OK
        assert report.passed is True
        assert len(report.failed_metrics) == 0

    def test_small_drift_passes(self):
        source = self._make_metrics(
            net_profit=1010.0,
            total_trades=101,
            win_rate=55.5,
            profit_factor=1.21,
            max_drawdown=505.0,
            sharpe_ratio=0.52,
        )
        ref = self._make_metrics(
            net_profit=1000.0,
            total_trades=100,
            win_rate=55.0,
            profit_factor=1.20,
            max_drawdown=500.0,
            sharpe_ratio=0.50,
        )
        report = compare_backtests(source, ref)
        assert report.overall == DriftSeverity.OK

    def test_large_trade_count_drift_fails(self):
        source = self._make_metrics(total_trades=120)
        ref = self._make_metrics(total_trades=100)
        report = compare_backtests(source, ref, DriftThresholds(trade_count_pct=5.0))
        # 20% drift > 5% threshold
        tc_drift = [d for d in report.drifts if d.metric == "total_trades"][0]
        assert tc_drift.severity == DriftSeverity.FAIL

    def test_large_profit_drift_fails(self):
        source = self._make_metrics(net_profit=1500.0)
        ref = self._make_metrics(net_profit=1000.0)
        report = compare_backtests(source, ref, DriftThresholds(net_profit_pct=10.0))
        np_drift = [d for d in report.drifts if d.metric == "net_profit"][0]
        assert np_drift.severity == DriftSeverity.FAIL

    def test_win_rate_uses_absolute_diff(self):
        source = self._make_metrics(win_rate=55.0)
        ref = self._make_metrics(win_rate=52.0)
        report = compare_backtests(source, ref, DriftThresholds(win_rate_abs=2.0))
        wr_drift = [d for d in report.drifts if d.metric == "win_rate"][0]
        # 3pp > 2pp threshold
        assert wr_drift.severity == DriftSeverity.FAIL
        assert wr_drift.absolute_diff == 3.0

    def test_sharpe_uses_absolute_diff(self):
        source = self._make_metrics(sharpe_ratio=0.5)
        ref = self._make_metrics(sharpe_ratio=0.1)
        report = compare_backtests(source, ref, DriftThresholds(sharpe_ratio_abs=0.3))
        sr_drift = [d for d in report.drifts if d.metric == "sharpe_ratio"][0]
        assert sr_drift.severity == DriftSeverity.FAIL

    def test_warn_threshold(self):
        # warn_factor=0.5, so warn at 50% of fail threshold
        source = self._make_metrics(total_trades=96)
        ref = self._make_metrics(total_trades=100)
        # 4% drift, threshold=5%, warn at 2.5%
        report = compare_backtests(source, ref, DriftThresholds(trade_count_pct=5.0, warn_factor=0.5))
        tc_drift = [d for d in report.drifts if d.metric == "total_trades"][0]
        assert tc_drift.severity == DriftSeverity.WARN

    def test_overall_is_worst_severity(self):
        source = self._make_metrics(
            net_profit=1000.0,  # matches
            total_trades=200,  # 100% drift
            win_rate=55.0,
            profit_factor=1.2,
            max_drawdown=500.0,
            sharpe_ratio=0.5,
        )
        ref = self._make_metrics(
            net_profit=1000.0,
            total_trades=100,
            win_rate=55.0,
            profit_factor=1.2,
            max_drawdown=500.0,
            sharpe_ratio=0.5,
        )
        report = compare_backtests(source, ref)
        assert report.overall == DriftSeverity.FAIL
        assert len(report.failed_metrics) >= 1

    def test_missing_source_value_noted(self):
        source = self._make_metrics()  # all None
        ref = self._make_metrics(total_trades=100, net_profit=1000.0)
        report = compare_backtests(source, ref)
        assert any("missing" in n for n in report.notes)

    def test_both_none_is_ok(self):
        source = self._make_metrics()
        ref = self._make_metrics()
        report = compare_backtests(source, ref)
        assert report.overall == DriftSeverity.OK

    def test_zero_reference_handles_division(self):
        source = self._make_metrics(net_profit=100.0)
        ref = self._make_metrics(net_profit=0.0)
        report = compare_backtests(source, ref)
        np_drift = [d for d in report.drifts if d.metric == "net_profit"][0]
        assert np_drift.relative_diff_pct == 100.0
        assert np_drift.severity == DriftSeverity.FAIL

    def test_custom_labels(self):
        source = self._make_metrics(total_trades=100)
        ref = self._make_metrics(total_trades=100)
        report = compare_backtests(
            source,
            ref,
            source_label="browser_v4",
            reference_label="python_backtest",
        )
        assert report.source_label == "browser_v4"
        assert report.reference_label == "python_backtest"


# ---------------------------------------------------------------------------
# ValidationReport tests
# ---------------------------------------------------------------------------


class TestValidationReport:
    def test_to_dict(self):
        source = TVSnapshotMetrics(net_profit=1000.0, total_trades=100)
        ref = TVSnapshotMetrics(net_profit=1000.0, total_trades=100)
        report = compare_backtests(source, ref)
        d = report.to_dict()
        assert d["overall"] == "ok"
        assert d["passed"] is True
        assert isinstance(d["drifts"], list)
        assert "timestamp" in d

    def test_save_and_load(self, tmp_path):
        source = TVSnapshotMetrics(net_profit=1000.0, total_trades=100)
        ref = TVSnapshotMetrics(net_profit=1000.0, total_trades=100)
        report = compare_backtests(source, ref)
        path = report.save(tmp_path / "report.json")
        loaded = json.loads(path.read_text())
        assert loaded["passed"] is True
        assert loaded["overall"] == "ok"

    def test_failed_and_warned_properties(self):
        source = TVSnapshotMetrics(
            net_profit=2000.0,  # big drift
            total_trades=100,
            win_rate=55.0,
            profit_factor=1.2,
            max_drawdown=500.0,
            sharpe_ratio=0.5,
        )
        ref = TVSnapshotMetrics(
            net_profit=1000.0,
            total_trades=100,
            win_rate=55.0,
            profit_factor=1.2,
            max_drawdown=500.0,
            sharpe_ratio=0.5,
        )
        report = compare_backtests(source, ref)
        assert len(report.failed_metrics) >= 1
        assert report.failed_metrics[0].metric == "net_profit"


# ---------------------------------------------------------------------------
# TV-CLI bridge test
# ---------------------------------------------------------------------------


class TestMetricsFromTvCliReport:
    def test_converts_report(self):
        """Ensure TVBacktestReport → TVSnapshotMetrics bridge works."""

        class FakePerformance:
            net_profit = 1149.0
            net_profit_pct = 1.15
            total_trades = 148
            win_rate = 52.03
            profit_factor = 1.07
            sharpe_ratio = 0.28
            sortino_ratio = 0.41
            max_drawdown = 892.5
            avg_trade = 7.76
            avg_win = 56.23
            avg_loss = -45.81
            largest_win = 234.0
            largest_loss = -178.5
            commission_paid = 1036.0

        class FakeReport:
            performance = FakePerformance()

        m = metrics_from_tv_cli_report(FakeReport())
        assert m.net_profit == 1149.0
        assert m.total_trades == 148
        assert m.profit_factor == 1.07
        assert m.sharpe_ratio == 0.28
        assert m.max_drawdown == 892.5
        assert m.commission_paid == 1036.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_snapshot_with_garbled_text(self):
        text = "Net Profit: $abc\nTotal Trades: xyz"
        m = extract_metrics_from_snapshot(text)
        # Should not crash, values should be None
        assert m.net_profit is None
        assert m.total_trades is None

    def test_snapshot_with_large_numbers(self):
        text = "Net Profit: $1,234,567.89\nTotal Trades: 10000"
        m = extract_metrics_from_snapshot(text)
        assert m.net_profit == 1234567.89
        assert m.total_trades == 10000

    def test_snapshot_case_insensitive(self):
        text = "net profit: $500.00\ntotal trades: 25"
        m = extract_metrics_from_snapshot(text)
        assert m.net_profit == 500.00
        assert m.total_trades == 25

    def test_absolute_diff_is_symmetric(self):
        """Absolute diff should be the same regardless of direction."""
        a = TVSnapshotMetrics(net_profit=1100.0, total_trades=100)
        b = TVSnapshotMetrics(net_profit=1000.0, total_trades=100)
        report_ab = compare_backtests(a, b)
        report_ba = compare_backtests(b, a)
        drift_ab = [d for d in report_ab.drifts if d.metric == "net_profit"][0]
        drift_ba = [d for d in report_ba.drifts if d.metric == "net_profit"][0]
        assert drift_ab.absolute_diff == drift_ba.absolute_diff
        # Note: relative % drift is NOT symmetric (100/1000=10% vs 100/1100=9.09%)
        # so severity may differ — this is expected behavior
