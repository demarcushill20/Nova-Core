"""Tests for FTMO-compliant position sizer.

Verifies:
  - Lot size calculation matches backtest engine formula
  - Clamping to [min_lot, max_lot] with 2-decimal rounding
  - Cross-check validation with configurable tolerance
  - Edge cases: tiny equity, huge stop, min/max clamping
"""

from __future__ import annotations

import pytest

from novatrade.risk.position_sizer import DrawdownScaler, PositionSizer

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


# ---------------------------------------------------------------------------
# Phase 3: Drawdown-adaptive & consecutive-loss scaling
# ---------------------------------------------------------------------------


class TestDrawdownScaling:
    """Drawdown-based position size reduction (4-tier)."""

    def test_tier_full_size_at_0pct(self):
        scaler = DrawdownScaler()
        assert scaler.drawdown_scale(0.0) == 1.0

    def test_tier_full_size_at_49pct(self):
        scaler = DrawdownScaler()
        assert scaler.drawdown_scale(0.49) == 1.0

    def test_tier_75pct_at_50pct_dd(self):
        scaler = DrawdownScaler()
        assert scaler.drawdown_scale(0.50) == 0.75

    def test_tier_75pct_at_69pct_dd(self):
        scaler = DrawdownScaler()
        assert scaler.drawdown_scale(0.69) == 0.75

    def test_tier_50pct_at_70pct_dd(self):
        scaler = DrawdownScaler()
        assert scaler.drawdown_scale(0.70) == 0.50

    def test_tier_50pct_at_84pct_dd(self):
        scaler = DrawdownScaler()
        assert scaler.drawdown_scale(0.84) == 0.50

    def test_tier_survival_at_85pct_dd(self):
        scaler = DrawdownScaler()
        assert scaler.drawdown_scale(0.85) == 0.25

    def test_tier_survival_at_100pct_dd(self):
        scaler = DrawdownScaler()
        assert scaler.drawdown_scale(1.0) == 0.25

    def test_tier_survival_above_100pct(self):
        """Even if DD exceeds limit, return survival mode (not crash)."""
        scaler = DrawdownScaler()
        assert scaler.drawdown_scale(1.5) == 0.25

    def test_negative_dd_treated_as_zero(self):
        scaler = DrawdownScaler()
        assert scaler.drawdown_scale(-0.1) == 1.0


class TestConsecutiveLossScaling:
    """Consecutive-loss-based position size reduction."""

    def test_zero_losses_full_size(self):
        scaler = DrawdownScaler()
        assert scaler.loss_streak_scale() == 1.0

    def test_one_loss_full_size(self):
        scaler = DrawdownScaler()
        scaler.record_loss()
        assert scaler.loss_streak_scale() == 1.0

    def test_two_losses_75pct(self):
        scaler = DrawdownScaler()
        scaler.record_loss()
        scaler.record_loss()
        assert scaler.loss_streak_scale() == 0.75

    def test_three_losses_50pct(self):
        scaler = DrawdownScaler()
        for _ in range(3):
            scaler.record_loss()
        assert scaler.loss_streak_scale() == 0.50

    def test_four_losses_survival(self):
        scaler = DrawdownScaler()
        for _ in range(4):
            scaler.record_loss()
        assert scaler.loss_streak_scale() == 0.25

    def test_five_losses_still_survival(self):
        scaler = DrawdownScaler()
        for _ in range(5):
            scaler.record_loss()
        assert scaler.loss_streak_scale() == 0.25

    def test_win_resets_streak(self):
        scaler = DrawdownScaler()
        for _ in range(4):
            scaler.record_loss()
        assert scaler.loss_streak_scale() == 0.25
        scaler.record_win()
        assert scaler.loss_streak_scale() == 1.0
        assert scaler.consecutive_losses == 0

    def test_reset_clears_streak(self):
        scaler = DrawdownScaler()
        for _ in range(3):
            scaler.record_loss()
        scaler.reset()
        assert scaler.consecutive_losses == 0
        assert scaler.loss_streak_scale() == 1.0


