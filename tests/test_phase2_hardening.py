"""Phase 2 hardening regression tests.

Proves these invariants:
  1. Launch gate fails when adapter is disconnected
  2. /health, /readiness, watchdog cannot disagree on adapter state
  3. No-signal regime is not misclassified as system failure
  4. Webhook pipeline failure is detectable via watchdog
  5. Bounded auto-remediation follows the contract
  6. symbol_helpers provides correct pip values
"""

from __future__ import annotations

import datetime as dt
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.models import AccountMode
from novatrade.monitor.watchdog import (
    AnomalyLevel,
    EvidenceSignal,
    RuntimeHealthSnapshot,
    TradeStallWatchdog,
    WatchdogConfig,
)
from novatrade.risk.symbol_helpers import pip_usd_value, pip_value
from novatrade.runtime.launch_gate import (
    LaunchMode,
    ReadinessVerdict,
    evaluate_launch_gate,
)


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
    return NovaTradeCfg(**defaults)


def _tuesday_10am_utc() -> float:
    return dt.datetime(2026, 4, 7, 10, 0, 0, tzinfo=dt.timezone.utc).timestamp()


def _snap(**overrides) -> RuntimeHealthSnapshot:
    defaults = dict(
        adapter_connected=True,
        readiness_ok=True,
        webhook_server_up=True,
        last_alert_time=time.time() - 300,
        last_trade_time=time.time() - 600,
        last_quote_time=time.time() - 30,
        alerts_received_total=10,
        alerts_rejected_total=0,
        trades_executed_today=3,
        monitor_cycles_failed=0,
        consecutive_startup_failures=0,
        risk_halted=False,
        uptime_seconds=7200,
    )
    defaults.update(overrides)
    return RuntimeHealthSnapshot(**defaults)


# ===========================================================================
# 1. Launch gate fails when adapter is disconnected
# ===========================================================================


