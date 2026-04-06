"""Smoke tests for novatrade.adapter.metaapi_provider module.

These tests verify basic functionality and instantiation of the MetaApiAdapter
class without requiring actual MetaApi connections. They focus on testing
structure, initialization, and error handling.
"""

from unittest.mock import patch

from novatrade.adapter.metaapi_provider import MetaApiAdapter, _NoRedis
from novatrade.config import MetaApiConfig
from novatrade.models import OrderRequest, OrderSide, OrderType


class TestNoRedisHelper:
    """Test the _NoRedis helper class."""

    def test_no_redis_instantiation(self):
        """Test that _NoRedis can be instantiated."""
        no_redis = _NoRedis()
        assert no_redis is not None

    def test_no_redis_methods(self):
        """Test that _NoRedis has expected methods that don't crash."""
        no_redis = _NoRedis()

        # Should not raise exceptions
        result = no_redis.hgetall("key")
        assert result == {}

        no_redis.hset("key", "field", "value")  # Should not crash
        no_redis.expire("key", 60)  # Should not crash


class TestMetaApiAdapterInstantiation:
    """Test MetaApiAdapter can be instantiated with valid config."""

    def create_test_config(self) -> MetaApiConfig:
        """Create a test MetaApiConfig."""
        return MetaApiConfig(token="test_token_123", account_id="test_account_456")

    def test_metaapi_adapter_creation(self):
        """Test that MetaApiAdapter can be instantiated."""
        config = self.create_test_config()
        adapter = MetaApiAdapter(config)

        assert adapter is not None
        assert adapter._config == config
        assert adapter._api is None
        assert adapter._account is None
        assert adapter._connection is None
        assert adapter._connected is False

    def test_metaapi_adapter_config_storage(self):
        """Test that config is properly stored."""
        config = self.create_test_config()
        adapter = MetaApiAdapter(config)

        assert adapter._config.token == "test_token_123"
        assert adapter._config.account_id == "test_account_456"

    def test_metaapi_adapter_with_minimal_config(self):
        """Test adapter with minimal config."""
        config = MetaApiConfig()  # Empty config
        adapter = MetaApiAdapter(config)

        assert adapter is not None
        assert adapter._config == config


class TestMetaApiAdapterMethods:
    """Test MetaApiAdapter method existence and basic behavior."""

    def create_test_adapter(self) -> MetaApiAdapter:
        """Create a test adapter."""
        config = MetaApiConfig(token="test_token", account_id="test_account")
        return MetaApiAdapter(config)

    def test_get_equity_method_exists(self):
        """Test get_equity method exists and returns None when disconnected."""
        adapter = self.create_test_adapter()

        # Should return None when not connected
        equity = adapter.get_equity()
        assert equity is None

    @patch("novatrade.adapter.metaapi_provider.SimpleCircuitBreaker")
    def test_breaker_property(self, mock_breaker):
        """Test circuit breaker property exists."""
        adapter = self.create_test_adapter()

        # Access breaker property - should create a circuit breaker
        breaker = adapter.breaker
        assert breaker is not None

    def test_adapter_inheritance(self):
        """Test that MetaApiAdapter inherits from MT5Adapter."""
        from novatrade.adapter.base import MT5Adapter

        adapter = self.create_test_adapter()
        assert isinstance(adapter, MT5Adapter)

    def test_abstract_methods_exist(self):
        """Test that required abstract methods exist."""
        adapter = self.create_test_adapter()

        # These should exist (though will fail without connection)
        assert hasattr(adapter, "connect")
        assert hasattr(adapter, "disconnect")
        assert hasattr(adapter, "health_check")
        assert hasattr(adapter, "get_account")
        assert hasattr(adapter, "get_symbol_price")
        assert hasattr(adapter, "place_order")

        # Methods should be callable
        assert callable(adapter.connect)
        assert callable(adapter.disconnect)
        assert callable(adapter.health_check)


class TestMetaApiAdapterConstants:
    """Test module constants and mappings."""

    def test_success_codes_constant(self):
        """Test that success codes constant is defined."""
        from novatrade.adapter.metaapi_provider import _SUCCESS_CODES

        assert isinstance(_SUCCESS_CODES, set)
        assert len(_SUCCESS_CODES) > 0
        assert 0 in _SUCCESS_CODES  # Basic success code

    def test_timeframe_mapping(self):
        """Test timeframe mapping constant."""
        from novatrade.adapter.metaapi_provider import _TIMEFRAME_MAP

        assert isinstance(_TIMEFRAME_MAP, dict)
        assert len(_TIMEFRAME_MAP) > 0

        # Test some expected mappings
        assert _TIMEFRAME_MAP.get("M1") == "1m"
        assert _TIMEFRAME_MAP.get("H1") == "1h"
        assert _TIMEFRAME_MAP.get("D1") == "1d"

        # Test pass-through mappings
        assert _TIMEFRAME_MAP.get("1m") == "1m"
        assert _TIMEFRAME_MAP.get("1h") == "1h"


