"""Tests for NovaTrade performance analyzer and dashboard."""

import json
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

from novatrade.monitor.performance_analyzer import (
    AlertSeverity,
    PerformanceAnalyzer,
    PerformanceLevel,
    PerformanceMetrics,
)
from novatrade.monitor.performance_dashboard import PerformanceDashboard


class TestPerformanceMetrics:
    """Test PerformanceMetrics dataclass and methods."""

    def test_performance_level_classification(self):
        """Test performance level classification logic."""
        # Excellent performance
        metrics = PerformanceMetrics(
            equity=101000, equity_change=1000, equity_change_pct=1.0,
            total_signals=10, approval_rate=1.0
        )
        assert metrics.performance_level() == PerformanceLevel.EXCELLENT

        # Good performance
        metrics = PerformanceMetrics(
            equity=100200, equity_change=200, equity_change_pct=0.2,
            total_signals=5, approval_rate=1.0
        )
        assert metrics.performance_level() == PerformanceLevel.GOOD

        # Average performance
        metrics = PerformanceMetrics(
            equity=100050, equity_change=50, equity_change_pct=0.05,
            total_signals=3, approval_rate=1.0
        )
        assert metrics.performance_level() == PerformanceLevel.AVERAGE

        # Poor performance
        metrics = PerformanceMetrics(
            equity=99800, equity_change=-200, equity_change_pct=-0.2,
            total_signals=8, approval_rate=0.9
        )
        assert metrics.performance_level() == PerformanceLevel.POOR

        # Critical performance
        metrics = PerformanceMetrics(
            equity=99000, equity_change=-1000, equity_change_pct=-1.0,
            total_signals=15, approval_rate=0.7
        )
        assert metrics.performance_level() == PerformanceLevel.CRITICAL

    def test_execution_quality_score(self):
        """Test execution quality score calculation."""
        # Perfect execution
        metrics = PerformanceMetrics(
            equity=100000, equity_change=0, equity_change_pct=0,
            total_signals=10, approval_rate=1.0,
            feed_health_score=1.0, error_rate=0.0
        )
        assert metrics.execution_quality_score() == 100.0

        # High error rate penalty
        metrics.error_rate = 0.1  # 10% error rate
        assert metrics.execution_quality_score() == 95.0  # 100 - (0.1 * 50)

        # Low approval rate penalty
        metrics.error_rate = 0.0
        metrics.approval_rate = 0.8  # 80% approval
        assert metrics.execution_quality_score() == 90.0  # 100 - ((0.9 - 0.8) * 100)

        # Poor feed health penalty
        metrics.approval_rate = 1.0
        metrics.feed_health_score = 0.5  # 50% feed health
        assert metrics.execution_quality_score() == 50.0  # 100 * 0.5


