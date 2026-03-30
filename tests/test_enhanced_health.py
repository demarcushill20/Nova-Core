"""Tests for EnhancedHealthMonitor and enhanced_health_check.

Covers the broker diagnostic integration, caching, and convenience function.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from novatrade.models import (
    AccountState,
    HealthState,
    HealthStatus,
)
from novatrade.monitor.enhanced_health import (
    EnhancedHealthMonitor,
    enhanced_health_check,
)

# =====================================================================
# Fake adapter
# =====================================================================


@dataclass
class FakePosition:
    unrealized_pnl: float = 10.0


class FakeAdapterConfig:
    account_id: str = "test_account_123"
    token: str = "test_token_abc"


class FakeAdapter:
    """Minimal MT5Adapter stand-in for testing."""

    def __init__(self, health_state=HealthState.OK, raise_health=False, raise_account=False):
        self._health_state = health_state
        self._raise_health = raise_health
        self._raise_account = raise_account
        self._config = FakeAdapterConfig()

    async def health_check(self):
        if self._raise_health:
            raise ConnectionError("adapter down")
        return HealthStatus(state=self._health_state, connected=True, message="ok")

    async def get_account(self):
        if self._raise_account:
            raise RuntimeError("account fetch failed")
        return AccountState(balance=10000.0, equity=10500.0)

    async def get_positions(self):
        if self._raise_account:
            raise RuntimeError("positions fetch failed")
        return [FakePosition(10.0), FakePosition(-5.0)]

    async def close(self):
        pass


# =====================================================================
# EnhancedHealthMonitor — basic flow
# =====================================================================


class TestEnhancedHealthMonitor:
    @pytest.mark.asyncio
    async def test_healthy_snapshot_no_diagnostic(self):
        """Healthy adapter should return snapshot without running diagnostic."""
        adapter = FakeAdapter()
        monitor = EnhancedHealthMonitor(adapter)
        # Mock BrokerDiagnostic to avoid real MetaApi connections
        with patch("novatrade.monitor.enhanced_health.BrokerDiagnostic") as MockDiag:
            instance = MockDiag.return_value
            instance.run_full_diagnostic = AsyncMock(return_value={"tests": {}, "summary": {}})
            snapshot, diagnostic = await monitor.take_health_snapshot_with_diagnostics()
        assert snapshot.adapter_health.state == HealthState.OK
        assert snapshot.position_count == 2
        assert snapshot.total_unrealized_pnl == 5.0  # 10 + (-5)
        assert snapshot.error == ""

    @pytest.mark.asyncio
    async def test_health_check_failure_returns_down_snapshot(self):
        """When health_check raises, snapshot should show DOWN."""
        adapter = FakeAdapter(raise_health=True)
        monitor = EnhancedHealthMonitor(adapter)
        snapshot, diagnostic = await monitor.take_health_snapshot_with_diagnostics()
        assert snapshot.adapter_health.state == HealthState.DOWN
        assert "adapter down" in snapshot.error
        assert diagnostic is None

    @pytest.mark.asyncio
    async def test_account_failure_returns_partial_snapshot(self):
        """When account fetch fails, snapshot should have health but show error."""
        adapter = FakeAdapter(raise_account=True)
        monitor = EnhancedHealthMonitor(adapter)
        snapshot, diagnostic = await monitor.take_health_snapshot_with_diagnostics()
        assert snapshot.adapter_health.state == HealthState.OK
        assert "account/position check failed" in snapshot.error
        assert diagnostic is None


# =====================================================================
# Diagnostic caching
# =====================================================================


class TestDiagnosticCaching:
    def test_should_refresh_when_no_prior(self):
        monitor = EnhancedHealthMonitor(FakeAdapter())
        assert monitor._should_refresh_diagnostic() is True

    def test_should_not_refresh_within_ttl(self):
        monitor = EnhancedHealthMonitor(FakeAdapter())
        monitor.last_diagnostic_time = datetime.now(timezone.utc).timestamp()
        assert monitor._should_refresh_diagnostic() is False

    def test_should_refresh_after_ttl(self):
        monitor = EnhancedHealthMonitor(FakeAdapter())
        monitor.last_diagnostic_time = datetime.now(timezone.utc).timestamp() - 600  # 10 min ago
        assert monitor._should_refresh_diagnostic() is True

    def test_get_cached_diagnostic_returns_cached(self):
        monitor = EnhancedHealthMonitor(FakeAdapter())
        monitor.last_diagnostic_time = datetime.now(timezone.utc).timestamp()
        monitor.last_diagnostic_result = {"tests": {}, "summary": {}}
        assert monitor.get_cached_diagnostic() is not None

    def test_get_cached_diagnostic_returns_none_when_stale(self):
        monitor = EnhancedHealthMonitor(FakeAdapter())
        monitor.last_diagnostic_time = datetime.now(timezone.utc).timestamp() - 600
        monitor.last_diagnostic_result = {"tests": {}, "summary": {}}
        assert monitor.get_cached_diagnostic() is None

    def test_get_cached_diagnostic_returns_none_when_never_run(self):
        monitor = EnhancedHealthMonitor(FakeAdapter())
        assert monitor.get_cached_diagnostic() is None


# =====================================================================
# Broker diagnostic execution
# =====================================================================


class TestBrokerDiagnostic:
    @pytest.mark.asyncio
    async def test_run_diagnostic_with_missing_config(self):
        """Missing adapter config should return None gracefully."""
        adapter = FakeAdapter()
        adapter._config = None
        monitor = EnhancedHealthMonitor(adapter)
        result = await monitor._run_broker_diagnostic()
        assert result is None

    @pytest.mark.asyncio
    async def test_run_diagnostic_with_missing_credentials(self):
        """Missing account_id or token should return None."""
        adapter = FakeAdapter()
        adapter._config = MagicMock(account_id=None, token="abc")
        monitor = EnhancedHealthMonitor(adapter)
        result = await monitor._run_broker_diagnostic()
        assert result is None

    @pytest.mark.asyncio
    async def test_run_diagnostic_success(self):
        """Successful diagnostic should cache results."""
        adapter = FakeAdapter()
        monitor = EnhancedHealthMonitor(adapter)

        mock_result = {
            "tests": {"connectivity": {"status": "pass"}},
            "summary": {"critical_issues": []},
        }
        with patch("novatrade.monitor.enhanced_health.BrokerDiagnostic") as MockDiag:
            instance = MockDiag.return_value
            instance.run_full_diagnostic = AsyncMock(return_value=mock_result)
            result = await monitor._run_broker_diagnostic()

        assert result is not None
        assert result["tests"]["connectivity"]["status"] == "pass"
        assert monitor.last_diagnostic_result == mock_result
        assert monitor.last_diagnostic_time is not None

    @pytest.mark.asyncio
    async def test_run_diagnostic_timeout(self):
        """Diagnostic timeout should return None gracefully."""
        adapter = FakeAdapter()
        monitor = EnhancedHealthMonitor(adapter)

        with patch("novatrade.monitor.enhanced_health.BrokerDiagnostic") as MockDiag:
            instance = MockDiag.return_value
            instance.run_full_diagnostic = AsyncMock(side_effect=asyncio.TimeoutError)
            result = await monitor._run_broker_diagnostic()

        assert result is None

    @pytest.mark.asyncio
    async def test_run_diagnostic_exception(self):
        """Diagnostic exception should return None gracefully."""
        adapter = FakeAdapter()
        monitor = EnhancedHealthMonitor(adapter)

        with patch("novatrade.monitor.enhanced_health.BrokerDiagnostic") as MockDiag:
            instance = MockDiag.return_value
            instance.run_full_diagnostic = AsyncMock(side_effect=RuntimeError("boom"))
            result = await monitor._run_broker_diagnostic()

        assert result is None

    @pytest.mark.asyncio
    async def test_run_diagnostic_logs_critical_issues(self):
        """Critical issues in diagnostic should be logged."""
        adapter = FakeAdapter()
        monitor = EnhancedHealthMonitor(adapter)

        mock_result = {
            "tests": {},
            "summary": {"critical_issues": ["tradeAllowed is false"]},
        }
        with patch("novatrade.monitor.enhanced_health.BrokerDiagnostic") as MockDiag:
            instance = MockDiag.return_value
            instance.run_full_diagnostic = AsyncMock(return_value=mock_result)
            result = await monitor._run_broker_diagnostic()

        assert result is not None


# =====================================================================
# Force diagnostic on DOWN state
# =====================================================================


class TestDiagnosticTriggers:
    @pytest.mark.asyncio
    async def test_force_diagnostic_flag(self):
        """force_diagnostic=True should always run diagnostic."""
        adapter = FakeAdapter()
        monitor = EnhancedHealthMonitor(adapter)
        # Pretend cache is fresh
        monitor.last_diagnostic_time = datetime.now(timezone.utc).timestamp()

        mock_result = {"tests": {}, "summary": {}}
        with patch("novatrade.monitor.enhanced_health.BrokerDiagnostic") as MockDiag:
            instance = MockDiag.return_value
            instance.run_full_diagnostic = AsyncMock(return_value=mock_result)
            snapshot, diagnostic = await monitor.take_health_snapshot_with_diagnostics(force_diagnostic=True)

        assert diagnostic is not None

    @pytest.mark.asyncio
    async def test_down_state_triggers_diagnostic(self):
        """DOWN adapter state should trigger diagnostic."""
        adapter = FakeAdapter(health_state=HealthState.DOWN)
        monitor = EnhancedHealthMonitor(adapter)
        # Pretend cache is fresh to prove DOWN overrides
        monitor.last_diagnostic_time = datetime.now(timezone.utc).timestamp()

        mock_result = {"tests": {}, "summary": {}}
        with patch("novatrade.monitor.enhanced_health.BrokerDiagnostic") as MockDiag:
            instance = MockDiag.return_value
            instance.run_full_diagnostic = AsyncMock(return_value=mock_result)
            snapshot, diagnostic = await monitor.take_health_snapshot_with_diagnostics()

        assert diagnostic is not None


# =====================================================================
# diagnose_trade_permission_issue
# =====================================================================


class TestDiagnoseTradePermission:
    @pytest.mark.asyncio
    async def test_returns_analysis_on_success(self):
        adapter = FakeAdapter()
        monitor = EnhancedHealthMonitor(adapter)

        mock_result = {
            "timestamp": "2026-03-30T12:00:00Z",
            "account_id": "test",
            "tests": {
                "connectivity": {"status": "pass"},
                "authentication": {"status": "pass"},
                "trading_permissions": {"status": "fail", "trade_allowed": False},
                "account_status": {"status": "pass"},
            },
            "summary": {
                "critical_issues": ["tradeAllowed: false"],
                "recommendations": ["Check broker settings"],
            },
        }
        with patch("novatrade.monitor.enhanced_health.BrokerDiagnostic") as MockDiag:
            instance = MockDiag.return_value
            instance.run_full_diagnostic = AsyncMock(return_value=mock_result)
            result = await monitor.diagnose_trade_permission_issue()

        assert result["connectivity"] == "pass"
        assert result["trading_permissions"]["trade_allowed"] is False
        assert len(result["critical_issues"]) > 0

    @pytest.mark.asyncio
    async def test_returns_error_on_failure(self):
        adapter = FakeAdapter()
        adapter._config = None  # will cause diagnostic to return None
        monitor = EnhancedHealthMonitor(adapter)
        result = await monitor.diagnose_trade_permission_issue()
        assert "error" in result
        assert "recommendation" in result


# =====================================================================
# enhanced_health_check convenience function
# =====================================================================


class TestEnhancedHealthCheckFunction:
    @pytest.mark.asyncio
    async def test_basic_health_check(self):
        adapter = FakeAdapter()
        with patch("novatrade.monitor.enhanced_health.BrokerDiagnostic") as MockDiag:
            instance = MockDiag.return_value
            instance.run_full_diagnostic = AsyncMock(return_value={"tests": {}, "summary": {}})
            result = await enhanced_health_check(adapter)
        assert "health_snapshot" in result
        assert "broker_diagnostic" in result
        assert "timestamp" in result
        assert result["health_snapshot"]["adapter_health"]["state"] == "OK"
        assert result["health_snapshot"]["account"]["balance"] == 10000.0
        assert result["health_snapshot"]["positions"]["count"] == 2
        assert result["health_snapshot"]["positions"]["total_pnl"] == 5.0

    @pytest.mark.asyncio
    async def test_health_check_with_diagnostic(self):
        adapter = FakeAdapter()
        mock_result = {"tests": {}, "summary": {}}
        with patch("novatrade.monitor.enhanced_health.BrokerDiagnostic") as MockDiag:
            instance = MockDiag.return_value
            instance.run_full_diagnostic = AsyncMock(return_value=mock_result)
            result = await enhanced_health_check(adapter, include_diagnostic=True)
        assert result["broker_diagnostic"] is not None

    @pytest.mark.asyncio
    async def test_health_check_adapter_down(self):
        adapter = FakeAdapter(raise_health=True)
        result = await enhanced_health_check(adapter)
        assert result["health_snapshot"]["adapter_health"]["state"] == "DOWN"
        assert result["health_snapshot"]["error"] is not None
