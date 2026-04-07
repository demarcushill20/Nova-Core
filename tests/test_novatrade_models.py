"""Tests for novatrade.models — domain model validation and construction."""

import pytest

from novatrade.models import (
    AccountMode,
    AccountState,
    Candle,
    HealthState,
    HealthStatus,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    RiskDecision,
    RiskVerdict,
    SymbolPrice,
)

# ---------------------------------------------------------------------------
# OrderRequest
# ---------------------------------------------------------------------------


class TestOrderRequest:
    def test_valid_market_order(self):
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.01,
        )
        assert req.symbol == "EURUSD"
        assert req.volume == 0.01
        assert req.price is None  # not needed for market

    def test_valid_limit_order(self):
        req = OrderRequest(
            symbol="GBPUSD",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            volume=0.5,
            price=1.2500,
            stop_loss=1.2600,
            take_profit=1.2400,
        )
        assert req.price == 1.2500
        assert req.stop_loss == 1.2600

    def test_zero_volume_rejected(self):
        with pytest.raises(ValueError, match="volume must be positive"):
            OrderRequest(
                symbol="EURUSD",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=0,
            )

    def test_negative_volume_rejected(self):
        with pytest.raises(ValueError, match="volume must be positive"):
            OrderRequest(
                symbol="EURUSD",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=-2.0,
            )

    def test_auto_size_sentinel_accepted(self):
        from novatrade.models import VOLUME_AUTO_SIZE

        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=VOLUME_AUTO_SIZE,
        )
        assert req.volume == VOLUME_AUTO_SIZE

    def test_limit_without_price_rejected(self):
        with pytest.raises(ValueError, match="LIMIT orders require a price"):
            OrderRequest(
                symbol="EURUSD",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                volume=0.1,
            )

    def test_stop_without_price_rejected(self):
        with pytest.raises(ValueError, match="STOP orders require a price"):
            OrderRequest(
                symbol="EURUSD",
                side=OrderSide.SELL,
                order_type=OrderType.STOP,
                volume=0.1,
            )

    def test_idempotency_key(self):
        req = OrderRequest(
            symbol="USDJPY",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.1,
            idempotency_key="abc-123",
        )
        assert req.idempotency_key == "abc-123"

    def test_strategy_metadata(self):
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.01,
            strategy_id="ema-cross-v1",
            strategy_version="1.2.0",
        )
        assert req.strategy_id == "ema-cross-v1"
        assert req.strategy_version == "1.2.0"


# ---------------------------------------------------------------------------
# OrderResult
# ---------------------------------------------------------------------------


class TestOrderResult:
    def test_success_result(self):
        res = OrderResult(ok=True, order_id="12345", status=OrderStatus.FILLED, fill_price=1.1050, filled_volume=0.1)
        assert res.ok
        assert res.fill_price == 1.1050
        assert res.timestamp > 0

    def test_failure_result(self):
        res = OrderResult(ok=False, error="insufficient margin")
        assert not res.ok
        assert res.error == "insufficient margin"
        assert res.order_id == ""


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------


class TestPosition:
    def test_basic_construction(self):
        pos = Position(
            position_id="P001",
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=0.1,
            open_price=1.1000,
            current_price=1.1050,
            unrealized_pnl=50.0,
        )
        assert pos.unrealized_pnl == 50.0
        assert pos.strategy_id == ""


# ---------------------------------------------------------------------------
# AccountState
# ---------------------------------------------------------------------------


class TestAccountState:
    def test_defaults(self):
        acct = AccountState(balance=10000.0, equity=10050.0)
        assert acct.currency == "USD"
        assert acct.leverage == 100
        assert acct.mode == AccountMode.DEMO
        assert acct.timestamp > 0

    def test_challenge_mode(self):
        acct = AccountState(
            balance=100000.0,
            equity=99000.0,
            mode=AccountMode.CHALLENGE,
        )
        assert acct.mode == AccountMode.CHALLENGE


# ---------------------------------------------------------------------------
# HealthStatus
# ---------------------------------------------------------------------------


class TestHealthStatus:
    def test_ok_state(self):
        h = HealthStatus(state=HealthState.OK, connected=True, latency_ms=12.5)
        assert h.connected
        assert h.latency_ms == 12.5

    def test_down_state(self):
        h = HealthStatus(state=HealthState.DOWN, message="connection refused")
        assert not h.connected
        assert "refused" in h.message


# ---------------------------------------------------------------------------
# RiskDecision
# ---------------------------------------------------------------------------


class TestRiskDecision:
    def test_allow(self):
        d = RiskDecision(verdict=RiskVerdict.ALLOW)
        assert d.verdict == RiskVerdict.ALLOW

    def test_deny_with_reason(self):
        d = RiskDecision(
            verdict=RiskVerdict.DENY,
            reason="daily drawdown limit reached",
            rule="max_daily_drawdown",
        )
        assert d.verdict == RiskVerdict.DENY
        assert d.rule == "max_daily_drawdown"


# ---------------------------------------------------------------------------
# SymbolPrice
# ---------------------------------------------------------------------------


class TestSymbolPrice:
    def test_auto_spread(self):
        sp = SymbolPrice(symbol="EURUSD", bid=1.10000, ask=1.10020)
        assert sp.spread == pytest.approx(0.0002)

    def test_explicit_spread(self):
        sp = SymbolPrice(symbol="EURUSD", bid=1.10000, ask=1.10020, spread=0.0003)
        assert sp.spread == 0.0003  # explicit overrides auto


# ---------------------------------------------------------------------------
# Candle
# ---------------------------------------------------------------------------


class TestCandle:
    def test_basic(self):
        c = Candle(
            timestamp=1700000000,
            open=1.1,
            high=1.12,
            low=1.09,
            close=1.11,
            volume=1000,
            symbol="EURUSD",
            timeframe="H1",
        )
        assert c.close == 1.11
        assert c.timeframe == "H1"


# ---------------------------------------------------------------------------
# Enum coverage
# ---------------------------------------------------------------------------


class TestEnums:
    def test_order_side_values(self):
        assert OrderSide.BUY.value == "BUY"
        assert OrderSide.SELL.value == "SELL"

    def test_order_type_values(self):
        assert set(t.value for t in OrderType) == {
            "MARKET",
            "LIMIT",
            "STOP",
            "STOP_LIMIT",
        }

    def test_order_status_values(self):
        assert OrderStatus.FILLED.value == "FILLED"
        assert OrderStatus.REJECTED.value == "REJECTED"

    def test_account_mode_values(self):
        assert AccountMode.FUNDED_PRESERVATION.value == "FUNDED_PRESERVATION"