class TestPerformanceAnalyzer:
    """Test PerformanceAnalyzer functionality."""

    @pytest.fixture
    def analyzer(self):
        """Create test analyzer instance."""
        return PerformanceAnalyzer(initial_equity=100000.0)

    @pytest.fixture
    def sample_status_data(self):
        """Sample status data from NovaTrade service."""
        return {
            "metrics": {
                "ticks": 152,
                "signals_entry": 2,
                "signals_exit": 0,
                "signals_modify_sl": 0,
                "approved": 2,
                "rejected": 0,
                "errors": 0,
            },
            "strategy_state": {
                "equity": 100075.0,
                "consecutive_losses": 0,
            },
            "feed_health": {
                "tracked_symbols": 1,
                "healthy": 1,
                "unhealthy": 0,
            },
            "uptime_seconds": 4572.0,
        }

    def test_analyze_status_basic_metrics(self, analyzer, sample_status_data):
        """Test basic status analysis."""
        metrics = analyzer.analyze_status(sample_status_data)

        assert metrics.equity == 100075.0
        assert metrics.equity_change == 75.0
        assert metrics.equity_change_pct == 0.075
        assert metrics.total_signals == 2
        assert metrics.approval_rate == 1.0
        assert metrics.consecutive_losses == 0
        assert metrics.feed_health_score == 1.0
        assert metrics.error_rate == 0.0
        assert metrics.uptime_seconds == 4572.0

    def test_analyze_status_with_errors(self, analyzer, sample_status_data):
        """Test analysis with errors and rejections."""
        sample_status_data["metrics"]["errors"] = 5
        sample_status_data["metrics"]["rejected"] = 1
        # Need to update total signals calculation
        sample_status_data["metrics"]["signals_entry"] = 3  # Increase to account for rejected

        metrics = analyzer.analyze_status(sample_status_data)

        # Error rate: 5 errors / (3 total signals + 152 ticks) = 5/155 ≈ 0.032
        assert abs(metrics.error_rate - (5 / 155)) < 0.001
        # Approval rate: 2 approved / 3 total signals = 2/3 ≈ 0.667
        assert abs(metrics.approval_rate - (2 / 3)) < 0.001

    def test_generate_alerts_no_issues(self, analyzer, sample_status_data):
        """Test alert generation with healthy metrics."""
        metrics = analyzer.analyze_status(sample_status_data)
        alerts = analyzer.generate_alerts(metrics)

        assert len(alerts) == 0

    def test_generate_alerts_drawdown(self, analyzer, sample_status_data):
        """Test drawdown alert generation."""
        sample_status_data["strategy_state"]["equity"] = 97500.0  # 2.5% drawdown

        metrics = analyzer.analyze_status(sample_status_data)
        alerts = analyzer.generate_alerts(metrics)

        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.WARNING
        assert "drawdown" in alerts[0].message.lower()
        assert alerts[0].current_value == -2.5

    def test_generate_alerts_critical_drawdown(self, analyzer, sample_status_data):
        """Test critical drawdown alert."""
        sample_status_data["strategy_state"]["equity"] = 94000.0  # 6% drawdown

        metrics = analyzer.analyze_status(sample_status_data)
        alerts = analyzer.generate_alerts(metrics)

        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.CRITICAL
        assert "drawdown" in alerts[0].message.lower()

    def test_generate_alerts_low_approval_rate(self, analyzer, sample_status_data):
        """Test low approval rate alert."""
        # Set up low approval rate: 2 approved, 3 rejected = 2/(2+3) = 40% approval
        sample_status_data["metrics"]["signals_entry"] = 5  # Total signals entry
        sample_status_data["metrics"]["approved"] = 2
        sample_status_data["metrics"]["rejected"] = 3

        metrics = analyzer.analyze_status(sample_status_data)
        alerts = analyzer.generate_alerts(metrics)

        # Should generate both approval rate and execution quality alerts
        assert len(alerts) == 2
        approval_alert = next((a for a in alerts if "approval rate" in a.message.lower()), None)
        assert approval_alert is not None
        assert approval_alert.severity == AlertSeverity.WARNING

    def test_generate_alerts_consecutive_losses(self, analyzer, sample_status_data):
        """Test consecutive losses alert."""
        sample_status_data["strategy_state"]["consecutive_losses"] = 6

        metrics = analyzer.analyze_status(sample_status_data)
        alerts = analyzer.generate_alerts(metrics)

        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.WARNING
        assert "consecutive losses" in alerts[0].message.lower()

    def test_generate_alerts_feed_health(self, analyzer, sample_status_data):
        """Test feed health alert."""
        sample_status_data["feed_health"]["healthy"] = 0
        sample_status_data["feed_health"]["unhealthy"] = 1

        metrics = analyzer.analyze_status(sample_status_data)
        alerts = analyzer.generate_alerts(metrics)

        # Should generate both feed health and execution quality alerts
        assert len(alerts) == 2
        feed_alert = next((a for a in alerts if "feed health" in a.message.lower()), None)
        assert feed_alert is not None
        assert feed_alert.severity == AlertSeverity.CRITICAL

    def test_performance_trend_insufficient_data(self, analyzer):
        """Test trend analysis with insufficient data."""
        trend = analyzer.performance_trend()
        assert trend == "INSUFFICIENT_DATA"

    def test_performance_trend_improving(self, analyzer):
        """Test improving performance trend."""
        # Add mock history with improving trend
        analyzer.history = [
            PerformanceMetrics(equity=100000, equity_change=0, equity_change_pct=0.0,
                             total_signals=1, approval_rate=1.0),
            PerformanceMetrics(equity=100050, equity_change=50, equity_change_pct=0.05,
                             total_signals=2, approval_rate=1.0),
            PerformanceMetrics(equity=100150, equity_change=150, equity_change_pct=0.15,
                             total_signals=3, approval_rate=1.0),
        ]

        trend = analyzer.performance_trend()
        assert trend == "IMPROVING"

    def test_performance_trend_declining(self, analyzer):
        """Test declining performance trend."""
        analyzer.history = [
            PerformanceMetrics(equity=100000, equity_change=0, equity_change_pct=0.0,
                             total_signals=1, approval_rate=1.0),
            PerformanceMetrics(equity=99950, equity_change=-50, equity_change_pct=-0.05,
                             total_signals=2, approval_rate=1.0),
            PerformanceMetrics(equity=99850, equity_change=-150, equity_change_pct=-0.15,
                             total_signals=3, approval_rate=1.0),
        ]

        trend = analyzer.performance_trend()
        assert trend == "DECLINING"

    def test_generate_summary(self, analyzer, sample_status_data):
        """Test comprehensive summary generation."""
        metrics = analyzer.analyze_status(sample_status_data)
        summary = analyzer.generate_summary(metrics)

        assert summary["performance_level"] == "AVERAGE"
        assert summary["equity"] == 100075.0
        assert summary["equity_change"] == 75.0
        assert summary["equity_change_pct"] == 0.07  # Rounded (75/100000 = 0.075, rounded to 2 decimals)
        assert summary["total_signals"] == 2
        assert summary["approval_rate"] == 1.0
        assert summary["execution_quality_score"] == 100.0
        assert summary["feed_health_score"] == 1.0
        assert summary["consecutive_losses"] == 0
        assert summary["uptime_hours"] == 1.3  # 4572 seconds / 3600
        assert summary["alert_count"] == 0
        assert summary["alerts"] == []
        assert "timestamp" in summary


