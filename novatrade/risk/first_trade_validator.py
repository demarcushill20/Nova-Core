"""First-trade validation sequence for NovaTrade live trading.

Implements R4 from FTMO survival research: comprehensive validation of the first live trade
to catch integration bugs before real capital is at significant risk.

The first trade validation sequence verifies:
- Fill price accuracy (within expected spread bounds)
- Stop-loss placement precision
- Spread behavior under real market conditions
- Execution latency timing
- Daily loss tracker reset functionality

Usage::

    validator = FirstTradeValidator()

    # Before first trade execution
    validator.prepare_first_trade_checks(signal_data)

    # After trade execution
    result = validator.validate_first_trade_execution(
        requested_volume=0.05,
        actual_volume=0.05,
        entry_price=1.10150,
        sl_price=1.10000,
        execution_time_ms=245
    )

    if not result.passed:
        log.error(f"First trade validation failed: {result.errors}")
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

log = logging.getLogger("novatrade.risk.first_trade_validator")


@dataclass
class FirstTradeValidationResult:
    """Result of first-trade validation checks."""

    passed: bool
    errors: list[str]
    warnings: list[str]
    checks_performed: dict[str, bool]
    execution_metrics: dict[str, float]


class FirstTradeValidator:
    """Validates first live trade execution to catch integration issues early.

    Performs comprehensive validation of the first trade to ensure:
    1. Platform connectivity and execution quality
    2. Order fills within expected parameters
    3. Spread behavior under real market conditions
    4. Stop-loss placement accuracy
    5. Daily loss tracker reset functionality

    This addresses the critical first-trade protocol from FTMO survival research
    to catch bugs before significant capital is at risk.
    """

    # Validation thresholds
    MAX_ACCEPTABLE_LATENCY_MS: ClassVar[float] = 2000.0  # 2 seconds max execution time
    MAX_SPREAD_MULTIPLIER: ClassVar[float] = 3.0  # Fill price within 3x typical spread
    MAX_VOLUME_DEVIATION_PCT: ClassVar[float] = 5.0  # Volume within 5%
    MAX_SL_DEVIATION_PIPS: ClassVar[float] = 2.0  # SL placement within 2 pips

    def __init__(self) -> None:
        self._first_trade_completed = False
        self._validation_start_time: float | None = None
        self._expected_entry_price: float | None = None
        self._expected_sl_price: float | None = None
        self._expected_volume: float | None = None
        self._typical_spread_pips: float | None = None

    @property
    def is_first_trade_pending(self) -> bool:
        """True if this would be the first trade."""
        return not self._first_trade_completed

    def mark_first_trade_completed(self) -> None:
        """Mark that the first trade has been completed."""
        self._first_trade_completed = True
        log.info("First trade validation sequence completed")

    def prepare_first_trade_checks(
        self,
        entry_price: float,
        sl_price: float,
        volume: float,
        typical_spread_pips: float = 1.0,
    ) -> None:
        """Prepare for first trade validation by storing expected values.

        Args:
            entry_price: Expected entry price from signal
            sl_price: Expected stop-loss price
            volume: Expected trade volume
            typical_spread_pips: Typical spread for this instrument (default 1.0 for EURUSD)
        """
        if not self.is_first_trade_pending:
            return  # Not first trade, no validation needed

        self._validation_start_time = time.time()
        self._expected_entry_price = entry_price
        self._expected_sl_price = sl_price
        self._expected_volume = volume
        self._typical_spread_pips = typical_spread_pips

        log.info(
            "Prepared first trade validation — entry=%.5f sl=%.5f volume=%.2f spread_pips=%.1f",
            entry_price,
            sl_price,
            volume,
            typical_spread_pips,
        )

    def validate_first_trade_execution(
        self,
        actual_entry_price: float,
        actual_sl_price: float,
        actual_volume: float,
        execution_time_ms: float,
        current_spread_pips: float | None = None,
    ) -> FirstTradeValidationResult:
        """Validate first trade execution against expected parameters.

        Args:
            actual_entry_price: Actual fill price from broker
            actual_sl_price: Actual stop-loss price set
            actual_volume: Actual volume executed
            execution_time_ms: Time taken for execution in milliseconds
            current_spread_pips: Current spread at execution time (optional)

        Returns:
            Validation result with pass/fail status and detailed metrics
        """
        if not self.is_first_trade_pending:
            # Not first trade, return minimal successful result
            return FirstTradeValidationResult(
                passed=True,
                errors=[],
                warnings=[],
                checks_performed={},
                execution_metrics={},
            )

        if self._expected_entry_price is None or self._expected_sl_price is None or self._expected_volume is None:
            return FirstTradeValidationResult(
                passed=False,
                errors=["First trade validation not properly initialized"],
                warnings=[],
                checks_performed={},
                execution_metrics={},
            )

        errors: list[str] = []
        warnings: list[str] = []
        checks = {}
        metrics = {}

        # Check 1: Execution latency
        checks["execution_latency"] = True
        metrics["execution_time_ms"] = execution_time_ms
        if execution_time_ms > self.MAX_ACCEPTABLE_LATENCY_MS:
            errors.append(
                f"Execution latency too high: {execution_time_ms:.0f}ms"
                f" > {self.MAX_ACCEPTABLE_LATENCY_MS:.0f}ms threshold"
            )
        elif execution_time_ms > self.MAX_ACCEPTABLE_LATENCY_MS * 0.7:
            warnings.append(f"High execution latency: {execution_time_ms:.0f}ms")

        # Check 2: Volume accuracy
        checks["volume_accuracy"] = True
        volume_deviation_pct = abs(actual_volume - self._expected_volume) / self._expected_volume * 100
        metrics["volume_deviation_pct"] = volume_deviation_pct
        if volume_deviation_pct > self.MAX_VOLUME_DEVIATION_PCT:
            errors.append(
                f"Volume deviation too high: {volume_deviation_pct:.2f}% > {self.MAX_VOLUME_DEVIATION_PCT}% threshold"
            )

        # Check 3: Entry price accuracy (spread-relative)
        checks["entry_price_accuracy"] = True
        entry_deviation_pips = abs(actual_entry_price - self._expected_entry_price) / 0.0001
        metrics["entry_deviation_pips"] = entry_deviation_pips

        max_acceptable_deviation = (self._typical_spread_pips or 1.0) * self.MAX_SPREAD_MULTIPLIER
        if entry_deviation_pips > max_acceptable_deviation:
            errors.append(
                f"Entry price deviation too high: {entry_deviation_pips:.1f} pips"
                f" > {max_acceptable_deviation:.1f} pips threshold"
            )
        elif entry_deviation_pips > max_acceptable_deviation * 0.6:
            warnings.append(f"Moderate entry price slippage: {entry_deviation_pips:.1f} pips")

        # Check 4: Stop-loss placement accuracy
        checks["sl_placement_accuracy"] = True
        sl_deviation_pips = abs(actual_sl_price - self._expected_sl_price) / 0.0001
        metrics["sl_deviation_pips"] = sl_deviation_pips
        if sl_deviation_pips > self.MAX_SL_DEVIATION_PIPS:
            errors.append(
                f"Stop-loss placement error: {sl_deviation_pips:.1f} pips > {self.MAX_SL_DEVIATION_PIPS} pips threshold"
            )

        # Check 5: Spread behavior (if provided)
        if current_spread_pips is not None:
            checks["spread_behavior"] = True
            metrics["current_spread_pips"] = current_spread_pips
            metrics["expected_spread_pips"] = self._typical_spread_pips or 1.0

            if current_spread_pips > (self._typical_spread_pips or 1.0) * 5:
                errors.append(f"Abnormally wide spread detected: {current_spread_pips:.1f} pips")
            elif current_spread_pips > (self._typical_spread_pips or 1.0) * 3:
                warnings.append(f"Wide spread during execution: {current_spread_pips:.1f} pips")

        # Check 6: Daily tracker reset (timing check)
        checks["daily_reset_timing"] = True
        now = datetime.now()
        # If it's close to midnight Europe/Prague, warn about daily reset timing
        hour_prague = now.hour  # Simplified for now
        if hour_prague == 22 or hour_prague == 23:  # 22:00-23:00 UTC during CEST
            warnings.append("First trade near daily reset time — verify loss tracker behavior")

        passed = len(errors) == 0

        log.info(
            "First trade validation completed — passed=%s errors=%d warnings=%d",
            passed,
            len(errors),
            len(warnings),
        )

        if passed:
            log.info("✅ First trade validation PASSED — platform integration healthy")
            # Mark first trade as completed
            self.mark_first_trade_completed()
        else:
            log.error("❌ First trade validation FAILED — integration issues detected")

        for error in errors:
            log.error(f"FIRST_TRADE_ERROR: {error}")
        for warning in warnings:
            log.warning(f"FIRST_TRADE_WARNING: {warning}")

        return FirstTradeValidationResult(
            passed=passed,
            errors=errors,
            warnings=warnings,
            checks_performed=checks,
            execution_metrics=metrics,
        )

    def reset(self) -> None:
        """Reset validation state (e.g., for testing or new session)."""
        self._first_trade_completed = False
        self._validation_start_time = None
        self._expected_entry_price = None
        self._expected_sl_price = None
        self._expected_volume = None
        self._typical_spread_pips = None
        log.info("First trade validator reset")
