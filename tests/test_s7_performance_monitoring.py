"""Tests for S7 Performance Monitoring enhancement.

Tests the enhanced v5 metrics collection, performance analysis,
and monitoring capabilities added for Sprint S7.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from novatrade.autonomy.collectors.performance import PerformanceCollector
from novatrade.monitor.performance_analyzer import AlertSeverity, PerformanceAnalyzer
from novatrade.monitor.v5_metrics_collector import V5MetricsCollector, get_metrics_collector


class TestS7PerformanceAnalyzer:
    """Test enhanced performance analyzer with v5 metrics."""

    @pytest.fixture
    def analyzer(self):
        return PerformanceAnalyzer(initial_equity=100_000.0)

    @pytest.fixture
    def sample_status_data(self):
        return {
            "metrics": {
                "signals_entry": 10,
                "signals_exit": 8,
                "signals_modify_sl": 5,
                "approved": 20,
                "ticks": 1000,
                "errors": 2,
                "total_trades": 15,
                "partial_exits": 8,
                "avg_partial_profit": 25.50,
                "partial_to_breakeven": 6,
                "m5_signals_1h": 4,
                "h1_signals_1h": 1,
                "mtf_aligned_signals": 18,
                "mtf_checked_signals": 23,
                "avg_trade_duration_hours": 12.5,
                "target_volume_sum": 10.0,
                "actual_volume_sum": 9.5,
                "sl_modifications": 3,
                "trail_triggers": 12,
            },
            "strategy_state": {
                "equity": 101_500.0,
                "consecutive_losses": 2,
            },
            "feed_health": {
                "healthy": 5,
                "tracked_symbols": 5,
            },
            "config": {
                "timeframe": "M5",
            },
            "uptime_seconds": 86400,  # 24 hours
        }

    def test_enhanced_metrics_collection(self, analyzer, sample_status_data):
        """Test that enhanced v5 metrics are collected correctly."""
        metrics = analyzer.analyze_status(sample_status_data)

        # Core metrics
        assert metrics.equity == 101_500.0
        assert metrics.equity_change == 1_500.0
        assert metrics.equity_change_pct == 1.5
        assert metrics.total_signals == 23
        assert metrics.approval_rate == pytest.approx(20 / 23, abs=0.01)

        # S7: Partial exit metrics
        assert metrics.partial_exits_count == 8
        assert metrics.partial_exit_rate == pytest.approx(53.33, abs=0.01)  # 8/15 * 100
        assert metrics.avg_partial_profit == 25.50
        assert metrics.partial_to_breakeven_rate == 75.0  # 6/8 * 100

        # S7: Timeframe metrics
        assert metrics.timeframe == "M5"
        assert metrics.m5_signal_rate == 4.0
        assert metrics.h1_signal_rate == 1.0
        assert metrics.mtf_alignment_rate == pytest.approx(78.26, abs=0.01)  # 18/23 * 100

        # S7: Enhanced strategy metrics
        assert metrics.trades_per_day == pytest.approx(15.0, abs=0.1)  # 15 trades in 24h
        assert metrics.avg_trade_duration_hours == 12.5
        assert metrics.volume_efficiency == 95.0  # 9.5/10.0 * 100
        assert metrics.sl_modification_count == 3
        assert metrics.trail_trigger_rate == 80.0  # 12/15 * 100

    def test_enhanced_alerts_generation(self, analyzer, sample_status_data):
        """Test that enhanced alert conditions trigger correctly."""
        # Modify data to trigger alerts
        sample_status_data["metrics"]["partial_exits"] = 2  # Low partial exit rate
        sample_status_data["metrics"]["total_trades"] = 50  # High trade frequency
        sample_status_data["metrics"]["avg_trade_duration_hours"] = 60.0  # Long duration
        sample_status_data["metrics"]["actual_volume_sum"] = 7.0  # Poor volume efficiency
        sample_status_data["metrics"]["sl_modifications"] = 50  # Too many SL mods

        metrics = analyzer.analyze_status(sample_status_data)
        alerts = analyzer.generate_alerts(metrics)

        # Check for specific S7 alerts
        alert_types = {alert.metric_name for alert in alerts}

        assert "partial_exit_rate" in alert_types
        assert "trades_per_day" in alert_types
        assert "avg_trade_duration" in alert_types
        assert "volume_efficiency" in alert_types
        assert "sl_modifications_per_day" in alert_types

        # Verify alert severity
        for alert in alerts:
            assert alert.severity in [AlertSeverity.WARNING, AlertSeverity.CRITICAL]
            assert alert.current_value is not None
            assert alert.threshold is not None

    def test_enhanced_summary_report(self, analyzer, sample_status_data):
        """Test that enhanced summary includes v5 metrics sections."""
        metrics = analyzer.analyze_status(sample_status_data)
        summary = analyzer.generate_summary(metrics)

        # Check new S7 sections are present
        assert "partial_exit_metrics" in summary
        assert "timeframe_metrics" in summary
        assert "trading_efficiency" in summary

        # Verify section contents
        partial_metrics = summary["partial_exit_metrics"]
        assert "partial_exits_count" in partial_metrics
        assert "partial_exit_rate" in partial_metrics
        assert "avg_partial_profit" in partial_metrics
        assert "partial_to_breakeven_rate" in partial_metrics

        timeframe_metrics = summary["timeframe_metrics"]
        assert "primary_timeframe" in timeframe_metrics
        assert "m5_signal_rate" in timeframe_metrics
        assert "h1_signal_rate" in timeframe_metrics
        assert "mtf_alignment_rate" in timeframe_metrics

        efficiency_metrics = summary["trading_efficiency"]
        assert "trades_per_day" in efficiency_metrics
        assert "avg_trade_duration_hours" in efficiency_metrics
        assert "volume_efficiency" in efficiency_metrics
        assert "trail_trigger_rate" in efficiency_metrics


class TestV5MetricsCollector:
    """Test v5 metrics collector for S7 enhancements."""

    @pytest.fixture
    def temp_state_dir(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("novatrade.monitor.v5_metrics_collector.STATE_DIR", Path(tmpdir)),
        ):
            yield Path(tmpdir)

    @pytest.fixture
    def collector(self, temp_state_dir):
        return V5MetricsCollector()

    def test_partial_exit_recording(self, collector):
        """Test partial exit event recording."""
        collector.record_partial_exit("trade_001", 15.75)

        assert collector.partial_exit_stats.partial_exits_count == 1
        assert collector.partial_exit_stats.total_partial_profit == 15.75
        assert collector.partial_exit_stats.partial_avg_profit == 15.75

        # Record another partial exit
        collector.record_partial_exit("trade_002", 22.50)

        assert collector.partial_exit_stats.partial_exits_count == 2
        assert collector.partial_exit_stats.total_partial_profit == 38.25
        assert collector.partial_exit_stats.partial_avg_profit == pytest.approx(19.125, abs=0.01)

    def test_trade_completion_recording(self, collector):
        """Test trade completion with partial exit status."""
        # Complete trade with partial exit
        collector.record_trade_completion(
            "trade_001",
            had_partial_exit=True,
            total_profit=45.00,
            duration_hours=8.5,
            reached_breakeven_after_partial=True,
        )

        assert collector.partial_exit_stats.total_trades == 1
        assert collector.partial_exit_stats.partial_trades == 1
        assert collector.partial_exit_stats.partial_to_breakeven == 1

        assert collector.trade_efficiency_stats.total_trades == 1
        assert collector.trade_efficiency_stats.total_duration_hours == 8.5
        assert collector.trade_efficiency_stats.avg_duration_hours == 8.5
        assert collector.trade_efficiency_stats.trades_2h_24h == 1

        # Complete full trade (no partial exit)
        collector.record_trade_completion(
            "trade_002",
            had_partial_exit=False,
            total_profit=30.00,
            duration_hours=25.0,
            reached_breakeven_after_partial=False,
        )

        assert collector.partial_exit_stats.total_trades == 2
        assert collector.partial_exit_stats.partial_trades == 1
        assert collector.partial_exit_stats.full_avg_profit == 30.00

        assert collector.trade_efficiency_stats.trades_over_24h == 1

    def test_signal_generation_recording(self, collector):
        """Test timeframe signal generation tracking."""
        # Record M5 signals
        collector.record_signal_generation("M5", "entry", mtf_aligned=True)
        collector.record_signal_generation("M5", "exit")

        assert collector.timeframe_stats.m5_signals_1h == 2
        assert collector.timeframe_stats.m5_signals_24h == 2
        assert collector.timeframe_stats.mtf_checked_signals == 1
        assert collector.timeframe_stats.mtf_aligned_signals == 1

        # Record H1 signals
        collector.record_signal_generation("H1", "entry", mtf_aligned=False)

        assert collector.timeframe_stats.h1_signals_1h == 1
        assert collector.timeframe_stats.mtf_checked_signals == 2
        assert collector.timeframe_stats.mtf_aligned_signals == 1
        assert collector.timeframe_stats.signal_ratio_m5_h1 == 2.0

    def test_volume_execution_recording(self, collector):
        """Test volume execution accuracy tracking."""
        # Record accurate execution
        collector.record_volume_execution(1.0, 0.98)  # 2% deviation

        assert collector.volume_stats.total_orders == 1
        assert collector.volume_stats.target_volume_sum == 1.0
        assert collector.volume_stats.actual_volume_sum == 0.98
        assert collector.volume_stats.volume_accuracy_pct == 98.0
        assert collector.volume_stats.orders_within_5pct == 1

        # Record inaccurate execution
        collector.record_volume_execution(1.0, 0.75)  # 25% deviation

        assert collector.volume_stats.total_orders == 2
        assert collector.volume_stats.orders_over_20pct_deviation == 1

    def test_persistence_and_loading(self, collector, temp_state_dir):
        """Test metrics persistence and loading from STATE files."""
        # Record some data
        collector.record_partial_exit("trade_001", 20.0)
        collector.record_trade_completion("trade_001", True, 40.0, 10.0)

        # Verify files were created
        assert (temp_state_dir / "partial_exit_stats.json").exists()
        assert (temp_state_dir / "trade_stats.json").exists()

        # Create new collector (should load existing data)
        new_collector = V5MetricsCollector()

        assert new_collector.partial_exit_stats.partial_exits_count == 1
        assert new_collector.partial_exit_stats.total_trades == 1
        assert new_collector.trade_efficiency_stats.avg_duration_hours == 10.0

    def test_summary_stats(self, collector):
        """Test summary statistics generation."""
        collector.record_partial_exit("trade_001", 25.0)
        collector.record_trade_completion("trade_001", True, 50.0, 12.0)
        collector.record_signal_generation("M5", "entry", True)
        collector.record_volume_execution(1.0, 0.95)

        summary = collector.get_summary_stats()

        assert "partial_exits" in summary
        assert "timeframe_signals" in summary
        assert "trade_efficiency" in summary
        assert "volume_execution" in summary
        assert "summary_timestamp" in summary

    def test_global_collector_instance(self):
        """Test global collector instance management."""
        collector1 = get_metrics_collector()
        collector2 = get_metrics_collector()

        # Should return same instance
        assert collector1 is collector2


class TestS7PerformanceCollectorEnhancement:
    """Test enhanced performance collector with S7 metrics."""

    @pytest.fixture
    def temp_base_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def collector(self, temp_base_path):
        return PerformanceCollector(base_path=temp_base_path)

    def test_enhanced_metrics_collection(self, collector, temp_base_path):
        """Test that new S7 metrics are collected."""
        # Create mock data files
        state_dir = Path(temp_base_path) / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)

        # Create partial exit stats
        (state_dir / "partial_exit_stats.json").write_text(
            json.dumps(
                {
                    "total_trades": 20,
                    "partial_trades": 12,
                    "partial_avg_profit": 18.5,
                    "full_avg_profit": 35.0,
                }
            )
        )

        # Create signal stats
        (state_dir / "signal_stats.json").write_text(
            json.dumps(
                {
                    "m5_signals_24h": 24,
                    "h1_signals_24h": 6,
                }
            )
        )

        # Create trade stats
        (state_dir / "trade_stats.json").write_text(
            json.dumps(
                {
                    "avg_duration_hours": 15.5,
                }
            )
        )

        # Create volume stats
        (state_dir / "volume_stats.json").write_text(
            json.dumps(
                {
                    "target_volume_sum": 50.0,
                    "actual_volume_sum": 47.5,
                }
            )
        )

        # Create equity data
        equity_data = [
            {"equity": 100000, "timestamp": "2024-01-01T00:00:00Z"},
            {"equity": 101000, "timestamp": "2024-01-01T12:00:00Z"},
            {"equity": 102000, "timestamp": "2024-01-02T00:00:00Z"},
        ]
        (state_dir / "equity_history.json").write_text(json.dumps(equity_data))

        # Test enhanced metric computation
        score, raw = collector._compute_partial_exit_efficiency(equity_data)
        assert score > 0  # Should compute successfully
        assert raw != collector._NO_DATA_RAW

        score, raw = collector._compute_timeframe_consistency(equity_data)
        assert score > 0  # Should compute successfully

        score, raw = collector._compute_trade_duration_efficiency(equity_data)
        assert score > 0  # Should compute successfully

        score, raw = collector._compute_volume_execution_accuracy(equity_data)
        assert score > 0  # Should compute successfully

    def test_collect_includes_s7_metrics(self, collector):
        """Test that collect() includes S7 enhanced metrics."""
        # Create minimal equity data
        state_dir = Path(collector.base_path) / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)

        equity_data = [{"equity": 100000}, {"equity": 101000}]
        (state_dir / "equity_history.json").write_text(json.dumps(equity_data))

        import asyncio

        dimension_score = asyncio.run(collector.collect())

        # Should have 9 metrics now (4 original + 4 S7 enhanced + 1 insufficient_data)
        assert len(dimension_score.sub_metrics) == 9

        metric_names = {m.name for m in dimension_score.sub_metrics}
        assert "partial_exit_efficiency" in metric_names
        assert "timeframe_signal_consistency" in metric_names
        assert "trade_duration_efficiency" in metric_names
        assert "volume_execution_accuracy" in metric_names
        assert "insufficient_data" in metric_names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
