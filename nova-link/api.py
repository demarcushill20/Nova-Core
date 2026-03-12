"""Nova-Link API — FastAPI backend for the Nova-Core mobile dashboard.

Provides REST endpoints for heartbeat status, task management, chat,
research reports, memory queries, and service controls.
WebSocket endpoint for real-time push updates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Shared conversation system with Telegram
from telegram.conversation import ConversationManager
from telegram.llm import format_history_for_prompt, generate_response

# --- Configuration -----------------------------------------------------------

BASE = Path("/home/nova/nova-core")
TASKS_DIR = BASE / "TASKS"
OUTPUT_DIR = BASE / "OUTPUT"
LOGS_DIR = BASE / "LOGS"
STATE_DIR = BASE / "STATE"
HEARTBEAT_FILE = BASE / "HEARTBEAT.md"
HEARTBEAT_MULTIAGENT = BASE / "HEARTBEAT_MULTIAGENT.md"
GOALS_FILE = STATE_DIR / "goals.json"
METRICS_FILE = STATE_DIR / "metrics.json"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude")

# Auth token — disabled for now (PWA runs on same origin)
# TODO: add proper auth (PIN code or session token) later
API_TOKEN = ""

# Share conversation with Telegram — same chat ID = same thread
SHARED_CHAT_ID = "530812511"
_conversations = ConversationManager(persist=True)

LOG = logging.getLogger("nova-link")

# --- App setup ---------------------------------------------------------------

app = FastAPI(title="Nova-Link", version="1.0.0", docs_url="/api/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Auth middleware ----------------------------------------------------------


def _check_token(token: str | None):
    """Validate API token. Raises 401 if invalid."""
    if not API_TOKEN:
        return  # No token configured = open (dev mode)
    if token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


# --- Models ------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str
    token: str | None = None


class TaskRequest(BaseModel):
    title: str
    body: str
    token: str | None = None


class TokenRequest(BaseModel):
    token: str | None = None


# --- WebSocket connections ---------------------------------------------------

_ws_clients: set[WebSocket] = set()


async def _broadcast(event: str, data: dict):
    """Send event to all connected WebSocket clients."""
    global _ws_clients
    msg = json.dumps({"event": event, "data": data, "ts": time.time()})
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _ws_clients -= dead


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Real-time event stream for the dashboard."""
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            # Keep connection alive, listen for pings
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"event": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


# --- Heartbeat / Dashboard --------------------------------------------------


@app.get("/api/heartbeat")
async def get_heartbeat(token: str | None = None):
    """Get current heartbeat status and all check results."""
    _check_token(token)

    result = {
        "status": "unknown",
        "last_check": None,
        "checks": [],
        "summary": "",
    }

    if HEARTBEAT_FILE.exists():
        text = HEARTBEAT_FILE.read_text()
        lines = text.strip().splitlines()

        # Parse checks from markdown
        checks = []
        for line in lines:
            if line.startswith("- ["):
                ok = line[3] == "x"
                rest = line[6:].strip()
                name, _, detail = rest.partition(": ")
                checks.append({"name": name, "ok": ok, "detail": detail})

        # Parse overall status
        for line in lines:
            if line.startswith("Overall:"):
                result["status"] = line.split(":")[1].strip()
            if line.startswith("Last check:"):
                result["last_check"] = line.split(":", 1)[1].strip()

        result["checks"] = checks
        result["summary"] = text

    # Add multi-agent health if available
    if HEARTBEAT_MULTIAGENT.exists():
        result["multiagent"] = HEARTBEAT_MULTIAGENT.read_text()

    return result


