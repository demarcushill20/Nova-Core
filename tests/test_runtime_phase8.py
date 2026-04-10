"""Tests for Phase 8 — runtime caller, webhook ingress, dry-run, end-to-end."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.execution.trading_agent import AgentState, TradingAgent
from novatrade.models import (
    AccountMode,
    AccountState,
    HealthState,
    OrderSide,
    OrderStatus,
    OrderType,
)
from novatrade.monitor.ops_monitor import OpsMonitor
from novatrade.risk.risk_engine import RiskEngine
from novatrade.runtime.dry_run import DryRunAdapter
from novatrade.runtime.monitor_loop import MonitorLoop
from novatrade.runtime.webhook_server import WebhookState, build_status, create_app
from novatrade.validation.evidence import EvidenceRecorder

# ---------------------------------------------------------------------------
# Fixtures
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
    )
    defaults.update(overrides)
    return NovaTradeCfg(**defaults)  # type: ignore[arg-type]


def _make_irb_signal(
    *,
    action: str = "PLACE_STOP_ORDER",
    side: str = "BUY",
    entry_price: float = 1.08550,
    stop_loss: float = 1.08350,
    volume: float = 0.10,
    bar_close_time: int = 1710000000000,
) -> dict:
    """Build a valid IRB signal alert payload."""
    return {
        "strategy_name": "Rob Hoffman IRB",
        "strategy_version": "5.0.0",
        "action": action,
        "signal_type": "LONG_IRB" if side == "BUY" else "SHORT_IRB",
        "irb_type": "UPTREND_IRB" if side == "BUY" else "DOWNTREND_IRB",
        "symbol": "EURUSD",
        "broker_symbol": "EURUSD.sim",
        "timeframe": "H1",
        "side": side,
        "order_type": "BUY_STOP" if side == "BUY" else "SELL_STOP",
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "stop_distance_pips": abs(entry_price - stop_loss) * 10000,
        "volume": volume,
        "risk_dollars": 1000.0,
        "bar_close_time": bar_close_time,
        "bar_ohlc_o": 1.08500,
        "bar_ohlc_h": 1.08600,
        "bar_ohlc_l": 1.08400,
        "bar_ohlc_c": 1.08550,
        "irb_range": 0.00200,
        "ema_20_h1": 1.08500,
        "ema_slope": 0.5,
        "ema_20_h4": 1.08400,
        "ema_20_h4_dir": "RISING",
        "adx_14": 25.0,
        "atr_14": 0.00150,
        "overextension_ratio": 1.33,
        "trigger_window_bars": 20,
        "strategy_state": "PENDING_LONG" if side == "BUY" else "PENDING_SHORT",
        "campaign": "ftmo-free-trial-march-2026",
    }


def _make_cancel_alert(side: str = "BUY") -> dict:
    return {
        "strategy_name": "Rob Hoffman IRB",
        "strategy_version": "5.0.0",
        "action": "CANCEL_ORDER",
        "symbol": "EURUSD",
        "side": side,
        "cancel_reason": "TRIGGER_WINDOW_EXPIRED",
        "bars_elapsed": 20,
        "campaign": "ftmo-free-trial-march-2026",
    }


def _make_close_alert(side: str = "BUY") -> dict:
    return {
        "strategy_name": "Rob Hoffman IRB",
        "strategy_version": "5.0.0",
        "action": "CLOSE_POSITION",
        "symbol": "EURUSD",
        "side": side,
        "close_reason": "TIME_STOP",
        "bars_held": 40,
        "close_price": 1.08700,
        "campaign": "ftmo-free-trial-march-2026",
    }


def _make_trail_alert(side: str = "BUY") -> dict:
    return {
        "strategy_name": "Rob Hoffman IRB",
        "strategy_version": "5.0.0",
        "action": "MODIFY_SL",
        "symbol": "EURUSD",
        "side": side,
        "old_stop": 1.08350,
        "new_stop": 1.08400,
        "best_close": 1.08700,
        "atr_14": 0.00150,
        "bars_since_entry": 5,
        "campaign": "ftmo-free-trial-march-2026",
    }


def _build_stack(tmp_path: Path, **cfg_overrides):
    """Build a full test stack with DryRunAdapter.

    The DryRunAdapter itself provides the safety net — no real
    broker operations occur.
    """
    cfg = _cfg(**cfg_overrides)
    adapter = DryRunAdapter()
    recorder = EvidenceRecorder(path=tmp_path / "evidence.jsonl")
    risk_engine = RiskEngine(cfg)
    risk_engine.initialize(AccountState(balance=100_000, equity=100_000, mode=AccountMode.DEMO))
    agent = TradingAgent(cfg=cfg, adapter=adapter, risk_engine=risk_engine, recorder=recorder)
    monitor = OpsMonitor(
        cfg=cfg,
        adapter=adapter,
        agent=agent,
        risk_engine=risk_engine,
        recorder=recorder,
    )
    ws = WebhookState(
        agent=agent,
        risk_engine=risk_engine,
        monitor=monitor,
        recorder=recorder,
        started_at=time.time(),
    )
    return ws, adapter, agent, risk_engine, monitor, recorder


# ===========================================================================
# 1. DryRunAdapter tests
# ===========================================================================


class TestDryRunAdapter:
    """Tests for the dry-run adapter stub."""

    @pytest.mark.asyncio
    async def test_connect(self):
        adapter = DryRunAdapter()
        status = await adapter.connect()
        assert status.state == HealthState.OK
        assert status.connected is True

    @pytest.mark.asyncio
    async def test_health_check(self):
        adapter = DryRunAdapter()
        status = await adapter.health_check()
        assert status.state == HealthState.OK

    @pytest.mark.asyncio
    async def test_get_account(self):
        adapter = DryRunAdapter()
        account = await adapter.get_account()
        assert account.equity == 100_000.0
        assert account.mode == AccountMode.DEMO

    @pytest.mark.asyncio
    async def test_place_order_returns_pending(self):
        adapter = DryRunAdapter()
        from novatrade.models import OrderRequest

        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.STOP,
            volume=0.10,
            price=1.08550,
            stop_loss=1.08350,
        )
        result = await adapter.place_order(req)
        assert result.ok is True
        assert result.order_id.startswith("DRY-")
        assert result.status == OrderStatus.PENDING

    @pytest.mark.asyncio
    async def test_place_order_tracks_pending(self):
        adapter = DryRunAdapter()
        from novatrade.models import OrderRequest

        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.STOP,
            volume=0.10,
            price=1.08550,
            stop_loss=1.08350,
        )
        result = await adapter.place_order(req)
        orders = await adapter.get_orders()
        assert len(orders) == 1
        assert orders[0].order_id == result.order_id

    @pytest.mark.asyncio
    async def test_cancel_order(self):
        adapter = DryRunAdapter()
        from novatrade.models import OrderRequest

        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.STOP,
            volume=0.10,
            price=1.08550,
            stop_loss=1.08350,
        )
        result = await adapter.place_order(req)
        cancel = await adapter.cancel_order(result.order_id)
        assert cancel.ok is True
        orders = await adapter.get_orders()
        assert len(orders) == 0

    @pytest.mark.asyncio
    async def test_simulate_fill(self):
        adapter = DryRunAdapter()
        from novatrade.models import OrderRequest

        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.STOP,
            volume=0.10,
            price=1.08550,
            stop_loss=1.08350,
        )
        result = await adapter.place_order(req)
        ok = adapter.simulate_fill(result.order_id, fill_price=1.08550)
        assert ok is True
        positions = await adapter.get_positions()
        assert len(positions) == 1
        assert positions[0].position_id == result.order_id

    @pytest.mark.asyncio
    async def test_simulate_broker_close(self):
        adapter = DryRunAdapter()
        from novatrade.models import OrderRequest

        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.STOP,
            volume=0.10,
            price=1.08550,
            stop_loss=1.08350,
        )
        result = await adapter.place_order(req)
        adapter.simulate_fill(result.order_id)
        ok = adapter.simulate_broker_close(result.order_id)
        assert ok is True
        positions = await adapter.get_positions()
        assert len(positions) == 0

    @pytest.mark.asyncio
    async def test_close_position(self):
        adapter = DryRunAdapter()
        result = await adapter.close_position("pos1")
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_modify_order(self):
        adapter = DryRunAdapter()
        result = await adapter.modify_order("ord1", stop_loss=1.08400)
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_get_symbol_price(self):
        adapter = DryRunAdapter()
        price = await adapter.get_symbol_price("EURUSD")
        assert price.symbol == "EURUSD"
        assert price.bid > 0

    @pytest.mark.asyncio
    async def test_dry_adapter_property(self):
        adapter = DryRunAdapter()
        assert adapter.dry_run is True

    @pytest.mark.asyncio
    async def test_reset(self):
        adapter = DryRunAdapter()
        from novatrade.models import OrderRequest

        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.STOP,
            volume=0.10,
            price=1.08550,
        )
        await adapter.place_order(req)
        adapter.reset()
        orders = await adapter.get_orders()
        assert len(orders) == 0


# ===========================================================================
# 2. Webhook server tests
# ===========================================================================


class TestWebhookServer:
    """Tests for the webhook ingress."""

    def test_health_endpoint(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_status_endpoint(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["trading_agent"]["state"] == "FLAT"
        assert data["risk_engine"]["halted"] is False

    def test_valid_signal_accepted(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        payload = _make_irb_signal()
        resp = client.post("/webhook/alert", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["state"] == "PENDING_LONG"

    def test_empty_body_rejected(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        resp = client.post("/webhook/alert", content=b"")
        assert resp.status_code == 400

    def test_malformed_json_rejected(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        resp = client.post(
            "/webhook/alert",
            content=b"{not valid json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_non_object_payload_rejected(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        resp = client.post("/webhook/alert", json=[1, 2, 3])
        assert resp.status_code == 400

    def test_webhook_secret_enforcement(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        ws.webhook_secret = "test-secret-123"
        app = create_app(ws)
        client = TestClient(app)
        payload = _make_irb_signal()

        # Missing secret
        resp = client.post("/webhook/alert", json=payload)
        assert resp.status_code == 403

        # Wrong secret
        resp = client.post(
            "/webhook/alert",
            json=payload,
            headers={"X-Webhook-Secret": "wrong"},
        )
        assert resp.status_code == 403

        # Correct secret
        resp = client.post(
            "/webhook/alert",
            json=payload,
            headers={"X-Webhook-Secret": "test-secret-123"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_duplicate_alert_suppressed(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        payload = _make_irb_signal()

        # First alert succeeds
        resp1 = client.post("/webhook/alert", json=payload)
        assert resp1.json()["ok"] is True

        # Same alert (duplicate) is rejected by idempotency
        resp2 = client.post("/webhook/alert", json=payload)
        data = resp2.json()
        assert data["ok"] is False  # duplicate suppressed

    def test_alert_counter_increments(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        payload = _make_irb_signal()
        client.post("/webhook/alert", json=payload)
        assert ws.alerts_received == 1
        assert ws.alerts_processed == 1

    def test_trigger_daily_summary(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        resp = client.post("/control/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "path" in data

    def test_no_agent_returns_503(self):
        ws = WebhookState()
        app = create_app(ws)
        client = TestClient(app)
        resp = client.post("/webhook/alert", json={"action": "test"})
        assert resp.status_code == 503


# ===========================================================================
# 3. MonitorLoop tests
# ===========================================================================


class TestMonitorLoop:
    """Tests for the monitoring loop caller."""

    @pytest.mark.asyncio
    async def test_run_once(self, tmp_path):
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        loop = MonitorLoop(monitor, interval_seconds=1, recorder=recorder)
        result = await loop.run_once()
        assert result is not None
        assert result.health_ok is True
        assert loop.stats.cycles_run == 1
        assert loop.stats.cycles_ok == 1

    @pytest.mark.asyncio
    async def test_max_cycles(self, tmp_path):
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        loop = MonitorLoop(
            monitor,
            interval_seconds=0.01,
            recorder=recorder,
            max_cycles=3,
        )
        await loop.start()
        assert loop.stats.cycles_run == 3
        assert loop.stats.cycles_ok == 3
        assert loop.running is False

    @pytest.mark.asyncio
    async def test_stop(self, tmp_path):
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        loop = MonitorLoop(monitor, interval_seconds=0.01, recorder=recorder)

        async def stop_after_delay():
            await asyncio.sleep(0.05)
            loop.stop()

        task = asyncio.create_task(stop_after_delay())
        await loop.start()
        await task
        assert loop.running is False
        assert loop.stats.cycles_run >= 1

    @pytest.mark.asyncio
    async def test_cycle_error_does_not_kill_loop(self, tmp_path):
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        loop = MonitorLoop(
            monitor,
            interval_seconds=0.01,
            recorder=recorder,
            max_cycles=3,
        )

        # Patch run_cycle to fail on the second call
        original = monitor.run_cycle
        call_count = 0

        async def flaky_cycle():
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("simulated failure")
            return await original()

        monitor.run_cycle = flaky_cycle
        await loop.start()
        assert loop.stats.cycles_run == 3
        assert loop.stats.cycles_failed == 1
        assert loop.stats.cycles_ok == 2

    @pytest.mark.asyncio
    async def test_stats_to_dict(self, tmp_path):
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        loop = MonitorLoop(
            monitor,
            interval_seconds=0.01,
            recorder=recorder,
            max_cycles=2,
        )
        await loop.start()
        stats = loop.stats.to_dict()
        assert stats["cycles_run"] == 2
        assert stats["cycles_ok"] == 2
        assert stats["avg_cycle_ms"] > 0

    @pytest.mark.asyncio
    async def test_evidence_recorded(self, tmp_path):
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        loop = MonitorLoop(
            monitor,
            interval_seconds=0.01,
            recorder=recorder,
            max_cycles=1,
        )
        await loop.start()
        records = recorder.load()
        events = [r.data.get("event") for r in records]
        assert "MONITOR_LOOP_STARTED" in events
        assert "MONITOR_CYCLE_COMPLETE" in events
        assert "MONITOR_LOOP_STOPPED" in events


# ===========================================================================
# 4. End-to-end dry-run wiring tests
# ===========================================================================


class TestEndToEndDryRun:
    """End-to-end tests proving the full stack works in dry-run."""

    def test_signal_through_full_stack(self, tmp_path):
        """Valid IRB signal → webhook → agent → risk → dry-run adapter → evidence."""
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)

        payload = _make_irb_signal()
        resp = client.post("/webhook/alert", json=payload)
        data = resp.json()

        assert data["ok"] is True
        assert data["state"] == "PENDING_LONG"
        assert agent.state == AgentState.PENDING_LONG

        # Evidence trail should have the webhook events
        records = recorder.load()
        events = [r.data.get("event") for r in records if "event" in r.data]
        assert "WEBHOOK_RECEIVED" in events
        assert "WEBHOOK_ROUTED" in events

    def test_risk_denial_path(self, tmp_path):
        """Risk engine halt blocks order placement."""
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        risk._halt("test halt")
        app = create_app(ws)
        client = TestClient(app)

        payload = _make_irb_signal()
        resp = client.post("/webhook/alert", json=payload)
        data = resp.json()

        assert data["ok"] is False
        assert agent.state == AgentState.FLAT

    def test_risk_halt_in_status(self, tmp_path):
        """Status endpoint reflects risk halt."""
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        risk._halt("drawdown breach")
        app = create_app(ws)
        client = TestClient(app)

        resp = client.get("/status")
        data = resp.json()
        assert data["risk_engine"]["halted"] is True
        assert data["risk_engine"]["halt_reason"] == "drawdown breach"

    def test_cancel_alert_path(self, tmp_path):
        """Place order then cancel it."""
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)

        # Place
        resp = client.post("/webhook/alert", json=_make_irb_signal())
        assert resp.json()["ok"] is True
        assert agent.state == AgentState.PENDING_LONG

        # Cancel
        resp = client.post("/webhook/alert", json=_make_cancel_alert())
        assert resp.json()["ok"] is True
        assert agent.state == AgentState.FLAT

    @pytest.mark.asyncio
    async def test_monitor_cycle_after_signal(self, tmp_path):
        """After placing an order, monitor cycle runs without error."""
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)

        # Place signal
        client.post("/webhook/alert", json=_make_irb_signal())
        assert agent.state == AgentState.PENDING_LONG

        # Run monitor cycle
        loop = MonitorLoop(monitor, recorder=recorder, max_cycles=1)
        await loop.run_once()
        assert loop.stats.cycles_ok == 1

    @pytest.mark.asyncio
    async def test_fill_detection_via_dry_adapter(self, tmp_path):
        """Simulate fill at adapter → reconciliation detects it."""
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)

        # Place signal → agent is PENDING_LONG
        client.post("/webhook/alert", json=_make_irb_signal())
        assert agent.state == AgentState.PENDING_LONG
        order_id = agent.pending_order_id

        # Simulate fill at the adapter level
        adapter.simulate_fill(order_id, fill_price=1.08550)

        # Monitor cycle should detect the fill
        result = await monitor.run_cycle()
        assert agent.state == AgentState.LONG

        # Check reconciliation detected the fill
        fill_actions = [a for a in result.reconciliation_actions if a.outcome.value == "NOTIFY_FILL"]
        assert len(fill_actions) == 1

    @pytest.mark.asyncio
    async def test_broker_close_detection_via_dry_adapter(self, tmp_path):
        """Simulate broker close → reconciliation detects it."""
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)

        # Place and fill
        client.post("/webhook/alert", json=_make_irb_signal())
        order_id = agent.pending_order_id
        adapter.simulate_fill(order_id, fill_price=1.08550)
        await monitor.run_cycle()
        assert agent.state == AgentState.LONG

        # Simulate broker close
        adapter.simulate_broker_close(order_id)

        # Monitor cycle should detect the close
        result = await monitor.run_cycle()
        assert agent.state == AgentState.FLAT

        close_actions = [a for a in result.reconciliation_actions if a.outcome.value == "NOTIFY_BROKER_CLOSE"]
        assert len(close_actions) == 1

    @pytest.mark.asyncio
    async def test_daily_summary_generation(self, tmp_path):
        """Daily summary can be generated and written."""
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)

        # Run a cycle first
        await monitor.run_cycle()

        summary = monitor.generate_daily_summary()
        path = monitor.write_daily_summary(summary, tmp_path)
        assert path.exists()

        content = json.loads(path.read_text())
        assert "date" in content
        assert "cycles_run" in content

    def test_malformed_payload_rejected(self, tmp_path):
        """Payloads missing required fields are rejected."""
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)

        # Missing strategy_name and other required fields
        resp = client.post("/webhook/alert", json={"action": "PLACE_STOP_ORDER"})
        data = resp.json()
        assert data["ok"] is False

    def test_evidence_trail_complete(self, tmp_path):
        """Evidence file contains all expected events."""
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)

        # Process a signal
        client.post("/webhook/alert", json=_make_irb_signal())

        records = recorder.load()
        assert len(records) > 0

        # Should have at least webhook events + execution events
        event_types = {r.event_type.value for r in records}
        assert "MONITORING" in event_types or "EXECUTION" in event_types


# ===========================================================================
# 5. Failure injection tests
# ===========================================================================


class TestFailureInjection:
    """Negative-path and failure injection tests."""

    def test_invalid_strategy_name(self, tmp_path):
        """Wrong strategy name is rejected."""
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        payload = _make_irb_signal()
        payload["strategy_name"] = "Wrong Strategy"
        resp = client.post("/webhook/alert", json=payload)
        data = resp.json()
        assert data["ok"] is False

    def test_invalid_strategy_version(self, tmp_path):
        """Wrong strategy version is rejected."""
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        payload = _make_irb_signal()
        payload["strategy_version"] = "1.0.0"
        resp = client.post("/webhook/alert", json=payload)
        data = resp.json()
        assert data["ok"] is False

    def test_wrong_state_for_action(self, tmp_path):
        """Cancel when FLAT is rejected."""
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)

        # Cancel with no pending order
        resp = client.post("/webhook/alert", json=_make_cancel_alert())
        data = resp.json()
        assert data["ok"] is False

    def test_close_when_no_position(self, tmp_path):
        """Close when FLAT is rejected."""
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        resp = client.post("/webhook/alert", json=_make_close_alert())
        data = resp.json()
        assert data["ok"] is False

    def test_modify_sl_when_flat(self, tmp_path):
        """Trail SL when FLAT is rejected."""
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)
        resp = client.post("/webhook/alert", json=_make_trail_alert())
        data = resp.json()
        assert data["ok"] is False

    def test_signal_while_pending(self, tmp_path):
        """New signal while already PENDING is rejected."""
        ws, *_ = _build_stack(tmp_path)
        app = create_app(ws)
        client = TestClient(app)

        # Place first signal
        resp = client.post("/webhook/alert", json=_make_irb_signal())
        assert resp.json()["ok"] is True

        # Try another signal with different bar time (not a duplicate)
        payload2 = _make_irb_signal(bar_close_time=1710003600000)
        resp = client.post("/webhook/alert", json=payload2)
        data = resp.json()
        # Should be rejected (already PENDING_LONG, can't place new order)
        assert data["ok"] is False

    @pytest.mark.asyncio
    async def test_monitor_survives_adapter_error(self, tmp_path):
        """Monitor loop handles adapter errors gracefully."""
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)

        # Make health_check raise an error
        async def failing_health():
            raise ConnectionError("simulated connection loss")

        adapter.health_check = failing_health
        loop = MonitorLoop(
            monitor,
            interval_seconds=0.01,
            recorder=recorder,
            max_cycles=1,
        )
        await loop.run_once()
        # Cycle should complete (health degraded) but loop continues
        assert loop.stats.cycles_run == 1

    @pytest.mark.asyncio
    async def test_orphan_pending_detected(self, tmp_path):
        """Agent FLAT but adapter has pending order → orphan detected."""
        ws, adapter, agent, risk, monitor, recorder = _build_stack(tmp_path)

        # Inject an orphan pending order at the adapter
        from novatrade.models import OrderRequest

        req = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.STOP,
            volume=0.10,
            price=1.08550,
        )
        await adapter.place_order(req)

        # Agent is FLAT, but adapter has a pending order
        assert agent.state == AgentState.FLAT

        result = await monitor.run_cycle()
        orphan_actions = [a for a in result.reconciliation_actions if a.outcome.value == "ORPHAN_PENDING"]
        assert len(orphan_actions) == 1


# ===========================================================================
# 6. Build status tests
# ===========================================================================


class TestBuildStatus:
    """Tests for the status builder."""

    def test_status_structure(self, tmp_path):
        ws, *_ = _build_stack(tmp_path)
        status = build_status(ws)
        assert "runtime_mode" in status
        assert "trading_agent" in status
        assert "risk_engine" in status
        assert "ops_monitor" in status
        assert "webhook" in status

    def test_status_with_no_components(self):
        ws = WebhookState()
        status = build_status(ws)
        assert status["trading_agent"]["state"] == "not_initialized"

    def test_status_active_mode(self, tmp_path):
        from novatrade.runtime.launch_gate import LaunchMode

        ws, *_ = _build_stack(tmp_path)
        ws.launch_mode = LaunchMode.ACTIVE_READY
        status = build_status(ws)
        assert status["runtime_mode"] == "active_ready"


# ===========================================================================
# 7. Runner build_stack tests
# ===========================================================================


class TestRunnerBuildStack:
    """Tests for the runner.build_stack() factory."""

    @pytest.mark.asyncio
    async def test_build_stack_active_demo(self):
        from novatrade.runtime.launch_gate import LaunchMode
        from novatrade.runtime.runner import build_stack

        cfg = _cfg()
        cfg.metaapi.token = "test-token"
        cfg.metaapi.account_id = "test-id"
        confirm_env = {
            "NOVATRADE_WEBHOOK_SECRET": "secret",
            "NOVATRADE_CONFIRM_PINE_COMPILED": "true",
            "NOVATRADE_CONFIRM_TV_BACKTEST": "true",
            "NOVATRADE_CONFIRM_WEBHOOK_URL": "true",
            "NOVATRADE_CONFIRM_ACTIVE_DEMO": "true",
        }
        with (
            patch.dict(os.environ, confirm_env, clear=True),
            patch("novatrade.runtime.runner._create_adapter") as mock_create,
            patch("novatrade.runtime.runner._adapter_type_name", return_value="MetaApiAdapter"),
        ):
            mock_adapter = DryRunAdapter()
            mock_create.return_value = mock_adapter
            ws, loop, readiness = await build_stack(cfg, mode=LaunchMode.ACTIVE_DEMO)
        assert ws.agent is not None
        assert ws.risk_engine is not None
        assert ws.monitor is not None
        assert ws.recorder is not None
        assert loop is not None

    @pytest.mark.asyncio
    async def test_build_stack_active_demo_fails_without_credentials(self):
        """ACTIVE_DEMO mode without credentials raises RuntimeError."""
        from novatrade.runtime.launch_gate import LaunchMode
        from novatrade.runtime.runner import build_stack

        cfg = _cfg()
        with pytest.raises(RuntimeError, match="METAAPI_TOKEN"):
            await build_stack(cfg, mode=LaunchMode.ACTIVE_DEMO)
