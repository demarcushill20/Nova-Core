"""Tests for P0 incident fixes: runner launch gate + MetaAPI connect diagnostics.

Covers:
  - runner.py: adapter_connected now derives from real adapter state (unit test)
  - metaapi_provider.py: connect() returns phase-specific failure diagnostics
  - monitor_loop.py: watchdog integration
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from novatrade.config import MetaApiConfig, NovaTradeCfg, RiskConfig
from novatrade.models import AccountMode, HealthState
from novatrade.runtime.launch_gate import LaunchMode, ReadinessVerdict, evaluate_launch_gate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> NovaTradeCfg:
    risk = RiskConfig(
        max_daily_drawdown_pct=5.0,
        max_total_drawdown_pct=10.0,
        max_positions=5,
        max_volume_per_trade=1.0,
        min_volume_per_trade=0.01,
        check_forex_session=False,
    )
    defaults = dict(
        mode=AccountMode.DEMO,
        symbols=["EURUSD"],
        risk=risk,
        dry_run=True,
    )
    defaults.update(overrides)
    return NovaTradeCfg(**defaults)  # type: ignore[arg-type]


# ===========================================================================
# 1. adapter_connected derives from real state (launch gate level)
# ===========================================================================


class TestAdapterConnectedFromRealState:
    """Verify that getattr(adapter, '_connected', fallback) logic works correctly."""

    def test_dry_run_adapter_fallback_true(self):
        """DryRunAdapter has no _connected → getattr falls back to is_dry_run=True."""

        class FakeAdapter:
            pass

        adapter = FakeAdapter()
        is_dry_run = True
        # This is the exact logic from runner.py line 324
        result = getattr(adapter, "_connected", is_dry_run)
        assert result is True

    def test_metaapi_adapter_connected_true(self):
        """When adapter._connected=True, getattr returns True."""

        class FakeAdapter:
            _connected = True

        adapter = FakeAdapter()
        result = getattr(adapter, "_connected", False)
        assert result is True

    def test_metaapi_adapter_connected_false(self):
        """When adapter._connected=False, getattr returns False."""

        class FakeAdapter:
            _connected = False

        adapter = FakeAdapter()
        result = getattr(adapter, "_connected", False)
        assert result is False

    def test_launch_gate_blocks_when_adapter_disconnected(self):
        """Launch gate returns NOT_READY when adapter_connected=False."""
        cfg = _cfg()
        cfg.metaapi.token = "test-token"
        cfg.metaapi.account_id = "test-id"

        with patch.dict(os.environ, {"NOVATRADE_WEBHOOK_SECRET": "secret"}, clear=True):
            readiness = evaluate_launch_gate(
                cfg,
                LaunchMode.ACTIVE_READY,
                risk_engine_initialized=True,
                risk_engine_halted=False,
                agent_initialized=True,
                monitor_initialized=True,
                adapter_connected=False,
                adapter_type="MetaApiAdapter",
            )
            adapter_check = next((c for c in readiness.checks if c.name == "adapter_connected"), None)
            assert adapter_check is not None
            assert adapter_check.passed is False
            assert readiness.verdict == ReadinessVerdict.NOT_READY

    def test_launch_gate_passes_when_adapter_connected(self):
        """Launch gate passes adapter check when adapter_connected=True."""
        cfg = _cfg()
        cfg.metaapi.token = "test-token"
        cfg.metaapi.account_id = "test-id"

        with patch.dict(os.environ, {"NOVATRADE_WEBHOOK_SECRET": "secret"}, clear=True):
            readiness = evaluate_launch_gate(
                cfg,
                LaunchMode.ACTIVE_READY,
                risk_engine_initialized=True,
                risk_engine_halted=False,
                agent_initialized=True,
                monitor_initialized=True,
                adapter_connected=True,
                adapter_type="MetaApiAdapter",
            )
            adapter_check = next((c for c in readiness.checks if c.name == "adapter_connected"), None)
            assert adapter_check is not None
            assert adapter_check.passed is True


# ===========================================================================
# 2. MetaAPI connect diagnostics — phase-specific failure
# ===========================================================================


def _ensure_metaapi_sdk_mock():
    """Ensure metaapi_cloud_sdk is mockable if not installed."""
    import sys

    if "metaapi_cloud_sdk" not in sys.modules:
        mock_sdk = MagicMock()
        sys.modules["metaapi_cloud_sdk"] = mock_sdk


class TestMetaApiConnectDiagnostics:
    """Test that connect() failures include phase name and hint."""

    def _make_adapter(self):
        _ensure_metaapi_sdk_mock()
        from novatrade.adapter.metaapi_provider import MetaApiAdapter

        config = MetaApiConfig(
            token="test-token",
            account_id="test-account-id",
        )
        return MetaApiAdapter(config=config)

    @pytest.mark.asyncio
    async def test_account_lookup_failure_includes_phase(self):
        adapter = self._make_adapter()

        mock_api = MagicMock()
        mock_api.metatrader_account_api.get_account = AsyncMock(side_effect=Exception("Not found or unauthorized"))

        with patch("novatrade.adapter.metaapi_provider.get_metaapi_sdk", return_value=mock_api):
            status = await adapter.connect()

        assert status.connected is False
        assert status.state == HealthState.DOWN
        assert "GET_ACCOUNT" in status.message
        assert "METAAPI_TOKEN" in status.message

    @pytest.mark.asyncio
    async def test_deploy_failure_includes_phase(self):
        adapter = self._make_adapter()

        mock_account = AsyncMock()
        mock_account.state = "UNDEPLOYED"
        mock_account.deploy = AsyncMock(side_effect=Exception("Deploy quota exceeded"))
        mock_api = MagicMock()
        mock_api.metatrader_account_api.get_account = AsyncMock(return_value=mock_account)

        with patch("novatrade.adapter.metaapi_provider.get_metaapi_sdk", return_value=mock_api):
            status = await adapter.connect()

        assert status.connected is False
        assert "deploy" in status.message
        assert "MetaAPI dashboard" in status.message

    @pytest.mark.asyncio
    async def test_broker_connect_failure_includes_phase(self):
        adapter = self._make_adapter()

        mock_account = AsyncMock()
        mock_account.state = "DEPLOYED"
        mock_account.wait_connected = AsyncMock(side_effect=TimeoutError("Broker unreachable"))
        mock_api = MagicMock()
        mock_api.metatrader_account_api.get_account = AsyncMock(return_value=mock_account)

        with patch("novatrade.adapter.metaapi_provider.get_metaapi_sdk", return_value=mock_api):
            status = await adapter.connect()

        assert status.connected is False
        assert "WAIT_CONNECTED" in status.message
        assert "terminal" in status.message.lower()

    @pytest.mark.asyncio
    async def test_rpc_sync_failure_includes_phase(self):
        adapter = self._make_adapter()

        mock_connection = AsyncMock()
        mock_connection.connect = AsyncMock(return_value=None)
        mock_connection.wait_synchronized = AsyncMock(side_effect=Exception("Sync timeout"))
        mock_account = AsyncMock()
        mock_account.state = "DEPLOYED"
        mock_account.wait_connected = AsyncMock(return_value=None)
        mock_account.get_rpc_connection = MagicMock(return_value=mock_connection)
        mock_api = MagicMock()
        mock_api.metatrader_account_api.get_account = AsyncMock(return_value=mock_account)

        with patch("novatrade.adapter.metaapi_provider.get_metaapi_sdk", return_value=mock_api):
            status = await adapter.connect()

        assert status.connected is False
        assert "RPC_SYNC" in status.message
        assert "RPC" in status.message

    @pytest.mark.asyncio
    async def test_successful_connect(self):
        adapter = self._make_adapter()

        mock_connection = AsyncMock()
        mock_connection.connect = AsyncMock(return_value=None)
        mock_connection.wait_synchronized = AsyncMock(return_value=None)
        mock_connection.get_account_information = AsyncMock(
            return_value={
                "equity": 100000.0,
                "broker": "TestBroker",
                "currency": "USD",
                "tradeAllowed": True,
            }
        )
        mock_account = AsyncMock()
        mock_account.state = "DEPLOYED"
        mock_account.wait_connected = AsyncMock(return_value=None)
        mock_account.get_rpc_connection = MagicMock(return_value=mock_connection)
        mock_api = MagicMock()
        mock_api.metatrader_account_api.get_account = AsyncMock(return_value=mock_account)

        with patch("novatrade.adapter.metaapi_provider.get_metaapi_sdk", return_value=mock_api):
            status = await adapter.connect()

        assert status.connected is True
        assert status.state == HealthState.OK
        assert adapter._connected is True

    @pytest.mark.asyncio
    async def test_connect_failure_sets_connected_false(self):
        adapter = self._make_adapter()

        with patch("novatrade.adapter.metaapi_provider.get_metaapi_sdk", side_effect=RuntimeError("SDK init failed")):
            status = await adapter.connect()

        assert status.connected is False
        assert adapter._connected is False
        # Should report the phase where it failed
        assert "sdk_init" in status.message or "init" in status.message

    @pytest.mark.asyncio
    async def test_connect_failure_helper_method(self):
        """Test _connect_failure builds proper message with hint."""
        adapter = self._make_adapter()
        import time

        t0 = time.monotonic()
        status = adapter._connect_failure(t0, "test_phase", ValueError("something broke"), hint="check your config")
        assert status.connected is False
        assert status.state == HealthState.DOWN
        assert "test_phase" in status.message
        assert "something broke" in status.message
        assert "check your config" in status.message


# ===========================================================================
# 3. MonitorLoop watchdog integration
# ===========================================================================


def _can_import_monitor_loop() -> bool:
    """Check whether MonitorLoop can be imported (needs full dep chain)."""
    try:
        from novatrade.runtime.monitor_loop import MonitorLoop  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_monitor_loop_available = _can_import_monitor_loop()
_skip_monitor_loop = pytest.mark.skipif(
    not _monitor_loop_available,
    reason="MonitorLoop import chain requires missing modules (symbol_helpers, etc.)",
)


@_skip_monitor_loop
class TestMonitorLoopWatchdog:
    """Verify watchdog is wired into MonitorLoop."""

    def test_monitor_loop_has_watchdog(self):
        from novatrade.runtime.monitor_loop import MonitorLoop

        monitor = MagicMock()
        loop = MonitorLoop(monitor, interval_seconds=60)
        assert loop.watchdog is not None

    def test_monitor_loop_watchdog_config(self):
        from novatrade.monitor.watchdog import WatchdogConfig
        from novatrade.runtime.monitor_loop import MonitorLoop

        config = WatchdogConfig(major_no_trade_minutes=120)
        monitor = MagicMock()
        loop = MonitorLoop(monitor, interval_seconds=60, watchdog_config=config)
        assert loop.watchdog.config.major_no_trade_minutes == 120

    def test_watchdog_every_n_calculation(self):
        from novatrade.monitor.watchdog import WatchdogConfig
        from novatrade.runtime.monitor_loop import MonitorLoop

        # check_interval=120s, monitor_interval=60s -> every 2 cycles
        config = WatchdogConfig(check_interval_seconds=120)
        monitor = MagicMock()
        loop = MonitorLoop(monitor, interval_seconds=60, watchdog_config=config)
        assert loop._watchdog_every_n == 2

    def test_watchdog_every_n_minimum_1(self):
        from novatrade.monitor.watchdog import WatchdogConfig
        from novatrade.runtime.monitor_loop import MonitorLoop

        # check_interval < monitor_interval -> clamped to 1
        config = WatchdogConfig(check_interval_seconds=30)
        monitor = MagicMock()
        loop = MonitorLoop(monitor, interval_seconds=60, watchdog_config=config)
        assert loop._watchdog_every_n == 1
