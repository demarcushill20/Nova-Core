"""Tests for novatrade.adapter.metaapi_provider — mocked MetaApi SDK wiring.

Covers:
- Translation functions (account, position, candle, symbol price, trade response)
- Adapter lifecycle (connect, disconnect, health check)
- All order types (market, limit, stop, stop-limit)
- Execution gap fixes:
  - Retry with exponential backoff on ALL operations (place, modify, close, cancel)
  - Circuit breaker integration
  - Auto-reconnect on connection failure
  - Slippage parameter support
  - Post-order verification polling
"""

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
        retry_max_attempts=3,
        circuit_breaker_threshold=5,
        circuit_breaker_reset_seconds=60.0,
        reconnect_max_attempts=2,
        reconnect_backoff_base=0.01,  # fast for tests
        order_verify_timeout=0.3,  # fast for tests
        order_verify_interval=0.05,
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
    conn.cancel_order = AsyncMock(
        return_value={
            "numericCode": 10009,
            "stringCode": "TRADE_RETCODE_DONE",
            "message": "Order cancelled",
            "orderId": "55001",
        }
    )
    conn.get_orders = AsyncMock(return_value=[])
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
        # Mock connect() so auto-reconnect fails fast without hitting real MetaApi SDK
        async def _fake_connect():
            from novatrade.adapter.metaapi_provider import HealthState, HealthStatus

            adapter._connected = False
            return HealthStatus(state=HealthState.DOWN, connected=False, latency_ms=0, message="test: no broker")

        adapter.connect = _fake_connect
        with pytest.raises(ConnectionError, match="not connected"):
            asyncio.new_event_loop().run_until_complete(adapter.get_account())

    def test_health_check_when_disconnected(self, adapter):
        # No token/account_id set means no reconnect attempt
        adapter._config.token = ""
        adapter._config.account_id = ""
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
        # Mock positions to contain the order for verification
        adapter._connection.get_positions = AsyncMock(
            return_value=[
                {"id": "99001", "type": "POSITION_TYPE_BUY", "symbol": "EURUSD", "volume": 0.1, "openPrice": 1.1002}
            ]
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
        adapter._connection.get_positions = AsyncMock(
            return_value=[
                {"id": "99002", "type": "POSITION_TYPE_SELL", "symbol": "EURUSD", "volume": 0.05, "openPrice": 1.1000}
            ]
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
        adapter._connection.get_positions = AsyncMock(
            return_value=[
                {"id": "99001", "type": "POSITION_TYPE_BUY", "symbol": "EURUSD", "volume": 0.1, "openPrice": 1.1002}
            ]
        )
        asyncio.new_event_loop().run_until_complete(adapter.place_order(req))
        call_args = adapter._connection.create_market_buy_order.call_args
        # Options dict is the last positional arg passed to the SDK method
        all_args = call_args[0]
        options = next((a for a in all_args if isinstance(a, dict)), None)
        assert options is not None, f"no dict arg found in call_args: {all_args}"
        assert options.get("clientId") == "abc-123"

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

    def test_order_retry_on_transient_failure(self, adapter):
        _wire_adapter(adapter)
        # Simulate transient failure on first 2 attempts, success on 3rd
        call_count = 0

        async def failing_order_call(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise Exception("connection timeout")  # noqa: TRY002
            return {"orderId": "retry-123", "description": "TRADE_RETCODE_DONE", "numericCode": 10009}

        adapter._connection.create_market_buy_order = AsyncMock(side_effect=failing_order_call)
        # For verification: order won't be found in positions (LIMIT-like result)
        adapter._connection.get_positions = AsyncMock(
            return_value=[
                {"id": "retry-123", "type": "POSITION_TYPE_BUY", "symbol": "EURUSD", "volume": 0.1, "openPrice": 1.1000}
            ]
        )

        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.1,
        )
        result = asyncio.new_event_loop().run_until_complete(adapter.place_order(req))

        # Should succeed on 3rd attempt
        assert result.ok
        assert result.order_id == "retry-123"
        assert call_count == 3  # Verify it retried twice before succeeding

    def test_order_retry_exhausted_returns_error(self, adapter):
        _wire_adapter(adapter)
        # Simulate persistent failure on all attempts
        adapter._connection.create_market_buy_order = AsyncMock(
            side_effect=Exception("persistent network error"),
        )
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.1,
        )
        result = asyncio.new_event_loop().run_until_complete(adapter.place_order(req))

        # Should fail after all retries exhausted
        assert not result.ok
        assert "Failed after 4 attempts" in result.error
        assert "persistent network error" in result.error
        # Verify it tried 4 times (1 initial + 3 retries)
        assert adapter._connection.create_market_buy_order.call_count == 4


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

    def test_modify_retry_on_failure(self, adapter):
        """Gap 1 fix: modify_order now has retry logic."""
        _wire_adapter(adapter)
        call_count = 0

        async def failing_modify(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise Exception("temporary error")  # noqa: TRY002
            return {
                "numericCode": 10009,
                "stringCode": "TRADE_RETCODE_DONE",
                "positionId": "12345",
            }

        adapter._connection.modify_position = AsyncMock(side_effect=failing_modify)
        result = asyncio.new_event_loop().run_until_complete(
            adapter.modify_order("12345", stop_loss=1.0900),
        )
        assert result.ok
        assert call_count == 3

    def test_modify_retry_exhausted(self, adapter):
        """Gap 1 fix: modify_order returns error after exhausting retries."""
        _wire_adapter(adapter)
        adapter._connection.modify_position = AsyncMock(
            side_effect=Exception("persistent error"),
        )
        result = asyncio.new_event_loop().run_until_complete(
            adapter.modify_order("12345", stop_loss=1.0900),
        )
        assert not result.ok
        assert "Failed after 4 attempts" in result.error
        assert adapter._connection.modify_position.call_count == 4


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

    def test_close_retry_on_failure(self, adapter):
        """Gap 1 fix: close_position now has retry logic."""
        _wire_adapter(adapter)
        call_count = 0

        async def failing_close(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise Exception("connection timeout")  # noqa: TRY002
            return {
                "numericCode": 10009,
                "stringCode": "TRADE_RETCODE_DONE",
                "positionId": "12345",
            }

        adapter._connection.close_position = AsyncMock(side_effect=failing_close)
        result = asyncio.new_event_loop().run_until_complete(
            adapter.close_position("12345"),
        )
        assert result.ok
        assert call_count == 2


class TestAdapterCancelOrder:
    def test_cancel_order(self, adapter):
        _wire_adapter(adapter)
        result = asyncio.new_event_loop().run_until_complete(
            adapter.cancel_order("55001"),
        )
        assert result.ok

    def test_cancel_retry_on_failure(self, adapter):
        """Gap 1 fix: cancel_order now has retry logic."""
        _wire_adapter(adapter)
        call_count = 0

        async def failing_cancel(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise Exception("network error")  # noqa: TRY002
            return {
                "numericCode": 10009,
                "stringCode": "TRADE_RETCODE_DONE",
                "orderId": "55001",
            }

        adapter._connection.cancel_order = AsyncMock(side_effect=failing_cancel)
        result = asyncio.new_event_loop().run_until_complete(
            adapter.cancel_order("55001"),
        )
        assert result.ok
        assert call_count == 2


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
# Execution Gap Fix Tests
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    """Gap 3 fix: Circuit breaker integration tests."""

    def test_circuit_breaker_exists(self, adapter):
        """Adapter has a circuit breaker instance."""
        assert adapter.breaker is not None
        assert adapter.breaker.name == "metaapi_broker"

    def test_circuit_breaker_config(self, adapter):
        """Breaker uses config values."""
        assert adapter.breaker.failure_threshold == 5
        assert adapter.breaker.reset_timeout_seconds == 60.0

    def test_circuit_breaker_trips_after_failures(self, adapter):
        """Breaker opens after enough failures, blocking subsequent calls."""
        _wire_adapter(adapter)
        adapter._connection.modify_position = AsyncMock(
            side_effect=Exception("error"),
        )
        adapter._config.retry_max_attempts = 0  # no retries for this test
        adapter._config.circuit_breaker_threshold = 3

        # Reset breaker with lower threshold
        from agents.circuit_breakers import SimpleCircuitBreaker

        adapter._breaker = SimpleCircuitBreaker(
            name="metaapi_broker",
            failure_threshold=3,
            reset_timeout_seconds=60.0,
            redis_client=None,
        )

        # Exhaust the circuit breaker
        for i in range(4):
            asyncio.new_event_loop().run_until_complete(
                adapter.modify_order(f"order-{i}", stop_loss=1.0),
            )

        # Next call should be rejected by circuit breaker
        result = asyncio.new_event_loop().run_until_complete(
            adapter.modify_order("order-blocked", stop_loss=1.0),
        )
        assert not result.ok
        assert "Circuit breaker" in result.error

    def test_circuit_breaker_records_success(self, adapter):
        """Successful operations reset the circuit breaker."""
        _wire_adapter(adapter)
        result = asyncio.new_event_loop().run_until_complete(
            adapter.modify_order("12345", stop_loss=1.0900),
        )
        assert result.ok
        # Breaker should still be closed
        assert adapter.breaker.state == "CLOSED"


class TestAutoReconnect:
    """Gap 4 fix: Auto-reconnect tests."""

    def test_reconnect_on_disconnected_health_check(self, adapter):
        """Health check triggers auto-reconnect when disconnected."""
        adapter._connected = False
        adapter._connection = None

        # Mock connect to succeed
        async def mock_connect():
            adapter._connected = True
            adapter._connection = _make_mock_connection()
            return MagicMock(connected=True, state=HealthState.OK)

        adapter.connect = AsyncMock(side_effect=mock_connect)

        h = asyncio.new_event_loop().run_until_complete(adapter.health_check())
        assert h.connected
        assert "reconnected" in h.message

    def test_reconnect_exhausted(self, adapter):
        """Auto-reconnect gives up after max attempts."""
        adapter._connected = False
        adapter._connection = None

        # Mock connect to always fail
        adapter.connect = AsyncMock(return_value=MagicMock(connected=False))

        h = asyncio.new_event_loop().run_until_complete(adapter.health_check())
        assert not h.connected
        assert adapter.connect.call_count == adapter._config.reconnect_max_attempts

    def test_reconnect_on_connection_error_during_operation(self, adapter):
        """Operations trigger reconnect on ConnectionError."""
        _wire_adapter(adapter)
        call_count = 0

        async def connection_then_success(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("lost connection")
            return {
                "numericCode": 10009,
                "stringCode": "TRADE_RETCODE_DONE",
                "positionId": "12345",
            }

        adapter._connection.modify_position = AsyncMock(side_effect=connection_then_success)

        # Mock reconnect to succeed and restore connection
        original_conn = adapter._connection

        async def mock_reconnect():
            adapter._connected = True
            adapter._connection = original_conn
            return True

        adapter._auto_reconnect = AsyncMock(side_effect=mock_reconnect)

        asyncio.new_event_loop().run_until_complete(
            adapter.modify_order("12345", stop_loss=1.0900),
        )
        # Should have attempted reconnect
        adapter._auto_reconnect.assert_called()

    def test_no_concurrent_reconnect(self, adapter):
        """Prevents concurrent reconnect attempts."""
        adapter._reconnecting = True
        result = asyncio.new_event_loop().run_until_complete(adapter._auto_reconnect())
        assert result is False


class TestSlippageControl:
    """Gap 5 fix: Slippage parameter tests."""

    def test_slippage_from_order_request(self, adapter):
        """Per-order slippage is passed to MetaApi options."""
        _wire_adapter(adapter)
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.1,
            max_slippage_pips=2.5,
        )
        adapter._connection.get_positions = AsyncMock(
            return_value=[
                {"id": "99001", "type": "POSITION_TYPE_BUY", "symbol": "EURUSD", "volume": 0.1, "openPrice": 1.1000}
            ]
        )
        asyncio.new_event_loop().run_until_complete(adapter.place_order(req))

        call_args = adapter._connection.create_market_buy_order.call_args[0]
        options = next((a for a in call_args if isinstance(a, dict)), None)
        assert options is not None
        assert options["slippage"] == 25  # 2.5 pips * 10 = 25 points

    def test_no_slippage_when_none(self, adapter):
        """No slippage key in options when not set."""
        _wire_adapter(adapter)
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.1,
        )
        adapter._connection.get_positions = AsyncMock(
            return_value=[
                {"id": "99001", "type": "POSITION_TYPE_BUY", "symbol": "EURUSD", "volume": 0.1, "openPrice": 1.1000}
            ]
        )
        asyncio.new_event_loop().run_until_complete(adapter.place_order(req))

        call_args = adapter._connection.create_market_buy_order.call_args[0]
        options = next((a for a in call_args if isinstance(a, dict)), None)
        # Options should either be None or not contain slippage
        if options is not None:
            assert "slippage" not in options

    def test_zero_slippage_not_sent(self, adapter):
        """Zero slippage means disabled, not sent to broker."""
        _wire_adapter(adapter)
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.1,
            max_slippage_pips=0.0,
        )
        adapter._connection.get_positions = AsyncMock(
            return_value=[
                {"id": "99001", "type": "POSITION_TYPE_BUY", "symbol": "EURUSD", "volume": 0.1, "openPrice": 1.1000}
            ]
        )
        asyncio.new_event_loop().run_until_complete(adapter.place_order(req))

        call_args = adapter._connection.create_market_buy_order.call_args[0]
        options = next((a for a in call_args if isinstance(a, dict)), None)
        if options is not None:
            assert "slippage" not in options

    def test_slippage_in_order_request_model(self):
        """OrderRequest accepts max_slippage_pips field."""
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.1,
            max_slippage_pips=3.0,
        )
        assert req.max_slippage_pips == 3.0

    def test_slippage_default_none(self):
        """OrderRequest defaults max_slippage_pips to None."""
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.1,
        )
        assert req.max_slippage_pips is None


