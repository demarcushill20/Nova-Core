#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import fcntl
import functools
import importlib.util
import json
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

# --- Import shim ---
# Our local telegram/ directory shadows the installed python-telegram-bot
# package. We temporarily hide it from sys.path so the library loads,
# then register our local telegram/parse.py into sys.modules so that
# a plain "from telegram.parse import ..." resolves correctly.
_here = str(Path(__file__).parent)
_path_backup = sys.path[:]
sys.path = [p for p in sys.path if os.path.realpath(p) != os.path.realpath(_here)]
sys.modules.pop("telegram", None)
sys.modules.pop("telegram.ext", None)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup  # noqa: E402  — python-telegram-bot library
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters  # noqa: E402
from telegram.constants import ChatAction  # noqa: E402

sys.path = _path_backup  # restore

# Register our local telegram/parse.py under "telegram.parse" in sys.modules
# so the canonical import below works without colliding with the library.
_spec = importlib.util.spec_from_file_location(
    "telegram.parse", os.path.join(_here, "telegram", "parse.py")
)
_tg_parse = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tg_parse)
sys.modules["telegram.parse"] = _tg_parse

from telegram.parse import parse_message  # noqa: E402

# Register additional local telegram modules via the same shim pattern.
for _mod_name in ("conversation", "llm", "persona", "delegation", "goals", "hardening", "working_memory"):
    _mod_spec = importlib.util.spec_from_file_location(
        f"telegram.{_mod_name}", os.path.join(_here, "telegram", f"{_mod_name}.py")
    )
    _mod_obj = importlib.util.module_from_spec(_mod_spec)
    sys.modules[f"telegram.{_mod_name}"] = _mod_obj  # register before exec (dataclass needs it)
    _mod_spec.loader.exec_module(_mod_obj)

from telegram.conversation import ConversationManager  # noqa: E402
from telegram.llm import generate_response, format_history_for_prompt  # noqa: E402
from telegram.persona import SYSTEM_PROMPT, DELEGATION_ACK_PROMPT, SESSION_START_HINT  # noqa: E402
from telegram.delegation import (  # noqa: E402
    DelegationTracker, find_completed_output, claim_notification,
    extract_output_summary, get_recent_completions, COMPLETION_SUMMARY_PROMPT,
)
from telegram.goals import (  # noqa: E402
    add_goal, complete_goal, remove_goal, list_goals,
    clear_completed, format_goals_for_context, format_goals_for_display,
)
from telegram.hardening import (  # noqa: E402
    RateLimiter, CircuitBreaker, MetricsCollector, ResponseCache,
    RATE_LIMIT_MESSAGE, CIRCUIT_OPEN_MESSAGE,
)
from telegram.working_memory import WorkingMemoryStore, ActiveTask  # noqa: E402

ROOT = Path("/home/nova/nova-core")
TASKS = ROOT / "TASKS"
OUTPUT = ROOT / "OUTPUT"
LOGS = ROOT / "LOGS"
STATE = ROOT / "STATE"

CANCEL_DIR = STATE / "cancel"
RUNNING_DIR = STATE / "running"
INTENTS_DIR = STATE / "intents"
CEO_DELEGATED_DIR = STATE / "ceo_delegated"
CHAT_MODES_FILE = STATE / "chat_modes.json"

for _d in (TASKS, OUTPUT, LOGS, STATE, CANCEL_DIR, RUNNING_DIR, INTENTS_DIR, CEO_DELEGATED_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Logging setup ---
_log = logging.getLogger("telegram_bot")
_log.setLevel(logging.INFO)
_log_handler = logging.StreamHandler(sys.stdout)
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))
_log.addHandler(_log_handler)

# --- Protocol constants (from PROTOCOL/telegram_commands.md v1.1) ---

_HELP_TEXT = (
    "NovaCore Commands\n"
    "Just type anything \u2014 casual questions get a clean reply.\n\n"
    "TASKS\n"
    "/run <title>   \u2014 queue a task\n"
    "/report <text> \u2014 force full structured report\n"
    "/status        \u2014 show recent tasks\n"
    "/last          \u2014 show most recent task\n"
    "/cancel <id>   \u2014 soft-cancel (/cancel last for newest)\n\n"
    "OUTPUT\n"
    "/get <file>    \u2014 retrieve output (/get <file> 2 for page 2)\n"
    "/tail <id>     \u2014 tail worker log (/tail <id> 100)\n\n"
    "CONVERSATION\n"
    "/chat <text>   \u2014 force clean chat reply\n"
    "/goals         \u2014 manage goals (add, done, remove, clear)\n"
    "/briefing      \u2014 system briefing\n\n"
    "SETTINGS\n"
    "/mode <level>  \u2014 compact | normal | verbose\n"
    "/help          \u2014 this message"
)

