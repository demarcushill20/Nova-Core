"""Tests for DrawdownProportionalRisk (Zeno's Paradox position sizing).

Verifies:
  - Tier assignment by drawdown depth
  - Risk percentage scaling from base
  - Boundary conditions at each tier transition
  - Halt signal at 10%+ drawdown
  - Disabled mode returns base risk unchanged
  - Edge cases: zero equity, negative drawdown, profit territory
  - Consecutive-losses-to-breach estimation
  - Integration with PositionSizer
"""

from __future__ import annotations

import pytest

from novatrade.risk.position_sizer import DrawdownProportionalRisk, PositionSizer

# ---------------------------------------------------------------------------
# Constants for test clarity
# ---------------------------------------------------------------------------
INITIAL = 100_000.0  # $100K FTMO account
BASE_RISK = 0.015  # 1.5% default


# ---------------------------------------------------------------------------
# Tier assignment tests
# ---------------------------------------------------------------------------


class TestTierAssignment:
    """Verify correct tier name at each drawdown level."""

    def test_no_drawdown_is_normal(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_tier_name(INITIAL, INITIAL) == "normal"

    def test_in_profit_is_normal(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_tier_name(105_000, INITIAL) == "normal"

    def test_1pct_drawdown_is_normal(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_tier_name(99_000, INITIAL) == "normal"

    def test_2pct_drawdown_is_cautious(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_tier_name(98_000, INITIAL) == "cautious"

    def test_3pct_drawdown_is_cautious(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_tier_name(97_000, INITIAL) == "cautious"

    def test_4pct_drawdown_is_defensive(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_tier_name(96_000, INITIAL) == "defensive"

    def test_5pct_drawdown_is_defensive(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_tier_name(95_000, INITIAL) == "defensive"

    def test_6pct_drawdown_is_survival(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_tier_name(94_000, INITIAL) == "survival"

    def test_7pct_drawdown_is_survival(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_tier_name(93_000, INITIAL) == "survival"

    def test_8pct_drawdown_is_emergency(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_tier_name(92_000, INITIAL) == "emergency"

    def test_9pct_drawdown_is_emergency(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_tier_name(91_000, INITIAL) == "emergency"

    def test_10pct_drawdown_is_halt(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_tier_name(90_000, INITIAL) == "halt"

    def test_12pct_drawdown_is_halt(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_tier_name(88_000, INITIAL) == "halt"


# ---------------------------------------------------------------------------
# Risk percentage calculation
# ---------------------------------------------------------------------------


class TestRiskPercentage:
    """Verify risk_pct returned at each tier."""

    def test_normal_tier_full_risk(self):
        """0% drawdown → 100% of base = 0.015."""
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(INITIAL, INITIAL) == BASE_RISK

    def test_in_profit_full_risk(self):
        """Equity above initial → full base risk."""
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(110_000, INITIAL) == BASE_RISK

    def test_1pct_drawdown_full_risk(self):
        """1% drawdown is in 0-2% tier → 100% of base."""
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(99_000, INITIAL) == BASE_RISK * 1.00

    def test_cautious_tier_70pct_risk(self):
        """3% drawdown → 70% of base = 0.0105."""
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(97_000, INITIAL) == BASE_RISK * 0.70

    def test_defensive_tier_50pct_risk(self):
        """5% drawdown → 50% of base = 0.0075."""
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(95_000, INITIAL) == BASE_RISK * 0.50

    def test_survival_tier_30pct_risk(self):
        """7% drawdown → 30% of base = 0.0045."""
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(93_000, INITIAL) == BASE_RISK * 0.30

    def test_emergency_tier_20pct_risk(self):
        """9% drawdown → 20% of base = 0.003."""
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(91_000, INITIAL) == BASE_RISK * 0.20

    def test_halt_at_10pct(self):
        """10% drawdown → 0% risk (halt)."""
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(90_000, INITIAL) == 0.0

    def test_halt_beyond_10pct(self):
        """12% drawdown → still 0% (halt)."""
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(88_000, INITIAL) == 0.0


# ---------------------------------------------------------------------------
# Boundary precision tests
# ---------------------------------------------------------------------------


class TestBoundaries:
    """Verify exact boundary transitions between tiers."""

    def test_just_below_2pct_is_normal(self):
        """1.99% drawdown → normal tier."""
        equity = INITIAL * (1 - 0.0199)
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(equity, INITIAL) == BASE_RISK * 1.00

    def test_exactly_2pct_is_cautious(self):
        """2.00% drawdown → cautious tier (boundary belongs to next tier)."""
        equity = INITIAL * (1 - 0.02)
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(equity, INITIAL) == BASE_RISK * 0.70

    def test_just_below_4pct_is_cautious(self):
        equity = INITIAL * (1 - 0.0399)
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(equity, INITIAL) == BASE_RISK * 0.70

    def test_exactly_4pct_is_defensive(self):
        equity = INITIAL * (1 - 0.04)
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(equity, INITIAL) == BASE_RISK * 0.50

    def test_just_below_6pct_is_defensive(self):
        equity = INITIAL * (1 - 0.0599)
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(equity, INITIAL) == BASE_RISK * 0.50

    def test_exactly_6pct_is_survival(self):
        equity = INITIAL * (1 - 0.06)
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(equity, INITIAL) == BASE_RISK * 0.30

    def test_just_below_8pct_is_survival(self):
        equity = INITIAL * (1 - 0.0799)
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(equity, INITIAL) == BASE_RISK * 0.30

    def test_exactly_8pct_is_emergency(self):
        equity = INITIAL * (1 - 0.08)
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(equity, INITIAL) == BASE_RISK * 0.20

    def test_just_below_10pct_is_emergency(self):
        equity = INITIAL * (1 - 0.0999)
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(equity, INITIAL) == BASE_RISK * 0.20

    def test_exactly_10pct_is_halt(self):
        equity = INITIAL * (1 - 0.10)
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(equity, INITIAL) == 0.0


# ---------------------------------------------------------------------------
# Disabled mode
# ---------------------------------------------------------------------------


class TestDisabledMode:
    """When disabled, always returns base risk unchanged."""

    def test_disabled_returns_base_at_0pct_dd(self):
        dpr = DrawdownProportionalRisk(enabled=False)
        assert dpr.get_risk_pct(INITIAL, INITIAL) == BASE_RISK

    def test_disabled_returns_base_at_9pct_dd(self):
        dpr = DrawdownProportionalRisk(enabled=False)
        assert dpr.get_risk_pct(91_000, INITIAL) == BASE_RISK

    def test_disabled_returns_base_at_12pct_dd(self):
        """Even beyond breach, disabled mode returns base."""
        dpr = DrawdownProportionalRisk(enabled=False)
        assert dpr.get_risk_pct(88_000, INITIAL) == BASE_RISK

    def test_disabled_tier_name_is_normal(self):
        dpr = DrawdownProportionalRisk(enabled=False)
        assert dpr.get_tier_name(90_000, INITIAL) == "normal"

    def test_enabled_property(self):
        assert DrawdownProportionalRisk(enabled=True).enabled is True
        assert DrawdownProportionalRisk(enabled=False).enabled is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_zero_initial_balance_returns_base(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(50_000, 0.0) == BASE_RISK

    def test_negative_initial_balance_returns_base(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(50_000, -1.0) == BASE_RISK

    def test_zero_equity_at_100pct_loss_is_halt(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(0.0, INITIAL) == 0.0

    def test_negative_equity_is_halt(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(-5_000, INITIAL) == 0.0

    def test_custom_base_risk(self):
        """Non-default base risk scales tiers correctly."""
        dpr = DrawdownProportionalRisk(base_risk_pct=0.01)
        assert dpr.get_risk_pct(INITIAL, INITIAL) == 0.01
        assert dpr.get_risk_pct(97_000, INITIAL) == 0.01 * 0.70
        assert dpr.get_risk_pct(95_000, INITIAL) == 0.01 * 0.50

    def test_base_risk_property(self):
        dpr = DrawdownProportionalRisk(base_risk_pct=0.005)
        assert dpr.base_risk_pct == 0.005

    def test_negative_base_risk_raises(self):
        with pytest.raises(ValueError, match="non-negative"):
            DrawdownProportionalRisk(base_risk_pct=-0.01)

    def test_zero_base_risk_returns_zero_always(self):
        dpr = DrawdownProportionalRisk(base_risk_pct=0.0)
        assert dpr.get_risk_pct(INITIAL, INITIAL) == 0.0
        assert dpr.get_risk_pct(97_000, INITIAL) == 0.0

    def test_tiny_drawdown_just_above_zero(self):
        """$1 drawdown on $100K → still normal tier."""
        dpr = DrawdownProportionalRisk()
        assert dpr.get_risk_pct(99_999, INITIAL) == BASE_RISK

    def test_small_account_25k(self):
        """$25K account with 3% drawdown → cautious tier."""
        dpr = DrawdownProportionalRisk()
        assert dpr.get_tier_name(24_250, 25_000) == "cautious"
        assert dpr.get_risk_pct(24_250, 25_000) == BASE_RISK * 0.70


# ---------------------------------------------------------------------------
# Consecutive losses to breach estimation
# ---------------------------------------------------------------------------


class TestConsecutiveLossesToBreach:
    """Verify the Zeno's Paradox loss count estimator."""

    def test_at_breach_returns_zero(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_consecutive_losses_to_breach(90_000, INITIAL) == 0

    def test_beyond_breach_returns_zero(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_consecutive_losses_to_breach(85_000, INITIAL) == 0

    def test_full_equity_needs_many_losses(self):
        """Starting at $100K, should need many losses to breach $90K."""
        dpr = DrawdownProportionalRisk()
        count = dpr.get_consecutive_losses_to_breach(INITIAL, INITIAL)
        # With Zeno scaling, should need significantly more than 13 losses
        assert count >= 13

    def test_disabled_returns_999(self):
        dpr = DrawdownProportionalRisk(enabled=False)
        assert dpr.get_consecutive_losses_to_breach(95_000, INITIAL) == 999

    def test_zero_initial_returns_999(self):
        dpr = DrawdownProportionalRisk()
        assert dpr.get_consecutive_losses_to_breach(50_000, 0) == 999

    def test_at_8pct_dd_needs_many_losses(self):
        """At 8% drawdown (emergency tier), need many more losses to breach."""
        dpr = DrawdownProportionalRisk()
        count = dpr.get_consecutive_losses_to_breach(92_000, INITIAL)
        # At emergency tier (0.3% risk with 1.5% base), need $2K more loss
        assert count >= 5

    def test_more_losses_needed_as_dd_deepens(self):
        """Zeno's paradox: deeper drawdown → more losses needed per % of remaining."""
        dpr = DrawdownProportionalRisk()
        losses_from_100k = dpr.get_consecutive_losses_to_breach(INITIAL, INITIAL)
        losses_from_96k = dpr.get_consecutive_losses_to_breach(96_000, INITIAL)
        # Even though 96K is closer to breach, the proportional reduction
        # means you still need significant losses
        assert losses_from_100k > losses_from_96k
        assert losses_from_96k > 0


# ---------------------------------------------------------------------------
# Integration with PositionSizer
# ---------------------------------------------------------------------------


class TestPositionSizerIntegration:
    """End-to-end: DrawdownProportionalRisk → PositionSizer → final volume."""

    def test_normal_tier_matches_base_calculation(self):
        """No drawdown → proportional risk equals base → same volume as before."""
        dpr = DrawdownProportionalRisk()
        sizer = PositionSizer()

        risk = dpr.get_risk_pct(INITIAL, INITIAL)
        assert risk == BASE_RISK

        lot = sizer.calculate(equity=INITIAL, entry=1.10150, stop=1.10000, risk_pct=risk)
        # 1500 / (15 * 10) = 10.00
        assert lot == 10.00

    def test_cautious_tier_reduces_volume(self):
        """3% drawdown → 70% risk → smaller position."""
        dpr = DrawdownProportionalRisk()
        sizer = PositionSizer()

        equity = 97_000.0
        risk = dpr.get_risk_pct(equity, INITIAL)
        assert risk == BASE_RISK * 0.70

        lot = sizer.calculate(equity=equity, entry=1.10150, stop=1.10000, risk_pct=risk)
        # risk_dollars = 97000 * 0.0105 = 1018.50
        # volume = 1018.50 / (15 * 10) = 6.79
        assert lot == 6.79

    def test_defensive_tier_further_reduces(self):
        """5% drawdown → 50% risk."""
        dpr = DrawdownProportionalRisk()
        sizer = PositionSizer()

        equity = 95_000.0
        risk = dpr.get_risk_pct(equity, INITIAL)
        assert risk == BASE_RISK * 0.50

        lot = sizer.calculate(equity=equity, entry=1.10150, stop=1.10000, risk_pct=risk)
        # risk_dollars = 95000 * 0.0075 = 712.50
        # volume = 712.50 / (15 * 10) = 4.75
        assert lot == 4.75

    def test_survival_tier_significant_reduction(self):
        """7% drawdown → 30% risk."""
        dpr = DrawdownProportionalRisk()
        sizer = PositionSizer()

        equity = 93_000.0
        risk = dpr.get_risk_pct(equity, INITIAL)
        assert risk == BASE_RISK * 0.30

        lot = sizer.calculate(equity=equity, entry=1.10150, stop=1.10000, risk_pct=risk)
        # risk_dollars = 93000 * 0.0045 = 418.50
        # volume = 418.50 / 150 = 2.79
        assert lot == 2.79

    def test_emergency_tier_minimal_risk(self):
        """9% drawdown → 20% risk."""
        dpr = DrawdownProportionalRisk()
        sizer = PositionSizer()

        equity = 91_000.0
        risk = dpr.get_risk_pct(equity, INITIAL)
        assert risk == BASE_RISK * 0.20

        lot = sizer.calculate(equity=equity, entry=1.10150, stop=1.10000, risk_pct=risk)
        # risk_dollars = 91000 * 0.003 = 273.00
        # volume = 273.00 / 150 = 1.82
        assert lot == 1.82

    def test_halt_tier_returns_zero_risk(self):
        """10% drawdown → 0% risk → cannot calculate (ValueError from sizer)."""
        dpr = DrawdownProportionalRisk()
        sizer = PositionSizer()

        risk = dpr.get_risk_pct(90_000, INITIAL)
        assert risk == 0.0

        # The pre-trade gate should catch this before calling sizer.calculate()
        with pytest.raises(ValueError, match="risk_pct"):
            sizer.calculate(equity=90_000, entry=1.10150, stop=1.10000, risk_pct=0.0)

    def test_volume_decreases_monotonically(self):
        """As drawdown deepens, position size strictly decreases."""
        dpr = DrawdownProportionalRisk()
        sizer = PositionSizer()

        equities = [100_000, 99_000, 97_000, 95_000, 93_000, 91_000]
        volumes = []
        for eq in equities:
            risk = dpr.get_risk_pct(eq, INITIAL)
            if risk > 0:
                vol = sizer.calculate(equity=eq, entry=1.10150, stop=1.10000, risk_pct=risk)
                volumes.append(vol)

        # Each volume should be <= the previous one
        for i in range(1, len(volumes)):
            assert volumes[i] <= volumes[i - 1], (
                f"Volume increased from {volumes[i - 1]} to {volumes[i]} at equity {equities[i]}"
            )

    def test_proportional_risk_reduces_volume(self):
        """Full pipeline: proportional risk reduces position sizes at drawdown."""
        dpr = DrawdownProportionalRisk()
        sizer = PositionSizer()

        # At 6% total DD: equity=94K
        equity = 94_000.0
        risk = dpr.get_risk_pct(equity, INITIAL)
        assert risk == BASE_RISK * 0.30  # survival tier

        base_vol = sizer.calculate(equity=equity, entry=1.10150, stop=1.10000, risk_pct=risk)

        # Compare against full risk volume
        full_vol = sizer.calculate(equity=equity, entry=1.10150, stop=1.10000, risk_pct=BASE_RISK)

        # Proportional risk should significantly reduce the volume
        assert base_vol < full_vol
        assert base_vol < 3.0
