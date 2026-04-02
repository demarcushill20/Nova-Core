"""Tests for MetaApi SDK singleton manager.

Validates that:
1. Multiple calls with same config return same instance
2. Different configs create different instances
3. Instances are properly cached and cleaned up
4. Thread safety is maintained
5. Integration with MetaApiAdapter works correctly
"""

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from novatrade.adapter.metaapi_sdk_singleton import (
    close_all_instances,
    get_metaapi_sdk,
    get_singleton_stats,
)
from novatrade.config import MetaApiConfig


@pytest.fixture(autouse=True)
def cleanup_singletons():
    """Clean up singleton instances before and after each test."""
    close_all_instances()
    yield
    close_all_instances()


@pytest.fixture
def mock_metaapi_config():
    """Mock MetaApiConfig for testing."""
    return MetaApiConfig(
        token="test-token-123",
        account_id="test-account-456",
        domain="test.example.com",
        region="test-region",
        application="TestApp",
    )


class TestMetaApiSingletonManager:
    """Test the MetaApi SDK singleton manager."""

    @patch("novatrade.adapter.metaapi_sdk_singleton.MetaApi")
    def test_singleton_same_config_returns_same_instance(self, mock_metaapi_class):
        """Multiple calls with identical config should return same instance."""
        # Mock the MetaApi constructor
        mock_instance = MagicMock()
        mock_metaapi_class.return_value = mock_instance

        # Get SDK instance twice with identical config
        instance1 = get_metaapi_sdk(
            token="test-token-123",
            domain="test.example.com",
            region="test-region",
            application="TestApp",
        )

        instance2 = get_metaapi_sdk(
            token="test-token-123",
            domain="test.example.com",
            region="test-region",
            application="TestApp",
        )

        # Should be the exact same object
        assert instance1 is instance2
        assert instance1 is mock_instance

        # MetaApi constructor should only be called once
        mock_metaapi_class.assert_called_once_with(
            "test-token-123",
            {
                "domain": "test.example.com",
                "region": "test-region",
                "application": "TestApp",
            },
        )

    @patch("novatrade.adapter.metaapi_sdk_singleton.MetaApi")
    def test_singleton_different_config_creates_different_instances(self, mock_metaapi_class):
        """Different configurations should create separate instances."""
        # Create two different mock instances
        mock_instance1 = MagicMock()
        mock_instance2 = MagicMock()
        mock_metaapi_class.side_effect = [mock_instance1, mock_instance2]

        # Get SDK instances with different configs
        instance1 = get_metaapi_sdk(
            token="token1",
            domain="domain1.com",
            region="region1",
            application="App1",
        )

        instance2 = get_metaapi_sdk(
            token="token2",
            domain="domain2.com",
            region="region2",
            application="App2",
        )

        # Should be different instances
        assert instance1 is not instance2
        assert instance1 is mock_instance1
        assert instance2 is mock_instance2

        # MetaApi should be called twice with different configs
        assert mock_metaapi_class.call_count == 2

    @patch("novatrade.adapter.metaapi_sdk_singleton.MetaApi")
    def test_singleton_stats(self, mock_metaapi_class):
        """get_singleton_stats should return accurate information."""
        mock_instance1 = MagicMock()
        mock_instance2 = MagicMock()
        mock_metaapi_class.side_effect = [mock_instance1, mock_instance2]

        # Initially no instances
        stats = get_singleton_stats()
        assert stats["instance_count"] == 0
        assert stats["config_keys"] == []

        # Create first instance
        get_metaapi_sdk("token1", "domain1", "region1", "app1")
        stats = get_singleton_stats()
        assert stats["instance_count"] == 1
        assert len(stats["config_keys"]) == 1

        # Create second instance
        get_metaapi_sdk("token2", "domain2", "region2", "app2")
        stats = get_singleton_stats()
        assert stats["instance_count"] == 2
        assert len(stats["config_keys"]) == 2

    def test_close_all_instances(self):
        """close_all_instances should close all cached instances."""
        with patch("novatrade.adapter.metaapi_sdk_singleton.MetaApi") as mock_metaapi_class:
            mock_instance1 = MagicMock()
            mock_instance2 = MagicMock()
            mock_metaapi_class.side_effect = [mock_instance1, mock_instance2]

            # Create two instances
            get_metaapi_sdk("token1", "domain1", "region1")
            get_metaapi_sdk("token2", "domain2", "region2")

            # Verify they exist
            stats = get_singleton_stats()
            assert stats["instance_count"] == 2

            # Close all
            close_all_instances()

            # Verify cleanup
            mock_instance1.close.assert_called_once()
            mock_instance2.close.assert_called_once()

            stats = get_singleton_stats()
            assert stats["instance_count"] == 0

    @patch("novatrade.adapter.metaapi_sdk_singleton.MetaApi")
    def test_thread_safety(self, mock_metaapi_class):
        """Singleton should be thread-safe."""
        mock_instance = MagicMock()
        mock_metaapi_class.return_value = mock_instance

        instances = []
        errors = []

        def get_instance_in_thread():
            try:
                instance = get_metaapi_sdk("token", "domain", "region", "app")
                instances.append(instance)
            except Exception as e:
                errors.append(e)

        # Create multiple threads trying to get the singleton
        threads = []
        for _ in range(10):
            thread = threading.Thread(target=get_instance_in_thread)
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Verify no errors occurred
        assert len(errors) == 0

        # All instances should be the same object
        assert len(instances) == 10
        assert all(instance is mock_instance for instance in instances)

        # MetaApi constructor should only be called once
        mock_metaapi_class.assert_called_once()

    @patch("novatrade.adapter.metaapi_sdk_singleton.MetaApi")
    def test_config_key_generation(self, mock_metaapi_class):
        """Config keys should be generated correctly and be distinguishable."""
        mock_instance1 = MagicMock()
        mock_instance2 = MagicMock()
        mock_metaapi_class.side_effect = [mock_instance1, mock_instance2]

        # Create instances with different tokens (different first 8 chars)
        get_metaapi_sdk("token123", "domain.com", "region", "app")
        get_metaapi_sdk("tokenabc", "domain.com", "region", "app")

        stats = get_singleton_stats()
        assert stats["instance_count"] == 2

        # Config keys should be different (different token prefixes)
        config_keys = stats["config_keys"]
        assert len(config_keys) == 2
        assert config_keys[0] != config_keys[1]

    def test_close_all_instances_handles_exceptions(self):
        """close_all_instances should handle exceptions gracefully."""
        with patch("novatrade.adapter.metaapi_sdk_singleton.MetaApi") as mock_metaapi_class:
            mock_instance = MagicMock()
            mock_instance.close.side_effect = Exception("Close failed")
            mock_metaapi_class.return_value = mock_instance

            # Create instance
            get_metaapi_sdk("token", "domain", "region")

            # close_all_instances should not raise exception
            close_all_instances()  # Should not raise

            # Instance should still be cleaned up from cache
            stats = get_singleton_stats()
            assert stats["instance_count"] == 0


