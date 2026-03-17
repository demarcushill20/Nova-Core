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
from novatrade.risk.risk_engine import RiskEngine
from novatrade.validation.evidence import EvidenceRecorder

log = logging.getLogger("novatrade.runtime.webhook")

# ---------------------------------------------------------------------------
# Webhook state container (injected at startup)
# ---------------------------------------------------------------------------


@dataclass
class WebhookState:
    """Shared state injected into the FastAPI app at startup."""

    agent: TradingAgent | None = None
    risk_engine: RiskEngine | None = None
    monitor: OpsMonitor | None = None
    recorder: EvidenceRecorder | None = None
    dry_run: bool = True
    webhook_secret: str = ""
    started_at: float = 0.0
    alerts_received: int = 0
    alerts_processed: int = 0
    alerts_rejected: int = 0
    last_alert_time: float = 0.0


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

        # --- Basic auth (optional shared secret) ---
        if ws.webhook_secret:
            token = request.headers.get("X-Webhook-Secret", "")
            if token != ws.webhook_secret:
                ws.alerts_rejected += 1
                _record_event(ws, "WEBHOOK_AUTH_FAILED", {"reason": "bad_secret"})
                raise HTTPException(status_code=403, detail="invalid webhook secret")

        # --- Parse body ---
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

        if not isinstance(payload, dict):
            ws.alerts_rejected += 1
            _record_event(ws, "WEBHOOK_NOT_OBJECT", {"type": type(payload).__name__})
            raise HTTPException(status_code=400, detail="payload must be a JSON object")

        # --- Route to Trading Agent ---
        if ws.agent is None:
            raise HTTPException(status_code=503, detail="trading agent not initialized")

        _record_event(
            ws,
            "WEBHOOK_RECEIVED",
            {
                "action": payload.get("action", "unknown"),
                "symbol": payload.get("symbol", "unknown"),
                "dry_run": ws.dry_run,
            },
        )

        result = await ws.agent.process_alert(payload)
        ws.last_alert_time = time.time()

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
                "dry_run": ws.dry_run,
            },
        )

        return {
            "ok": result.success,
            "state": result.state_after,
            "rejected_reason": result.rejected_reason or None,
            "error": result.error or None,
            "dry_run": ws.dry_run,
        }

    # -- Health endpoint -------------------------------------------------------

    @app.get("/health")
    async def health(request: Request) -> dict:
        ws: WebhookState = request.app.state.ws
        agent_state = ws.agent.state.value if ws.agent else "not_initialized"
        halted = ws.risk_engine.halted if ws.risk_engine else False
        return {
            "status": "ok" if ws.agent else "degraded",
            "dry_run": ws.dry_run,
            "agent_state": agent_state,
            "risk_halted": halted,
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
        "runtime_mode": "dry-run" if ws.dry_run else "active-ready",
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
        "ops_monitor": {
            "initialized": ws.monitor is not None,
        },
        "webhook": {
            "alerts_received": ws.alerts_received,
            "alerts_processed": ws.alerts_processed,
            "alerts_rejected": ws.alerts_rejected,
            "last_alert_time": ws.last_alert_time,
        },
        "dry_run": ws.dry_run,
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
