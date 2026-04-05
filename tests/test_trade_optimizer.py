#!/usr/bin/env python3
"""Tests for the trade optimizer module."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from novatrade.modules.trade_optimizer import (
    LiveTradeOptimizer,
    TradeOptimizationReport,
)


def create_test_evidence_file(events: list[dict]) -> Path:
    """Create a temporary evidence file with test data."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as temp_file:
        for event in events:
            json.dump(event, temp_file)
            temp_file.write("\n")

    return Path(temp_file.name)


def create_sample_trade_events() -> list[dict]:
    """Create sample trade events for testing."""
    base_time = datetime.now(timezone.utc).timestamp() - 3600  # 1 hour ago

    events = [
        # Order placed
        {
            "event_type": "EXECUTION",
            "timestamp": base_time + 0,
            "data": {
                "trading_agent_event": "ORDER_PLACED",
                "position_id": "test_123",
                "campaign": "irb-live",
                "intent": {"side": "BUY", "broker_symbol": "EURUSD.sim"},
            },
        },
        # Order filled
        {
            "event_type": "EXECUTION",
            "timestamp": base_time + 60,
            "data": {
                "trading_agent_event": "ORDER_FILLED",
                "position_id": "test_123",
                "fill_price": 1.1550,
                "volume": 1,
                "state": "LONG",
                "campaign": "irb-live",
            },
        },
        # Multiple SL modifications (should trigger recommendation)
        {
            "event_type": "EXECUTION",
            "timestamp": base_time + 120,
            "data": {
                "trading_agent_event": "SL_MODIFIED",
                "position_id": "test_123",
                "intent": {"new_stop_loss": 1.1530, "old_stop_loss": 1.1520},
                "campaign": "irb-live",
            },
        },
        {
            "event_type": "EXECUTION",
            "timestamp": base_time + 180,
            "data": {
                "trading_agent_event": "SL_MODIFIED",
                "position_id": "test_123",
                "intent": {"new_stop_loss": 1.1535, "old_stop_loss": 1.1530},
                "campaign": "irb-live",
            },
        },
        {
            "event_type": "EXECUTION",
            "timestamp": base_time + 240,
            "data": {
                "trading_agent_event": "SL_MODIFIED",
                "position_id": "test_123",
                "intent": {"new_stop_loss": 1.1540, "old_stop_loss": 1.1535},
                "campaign": "irb-live",
            },
        },
        {
            "event_type": "EXECUTION",
            "timestamp": base_time + 300,
            "data": {
                "trading_agent_event": "SL_MODIFIED",
                "position_id": "test_123",
                "intent": {"new_stop_loss": 1.1545, "old_stop_loss": 1.1540},
                "campaign": "irb-live",
            },
        },
        {
            "event_type": "EXECUTION",
            "timestamp": base_time + 360,
            "data": {
                "trading_agent_event": "SL_MODIFIED",
                "position_id": "test_123",
                "intent": {"new_stop_loss": 1.1548, "old_stop_loss": 1.1545},
                "campaign": "irb-live",
            },
        },
        {
            "event_type": "EXECUTION",
            "timestamp": base_time + 420,
            "data": {
                "trading_agent_event": "SL_MODIFIED",
                "position_id": "test_123",
                "intent": {"new_stop_loss": 1.1550, "old_stop_loss": 1.1548},
                "campaign": "irb-live",
            },
        },
        # Rollover denial (should trigger recommendation)
        {
            "event_type": "EXECUTION",
            "timestamp": base_time + 480,
            "data": {
                "trading_agent_event": "RISK_DENIED",
                "reason": "in rollover dead zone (21:00-23:00 UTC), current=22:10 UTC",
                "campaign": "irb-live",
            },
        },
        {
            "event_type": "EXECUTION",
            "timestamp": base_time + 540,
            "data": {
                "trading_agent_event": "RISK_DENIED",
                "reason": "in rollover dead zone (21:00-23:00 UTC), current=22:15 UTC",
                "campaign": "irb-live",
            },
        },
        {
            "event_type": "EXECUTION",
            "timestamp": base_time + 600,
            "data": {
                "trading_agent_event": "RISK_DENIED",
                "reason": "in rollover dead zone (21:00-23:00 UTC), current=22:20 UTC",
                "campaign": "irb-live",
            },
        },
        # Position close
        {
            "event_type": "EXECUTION",
            "timestamp": base_time + 1200,
            "data": {
                "trading_agent_event": "BROKER_CLOSE",
                "position_id": "test_123",
                "exit_reason": "STOP_LOSS",
                "state_before": "LONG",
                "campaign": "irb-live",
            },
        },
    ]

    return events