# --- Conversation manager (Phase 1: CEO Nova) ---
_conversations = ConversationManager()

# --- Delegation tracker (Phase 3: proactive completion notifications) ---
_delegations = DelegationTracker()

# --- Production hardening (Phase 7) ---
_rate_limiter = RateLimiter(per_chat_limit=10, per_chat_window=60,
                            global_limit=30, global_window=60)
_circuit_breaker = CircuitBreaker(failure_threshold=5, cooldown_seconds=120)
_metrics = MetricsCollector()
_response_cache = ResponseCache(max_size=50, ttl_seconds=300)

# --- Working memory (Phase 8) ---
_working_memory = WorkingMemoryStore()

_STATUS_LIMITS = {"compact": 5, "normal": 10, "verbose": 20}

# Unicode status indicators for /status display (no emoji — plain text safe)
_STATUS_ICON = {
    "queued":      "\u25cb",  # ○
    "inprogress":  "\u25c9",  # ◉
    "done":        "\u2713",  # ✓
    "failed":      "\u2717",  # ✗
    "skip":        "\u2014",  # —
}

# Extension-to-status mapping — ordered longest-first for matching
_EXT_STATUS = [
    (".md.inprogress", "inprogress"),
    (".md.cancelled",  "skip"),
    (".md.failed",     "failed"),
    (".md.done",       "done"),
    (".md.skip",       "skip"),
    (".skip",          "skip"),
    (".inprogress",    "inprogress"),
    (".failed",        "failed"),
    (".done",          "done"),
    (".md",            "queued"),
]


# --- Helpers (preserved from original) ---

def slugify(text: str, max_len: int = 50) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip()).strip("_").lower()
    return s[:max_len] or "task"


def safe_write_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def safe_join(base_dir: Path, user_filename: str) -> Path | None:
    clean = user_filename.replace("\\", "/")
    if clean.startswith("/") or ".." in clean.split("/"):
        return None
    resolved = (base_dir / clean).resolve()
    if not str(resolved).startswith(str(base_dir.resolve())):
        return None
    return resolved


def read_tail_lines(path: Path, n: int = 80) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-n:] if len(lines) > n else lines
    return "\n".join(tail)


def chunk_text(text: str, chunk_size: int = 3500) -> list[str]:
    chunks: list[str] = []
    remaining = text
    while len(remaining) > chunk_size:
        cut = remaining.rfind("\n", 0, chunk_size)
        if cut < 500:
            cut = chunk_size
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


LAST_TASK_FILE = STATE / "last_task_id.txt"


def persist_last_task_id(task_id: str) -> None:
    CANCEL_DIR.mkdir(parents=True, exist_ok=True)
    LAST_TASK_FILE.write_text(task_id, encoding="utf-8")


def _write_delegation_marker(stem: str) -> None:
    """Mark a task as CEO-delegated so the notifier defers."""
    CEO_DELEGATED_DIR.mkdir(parents=True, exist_ok=True)
    marker = CEO_DELEGATED_DIR / f"{stem}.delegated"
    marker.write_text(f"{time.time()}\n", encoding="utf-8")


def _cleanup_delegation_marker(stem: str) -> None:
    """Remove delegation marker after CEO Nova delivers the notification."""
    marker = CEO_DELEGATED_DIR / f"{stem}.delegated"
    marker.unlink(missing_ok=True)


def _cleanup_stale_delegation_markers(max_age_seconds: int = 86400) -> None:
    """Remove delegation markers older than max_age_seconds."""
    if not CEO_DELEGATED_DIR.exists():
        return
    cutoff = time.time() - max_age_seconds
    for marker in CEO_DELEGATED_DIR.glob("*.delegated"):
        try:
            if marker.stat().st_mtime < cutoff:
                marker.unlink()
        except OSError:
            pass


def read_last_task_id() -> str | None:
    try:
        val = LAST_TASK_FILE.read_text(encoding="utf-8").strip()
        return val or None
    except (FileNotFoundError, OSError):
        return None


