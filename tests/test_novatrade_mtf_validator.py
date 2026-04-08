"""Tests for MTF (Multi-timeframe) signal validation.

Tests for novatrade.validation.mtf_validator — comprehensive signal coherence
validation between M5 and H1 timeframes for Sprint Item S6.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from novatrade.validation.mtf_validator import (
    MTFSignal,
    MTFSignalValidator,
    MTFValidationReport,
    ValidationResult,
)


class TestMTFSignal:
    """Test MTF signal data structure."""

    def test_mtf_signal_creation(self):
        """Test MTF signal creation with all fields."""
        signal = MTFSignal(
            timestamp=1640995200.0,  # 2022-01-01 00:00:00 UTC
            symbol="EURUSD",
            timeframe="M5",
            direction="BUY",
            ema_slope=0.000123,
            adx=25.5,
            atr_ratio=1.8,
            confidence=0.85,
            irb_detected=True,
            stop_level=1.1250,
            entry_level=1.1280,
        )

        assert signal.symbol == "EURUSD"
        assert signal.timeframe == "M5"
        assert signal.direction == "BUY"
        assert signal.ema_slope == 0.000123
        assert signal.adx == 25.5
        assert signal.irb_detected is True


class TestMTFSignalValidator:
    """Test MTF signal validator functionality."""

    @pytest.fixture
    def temp_files(self):
        """Create temporary signal files for testing."""
        temp_dir = tempfile.mkdtemp()
        m5_path = Path(temp_dir) / "m5_signals.jsonl"
        h1_path = Path(temp_dir) / "h1_signals.jsonl"
        output_path = Path(temp_dir) / "mtf_validation.jsonl"

        # Create sample M5 signals
        m5_signals = [
            {
                "timestamp": 1640995200.0,
                "symbol": "EURUSD",
                "direction": "BUY",
                "ema_slope": 0.000123,
                "adx": 25.5,
                "atr_ratio": 1.8,
                "confidence": 0.85,
                "irb_detected": True,
                "stop_level": 1.1250,
                "entry_level": 1.1280,
            },
            {
                "timestamp": 1640995500.0,
                "symbol": "EURUSD",
                "direction": "SELL",
                "ema_slope": -0.000089,
                "adx": 22.1,
                "atr_ratio": 2.1,
                "confidence": 0.78,
                "irb_detected": True,
                "stop_level": 1.1320,
                "entry_level": 1.1295,
            },
        ]

        # Create sample H1 signals
        h1_signals = [
            {
                "timestamp": 1640995800.0,  # 10 minutes after first M5
                "symbol": "EURUSD",
                "direction": "BUY",
                "ema_slope": 0.000115,
                "adx": 24.8,
                "atr_ratio": 1.9,
                "confidence": 0.88,
                "irb_detected": True,
                "stop_level": 1.1248,
                "entry_level": 1.1282,
            },
        ]

        # Write signal files
        with open(m5_path, "w") as f:
            for signal in m5_signals:
                f.write(json.dumps(signal) + "\n")

        with open(h1_path, "w") as f:
            for signal in h1_signals:
                f.write(json.dumps(signal) + "\n")

        return {
            "m5_path": m5_path,
            "h1_path": h1_path,
            "output_path": output_path,
        }

    def test_validator_initialization(self, temp_files):
        """Test validator initialization with custom paths."""
        validator = MTFSignalValidator(
            m5_signals_path=temp_files["m5_path"],
            h1_signals_path=temp_files["h1_path"],
            output_path=temp_files["output_path"],
        )

        assert validator.m5_signals_path == temp_files["m5_path"]
        assert validator.h1_signals_path == temp_files["h1_path"]
        assert validator.min_adx_threshold == 20.0
        assert validator.max_timing_offset_seconds == 900.0

    def test_load_signals(self, temp_files):
        """Test loading signals from JSONL files."""
        validator = MTFSignalValidator(
            m5_signals_path=temp_files["m5_path"],
            h1_signals_path=temp_files["h1_path"],
        )

        m5_signals = validator.load_signals("M5")
        h1_signals = validator.load_signals("H1")

        assert len(m5_signals) == 2
        assert len(h1_signals) == 1

        # Check M5 signal content
        assert m5_signals[0].symbol == "EURUSD"
        assert m5_signals[0].direction == "BUY"
        assert m5_signals[0].timeframe == "M5"
        assert m5_signals[0].adx == 25.5

        # Check H1 signal content
        assert h1_signals[0].symbol == "EURUSD"
        assert h1_signals[0].direction == "BUY"
        assert h1_signals[0].timeframe == "H1"
        assert h1_signals[0].adx == 24.8

    def test_load_signals_missing_file(self):
        """Test loading signals from non-existent file."""
        validator = MTFSignalValidator(
            m5_signals_path=Path("/non/existent/path"),
            h1_signals_path=Path("/non/existent/path"),
        )

        signals = validator.load_signals("M5")
        assert signals == []

    def test_find_matching_signals(self, temp_files):
        """Test finding matching signal pairs within time window."""
        validator = MTFSignalValidator(
            m5_signals_path=temp_files["m5_path"],
            h1_signals_path=temp_files["h1_path"],
        )

        m5_signals = validator.load_signals("M5")
        h1_signals = validator.load_signals("H1")

        matches = validator.find_matching_signals(m5_signals, h1_signals, "EURUSD")

        # Should find 3 matches: 2 M5 signals (one matched, one unmatched) + 1 H1 signal
        assert len(matches) >= 2

        # Check that first M5 signal is matched with H1 signal
        m5_match, h1_match = matches[0]
        assert m5_match is not None
        assert h1_match is not None
        assert m5_match.direction == "BUY"
        assert h1_match.direction == "BUY"

    def test_validate_direction_coherence_pass(self):
        """Test direction coherence validation - passing case."""
        validator = MTFSignalValidator()

        m5_signal = MTFSignal(
            timestamp=1640995200.0,
            symbol="EURUSD",
            timeframe="M5",
            direction="BUY",
            ema_slope=0.0001,
            adx=25.0,
            atr_ratio=1.8,
            confidence=0.8,
        )
        h1_signal = MTFSignal(
            timestamp=1640995800.0,
            symbol="EURUSD",
            timeframe="H1",
            direction="BUY",
            ema_slope=0.0001,
            adx=24.0,
            atr_ratio=1.9,
            confidence=0.85,
        )

        result, agreement, issues = validator.validate_direction_coherence(m5_signal, h1_signal)

        assert result == ValidationResult.PASS
        assert agreement is True
        assert len(issues) == 0

    def test_validate_direction_coherence_fail(self):
        """Test direction coherence validation - failing case."""
        validator = MTFSignalValidator()

        m5_signal = MTFSignal(
            timestamp=1640995200.0,
            symbol="EURUSD",
            timeframe="M5",
            direction="BUY",
            ema_slope=0.0001,
            adx=25.0,
            atr_ratio=1.8,
            confidence=0.8,
        )
        h1_signal = MTFSignal(
            timestamp=1640995800.0,
            symbol="EURUSD",
            timeframe="H1",
            direction="SELL",
            ema_slope=0.0001,
            adx=24.0,
            atr_ratio=1.9,
            confidence=0.85,
        )

        result, agreement, issues = validator.validate_direction_coherence(m5_signal, h1_signal)

        assert result == ValidationResult.FAIL
        assert agreement is False
        assert len(issues) == 1
        assert "Direction mismatch" in issues[0]

    def test_validate_ema_alignment_pass(self):
        """Test EMA alignment validation - passing case."""
        validator = MTFSignalValidator()

        m5_signal = MTFSignal(
            timestamp=1640995200.0,
            symbol="EURUSD",
            timeframe="M5",
            direction="BUY",
            ema_slope=0.0001,
            adx=25.0,
            atr_ratio=1.8,
            confidence=0.8,
        )
        h1_signal = MTFSignal(
            timestamp=1640995800.0,
            symbol="EURUSD",
            timeframe="H1",
            direction="BUY",
            ema_slope=0.0001,
            adx=24.0,
            atr_ratio=1.9,
            confidence=0.85,
        )

        result, slope_diff, issues = validator.validate_ema_alignment(m5_signal, h1_signal)

        assert result == ValidationResult.PASS
        assert slope_diff == 0.0
        assert len(issues) == 0

    def test_validate_ema_alignment_fail(self):
        """Test EMA alignment validation - failing case (opposite slopes)."""
        validator = MTFSignalValidator()

        m5_signal = MTFSignal(
            timestamp=1640995200.0,
            symbol="EURUSD",
            timeframe="M5",
            direction="BUY",
            ema_slope=0.0001,
            adx=25.0,
            atr_ratio=1.8,
            confidence=0.8,
        )
        h1_signal = MTFSignal(
            timestamp=1640995800.0,
            symbol="EURUSD",
            timeframe="H1",
            direction="BUY",
            ema_slope=-0.0001,
            adx=24.0,
            atr_ratio=1.9,
            confidence=0.85,
        )

        result, slope_diff, issues = validator.validate_ema_alignment(m5_signal, h1_signal)

        assert result == ValidationResult.FAIL
        assert slope_diff > 0
        assert len(issues) == 1
        assert "EMA slope direction mismatch" in issues[0]

    def test_validate_adx_confirmation_pass(self):
        """Test ADX confirmation validation - passing case."""
        validator = MTFSignalValidator()

        m5_signal = MTFSignal(
            timestamp=1640995200.0,
            symbol="EURUSD",
            timeframe="M5",
            direction="BUY",
            ema_slope=0.0001,
            adx=25.0,
            atr_ratio=1.8,
            confidence=0.8,
        )
        h1_signal = MTFSignal(
            timestamp=1640995800.0,
            symbol="EURUSD",
            timeframe="H1",
            direction="BUY",
            ema_slope=0.0001,
            adx=24.0,
            atr_ratio=1.9,
            confidence=0.85,
        )

        result, adx_ratio, issues = validator.validate_adx_confirmation(m5_signal, h1_signal)

        assert result == ValidationResult.PASS
        assert adx_ratio > 0.9  # Should be close to 1.0
        assert len(issues) == 0

    def test_validate_adx_confirmation_fail(self):
        """Test ADX confirmation validation - failing case (low ADX)."""
        validator = MTFSignalValidator()

        m5_signal = MTFSignal(
            timestamp=1640995200.0,
            symbol="EURUSD",
            timeframe="M5",
            direction="BUY",
            ema_slope=0.0001,
            adx=15.0,
            atr_ratio=1.8,
            confidence=0.8,
        )
        h1_signal = MTFSignal(
            timestamp=1640995800.0,
            symbol="EURUSD",
            timeframe="H1",
            direction="BUY",
            ema_slope=0.0001,
            adx=18.0,
            atr_ratio=1.9,
            confidence=0.85,
        )

        result, adx_ratio, issues = validator.validate_adx_confirmation(m5_signal, h1_signal)

        assert result == ValidationResult.FAIL
        assert len(issues) >= 1
        assert any("ADX below threshold" in issue for issue in issues)

    def test_validate_timing_sequence_pass(self):
        """Test timing sequence validation - passing case."""
        validator = MTFSignalValidator()

        m5_signal = MTFSignal(
            timestamp=1640995200.0,
            symbol="EURUSD",
            timeframe="M5",
            direction="BUY",
            ema_slope=0.0001,
            adx=25.0,
            atr_ratio=1.8,
            confidence=0.8,
        )
        h1_signal = MTFSignal(
            timestamp=1640995500.0,
            symbol="EURUSD",
            timeframe="H1",  # 5 minutes later
            direction="BUY",
            ema_slope=0.0001,
            adx=24.0,
            atr_ratio=1.9,
            confidence=0.85,
        )

        result, timing_offset, issues = validator.validate_timing_sequence(m5_signal, h1_signal)

        assert result == ValidationResult.PASS
        assert timing_offset == 300.0  # 5 minutes
        assert len(issues) == 0

    def test_validate_timing_sequence_fail(self):
        """Test timing sequence validation - failing case (large offset)."""
        validator = MTFSignalValidator()

        m5_signal = MTFSignal(
            timestamp=1640995200.0,
            symbol="EURUSD",
            timeframe="M5",
            direction="BUY",
            ema_slope=0.0001,
            adx=25.0,
            atr_ratio=1.8,
            confidence=0.8,
        )
        h1_signal = MTFSignal(
            timestamp=1640997000.0,
            symbol="EURUSD",
            timeframe="H1",  # 30 minutes later
            direction="BUY",
            ema_slope=0.0001,
            adx=24.0,
            atr_ratio=1.9,
            confidence=0.85,
        )

        result, timing_offset, issues = validator.validate_timing_sequence(m5_signal, h1_signal)

        assert result == ValidationResult.FAIL
        assert timing_offset == 1800.0  # 30 minutes
        assert len(issues) == 1
        assert "Large timing offset" in issues[0]

    def test_validate_signal_pair_all_pass(self):
        """Test complete signal pair validation - all checks pass."""
        validator = MTFSignalValidator()

        m5_signal = MTFSignal(
            timestamp=1640995200.0,
            symbol="EURUSD",
            timeframe="M5",
            direction="BUY",
            ema_slope=0.0001,
            adx=25.0,
            atr_ratio=1.8,
            confidence=0.8,
            irb_detected=True,
        )
        h1_signal = MTFSignal(
            timestamp=1640995500.0,
            symbol="EURUSD",
            timeframe="H1",
            direction="BUY",
            ema_slope=0.0001,
            adx=24.0,
            atr_ratio=1.9,
            confidence=0.85,
            irb_detected=True,
        )

        report = validator.validate_signal_pair(m5_signal, h1_signal, "EURUSD", 1640995200.0)

        assert report.symbol == "EURUSD"
        assert report.direction_coherence == ValidationResult.PASS
        assert report.ema_alignment == ValidationResult.PASS
        assert report.adx_confirmation == ValidationResult.PASS
        assert report.timing_sequence == ValidationResult.PASS
        assert report.irb_alignment == ValidationResult.PASS
        assert report.coherence_score > 0.9  # Should be high
        assert report.direction_agreement is True

    def test_compute_coherence_score(self):
        """Test coherence score computation."""
        validator = MTFSignalValidator()

        # Create a report with all passing validations
        report = MTFValidationReport(
            timestamp=1640995200.0,
            symbol="EURUSD",
            direction_coherence=ValidationResult.PASS,
            ema_alignment=ValidationResult.PASS,
            adx_confirmation=ValidationResult.PASS,
            timing_sequence=ValidationResult.PASS,
            irb_alignment=ValidationResult.PASS,
        )

        score = validator.compute_coherence_score(report)
        assert score == 1.0  # Perfect score

        # Test with mixed results
        report.direction_coherence = ValidationResult.FAIL
        score = validator.compute_coherence_score(report)
        assert score < 1.0  # Should be reduced

    def test_run_validation_end_to_end(self, temp_files):
        """Test complete validation workflow."""
        validator = MTFSignalValidator(
            m5_signals_path=temp_files["m5_path"],
            h1_signals_path=temp_files["h1_path"],
            output_path=temp_files["output_path"],
        )

        reports = validator.run_validation(["EURUSD"])

        assert len(reports) > 0
        assert temp_files["output_path"].exists()

        # Check output file content
        with open(temp_files["output_path"]) as f:
            lines = f.readlines()
            assert len(lines) > 0

            # Parse first report
            first_report = json.loads(lines[0])
            assert "symbol" in first_report
            assert "coherence_score" in first_report
            assert "direction_coherence" in first_report

    def test_generate_summary(self, temp_files):
        """Test validation summary generation."""
        validator = MTFSignalValidator(
            m5_signals_path=temp_files["m5_path"],
            h1_signals_path=temp_files["h1_path"],
        )

        reports = validator.run_validation(["EURUSD"])
        summary = validator.generate_summary(reports)

        assert "total_reports" in summary
        assert "direction_coherence_pass_rate" in summary
        assert "ema_alignment_pass_rate" in summary
        assert "adx_confirmation_pass_rate" in summary
        assert "timing_sequence_pass_rate" in summary
        assert "overall_pass_rate" in summary
        assert "average_coherence_score" in summary

        assert summary["total_reports"] > 0
        assert 0.0 <= summary["average_coherence_score"] <= 1.0

    def test_empty_signals_handling(self):
        """Test handling of empty signal files."""
        validator = MTFSignalValidator(
            m5_signals_path=Path("/non/existent"),
            h1_signals_path=Path("/non/existent"),
        )

        reports = validator.run_validation(["EURUSD"])
        summary = validator.generate_summary(reports)

        assert summary["total_reports"] == 0

    def test_no_data_validation_results(self):
        """Test validation with no data."""
        validator = MTFSignalValidator()

        result, agreement, issues = validator.validate_direction_coherence(None, None)
        assert result == ValidationResult.NO_DATA
        assert agreement is False
        assert len(issues) == 0

        result, slope_diff, issues = validator.validate_ema_alignment(None, None)
        assert result == ValidationResult.NO_DATA
        assert slope_diff == 0.0
        assert len(issues) == 0


class TestMTFValidationIntegration:
    """Integration tests for MTF validation."""

    def test_mtf_validation_with_real_timeframes(self):
        """Test MTF validation with realistic timeframe data."""
        validator = MTFSignalValidator()

        # Simulate M5 signal
        m5_signal = MTFSignal(
            timestamp=1640995200.0,  # H1 bar boundary
            symbol="EURUSD",
            timeframe="M5",
            direction="BUY",
            ema_slope=0.000089,
            adx=26.3,
            atr_ratio=1.7,
            confidence=0.82,
            irb_detected=True,
        )

        # Simulate H1 signal (slightly later, aligned trend)
        h1_signal = MTFSignal(
            timestamp=1640995380.0,  # 3 minutes after M5
            symbol="EURUSD",
            timeframe="H1",
            direction="BUY",
            ema_slope=0.000095,  # Similar but slightly different
            adx=25.1,  # Slightly lower but above threshold
            atr_ratio=1.8,
            confidence=0.87,
            irb_detected=True,
        )

        report = validator.validate_signal_pair(m5_signal, h1_signal, "EURUSD", 1640995200.0)

        # Should pass most validations
        assert report.direction_coherence == ValidationResult.PASS
        assert report.ema_alignment == ValidationResult.PASS
        assert report.adx_confirmation == ValidationResult.PASS
        assert report.timing_sequence == ValidationResult.PASS
        assert report.irb_alignment == ValidationResult.PASS
        assert report.coherence_score > 0.9

    def test_mtf_validation_with_conflicting_signals(self):
        """Test MTF validation with conflicting timeframe signals."""
        validator = MTFSignalValidator()

        # M5 signal suggests BUY
        m5_signal = MTFSignal(
            timestamp=1640995200.0,
            symbol="EURUSD",
            timeframe="M5",
            direction="BUY",
            ema_slope=0.000089,
            adx=26.3,
            atr_ratio=1.7,
            confidence=0.82,
            irb_detected=True,
        )

        # H1 signal suggests SELL (conflict!)
        h1_signal = MTFSignal(
            timestamp=1640995380.0,
            symbol="EURUSD",
            timeframe="H1",
            direction="SELL",  # Conflict with M5
            ema_slope=-0.000045,  # Opposite slope
            adx=18.5,  # Below threshold
            atr_ratio=1.8,
            confidence=0.75,
            irb_detected=False,  # IRB conflict too
        )

        report = validator.validate_signal_pair(m5_signal, h1_signal, "EURUSD", 1640995200.0)

        # Should fail multiple validations
        assert report.direction_coherence == ValidationResult.FAIL
        assert report.ema_alignment == ValidationResult.FAIL
        assert report.adx_confirmation == ValidationResult.FAIL
        assert report.irb_alignment == ValidationResult.WARNING
        assert report.coherence_score < 0.5  # Low coherence
        assert len(report.issues) > 0


if __name__ == "__main__":
    pytest.main([__file__])