class TestCombinedScaling:
    """Combined scaling takes the minimum of drawdown and loss streak."""

    def test_both_full_returns_full(self):
        scaler = DrawdownScaler()
        assert scaler.combined_scale(0.0) == 1.0

    def test_dd_more_restrictive(self):
        """0 losses (1.0) vs 60% DD (0.75) → 0.75 wins."""
        scaler = DrawdownScaler()
        assert scaler.combined_scale(0.60) == 0.75

    def test_losses_more_restrictive(self):
        """3 losses (0.50) vs 30% DD (1.0) → 0.50 wins."""
        scaler = DrawdownScaler()
        for _ in range(3):
            scaler.record_loss()
        assert scaler.combined_scale(0.30) == 0.50

    def test_both_restrictive_takes_min(self):
        """4 losses (0.25) vs 72% DD (0.50) → 0.25 wins."""
        scaler = DrawdownScaler()
        for _ in range(4):
            scaler.record_loss()
        assert scaler.combined_scale(0.72) == 0.25

    def test_equal_scales_returns_same(self):
        """2 losses (0.75) vs 55% DD (0.75) → 0.75."""
        scaler = DrawdownScaler()
        scaler.record_loss()
        scaler.record_loss()
        assert scaler.combined_scale(0.55) == 0.75


class TestScaleVolume:
    """Integration: scale_volume applies combined scaling to base volume."""

    def test_full_scale_no_reduction(self):
        scaler = DrawdownScaler()
        assert scaler.scale_volume(5.00, dd_used_pct=0.0) == 5.00

    def test_75pct_scale(self):
        scaler = DrawdownScaler()
        # 5.00 × 0.75 = 3.75
        assert scaler.scale_volume(5.00, dd_used_pct=0.55) == 3.75

    def test_50pct_scale(self):
        scaler = DrawdownScaler()
        # 5.00 × 0.50 = 2.50
        assert scaler.scale_volume(5.00, dd_used_pct=0.72) == 2.50

    def test_survival_scale(self):
        scaler = DrawdownScaler()
        # 5.00 × 0.25 = 1.25
        assert scaler.scale_volume(5.00, dd_used_pct=0.90) == 1.25

    def test_min_lot_floor(self):
        scaler = DrawdownScaler()
        # 0.02 × 0.25 = 0.005 → clamped to min_lot=0.01
        assert scaler.scale_volume(0.02, dd_used_pct=0.90) == 0.01

    def test_rounding_to_2_decimals(self):
        scaler = DrawdownScaler()
        # 3.33 × 0.75 = 2.4975 → 2.50
        assert scaler.scale_volume(3.33, dd_used_pct=0.55) == 2.50

    def test_loss_streak_reduces_volume(self):
        scaler = DrawdownScaler()
        for _ in range(3):
            scaler.record_loss()
        # 5.00 × 0.50 (3 losses) → 2.50 (dd at 0% = 1.0, so loss streak wins)
        assert scaler.scale_volume(5.00, dd_used_pct=0.0) == 2.50

    def test_end_to_end_with_position_sizer(self):
        """Full integration: PositionSizer → DrawdownScaler → final volume."""
        sizer = PositionSizer()
        scaler = DrawdownScaler()

        # $100K, 1.5% risk, 15-pip stop → 10.00 lots base
        base_lot = sizer.calculate(equity=100_000, entry=1.10150, stop=1.10000)
        assert base_lot == 10.00

        # After 60% DD used → 75% scale → 7.50 lots
        scaled = scaler.scale_volume(base_lot, dd_used_pct=0.60)
        assert scaled == 7.50

    def test_end_to_end_survival_mode(self):
        """Worst case: high DD + loss streak → 25% of base volume."""
        sizer = PositionSizer()
        scaler = DrawdownScaler()
        for _ in range(4):
            scaler.record_loss()

        base_lot = sizer.calculate(equity=100_000, entry=1.10150, stop=1.10000)
        assert base_lot == 10.00

        # 90% DD + 4 losses → both at 0.25 → 10.00 × 0.25 = 2.50
        scaled = scaler.scale_volume(base_lot, dd_used_pct=0.90)
        assert scaled == 2.50