class TestMetaApiAdapterDisconnectedBehavior:
    """Test adapter behavior when disconnected (safe operations)."""

    def create_test_adapter(self) -> MetaApiAdapter:
        """Create a test adapter."""
        config = MetaApiConfig(token="test_token", account_id="test_account")
        return MetaApiAdapter(config)

    def test_disconnected_state(self):
        """Test adapter starts in disconnected state."""
        adapter = self.create_test_adapter()

        assert adapter._connected is False
        assert adapter._api is None
        assert adapter._account is None
        assert adapter._connection is None

    def test_get_equity_disconnected(self):
        """Test get_equity returns None when disconnected."""
        adapter = self.create_test_adapter()

        equity = adapter.get_equity()
        assert equity is None

    def test_health_check_method_exists(self):
        """Test health check method exists."""
        adapter = self.create_test_adapter()

        # Health check method should exist
        assert hasattr(adapter, "health_check")
        assert callable(adapter.health_check)

        # It should be an async method (coroutine function)
        import asyncio

        assert asyncio.iscoroutinefunction(adapter.health_check)


class TestMetaApiAdapterConfig:
    """Test configuration handling."""

    def test_config_validation(self):
        """Test that different config values work."""
        configs = [
            MetaApiConfig(token="token1", account_id="account1"),
            MetaApiConfig(token="", account_id=""),
            MetaApiConfig(token="test_token", account_id="test_account", domain="custom.domain.com", region="london"),
        ]

        for config in configs:
            adapter = MetaApiAdapter(config)
            assert adapter._config == config

    def test_config_properties_accessible(self):
        """Test that config properties are accessible."""
        config = MetaApiConfig(
            token="test_token", account_id="test_account", retry_max_attempts=5, circuit_breaker_threshold=10
        )

        adapter = MetaApiAdapter(config)

        assert adapter._config.token == "test_token"
        assert adapter._config.account_id == "test_account"
        assert adapter._config.retry_max_attempts == 5
        assert adapter._config.circuit_breaker_threshold == 10


class TestMetaApiAdapterOrderRequestHandling:
    """Test order request structure handling (without submission)."""

    def test_order_request_creation(self):
        """Test that OrderRequest objects can be created for testing."""
        # This tests that the models work correctly with the adapter
        request = OrderRequest(
            symbol="EURUSD", side=OrderSide.BUY, volume=1.0, order_type=OrderType.MARKET, idempotency_key="test_key_123"
        )

        assert request.symbol == "EURUSD"
        assert request.side == OrderSide.BUY
        assert request.volume == 1.0
        assert request.order_type == OrderType.MARKET
        assert request.idempotency_key == "test_key_123"

    def test_adapter_with_order_request(self):
        """Test that adapter can accept OrderRequest type (structure test)."""
        adapter = self.create_test_adapter()

        request = OrderRequest(
            symbol="EURUSD", side=OrderSide.SELL, volume=0.1, order_type=OrderType.LIMIT, price=1.0500
        )

        # Verify the request was created properly
        assert request.symbol == "EURUSD"
        assert request.side == OrderSide.SELL

        # The adapter should have a place_order method that accepts OrderRequest
        assert hasattr(adapter, "place_order")
        assert callable(adapter.place_order)

        # We don't actually call it since it would require connection

    def create_test_adapter(self) -> MetaApiAdapter:
        """Create a test adapter."""
        config = MetaApiConfig(token="test_token", account_id="test_account")
        return MetaApiAdapter(config)


class TestMetaApiAdapterIntegration:
    """Integration smoke tests for the adapter."""

    def test_full_instantiation_workflow(self):
        """Test complete instantiation workflow."""
        # Create config
        config = MetaApiConfig(
            token="integration_test_token", account_id="integration_test_account", domain="test.domain.com"
        )

        # Create adapter
        adapter = MetaApiAdapter(config)

        # Verify state
        assert adapter is not None
        assert adapter._connected is False

        # Verify methods exist
        assert hasattr(adapter, "connect")
        assert hasattr(adapter, "disconnect")
        assert hasattr(adapter, "health_check")
        assert hasattr(adapter, "get_equity")

        # Verify disconnected behavior
        equity = adapter.get_equity()
        assert equity is None

    def test_error_handling_structure(self):
        """Test that the adapter has proper error handling structure."""
        adapter = MetaApiAdapter(MetaApiConfig())

        # Should not crash on basic operations when disconnected
        equity = adapter.get_equity()
        assert equity is None

        # Circuit breaker should exist
        assert hasattr(adapter, "breaker")

    def test_import_structure(self):
        """Test that all required imports work."""
        # This test verifies the module can be imported without errors
        from novatrade.adapter.metaapi_provider import (
            _SUCCESS_CODES,
            _TIMEFRAME_MAP,
            MetaApiAdapter,
            _NoRedis,
        )

        assert MetaApiAdapter is not None
        assert _SUCCESS_CODES is not None
        assert _TIMEFRAME_MAP is not None
        assert _NoRedis is not None
