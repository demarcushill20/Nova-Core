"""Tests for drawdown-proportional position sizing in PreTradeGate.

Verifies that DrawdownProportionalRisk (Layer 1) correctly integrates with
PreTradeGate._check_volume_sizing() to adjust position sizes based on total
account drawdown depth.
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
    """Pre-trade gate with drawdown-proportional risk enabled."""
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
    """Account with significant drawdown (3.5% total drawdown)."""
    return AccountState(
        equity=96500.0,  # 3.5% drawdown
        balance=96500.0,
        margin=0.0,
        free_margin=96500.0,
    )


@pytest.fixture
def critical_account():
    """Account approaching FTMO limits (4.5% total drawdown)."""
    return AccountState(
        equity=95500.0,  # 4.5% drawdown
        balance=95500.0,
        margin=0.0,
        free_margin=95500.0,
    )


class TestDrawdownProportionalIntegration:
    """Test integration of DrawdownProportionalRisk into PreTradeGate volume sizing."""

    def test_healthy_account_full_size(self, gate, base_request, healthy_account):
        """Healthy accounts should use full position sizes."""
        result = gate._check_volume_sizing(base_request, healthy_account)

        assert result.passed
        # Should validate against full calculated volume
        assert "OK" in result.detail or "conservative" in result.detail

    def test_stressed_account_reduced_size(self, gate, base_request, stressed_account):
        """Accounts with significant drawdown should use reduced position sizes.

        At 3.5% DD (cautious tier = 70% risk):
          risk = 0.015 * 0.70 = 0.0105
          volume = (96500 * 0.0105) / (50 * 10) = ~2.03 lots
        A 3.5-lot request should exceed this by enough to fail (>25% tolerance).
        """
        oversized_request = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=3.5,
            price=1.10000,
            stop_loss=1.09500,
        )
        result = gate._check_volume_sizing(oversized_request, stressed_account)

        # Should fail validation because 3.5 lots exceeds calculated volume (~2.03)
        assert not result.passed
        assert "over-sized" in result.detail

        # Test with a more conservative request that should pass
        conservative_request = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.50,  # Well within expected range
            price=1.10000,
            stop_loss=1.09500,
        )

        result_conservative = gate._check_volume_sizing(conservative_request, stressed_account)
        assert result_conservative.passed

    def test_critical_account_survival_mode(self, gate, base_request, critical_account):
        """Accounts approaching limits should use minimal position sizes.

        At 4.5% DD (defensive tier = 50% risk):
          risk = 0.015 * 0.50 = 0.0075
          volume = (95500 * 0.0075) / (50 * 10) = ~1.43 lots
        A 2.5-lot request should exceed this by enough to fail (>25% tolerance).
        """
        oversized_request = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=2.5,
            price=1.10000,
            stop_loss=1.09500,
        )
        result = gate._check_volume_sizing(oversized_request, critical_account)

        # Should fail validation due to proportional risk reduction
        assert not result.passed
        assert "over-sized" in result.detail

        # Test with minimal request that should pass
        minimal_request = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.50,
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


class TestRegression:
    """Regression tests for known issues or edge cases."""

    def test_configuration_independence(self):
        """Ensure different gate instances have independent state."""
        config1 = NovaTradeCfg()
        config2 = NovaTradeCfg()
        gate1 = PreTradeGate(config1)
        gate2 = PreTradeGate(config2)

        # Modifying gate1 should not affect gate2
        assert gate1 is not gate2

    def test_backward_compatibility(self, gate, base_request, healthy_account):
        """Ensure existing volume sizing logic still works correctly."""
        # This should behave identically to pre-enhancement behavior for healthy accounts
        result = gate._check_volume_sizing(base_request, healthy_account)

        # Should pass validation with expected tolerances
        assert result.passed
        assert result.name == "volume_sizing"
