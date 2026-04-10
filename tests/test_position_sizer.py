"""Tests for FTMO-compliant position sizer.

Verifies:
  - Lot size calculation matches backtest engine formula
  - Clamping to [min_lot, max_lot] with 2-decimal rounding
  - Cross-check validation with configurable tolerance
  - Edge cases: tiny equity, huge stop, min/max clamping
"""

from __future__ import annotations

import pytest

from novatrade.risk.position_sizer import PositionSizer

# ---------------------------------------------------------------------------
# Calculation tests
# ---------------------------------------------------------------------------


class TestCalculate:
    """Position size calculation (1% equity risk model)."""

    def test_basic_eurusd_long(self):
        """Standard EURUSD long: $10k equity, 50-pip stop → 0.20 lots."""
        sizer = PositionSizer()
        # equity=10000, risk=1%, stop=50 pips, pip_value_per_lot=$10
        # risk_dollars = 10000 * 0.01 = 100
        # volume = 100 / (50 * 10) = 0.20
        lot = sizer.calculate(
            equity=10000,
            entry=1.10500,
            stop=1.10000,
            risk_pct=0.01,
            pip_value=0.0001,
            pip_value_per_lot=10.0,
        )
        assert lot == 0.20

    def test_basic_eurusd_short(self):
        """Short side: stop above entry, same result."""
        sizer = PositionSizer()
        lot = sizer.calculate(
            equity=10000,
            entry=1.10000,
            stop=1.10500,
            risk_pct=0.01,
            pip_value=0.0001,
            pip_value_per_lot=10.0,
        )
        assert lot == 0.20

    def test_tight_stop_larger_lot(self):
        """Tight 20-pip stop → larger lot size."""
        sizer = PositionSizer()
        # risk=100, stop=20 pips → 100/200 = 0.50
        lot = sizer.calculate(
            equity=10000,
            entry=1.10200,
            stop=1.10000,
            risk_pct=0.01,
            pip_value=0.0001,
            pip_value_per_lot=10.0,
        )
        assert lot == 0.50

    def test_wide_stop_small_lot(self):
        """Wide 200-pip stop → small lot."""
        sizer = PositionSizer()
        # risk=100, stop=200 pips → 100/2000 = 0.05
        lot = sizer.calculate(
            equity=10000,
            entry=1.12000,
            stop=1.10000,
            risk_pct=0.01,
            pip_value=0.0001,
            pip_value_per_lot=10.0,
        )
        assert lot == 0.05

    def test_clamp_max(self):
        """Lot clamped to max_lot when risk model suggests more."""
        sizer = PositionSizer(max_lot=1.00)
        # equity=100000, risk=1%, stop=10 pips → 100 lots (way over)
        lot = sizer.calculate(
            equity=100000,
            entry=1.10100,
            stop=1.10000,
            risk_pct=0.01,
            pip_value=0.0001,
            pip_value_per_lot=10.0,
        )
        assert lot == 1.00

    def test_clamp_min(self):
        """Lot clamped to min_lot when risk model suggests less."""
        sizer = PositionSizer(min_lot=0.01)
        # equity=500, risk=1%, stop=200 pips → 5/2000 = 0.0025 → clamped to 0.01
        lot = sizer.calculate(
            equity=500,
            entry=1.12000,
            stop=1.10000,
            risk_pct=0.01,
            pip_value=0.0001,
            pip_value_per_lot=10.0,
        )
        assert lot == 0.01

    def test_rounding_to_2_decimals(self):
        """Volume rounded to 2 decimal places."""
        sizer = PositionSizer()
        # risk=150, stop=70 pips → 150/700 = 0.21428... → 0.21
        lot = sizer.calculate(
            equity=15000,
            entry=1.10700,
            stop=1.10000,
            risk_pct=0.01,
            pip_value=0.0001,
            pip_value_per_lot=10.0,
        )
        assert lot == 0.21

    def test_custom_risk_pct(self):
        """2% risk instead of 1%."""
        sizer = PositionSizer()
        lot = sizer.calculate(
            equity=10000,
            entry=1.10500,
            stop=1.10000,
            risk_pct=0.02,
            pip_value=0.0001,
            pip_value_per_lot=10.0,
        )
        assert lot == 0.40

    def test_custom_bounds(self):
        """Custom min/max lot bounds."""
        sizer = PositionSizer(min_lot=0.05, max_lot=0.50)
        # Would be 0.20 normally
        lot = sizer.calculate(
            equity=10000,
            entry=1.10500,
            stop=1.10000,
            risk_pct=0.01,
            pip_value=0.0001,
            pip_value_per_lot=10.0,
        )
        assert lot == 0.20
        assert sizer.min_lot == 0.05
        assert sizer.max_lot == 0.50


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidate:
    """Cross-check validation (asymmetric: under-sizing always OK)."""

    def test_exact_match(self):
        sizer = PositionSizer()
        ok, reason = sizer.validate(requested=0.20, calculated=0.20)
        assert ok is True
        assert "OK" in reason

    def test_slightly_over_within_tolerance(self):
        sizer = PositionSizer()
        ok, reason = sizer.validate(requested=0.22, calculated=0.20, tolerance=0.10)
        assert ok is True  # 10% over = exactly at tolerance

    def test_over_exceeds_tolerance(self):
        sizer = PositionSizer()
        ok, reason = sizer.validate(requested=0.30, calculated=0.20, tolerance=0.10)
        assert ok is False
        assert "over-sized" in reason

    def test_under_sized_always_ok(self):
        """Under-sizing (conservative risk) always passes."""
        sizer = PositionSizer()
        ok, reason = sizer.validate(requested=0.10, calculated=0.50, tolerance=0.10)
        assert ok is True
        assert "conservative" in reason

    def test_zero_calculated(self):
        sizer = PositionSizer()
        ok, reason = sizer.validate(requested=0.20, calculated=0.0)
        assert ok is False
        assert "non-positive" in reason

    def test_zero_requested_auto_sizes(self):
        sizer = PositionSizer()
        ok, reason = sizer.validate(requested=0.0, calculated=0.20)
        assert ok is True
        assert "auto-sized" in reason

    def test_slightly_under(self):
        sizer = PositionSizer()
        ok, reason = sizer.validate(requested=0.19, calculated=0.20, tolerance=0.10)
        assert ok is True  # under-sized = conservative


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrors:
    """Input validation."""

    def test_zero_equity(self):
        sizer = PositionSizer()
        with pytest.raises(ValueError, match="equity"):
            sizer.calculate(equity=0, entry=1.10, stop=1.09)

    def test_negative_equity(self):
        sizer = PositionSizer()
        with pytest.raises(ValueError, match="equity"):
            sizer.calculate(equity=-1000, entry=1.10, stop=1.09)

    def test_zero_risk_pct(self):
        sizer = PositionSizer()
        with pytest.raises(ValueError, match="risk_pct"):
            sizer.calculate(equity=10000, entry=1.10, stop=1.09, risk_pct=0)

    def test_entry_equals_stop(self):
        sizer = PositionSizer()
        with pytest.raises(ValueError, match="entry and stop must differ"):
            sizer.calculate(equity=10000, entry=1.10, stop=1.10)

    def test_zero_pip_value(self):
        sizer = PositionSizer()
        with pytest.raises(ValueError, match="pip_value"):
            sizer.calculate(equity=10000, entry=1.10, stop=1.09, pip_value=0)

    def test_zero_pip_value_per_lot(self):
        sizer = PositionSizer()
        with pytest.raises(ValueError, match="pip_value_per_lot"):
            sizer.calculate(equity=10000, entry=1.10, stop=1.09, pip_value_per_lot=0)


