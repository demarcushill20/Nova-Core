"""Nova-Link API — FastAPI backend for the Nova-Core mobile dashboard.

Provides REST endpoints for heartbeat status, task management, chat,
research reports, memory queries, and service controls.
WebSocket endpoint for real-time push updates.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import edge_tts
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Shared conversation system with Telegram
from telegram.conversation import ConversationManager
from telegram.llm import generate_response

# Thread system (importlib needed because "nova-link" has a hyphen)
_threads_mod = importlib.import_module("nova-link.threads")
ThreadStore = _threads_mod.ThreadStore

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

# Thread store for Nova-Link threaded conversations
_thread_store = ThreadStore()

LOG = logging.getLogger("nova-link")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")

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
    thread_id: str | None = None  # None = use default thread
    token: str | None = None


class TaskRequest(BaseModel):
    title: str
    body: str
    token: str | None = None


class CreateThreadRequest(BaseModel):
    title: str | None = None
    token: str | None = None


class TokenRequest(BaseModel):
    token: str | None = None


class TTSRequest(BaseModel):
    text: str
    voice: str | None = None


# --- Background task tracking ------------------------------------------------
# Python 3.10: asyncio.create_task() does NOT hold a strong reference.
# Without this set, tasks get garbage-collected mid-execution, silently
# killing LLM calls.  See: https://docs.python.org/3/library/asyncio-task.html
_background_tasks: set[asyncio.Task] = set()

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
            LOG.debug("Failed to load goals", exc_info=True)

    # Metrics
    metrics = {}
    if METRICS_FILE.exists():
        try:
            metrics = json.loads(METRICS_FILE.read_text())
        except Exception:
            LOG.debug("Failed to load metrics", exc_info=True)

    # Services (async to avoid blocking the event loop)
    services = {}
    for svc in ["novacore-watcher", "novacore-telegram", "novacore-telegram-notifier"]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl",
                "is-active",
                svc,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            services[svc] = stdout.decode().strip()
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
_JOB_TTL_S = 14400  # 4 hours — match CONVERSATION_TIMEOUT for long-running LLM calls
_MAX_JOBS = 100


def _prune_jobs() -> None:
    """Remove expired jobs to bound memory usage."""
    if len(_chat_jobs) <= _MAX_JOBS // 2:
        return
    now = time.time()
    expired = [jid for jid, j in _chat_jobs.items() if now - j["created_at"] > _JOB_TTL_S]
    for jid in expired:
        _chat_jobs.pop(jid, None)


def _build_thread_context(
    messages: list,
    summary: dict | None,
) -> str:
    """Build LLM conversation context from thread messages and summary.

    The thread summary (if present) is injected as a bracketed context
    block — visible to the LLM but clearly marked as background context,
    not as a user message.  Recent turns use the same Human/You format
    that ``format_history_for_prompt`` produces.
    """
    parts: list[str] = []

    if summary:
        summary_text = summary.get("content", "")
        if summary_text:
            parts.append(f"[Earlier conversation context: {summary_text}]")

    if messages:
        lines: list[str] = []
        for m in messages:
            role = "You" if m.role == "assistant" else "Human"
            lines.append(f"{role}: {m.content}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


# Max recent thread turns to include in LLM context (roughly 10 exchanges)
_THREAD_CONTEXT_LIMIT = 20

# --- Thread summarization (Phase 5) -----------------------------------------

# Trigger summarization when message count exceeds this threshold
_SUMMARIZE_THRESHOLD = 30
# Keep this many recent messages after compaction
_SUMMARIZE_KEEP_RECENT = 15

_SUMMARIZE_PROMPT = """\
You are summarizing a conversation thread for an AI assistant's context window.

{existing_section}

Here are the messages to summarize:

{messages_text}