class TestOrderVerification:
    """Gap 2 fix: Post-order verification polling tests."""

    def test_verification_finds_position(self, adapter):
        """Successful verification populates fill_price and filled_volume."""
        _wire_adapter(adapter)
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.1,
        )
        # Mock position matching order_id
        adapter._connection.get_positions = AsyncMock(
            return_value=[
                {
                    "id": "99001",
                    "type": "POSITION_TYPE_BUY",
                    "symbol": "EURUSD",
                    "volume": 0.1,
                    "openPrice": 1.10025,
                }
            ]
        )
        result = asyncio.new_event_loop().run_until_complete(adapter.place_order(req))
        assert result.ok
        assert result.fill_price == 1.10025
        assert result.filled_volume == 0.1

    def test_verification_timeout(self, adapter):
        """Verification timeout adds warning but doesn't fail the order."""
        _wire_adapter(adapter)
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.1,
        )
        # Mock no matching position found
        adapter._connection.get_positions = AsyncMock(return_value=[])
        result = asyncio.new_event_loop().run_until_complete(adapter.place_order(req))
        assert result.ok  # Still OK — order was placed
        assert "verification timed out" in result.error

    def test_no_verification_for_limit_orders(self, adapter):
        """Limit orders skip post-order verification."""
        _wire_adapter(adapter)
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            volume=0.1,
            price=1.0900,
        )
        result = asyncio.new_event_loop().run_until_complete(adapter.place_order(req))
        assert result.ok
        # get_positions should not have been called for verification
        # (only the default mock from _wire_adapter, not extra verification calls)
        assert result.fill_price is None

    def test_verification_handles_poll_error(self, adapter):
        """Verification gracefully handles poll errors."""
        _wire_adapter(adapter)
        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.1,
        )
        # Mock position poll raises errors
        adapter._connection.get_positions = AsyncMock(side_effect=Exception("poll error"))
        result = asyncio.new_event_loop().run_until_complete(adapter.place_order(req))
        assert result.ok  # Order placement succeeded
        assert "verification timed out" in result.error