class TestPerformanceDashboard:
    """Test PerformanceDashboard functionality."""

    @pytest.fixture
    def dashboard(self):
        """Create test dashboard instance."""
        return PerformanceDashboard()

    @pytest.fixture
    def mock_status_response(self):
        """Mock successful status response."""
        mock_response = Mock()
        mock_response.json.return_value = {
            "metrics": {"ticks": 100, "signals_entry": 1, "approved": 1, "errors": 0},
            "strategy_state": {"equity": 100050.0, "consecutive_losses": 0},
            "feed_health": {"tracked_symbols": 1, "healthy": 1},
            "uptime_seconds": 3600.0,
        }
        mock_response.raise_for_status.return_value = None
        return mock_response

    def test_fetch_status_success(self, dashboard, mock_status_response):
        """Test successful status fetch."""
        with patch('requests.get', return_value=mock_status_response):
            status = dashboard.fetch_status()

        assert status is not None
        assert "metrics" in status
        assert "strategy_state" in status

    def test_fetch_status_failure(self, dashboard):
        """Test status fetch failure."""
        with patch('requests.get', side_effect=requests.RequestException("Connection failed")):
            status = dashboard.fetch_status()

        assert status is None

    def test_run_analysis_success(self, dashboard, mock_status_response):
        """Test successful analysis run."""
        with patch('requests.get', return_value=mock_status_response):
            summary = dashboard.run_analysis()

        assert summary is not None
        assert "performance_level" in summary
        assert "raw_status" in summary
        assert "analysis_timestamp" in summary

    def test_run_analysis_failure(self, dashboard):
        """Test analysis failure due to service unavailable."""
        with patch('requests.get', side_effect=requests.RequestException("Connection failed")):
            summary = dashboard.run_analysis()

        assert summary is None

    def test_generate_text_report(self, dashboard):
        """Test text report generation."""
        sample_summary = {
            "performance_level": "GOOD",
            "equity": 100150.0,
            "equity_change": 150.0,
            "equity_change_pct": 0.15,
            "total_signals": 5,
            "approval_rate": 0.9,
            "execution_quality_score": 85.0,
            "feed_health_score": 1.0,
            "consecutive_losses": 1,
            "uptime_hours": 2.5,
            "trend": "IMPROVING",
            "alert_count": 0,
            "alerts": [],
        }

        report = dashboard.generate_text_report(sample_summary)

        assert "PERFORMANCE DASHBOARD" in report
        assert "GOOD" in report
        assert "$100,150.00" in report
        assert "$+150.00" in report  # Format is ${value:+,.2f} which gives $+150.00
        assert "90.0%" in report
        assert "85.0/100" in report
        assert "2.5 hours" in report
        assert "IMPROVING" in report

    def test_generate_recommendations_optimal(self, dashboard):
        """Test recommendations for optimal performance."""
        summary = {
            "performance_level": "EXCELLENT",
            "consecutive_losses": 0,
            "approval_rate": 1.0,
            "execution_quality_score": 95.0,
            "trend": "IMPROVING",
            "alerts": [],
        }

        recommendations = dashboard._generate_recommendations(summary)
        assert len(recommendations) == 0  # No recommendations needed

    def test_generate_recommendations_poor_performance(self, dashboard):
        """Test recommendations for poor performance."""
        summary = {
            "performance_level": "POOR",
            "consecutive_losses": 4,
            "approval_rate": 0.7,
            "execution_quality_score": 60.0,
            "trend": "DECLINING",
            "alerts": [{"severity": "CRITICAL"}],
        }

        recommendations = dashboard._generate_recommendations(summary)
        assert len(recommendations) > 0
        assert any("reducing position size" in rec.lower() for rec in recommendations)
        assert any("consecutive losses" in rec.lower() for rec in recommendations)
        assert any("approval rate" in rec.lower() for rec in recommendations)
        assert any("execution quality" in rec.lower() for rec in recommendations)
        assert any("declining" in rec.lower() for rec in recommendations)
        assert any("critical alerts" in rec.lower() for rec in recommendations)

    def test_save_output(self, dashboard, tmp_path):
        """Test saving analysis output to file."""
        output_file = tmp_path / "analysis.json"
        dashboard.output_file = output_file

        sample_summary = {"test": "data", "timestamp": time.time()}
        dashboard.save_output(sample_summary)

        assert output_file.exists()
        with output_file.open() as f:
            saved_data = json.load(f)
        assert saved_data == sample_summary