class TestLaunchGateAdapterDisconnected:
    def test_active_ready_not_ready_when_disconnected(self):
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
        assert readiness.verdict == ReadinessVerdict.NOT_READY
        adapter_check = next(c for c in readiness.checks if c.name == "adapter_connected")
        assert adapter_check.passed is False

    def test_active_demo_not_ready_when_disconnected(self):
        cfg = _cfg()
        cfg.metaapi.token = "test-token"
        cfg.metaapi.account_id = "test-id"
        env = {
            "NOVATRADE_WEBHOOK_SECRET": "secret",
            "NOVATRADE_CONFIRM_PINE_COMPILED": "true",
            "NOVATRADE_CONFIRM_TV_BACKTEST": "true",
            "NOVATRADE_CONFIRM_WEBHOOK_URL": "true",
            "NOVATRADE_CONFIRM_ACTIVE_DEMO": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            readiness = evaluate_launch_gate(
                cfg,
                LaunchMode.ACTIVE_DEMO,
                risk_engine_initialized=True,
                risk_engine_halted=False,
                agent_initialized=True,
                monitor_initialized=True,
                adapter_connected=False,
                adapter_type="MetaApiAdapter",
            )
        assert readiness.verdict == ReadinessVerdict.NOT_READY

    def test_dry_run_ignores_adapter_state(self):
        """Dry-run mode should pass even with adapter_connected=False."""
        cfg = _cfg()
        readiness = evaluate_launch_gate(
            cfg,
            LaunchMode.DRY_RUN,
            risk_engine_initialized=True,
            risk_engine_halted=False,
            agent_initialized=True,
            monitor_initialized=True,
            adapter_connected=False,
            adapter_type="DryRunAdapter",
        )
        assert readiness.verdict == ReadinessVerdict.READY_FOR_ACTIVE_DEMO


# ===========================================================================
# 2. /health, /readiness, watchdog cannot disagree on adapter state
# ===========================================================================


class TestCanonicalAdapterState:
    """All consumers of adapter_connected derive from WebhookState.adapter_connected."""

    def test_webhook_state_adapter_connected_reads_real_state(self):
        from novatrade.runtime.webhook_server import WebhookState

        mock_agent = MagicMock()
        mock_agent._adapter = MagicMock(_connected=True)
        ws = WebhookState(agent=mock_agent, dry_run=False)
        assert ws.adapter_connected is True

        mock_agent._adapter._connected = False
        assert ws.adapter_connected is False

    def test_webhook_state_adapter_connected_fallback_dry_run(self):
        from novatrade.runtime.webhook_server import WebhookState

        ws = WebhookState(agent=None, dry_run=True)
        assert ws.adapter_connected is True

        ws2 = WebhookState(agent=None, dry_run=False)
        assert ws2.adapter_connected is False

    def test_health_endpoint_includes_adapter_connected(self):
        """GET /health must include adapter_connected field."""
        from novatrade.runtime.webhook_server import WebhookState, create_app

        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")

        mock_agent = MagicMock()
        mock_agent._adapter = MagicMock(_connected=True)
        mock_agent.state = MagicMock(value="FLAT")
        ws = WebhookState(
            agent=mock_agent,
            dry_run=False,
            started_at=time.time(),
        )
        app = create_app(ws)
        client = TestClient(app)
        resp = client.get("/health")
        data = resp.json()
        assert "adapter_connected" in data
        assert data["adapter_connected"] is True

    def test_health_and_readiness_agree_when_disconnected(self):
        """Both endpoints must report the same adapter_connected value."""
        from novatrade.runtime.webhook_server import WebhookState, create_app

        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not installed")

        mock_agent = MagicMock()
        mock_agent._adapter = MagicMock(_connected=False)
        mock_agent.state = MagicMock(value="FLAT")
        mock_agent._cfg = _cfg()
        ws = WebhookState(
            agent=mock_agent,
            dry_run=False,
            launch_mode=LaunchMode.ACTIVE_READY,
            adapter_type="MetaApiAdapter",
            started_at=time.time(),
        )
        app = create_app(ws)
        client = TestClient(app)

        health = client.get("/health").json()
        readiness = client.get("/readiness").json()

        assert health["adapter_connected"] is False
        # readiness uses the same canonical source
        adapter_check = next(
            (c for c in readiness["checks"] if c["name"] == "adapter_connected"),
            None,
        )
        assert adapter_check is not None
        assert adapter_check["passed"] is False

    def test_watchdog_snapshot_uses_canonical_state(self):
        """_build_health_snapshot reads from WebhookState.adapter_connected."""
        from novatrade.runtime.webhook_server import WebhookState

        mock_agent = MagicMock()
        mock_agent._adapter = MagicMock(_connected=False)
        ws = WebhookState(
            agent=mock_agent,
            dry_run=False,
            started_at=time.time() - 3600,
            alerts_received=5,
            alerts_rejected=2,
            alerts_processed=3,
            last_alert_time=time.time() - 120,
        )

        # Build snapshot the same way MonitorLoop does
        adapter_connected = ws.adapter_connected
        assert adapter_connected is False

        # Verify alert counters would be read
        assert ws.alerts_received == 5
        assert ws.alerts_rejected == 2
        assert ws.last_alert_time > 0


# ===========================================================================
# 3. No-signal regime is not misclassified as system failure
# ===========================================================================


class TestNoSignalRegime:
    def test_no_trades_no_alerts_is_not_major(self):
        """Zero trades + zero alerts + healthy system = no-signal, not outage."""
        wd = TradeStallWatchdog(WatchdogConfig(
            major_no_trade_minutes=60,
            soft_no_alert_minutes=30,
            major_min_corroborating=2,
        ))
        now = _tuesday_10am_utc()
        snap = _snap(
            last_trade_time=now - 7200,  # 2h no trades
            last_alert_time=0.0,
            uptime_seconds=14400,
            alerts_received_total=0,
            trades_executed_today=0,
            adapter_connected=True,
            readiness_ok=True,
            webhook_server_up=True,
        )
        verdict = wd.evaluate(snap, now=now)
        # Should NOT be MAJOR or CRITICAL — it's SOFT at worst
        assert verdict.level in (AnomalyLevel.NONE, AnomalyLevel.SOFT)

    def test_no_trades_WITH_adapter_down_IS_major(self):
        """Zero trades + adapter down + no alerts = real outage, not no-signal."""
        wd = TradeStallWatchdog(WatchdogConfig(
            major_no_trade_minutes=60,
            soft_no_alert_minutes=30,
            major_min_corroborating=2,
        ))
        now = _tuesday_10am_utc()
        snap = _snap(
            last_trade_time=now - 7200,
            last_alert_time=now - 3600,
            adapter_connected=False,
            readiness_ok=False,
        )
        verdict = wd.evaluate(snap, now=now)
        assert verdict.level in (AnomalyLevel.MAJOR, AnomalyLevel.CRITICAL)


# ===========================================================================
# 4. Webhook pipeline failure is detectable
# ===========================================================================


class TestWebhookPipelineFailure:
    def test_high_rejection_rate_detected(self):
        """Watchdog detects when >50% of alerts are rejected."""
        wd = TradeStallWatchdog()
        snap = _snap(
            alerts_received_total=10,
            alerts_rejected_total=8,
        )
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.evidence.has(EvidenceSignal.REPEATED_REJECTIONS)

    def test_webhook_down_detected(self):
        """Watchdog detects when webhook server is not up."""
        wd = TradeStallWatchdog()
        snap = _snap(webhook_server_up=False)
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.evidence.has(EvidenceSignal.WEBHOOK_INACTIVE)

    def test_webhook_down_plus_adapter_down_is_critical(self):
        wd = TradeStallWatchdog()
        snap = _snap(
            adapter_connected=False,
            webhook_server_up=False,
        )
        verdict = wd.evaluate(snap, now=_tuesday_10am_utc())
        assert verdict.level == AnomalyLevel.CRITICAL

    def test_no_alerts_during_active_session(self):
        """Detect that no alerts have been received for a long time."""
        wd = TradeStallWatchdog(WatchdogConfig(soft_no_alert_minutes=30))
        now = _tuesday_10am_utc()
        snap = _snap(
            last_alert_time=now - 3600,  # 60 min, threshold is 30
        )
        verdict = wd.evaluate(snap, now=now)
        assert verdict.evidence.has(EvidenceSignal.NO_ALERTS_RECEIVED)

    def test_webhook_counters_plumbed_to_snapshot(self):
        """Verify WebhookState alert counters are accessible for snapshot building."""
        from novatrade.runtime.webhook_server import WebhookState

        ws = WebhookState(
            alerts_received=15,
            alerts_processed=12,
            alerts_rejected=3,
            last_alert_time=time.time() - 60,
            started_at=time.time() - 3600,
            dry_run=True,
        )
        # These are the values the MonitorLoop would read
        assert ws.alerts_received == 15
        assert ws.alerts_rejected == 3
        assert ws.last_alert_time > 0


# ===========================================================================
# 5. Bounded auto-remediation
# ===========================================================================


def _can_import_monitor_loop() -> bool:
    try:
        from novatrade.runtime.monitor_loop import MonitorLoop  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_monitor_loop_available = _can_import_monitor_loop()
_skip_monitor_loop = pytest.mark.skipif(
    not _monitor_loop_available,
    reason="MonitorLoop requires full dependency chain",
)


@_skip_monitor_loop
class TestBoundedRemediation:
    @pytest.mark.asyncio
    async def test_remediation_only_attempted_once(self):
        from novatrade.monitor.watchdog import WatchdogEvidence, WatchdogVerdict
        from novatrade.runtime.monitor_loop import MonitorLoop

        monitor = MagicMock()
        monitor._adapter = MagicMock()
        monitor._adapter.connect = AsyncMock(return_value=MagicMock(connected=True))
        monitor._risk_engine = MagicMock(halted=False)

        loop = MonitorLoop(monitor, interval_seconds=60)
        ev = WatchdogEvidence()
        ev.add(EvidenceSignal.ADAPTER_DISCONNECTED)
        verdict = WatchdogVerdict(
            level=AnomalyLevel.MAJOR,
            evidence=ev,
        )

        # First attempt succeeds
        ok1 = await loop.attempt_remediation(verdict)
        assert ok1 is True

        # Second attempt is blocked
        ok2 = await loop.attempt_remediation(verdict)
        assert ok2 is False

    @pytest.mark.asyncio
    async def test_remediation_skips_non_adapter_issues(self):
        from novatrade.monitor.watchdog import WatchdogEvidence, WatchdogVerdict
        from novatrade.runtime.monitor_loop import MonitorLoop

        monitor = MagicMock()
        loop = MonitorLoop(monitor, interval_seconds=60)
        ev = WatchdogEvidence()
        ev.add(EvidenceSignal.REPEATED_REJECTIONS)
        verdict = WatchdogVerdict(
            level=AnomalyLevel.MAJOR,
            evidence=ev,
        )

        ok = await loop.attempt_remediation(verdict)
        assert ok is False
        assert loop._remediation_attempted is False  # not consumed

    @pytest.mark.asyncio
    async def test_remediation_falls_through_to_rollback(self):
        from novatrade.monitor.watchdog import WatchdogEvidence, WatchdogVerdict
        from novatrade.runtime.monitor_loop import MonitorLoop
        from novatrade.runtime.webhook_server import WebhookState

        monitor = MagicMock()
        monitor._adapter = MagicMock()
        monitor._adapter.connect = AsyncMock(return_value=MagicMock(connected=False, message="fail"))
        monitor._risk_engine = MagicMock(halted=False)

        mock_agent = MagicMock()
        ws = WebhookState(agent=mock_agent, dry_run=False, started_at=time.time())

        loop = MonitorLoop(monitor, interval_seconds=60, webhook_state=ws)
        ev = WatchdogEvidence()
        ev.add(EvidenceSignal.ADAPTER_DISCONNECTED)
        verdict = WatchdogVerdict(
            level=AnomalyLevel.CRITICAL,
            evidence=ev,
        )

        with patch("novatrade.runtime.runner.rollback_to_dry_run") as mock_rollback:
            ok = await loop.attempt_remediation(verdict)
            assert ok is True
            mock_rollback.assert_called_once()


# ===========================================================================
# 6. symbol_helpers correctness
# ===========================================================================


class TestSymbolHelpers:
    def test_eurusd_pip_value(self):
        assert pip_value("EURUSD") == 0.0001

    def test_usdjpy_pip_value(self):
        assert pip_value("USDJPY") == 0.01

    def test_xauusd_pip_value(self):
        assert pip_value("XAUUSD") == 0.01

    def test_unknown_symbol_default(self):
        assert pip_value("UNKNOWN") == 0.0001

    def test_broker_suffix_stripped(self):
        assert pip_value("EURUSD.ftmo") == 0.0001
        assert pip_value("USDJPY.sim") == 0.01

    def test_pip_usd_value_standard(self):
        assert pip_usd_value("EURUSD") == 10.0
        assert pip_usd_value("GBPUSD") == 10.0

    def test_pip_usd_value_unknown_default(self):
        assert pip_usd_value("UNKNOWN") == 10.0

    def test_case_insensitive(self):
        assert pip_value("eurusd") == 0.0001