class TestConfigExecutionGaps:
    """Config parameter tests for execution gap fields."""

    def test_metaapi_config_defaults(self):
        """MetaApiConfig has sensible defaults for execution gap fields."""
        cfg = MetaApiConfig()
        assert cfg.retry_max_attempts == 3
        assert cfg.circuit_breaker_threshold == 5
        assert cfg.circuit_breaker_reset_seconds == 60.0
        assert cfg.reconnect_max_attempts == 3
        assert cfg.reconnect_backoff_base == 2.0
        assert cfg.order_verify_timeout == 5.0
        assert cfg.order_verify_interval == 0.5

    def test_risk_config_slippage(self):
        from novatrade.config import RiskConfig

        cfg = RiskConfig()
        assert cfg.max_slippage_pips == 3.0

    def test_metaapi_config_from_env(self):
        """from_env still works with new fields defaulting."""
        import os

        os.environ["METAAPI_TOKEN"] = "test-tok"
        os.environ["METAAPI_ACCOUNT_ID"] = "test-acct"
        try:
            cfg = MetaApiConfig.from_env()
            assert cfg.token == "test-tok"
            assert cfg.retry_max_attempts == 3  # default preserved
        finally:
            os.environ.pop("METAAPI_TOKEN", None)
            os.environ.pop("METAAPI_ACCOUNT_ID", None)


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


# ---------------------------------------------------------------------------
# Symbol normalization for MetaApi compatibility
# ---------------------------------------------------------------------------