def _task_id_from_path(path: Path) -> str:
    name = path.name
    for suffix in (".md.inprogress", ".md.cancelled", ".md.failed", ".md.done", ".md"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _is_valid_task_id(task_id: str) -> bool:
    return bool(re.match(r'^(tg_|\d{4}_)', task_id))


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def terminate_worker(task_id: str) -> str:
    if not _is_valid_task_id(task_id):
        return "skipped (invalid task_id pattern)"
    pid_file = RUNNING_DIR / f"{task_id}.pid"
    if not pid_file.exists():
        return "no pid file (not currently running)"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return "bad pid file"
    if not _pid_is_alive(pid):
        pid_file.unlink(missing_ok=True)
        return f"stale pid {pid} (already exited)"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        return f"pid {pid} exited before SIGTERM"
    except PermissionError:
        return f"no permission to kill pid {pid}"
    for _ in range(6):
        time.sleep(0.5)
        if not _pid_is_alive(pid):
            pid_file.unlink(missing_ok=True)
            return f"terminated pid {pid} (SIGTERM)"
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        return f"pid {pid} exited during grace period"
    except PermissionError:
        return f"SIGTERM sent but no permission for SIGKILL on pid {pid}"
    pid_file.unlink(missing_ok=True)
    return f"killed pid {pid} (SIGKILL after 3s grace)"


def write_cancel_marker(task_id: str) -> Path:
    CANCEL_DIR.mkdir(parents=True, exist_ok=True)
    marker = CANCEL_DIR / f"{task_id}.cancel"
    marker.write_text(
        f"Cancel requested at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n",
        encoding="utf-8",
    )
    return marker


# --- Mode helpers ---

def load_chat_mode(chat_id: str) -> str:
    """Read the mode for a chat_id from STATE/chat_modes.json. Default: normal."""
    try:
        data = json.loads(CHAT_MODES_FILE.read_text(encoding="utf-8"))
        return data.get(chat_id, "normal")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return "normal"


# --- Action handlers (protocol v1.1) ---


def handle_help() -> str:
    """Return the canonical help text."""
    return _HELP_TEXT


def _task_status(name: str) -> str:
    """Derive task status from filename extension."""
    for ext, status in _EXT_STATUS:
        if name.endswith(ext):
            return status
    return "?"


def _task_stem(name: str) -> str:
    """Extract the stem (number + title) from a task filename."""
    for ext, _ in _EXT_STATUS:
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _task_number(name: str) -> str:
    """Extract the leading number (e.g., '0005') from a task filename, or '' for tg_ files."""
    stem = _task_stem(name)
    m = re.match(r"^(\d{4})", stem)
    return m.group(1) if m else stem


def handle_status(chat_id: str) -> str:
    """Build the /status response by scanning TASKS/."""
    if not TASKS.exists():
        return "No tasks found."

    files = sorted(TASKS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return "No tasks found."

    mode = load_chat_mode(chat_id)
    limit = _STATUS_LIMITS.get(mode, 10)

    lines = []
    for p in files[:limit]:
        name = p.name
        stem = _task_stem(name)
        status = _task_status(name)
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        ts_str = mtime.strftime("%Y-%m-%d %H:%M")
        # Use the leading number if available, else the full stem
        display_id = _task_number(name)
        icon = _STATUS_ICON.get(status, "?")
        # Extract brief title from stem (strip number prefix, replace _ with spaces)
        title_part = re.sub(r"^\d{4}_", "", stem).replace("_", " ")
        if len(title_part) > 40:
            title_part = title_part[:37] + "..."
        lines.append(f"{icon} #{display_id} {title_part}  [{status}] {ts_str}")

    return "\n".join(lines)


def _sanitize_title(title: str) -> str:
    """Sanitize title for filename: replace non-alnum/underscore/hyphen with _, truncate to 80."""
    s = re.sub(r"[^a-zA-Z0-9_-]", "_", title)
    return s[:80]


def _next_task_number() -> str:
    """Determine the next 4-digit zero-padded task number from TASKS/."""
    if not TASKS.exists():
        return "0001"
    highest = 0
    for p in TASKS.iterdir():
        m = re.match(r"^(\d{4})", p.name)
        if m:
            highest = max(highest, int(m.group(1)))
    return f"{highest + 1:04d}"


def _store_intent(stem: str, intent: str) -> None:
    """Persist task intent (chat/task) for the notifier to read later."""
    INTENTS_DIR.mkdir(parents=True, exist_ok=True)
    (INTENTS_DIR / f"{stem}.intent").write_text(intent, encoding="utf-8")


def load_intent(stem: str) -> str:
    """Read stored intent for a task stem. Default: 'task'."""
    try:
        return (INTENTS_DIR / f"{stem}.intent").read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return "task"


def handle_run_task(chat_id: str, title: str, body: str = "",
                    intent: str = "task") -> tuple[str, str]:
    """Create a task file from a run_task action.

    Returns (response_string, task_stem) so callers can track the task.
    """
    sanitized = _sanitize_title(title)
    number = _next_task_number()
    filename = f"{number}_{sanitized}.md"
    path = TASKS / filename

    # Include title as first line so the watcher classifier sees the full
    # task description (title goes into filename but gets sanitized; body
    # alone may lack classifier-relevant keywords).
    full_body = f"{title}\n\n{body}".strip() if body else title
    path.write_text(full_body, encoding="utf-8")

    stem = f"{number}_{sanitized}"
    _store_intent(stem, intent)
    persist_last_task_id(stem)

    ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    return f"Queued: {filename} ({ts_str} UTC)", stem


def _find_highest_task() -> Path | None:
    """Find the highest-numbered task file in TASKS/ (any status extension)."""
    if not TASKS.exists():
        return None
    best_num = -1
    best_path = None
    for p in TASKS.iterdir():
        if p.name.startswith("."):
            continue
        m = re.match(r"^(\d{4})", p.name)
        if m:
            n = int(m.group(1))
            if n > best_num:
                best_num = n
                best_path = p
    # Fallback: if no numbered tasks, pick most recent by mtime
    if best_path is None:
        candidates = [p for p in TASKS.iterdir() if not p.name.startswith(".")]
        if candidates:
            best_path = max(candidates, key=lambda p: p.stat().st_mtime)
    return best_path


def handle_get_last(chat_id: str) -> str:
    """Build the /last response."""
    task = _find_highest_task()
    if task is None:
        return "No tasks found."

    name = task.name
    stem = _task_stem(name)
    status = _task_status(name)
    mtime = datetime.fromtimestamp(task.stat().st_mtime, tz=timezone.utc)
    ts_str = mtime.strftime("%Y-%m-%d %H:%M")

    mode = load_chat_mode(chat_id)
    header = f"#{_task_number(name)} {stem} [{status}]\nCreated: {ts_str} UTC"

    if mode == "compact":
        return header

    body = task.read_text(encoding="utf-8", errors="replace")
    if len(body) > 2000:
        body = body[:2000] + "... [truncated]"

    result = f"{header}\n\n{body}" if body else header

    if mode == "verbose":
        size = task.stat().st_size
        result += f"\n\nPath: {task}\nSize: {size} bytes"

    return result


def _resolve_output_file(filename: str) -> Path | None:
    """Resolve a filename against OUTPUT/ with .md fallback and prefix matching."""
    if not OUTPUT.exists():
        return None

    # Exact match
    exact = OUTPUT / filename
    if exact.is_file():
        return exact

    # Try appending .md
    if not filename.endswith(".md"):
        exact_md = OUTPUT / (filename + ".md")
        if exact_md.is_file():
            return exact_md

    # Prefix match — pick most recent by mtime
    candidates = [
        p for p in OUTPUT.iterdir()
        if p.is_file() and p.name.startswith(filename)
    ]
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)

    return None


_GET_CHUNK = 3000
_GET_MAX_PAGES = 20


def handle_get_output(chat_id: str, filename: str, page: int = 1) -> str:
    """Build the /get response with paging."""
    path = _resolve_output_file(filename)
    if path is None:
        return f'Error: no output file matching "{filename}"'

    content = path.read_text(encoding="utf-8", errors="replace")
    total_pages = min((len(content) + _GET_CHUNK - 1) // _GET_CHUNK, _GET_MAX_PAGES) or 1

    if page < 1 or page > total_pages:
        return f"Error: page {page} out of range (1-{total_pages})"

    start = (page - 1) * _GET_CHUNK
    end = start + _GET_CHUNK
    chunk = content[start:end]

    resolved_name = path.name
    header = f"[{resolved_name} page {page}/{total_pages}]"
    result = f"{header}\n\n{chunk}"

    if page < total_pages:
        result += f"\n\n\u2014 /get {resolved_name} {page + 1} for next page"

    return result


def _find_log_file(task_id: str) -> Path | None:
    """Find a log file matching task_id prefix in LOGS/."""
    if not LOGS.exists():
        return None
    candidates = []
    for p in LOGS.iterdir():
        if not p.is_file():
            continue
        if (p.name.startswith(f"worker_{task_id}") or
                p.name.startswith(f"task_{task_id}")) and p.name.endswith(".log"):
            candidates.append(p)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


_TAIL_RESPONSE_MAX = 3000


def handle_tail_log(chat_id: str, task_id: str, lines: int = 50) -> str:
    """Build the /tail response."""
    if not isinstance(lines, int) or lines <= 0 or lines > 200:
        return "Error: lines must be a positive integer (max 200)"

    log = _find_log_file(task_id)
    if log is None:
        return f'Error: no log file matching "{task_id}"'

    all_lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
    body = "\n".join(tail)

    header = f"[{log.name} \u2014 last {len(tail)} lines]"
    result = f"{header}\n\n{body}"

    if len(result) > _TAIL_RESPONSE_MAX:
        result = result[:_TAIL_RESPONSE_MAX] + "\n... [truncated to 3000 chars]"

    return result


def _find_task_by_id(task_id: str) -> Path | None:
    """Find a task file matching task_id prefix in TASKS/."""
    if not TASKS.exists():
        return None
    candidates = [
        p for p in TASKS.iterdir()
        if not p.name.startswith(".") and p.name.startswith(task_id)
    ]
    if not candidates:
        return None
    # Prefer exact number match, then most recent
    return max(candidates, key=lambda p: p.stat().st_mtime)


def handle_cancel_task(chat_id: str, task_id_or_last: str) -> str:
    """Handle /cancel per protocol v1.1 soft-cancel semantics."""
    # Resolve "last"
    if task_id_or_last == "last":
        task = _find_highest_task()
        if task is None:
            return "Error: no tasks found to cancel"
    else:
        task = _find_task_by_id(task_id_or_last)
        if task is None:
            return f'Error: no task matching "{task_id_or_last}"'

    name = task.name
    stem = _task_stem(name)
    status = _task_status(name)

    cancel_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if status == "queued":
        # Rename .md -> .skip
        new_path = task.parent / f"{stem}.skip"
        task.rename(new_path)
        return f"Cancelled: {stem} (.md \u2192 .skip)"

    if status == "inprogress":
        # Create marker file, do NOT kill process
        marker = TASKS / f".{stem}.cancel_requested"
        marker.write_text("", encoding="utf-8")
        # Append cancellation note to worker log (or task log)
        log = _find_log_file(stem)
        if log is None:
            log = LOGS / f"task_{stem}.log"
        with log.open("a", encoding="utf-8") as f:
            f.write(f"[CANCELLED by user via Telegram at {cancel_ts} UTC]\n")
        return f"Cancel requested: {stem} (will skip after worker exits)"

    if status in ("done", "failed"):
        return f"Error: task {_task_number(name)} is already {status}, cannot cancel"

    if status == "skip":
        return f"Error: task {_task_number(name)} is already cancelled"

    return f"Error: task {_task_number(name)} is in unknown state: {status}"


def handle_set_mode(chat_id: str, mode: str) -> str:
    """Set the chat mode and persist to STATE/chat_modes.json."""
    data = {}
    try:
        data = json.loads(CHAT_MODES_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    data[chat_id] = mode
    CHAT_MODES_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"Mode set to: {mode}"


def handle_get_mode(chat_id: str) -> str:
    """Return the current mode for this chat."""
    mode = load_chat_mode(chat_id)
    return f"Current mode: {mode}"


# --- Auth ---

def _allowed(update: Update) -> bool:
    allowed = os.environ.get("ALLOWED_CHAT_ID", "").strip()
    if not allowed:
        return True
    try:
        return str(update.effective_chat.id) == str(int(allowed))
    except Exception:
        return False


def _guard(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _allowed(update):
            await update.message.reply_text("Not authorized.")
            return
        return await func(update, context)
    return wrapper


# --- CEO Nova conversation handler ---

async def handle_conversation(chat_id: str, text: str) -> str:
    """Handle a conversational message via Claude CLI (fast path).

    No task file is created. The response comes directly from Claude.
    Includes rate limiting, circuit breaker, caching, and metrics.
    """
    _metrics.record_conversation()
    start_time = time.time()

    # Rate limiting (Phase 7)
    allowed, reason = _rate_limiter.check(chat_id)
    if not allowed:
        _metrics.record_rate_limit()
        _log.warning("RATE_LIMITED chat=%s reason=%s", chat_id, reason)
        return RATE_LIMIT_MESSAGE

    # Circuit breaker (Phase 7)
    if _circuit_breaker.is_open():
        _metrics.record_circuit_break()
        _log.warning("CIRCUIT_OPEN chat=%s", chat_id)
        return CIRCUIT_OPEN_MESSAGE

    # Detect session start BEFORE adding the message (so the buffer is still empty/stale)
    is_new_session = _conversations.is_session_start(chat_id)

    # Record the user message in conversation buffer
    _conversations.add_user_message(chat_id, text)

    # Build conversation context from buffer (excluding the message we just added)
    history = _conversations.get_history(chat_id)
    # Remove the last entry (the current message) — it's in the prompt already
    context_history = history[:-1] if len(history) > 1 else []
    context_str = format_history_for_prompt(context_history)

    # Build effective prompt with injected context
    effective_prompt = text
    if is_new_session:
        effective_prompt = f"{SESSION_START_HINT}\n\n{text}"
        _log.info("SESSION_START chat=%s — injecting memory hint", chat_id)

    # Inject active goals as context (Phase 6)
    goals_context = format_goals_for_context()
    if goals_context:
        context_str = f"{goals_context}\n\n{context_str}" if context_str else goals_context

    # Inject active working memory tasks as context (Phase 8)
    wm_context = _working_memory.format_for_context(chat_id)
    if wm_context:
        context_str = f"{wm_context}\n\n{context_str}" if context_str else wm_context

    # Inject recent task completions as context (Phase 5)
    recent = get_recent_completions(max_age_seconds=3600, limit=3)
    if recent:
        task_context = "RECENT BACKGROUND TASK RESULTS (reference if relevant):\n"
        for r in recent:
            task_context += f"- {r['stem']}: {r['summary_line']}\n"
        context_str = f"{task_context}\n{context_str}" if context_str else task_context

    # Response cache check (Phase 7) — skip for session starts (need fresh memory)
    if not is_new_session:
        cached = _response_cache.get(effective_prompt, context_str)
        if cached:
            _metrics.record_cache_hit()
            _log.info("CACHE_HIT chat=%s", chat_id)
            _conversations.add_assistant_message(chat_id, cached)
            _rate_limiter.record(chat_id)
            return cached
    _metrics.record_cache_miss()

    # Call Claude
    response = await generate_response(
        prompt=effective_prompt,
        system_prompt=SYSTEM_PROMPT,
        conversation_context=context_str,
    )

    # Track success/failure for circuit breaker
    is_error = response.startswith("Sorry,") or response.startswith("Something went wrong")
    if is_error:
        _circuit_breaker.record_failure()
        _metrics.record_error()
    else:
        _circuit_breaker.record_success()
        # Cache successful responses (not errors)
        _response_cache.put(effective_prompt, response, context_str)
        # Only consume rate limit tokens on successful responses —
        # don't penalize users for system failures
        _rate_limiter.record(chat_id)

    # Record metrics
    elapsed_ms = (time.time() - start_time) * 1000
    _metrics.record_response_time(elapsed_ms)

    # Record the assistant response in conversation buffer
    _conversations.add_assistant_message(chat_id, response)

    return response


async def handle_delegation_ack(chat_id: str, text: str, task_reply: str) -> str:
    """Generate a natural acknowledgment after delegating to Nova-Core."""
    # Record in conversation buffer
    _conversations.add_user_message(chat_id, text)

    context_str = format_history_for_prompt(_conversations.get_history(chat_id)[:-1])

    # Phase 8: inject active task context for goal-aware ack
    wm_context = _working_memory.format_for_context(chat_id)
    if wm_context:
        context_str = f"{wm_context}\n\n{context_str}" if context_str else wm_context

    # Inject goals context (same as handle_conversation)
    goals_context = format_goals_for_context()
    if goals_context:
        context_str = f"{goals_context}\n\n{context_str}" if context_str else goals_context

    # Inject recent completions for continuity
    recent = get_recent_completions(max_age_seconds=3600, limit=3)
    if recent:
        task_context = "RECENT BACKGROUND TASK RESULTS:\n"
        for r in recent:
            task_context += f"- {r['stem']}: {r['summary_line']}\n"
        context_str = f"{task_context}\n{context_str}" if context_str else task_context

    prompt = (
        f"{DELEGATION_ACK_PROMPT}\n\n"
        f"The user said: {text}\n"
        f"The task has been queued successfully."
    )
    response = await generate_response(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        conversation_context=context_str,
    )

    _conversations.add_assistant_message(chat_id, response)
    return response


# --- Goal and briefing handlers (Phase 6) ---

def _handle_goals(action: dict) -> str:
    """Handle /goals subcommands."""
    sub = action.get("subcommand", "list")

    if sub == "list":
        return format_goals_for_display()
    elif sub == "add":
        goal = add_goal(action["text"])
        return f"Goal #{goal['id']} added: {goal['text']}"
    elif sub == "done":
        goal = complete_goal(action["goal_id"])
        if goal:
            return f"Goal #{goal['id']} completed: {goal['text']}"
        return f"Error: no active goal with ID #{action['goal_id']}"
    elif sub == "remove":
        goal = remove_goal(action["goal_id"])
        if goal:
            return f"Goal #{goal['id']} removed: {goal['text']}"
        return f"Error: no goal with ID #{action['goal_id']}"
    elif sub == "clear":
        count = clear_completed()
        return f"Cleared {count} completed goal(s)." if count else "No completed goals to clear."
    return "Unknown goals subcommand."


BRIEFING_PROMPT = """\
Generate a concise system briefing for the user. Cover:
1. Active goals and their status
2. Recent task activity (what completed, what's running)
3. System health (any issues?)

Keep it to 5-10 sentences. Be natural, not robotic. Lead with the most important information.
Don't use headers or bullet points unless there's a lot to cover. Reference goals by name, not ID.
"""


async def _handle_briefing(chat_id: str) -> str:
    """Generate a system briefing via Claude CLI with full context."""
    # Gather context for the briefing
    parts = []

    # Goals
    goals_ctx = format_goals_for_context()
    if goals_ctx:
        parts.append(goals_ctx)
    else:
        parts.append("NO ACTIVE GOALS SET")

    # Recent completions
    recent = get_recent_completions(max_age_seconds=86400, limit=5)  # last 24h
    if recent:
        parts.append("RECENT TASK COMPLETIONS (last 24h):")
        for r in recent:
            parts.append(f"- {r['stem']}: {r['summary_line']}")
    else:
        parts.append("NO RECENT TASK COMPLETIONS")

    # System health — read HEARTBEAT.md if available
    heartbeat_path = ROOT / "HEARTBEAT.md"
    if heartbeat_path.exists():
        try:
            hb = heartbeat_path.read_text(encoding="utf-8", errors="replace")
            # Just the first 500 chars for summary
            parts.append(f"SYSTEM HEALTH (from HEARTBEAT.md):\n{hb[:500]}")
        except OSError:
            parts.append("SYSTEM HEALTH: unable to read HEARTBEAT.md")

    # Pending tasks
    pending = [p.name for p in TASKS.iterdir()
               if p.is_file() and p.suffix == ".md" and not p.name.startswith(".")]
    if pending:
        parts.append(f"PENDING TASKS IN QUEUE: {len(pending)}")
    else:
        parts.append("TASK QUEUE: empty")

    # CEO Nova metrics (Phase 7)
    snap = _metrics.snapshot()
    parts.append(
        f"CEO NOVA METRICS:\n"
        f"- Messages processed: {snap['total_messages']}\n"
        f"- Conversations: {snap['conversation_messages']}\n"
        f"- Delegations: {snap['task_delegations']}\n"
        f"- Avg response time: {snap['avg_response_time_ms']:.0f}ms\n"
        f"- Errors: {snap['errors']}\n"
        f"- Uptime: {snap['uptime_seconds']}s"
    )

    context = "\n\n".join(parts)

    response = await generate_response(
        prompt=f"{BRIEFING_PROMPT}\n\nCURRENT STATE:\n{context}",
        system_prompt=SYSTEM_PROMPT,
    )

    # Record in conversation buffer
    _conversations.add_assistant_message(chat_id, response)
    return response


# --- Proactive completion notifications (Phase 3) ---

async def _check_completions(app) -> None:
    """Periodic loop: check if any delegated tasks have completed."""
    _log.info("Completion checker started (every 15s)")
    _persist_counter = 0
    while True:
        await asyncio.sleep(15)
        # Persist metrics every ~5 minutes (20 cycles * 15s)
        _persist_counter += 1
        if _persist_counter % 20 == 0:
            _metrics.persist()
            # Cleanup stale working memory entries (>24h old)
            stale_count = _working_memory.cleanup_stale(max_age_seconds=86400)
            if stale_count:
                _log.info("WM_CLEANUP archived %d stale task(s)", stale_count)
            # Phase 9: cleanup stale delegation markers (>24h old)
            _cleanup_stale_delegation_markers(max_age_seconds=86400)
        if not _delegations.has_pending():
            continue

        for stem in _delegations.pending_stems():
            output_path = find_completed_output(stem)
            if output_path is None:
                continue

            # Atomically claim the notification — if we lose, notifier handles it
            if not claim_notification(output_path.name):
                _delegations.complete(stem)
                _log.info("COMPLETION claim lost for %s (notifier got it)", stem)
                continue

            chat_id = _delegations.complete(stem)
            if not chat_id:
                continue

            _log.info("COMPLETION detected: stem=%s output=%s chat=%s",
                      stem, output_path.name, chat_id)

            # Read output and generate natural summary
            try:
                output_content = extract_output_summary(output_path)

                # Phase 8: inject original request context from working memory
                wm_task = _working_memory.complete(stem)
                wm_prefix = ""
                reply_to = None
                if wm_task:
                    wm_prefix = _working_memory.format_completion_context(wm_task) + "\n\n"
                    if wm_task.message_id:
                        reply_to = wm_task.message_id

                summary = await generate_response(
                    prompt=f"{wm_prefix}{COMPLETION_SUMMARY_PROMPT}{output_content}",
                    system_prompt=SYSTEM_PROMPT,
                )

                # Record in conversation buffer for continuity
                _conversations.add_assistant_message(chat_id, summary)

                # Send to user — reply to original message if available
                chunks = chunk_text(summary, chunk_size=4000)
                for i, chunk in enumerate(chunks):
                    kwargs = {"chat_id": chat_id, "text": chunk}
                    if i == 0 and reply_to:
                        kwargs["reply_to_message_id"] = reply_to
                        kwargs["allow_sending_without_reply"] = True
                    await app.bot.send_message(**kwargs)

                # Phase 9: clean up delegation marker after CEO Nova delivers
                _cleanup_delegation_marker(stem)

                _log.info("COMPLETION notified: stem=%s chat=%s len=%d",
                          stem, chat_id, len(summary))
            except Exception as exc:
                _log.error("COMPLETION notification failed for %s: %s",
                           stem, exc, exc_info=True)


# --- Unified message handler ---

@_guard
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Single entry point for all messages. Routes through parse_message."""
    text = update.message.text or ""
    chat_id = str(update.effective_chat.id)
    ts = time.time()

    _log.info("MSG chat=%s len=%d text=%r", chat_id, len(text), text[:80])
    _metrics.record_message()

    result = parse_message(text, chat_id, ts)

    if result is None:
        _log.info("SKIP chat=%s (parse returned None)", chat_id)
        return

    # Parse error: reply with the error string
    if not result["ok"]:
        _log.info("PARSE_ERR chat=%s error=%s", chat_id, result["error"])
        await update.message.reply_text(result["error"])
        return

    action = result["action"]
    action_type = action["action"]
    _log.info("ACTION chat=%s type=%s", chat_id, action_type)

    # Send typing indicator for slow operations (Phase 9: UX polish)
    if action_type in ("conversation", "run_task", "briefing"):
        try:
            await update.effective_chat.send_action(ChatAction.TYPING)
        except Exception:
            pass  # non-critical — don't block on failure

    # Dispatch all actions — extract explicit args from the parsed action dict
    if action_type == "conversation":
        # CEO Nova fast conversation path — no task queue
        reply = await handle_conversation(chat_id, action["text"])
    elif action_type == "show_help":
        reply = handle_help()
    elif action_type == "get_status":
        reply = handle_status(chat_id)
    elif action_type == "run_task":
        # Delegate to task queue, then generate natural acknowledgment
        _metrics.record_delegation()
        task_reply, task_stem = handle_run_task(
            chat_id, action["title"],
            action.get("body", ""),
            intent=action.get("intent", "task"),
        )
        _log.info("DELEGATED chat=%s task=%s stem=%s", chat_id, task_reply, task_stem)
        _delegations.track(task_stem, chat_id)

        # Phase 9: write delegation marker so notifier defers to CEO Nova
        _write_delegation_marker(task_stem)

        # Phase 8: capture working memory for this task
        user_text = action.get("body", "") or action.get("title", "")
        original_msg = text  # raw user message before parsing
        context_snap = _conversations.get_history(chat_id)[-10:]  # last 10 msgs
        task_path = str(TASKS / f"{task_stem}.md")
        _working_memory.add(ActiveTask(
            task_stem=task_stem,
            chat_id=chat_id,
            original_message=original_msg,
            intent_summary=action.get("title", original_msg)[:150],
            created_at=time.time(),
            status="pending",
            context_snapshot=context_snap,
            task_file=task_path,
            message_id=update.message.message_id,
        ))

        reply = await handle_delegation_ack(chat_id, user_text, task_reply)
    elif action_type == "get_last":
        reply = handle_get_last(chat_id)
    elif action_type == "get_output":
        reply = handle_get_output(chat_id, action["filename"], action.get("page", 1))
    elif action_type == "tail_log":
        reply = handle_tail_log(chat_id, action["task_id"], action.get("lines", 50))
    elif action_type == "cancel_task":
        reply = handle_cancel_task(chat_id, action["task_id"])
    elif action_type == "set_mode":
        reply = handle_set_mode(chat_id, action["mode"])
    elif action_type == "get_mode":
        reply = handle_get_mode(chat_id)
    elif action_type == "goals":
        reply = _handle_goals(action)
    elif action_type == "briefing":
        reply = await _handle_briefing(chat_id)
    else:
        reply = f"Unknown action: {action_type}. Try /help"

    # Send response, chunking if needed for Telegram's 4096 char limit
    for chunk in chunk_text(reply, chunk_size=4000):
        await update.message.reply_text(chunk)


# --- Single-instance lock ---

_LOCK_PATH = STATE / "telegram_bot.lock"


def _acquire_lock() -> bool:
    """Acquire an exclusive, non-blocking lock. Returns True if acquired.

    The file descriptor is intentionally kept open (and NOT closed) for the
    lifetime of the process — the OS releases the lock on process exit.
    """
    STATE.mkdir(parents=True, exist_ok=True)
    # Open (or create) the lock file; keep the fd in a global so it survives GC.
    fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    # Write our PID for debugging; truncate any stale content first.
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode())
    # Stash fd so it isn't garbage-collected.
    _acquire_lock._fd = fd  # type: ignore[attr-defined]
    return True


# --- Main ---

def main() -> None:
    if not _acquire_lock():
        print("telegram_bot: another instance is already running — exiting.", flush=True)
        raise SystemExit(0)
    print(f"telegram_bot: lock acquired (pid={os.getpid()})", flush=True)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN env var.")
    app = Application.builder().token(token).build()

    # Single handler catches ALL text (commands + non-commands).
    # parse_message handles routing; non-commands return None (ignored).
    app.add_handler(MessageHandler(filters.TEXT, on_message))

    # Phase 3: start background completion checker after app initializes
    async def _post_init(application) -> None:
        asyncio.create_task(_check_completions(application))
    app.post_init = _post_init

    async def _on_error(update, context):
        from telegram.error import Conflict
        _log.error("Unhandled: %s", context.error, exc_info=context.error)
        if isinstance(context.error, Conflict):
            _log.error("Conflict detected — exiting for systemd restart")
            os._exit(1)
    app.add_error_handler(_on_error)

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
