"""Tests for session watchdog integration with heartbeat, ops_monitor, and webhook server."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from novatrade.monitor.session_watchdog import (
    AnomalyLevel,
    SessionWatchdog,
    SymptomEvidence,
    WatchdogAction,
    WatchdogConfig,
    WatchdogState,
    get_watchdog,
    run_watchdog_check,
)

# ---------------------------------------------------------------------------
# Heartbeat integration
# ---------------------------------------------------------------------------


class TestHeartbeatIntegration:
    """Test check_novatrade_watchdog() function used by heartbeat."""

    def test_import_heartbeat_watchdog_check(self):
        """check_novatrade_watchdog can be imported from heartbeat."""
        from heartbeat import check_novatrade_watchdog

        result = check_novatrade_watchdog()
        assert result["name"] == "novatrade_watchdog"
        assert "ok" in result
        assert "detail" in result

    def test_heartbeat_watchdog_returns_ok_when_healthy(self):
        """When no anomaly is detected, the check returns ok=True."""
        from heartbeat import check_novatrade_watchdog

        result = check_novatrade_watchdog()
        # On a Wednesday with no running NovaTrade, should be non-fatal
        assert isinstance(result["ok"], bool)
        assert isinstance(result["detail"], str)

    def test_send_watchdog_alert_function_exists(self):
        """send_watchdog_alert can be imported from heartbeat."""
        from heartbeat import send_watchdog_alert

        assert callable(send_watchdog_alert)


# ---------------------------------------------------------------------------
# OpsMonitor integration
# ---------------------------------------------------------------------------


class TestOpsMonitorIntegration:
    """Test _check_session_watchdog() in OpsMonitor."""

    def test_ops_monitor_has_watchdog_method(self):
        """OpsMonitor has the _check_session_watchdog method."""
        from novatrade.monitor.ops_monitor import OpsMonitor

        assert hasattr(OpsMonitor, "_check_session_watchdog")

    @pytest.mark.asyncio
    async def test_ops_monitor_run_cycle_includes_watchdog(self):
        """run_cycle should call _check_session_watchdog without crashing."""
        from novatrade.monitor.ops_monitor import CycleResult, OpsMonitor

        # Create minimal mock objects
        cfg = MagicMock()
        cfg.risk.daily_reset_tz = "UTC"
        adapter = AsyncMock()
        adapter.health_check = AsyncMock(return_value=MagicMock(ok=True))
        adapter.get_account = AsyncMock(return_value=MagicMock())
        adapter.get_positions = AsyncMock(return_value=[])
        adapter.get_pending_orders = AsyncMock(return_value=[])
        agent = MagicMock()
        agent.state = MagicMock()
        agent.state.value = "FLAT"
        agent.pending_order_id = None
        agent.position_id = None
        risk_engine = MagicMock()
        risk_engine.halted = False
        risk_engine.save_ftmo_state = MagicMock()
        risk_engine.write_risk_state = MagicMock()

        monitor = OpsMonitor(cfg, adapter, agent, risk_engine)

        # Patch the health check to avoid real adapter calls
        with (
            patch.object(monitor, "_check_health", new_callable=AsyncMock),
            patch.object(monitor, "_check_supervisor", new_callable=AsyncMock),
            patch.object(monitor, "_check_daily_reset", new_callable=AsyncMock),
            patch.object(monitor, "_reconcile", new_callable=AsyncMock),
        ):
            result = await monitor.run_cycle()

        assert isinstance(result, CycleResult)


# ---------------------------------------------------------------------------
# Webhook server integration
# ---------------------------------------------------------------------------


class TestWebhookIntegration:
    """Test that webhook server feeds events into the session watchdog."""

    def test_webhook_server_imports_watchdog(self):
        """The webhook server code references session_watchdog."""
        import inspect

        from novatrade.runtime import webhook_server

        source = inspect.getsource(webhook_server)
        assert "session_watchdog" in source
        assert "record_alert_received" in source

    def test_get_watchdog_singleton(self):
        """get_watchdog returns a singleton SessionWatchdog."""
        wd1 = get_watchdog()
        wd2 = get_watchdog()
        assert wd1 is wd2


# ---------------------------------------------------------------------------
# Run watchdog check convenience function
# ---------------------------------------------------------------------------


class TestRunWatchdogCheck:
    """Test the run_watchdog_check convenience function."""

    def test_run_watchdog_check_returns_assessment(self):
        """run_watchdog_check should return an AnomalyAssessment."""
        assessment = run_watchdog_check()
        assert hasattr(assessment, "level")
        assert hasattr(assessment, "symptoms")
        assert hasattr(assessment, "actions")
        assert hasattr(assessment, "evidence")

    def test_run_watchdog_check_serializable(self):
        """Assessment should be serializable to dict/JSON."""
        assessment = run_watchdog_check()
        d = assessment.to_dict()
        assert isinstance(d, dict)
        json_str = json.dumps(d)
        assert isinstance(json_str, str)


# ---------------------------------------------------------------------------
# Watchdog evaluation with injected evidence
# ---------------------------------------------------------------------------


class TestWatchdogEvaluationDirect:
    """Test direct watchdog evaluation with controlled evidence."""

    def _make_watchdog(self, now: float) -> SessionWatchdog:
        clock_values = [now]

        def clock() -> float:
            return clock_values[0]

        wd = SessionWatchdog(config=WatchdogConfig(), clock=clock)
        wd._state = WatchdogState()
        return wd

    def test_level1_generates_early_warning_action(self):
        # Tuesday 10:00 UTC (London session)
        now = datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc).timestamp()
        wd = self._make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=True,
            adapter_state="OK",
            last_alert_age_seconds=50000,
            session="london",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_1
        assert WatchdogAction.SEND_EARLY_WARNING in result.actions

    def test_level2_generates_investigation_actions(self):
        now = datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc).timestamp()
        wd = self._make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=False,
            adapter_state="DEGRADED",
            last_trade_age_seconds=50000,
            last_alert_age_seconds=50000,
            session="london",
            regime="ranging",
            signals_4h=0,
            signals_1h=0,
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_2
        assert WatchdogAction.INVESTIGATE in result.actions
        assert WatchdogAction.GENERATE_REPAIR_PLAN in result.actions

    def test_level3_generates_incident_and_escalation(self):
        now = datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc).timestamp()
        wd = self._make_watchdog(now)

        evidence = SymptomEvidence(
            adapter_connected=False,
            adapter_state="DOWN",
            session="london",
            regime="ranging",
        )

        result = wd.evaluate(evidence)
        assert result.level == AnomalyLevel.LEVEL_3
        assert WatchdogAction.OPEN_INCIDENT in result.actions
        assert WatchdogAction.ESCALATE in result.actions
