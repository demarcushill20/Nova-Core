"""Tests for novatrade.adapter.metaapi_provider — mocked MetaApi SDK wiring."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from novatrade.adapter.metaapi_provider import (
    MetaApiAdapter,
    _safe_error,
    _translate_account,
    _translate_candle,
    _translate_position,
    _translate_symbol_price,
    _translate_trade_response,
)
from novatrade.config import MetaApiConfig
from novatrade.models import (
    AccountMode,
    HealthState,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config():
    return MetaApiConfig(
        token="test-token",
        account_id="test-account-id",
        domain="test.example.com",
        region="london",
        application="NovaTrade-Test",
    )


@pytest.fixture
def adapter(config):
    return MetaApiAdapter(config)


def _make_mock_connection():
    """Create a mock RPC connection with all needed methods."""
    conn = AsyncMock()
    conn.get_account_information = AsyncMock(
        return_value={
            "balance": 10000.0,
            "equity": 10050.0,
            "margin": 200.0,
            "freeMargin": 9850.0,
            "currency": "USD",
            "leverage": 100,
            "server": "Demo-Server",
            "broker": "TestBroker",
            "type": "ACCOUNT_TRADE_MODE_DEMO",
        }
    )
    conn.get_positions = AsyncMock(
        return_value=[
            {
                "id": 12345,
                "type": "POSITION_TYPE_BUY",
                "symbol": "EURUSD",
                "volume": 0.1,
                "openPrice": 1.1000,
                "currentPrice": 1.1050,
                "unrealizedProfit": 50.0,
                "stopLoss": 1.0950,
                "takeProfit": 1.1100,
                "time": datetime(2026, 3, 15, 10, 30, tzinfo=timezone.utc),
                "comment": "test-order",
            },
        ]
    )
    conn.get_symbol_price = AsyncMock(
        return_value={
            "symbol": "EURUSD",
            "bid": 1.10500,
            "ask": 1.10520,
            "time": datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        }
    )
    conn.create_market_buy_order = AsyncMock(
        return_value={
            "numericCode": 10009,
            "stringCode": "TRADE_RETCODE_DONE",
            "message": "Request completed",
            "orderId": "99001",
            "positionId": "99001",
        }
    )
    conn.create_market_sell_order = AsyncMock(
        return_value={
            "numericCode": 10009,
            "stringCode": "TRADE_RETCODE_DONE",
            "message": "Request completed",
            "orderId": "99002",
        }
    )
    conn.create_limit_buy_order = AsyncMock(
        return_value={
            "numericCode": 10008,
            "stringCode": "TRADE_RETCODE_PLACED",
            "message": "Order placed",
            "orderId": "99003",
        }
    )
    conn.create_limit_sell_order = AsyncMock(
        return_value={
            "numericCode": 10008,
            "stringCode": "TRADE_RETCODE_PLACED",
            "message": "Order placed",
            "orderId": "99004",
        }
    )
    conn.create_stop_buy_order = AsyncMock(
        return_value={
            "numericCode": 10008,
            "stringCode": "TRADE_RETCODE_PLACED",
            "orderId": "99005",
        }
    )
    conn.create_stop_sell_order = AsyncMock(
        return_value={
            "numericCode": 10008,
            "stringCode": "TRADE_RETCODE_PLACED",
            "orderId": "99006",
        }
    )
    conn.modify_position = AsyncMock(
        return_value={
            "numericCode": 10009,
            "stringCode": "TRADE_RETCODE_DONE",
            "message": "Position modified",
            "positionId": "12345",
        }
    )
    conn.close_position = AsyncMock(
        return_value={
            "numericCode": 10009,
            "stringCode": "TRADE_RETCODE_DONE",
            "message": "Position closed",
            "positionId": "12345",
        }
    )
    conn.close_position_partially = AsyncMock(
        return_value={
            "numericCode": 10009,
            "stringCode": "TRADE_RETCODE_DONE",
            "message": "Position partially closed",
            "positionId": "12345",
        }
    )
    conn.connect = AsyncMock()
    conn.wait_synchronized = AsyncMock()
    conn.close = AsyncMock()
    return conn


def _wire_adapter(adapter_instance):
    """Wire a mock connection into the adapter as if connect() succeeded."""
    adapter_instance._connection = _make_mock_connection()
    adapter_instance._connected = True
    adapter_instance._account = MagicMock()
    adapter_instance._account.get_historical_candles = AsyncMock(
        return_value=[
            {
                "time": datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc),
                "open": 1.1000,
                "high": 1.1050,
                "low": 1.0980,
                "close": 1.1040,
                "tickVolume": 500,
                "volume": 1200,
            },
            {
                "time": datetime(2026, 3, 15, 11, 0, tzinfo=timezone.utc),
                "open": 1.1040,
                "high": 1.1060,
                "low": 1.1020,
                "close": 1.1055,
                "tickVolume": 420,
                "volume": 980,
            },
        ]
    )
    return adapter_instance


# ---------------------------------------------------------------------------
# Translation unit tests
# ---------------------------------------------------------------------------


class TestTranslateAccount:
    def test_demo_account(self):
        info = {
            "balance": 10000.0,
            "equity": 10050.0,
            "margin": 200.0,
            "freeMargin": 9850.0,
            "currency": "USD",
            "leverage": 100,
            "server": "Demo",
            "broker": "Broker",
            "type": "ACCOUNT_TRADE_MODE_DEMO",
        }
        acct = _translate_account(info)
        assert acct.balance == 10000.0
        assert acct.mode == AccountMode.DEMO
        assert acct.leverage == 100

    def test_contest_maps_to_challenge(self):
        info = {"balance": 0, "equity": 0, "type": "ACCOUNT_TRADE_MODE_CONTEST"}
        acct = _translate_account(info)
        assert acct.mode == AccountMode.CHALLENGE

    def test_missing_fields_use_defaults(self):
        acct = _translate_account({})
        assert acct.balance == 0.0
        assert acct.currency == "USD"


class TestTranslatePosition:
    def test_buy_position(self):
        raw = {
            "id": 42,
            "type": "POSITION_TYPE_BUY",
            "symbol": "GBPUSD",
            "volume": 0.5,
            "openPrice": 1.2500,
            "currentPrice": 1.2550,
            "unrealizedProfit": 250.0,
            "stopLoss": 1.2400,
            "takeProfit": 1.2600,
            "time": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "comment": "test",
        }
        pos = _translate_position(raw)
        assert pos.position_id == "42"
        assert pos.side == OrderSide.BUY
        assert pos.unrealized_pnl == 250.0

    def test_sell_position(self):
        raw = {
            "id": 99,
            "type": "POSITION_TYPE_SELL",
            "symbol": "USDJPY",
            "volume": 1.0,
            "openPrice": 150.0,
            "currentPrice": 149.5,
            "profit": 500.0,
        }
        pos = _translate_position(raw)
        assert pos.side == OrderSide.SELL
        assert pos.unrealized_pnl == 500.0


class TestTranslateSymbolPrice:
    def test_basic(self):
        raw = {"symbol": "EURUSD", "bid": 1.1050, "ask": 1.1052, "time": datetime(2026, 3, 15, tzinfo=timezone.utc)}
        sp = _translate_symbol_price(raw)
        assert sp.symbol == "EURUSD"
        assert sp.bid == 1.1050
        assert sp.spread == pytest.approx(0.0002)


class TestTranslateCandle:
    def test_basic(self):
        raw = {
            "time": datetime(2026, 3, 15, tzinfo=timezone.utc),
            "open": 1.1,
            "high": 1.12,
            "low": 1.09,
            "close": 1.11,
            "tickVolume": 100,
        }
        c = _translate_candle(raw, "EURUSD", "H1")
        assert c.symbol == "EURUSD"
        assert c.timeframe == "H1"
        assert c.volume == 100


class TestTranslateTradeResponse:
    def test_success_done(self):
        raw = {"numericCode": 10009, "stringCode": "TRADE_RETCODE_DONE", "message": "ok", "orderId": "123"}
        res = _translate_trade_response(raw)
        assert res.ok
        assert res.status == OrderStatus.FILLED
        assert res.order_id == "123"

    def test_success_placed(self):
        raw = {"numericCode": 10008, "stringCode": "TRADE_RETCODE_PLACED", "orderId": "456"}
        res = _translate_trade_response(raw)
        assert res.ok
        assert res.status == OrderStatus.FILLED

    def test_partial_fill(self):
        raw = {"numericCode": 10010, "stringCode": "TRADE_RETCODE_DONE_PARTIAL", "orderId": "789"}
        res = _translate_trade_response(raw)
        assert res.ok
        assert res.status == OrderStatus.PARTIALLY_FILLED

    def test_reject(self):
        raw = {"numericCode": 10016, "stringCode": "TRADE_RETCODE_REJECT", "message": "no money"}
        res = _translate_trade_response(raw)
        assert not res.ok
        assert res.status == OrderStatus.REJECTED
        assert "no money" in res.error

    def test_unknown_code(self):
        raw = {"numericCode": 99999, "stringCode": "UNKNOWN", "message": "???"}
        res = _translate_trade_response(raw)
        assert not res.ok


# ---------------------------------------------------------------------------
# Adapter method tests (mocked SDK)
# ---------------------------------------------------------------------------


class TestAdapterNotConnected:
    def test_operations_fail_before_connect(self, adapter):
        with pytest.raises(ConnectionError, match="not connected"):
            asyncio.new_event_loop().run_until_complete(adapter.get_account())

    def test_health_check_when_disconnected(self, adapter):
        h = asyncio.new_event_loop().run_until_complete(adapter.health_check())
        assert h.state == HealthState.DOWN
        assert not h.connected


class TestAdapterGetAccount:
    def test_returns_account_state(self, adapter):
        _wire_adapter(adapter)
        acct = asyncio.new_event_loop().run_until_complete(adapter.get_account())
        assert acct.balance == 10000.0
        assert acct.equity == 10050.0
        assert acct.mode == AccountMode.DEMO


class TestAdapterGetPositions:
    def test_returns_positions(self, adapter):
        _wire_adapter(adapter)
        positions = asyncio.new_event_loop().run_until_complete(adapter.get_positions())
        assert len(positions) == 1
        assert positions[0].symbol == "EURUSD"
        assert positions[0].side == OrderSide.BUY

    def test_empty_positions(self, adapter):
        _wire_adapter(adapter)
        adapter._connection.get_positions = AsyncMock(return_value=[])
        positions = asyncio.new_event_loop().run_until_complete(adapter.get_positions())
        assert positions == []


class TestAdapterGetSymbolPrice:
    def test_returns_price(self, adapter):
        _wire_adapter(adapter)
        sp = asyncio.new_event_loop().run_until_complete(
            adapter.get_symbol_price("EURUSD"),
        )
        assert sp.symbol == "EURUSD"
        assert sp.bid == 1.10500


class TestAdapterGetCandles:
    def test_returns_candles(self, adapter):
        _wire_adapter(adapter)
        candles = asyncio.new_event_loop().run_until_complete(
            adapter.get_candles("EURUSD", "H1", count=2),
        )
        assert len(candles) == 2
        assert candles[0].symbol == "EURUSD"
        assert candles[1].close == 1.1055

    def test_timeframe_translation(self, adapter):
        _wire_adapter(adapter)
        asyncio.new_event_loop().run_until_complete(
            adapter.get_candles("EURUSD", "M15", count=10),
        )
        adapter._account.get_historical_candles.assert_called_once_with(
            "EURUSD",
            "15m",
            limit=10,
        )


class TestAdapterPlaceOrder:
    def test_market_buy(self, adapter):
        _wire_adapter(adapter)
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.1,
        )
        result = asyncio.new_event_loop().run_until_complete(adapter.place_order(req))
        assert result.ok
        assert result.order_id == "99001"
        adapter._connection.create_market_buy_order.assert_called_once()

    def test_market_sell(self, adapter):
        _wire_adapter(adapter)
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            volume=0.05,
        )
        result = asyncio.new_event_loop().run_until_complete(adapter.place_order(req))
        assert result.ok
        adapter._connection.create_market_sell_order.assert_called_once()

    def test_limit_buy(self, adapter):
        _wire_adapter(adapter)
        req = OrderRequest(
            symbol="GBPUSD",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            volume=0.2,
            price=1.2500,
        )
        result = asyncio.new_event_loop().run_until_complete(adapter.place_order(req))
        assert result.ok
        adapter._connection.create_limit_buy_order.assert_called_once()

    def test_limit_sell(self, adapter):
        _wire_adapter(adapter)
        req = OrderRequest(
            symbol="GBPUSD",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            volume=0.2,
            price=1.2600,
        )
        result = asyncio.new_event_loop().run_until_complete(adapter.place_order(req))
        assert result.ok

    def test_stop_buy(self, adapter):
        _wire_adapter(adapter)
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.STOP,
            volume=0.1,
            price=1.1100,
        )
        result = asyncio.new_event_loop().run_until_complete(adapter.place_order(req))
        assert result.ok

    def test_idempotency_key_in_comment(self, adapter):
        _wire_adapter(adapter)
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.1,
            idempotency_key="abc-123",
        )
        asyncio.new_event_loop().run_until_complete(adapter.place_order(req))
        call_args = adapter._connection.create_market_buy_order.call_args
        # Options dict is the last positional arg passed to the SDK method
        all_args = call_args[0]
        options = next((a for a in all_args if isinstance(a, dict)), None)
        assert options is not None, f"no dict arg found in call_args: {all_args}"
        assert "abc-123" in options["comment"]

    def test_order_failure_returns_error(self, adapter):
        _wire_adapter(adapter)
        adapter._connection.create_market_buy_order = AsyncMock(
            side_effect=Exception("insufficient margin"),
        )
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=100.0,
        )
        result = asyncio.new_event_loop().run_until_complete(adapter.place_order(req))
        assert not result.ok
        assert "insufficient margin" in result.error


class TestAdapterModifyOrder:
    def test_modify_sl_tp(self, adapter):
        _wire_adapter(adapter)
        result = asyncio.new_event_loop().run_until_complete(
            adapter.modify_order("12345", stop_loss=1.0900, take_profit=1.1200),
        )
        assert result.ok
        adapter._connection.modify_position.assert_called_once_with(
            "12345",
            stop_loss=1.0900,
            take_profit=1.1200,
        )


class TestAdapterClosePosition:
    def test_full_close(self, adapter):
        _wire_adapter(adapter)
        result = asyncio.new_event_loop().run_until_complete(
            adapter.close_position("12345"),
        )
        assert result.ok
        adapter._connection.close_position.assert_called_once_with("12345")

    def test_partial_close(self, adapter):
        _wire_adapter(adapter)
        result = asyncio.new_event_loop().run_until_complete(
            adapter.close_position("12345", volume=0.05),
        )
        assert result.ok
        adapter._connection.close_position_partially.assert_called_once_with("12345", 0.05)


class TestAdapterHealthCheck:
    def test_ok_when_connected(self, adapter):
        _wire_adapter(adapter)
        h = asyncio.new_event_loop().run_until_complete(adapter.health_check())
        assert h.state == HealthState.OK
        assert h.connected
        assert h.latency_ms is not None

    def test_down_on_failure(self, adapter):
        _wire_adapter(adapter)
        adapter._connection.get_account_information = AsyncMock(
            side_effect=Exception("timeout"),
        )
        h = asyncio.new_event_loop().run_until_complete(adapter.health_check())
        assert h.state == HealthState.DOWN
        assert not adapter._connected


class TestAdapterDisconnect:
    def test_disconnect_clears_state(self, adapter):
        _wire_adapter(adapter)
        asyncio.new_event_loop().run_until_complete(adapter.disconnect())
        assert not adapter._connected
        assert adapter._connection is None
        assert adapter._account is None


# ---------------------------------------------------------------------------
# Safe error formatting
# ---------------------------------------------------------------------------


class TestSafeError:
    def test_plain_message(self):
        assert _safe_error(Exception("something broke")) == "something broke"

    def test_redacts_token(self):
        msg = _safe_error(Exception("url?token=sk-abc123-secret"))
        assert "***REDACTED***" in msg
        assert "sk-abc123" not in msg

    def test_redacts_bearer(self):
        msg = _safe_error(Exception("Bearer eyJ0eXAi..."))
        assert "***REDACTED***" in msg
