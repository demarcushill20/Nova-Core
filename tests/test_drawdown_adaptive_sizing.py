"""Tests for drawdown-adaptive position sizing (P0 critical priority).

This feature integrates the DrawdownScaler into the PreTradeGate to automatically
reduce position sizes as drawdown approaches FTMO limits, preventing account blowups.
"""

import pytest

from novatrade.config import NovaTradeCfg
from novatrade.models import AccountState, OrderRequest, OrderSide, OrderType
from novatrade.risk.pre_trade_gate import PreTradeGate


@pytest.fixture
def config():
    """Standard test configuration with FTMO 100K account baseline."""
    from novatrade.config import FtmoProfile

    return NovaTradeCfg(ftmo=FtmoProfile(account_size=100000))


@pytest.fixture
def gate(config):
    """Pre-trade gate with drawdown-adaptive scaling enabled."""
    return PreTradeGate(config)


@pytest.fixture
def base_request():
    """Standard order request for testing."""
    return OrderRequest(
        symbol="EURUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        volume=1.0,
        price=1.10000,
        stop_loss=1.09500,  # 50 pip stop
    )


@pytest.fixture
def healthy_account():
    """Account with healthy equity levels."""
    return AccountState(
        equity=100000.0,
        balance=100000.0,
        margin=0.0,
        free_margin=100000.0,
    )


@pytest.fixture
def stressed_account():
    """Account with significant drawdown (70% of 5% daily limit used)."""
    return AccountState(
        equity=96500.0,  # 3.5% drawdown = 70% of 5% limit
        balance=96500.0,
        margin=0.0,
        free_margin=96500.0,
    )


@pytest.fixture
def critical_account():
    """Account approaching FTMO limits (90% of daily limit used)."""
    return AccountState(
        equity=95500.0,  # 4.5% drawdown = 90% of 5% limit
        balance=95500.0,
        margin=0.0,
        free_margin=95500.0,
    )


class TestDrawdownAdaptiveIntegration:
    """Test integration of DrawdownScaler into PreTradeGate volume sizing."""

    def test_healthy_account_full_size(self, gate, base_request, healthy_account):
        """Healthy accounts should use full position sizes."""
        result = gate._check_volume_sizing(base_request, healthy_account)

        assert result.passed
        # Should validate against full calculated volume
        assert "OK" in result.detail or "conservative" in result.detail

    def test_stressed_account_reduced_size(self, gate, base_request, stressed_account):
        """Accounts with significant drawdown should use reduced position sizes."""
        from novatrade.models import OrderRequest, OrderSide, OrderType

        # With 1.5% base risk, stressed account calculates ~1.01 lots.
        # Use a larger request (2.0 lots) to exceed the calculated volume.
        oversized_request = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=2.0,
            price=1.10000,
            stop_loss=1.09500,
        )
        result = gate._check_volume_sizing(oversized_request, stressed_account)

        # Should fail validation because 2.0 lots exceeds scaled calculated volume (~1.01)
        assert not result.passed
        assert "over-sized" in result.detail

        # Test with a more conservative request that should pass
        conservative_request = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.50,  # Within expected scaled range
            price=1.10000,
            stop_loss=1.09500,
        )

        result_conservative = gate._check_volume_sizing(conservative_request, stressed_account)
        assert result_conservative.passed

    def test_critical_account_survival_mode(self, gate, base_request, critical_account):
        """Accounts approaching limits should use minimal position sizes."""
        result = gate._check_volume_sizing(base_request, critical_account)

        # Should fail validation due to heavy scaling down
        assert not result.passed
        assert "over-sized" in result.detail

        # Test with minimal request that should pass in survival mode
        from novatrade.models import OrderRequest, OrderSide, OrderType

        minimal_request = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.15,  # Very small request for survival mode (~0.18 calculated after proportional risk)
            price=1.10000,
            stop_loss=1.09500,
        )

        result_minimal = gate._check_volume_sizing(minimal_request, critical_account)
        assert result_minimal.passed

    def test_missing_price_skips_scaling(self, gate, healthy_account):
        """Requests without price/stop should skip scaling (informational pass)."""
        request = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=1.0,
            price=None,  # Missing price
            stop_loss=1.09500,
        )

        result = gate._check_volume_sizing(request, healthy_account)

        assert result.passed
        assert "skipped" in result.detail

    def test_zero_equity_skips_scaling(self, gate, base_request):
        """Zero equity accounts should skip scaling (defensive pass)."""
        zero_account = AccountState(
            equity=0.0,
            balance=0.0,
            margin=0.0,
            free_margin=0.0,
        )

        result = gate._check_volume_sizing(base_request, zero_account)

        assert result.passed
        assert "skipped" in result.detail


