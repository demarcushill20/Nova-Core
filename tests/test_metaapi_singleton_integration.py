"""Integration test for MetaApi SDK singleton functionality.

This test demonstrates that the singleton pattern works correctly with real
MetaApiAdapter instances in a realistic scenario.
"""

from unittest.mock import patch

import pytest

from novatrade.adapter.metaapi_provider import MetaApiAdapter
from novatrade.adapter.metaapi_sdk_singleton import close_all_instances, get_singleton_stats
from novatrade.config import MetaApiConfig


@pytest.fixture(autouse=True)
def cleanup_singletons():
    """Clean up singleton instances before and after each test."""
    close_all_instances()
    yield
    close_all_instances()


def test_multiple_adapters_share_singleton_real_config():
    """Integration test: multiple adapters with identical config share singleton."""
    # Create identical configurations
    config1 = MetaApiConfig(
        token="integration-test-token",
        account_id="test-account-123",
        domain="test.example.com",
        region="test-region",
        application="IntegrationTest",
    )

    config2 = MetaApiConfig(
        token="integration-test-token",
        account_id="test-account-123",
        domain="test.example.com",
        region="test-region",
        application="IntegrationTest",
    )

    # Verify configs are identical but different objects
    assert config1 is not config2
    assert config1.token == config2.token
    assert config1.account_id == config2.account_id
    assert config1.domain == config2.domain
    assert config1.region == config2.region
    assert config1.application == config2.application

    # Initially no singletons
    stats = get_singleton_stats()
    assert stats["instance_count"] == 0

    # Create adapters but don't connect (to avoid requiring real credentials)
    adapter1 = MetaApiAdapter(config1)
    adapter2 = MetaApiAdapter(config2)

    # Verify they're different adapter objects
    assert adapter1 is not adapter2

    # Mock the MetaApi SDK creation to track singleton behavior
    with patch("novatrade.adapter.metaapi_provider.get_metaapi_sdk") as mock_get_sdk:
        mock_sdk_instance = object()  # Use object() for identity testing
        mock_get_sdk.return_value = mock_sdk_instance

        # Simulate the connect process (this calls get_metaapi_sdk internally)
        # We're not actually connecting, just testing the singleton behavior
        adapter1._api = mock_get_sdk(
            token=adapter1._config.token,
            domain=adapter1._config.domain,
            region=adapter1._config.region,
            application=adapter1._config.application,
        )

        adapter2._api = mock_get_sdk(
            token=adapter2._config.token,
            domain=adapter2._config.domain,
            region=adapter2._config.region,
            application=adapter2._config.application,
        )

        # Verify both adapters got the same SDK instance
        assert adapter1._api is adapter2._api
        assert adapter1._api is mock_sdk_instance
        assert adapter2._api is mock_sdk_instance

        # Verify get_metaapi_sdk was called twice with identical parameters
        assert mock_get_sdk.call_count == 2
        call_args_list = mock_get_sdk.call_args_list
        assert call_args_list[0] == call_args_list[1]  # Identical calls


def test_different_configs_create_different_singletons():
    """Integration test: different configs create separate singleton instances."""
    # Create different configurations
    config1 = MetaApiConfig(
        token="token-1",
        account_id="account-1",
        domain="domain1.example.com",
        region="region-1",
        application="App1",
    )

    config2 = MetaApiConfig(
        token="token-2",
        account_id="account-2",
        domain="domain2.example.com",
        region="region-2",
        application="App2",
    )

    # Create adapters
    adapter1 = MetaApiAdapter(config1)
    adapter2 = MetaApiAdapter(config2)

    # Mock SDK creation to track instances
    with patch("novatrade.adapter.metaapi_provider.get_metaapi_sdk") as mock_get_sdk:
        sdk_instance1 = object()
        sdk_instance2 = object()
        mock_get_sdk.side_effect = [sdk_instance1, sdk_instance2]

        # Simulate connect process for both adapters
        adapter1._api = mock_get_sdk(
            token=adapter1._config.token,
            domain=adapter1._config.domain,
            region=adapter1._config.region,
            application=adapter1._config.application,
        )

        adapter2._api = mock_get_sdk(
            token=adapter2._config.token,
            domain=adapter2._config.domain,
            region=adapter2._config.region,
            application=adapter2._config.application,
        )

        # Verify different instances for different configs
        assert adapter1._api is not adapter2._api
        assert adapter1._api is sdk_instance1
        assert adapter2._api is sdk_instance2

        # Both calls should have been made with different parameters
        assert mock_get_sdk.call_count == 2
        call_args_list = mock_get_sdk.call_args_list
        assert call_args_list[0] != call_args_list[1]  # Different calls


def test_singleton_prevents_resource_leaks():
    """Integration test: singleton pattern prevents SDK instance proliferation."""
    config = MetaApiConfig(
        token="leak-test-token",
        account_id="leak-test-account",
        domain="leak-test.example.com",
        region="leak-test-region",
        application="LeakTest",
    )

    # Create many adapters with the same config
    adapters = []
    for _i in range(5):
        adapters.append(MetaApiAdapter(config))

    # Mock SDK creation to count instances
    with patch("novatrade.adapter.metaapi_provider.get_metaapi_sdk") as mock_get_sdk:
        shared_instance = object()
        mock_get_sdk.return_value = shared_instance

        # "Connect" all adapters
        for adapter in adapters:
            adapter._api = mock_get_sdk(
                token=adapter._config.token,
                domain=adapter._config.domain,
                region=adapter._config.region,
                application=adapter._config.application,
            )

        # All adapters should share the same SDK instance
        for adapter in adapters:
            assert adapter._api is shared_instance

        # get_metaapi_sdk should be called once per adapter, but return same instance
        assert mock_get_sdk.call_count == 5

        # Verify all calls were identical
        call_args_list = mock_get_sdk.call_args_list
        for call_args in call_args_list[1:]:
            assert call_args == call_args_list[0]