# ---------------------------------------------------------------------------
# Phase 2: $100K FTMO sizing tests (0.75% risk, max_lot=10.0)
# ---------------------------------------------------------------------------


class TestFTMO100KSizing:
    """Verify correct lot sizing for $100K FTMO challenge accounts.

    Kelly criterion validates 0.75% as optimal risk per trade.
    With max_lot=10.0, the sizer should no longer under-risk by 5×.
    """

    def test_100k_15pip_stop_returns_10_lots(self):
        """$100K, 1.5% risk, 15-pip stop → 10.00 lots."""
        sizer = PositionSizer()  # defaults: max_lot=50.0, risk_pct=0.015
        # risk_dollars = 100000 * 0.015 = 1500
        # stop_distance_pips = 15
        # volume = 1500 / (15 * 10) = 10.00
        lot = sizer.calculate(
            equity=100_000,
            entry=1.10150,
            stop=1.10000,
        )
        assert lot == 10.00

    def test_100k_20pip_stop_returns_7_50_lots(self):
        """$100K, 1.5% risk, 20-pip stop → 7.50 lots."""
        sizer = PositionSizer()
        # risk_dollars = 1500, volume = 1500 / (20 * 10) = 7.50
        lot = sizer.calculate(
            equity=100_000,
            entry=1.10200,
            stop=1.10000,
        )
        assert lot == 7.50

    def test_100k_10pip_stop_returns_15_lots(self):
        """$100K, 1.5% risk, 10-pip stop → 15.00 lots."""
        sizer = PositionSizer()
        # risk_dollars = 1500, volume = 1500 / (10 * 10) = 15.00
        lot = sizer.calculate(
            equity=100_000,
            entry=1.10100,
            stop=1.10000,
        )
        assert lot == 15.00

    def test_100k_50pip_stop_returns_3_lots(self):
        """$100K, 1.5% risk, 50-pip stop → 3.00 lots."""
        sizer = PositionSizer()
        # risk_dollars = 1500, volume = 1500 / (50 * 10) = 3.00
        lot = sizer.calculate(
            equity=100_000,
            entry=1.10500,
            stop=1.10000,
        )
        assert lot == 3.00

    def test_default_risk_pct_is_1_5(self):
        """Default risk_pct should be 1.5%."""
        sizer = PositionSizer()
        # With 1.5% risk (default), $10K, 50-pip stop:
        # risk_dollars = 10000 * 0.015 = 150
        # volume = 150 / (50 * 10) = 0.30
        lot = sizer.calculate(
            equity=10_000,
            entry=1.10500,
            stop=1.10000,
        )
        assert lot == 0.30

    def test_default_max_lot_is_50(self):
        """Default max_lot should be 50.0."""
        sizer = PositionSizer()
        assert sizer.max_lot == 50.0

    def test_clamp_at_50_lots(self):
        """Verify clamping at 50.0 lots for very tight stops."""
        sizer = PositionSizer()
        # $500K, 1.5% risk, 5-pip stop → 7500 / 50 = 150.0 → clamped to 50.0
        lot = sizer.calculate(
            equity=500_000,
            entry=1.10050,
            stop=1.10000,
        )
        assert lot == 50.0

    def test_over_50_logs_warning(self, caplog):
        """Calculated volume > 50.0 should log a warning."""
        import logging

        sizer = PositionSizer()
        with caplog.at_level(logging.WARNING, logger="novatrade.risk.position_sizer"):
            lot = sizer.calculate(
                equity=500_000,
                entry=1.10050,
                stop=1.10000,
            )
        assert lot == 50.0
        assert "exceeds 50.0 lots" in caplog.text

    def test_backward_compat_explicit_1_lot_max(self):
        """Callers can still explicitly set max_lot=1.0 for demo accounts."""
        sizer = PositionSizer(max_lot=1.00)
        lot = sizer.calculate(
            equity=100_000,
            entry=1.10150,
            stop=1.10000,
        )
        assert lot == 1.00  # clamped to explicit max

    def test_10k_demo_with_defaults(self):
        """$10K demo with default 1.5% risk still works correctly."""
        sizer = PositionSizer()
        # risk=150, stop=30 pips → 150/300 = 0.50
        lot = sizer.calculate(
            equity=10_000,
            entry=1.10300,
            stop=1.10000,
        )
        assert lot == 0.50
