"""Tests for first-trade validation sequence (R4 priority from FTMO survival research).

Validates the comprehensive first trade execution checks designed to catch
integration bugs before significant capital is at risk.
"""

from novatrade.risk.first_trade_validator import (
    FirstTradeValidationResult,
    FirstTradeValidator,
)


class TestFirstTradeValidator:
    """Test the FirstTradeValidator implementation."""

    def test_initial_state(self) -> None:
        """Test validator initial state."""
        validator = FirstTradeValidator()

        assert validator.is_first_trade_pending is True

        # Should not validate without preparation
        result = validator.validate_first_trade_execution(
            actual_entry_price=1.10150,
            actual_sl_price=1.10000,
            actual_volume=0.05,
            execution_time_ms=200,
        )

        assert not result.passed
        assert "not properly initialized" in result.errors[0]

    def test_prepare_first_trade_checks(self) -> None:
        """Test preparation of first trade validation."""
        validator = FirstTradeValidator()

        validator.prepare_first_trade_checks(
            entry_price=1.10150,
            sl_price=1.10000,
            volume=0.05,
            typical_spread_pips=1.2,
        )

        # Internal state should be set
        assert validator._expected_entry_price == 1.10150
        assert validator._expected_sl_price == 1.10000
        assert validator._expected_volume == 0.05
        assert validator._typical_spread_pips == 1.2

    def test_successful_first_trade_validation(self) -> None:
        """Test successful first trade validation with good parameters."""
        validator = FirstTradeValidator()

        # Prepare validation
        validator.prepare_first_trade_checks(
            entry_price=1.10150,
            sl_price=1.10000,
            volume=0.05,
            typical_spread_pips=1.0,
        )

        # Execute validation with good parameters
        result = validator.validate_first_trade_execution(
            actual_entry_price=1.10152,  # 0.2 pip slippage (acceptable)
            actual_sl_price=1.10000,  # Exact SL placement
            actual_volume=0.05,  # Exact volume
            execution_time_ms=250,  # Good latency
            current_spread_pips=1.1,  # Normal spread
        )

        assert result.passed
        assert len(result.errors) == 0
        assert "execution_latency" in result.checks_performed
        assert "volume_accuracy" in result.checks_performed
        assert "entry_price_accuracy" in result.checks_performed
        assert "sl_placement_accuracy" in result.checks_performed
        assert "spread_behavior" in result.checks_performed

        # Metrics should be recorded
        assert result.execution_metrics["execution_time_ms"] == 250
        assert abs(result.execution_metrics["entry_deviation_pips"] - 0.2) < 0.01
        assert result.execution_metrics["sl_deviation_pips"] == 0.0
        assert result.execution_metrics["volume_deviation_pct"] == 0.0
        assert result.execution_metrics["current_spread_pips"] == 1.1

        # First trade should be marked as completed
        assert not validator.is_first_trade_pending

    def test_execution_latency_failure(self) -> None:
        """Test validation failure due to high execution latency."""
        validator = FirstTradeValidator()

        validator.prepare_first_trade_checks(
            entry_price=1.10150,
            sl_price=1.10000,
            volume=0.05,
        )

        result = validator.validate_first_trade_execution(
            actual_entry_price=1.10150,
            actual_sl_price=1.10000,
            actual_volume=0.05,
            execution_time_ms=3000,  # 3 seconds > 2 second threshold
        )

        assert not result.passed
        assert any("execution latency too high" in error.lower() for error in result.errors)
        assert result.execution_metrics["execution_time_ms"] == 3000

    def test_volume_deviation_failure(self) -> None:
        """Test validation failure due to volume deviation."""
        validator = FirstTradeValidator()

        validator.prepare_first_trade_checks(
            entry_price=1.10150,
            sl_price=1.10000,
            volume=0.05,
        )

        result = validator.validate_first_trade_execution(
            actual_entry_price=1.10150,
            actual_sl_price=1.10000,
            actual_volume=0.055,  # 10% deviation > 5% threshold
            execution_time_ms=200,
        )

        assert not result.passed
        assert any("volume deviation too high" in error.lower() for error in result.errors)
        assert abs(result.execution_metrics["volume_deviation_pct"] - 10.0) < 0.01

    def test_entry_price_slippage_failure(self) -> None:
        """Test validation failure due to excessive entry price slippage."""
        validator = FirstTradeValidator()

        validator.prepare_first_trade_checks(
            entry_price=1.10150,
            sl_price=1.10000,
            volume=0.05,
            typical_spread_pips=1.0,
        )

        result = validator.validate_first_trade_execution(
            actual_entry_price=1.10190,  # 4 pip slippage > 3 pip threshold (3x spread)
            actual_sl_price=1.10000,
            actual_volume=0.05,
            execution_time_ms=200,
        )

        assert not result.passed
        assert any("entry price deviation too high" in error.lower() for error in result.errors)
        assert abs(result.execution_metrics["entry_deviation_pips"] - 4.0) < 0.01

    def test_sl_placement_error(self) -> None:
        """Test validation failure due to stop-loss placement error."""
        validator = FirstTradeValidator()

        validator.prepare_first_trade_checks(
            entry_price=1.10150,
            sl_price=1.10000,
            volume=0.05,
        )

        result = validator.validate_first_trade_execution(
            actual_entry_price=1.10150,
            actual_sl_price=1.10030,  # 3 pip error > 2 pip threshold
            actual_volume=0.05,
            execution_time_ms=200,
        )

        assert not result.passed
        assert any("stop-loss placement error" in error.lower() for error in result.errors)
        assert abs(result.execution_metrics["sl_deviation_pips"] - 3.0) < 0.01

    def test_abnormal_spread_warning(self) -> None:
        """Test warning for abnormally wide spread."""
        validator = FirstTradeValidator()

        validator.prepare_first_trade_checks(
            entry_price=1.10150,
            sl_price=1.10000,
            volume=0.05,
            typical_spread_pips=1.0,
        )

        result = validator.validate_first_trade_execution(
            actual_entry_price=1.10150,
            actual_sl_price=1.10000,
            actual_volume=0.05,
            execution_time_ms=200,
            current_spread_pips=6.0,  # 6x typical spread = abnormal
        )

        assert not result.passed
        assert any("abnormally wide spread" in error.lower() for error in result.errors)
        assert len(result.warnings) >= 0  # May also have warnings

    def test_multiple_failures(self) -> None:
        """Test validation with multiple simultaneous failures."""
        validator = FirstTradeValidator()

        validator.prepare_first_trade_checks(
            entry_price=1.10150,
            sl_price=1.10000,
            volume=0.05,
        )

        result = validator.validate_first_trade_execution(
            actual_entry_price=1.10190,  # 4 pip slippage (fail)
            actual_sl_price=1.10030,  # 3 pip SL error (fail)
            actual_volume=0.055,  # 10% volume deviation (fail)
            execution_time_ms=3000,  # 3s latency (fail)
        )

        assert not result.passed
        assert len(result.errors) == 4  # All checks should fail

        # All metrics should be recorded despite failures
        assert result.execution_metrics["execution_time_ms"] == 3000
        assert abs(result.execution_metrics["entry_deviation_pips"] - 4.0) < 0.01
        assert abs(result.execution_metrics["sl_deviation_pips"] - 3.0) < 0.01
        assert abs(result.execution_metrics["volume_deviation_pct"] - 10.0) < 0.01

    def test_warnings_only_case(self) -> None:
        """Test case with warnings but no failures."""
        validator = FirstTradeValidator()

        validator.prepare_first_trade_checks(
            entry_price=1.10150,
            sl_price=1.10000,
            volume=0.05,
            typical_spread_pips=1.0,
        )

        result = validator.validate_first_trade_execution(
            actual_entry_price=1.10165,  # 1.5 pip slippage (warning threshold)
            actual_sl_price=1.10000,
            actual_volume=0.05,
            execution_time_ms=1500,  # 1.5s latency (warning threshold)
            current_spread_pips=3.5,  # 3.5x spread (warning threshold)
        )

        assert result.passed  # Should pass despite warnings
        assert len(result.errors) == 0
        assert len(result.warnings) >= 2  # Should have latency and spread warnings

    def test_subsequent_trades_skip_validation(self) -> None:
        """Test that subsequent trades skip first trade validation."""
        validator = FirstTradeValidator()

        # Complete first trade validation
        validator.prepare_first_trade_checks(
            entry_price=1.10150,
            sl_price=1.10000,
            volume=0.05,
        )

        result1 = validator.validate_first_trade_execution(
            actual_entry_price=1.10150,
            actual_sl_price=1.10000,
            actual_volume=0.05,
            execution_time_ms=200,
        )

        assert result1.passed
        assert not validator.is_first_trade_pending

        # Second trade should skip validation
        result2 = validator.validate_first_trade_execution(
            actual_entry_price=1.10190,  # Would fail if validated
            actual_sl_price=1.10030,
            actual_volume=0.055,
            execution_time_ms=3000,
        )

        assert result2.passed  # Should pass because validation is skipped
        assert len(result2.errors) == 0
        assert len(result2.checks_performed) == 0  # No checks performed

    def test_reset_functionality(self) -> None:
        """Test validator reset functionality."""
        validator = FirstTradeValidator()

        # Complete first trade
        validator.prepare_first_trade_checks(1.10150, 1.10000, 0.05)
        validator.validate_first_trade_execution(1.10150, 1.10000, 0.05, 200)

        assert not validator.is_first_trade_pending

        # Reset should restore to initial state
        validator.reset()

        assert validator.is_first_trade_pending
        assert validator._expected_entry_price is None
        assert validator._expected_sl_price is None
        assert validator._expected_volume is None

    def test_prepare_non_first_trade_no_op(self) -> None:
        """Test that preparation on non-first trade is a no-op."""
        validator = FirstTradeValidator()

        # Mark first trade as completed
        validator.mark_first_trade_completed()
        assert not validator.is_first_trade_pending

        # Preparation should be ignored
        validator.prepare_first_trade_checks(1.10150, 1.10000, 0.05)

        # Internal state should remain None
        assert validator._expected_entry_price is None
        assert validator._expected_sl_price is None
        assert validator._expected_volume is None


class TestFirstTradeValidationResult:
    """Test the FirstTradeValidationResult dataclass."""

    def test_result_structure(self) -> None:
        """Test validation result structure."""
        result = FirstTradeValidationResult(
            passed=True,
            errors=["error1", "error2"],
            warnings=["warning1"],
            checks_performed={"check1": True, "check2": False},
            execution_metrics={"metric1": 1.0, "metric2": 2.0},
        )

        assert result.passed is True
        assert result.errors == ["error1", "error2"]
        assert result.warnings == ["warning1"]
        assert result.checks_performed == {"check1": True, "check2": False}
        assert result.execution_metrics == {"metric1": 1.0, "metric2": 2.0}