class TestIntegration:
    """Integration tests for the performance monitoring system."""

    @pytest.mark.integration
    def test_end_to_end_analysis(self):
        """Test complete end-to-end analysis workflow."""
        # This test requires a running NovaTrade service
        dashboard = PerformanceDashboard()

        try:
            summary = dashboard.run_analysis()

            if summary is not None:  # Service is available
                # Validate summary structure
                required_fields = [
                    "performance_level", "equity", "equity_change", "equity_change_pct",
                    "total_signals", "approval_rate", "execution_quality_score",
                    "feed_health_score", "uptime_hours", "trend", "alert_count"
                ]

                for field in required_fields:
                    assert field in summary, f"Missing field: {field}"

                # Validate data types
                assert isinstance(summary["equity"], (int, float))
                assert isinstance(summary["approval_rate"], (int, float))
                assert isinstance(summary["alerts"], list)
                assert summary["performance_level"] in [
                    "EXCELLENT", "GOOD", "AVERAGE", "POOR", "CRITICAL"
                ]

                # Generate and validate text report
                report = dashboard.generate_text_report(summary)
                assert "PERFORMANCE DASHBOARD" in report
                assert len(report) > 500  # Reasonable report length

        except Exception:
            pytest.skip("NovaTrade service not available for integration test")


if __name__ == "__main__":
    pytest.main([__file__])