@app.get("/api/dashboard")
async def get_dashboard(token: str | None = None):
    """Aggregated dashboard data — heartbeat, tasks, goals, metrics."""
    _check_token(token)

    # Heartbeat
    heartbeat = {"status": "unknown", "checks_passed": 0, "checks_total": 0}
    if HEARTBEAT_FILE.exists():
        text = HEARTBEAT_FILE.read_text()
        for line in text.splitlines():
            if line.startswith("- ["):
                heartbeat["checks_total"] += 1
                if line[3] == "x":
                    heartbeat["checks_passed"] += 1
            if line.startswith("Overall:"):
                heartbeat["status"] = line.split(":")[1].strip()
            if line.startswith("Last check:"):
                heartbeat["last_check"] = line.split(":", 1)[1].strip()

    # Task queue
    pending = failed = done = 0
    if TASKS_DIR.exists():
        for f in TASKS_DIR.iterdir():
            if f.name.endswith(".md") and not any(
                f.name.endswith(s) for s in (".done", ".failed", ".inprogress", ".cancelled")
            ):
                pending += 1
            elif f.name.endswith(".failed"):
                failed += 1
            elif f.name.endswith(".done"):
                done += 1

    # Goals
    goals = []
    if GOALS_FILE.exists():
        try:
            data = json.loads(GOALS_FILE.read_text())
            goals_list = data.get("goals", []) if isinstance(data, dict) else data
            goals = [g for g in goals_list if g.get("status") != "done"]
        except Exception:
            pass

    # Metrics
    metrics = {}
    if METRICS_FILE.exists():
        try:
            metrics = json.loads(METRICS_FILE.read_text())
        except Exception:
            pass

    # Services
    services = {}
    for svc in ["novacore-watcher", "novacore-telegram", "novacore-telegram-notifier"]:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True,
                text=True,
                timeout=5,
            )
            services[svc] = r.stdout.strip()
        except Exception:
            services[svc] = "unknown"

    # Recent research/planning cycles
    research_log = LOGS_DIR / "research_cycle.log"
    planning_log = LOGS_DIR / "planning_cycle.log"
    last_research = None
    last_planning = None
    if research_log.exists():
        lines = research_log.read_text().strip().splitlines()
        if lines:
            last_research = lines[-1]
    if planning_log.exists():
        lines = planning_log.read_text().strip().splitlines()
        if lines:
            last_planning = lines[-1]

    return {
        "heartbeat": heartbeat,
        "tasks": {"pending": pending, "failed": failed, "done": done},
        "goals": goals,
        "metrics": metrics,
        "services": services,
        "last_research": last_research,
        "last_planning": last_planning,
    }


# --- Tasks -------------------------------------------------------------------