# ---------------------------------------------------------------------------
# Phase 4: Hysteresis bands
# ---------------------------------------------------------------------------


class TestHysteresisScaling:
    """Drawdown hysteresis prevents rapid tier oscillation at boundaries."""

    def test_enter_tier_at_entry_threshold(self):
        """DD rising above 50% enters 0.75 tier."""
        scaler = DrawdownScaler(hysteresis=True)
        assert scaler.drawdown_scale(0.51) == 0.75

    def test_stay_in_tier_above_exit_threshold(self):
        """Once in 0.75 tier, DD at 46% stays in tier (exit is 45%)."""
        scaler = DrawdownScaler(hysteresis=True)
        scaler.drawdown_scale(0.55)  # enter 0.75 tier
        assert scaler.drawdown_scale(0.46) == 0.75  # still above 45% exit

    def test_exit_tier_below_exit_threshold(self):
        """DD dropping below 45% exits 0.75 tier → full size."""
        scaler = DrawdownScaler(hysteresis=True)
        scaler.drawdown_scale(0.55)  # enter 0.75 tier
        assert scaler.drawdown_scale(0.44) == 1.0  # below 45% exit

    def test_enter_half_size_tier(self):
        """DD at 72% enters 0.50 tier."""
        scaler = DrawdownScaler(hysteresis=True)
        assert scaler.drawdown_scale(0.72) == 0.50

    def test_half_size_stays_until_exit(self):
        """In 0.50 tier, DD at 62% stays (exit is 60%)."""
        scaler = DrawdownScaler(hysteresis=True)
        scaler.drawdown_scale(0.72)  # enter 0.50 tier
        assert scaler.drawdown_scale(0.62) == 0.50

    def test_half_size_exits_below_60(self):
        """DD below 60% exits 0.50 tier → 0.75 or 1.0."""
        scaler = DrawdownScaler(hysteresis=True)
        scaler.drawdown_scale(0.72)  # enter 0.50 tier
        # DD at 58% is below 60% exit for half-size, but above 50% entry for 0.75
        assert scaler.drawdown_scale(0.52) == 0.75

    def test_survival_tier_hysteresis(self):
        """DD at 87% enters survival, DD at 76% stays, DD at 74% exits."""
        scaler = DrawdownScaler(hysteresis=True)
        assert scaler.drawdown_scale(0.87) == 0.25
        assert scaler.drawdown_scale(0.76) == 0.25  # above 75% exit
        assert scaler.drawdown_scale(0.74) == 0.50  # below 75% exit → next lower tier (above 70% entry)

    def test_no_hysteresis_mode_matches_legacy(self):
        """hysteresis=False uses simple threshold matching."""
        scaler = DrawdownScaler(hysteresis=False)
        assert scaler.drawdown_scale(0.49) == 1.0
        assert scaler.drawdown_scale(0.50) == 0.75
        assert scaler.drawdown_scale(0.70) == 0.50
        assert scaler.drawdown_scale(0.85) == 0.25

    def test_hysteresis_prevents_oscillation(self):
        """DD hovering around 50% doesn't cause rapid tier changes."""
        scaler = DrawdownScaler(hysteresis=True)
        results = []
        # Simulate DD oscillating around 50%
        for dd in [0.48, 0.51, 0.49, 0.50, 0.47, 0.46, 0.44]:
            results.append(scaler.drawdown_scale(dd))
        # Should enter tier at 0.51, stay until below 0.45
        assert results == [1.0, 0.75, 0.75, 0.75, 0.75, 0.75, 1.0]

    def test_reset_clears_hysteresis_state(self):
        """Reset returns to full size regardless of prior tier."""
        scaler = DrawdownScaler(hysteresis=True)
        scaler.drawdown_scale(0.72)  # enter 0.50 tier
        scaler.reset()
        assert scaler.current_dd_scale == 1.0


