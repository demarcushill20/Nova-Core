"""Webhook ingress for TradingView-style alerts — Phase 8.

Minimal FastAPI application that receives JSON POST payloads from
TradingView (or a dry-run test harness), validates basic structure,
routes them to the Trading Agent, and records evidence.

Phase 8 owns:
  - HTTP POST /webhook/alert — alert ingress
  - HTTP GET /health — operator status
  - HTTP GET /status — detailed runtime status
  - HTTP POST /control/summary — trigger daily summary

Phase 8 does NOT own:
  - TLS termination (reverse proxy concern)
  - Authentication (Phase 8 uses shared secret; production needs more)
  - Load balancing
  - Dashboard UI
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request

from novatrade.execution.trading_agent import TradingAgent
from novatrade.models import EvidenceRecord, EvidenceType
from novatrade.monitor.ops_monitor import OpsMonitor
from novatrade.risk.hard_risk_supervisor import HardRiskSupervisor
from novatrade.risk.risk_engine import RiskEngine
from novatrade.runtime.launch_gate import LaunchMode
from novatrade.validation.evidence import EvidenceRecorder

log = logging.getLogger("novatrade.runtime.webhook")

# ---------------------------------------------------------------------------
# Webhook state container (injected at startup)
# ---------------------------------------------------------------------------


@dataclass
class WebhookState:
    """Shared state injected into the FastAPI app at startup.

    Canonical runtime state: ``adapter_connected`` is the single truth
    source for adapter connectivity.  All consumers (/health, /readiness,
    MonitorLoop, watchdog) MUST read from this property.
    """

    agent: TradingAgent | None = None
    risk_engine: RiskEngine | None = None
    monitor: OpsMonitor | None = None
    recorder: EvidenceRecorder | None = None
    supervisor: HardRiskSupervisor | None = None
    launch_mode: LaunchMode = LaunchMode.ACTIVE_DEMO
    adapter_type: str = "MetaApiAdapter"
    webhook_secret: str = ""
    started_at: float = 0.0
    alerts_received: int = 0
    alerts_processed: int = 0
    alerts_rejected: int = 0
    last_alert_time: float = 0.0

    @property
    def adapter_connected(self) -> bool:
        """Canonical adapter connectivity — single source of truth.

        Reads the real ``_connected`` flag from the adapter object.
        Falls back to ``dry_run`` when no adapter is available
        (DryRunAdapter is always considered "connected").
        """
        adapter = self.agent._adapter if self.agent else None
        return getattr(adapter, "_connected", False)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------


def create_app(state: WebhookState | None = None) -> FastAPI:
    """Create the webhook FastAPI application.

    If *state* is None, a default empty state is used (for testing).
    """
    app = FastAPI(
        title="NovaTrade Webhook Ingress",
        description="Phase 8 — TradingView alert receiver",
        version="8.0.0",
    )
    app.state.ws = state or WebhookState()

    # -- Alert ingress ---------------------------------------------------------

    @app.post("/webhook/alert")
    async def receive_alert(request: Request) -> dict:
        ws: WebhookState = request.app.state.ws
        ws.alerts_received += 1

        # --- Parse body first (needed for body-based secret check) ---
        try:
            body = await request.body()
            if not body:
                ws.alerts_rejected += 1
                _record_event(ws, "WEBHOOK_EMPTY_BODY", {})
                raise HTTPException(status_code=400, detail="empty request body")
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            ws.alerts_rejected += 1
            _record_event(ws, "WEBHOOK_MALFORMED_JSON", {"error": str(exc)[:200]})
            raise HTTPException(status_code=400, detail=f"malformed JSON: {exc}") from exc

        # --- Auth: check header first, fall back to body field ---
        if ws.webhook_secret:
            token = request.headers.get("X-Webhook-Secret", "")
            if not token and isinstance(payload, dict):
                token = payload.pop("webhook_secret", "")
            if token != ws.webhook_secret:
                ws.alerts_rejected += 1
                _record_event(ws, "WEBHOOK_AUTH_FAILED", {"reason": "bad_secret"})
                raise HTTPException(status_code=403, detail="invalid webhook secret")

        if not isinstance(payload, dict):
            ws.alerts_rejected += 1
            _record_event(ws, "WEBHOOK_NOT_OBJECT", {"type": type(payload).__name__})
            raise HTTPException(status_code=400, detail="payload must be a JSON object")

        # --- Route to Trading Agent ---
        if ws.agent is None:
            raise HTTPException(status_code=503, detail="trading agent not initialized")

        # Capture the load-bearing alert fields for downstream reconciliation
        # (scripts/diff_pine_alerts_vs_metaapi.py pairs alerts to MetaApi deals
        # by side/bar_close_time/volume/entry_price). Excludes webhook_secret.
        _record_event(
            ws,
            "WEBHOOK_RECEIVED",
            {
                "action": payload.get("action", "unknown"),
                "symbol": payload.get("symbol", "unknown"),
                "broker_symbol": payload.get("broker_symbol", ""),
                "side": payload.get("side", ""),
                "entry_price": payload.get("entry_price"),
                "stop_loss": payload.get("stop_loss"),
                "volume": payload.get("volume"),
                "bar_close_time": payload.get("bar_close_time"),
                "campaign": payload.get("campaign", ""),
                "strategy_version": payload.get("strategy_version", ""),
            },
        )

        result = await ws.agent.process_alert(payload)
        ws.last_alert_time = time.time()

        # Feed event into session watchdog for anomaly tracking
        try:
            from novatrade.monitor.session_watchdog import get_watchdog as _get_wd

            _wd = _get_wd()
            _wd.record_alert_received()
            if result.success:
                _wd.record_trade_executed()
        except Exception as exc:
            log.debug("Session watchdog update failed: %s", exc)

        if result.success:
            ws.alerts_processed += 1
        else:
            ws.alerts_rejected += 1

        _record_event(
            ws,
            "WEBHOOK_ROUTED",
            {
                "success": result.success,
                "state_before": result.state_before.value,
                "state_after": result.state_after.value,
                "rejected_reason": result.rejected_reason,
                "error": result.error,
            },
        )

        return {
            "ok": result.success,
            "state": result.state_after,
            "rejected_reason": result.rejected_reason or None,
            "error": result.error or None,
        }

    # -- Health endpoint -------------------------------------------------------

    @app.get("/health")
    async def health(request: Request) -> dict:
        ws: WebhookState = request.app.state.ws
        agent_state = ws.agent.state.value if ws.agent else "not_initialized"
        halted = ws.risk_engine.halted if ws.risk_engine else False
        sv_halted = ws.supervisor.halted if ws.supervisor else False
        return {
            "status": "ok" if ws.agent else "degraded",
            "launch_mode": ws.launch_mode.value,
            "adapter_type": ws.adapter_type,
            "adapter_connected": ws.adapter_connected,
            "agent_state": agent_state,
            "risk_halted": halted,
            "supervisor_halted": sv_halted,
            "uptime_seconds": time.time() - ws.started_at if ws.started_at else 0,
        }

    # -- Detailed status -------------------------------------------------------

    @app.get("/status")
    async def status(request: Request) -> dict:
        ws: WebhookState = request.app.state.ws
        return build_status(ws)

    # -- Trigger daily summary -------------------------------------------------

    @app.post("/control/summary")
    async def trigger_summary(request: Request) -> dict:
        ws: WebhookState = request.app.state.ws
        if ws.monitor is None:
            raise HTTPException(status_code=503, detail="monitor not initialized")

        summary = ws.monitor.generate_daily_summary()
        output_dir = "OUTPUT/novatrade"
        path = ws.monitor.write_daily_summary(summary, output_dir)
        _record_event(ws, "DAILY_SUMMARY_TRIGGERED", {"path": str(path)})
        return {"ok": True, "path": str(path), "summary": summary.to_dict()}

    # -- Resume endpoint -------------------------------------------------------

    @app.post("/control/resume")
    async def resume_trading(request: Request) -> dict:
        """Clear halt state on risk engine and supervisor so trading can resume."""
        ws: WebhookState = request.app.state.ws

        resumed = []
        if ws.risk_engine and ws.risk_engine.halted:
            prev = ws.risk_engine.halt_reason
            ws.risk_engine.resume()
            resumed.append(f"risk_engine (was: {prev})")
        if ws.supervisor and ws.supervisor.halted:
            prev = ws.supervisor._halt_reason
            ws.supervisor.resume()
            resumed.append(f"supervisor (was: {prev})")

        if not resumed:
            return {"ok": True, "message": "nothing was halted"}

        _record_event(ws, "OPERATOR_RESUME", {"resumed": resumed})
        return {"ok": True, "resumed": resumed}

    # -- Readiness endpoint ----------------------------------------------------

    @app.get("/readiness")
    async def readiness(request: Request) -> dict:
        """Return current launch-gate readiness assessment."""
        from novatrade.runtime.launch_gate import evaluate_launch_gate

        ws: WebhookState = request.app.state.ws
        r = evaluate_launch_gate(
            ws.agent._cfg if ws.agent else None,  # type: ignore[arg-type]
            ws.launch_mode,
            risk_engine_initialized=ws.risk_engine is not None,
            risk_engine_halted=ws.risk_engine.halted if ws.risk_engine else False,
            agent_initialized=ws.agent is not None,
            monitor_initialized=ws.monitor is not None,
            adapter_connected=ws.adapter_connected,
            adapter_type=ws.adapter_type,
        )
        return r.to_dict()

    return app


# ---------------------------------------------------------------------------
# Status builder
# ---------------------------------------------------------------------------


def build_status(ws: WebhookState) -> dict:
    """Build the detailed runtime status dict."""
    agent_state = ws.agent.state.value if ws.agent else "not_initialized"
    pending_id = ws.agent.pending_order_id if ws.agent else None
    position_id = ws.agent.position_id if ws.agent else None

    halted = ws.risk_engine.halted if ws.risk_engine else False
    halt_reason = ws.risk_engine.halt_reason if ws.risk_engine else ""

    return {
        "runtime_mode": ws.launch_mode.value,
        "adapter_type": ws.adapter_type,
        "started_at": ws.started_at,
        "uptime_seconds": time.time() - ws.started_at if ws.started_at else 0,
        "trading_agent": {
            "state": agent_state,
            "pending_order_id": pending_id,
            "position_id": position_id,
        },
        "risk_engine": {
            "halted": halted,
            "halt_reason": halt_reason,
        },
        "hard_supervisor": ws.supervisor.snapshot() if ws.supervisor else {"enabled": False},
        "ops_monitor": {
            "initialized": ws.monitor is not None,
        },
        "webhook": {
            "alerts_received": ws.alerts_received,
            "alerts_processed": ws.alerts_processed,
            "alerts_rejected": ws.alerts_rejected,
            "last_alert_time": ws.last_alert_time,
        },
    }


# ---------------------------------------------------------------------------
# Evidence helper
# ---------------------------------------------------------------------------


def _record_event(ws: WebhookState, event_name: str, data: dict) -> None:
    """Record an event to the evidence trail if a recorder is available."""
    if ws.recorder is None:
        return
    ws.recorder._append(
        EvidenceRecord(
            event_type=EvidenceType.MONITORING,
            data={"event": event_name, **data},
        )
    )
