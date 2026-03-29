#!/usr/bin/env python3
"""Tests for broker_diagnostic module."""

import unittest.mock as mock
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.broker_diagnostic import BrokerDiagnostic


class TestBrokerDiagnostic:
    """Test suite for BrokerDiagnostic."""

    def test_init(self):
        """Test BrokerDiagnostic initialization."""
        diagnostic = BrokerDiagnostic("test_account_123", "test_token")

        assert diagnostic.account_id == "test_account_123"
        assert diagnostic.token == "test_token"
        assert diagnostic.adapter is None
        assert "timestamp" in diagnostic.results
        assert diagnostic.results["account_id"] == "test_acc..."

    @pytest.mark.asyncio
    async def test_connectivity_success(self):
        """Test successful connectivity check."""
        diagnostic = BrokerDiagnostic("test_account", "test_token")

        # Mock MetaApiAdapter import inside the method
        with mock.patch("novatrade.adapter.metaapi_provider.MetaApiAdapter") as mock_adapter:
            adapter_instance = AsyncMock()
            mock_adapter.return_value = adapter_instance

            await diagnostic._test_connectivity()

            # Verify adapter was created and connected
            mock_adapter.assert_called_once_with(
                token="test_token",
                account_id="test_account",
                region="us",
            )
            adapter_instance.connect.assert_called_once()

            # Verify results
            assert diagnostic.results["tests"]["connectivity"]["status"] == "PASS"
            assert diagnostic.adapter is adapter_instance

    async def test_connectivity_failure(self):
        """Test connectivity failure handling."""
        diagnostic = BrokerDiagnostic("test_account", "test_token")

        # Mock MetaApiAdapter to raise exception
        with mock.patch("novatrade.adapter.metaapi_provider.MetaApiAdapter") as mock_adapter:
            mock_adapter.side_effect = Exception("Connection failed")

            await diagnostic._test_connectivity()

            # Verify failure is recorded
            assert diagnostic.results["tests"]["connectivity"]["status"] == "FAIL"
            assert "Connection failed" in diagnostic.results["tests"]["connectivity"]["error"]
            assert diagnostic.adapter is None

    async def test_authentication_success(self):
        """Test successful authentication."""
        diagnostic = BrokerDiagnostic("test_account", "test_token")
        diagnostic.adapter = AsyncMock()

        # Mock account info
        account_info = MagicMock()
        account_info.type = "demo"
        account_info.server = "test-server"
        diagnostic.adapter.get_account.return_value = account_info

        await diagnostic._test_authentication()

        # Verify results
        test_result = diagnostic.results["tests"]["authentication"]
        assert test_result["status"] == "PASS"
        assert test_result["account_type"] == "demo"
        assert test_result["server"] == "test-server"

    async def test_authentication_skip_no_adapter(self):
        """Test authentication skipped when no adapter."""
        diagnostic = BrokerDiagnostic("test_account", "test_token")
        # adapter remains None

        await diagnostic._test_authentication()

        # Verify skip
        assert diagnostic.results["tests"]["authentication"]["status"] == "SKIP"

    async def test_account_status_trade_allowed_true(self):
        """Test account status with tradeAllowed=true."""
        diagnostic = BrokerDiagnostic("test_account", "test_token")
        diagnostic.adapter = AsyncMock()

        # Mock account with trading enabled
        account = MagicMock()
        account.tradeAllowed = True
        account.expertAllowed = True
        account.equity = 10000.0
        account.balance = 10000.0
        account.leverage = 100
        account.currency = "USD"
        account.state = "CONNECTED"
        diagnostic.adapter.get_account.return_value = account

        await diagnostic._test_account_status()

        # Verify results
        test_result = diagnostic.results["tests"]["account_status"]
        assert test_result["status"] == "PASS"
        assert test_result["account_data"]["trade_allowed"] is True
        assert test_result["message"] == "Trade allowed: True"

    async def test_account_status_trade_allowed_false(self):
        """Test account status with tradeAllowed=false (critical blocker)."""
        diagnostic = BrokerDiagnostic("test_account", "test_token")
        diagnostic.adapter = AsyncMock()

        # Mock account with trading disabled
        account = MagicMock()
        account.tradeAllowed = False
        account.expertAllowed = True
        account.equity = 10000.0
        diagnostic.adapter.get_account.return_value = account

        await diagnostic._test_account_status()

        # Verify results
        test_result = diagnostic.results["tests"]["account_status"]
        assert test_result["status"] == "FAIL"
        assert test_result["account_data"]["trade_allowed"] is False
        assert test_result["message"] == "Trade allowed: False"

    async def test_trading_permissions_full_access(self):
        """Test trading permissions with full access."""
        diagnostic = BrokerDiagnostic("test_account", "test_token")
        diagnostic.adapter = AsyncMock()

        # Mock successful positions and orders access
        diagnostic.adapter.get_positions.return_value = []
        diagnostic.adapter.get_orders.return_value = []

        await diagnostic._test_trading_permissions()

        # Verify results
        test_result = diagnostic.results["tests"]["trading_permissions"]
        assert test_result["status"] == "PASS"
        assert test_result["permissions"]["positions_access"] is True
        assert test_result["permissions"]["orders_access"] is True

    async def test_trading_permissions_partial_access(self):
        """Test trading permissions with partial access."""
        diagnostic = BrokerDiagnostic("test_account", "test_token")
        diagnostic.adapter = AsyncMock()

        # Mock positions access success, orders access failure
        diagnostic.adapter.get_positions.return_value = []
        diagnostic.adapter.get_orders.side_effect = Exception("Orders access denied")

        await diagnostic._test_trading_permissions()

        # Verify results
        test_result = diagnostic.results["tests"]["trading_permissions"]
        assert test_result["status"] == "PARTIAL"
        assert test_result["permissions"]["positions_access"] is True
        assert test_result["permissions"]["orders_access"] is False
        assert "Orders access denied" in test_result["permissions"]["orders_error"]

    async def test_symbol_availability_success(self):
        """Test symbol availability with successful spec and price retrieval."""
        diagnostic = BrokerDiagnostic("test_account", "test_token")
        diagnostic.adapter = AsyncMock()

        # Mock symbol spec
        spec = MagicMock()
        spec.minVolume = 0.01
        spec.maxVolume = 100.0
        spec.volumeStep = 0.01
        spec.digits = 5
        spec.tradeAllowed = True
        diagnostic.adapter.get_symbol_spec.return_value = spec

        # Mock price
        price = MagicMock()
        price.bid = 1.0850
        price.ask = 1.0852
        price.time = datetime.now(timezone.utc)
        diagnostic.adapter.get_price.return_value = price

        await diagnostic._test_symbol_availability()

        # Verify results
        test_result = diagnostic.results["tests"]["symbol_availability"]
        assert test_result["status"] == "PASS"
        assert test_result["symbol_data"]["spec_available"] is True
        assert test_result["symbol_data"]["price_available"] is True
        assert test_result["symbol_data"]["spec_data"]["min_volume"] == 0.01
        assert test_result["symbol_data"]["price_data"]["bid"] == 1.0850

    def test_generate_summary_trading_ready(self):
        """Test summary generation when trading ready."""
        diagnostic = BrokerDiagnostic("test_account", "test_token")

        # Mock all tests passing
        diagnostic.results["tests"] = {
            "connectivity": {"status": "PASS"},
            "authentication": {"status": "PASS"},
            "account_status": {"status": "PASS", "account_data": {"trade_allowed": True}},
            "trading_permissions": {"status": "PASS"},
            "symbol_availability": {"status": "PASS"},
            "order_capability": {"status": "PASS"},
        }

        diagnostic._generate_summary()

        summary = diagnostic.results["summary"]
        assert summary["total_tests"] == 6
        assert summary["passed"] == 6
        assert summary["failed"] == 0
        assert summary["critical_blocker"] is None
        assert summary["trading_ready"] is True
        assert "All critical tests passed" in summary["recommendations"][0]

    def test_generate_summary_trade_allowed_false(self):
        """Test summary generation with tradeAllowed=false blocker."""
        diagnostic = BrokerDiagnostic("test_account", "test_token")

        # Mock tradeAllowed=false
        diagnostic.results["tests"] = {
            "connectivity": {"status": "PASS"},
            "authentication": {"status": "PASS"},
            "account_status": {"status": "FAIL", "account_data": {"trade_allowed": False}},
            "trading_permissions": {"status": "PASS"},
            "symbol_availability": {"status": "PASS"},
            "order_capability": {"status": "PASS"},
        }

        diagnostic._generate_summary()

        summary = diagnostic.results["summary"]
        assert summary["critical_blocker"] == "tradeAllowed=false on broker account"
        assert summary["trading_ready"] is False
        assert any("Contact FTMO support" in rec for rec in summary["recommendations"])

    def test_generate_summary_connectivity_failure(self):
        """Test summary generation with connectivity failure."""
        diagnostic = BrokerDiagnostic("test_account", "test_token")

        # Mock connectivity failure
        diagnostic.results["tests"] = {
            "connectivity": {"status": "FAIL"},
            "authentication": {"status": "SKIP"},
            "account_status": {"status": "SKIP"},
            "trading_permissions": {"status": "SKIP"},
            "symbol_availability": {"status": "SKIP"},
            "order_capability": {"status": "SKIP"},
        }

        diagnostic._generate_summary()

        summary = diagnostic.results["summary"]
        assert summary["critical_blocker"] == "MetaAPI connectivity failure"
        assert summary["trading_ready"] is False
        assert any("Check MetaAPI token" in rec for rec in summary["recommendations"])

    def test_results_format(self):
        """Test results dictionary format."""
        diagnostic = BrokerDiagnostic("test_account_12345", "test_token")

        # Verify initial results structure
        assert "timestamp" in diagnostic.results
        assert diagnostic.results["account_id"] == "test_acc..."
        assert "tests" in diagnostic.results
        assert "summary" in diagnostic.results

        # Verify timestamp is valid ISO format
        timestamp_str = diagnostic.results["timestamp"]
        parsed_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        assert isinstance(parsed_time, datetime)