class TestLossStreakScaling:
    """Test consecutive loss tracking and additional scaling."""

    def test_record_loss_increases_count(self, gate):
        """Recording losses should increase consecutive loss count."""
        initial_count = gate.consecutive_losses
        gate.record_trade_loss()
        assert gate.consecutive_losses == initial_count + 1

    def test_record_win_resets_count(self, gate):
        """Recording wins should reset consecutive loss count (instant mode)."""
        # Accumulate some losses
        gate.record_trade_loss()
        gate.record_trade_loss()
        gate.record_trade_loss()
        assert gate.consecutive_losses == 3

        # Single win should reset
        gate.record_trade_win()
        assert gate.consecutive_losses == 0

    def test_daily_reset_clears_state(self, gate):
        """Daily reset should clear all scaling state."""
        # Build up some state
        gate.record_trade_loss()
        gate.record_trade_loss()

        # Reset
        gate.reset_daily_risk_scaling()

        # Should be cleared
        assert gate.consecutive_losses == 0
        assert gate.current_drawdown_scale == 1.0

    def test_multiple_losses_compound_scaling(self, gate, base_request, healthy_account):
        """Multiple consecutive losses should progressively reduce position sizes."""
        # Record several losses
        for _ in range(4):
            gate.record_trade_loss()

        # Should be in survival mode now (4+ losses = 0.25x scale)
        assert gate.consecutive_losses >= 4

        # The validation should fail because base_request asks for 1.0 lot
        # but the system calculates much smaller due to loss scaling
        result = gate._check_volume_sizing(base_request, healthy_account)

        # Should fail due to oversized request (1.0 vs ~0.38 calculated)
        assert not result.passed
        assert "over-sized" in result.detail

        # Now test with a smaller request that should pass
        from novatrade.models import OrderRequest, OrderSide, OrderType

        small_request = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.25,  # Much smaller request
            price=1.10000,
            stop_loss=1.09500,
        )

        result_small = gate._check_volume_sizing(small_request, healthy_account)
        # This should pass since we're requesting less than calculated
        assert result_small.passed


class TestDrawdownTierHysteresis:
    """Test hysteresis behavior in drawdown tier transitions."""

    def test_tier_entry_thresholds(self, gate):
        """Test tier entry at configured thresholds."""
        scaler = gate._drawdown_scaler

        # Should start at full scale
        assert scaler.current_dd_scale == 1.0

        # Cross 50% threshold → should drop to 0.75
        scale = scaler.drawdown_scale(0.51)
        assert scale == 0.75
        assert scaler.current_dd_scale == 0.75

    def test_tier_exit_hysteresis(self, gate):
        """Test that tier exits use lower thresholds (hysteresis)."""
        scaler = gate._drawdown_scaler

        # Enter elevated tier (50% → 0.75x)
        scaler.drawdown_scale(0.51)
        assert scaler.current_dd_scale == 0.75

        # Partial recovery (to 48%) should not restore full size yet
        scale = scaler.drawdown_scale(0.48)
        assert scale == 0.75  # Still in tier

        # Full recovery below exit threshold (45%) should restore
        scale = scaler.drawdown_scale(0.44)
        assert scale == 1.0

    def test_total_dd_scaling_independence(self, gate):
        """Test that total DD scaling works independently."""
        scaler = gate._drawdown_scaler

        # Test total DD scaling at various levels
        assert scaler.total_dd_scale(0.40) == 1.0  # <50% = full size
        assert scaler.total_dd_scale(0.60) == 0.75  # 50-70% = 3/4 size
        assert scaler.total_dd_scale(0.80) == 0.50  # 70-85% = 1/2 size
        assert scaler.total_dd_scale(0.90) == 0.10  # 85%+ = survival mode

    def test_combined_scaling_takes_minimum(self, gate):
        """Test that combined scaling uses the most restrictive factor."""
        scaler = gate._drawdown_scaler

        # Set up different scaling factors
        daily_factor = scaler.drawdown_scale(0.60)  # 0.75 (elevated)
        total_factor = scaler.total_dd_scale(0.80)  # 0.50 (critical)

        # Combined should take the minimum
        combined = scaler.combined_scale(
            dd_used_pct=0.60,
            total_dd_used_pct=0.80,
        )
        assert combined == min(daily_factor, total_factor, scaler.loss_streak_scale())


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_negative_equity_handling(self, gate, base_request):
        """Test handling of negative equity."""
        negative_account = AccountState(
            equity=-1000.0,
            balance=-1000.0,
            margin=0.0,
            free_margin=-1000.0,
        )

        result = gate._check_volume_sizing(base_request, negative_account)

        # Should still pass but skip sizing
        assert result.passed
        assert "skipped" in result.detail

    def test_invalid_stop_distance(self, gate, healthy_account):
        """Test handling when entry == stop (zero distance)."""
        invalid_request = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=1.0,
            price=1.10000,
            stop_loss=1.10000,  # Same as entry = zero distance
        )

        result = gate._check_volume_sizing(invalid_request, healthy_account)

        # Should gracefully handle the error
        assert result.passed
        assert "skipped" in result.detail

    def test_extreme_drawdown_levels(self, gate):
        """Test scaling behavior at extreme drawdown levels."""
        scaler = gate._drawdown_scaler

        # Test extreme values
        assert scaler.drawdown_scale(1.5) == 0.25  # 150% of limit = survival mode
        assert scaler.drawdown_scale(-0.1) == 1.0  # Negative DD = full size

    def test_concurrent_access_safety(self, gate):
        """Test that concurrent access to scaler state is handled safely."""
        # This is a basic test - full thread safety would need more sophisticated testing
        initial_state = gate.consecutive_losses

        # Multiple operations should maintain consistency
        gate.record_trade_loss()
        gate.record_trade_win()
        gate.record_trade_loss()

        # State should be predictable
        assert gate.consecutive_losses == initial_state + 1