class TestMetaApiAdapterSingletonIntegration:
    """Test integration between MetaApiAdapter and the singleton manager."""

    def test_adapter_uses_singleton(self, mock_metaapi_config):
        """MetaApiAdapter should use singleton MetaApi instances."""
        from novatrade.adapter.metaapi_provider import MetaApiAdapter

        with patch("novatrade.adapter.metaapi_provider.get_metaapi_sdk") as mock_get_singleton:
            mock_sdk = MagicMock()
            mock_get_singleton.return_value = mock_sdk

            # Create adapter
            adapter = MetaApiAdapter(mock_metaapi_config)

            # Mock the account and connection setup with proper async mocks
            mock_account = MagicMock()
            mock_account.state = "DEPLOYED"
            mock_connection = MagicMock()

            # Use AsyncMock for async operations
            mock_sdk.metatrader_account_api.get_account = AsyncMock(return_value=mock_account)
            mock_account.get_rpc_connection.return_value = mock_connection
            mock_account.wait_connected = AsyncMock()
            mock_connection.connect = AsyncMock()
            mock_connection.wait_synchronized = AsyncMock()
            mock_connection.get_account_information = AsyncMock(
                return_value={"tradeAllowed": True, "broker": "test", "equity": 1000.0}
            )

            # Connect should use the singleton
            import asyncio

            async def test_connect():
                status = await adapter.connect()
                return status

            status = asyncio.run(test_connect())

            # Verify singleton was called with correct parameters
            mock_get_singleton.assert_called_once_with(
                token=mock_metaapi_config.token,
                domain=mock_metaapi_config.domain,
                region=mock_metaapi_config.region,
                application=mock_metaapi_config.application,
            )

            # Verify connection was successful
            assert status.connected is True

    def test_multiple_adapters_share_singleton(self, mock_metaapi_config):
        """Multiple adapters with same config should share SDK instance."""
        from novatrade.adapter.metaapi_provider import MetaApiAdapter

        with patch("novatrade.adapter.metaapi_provider.get_metaapi_sdk") as mock_get_singleton:
            mock_sdk = MagicMock()
            mock_get_singleton.return_value = mock_sdk

            # Create two adapters with same config
            adapter1 = MetaApiAdapter(mock_metaapi_config)
            adapter2 = MetaApiAdapter(mock_metaapi_config)

            # Mock connection setup (minimal for this test) with proper async mocks
            mock_account = MagicMock()
            mock_account.state = "DEPLOYED"
            mock_connection = MagicMock()

            # Use AsyncMock for async operations
            mock_sdk.metatrader_account_api.get_account = AsyncMock(return_value=mock_account)
            mock_account.get_rpc_connection.return_value = mock_connection
            mock_account.wait_connected = AsyncMock()
            mock_connection.connect = AsyncMock()
            mock_connection.wait_synchronized = AsyncMock()
            mock_connection.get_account_information = AsyncMock(return_value={"tradeAllowed": True})

            # Connect both adapters
            import asyncio

            async def test_both_connect():
                await adapter1.connect()
                await adapter2.connect()

            asyncio.run(test_both_connect())

            # Singleton should be called twice with same parameters
            assert mock_get_singleton.call_count == 2
            call_args_list = mock_get_singleton.call_args_list
            assert call_args_list[0] == call_args_list[1]  # Same arguments

            # But both should receive the same SDK instance
            assert adapter1._api is mock_sdk
            assert adapter2._api is mock_sdk
            assert adapter1._api is adapter2._api
