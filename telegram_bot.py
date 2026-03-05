#!/usr/bin/env python3
from __future__ import annotations

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

from telegram import Update  # noqa: E402  — python-telegram-bot library
from telegram.ext import Application, MessageHandler, ContextTypes, filters  # noqa: E402

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

ROOT = Path("/home/nova/nova-core")
TASKS = ROOT / "TASKS"
OUTPUT = ROOT / "OUTPUT"
LOGS = ROOT / "LOGS"
STATE = ROOT / "STATE"

CANCEL_DIR = STATE / "cancel"
RUNNING_DIR = STATE / "running"
INTENTS_DIR = STATE / "intents"
CHAT_MODES_FILE = STATE / "chat_modes.json"

for _d in (TASKS, OUTPUT, LOGS, STATE, CANCEL_DIR, RUNNING_DIR, INTENTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Logging setup ---
_log = logging.getLogger("telegram_bot")
_log.setLevel(logging.INFO)
_log_handler = logging.StreamHandler(sys.stdout)
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))
_log.addHandler(_log_handler)

# --- Protocol constants (from PROTOCOL/telegram_commands.md v1.1) ---

_HELP_TEXT = (
    "NovaCore Commands:\n"
    "Just type anything \u2014 casual questions get a clean reply.\n\n"
    "/run <title>   \u2014 queue a task (full structured output)\n"
    "/chat <text>   \u2014 force clean chat reply (no report junk)\n"
    "/report <text> \u2014 force full structured report\n"
    "/status        \u2014 show recent tasks and their status\n"
    "/last          \u2014 show most recent task details\n"
    "/get <file>    \u2014 retrieve output file (/get <file> 2 for page 2)\n"
    "/tail <id>     \u2014 tail worker log (/tail <id> 100 for 100 lines)\n"
    "/cancel <id>   \u2014 soft-cancel a task (/cancel last for newest)\n"
    "/mode <level>  \u2014 set notifier verbosity: compact|normal|verbose\n"
    "/help          \u2014 this message"
)

_STATUS_LIMITS = {"compact": 5, "normal": 10, "verbose": 20}

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
        lines.append(f"#{display_id} {stem}  {status}  {ts_str} UTC")

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
                    intent: str = "task") -> str:
    """Create a task file from a run_task action. Returns the response string."""
    sanitized = _sanitize_title(title)
    number = _next_task_number()
    filename = f"{number}_{sanitized}.md"
    path = TASKS / filename

    path.write_text(body, encoding="utf-8")

    stem = f"{number}_{sanitized}"
    _store_intent(stem, intent)
    persist_last_task_id(stem)

    ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    return f"Queued: {filename} ({ts_str} UTC)"


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


# --- Unified message handler ---

@_guard
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Single entry point for all messages. Routes through parse_message."""
    text = update.message.text or ""
    chat_id = str(update.effective_chat.id)
    ts = time.time()

    _log.info("MSG chat=%s len=%d text=%r", chat_id, len(text), text[:80])

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

    # Dispatch all actions — extract explicit args from the parsed action dict
    if action_type == "show_help":
        reply = handle_help()
    elif action_type == "get_status":
        reply = handle_status(chat_id)
    elif action_type == "run_task":
        reply = handle_run_task(chat_id, action["title"], action.get("body", ""),
                                intent=action.get("intent", "task"))
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
    else:
        reply = f"Unknown action: {action_type}. Try /help"

    await update.message.reply_text(reply)


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

    async def _on_error(update, context):
        from telegram.error import Conflict
        _log.error("Unhandled: %s", context.error, exc_info=context.error)
        if isinstance(context.error, Conflict):
            _log.error("Conflict detected — exiting for systemd restart")
            os._exit(1)
    app.add_error_handler(_on_error)

    app.run_polling(drop_pending_updates=True, close_loop=False)


if __name__ == "__main__":
    main()