# ---------------------------------------------------------------------------
# Integration benchmarks
# ---------------------------------------------------------------------------


try:
    import pytest_benchmark  # noqa: F401

    _HAS_BENCHMARK = True
except ImportError:
    _HAS_BENCHMARK = False


@pytest.mark.skipif(not _HAS_BENCHMARK, reason="pytest-benchmark not installed")
class TestPerformanceImpact:
    """Ensure drawdown-adaptive sizing doesn't significantly impact performance."""

    def test_sizing_calculation_performance(self, gate, base_request, healthy_account, benchmark):
        """Benchmark the volume sizing calculation performance."""

        def volume_sizing_check():
            return gate._check_volume_sizing(base_request, healthy_account)

        result = benchmark(volume_sizing_check)
        assert result.passed

    def test_scaler_performance_under_load(self, gate, benchmark):
        """Test DrawdownScaler performance with rapid scaling calculations."""
        scaler = gate._drawdown_scaler

        def rapid_scaling():
            for pct in [0.3, 0.6, 0.8, 0.4, 0.7]:
                scaler.drawdown_scale(pct)

        benchmark(rapid_scaling)


# ---------------------------------------------------------------------------
# Property-based testing
# ---------------------------------------------------------------------------


@pytest.mark.property
class TestPropertyBasedScaling:
    """Property-based tests for drawdown scaling invariants."""

    def test_scaling_never_exceeds_one(self, gate):
        """Scale factors should never exceed 1.0 (never increase base size)."""
        scaler = gate._drawdown_scaler

        # Test range of drawdown percentages
        for pct in [0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0, 1.5]:
            daily_scale = scaler.drawdown_scale(pct)
            total_scale = scaler.total_dd_scale(pct)
            combined = scaler.combined_scale(pct, pct)

            assert daily_scale <= 1.0
            assert total_scale <= 1.0
            assert combined <= 1.0

    def test_scaling_monotonic_decrease(self, gate):
        """Higher drawdown percentages should never result in higher scaling."""
        scaler = gate._drawdown_scaler

        prev_scale = 1.0
        for pct in [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]:
            current_scale = scaler.total_dd_scale(pct)  # Use total DD to avoid hysteresis
            assert current_scale <= prev_scale
            prev_scale = current_scale

    def test_volume_scaling_preserves_bounds(self, gate, base_request):
        """Scaled volumes should always respect min/max lot bounds."""
        scaler = gate._drawdown_scaler
        base_volume = 5.0
        min_lot = 0.01

        for dd_pct in [0.0, 0.3, 0.6, 0.9, 1.5]:
            scaled = scaler.scale_volume(base_volume, dd_pct, min_lot)
            assert scaled >= min_lot
            assert scaled <= base_volume  # Never increase


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


class TestRegression:
    """Regression tests for known issues or edge cases."""

    def test_state_persistence_across_requests(self, gate, base_request, healthy_account):
        """Ensure DrawdownScaler state persists across multiple requests."""
        # Record some losses
        gate.record_trade_loss()
        gate.record_trade_loss()
        initial_losses = gate.consecutive_losses

        # Multiple volume sizing calls shouldn't affect loss count
        gate._check_volume_sizing(base_request, healthy_account)
        gate._check_volume_sizing(base_request, healthy_account)

        assert gate.consecutive_losses == initial_losses

    def test_configuration_independence(self):
        """Ensure different gate instances have independent scaling state."""
        config1 = NovaTradeCfg()
        config2 = NovaTradeCfg()
        gate1 = PreTradeGate(config1)
        gate2 = PreTradeGate(config2)

        # Modify state on gate1
        gate1.record_trade_loss()
        gate1.record_trade_loss()

        # gate2 should be unaffected
        assert gate2.consecutive_losses == 0
        assert gate1.consecutive_losses == 2

    def test_backward_compatibility(self, gate, base_request, healthy_account):
        """Ensure existing volume sizing logic still works correctly."""
        # This should behave identically to pre-enhancement behavior for healthy accounts
        result = gate._check_volume_sizing(base_request, healthy_account)

        # Should pass validation with expected tolerances
        assert result.passed
        assert result.name == "volume_sizing"