Produce a JSON object with exactly these keys:
- "type": "structured"
- "content": A 2-3 sentence plain-text summary (fallback for simple readers)
- "goals": List of active goals the user is pursuing (strings, max 5)
- "decisions": List of key decisions made in this conversation (strings, max 5)
- "open_questions": List of unresolved questions or pending items (strings, max 3)
- "artifacts": List of important files, URLs, or outputs mentioned (strings, max 5)
- "user_preferences": List of preferences the user expressed (strings, max 3)

Output ONLY the JSON object, no markdown fencing, no commentary."""


def _deterministic_fallback_summary(
    messages: list,
    existing_summary: dict | None,
) -> dict:
    """Build a simple summary without LLM when the LLM call fails."""
    user_msgs = [m for m in messages if m.role == "user"]
    topics = []
    for m in user_msgs[:5]:
        first_line = m.content[:80].split("\n")[0]
        if first_line:
            topics.append(first_line)

    content_parts = []
    if existing_summary and existing_summary.get("content"):
        content_parts.append(existing_summary["content"])
    if topics:
        content_parts.append(f"Recent topics: {'; '.join(topics)}")

    return {
        "type": "text",
        "content": " | ".join(content_parts) if content_parts else "Conversation history",
    }


async def _summarize_via_llm(prompt_text: str) -> str | None:
    """Run a lightweight Claude CLI call for summarization.

    Returns the raw stdout text, or None on failure.
    """
    import subprocess

    claude_bin = os.environ.get("CLAUDE_BIN", CLAUDE_BIN)
    cmd = [
        claude_bin,
        "-p",
        "--model",
        "claude-sonnet-4-20250514",
        "--dangerously-skip-permissions",
        prompt_text,
    ]

    child_env = os.environ.copy()
    child_env.pop("CLAUDECODE", None)

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            env=child_env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        LOG.warning("Summarization CLI returned code %d", result.returncode)
        return None
    except Exception as exc:
        LOG.warning("Summarization CLI failed: %s", exc)
        return None


async def _generate_thread_summary(
    messages: list,
    existing_summary: dict | None,
) -> dict:
    """Generate a structured thread summary via LLM with deterministic fallback."""
    # Build existing summary section for the prompt
    existing_section = ""
    if existing_summary and existing_summary.get("content"):
        existing_section = (
            f"The conversation already has this summary from earlier:\n"
            f'"{existing_summary["content"]}"\n'
            f"Incorporate and update it — don't lose prior context."
        )

    # Format messages for the prompt
    lines = []
    for m in messages:
        role = "User" if m.role == "user" else "Assistant"
        # Truncate very long messages to keep the summarization prompt reasonable
        content = m.content[:500] + "..." if len(m.content) > 500 else m.content
        lines.append(f"{role}: {content}")
    messages_text = "\n".join(lines)

    prompt_text = _SUMMARIZE_PROMPT.format(
        existing_section=existing_section,
        messages_text=messages_text,
    )

    raw = await _summarize_via_llm(prompt_text)
    if raw:
        try:
            # Strip markdown fencing if present
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = "\n".join(cleaned.split("\n")[1:])
            if cleaned.endswith("```"):
                cleaned = "\n".join(cleaned.split("\n")[:-1])
            parsed = json.loads(cleaned.strip())
            # Validate required fields
            if isinstance(parsed, dict) and "content" in parsed:
                parsed.setdefault("type", "structured")
                return parsed
        except (json.JSONDecodeError, ValueError) as exc:
            LOG.warning("Failed to parse LLM summary JSON: %s", exc)

    LOG.info("Using deterministic fallback for thread summary")
    return _deterministic_fallback_summary(messages, existing_summary)


async def _maybe_summarize_thread(thread_id: str) -> None:
    """Check if a thread needs summarization and compact it if so.

    Called after each assistant response. Only triggers when message_count
    exceeds _SUMMARIZE_THRESHOLD to avoid unnecessary LLM calls.
    """
    thread = _thread_store.get_thread(thread_id)
    if thread is None:
        return

    if thread.message_count < _SUMMARIZE_THRESHOLD:
        return

    LOG.info(
        "Thread %s has %d messages (threshold=%d) — summarizing",
        thread_id,
        thread.message_count,
        _SUMMARIZE_THRESHOLD,
    )

    # Get ALL messages (we'll summarize older ones, keep recent)
    all_msgs = _thread_store.get_messages(thread_id, limit=9999, order="asc")
    if len(all_msgs) <= _SUMMARIZE_KEEP_RECENT:
        return

    # Messages to summarize = everything except the most recent N
    to_summarize = all_msgs[:-_SUMMARIZE_KEEP_RECENT]

    new_summary = await _generate_thread_summary(to_summarize, thread.summary)

    _thread_store.compact_thread(
        thread_id,
        keep_recent=_SUMMARIZE_KEEP_RECENT,
        new_summary=new_summary,
    )
    LOG.info(
        "Compacted thread %s: summarized %d messages, kept %d",
        thread_id,
        len(to_summarize),
        _SUMMARIZE_KEEP_RECENT,
    )


async def _run_chat_job(job_id: str, message: str, thread_id: str | None = None) -> None:
    """Background coroutine that runs the LLM call and stores the result.

    Context is assembled from the active thread's recent messages and
    summary.  The legacy ConversationBuffer is kept in sync for Telegram.
    """
    # Resolve thread (creates default / migrates legacy on first call)
    if thread_id:
        thread = _thread_store.get_thread(thread_id)
        if thread is None:
            thread = _thread_store.get_or_create_default_thread()
    else:
        thread = _thread_store.get_or_create_default_thread()

    LOG.info(
        "Chat job %s: starting LLM call (msg_len=%d, thread=%s)",
        job_id,
        len(message),
        thread.id,
    )
    try:
        system_prompt = ""
        persona_file = BASE / "telegram" / "persona.md"
        if persona_file.exists():
            system_prompt = persona_file.read_text()[:4000]

        # 1. Persist user message to thread FIRST (survives crashes)
        _thread_store.add_message(thread.id, "user", message)

        # 2. Build context from thread history
        thread_obj = _thread_store.get_thread(thread.id)
        recent = _thread_store.get_messages(
            thread.id,
            limit=_THREAD_CONTEXT_LIMIT + 1,
            order="asc",
        )
        # Exclude the message we just added (it's the prompt, not context)
        prior_turns = recent[:-1] if recent else []
        # Keep only the most recent N turns for context
        prior_turns = prior_turns[-_THREAD_CONTEXT_LIMIT:]
        context = _build_thread_context(
            prior_turns,
            thread_obj.summary if thread_obj else None,
        )

        # 3. Sync to ConversationBuffer (Telegram compatibility)
        _conversations.reload_chat(SHARED_CHAT_ID)
        _conversations.add_user_message(SHARED_CHAT_ID, message)

        response = await generate_response(
            prompt=message,
            system_prompt=system_prompt,
            conversation_context=context,
        )

        # 4. Save response to both stores
        _thread_store.add_message(thread.id, "assistant", response)
        _conversations.add_assistant_message(SHARED_CHAT_ID, response)

        # 5. Trigger rolling summarization if needed (fire-and-forget)
        try:
            await _maybe_summarize_thread(thread.id)
        except Exception as sum_exc:
            LOG.warning("Thread summarization failed (non-fatal): %s", sum_exc)

        _chat_jobs[job_id]["status"] = "completed"
        _chat_jobs[job_id]["response"] = response
        _chat_jobs[job_id]["thread_id"] = thread.id
        _chat_jobs[job_id]["completed_at"] = time.time()
        LOG.info("Chat job %s: completed (response_len=%d)", job_id, len(response))

        await _broadcast(
            "chat_response",
            {
                "job_id": job_id,
                "thread_id": thread.id,
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
        "thread_id": req.thread_id,
        "response": None,
        "error": None,
        "created_at": time.time(),
        "completed_at": None,
    }

    # Fire-and-forget — but MUST keep a strong reference (Python 3.10 GC issue)
    task = asyncio.create_task(_run_chat_job(job_id, req.message, req.thread_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    LOG.info("Chat job %s started (task=%s)", job_id, task.get_name())
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
    if job.get("thread_id"):
        result["thread_id"] = job["thread_id"]
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


# --- Threads -----------------------------------------------------------------


@app.get("/api/threads")
async def list_threads(
    status: str | None = "active",
    limit: int = 50,
    offset: int = 0,
    token: str | None = None,
):
    """List conversation threads, newest first."""
    _check_token(token)
    threads = _thread_store.list_threads(status=status, limit=limit, offset=offset)
    return {
        "threads": [t.to_dict() for t in threads],
        "count": len(threads),
        "offset": offset,
        "limit": limit,
    }


@app.post("/api/threads")
async def create_thread_endpoint(req: CreateThreadRequest):
    """Create a new empty conversation thread."""
    _check_token(req.token)
    thread = _thread_store.create_thread(title=req.title or "")
    await _broadcast("thread_created", {"thread_id": thread.id, "title": thread.title})
    return thread.to_dict()


@app.get("/api/threads/default")
async def get_default_thread(token: str | None = None):
    """Get or create the default thread (triggers migration if needed)."""
    _check_token(token)
    thread = _thread_store.get_or_create_default_thread()
    return thread.to_dict()


@app.get("/api/threads/{thread_id}")
async def get_thread(thread_id: str, token: str | None = None):
    """Get a thread by ID."""
    _check_token(token)
    thread = _thread_store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return thread.to_dict()


@app.get("/api/threads/{thread_id}/messages")
async def get_thread_messages(
    thread_id: str,
    limit: int = 50,
    offset: int = 0,
    order: str = "asc",
    token: str | None = None,
):
    """Get messages for a thread with pagination.

    order: "asc" (oldest first) or "desc" (newest first).
    """
    _check_token(token)
    thread = _thread_store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    messages = _thread_store.get_messages(
        thread_id,
        limit=limit,
        offset=offset,
        order=order,
    )
    return {
        "thread_id": thread_id,
        "messages": [m.to_dict() for m in messages],
        "count": len(messages),
        "total": thread.message_count,
        "offset": offset,
        "limit": limit,
    }


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
            proc = await asyncio.create_subprocess_exec(
                "systemctl",
                "show",
                svc,
                "--property=ActiveState,SubState,MainPID,ActiveEnterTimestamp",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            props = dict(line.split("=", 1) for line in stdout.decode().strip().splitlines() if "=" in line)
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


# --- Text-to-Speech (edge-tts) -----------------------------------------------

DEFAULT_TTS_VOICE = "en-US-AriaNeural"

_VALID_TTS_VOICES = {
    "en-US-AriaNeural",
    "en-US-GuyNeural",
    "en-US-JennyNeural",
    "en-US-EricNeural",
    "en-US-DavisNeural",
    "en-US-SaraNeural",
}


@app.post("/api/tts")
async def text_to_speech(req: TTSRequest):
    """Convert text to speech audio via edge-tts. Returns MP3."""
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    text = re.sub(r"```[\s\S]*?```", "code block omitted", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"#{1,6}\s", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[-*]\s", "", text)
    text = text.strip()

    if not text:
        raise HTTPException(status_code=400, detail="No speakable text")

    voice = req.voice or DEFAULT_TTS_VOICE
    if voice not in _VALID_TTS_VOICES:
        LOG.warning("Invalid TTS voice %r, falling back to default", voice)
        voice = DEFAULT_TTS_VOICE
    communicate = edge_tts.Communicate(text, voice)
    audio_bytes = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_bytes += chunk["data"]

    return Response(content=audio_bytes, media_type="audio/mpeg")


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