@app.get("/api/tasks")
async def list_tasks(token: str | None = None):
    """List all tasks with their lifecycle status."""
    _check_token(token)

    tasks = []
    if TASKS_DIR.exists():
        for f in sorted(TASKS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not f.name.endswith(".gitkeep"):
                status = "pending"
                if f.name.endswith(".done"):
                    status = "done"
                elif f.name.endswith(".failed"):
                    status = "failed"
                elif f.name.endswith(".inprogress"):
                    status = "in_progress"
                elif f.name.endswith(".cancelled"):
                    status = "cancelled"

                age_min = (
                    datetime.now(timezone.utc) - datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                ).total_seconds() / 60

                tasks.append(
                    {
                        "name": f.name,
                        "status": status,
                        "age_minutes": round(age_min),
                        "size": f.stat().st_size,
                    }
                )

    return {"count": len(tasks), "tasks": tasks[:50]}


@app.post("/api/tasks")
async def create_task(req: TaskRequest):
    """Inject a new task into the queue."""
    _check_token(req.token)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # Sanitize title for filename
    import re

    slug = re.sub(r"[^a-zA-Z0-9]+", "_", req.title.strip()).strip("_").lower()[:60]
    stem = f"nl_{ts}_{slug}"
    task_path = TASKS_DIR / f"{stem}.md"

    content = f"# {req.title}\n\n{req.body}"
    task_path.write_text(content)

    await _broadcast("task_created", {"stem": stem, "title": req.title})
    return {"status": "created", "stem": stem, "path": str(task_path)}


# --- Outputs / Reports -------------------------------------------------------


@app.get("/api/outputs")
async def list_outputs(limit: int = 20, token: str | None = None):
    """List recent OUTPUT reports."""
    _check_token(token)

    outputs = []
    if OUTPUT_DIR.exists():
        files = sorted(OUTPUT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[:limit]:
            age_hr = (
                datetime.now(timezone.utc) - datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            ).total_seconds() / 3600
            outputs.append(
                {
                    "name": f.name,
                    "stem": f.stem,
                    "size": f.stat().st_size,
                    "age_hours": round(age_hr, 1),
                }
            )

    return {"count": len(outputs), "outputs": outputs}


@app.get("/api/outputs/{filename}")
async def read_output(filename: str, token: str | None = None):
    """Read the full content of an OUTPUT report."""
    _check_token(token)

    path = OUTPUT_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Output not found")

    # Security: ensure we're not escaping OUTPUT_DIR
    if not path.resolve().is_relative_to(OUTPUT_DIR.resolve()):
        raise HTTPException(status_code=403, detail="Access denied")

    return {"filename": filename, "content": path.read_text(errors="replace")}


# --- Chat (CEO Nova) — async job-based pattern ------------------------------

# In-memory job store for long-running chat requests.
# Jobs expire after JOB_TTL_S to prevent unbounded growth.
_chat_jobs: dict[str, dict] = {}
_JOB_TTL_S = 900  # 15 minutes
_MAX_JOBS = 100


def _prune_jobs() -> None:
    """Remove expired jobs to bound memory usage."""
    if len(_chat_jobs) <= _MAX_JOBS // 2:
        return
    now = time.time()
    expired = [jid for jid, j in _chat_jobs.items() if now - j["created_at"] > _JOB_TTL_S]
    for jid in expired:
        _chat_jobs.pop(jid, None)


async def _run_chat_job(job_id: str, message: str) -> None:
    """Background coroutine that runs the LLM call and stores the result."""
    try:
        system_prompt = ""
        persona_file = BASE / "telegram" / "persona.md"
        if persona_file.exists():
            system_prompt = persona_file.read_text()[:4000]

        _conversations.reload_chat(SHARED_CHAT_ID)
        _conversations.add_user_message(SHARED_CHAT_ID, message)

        history = _conversations.get_history(SHARED_CHAT_ID)
        context = format_history_for_prompt(history[:-1])

        response = await generate_response(
            prompt=message,
            system_prompt=system_prompt,
            conversation_context=context,
        )

        _conversations.add_assistant_message(SHARED_CHAT_ID, response)

        _chat_jobs[job_id]["status"] = "completed"
        _chat_jobs[job_id]["response"] = response
        _chat_jobs[job_id]["completed_at"] = time.time()

        await _broadcast(
            "chat_response",
            {
                "job_id": job_id,
                "preview": response[:100],
                "response": response,
            },
        )

    except Exception as exc:
        LOG.error("Chat job %s failed: %s", job_id, exc, exc_info=True)
        _chat_jobs[job_id]["status"] = "failed"
        _chat_jobs[job_id]["error"] = str(exc)[:500]
        _chat_jobs[job_id]["completed_at"] = time.time()

        await _broadcast(
            "chat_response",
            {
                "job_id": job_id,
                "error": "Processing failed — please try again.",
            },
        )


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """Submit a message to CEO Nova.

    Returns immediately with a job_id. The response is delivered via:
    1. WebSocket push (event: chat_response, data.job_id + data.response)
    2. Polling GET /api/chat/jobs/{job_id}

    This prevents browser fetch timeouts on long-running LLM calls.
    """
    _check_token(req.token)

    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")

    _prune_jobs()

    job_id = uuid.uuid4().hex[:12]
    _chat_jobs[job_id] = {
        "status": "processing",
        "message": req.message[:500],
        "response": None,
        "error": None,
        "created_at": time.time(),
        "completed_at": None,
    }

    # Fire-and-forget the LLM call as a background task
    asyncio.create_task(_run_chat_job(job_id, req.message))

    return {"job_id": job_id, "status": "processing"}


@app.get("/api/chat/jobs/{job_id}")
async def get_chat_job(job_id: str, token: str | None = None):
    """Poll for the result of a chat job.

    Returns status: processing | completed | failed.
    When completed, includes the full response.
    """
    _check_token(token)

    job = _chat_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")

    result: dict = {"job_id": job_id, "status": job["status"]}
    if job["status"] == "completed":
        result["response"] = job["response"]
    elif job["status"] == "failed":
        result["error"] = job["error"]
    return result


@app.get("/api/chat/history")
async def chat_history(token: str | None = None):
    """Get recent conversation history (shared with Telegram)."""
    _check_token(token)
    _conversations.reload_chat(SHARED_CHAT_ID)
    history = _conversations.get_history(SHARED_CHAT_ID)
    return {"messages": history}


# --- Memory ------------------------------------------------------------------


@app.get("/api/memory/search")
async def memory_search(q: str, token: str | None = None):
    """Search Fusion Memory for relevant context."""
    _check_token(token)

    # Use the nova-memory MCP server via CLI
    # For now, return recent memory-related state
    results = []
    research_log = LOGS_DIR / "research_cycle.log"
    if research_log.exists():
        lines = research_log.read_text().strip().splitlines()
        for line in lines[-10:]:
            results.append({"source": "research_log", "content": line})

    planning_log = LOGS_DIR / "planning_cycle.log"
    if planning_log.exists():
        lines = planning_log.read_text().strip().splitlines()
        for line in lines[-10:]:
            results.append({"source": "planning_log", "content": line})

    return {"query": q, "results": results}


# --- Services / Controls -----------------------------------------------------


@app.get("/api/services")
async def list_services(token: str | None = None):
    """Get status of all NovaCore services."""
    _check_token(token)

    services = []
    for svc in ["novacore-watcher", "novacore-telegram", "novacore-telegram-notifier", "novacore-heartbeat.timer"]:
        try:
            r = subprocess.run(
                ["systemctl", "show", svc, "--property=ActiveState,SubState,MainPID,ActiveEnterTimestamp"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            props = dict(line.split("=", 1) for line in r.stdout.strip().splitlines() if "=" in line)
            services.append(
                {
                    "name": svc,
                    "active": props.get("ActiveState", "unknown"),
                    "sub_state": props.get("SubState", "unknown"),
                    "pid": props.get("MainPID", "?"),
                    "since": props.get("ActiveEnterTimestamp", "?"),
                }
            )
        except Exception:
            services.append({"name": svc, "active": "unknown"})

    return {"services": services}


@app.post("/api/heartbeat/trigger")
async def trigger_heartbeat(req: TokenRequest):
    """Manually trigger a heartbeat check."""
    _check_token(req.token)

    try:
        proc = await asyncio.create_subprocess_exec(
            "python3",
            str(BASE / "heartbeat.py"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE),
        )
        # Don't wait for completion — heartbeat can take 10+ minutes
        return {"status": "triggered", "pid": proc.pid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# --- Static files (PWA) -----------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_index():
    """Serve the PWA index page."""
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index, media_type="text/html")
    return {"message": "Nova-Link API", "docs": "/api/docs"}


@app.get("/manifest.json")
async def serve_manifest():
    """Serve PWA manifest."""
    manifest = STATIC_DIR / "manifest.json"
    if manifest.exists():
        return FileResponse(manifest, media_type="application/manifest+json")
    raise HTTPException(status_code=404)


@app.get("/sw.js")
async def serve_sw():
    """Serve service worker from root."""
    sw = STATIC_DIR / "sw.js"
    if sw.exists():
        return FileResponse(sw, media_type="application/javascript")
    raise HTTPException(status_code=404)