class TestTradeOptimizer:
    """Test cases for the LiveTradeOptimizer."""

    def test_init(self):
        """Test optimizer initialization."""
        evidence_file = Path("/tmp/test_evidence.jsonl")
        optimizer = LiveTradeOptimizer(evidence_file)

        assert optimizer.evidence_file == evidence_file
        assert optimizer.trades_data == []

    def test_load_recent_trades_empty_file(self):
        """Test loading from non-existent file."""
        evidence_file = Path("/tmp/nonexistent.jsonl")
        optimizer = LiveTradeOptimizer(evidence_file)

        trades = optimizer.load_recent_trades(24)
        assert trades == []

    def test_load_recent_trades_with_data(self):
        """Test loading recent trades with sample data."""
        events = create_sample_trade_events()
        evidence_file = create_test_evidence_file(events)

        try:
            optimizer = LiveTradeOptimizer(evidence_file)
            trades = optimizer.load_recent_trades(24)

            # Should load all events since they're all recent
            assert len(trades) == len(events)

            # All should be EXECUTION events
            for trade in trades:
                assert trade["event_type"] == "EXECUTION"

        finally:
            evidence_file.unlink()  # cleanup

    def test_analyze_stop_loss_effectiveness_high_modifications(self):
        """Test SL effectiveness analysis with high modification count."""
        events = create_sample_trade_events()
        evidence_file = create_test_evidence_file(events)

        try:
            optimizer = LiveTradeOptimizer(evidence_file)
            trades = optimizer.load_recent_trades(24)

            recommendation = optimizer.analyze_stop_loss_effectiveness(trades)

            # Should generate recommendation due to 6 SL modifications
            assert recommendation is not None
            assert recommendation.category == "risk_management"
            assert recommendation.priority == "medium"
            assert "High SL modification frequency" in recommendation.description
            assert recommendation.supporting_data["avg_modifications_per_position"] == 6.0

        finally:
            evidence_file.unlink()

    def test_analyze_rollover_denials(self):
        """Test rollover denial analysis."""
        events = create_sample_trade_events()
        evidence_file = create_test_evidence_file(events)

        try:
            optimizer = LiveTradeOptimizer(evidence_file)
            trades = optimizer.load_recent_trades(24)

            recommendation = optimizer.analyze_rollover_denials(trades)

            # Should generate recommendation due to 3 rollover denials
            assert recommendation is not None
            assert recommendation.category == "entry_timing"
            assert recommendation.priority == "high"
            assert "rollover zone denials" in recommendation.description
            assert recommendation.supporting_data["rollover_denials"] == 3

        finally:
            evidence_file.unlink()

    def test_analyze_position_lifecycle(self):
        """Test position lifecycle analysis."""
        events = create_sample_trade_events()
        evidence_file = create_test_evidence_file(events)

        try:
            optimizer = LiveTradeOptimizer(evidence_file)
            trades = optimizer.load_recent_trades(24)

            pattern = optimizer.analyze_position_lifecycle(trades)

            # Should identify position duration pattern
            assert pattern is not None
            assert pattern.pattern_type == "position_duration"
            assert pattern.occurrence_count == 1
            assert pattern.avg_duration_minutes == 20.0  # 1200 seconds = 20 minutes
            assert pattern.confidence >= 0.6

        finally:
            evidence_file.unlink()

    def test_generate_optimization_report(self):
        """Test full optimization report generation."""
        events = create_sample_trade_events()
        evidence_file = create_test_evidence_file(events)

        try:
            optimizer = LiveTradeOptimizer(evidence_file)
            report = optimizer.generate_optimization_report(24)

            assert isinstance(report, TradeOptimizationReport)
            assert report.analysis_period == "Last 24 hours"
            assert report.trades_analyzed == len(events)
            assert len(report.recommendations) >= 2  # SL and rollover recommendations
            assert len(report.patterns_identified) >= 1  # Position duration pattern
            assert 0 <= report.overall_efficiency_score <= 100

            # Check specific recommendations
            rec_categories = [r.category for r in report.recommendations]
            assert "risk_management" in rec_categories
            assert "entry_timing" in rec_categories

        finally:
            evidence_file.unlink()

    def test_save_report(self):
        """Test saving report to file."""
        events = create_sample_trade_events()
        evidence_file = create_test_evidence_file(events)

        try:
            optimizer = LiveTradeOptimizer(evidence_file)
            report = optimizer.generate_optimization_report(24)

            with tempfile.TemporaryDirectory() as temp_dir:
                output_dir = Path(temp_dir)
                saved_file = optimizer.save_report(report, output_dir)

                assert saved_file.exists()
                assert saved_file.name.startswith("trade_optimization_report_")
                assert saved_file.suffix == ".json"

                # Verify file contents
                with open(saved_file) as f:
                    saved_data = json.load(f)

                assert saved_data["trades_analyzed"] == report.trades_analyzed
                assert saved_data["overall_efficiency_score"] == report.overall_efficiency_score
                assert "generated_by" in saved_data

        finally:
            evidence_file.unlink()

    def test_efficiency_score_calculation(self):
        """Test efficiency score calculation logic."""
        # Create events with some denials
        events = [
            {
                "event_type": "EXECUTION",
                "timestamp": datetime.now(timezone.utc).timestamp(),
                "data": {"trading_agent_event": "ORDER_PLACED"},
            },
            {
                "event_type": "EXECUTION",
                "timestamp": datetime.now(timezone.utc).timestamp(),
                "data": {"trading_agent_event": "RISK_DENIED"},
            },
            {
                "event_type": "EXECUTION",
                "timestamp": datetime.now(timezone.utc).timestamp(),
                "data": {"trading_agent_event": "ORDER_FILLED"},
            },
        ]

        evidence_file = create_test_evidence_file(events)

        try:
            optimizer = LiveTradeOptimizer(evidence_file)
            report = optimizer.generate_optimization_report(24)

            # 1 denial out of 3 total signals = 33.3% denial rate
            # Efficiency = 100 - 33.3 = 66.7 (rounded)
            expected_score = 100 - (1 / 3) * 100
            assert abs(report.overall_efficiency_score - expected_score) < 1.0

        finally:
            evidence_file.unlink()


@pytest.mark.integration
def test_trade_optimizer_integration():
    """Integration test with actual evidence file if it exists."""
    evidence_file = Path("/home/nova/nova-core/OUTPUT/novatrade/live_evidence.jsonl")

    if evidence_file.exists():
        optimizer = LiveTradeOptimizer(evidence_file)
        report = optimizer.generate_optimization_report(168)  # Last 7 days

        assert isinstance(report, TradeOptimizationReport)
        assert report.trades_analyzed >= 0
        assert 0 <= report.overall_efficiency_score <= 100

        # Should be able to save the report
        output_dir = Path("/tmp")
        saved_file = optimizer.save_report(report, output_dir)
        assert saved_file.exists()

        # Cleanup
        saved_file.unlink()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