# ---------------------------------------------------------------------------
# Phase 4: Total-account DD scaling
# ---------------------------------------------------------------------------


class TestTotalAccountDDScaling:
    """Total-account drawdown scaling (10% max overall loss)."""

    def test_total_dd_below_50_full_size(self):
        scaler = DrawdownScaler(total_dd_enabled=True)
        assert scaler.total_dd_scale(0.30) == 1.0

    def test_total_dd_at_50_three_quarter(self):
        scaler = DrawdownScaler(total_dd_enabled=True)
        assert scaler.total_dd_scale(0.50) == 0.75

    def test_total_dd_at_70_half(self):
        scaler = DrawdownScaler(total_dd_enabled=True)
        assert scaler.total_dd_scale(0.70) == 0.50

    def test_total_dd_at_85_hard_survival(self):
        scaler = DrawdownScaler(total_dd_enabled=True)
        assert scaler.total_dd_scale(0.85) == 0.10

    def test_total_dd_combined_takes_minimum(self):
        """Total DD at 85% (0.10) wins over daily DD at 0% (1.0)."""
        scaler = DrawdownScaler(total_dd_enabled=True)
        scale = scaler.combined_scale(dd_used_pct=0.0, total_dd_used_pct=0.85)
        assert scale == 0.10

    def test_total_dd_not_used_when_disabled(self):
        """Total DD layer ignored when total_dd_enabled=False."""
        scaler = DrawdownScaler(total_dd_enabled=False)
        scale = scaler.combined_scale(dd_used_pct=0.0, total_dd_used_pct=0.95)
        assert scale == 1.0

    def test_scale_volume_with_total_dd(self):
        scaler = DrawdownScaler(total_dd_enabled=True)
        vol = scaler.scale_volume(5.00, dd_used_pct=0.0, total_dd_used_pct=0.85)
        assert vol == 0.50  # 5.00 × 0.10 = 0.50


# ---------------------------------------------------------------------------
# Phase 4: Gradual loss-streak recovery
# ---------------------------------------------------------------------------


class TestGradualLossRecovery:
    """Gradual recovery mode: each win steps up one tier."""

    def test_instant_mode_resets_fully(self):
        scaler = DrawdownScaler(loss_recovery_mode="instant")
        for _ in range(4):
            scaler.record_loss()
        assert scaler.loss_streak_scale() == 0.25
        scaler.record_win()
        assert scaler.loss_streak_scale() == 1.0
        assert scaler.consecutive_losses == 0

    def test_gradual_mode_steps_up_one_tier(self):
        scaler = DrawdownScaler(loss_recovery_mode="gradual")
        for _ in range(4):
            scaler.record_loss()
        assert scaler.loss_streak_scale() == 0.25
        scaler.record_win()
        assert scaler.loss_streak_scale() == 0.50  # one step up
        scaler.record_win()
        assert scaler.loss_streak_scale() == 0.75  # another step
        scaler.record_win()
        assert scaler.loss_streak_scale() == 1.00  # fully recovered

    def test_gradual_mode_loss_after_partial_recovery(self):
        scaler = DrawdownScaler(loss_recovery_mode="gradual")
        for _ in range(4):
            scaler.record_loss()
        scaler.record_win()  # 0.25 → 0.50
        scaler.record_loss()  # back to loss
        # Scale should be computed from consecutive_losses
        assert scaler.loss_streak_scale() <= 0.50

    def test_gradual_consecutive_losses_decrement(self):
        scaler = DrawdownScaler(loss_recovery_mode="gradual")
        for _ in range(4):
            scaler.record_loss()
        assert scaler.consecutive_losses == 4
        scaler.record_win()
        assert scaler.consecutive_losses == 3  # decremented by 1
