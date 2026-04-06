#!/usr/bin/env python3
"""NovaCore Task Watcher & Execution Dispatcher.

Monitors TASKS/ for pending .md files, dispatches each to a
non-interactive Claude subprocess, and verifies output artifacts
before marking tasks as done.

Phase 4.3: Async task execution with bounded concurrency, priority queue,
graceful cancellation, and observability.
"""

import asyncio
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from agents.memory_engine import (
    format_retrieval_for_planner,  # legacy — retained for recall result formatting
)
from agents.memory_router import router as memory_router
from agents.memory_triggers import trigger_engine
from agents.session_manager import SessionManager
from nova_kill_switch import MODE_RUN, check_kill_switch
from tools.contracts import validate_contract
from tools.skills import load_skills, render_append_prompt, select_skills
from tools.task_classifier import classify_and_route
from utils.async_worker_pool import AsyncWorkerPool, TaskPriority, parse_task_priority
from utils.audit_log import get_audit_logger
from utils.cost_router import route_task as cost_route_task
from utils.dlp_gate import dlp
from utils.execution_lanes import classify_lane, get_lane_config
from utils.file_watcher import TaskFileWatcher
from utils.langfuse_tracing import trace_llm_call
from utils.max_plan_guard import record_invocation as _mpg_record
from utils.sd_notify import notify_ready, notify_status, notify_stopping, notify_watchdog
from utils.self_healing import DegradationTier as _DegradationTier
from utils.self_healing import get_degradation_tier as _sh_get_tier
from utils.self_healing import record_error as _sh_record_error
from utils.self_healing import touch_dead_man_switch as _sh_touch
from utils.token_ledger import record_invocation as _ledger_record
from utils.tool_profiles import get_allowed_tools_args

try:
    from utils.warning_router import WarningSeverity as _WarnSev
    from utils.warning_router import emit as _wr_emit
except ImportError:
    _wr_emit = None  # type: ignore[assignment]
    _WarnSev = None  # type: ignore[assignment,misc]
from utils.structured_log import slog
from utils.task_checkpoint import (
    MAX_TASK_RETRIES,
    TaskCheckpoint,
    clear_checkpoint,
    increment_retry,
    list_incomplete_checkpoints,
    load_checkpoint,
    save_checkpoint,
)
from utils.task_checkpoint import _now_iso as _cp_now_iso
from utils.task_checkpoint import (
    update_status as update_checkpoint_status,
)
from utils.task_validator import audit_task_execution, validate_task_content
from utils.trace_context import TraceContext

# Daily session circuit breaker — hard cap on Claude subprocesses per UTC day
try:
    from utils.session_counter import MAX_DAILY_SESSIONS as _session_counter_max
    from utils.session_counter import can_dispatch as _session_can_dispatch
    from utils.session_counter import increment as _session_counter_increment

    _HAS_SESSION_COUNTER = True
except ImportError:
    _HAS_SESSION_COUNTER = False

# Quota-aware scheduling (optional — degrades gracefully if quota file absent)
try:
    from utils.quota_manager import check_emergency_brake as _quota_emergency_brake
    from utils.quota_manager import filter_tasks_for_quota as _quota_filter

    _HAS_QUOTA = True
except ImportError:
    _HAS_QUOTA = False

# Intelligent Dynamic Block Scheduler (optional — degrades gracefully)
try:
    from utils.scheduler.execution_log import (
        create_execution_record,
        log_execution,
    )
    from utils.scheduler.replanner import (
        BlockState,
        load_block_state,
        on_task_complete,
        on_task_fail,
        on_task_start,
        save_block_state,
    )
    from utils.scheduler.work_unit import WorkUnit

    _HAS_SCHEDULER = True
except ImportError:
    _HAS_SCHEDULER = False

# --- Skill evolution: register existing skills in version store (idempotent) ---
_skill_version_store = None
_skill_evolution_queue = None
try:
    from skills.evolution_queue import EvolutionQueue as _EvolutionQueue
    from skills.version_store import SkillVersionStore, register_existing_skills

    _skill_version_store = SkillVersionStore()
    _skill_evolution_queue = _EvolutionQueue()
    _all_skills = load_skills()
    _registered = register_existing_skills(_skill_version_store, _all_skills)
    if _registered:
        logging.getLogger(__name__).info("SKILL EVOLUTION: registered %d new skills in version store", _registered)
except Exception as _sve_exc:
    logging.getLogger(__name__).warning("Skill version store init failed (non-fatal): %s", _sve_exc)
    _skill_version_store = None
    _skill_evolution_queue = None

# --- Audit logger for watcher lifecycle events ---
_audit = get_audit_logger("watcher")

# --- Configuration ---
BASE_DIR = Path(__file__).resolve().parent
TASKS_DIR = BASE_DIR / "TASKS"
OUTPUT_DIR = BASE_DIR / "OUTPUT"
WORK_DIR = BASE_DIR / "WORK"
LOGS_DIR = BASE_DIR / "LOGS"
STATE_DIR = BASE_DIR / "STATE"
CANCEL_DIR = STATE_DIR / "cancel"
RUNNING_DIR = STATE_DIR / "running"
SCHEDULER_STATE_DIR = STATE_DIR / "scheduler"
LOG_FILE = LOGS_DIR / "watcher.log"
POLL_INTERVAL = 60  # seconds between scans
TASK_TIMEOUT = 14400  # max seconds per task execution (4 hours)
ARTIFACT_WINDOW = 600  # seconds — OUTPUT file must be this recent
MAX_SUPERVISOR_ATTEMPTS = 2  # total attempts per task (1 original + up to 1 retry)
MAX_CONCURRENT_TASKS = int(os.environ.get("NOVA_MAX_CONCURRENT_TASKS", "2"))  # Phase 4.3
STALE_TASK_AGE_HOURS = 12  # auto-mark pending .md tasks as .done after this many hours untouched
STALE_REAP_INTERVAL = 3600  # seconds between staleness reaper runs (1 hour)
JIT_SHIFT_INTERVAL = 1800  # seconds between JIT shift generation runs (30 min)

METRICS_FILE = STATE_DIR / "metrics.json"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude")

# Stopwords for keyword extraction (memory retrieval)
_STOPWORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "can",
        "could",
        "of",
        "in",
        "to",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "out",
        "up",
        "down",
        "off",
        "over",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "and",
        "but",
        "or",
        "nor",
        "if",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "my",
        "your",
        "his",
        "her",
        "our",
        "their",
        "what",
        "which",
        "who",
        "whom",
        "please",
        "also",
        "just",
        "about",
        "using",
        "use",
        "used",
        "create",
        "make",
    ]
)


def _extract_keywords(task_text: str, max_keywords: int = 10) -> list[str]:
    """Extract meaningful keywords from task text for memory retrieval."""
    import re

    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", task_text.lower())
    seen = set()
    keywords = []
    for w in words:
        if w not in _STOPWORDS and w not in seen:
            seen.add(w)
            keywords.append(w)
            if len(keywords) >= max_keywords:
                break
    return keywords


def _get_current_block_id() -> str | None:
    """Get the block ID of the currently executing shift block, if any."""
    if not _HAS_SCHEDULER:
        return None
    try:
        # Look for the most recent shift task in TASKS/ that is .inprogress
        inprogress = list(TASKS_DIR.glob("shift_*.md.inprogress"))
        if inprogress:
            stem = inprogress[0].name.replace(".md.inprogress", "")
            # Extract shift_YYYYMMDD from filename like shift_20260329_1_system_health
            parts = stem.split("_")
            if len(parts) >= 2 and parts[0] == "shift":
                block_id = f"{parts[0]}_{parts[1]}"  # shift_YYYYMMDD
                return block_id
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Filename slug → work_mode fallback mapping
# ---------------------------------------------------------------------------
_SLUG_WORK_MODE_MAP: dict[str, str] = {
    "system_health": "monitoring",
    "health": "monitoring",
    "monitoring": "monitoring",
    "implementation": "deep_implementation",
    "impl": "deep_implementation",
    "novatrade": "deep_implementation",
    "research": "research",
    "deep_research": "research",
    "analysis": "analysis",
    "review": "review",
    "testing": "review",
    "quality": "review",
    "free_will": "analysis",
    "autonomy": "analysis",
    "session_wrap": "communication",
    "evening_wrap": "communication",
    "memory_hygiene": "admin",
    "deployment": "deployment",
    "debugging": "debugging",
}


def _extract_task_metadata(task_path: Path) -> dict:
    """Extract scheduler-relevant metadata from a task file's YAML frontmatter.

    Returns a dict with keys: work_mode, task_class, commitment_level,
    estimated_expected_min.  Falls back to sensible defaults if frontmatter
    is missing or unparseable.
    """
    defaults: dict = {
        "work_mode": "admin",
        "task_class": "bounded",
        "commitment_level": "soft",
        "estimated_expected_min": 35.0,
    }
    try:
        raw = task_path.read_text(encoding="utf-8", errors="replace")[:8192]
    except Exception:
        return defaults

    # --- Parse YAML frontmatter (between leading --- markers) ---
    fm: dict = {}
    stripped = raw.lstrip()
    if stripped.startswith("---"):
        parts = stripped.split("---", 2)  # ['', yaml_block, rest]
        if len(parts) >= 3:
            yaml_block = parts[1]
            for line in yaml_block.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                colon_idx = line.find(":")
                if colon_idx > 0:
                    key = line[:colon_idx].strip()
                    val = line[colon_idx + 1 :].strip().strip('"').strip("'")
                    fm[key] = val

    result = dict(defaults)

    # Extract work_mode from frontmatter first
    if "work_mode" in fm:
        result["work_mode"] = fm["work_mode"]
    else:
        # Infer from filename slug as fallback
        stem = task_path.stem.replace(".md", "")  # handle .md.inprogress etc.
        slug_parts = stem.lower().split("_")
        for slug, mode in _SLUG_WORK_MODE_MAP.items():
            slug_tokens = slug.split("_")
            # Check if slug tokens appear as a contiguous subsequence
            for i in range(len(slug_parts) - len(slug_tokens) + 1):
                if slug_parts[i : i + len(slug_tokens)] == slug_tokens:
                    result["work_mode"] = mode
                    break
            else:
                continue
            break

    if "task_class" in fm:
        result["task_class"] = fm["task_class"]
    if "commitment_level" in fm:
        result["commitment_level"] = fm["commitment_level"]
    if "estimated_expected_min" in fm:
        try:
            result["estimated_expected_min"] = float(fm["estimated_expected_min"])
        except (ValueError, TypeError):
            pass

    return result


DISPATCH_PROMPT_TEMPLATE = """\
You are the NovaCore Executive Agent. Execute the task described below.

TASK FILE (read this first):
  {task_path}

WORKING DIRECTORIES (use absolute paths for ALL file operations):
  output_dir = {output_dir}
  work_dir   = {work_dir}
  logs_dir   = {logs_dir}

REQUIRED STEPS — complete every one, in order:

1. Read the task file at {task_path} fully.

2. Perform all work described in the task.
   - If the task asks you to create any file, create it at the exact path specified.
   - If the task references WORK/, use {work_dir}/ as the base directory.
   - Example: "Create WORK/foo.txt" means write to {work_dir}/foo.txt

3. Create an output report at:
     {output_dir}/{task_stem}__<YYYYMMDD-HHMMSS>.md
   The report must summarise what was done and list every file created or modified.

4. Append a one-line summary to {logs_dir}/claude.log

5. Do NOT rename the task file — the dispatcher handles lifecycle.

6. SAVE TO MEMORY SYSTEMS (mandatory for research/planning tasks):
   If this task involves research, analysis, or planning:
   a) Save a dense summary to Fusion Memory via `upsert_memory` with:
      - metadata: {{"category": "research", "project": "nova-core"}}
   b) Save full findings to Obsidian Vault via `vault_write` with:
      - path: "40-research/<topic-slug>.md" (or "00-inbox/" for plans)
      - frontmatter must include: source: "nova-core-memory", tags with "#type/research"
   If this task is NOT research/planning, skip this step.

7. SELF-CHECK (mandatory before exiting):
   - List the contents of {output_dir}/ and confirm your report file exists.
   - List the contents of {work_dir}/ and confirm any work artifacts exist.
   - If any required file is missing, create it NOW before exiting.

8. CONTRACT BLOCK (mandatory — your output report WILL BE REJECTED without this):
   Your output report MUST end with a ## CONTRACT block containing ALL of these fields.
   Copy this template and fill in every field — do not omit any:

   ## CONTRACT
   summary: <one-line description of what was done>
   files_changed: <comma-separated list of files created or modified, or "none" if no files changed>
   verification: <how you confirmed correctness — e.g. "ran tests", "confirmed file exists", or "not run">
   confidence: <low | medium | high>

   Rules:
   - The ## CONTRACT heading must appear at the END of your output report.
   - Every field above is REQUIRED. Never omit a field.
   - If no files were changed, write: files_changed: none
   - If verification was not possible, write: verification: not run
   - Do not fabricate data. Use honest values.

Begin immediately. Do not ask questions or wait for prompts."""

# --- Ensure directories exist ---
for _d in (TASKS_DIR, OUTPUT_DIR, WORK_DIR, LOGS_DIR, STATE_DIR, CANCEL_DIR, RUNNING_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Session manager (Phase 5) ---
_session_mgr = SessionManager()

# --- Logging setup ---
logger = logging.getLogger("watcher")
logger.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# --- Shutdown handling ---
_running = True
_wake_event = threading.Event()  # Phase 4.1: watchdog trigger for immediate wakeup


def _shutdown(signum, _frame):
    global _running
    sig_name = signal.Signals(signum).name
    logger.info("Received %s — shutting down gracefully.", sig_name)
    _running = False
    _wake_event.set()  # unblock any wait immediately


# NOTE: Sync signal handlers removed — the async run() function registers
# its own handlers via loop.add_signal_handler() which are async-safe.
# The _shutdown() function is retained for use by add_signal_handler().

# --- Duplicate-prevention state ---
_dispatched: set[str] = set()
# Deferred tasks: task_name -> monotonic timestamp when deferred.
# Tasks in this dict are skipped during scan until the cooldown expires.
_deferred: dict[str, float] = {}
_DEFER_COOLDOWN_S: float = 300.0  # 5 minutes before retrying a deferred task


# --- Helpers ---
def _task_stem(task_name: str) -> str:
    """'0004_foo.md' -> '0004_foo', '0004_foo.md.inprogress' -> '0004_foo'."""
    stem = task_name
    for suffix in (".inprogress", ".md"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


def _output_matches_stem(filename: str, stem: str) -> bool:
    """Check if an OUTPUT filename matches a task stem with proper boundary.

    Prevents false matches when one stem is a prefix of another:
    e.g. stem 'shift_1' must NOT match 'shift_10_foo__ts.md'.

    Valid matches after the stem: '__' (timestamp separator), '_' (sub-workflow),
    '.' (extension), or end-of-string.
    """
    if not filename.startswith(stem):
        return False
    rest = filename[len(stem) :]
    return rest == "" or (len(rest) > 0 and rest[0] in ("_", "."))


def _find_recent_output(task_stem: str) -> Path | None:
    """Return the newest OUTPUT file matching task_stem,
    created within the last ARTIFACT_WINDOW seconds. None if missing."""
    if not OUTPUT_DIR.exists():
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ARTIFACT_WINDOW)
    matches = sorted(
        (p for p in OUTPUT_DIR.iterdir() if _output_matches_stem(p.stem, task_stem) and p.suffix == ".md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        return None
    newest = matches[0]
    mtime = datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)
    return newest if mtime >= cutoff else None


# --- Frontmatter helpers ---
def parse_frontmatter(path: Path) -> dict:
    """Parse YAML frontmatter from a task file.

    Returns an empty dict if the file has no frontmatter or parsing fails.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}


def is_task_ready(path: Path) -> bool:
    """Check if a task is ready for dispatch (not scheduled for the future).

    Tasks with no ``scheduled_at`` frontmatter field are always ready.
    Tasks whose ``scheduled_at`` datetime is in the past (or now) are ready.
    Tasks scheduled for the future are skipped.
    """
    fm = parse_frontmatter(path)
    return _check_scheduled(fm.get("scheduled_at"))


def _check_scheduled(scheduled_at) -> bool:
    """Return True if the task should run now, False if it should be deferred."""
    if scheduled_at is None:
        return True
    # yaml.safe_load can produce datetime.date for bare dates (e.g. 2026-03-20)
    if isinstance(scheduled_at, date) and not isinstance(scheduled_at, datetime):
        scheduled_at = datetime(scheduled_at.year, scheduled_at.month, scheduled_at.day)
    elif isinstance(scheduled_at, str):
        try:
            scheduled_at = datetime.fromisoformat(scheduled_at)
        except (ValueError, TypeError):
            return True  # Unparseable → ready immediately
    elif isinstance(scheduled_at, datetime):
        pass  # yaml.safe_load may parse datetime directly
    else:
        return True  # Unexpected type → ready immediately
    now = datetime.now(tz=scheduled_at.tzinfo)
    return now >= scheduled_at


def _check_expired(fm: dict) -> bool:
    """Return True if task has exceeded its expiry_minutes window.

    Tasks without expiry metadata are never considered expired.
    """
    expiry_minutes = fm.get("expiry_minutes")
    generated_at = fm.get("generated_at")
    if expiry_minutes is None or generated_at is None:
        return False
    try:
        expiry_minutes = int(expiry_minutes)
    except (ValueError, TypeError):
        return False
    if expiry_minutes <= 0:
        return False
    if isinstance(generated_at, str):
        try:
            gen_dt = datetime.fromisoformat(generated_at)
        except (ValueError, TypeError):
            return False
    elif isinstance(generated_at, datetime):
        gen_dt = generated_at
    else:
        return False
    now = datetime.now(tz=gen_dt.tzinfo)
    return now > gen_dt + timedelta(minutes=expiry_minutes)


# --- Core logic ---
def get_pending_tasks() -> list[Path]:
    """Return sorted list of pending .md task files.

    Ignores: .done, .failed, .inprogress, .cancelled
    Defers: tasks with a future ``scheduled_at`` frontmatter field.
    Expires: tasks past their ``expiry_minutes`` window (auto-marked .done).
    """
    if not TASKS_DIR.exists():
        return []
    ready: list[Path] = []
    for p in sorted(TASKS_DIR.iterdir()):
        if p.suffix != ".md":
            continue
        fm = parse_frontmatter(p)
        if not _check_scheduled(fm.get("scheduled_at")):
            logger.debug(
                "Task %s scheduled for %s, skipping (not yet ready)",
                p.name,
                fm.get("scheduled_at"),
            )
            continue
        if _check_expired(fm):
            expired_path = p.with_name(f"{p.stem}.md.done")
            try:
                p.rename(expired_path)
                logger.info(
                    "EXPIRY: %s → %s (generated_at=%s, expiry_minutes=%s)",
                    p.name,
                    expired_path.name,
                    fm.get("generated_at"),
                    fm.get("expiry_minutes"),
                )
            except OSError as exc:
                logger.warning("EXPIRY: rename failed for %s: %s", p.name, exc)
            continue
        ready.append(p)
    return ready


# --- Staleness reaper ---
_last_reap_time: float = 0.0


def reap_stale_tasks(*, force: bool = False) -> list[str]:
    """Auto-mark pending .md tasks as .done if untouched for STALE_TASK_AGE_HOURS.

    Tasks handled via direct Telegram conversation never go through the watcher
    pipeline, so they accumulate as stale .md files. This reaper cleans them up.

    Tasks with a future ``scheduled_at`` frontmatter field are exempt — they are
    legitimately waiting for their scheduled time and must not be reaped early.

    Runs at most once per STALE_REAP_INTERVAL seconds unless force=True.
    Returns list of reaped task filenames.
    """
    global _last_reap_time
    import time as _time

    now = _time.time()
    if not force and (now - _last_reap_time) < STALE_REAP_INTERVAL:
        return []
    _last_reap_time = now

    if not TASKS_DIR.exists():
        return []

    reaped: list[str] = []
    cutoff = now - (STALE_TASK_AGE_HOURS * 3600)

    for p in sorted(TASKS_DIR.iterdir()):
        if p.suffix != ".md":
            continue
        # Skip tasks currently being processed
        if p.with_suffix(".md.inprogress").exists():
            continue
        # Skip tasks with a future scheduled_at — they are properly deferred
        fm = parse_frontmatter(p)
        if not _check_scheduled(fm.get("scheduled_at")):
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            done_path = p.with_name(f"{p.stem}.md.done")
            try:
                p.rename(done_path)
                logger.info(
                    "STALE REAPER: %s → %s (untouched %.1fh)",
                    p.name,
                    done_path.name,
                    (now - mtime) / 3600,
                )
                reaped.append(p.name)
            except OSError as exc:
                logger.warning("STALE REAPER: failed to rename %s: %s", p.name, exc)

    if reaped:
        slog.event("task.stale_reaped", count=len(reaped), tasks=reaped)
    return reaped


# --- Orphan recovery ---
ORPHAN_INPROGRESS_MINUTES = 15  # match heartbeat threshold
ORPHAN_RECOVERY_INTERVAL = 300  # seconds between orphan recovery runs (5 min)
_last_orphan_recovery_time: float = 0.0
_watcher_boot_time: float = time.time()  # Track startup to prevent premature orphan recovery


def recover_orphaned_tasks(*, force: bool = False) -> list[str]:
    """Auto-recover .inprogress tasks that are stuck (worker died/timed out).

    Detects .inprogress files older than ORPHAN_INPROGRESS_MINUTES and:
    - If checkpoint retry count < MAX_TASK_RETRIES: reset to .md for re-dispatch
    - If max retries exhausted AND no OUTPUT exists: rename to .failed
    - If max retries exhausted BUT OUTPUT exists: rename to .done (rescue)

    Includes a startup grace period to prevent mass false-failures when the
    watcher restarts while workers are still executing.

    Runs at most once per ORPHAN_RECOVERY_INTERVAL seconds unless force=True.
    Returns list of recovered/failed task filenames.
    """
    global _last_orphan_recovery_time
    import time as _time

    now_mono = _time.time()
    if not force and (now_mono - _last_orphan_recovery_time) < ORPHAN_RECOVERY_INTERVAL:
        return []

    # Startup grace period: don't run orphan recovery for the first
    # ORPHAN_INPROGRESS_MINUTES after watcher boot. This prevents marking
    # actively-executing tasks as failed after a watcher restart.
    uptime_s = now_mono - _watcher_boot_time
    startup_grace_s = ORPHAN_INPROGRESS_MINUTES * 60
    if not force and uptime_s < startup_grace_s:
        logger.debug(
            "ORPHAN RECOVERY: skipped — watcher uptime %.0fs < grace period %ds",
            uptime_s,
            startup_grace_s,
        )
        return []

    _last_orphan_recovery_time = now_mono

    if not TASKS_DIR.exists():
        return []

    recovered: list[str] = []
    cutoff_seconds = ORPHAN_INPROGRESS_MINUTES * 60

    for ip in sorted(TASKS_DIR.glob("*.inprogress")):
        try:
            mtime = ip.stat().st_mtime
        except OSError:
            continue

        age_s = now_mono - mtime
        if age_s < cutoff_seconds:
            continue  # Not old enough to be orphaned

        stem = ip.name.replace(".md.inprogress", "")
        task_file = TASKS_DIR / f"{stem}.md"
        failed_file = TASKS_DIR / f"{stem}.md.failed"
        done_file = TASKS_DIR / f"{stem}.md.done"

        # Check if retry is possible via checkpoint system.
        # Proactive tasks use a stricter retry limit than global tasks.
        is_proactive = stem.startswith("hb_proactive_")
        is_research = is_proactive and "research" in stem
        can_retry = True
        try:
            cp = load_checkpoint(stem)
            if cp is None:
                # No checkpoint — first recovery attempt, allow it
                can_retry = True
            else:
                if is_proactive:
                    from utils.heartbeat_rules import PROACTIVE_MAX_RETRIES

                    max_r = PROACTIVE_MAX_RETRIES
                else:
                    max_r = MAX_TASK_RETRIES
                if cp.retry_count >= max_r:
                    can_retry = False
        except Exception:
            can_retry = True  # Err on side of retrying

        if can_retry:
            # For proactive tasks, apply adaptive retry context before re-dispatch
            if is_proactive:
                try:
                    _append_retry_context_watcher(ip)
                except Exception:
                    pass  # best-effort — don't block recovery
            try:
                ip.rename(task_file)
                logger.info(
                    "ORPHAN RECOVERY: %s → %s (stuck %.1f min, reset for retry%s)",
                    ip.name,
                    task_file.name,
                    age_s / 60,
                    ", adaptive" if is_proactive else "",
                )
                recovered.append(ip.name)
            except (OSError, FileNotFoundError) as exc:
                logger.warning("ORPHAN RECOVERY: failed to reset %s: %s", ip.name, exc)
        else:
            # Record circuit breaker failure for research injection tasks
            if is_research:
                try:
                    from utils.heartbeat_rules import HeartbeatRulesEngine

                    HeartbeatRulesEngine.record_research_injection_result(
                        success=False,
                        failure_reason=f"max retries exhausted via watcher orphan recovery ({stem})",
                    )
                except Exception:
                    pass

            # Before marking failed, check if OUTPUT exists — the worker may
            # have completed successfully but the watcher missed the lifecycle
            # transition (e.g., watcher restarted during execution).
            output_exists = _find_any_output(stem)
            if output_exists:
                try:
                    ip.rename(done_file)
                    clear_checkpoint(stem)
                    logger.info(
                        "ORPHAN RESCUE: %s → %s (OUTPUT exists at %s — task actually succeeded)",
                        ip.name,
                        done_file.name,
                        output_exists.name,
                    )
                    recovered.append(ip.name)
                    # Correct the circuit breaker — task actually succeeded
                    if is_research:
                        try:
                            from utils.heartbeat_rules import HeartbeatRulesEngine

                            HeartbeatRulesEngine.record_research_injection_result(success=True)
                        except Exception:
                            pass
                except (OSError, FileNotFoundError) as exc:
                    logger.warning("ORPHAN RESCUE: failed to mark %s as done: %s", ip.name, exc)
            else:
                try:
                    ip.rename(failed_file)
                    logger.warning(
                        "ORPHAN RECOVERY: %s → %s (max retries exhausted after %.1f min, no OUTPUT found)",
                        ip.name,
                        failed_file.name,
                        age_s / 60,
                    )
                    recovered.append(ip.name)
                except (OSError, FileNotFoundError) as exc:
                    logger.warning("ORPHAN RECOVERY: failed to mark %s as failed: %s", ip.name, exc)

    if recovered:
        slog.event("task.orphan_recovered", count=len(recovered), tasks=recovered)
    return recovered


def _append_retry_context_watcher(task_path: Path) -> None:
    """Append adaptive retry context to a proactive task before watcher re-dispatch.

    Mirror of heartbeat._append_retry_context for the watcher orphan-recovery
    path, ensuring retries through the watcher are also adaptive (not identical).
    """
    original = task_path.read_text(encoding="utf-8")
    if "RETRY ESCALATION" in original:
        return  # already has retry context

    prior_detail = ""
    try:
        from utils.heartbeat_rules import HeartbeatRulesEngine

        saved_reason = HeartbeatRulesEngine.get_last_failure_reason()
        if saved_reason:
            prior_detail = f"\nPRIOR FAILURE REASON: {saved_reason}\n"
    except Exception:
        pass

    retry_block = (
        "\n\n---\n"
        "## RETRY ESCALATION\n"
        "This task previously failed and was recovered by the watcher.\n"
        f"{prior_detail}\n"
        "Common failure reasons:\n"
        "- Missing ## CONTRACT block in output\n"
        "- Missing required output file in OUTPUT/\n"
        "- Incomplete output format\n\n"
        "MANDATORY: You MUST end your output with a ## CONTRACT block:\n"
        "## CONTRACT\n"
        "summary: <one-line description>\n"
        "files_changed: <comma-separated list>\n"
        "verification: <how you verified>\n"
        "confidence: <high|medium|low>\n"
    )
    task_path.write_text(original + retry_block, encoding="utf-8")


def _find_any_output(task_stem: str) -> Path | None:
    """Return the newest OUTPUT file matching task_stem, regardless of age.

    Unlike _find_recent_output() which enforces ARTIFACT_WINDOW, this checks
    for any matching output file. Used by orphan recovery to rescue tasks that
    completed but whose lifecycle transition was missed.
    """
    if not OUTPUT_DIR.exists():
        return None
    matches = sorted(
        (p for p in OUTPUT_DIR.iterdir() if _output_matches_stem(p.stem, task_stem) and p.suffix == ".md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


# --- JIT shift generation ---
_last_jit_time: float = 0.0
_JIT_SCRIPT = str(BASE_DIR / "scripts" / "daily_shift_generator.py")


async def _maybe_run_jit_shift_generation() -> None:
    """Periodically run the JIT shift generator to create upcoming blocks.

    Fires every JIT_SHIFT_INTERVAL seconds. The generator itself handles
    lead-time filtering and duplicate prevention, so this is safe to call
    frequently.
    """
    global _last_jit_time
    now = time.time()
    if (now - _last_jit_time) < JIT_SHIFT_INTERVAL:
        return
    _last_jit_time = now

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            _JIT_SCRIPT,
            "--jit",
            "--lead-minutes",
            "30",
            cwd=str(BASE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0:
            out = stdout.decode().strip()
            if out:
                logger.info("JIT shift gen: %s", out.split("\n")[-1])
        else:
            logger.warning("JIT shift gen failed (rc=%d): %s", proc.returncode, stderr.decode()[:200])
    except asyncio.TimeoutError:
        logger.warning("JIT shift gen timed out after 60s")
    except Exception as exc:
        logger.warning("JIT shift gen error: %s", exc)


def _is_retry_task(stem: str) -> bool:
    """Check if a task stem is already a retry task (contains __retry1)."""
    return "__retry1" in stem


def _original_stem(stem: str) -> str:
    """Extract the original task stem from a retry stem.

    '0012_broken__retry1' -> '0012_broken'
    """
    idx = stem.find("__retry1")
    return stem[:idx] if idx != -1 else stem


def _update_metrics(event: str, tool_name: str | None = None):
    """Increment a counter in STATE/metrics.json.

    Never throws, never blocks.  If the file is corrupt or missing,
    it resets to an empty dict and continues.
    """
    try:
        data: dict = {}
        if METRICS_FILE.exists():
            try:
                raw = METRICS_FILE.read_text(encoding="utf-8")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    data = {}
            except (json.JSONDecodeError, ValueError, OSError):
                data = {}

        if event not in data or not isinstance(data[event], dict):
            data[event] = {"_total": 0}

        data[event]["_total"] = data[event].get("_total", 0) + 1

        key = tool_name or "unknown"
        data[event][key] = data[event].get(key, 0) + 1

        METRICS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass  # Never throw, never block


def _create_retry_task(stem: str, output_file: Path, errors: list[str], warnings: list[str]) -> Path:
    """Create a retry TASK file that asks the agent to repair the contract.

    For proactive research injection tasks, includes adaptive context
    (prior failure reason from circuit breaker) so the retry is not
    identical to the original attempt.

    Returns the path to the created retry task file.
    """
    retry_stem = f"{stem}__retry1"
    retry_path = TASKS_DIR / f"{retry_stem}.md"

    error_text = "\n".join(f"- {e}" for e in errors)
    warning_text = "\n".join(f"- {w}" for w in warnings) if warnings else "(none)"

    # Adaptive context for research injection retries
    adaptive_section = ""
    is_research_proactive = stem.startswith("hb_proactive_") and "research" in stem
    if is_research_proactive:
        prior_reason = ""
        try:
            from utils.heartbeat_rules import HeartbeatRulesEngine

            prior_reason = HeartbeatRulesEngine.get_last_failure_reason()
        except Exception:
            pass
        adaptive_section = (
            "\n## Prior Failure Context\n"
            "This is a proactive research injection task. The most common failure\n"
            "is a missing or incomplete ## CONTRACT block in the output.\n"
        )
        if prior_reason:
            adaptive_section += f"Prior failure reason: {prior_reason}\n"
        adaptive_section += (
            "\nCRITICAL: The output report MUST end with a complete ## CONTRACT block.\n"
            "Do not skip this step under any circumstances.\n"
        )

    content = f"""\
# Retry: Repair Contract for {stem}

## Context
The original task `{stem}` completed execution but its output failed
contract validation. Your job is to repair the output file by adding a
valid `## CONTRACT` block.

## Original task stem
{stem}

## Output file to repair
{output_file}

## Validation errors
{error_text}

## Validation warnings
{warning_text}
{adaptive_section}
## Instructions

1. Read the output file at `{output_file}`.
2. Analyse the existing content to understand what was done.
3. Append a valid `## CONTRACT` block at the end of the file with ALL of
   these required fields:
   - `summary`: one-line summary of what the original task accomplished
   - `files_changed`: comma-separated list of files created or modified,
     or "none" if no files changed
   - `verification`: describe how the result was verified (re-run tests,
     check file existence, inspect output, etc.)
   - `confidence`: a value of `low`, `medium`, or `high` (or a float 0.0–1.0)
4. If verification requires re-running tests or checking status, do so
   and record the results in the `verification` field.
5. Do NOT change the original output content unless strictly necessary.
   The primary goal is contract + verification repair.
6. Create an output report at:
     {OUTPUT_DIR}/{retry_stem}__<YYYYMMDD-HHMMSS>.md
   that summarises the repair. This report MUST also contain a valid
   `## CONTRACT` block.
"""

    retry_path.write_text(content, encoding="utf-8")
    return retry_path


def verify_artifacts(stem: str) -> tuple[bool, list[str]]:
    """Check that required artifacts exist after execution.

    Returns (passed, list_of_messages).
    """
    messages: list[str] = []
    passed = True

    # 1. OUTPUT file must exist and be recent
    output_file = _find_recent_output(stem)
    if output_file:
        messages.append(f"OUTPUT verified: {output_file.name}")
    else:
        messages.append(f"OUTPUT missing: no recent file matching '{stem}' in {OUTPUT_DIR}")
        passed = False

    # 2. Contract validation gate
    if output_file:
        contract_ok, contract_msgs = _check_contract(output_file)
        messages.extend(contract_msgs)
        if contract_ok:
            _update_metrics("contract_success", stem)
            if _is_retry_task(stem):
                _update_metrics("retry_success", stem)
        else:
            passed = False
            _update_metrics("contract_failure", stem)
            if _is_retry_task(stem):
                _update_metrics("retry_failed", stem)
            # --- Retry logic: create ONE retry task if eligible ---
            _maybe_create_retry(stem, output_file, contract_msgs)

    # 3. Task-specific: 0004 requires WORK/real_autonomy_confirmed.txt
    if stem.startswith("0004"):
        confirm_file = WORK_DIR / "real_autonomy_confirmed.txt"
        if confirm_file.exists():
            messages.append(f"WORK artifact verified: {confirm_file.name}")
        else:
            messages.append(f"WORK artifact missing: {confirm_file}")
            passed = False

    return passed, messages


def _maybe_create_retry(stem: str, output_file: Path, contract_msgs: list[str]):
    """Create a retry task if this is the first contract failure for stem.

    Does nothing if:
    - stem is already a retry task (__retry1)
    - a retry task already exists (pending, inprogress, done, or failed)
    """
    # Already a retry — never chain further
    if _is_retry_task(stem):
        logger.info("RETRY SKIP: %s is already a retry task — no further retry.", stem)
        return

    retry_stem = f"{stem}__retry1"

    # Check if any lifecycle variant of the retry task already exists
    for suffix in (".md", ".md.inprogress", ".md.done", ".md.failed", ".md.cancelled"):
        if (TASKS_DIR / f"{retry_stem}{suffix}").exists():
            logger.info("RETRY SKIP: %s already exists — no duplicate retry.", retry_stem)
            return

    # Extract errors and warnings from contract messages
    errors = [m.replace("  contract error: ", "") for m in contract_msgs if m.startswith("  contract error:")]
    warnings = [m.replace("  contract warning: ", "") for m in contract_msgs if m.startswith("  contract warning:")]

    retry_path = _create_retry_task(stem, output_file, errors, warnings)
    _update_metrics("retry_issued", stem)
    logger.info("RETRY CREATED: %s → %s", stem, retry_path.name)


def _check_contract(output_file: Path) -> tuple[bool, list[str]]:
    """Validate the ## CONTRACT block in an output file.

    Returns (ok, list_of_messages).  If invalid, appends a failure
    section to the output file.
    """
    messages: list[str] = []
    text = output_file.read_text(encoding="utf-8")
    result = validate_contract(text)

    if result["valid"]:
        messages.append("CONTRACT validated: all required fields present")
        return True, messages

    # Contract invalid — append failure report to output file
    messages.append("CONTRACT FAILED: output missing valid ## CONTRACT")
    for err in result["errors"]:
        messages.append(f"  contract error: {err}")
    for warn in result["warnings"]:
        messages.append(f"  contract warning: {warn}")

    failure_section = (
        "\n\n---\n## CONTRACT VALIDATION FAILED\n\n"
        "The output did not contain a valid ## CONTRACT block.\n\n"
        "**Errors:**\n"
    )
    for err in result["errors"]:
        failure_section += f"- {err}\n"
    if result["warnings"]:
        failure_section += "\n**Warnings:**\n"
        for warn in result["warnings"]:
            failure_section += f"- {warn}\n"
    failure_section += (
        "\n**Suggestion:** Fix output to include ## CONTRACT "
        "with required fields: summary, verification, confidence, "
        "and at least one action detail field.\n"
    )

    with output_file.open("a", encoding="utf-8") as f:
        f.write(failure_section)

    return False, messages


def _quick_contract_check(stem: str) -> tuple[bool, list[str]]:
    """Quick contract validation for the supervisor retry loop.

    Returns (valid, error_messages).
    Does NOT modify the output file or update metrics — that happens in
    verify_artifacts() after the retry loop exits.
    """
    output_file = _find_recent_output(stem)
    if not output_file:
        return False, ["No recent output file found"]
    text = output_file.read_text(encoding="utf-8")
    result = validate_contract(text)
    if result["valid"]:
        return True, []
    return False, result.get("errors", [])


async def _watchdog_keepalive(interval: int = 300):
    """Send sd_notify watchdog pings during long-running tasks."""
    try:
        while True:
            await asyncio.sleep(interval)
            notify_watchdog()
    except asyncio.CancelledError:
        pass


async def _async_write(path: Path, content: str, mode: str = "a") -> None:
    """Write content to a file without blocking the event loop."""

    def _do():
        with open(path, mode, encoding="utf-8") as f:
            f.write(content)

    await asyncio.to_thread(_do)


async def _execute_worker(
    stem: str,
    cmd: list[str],
    worker_log: Path,
    selected_names: list[str],
    skill_flag_note: str,
    attempt: int,
    max_attempts: int,
    child_env: dict[str, str] | None = None,
) -> int:
    """Execute a Claude worker subprocess (async).

    Returns the process exit code (-1 on timeout/error).
    Appends to *worker_log*; on attempt > 1, writes a retry separator first.
    """
    logger.info("EXECUTION STARTED: %s (attempt %d/%d)", stem, attempt, max_attempts)
    start_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    exit_code = -1
    pid_file = RUNNING_DIR / f"{stem}.pid"

    log_mode = "w" if attempt == 1 else "a"
    header_lines = []
    if attempt > 1:
        header_lines.append(f"\n{'=' * 60}\n")
        header_lines.append(f"=== SUPERVISOR RETRY: attempt {attempt}/{max_attempts} ===\n")
        header_lines.append(f"{'=' * 60}\n")
    header_lines.append(f"=== WORKER LOG: {stem} ===\n")
    header_lines.append(f"=== START: {start_utc} ===\n")
    header_lines.append(f"=== SKILLS: {', '.join(selected_names) or '(none)'} ===\n")
    header_lines.append(
        f"=== COMMAND: {CLAUDE_BIN} -p --verbose --dangerously-skip-permissions{skill_flag_note} <prompt> ===\n\n"
    )
    await _async_write(worker_log, "".join(header_lines), mode=log_mode)

    if child_env is None:
        child_env = os.environ.copy()
        child_env.pop("CLAUDECODE", None)

    # Max-plan guard: extract model from cmd for ledger
    _mpg_model = ""
    for _i, _a in enumerate(cmd):
        if _a == "--model" and _i + 1 < len(cmd):
            _mpg_model = cmd[_i + 1]
            break
    _mpg_t0 = datetime.now(timezone.utc)

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd="/home/nova/nova-core",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )

        await asyncio.to_thread(pid_file.write_text, str(proc.pid), "utf-8")
        logger.info("Worker PID %d written to %s", proc.pid, pid_file)

        # Daily session counter — track actual Claude subprocess spawns
        if _HAS_SESSION_COUNTER:
            _sc_count = _session_counter_increment()
            logger.info("SESSION COUNTER: %d/%d for today", _sc_count, _session_counter_max)

        # Start watchdog keepalive pings during long-running subprocess
        keepalive_task = asyncio.create_task(_watchdog_keepalive())

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=TASK_TIMEOUT,
            )
            stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
            # Phase 1: Parse JSON output for token usage
            _input_toks, _output_toks = 0, 0
            if stdout:
                try:
                    import json as _json

                    _resp = _json.loads(stdout)
                    if isinstance(_resp, dict):
                        _usage = _resp.get("usage", {})
                        _input_toks = _usage.get("input_tokens", 0)
                        _output_toks = _usage.get("output_tokens", 0)
                        # Extract the actual text result for downstream processing
                        _result_text = _resp.get("result", "")
                        if _result_text:
                            stdout = _result_text
                except (ValueError, TypeError, KeyError):
                    pass  # Not JSON or unexpected format — use raw stdout
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            end_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            logger.error("EXECUTION TIMEOUT: %s (exceeded %ds)", stem, TASK_TIMEOUT)
            _sh_record_error("watcher._execute_worker", f"timeout after {TASK_TIMEOUT}s", task_id=stem)
            if _wr_emit is not None:
                _wr_emit(
                    "watcher",
                    "runaway",
                    f"Task timeout: {stem} exceeded {TASK_TIMEOUT}s",
                    severity=2,
                    context={"task": stem, "timeout": TASK_TIMEOUT},
                )
            await _async_write(
                worker_log,
                f"=== TIMEOUT after {TASK_TIMEOUT}s ===\n\n=== EXIT CODE: -1 (timeout) ===\n=== END: {end_utc} ===\n",
            )
            return -1
        except asyncio.CancelledError:
            # Graceful cancellation: terminate, wait, then kill if needed
            logger.info("EXECUTION CANCELLED: %s — terminating subprocess", stem)
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.communicate(), timeout=30)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    proc.kill()
                    await proc.communicate()
            end_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            await _async_write(
                worker_log,
                f"\n=== CANCELLED ===\n=== END: {end_utc} ===\n",
            )
            raise
        finally:
            keepalive_task.cancel()

        exit_code = proc.returncode if proc.returncode is not None else 0
        end_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        await _async_write(
            worker_log,
            f"=== STDOUT ===\n"
            f"{stdout or '(empty)'}\n"
            f"\n=== STDERR ===\n"
            f"{stderr or '(empty)'}\n"
            f"\n=== EXIT CODE: {exit_code} ===\n"
            f"=== END: {end_utc} ===\n",
        )

        logger.info("Claude exited with code %d for %s", exit_code, stem)
        log_size = await asyncio.to_thread(lambda: worker_log.stat().st_size)
        logger.info("Worker log: %s (%d bytes)", worker_log, log_size)

        # Phase 1: Record token usage in ledger
        _dur = (datetime.now(timezone.utc) - _mpg_t0).total_seconds()
        if _input_toks > 0 or _output_toks > 0:
            try:
                _ledger_record(
                    caller=stem.split("_")[0] if "_" in stem else "watcher",
                    input_tokens=_input_toks,
                    output_tokens=_output_toks,
                    model=_mpg_model,
                    duration_secs=_dur,
                    task_id=stem,
                )
            except Exception:
                logger.debug("Token ledger record failed for %s (non-fatal)", stem)

    except asyncio.CancelledError:
        raise  # re-raise, already handled above
    except Exception as exc:
        end_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        logger.exception("EXECUTION ERROR: %s", stem)
        _sh_record_error("watcher._execute_worker", exc, task_id=stem)
        if _wr_emit is not None:
            _wr_emit(
                "watcher",
                "error_spike",
                f"Task execution error: {stem} — {exc}",
                severity=1,
                context={"task": stem, "error": str(exc)},
            )
        await _async_write(
            worker_log,
            f"\n=== EXCEPTION: {exc} ===\n=== EXIT CODE: -1 (error) ===\n=== END: {end_utc} ===\n",
        )

    finally:
        await asyncio.to_thread(pid_file.unlink, True)
        logger.info("PID file removed: %s", pid_file)
        # Max-plan guard: record completion
        _mpg_dur = (datetime.now(timezone.utc) - _mpg_t0).total_seconds()
        _mpg_record(
            caller="watcher",
            component="watcher._execute_worker",
            task_id=stem,
            model=_mpg_model,
            success=exit_code == 0,
            duration_secs=_mpg_dur,
        )

    return exit_code


async def dispatch(task_path: Path):
    """Dispatch a single task (async). Wraps _dispatch_inner with cancellation handling."""
    stem = _task_stem(task_path.name)
    task_name = task_path.name
    inprogress_path = task_path.with_name(f"{stem}.md.inprogress")
    try:
        await _dispatch_inner(task_path)
    except asyncio.CancelledError:
        logger.info("TASK CANCELLED (async): %s — saving partial progress", stem)
        _save_partial_progress(stem, inprogress_path)
        slog.event("task.cancelled", stem=stem, reason="async_cancellation")
        _audit.log("task.cancelled", {"task_stem": stem, "reason": "async_cancellation"}, correlation_id=f"task_{stem}")
        raise
    finally:
        # Always remove from _dispatched so the task can be retried on the next
        # poll cycle if it was renamed back to .md (e.g. after timeout or crash).
        _dispatched.discard(task_name)
        _deferred.pop(task_name, None)


async def _dispatch_inner(task_path: Path):
    """Core dispatch logic for a single task."""
    task_name = task_path.name
    stem = _task_stem(task_name)
    inprogress_path = task_path.with_name(f"{stem}.md.inprogress")

    # --- Phase 1.2: Kill switch check ---
    ks_mode = check_kill_switch()
    if ks_mode != MODE_RUN:
        logger.info("KILL_SWITCH: mode=%s — deferring task %s for %ds", ks_mode, stem, int(_DEFER_COOLDOWN_S))
        _deferred[task_name] = time.monotonic()
        return

    # --- Equity drawdown kill switch — blocks only NovaTrade tasks ---
    try:
        from nova_kill_switch import check_equity_kill_switch, is_novatrade_task

        _task_content = ""
        try:
            _task_content = (await asyncio.to_thread(task_path.read_text, "utf-8"))[: 50 * 1024]
        except Exception:
            pass
        if check_equity_kill_switch() and is_novatrade_task(stem, _task_content):
            logger.warning("EQUITY_KILL_SWITCH: deferring NovaTrade task %s for %ds", stem, int(_DEFER_COOLDOWN_S))
            # Move back from .inprogress to .md so it can be retried later
            if await asyncio.to_thread(inprogress_path.exists):
                await asyncio.to_thread(
                    inprogress_path.rename,
                    inprogress_path.with_suffix("").with_suffix(".md"),
                )
            _deferred[task_name] = time.monotonic()
            return
    except ImportError:
        pass  # kill switch module not available

    # --- Phase 2.2: Budget check before spawning worker ---
    try:
        from agents.budget_enforcer import budget

        can_go, budget_msg = budget.can_proceed()
        if not can_go:
            logger.warning("BUDGET_EXCEEDED: %s — deferring task %s for %ds", budget_msg, stem, int(_DEFER_COOLDOWN_S))
            _deferred[task_name] = time.monotonic()
            return
    except ImportError:
        pass

    # --- Guard: skip if already in-progress or completed ---
    # The .inprogress file check is the authoritative filesystem-level guard.
    if await asyncio.to_thread(inprogress_path.exists):
        logger.info("Skipping %s — .inprogress file exists.", task_name)
        return

    # Guard: skip if already completed (.done exists) — prevents runaway loops
    # where a generator recreates a .md file for a task that already finished.
    done_path_check = task_path.with_name(f"{stem}.md.done")
    if await asyncio.to_thread(done_path_check.exists):
        logger.info("Skipping %s — .done file already exists (duplicate .md).", task_name)
        # Remove the duplicate .md file to prevent future re-dispatch
        try:
            await asyncio.to_thread(task_path.unlink)
            logger.info("Removed duplicate .md: %s", task_name)
        except FileNotFoundError:
            pass
        return

    # --- Claim: atomic rename to .inprogress ---
    _dispatched.add(task_name)
    await asyncio.to_thread(task_path.rename, inprogress_path)
    logger.info("TASK DETECTED: %s → renamed to %s", task_name, inprogress_path.name)

    # --- Phase 6B.14: Create checkpoint on dispatch ---
    # Preserve retry_count from any prior checkpoint to prevent infinite re-dispatch loops
    _prior_cp = load_checkpoint(stem)
    _prior_retry = _prior_cp.retry_count if _prior_cp else 0
    save_checkpoint(
        TaskCheckpoint(
            task_id=stem,
            task_file=task_name,
            status="dispatched",
            started_at=_cp_now_iso(),
            last_updated=_cp_now_iso(),
            retry_count=_prior_retry,
        )
    )

    # --- Trace context + audit: task pickup ---
    task_correlation_id = f"task_{stem}"
    trace_ctx = TraceContext.new("watcher", task=stem)
    _audit.log("task.pickup", {"task_stem": stem, "task_file": task_name}, correlation_id=task_correlation_id)
    slog.event("task.pickup", trace_ctx, stem=stem, file=task_name)

    # --- Cancel check: before running Claude, see if cancel was requested ---
    cancel_marker = CANCEL_DIR / f"{stem}.cancel"
    if cancel_marker.exists():
        logger.info("CANCEL DETECTED: %s — skipping execution.", stem)
        cancelled_path = inprogress_path.with_name(f"{stem}.md.cancelled")
        inprogress_path.rename(cancelled_path)
        # Write a cancellation output report
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        report = (
            f"# Cancelled: {stem}\n\n"
            f"**Task:** {stem}\n"
            f"**Cancelled:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
            f"Task was cancelled via /cancel before execution started.\n"
        )
        report_path = OUTPUT_DIR / f"{stem}__{stamp}.md"
        report_path.write_text(report, encoding="utf-8")
        logger.info("Cancel report written: %s", report_path.name)
        cancel_marker.unlink(missing_ok=True)
        clear_checkpoint(stem)
        return

    # --- Read task text for skill selection (cap 50 KB) ---
    try:
        task_text = inprogress_path.read_text(encoding="utf-8")[: 50 * 1024]
    except Exception as exc:
        logger.warning("Could not read task file for skill selection: %s", exc)
        task_text = ""

    # --- Extract scheduler metadata from frontmatter ---
    _task_meta: dict = {}
    if _HAS_SCHEDULER:
        try:
            _task_meta = _extract_task_metadata(inprogress_path)
        except Exception:
            _task_meta = {}

    # --- Phase 1.5: Task content validation ---
    validation = validate_task_content(task_text, task_stem=stem)
    if validation["blocked"]:
        logger.warning(
            "TASK_BLOCKED: %s — risk_score=%d risks=%s",
            stem,
            validation["risk_score"],
            [r["name"] for r in validation["risks"]],
        )
        audit_task_execution(stem, task_text, validation, execution_mode="blocked")
        failed_path = inprogress_path.with_name(f"{stem}.md.failed")
        inprogress_path.rename(failed_path)
        clear_checkpoint(stem)
        return
    audit_task_execution(stem, task_text, validation, classification="pre_validation", execution_mode="worker")

    # --- Phase 3.2: DLP scan on task input ---
    dlp_result = dlp.scan(task_text, context="task_input")
    if dlp_result.action == "block":
        logger.warning("DLP_BLOCKED: %s — findings=%s", stem, [f.pattern_name for f in dlp_result.findings])
        _audit.log(
            "task.dlp_blocked",
            {"task_stem": stem, "findings": [f.pattern_name for f in dlp_result.findings]},
            correlation_id=task_correlation_id,
        )
        failed_path = inprogress_path.with_name(f"{stem}.md.failed")
        inprogress_path.rename(failed_path)
        clear_checkpoint(stem)
        return
    if dlp_result.action == "redact":
        logger.info("DLP_REDACT: %s — redacted %d finding(s)", stem, len(dlp_result.findings))
        _audit.log(
            "task.dlp_redacted",
            {"task_stem": stem, "findings": [f.pattern_name for f in dlp_result.findings]},
            correlation_id=task_correlation_id,
        )
        task_text = dlp_result.redacted_text

    # --- Task classification & routing ---
    routing = classify_and_route(task_text)
    stage = routing.get("stage", "")
    logger.info(
        "ROUTING: class=%s confidence=%.2f orchestrator=%s stage=%s reason=%s",
        routing["task_class"],
        routing["confidence"],
        routing["use_orchestrator"],
        stage or "default",
        routing.get("fallback_reason", "routed_to_orchestrator"),
    )

    # --- Phase 4.4: Cost-aware model routing ---
    task_priority = parse_task_priority(task_text)
    cost_decision = cost_route_task(
        task_text,
        task_class=routing["task_class"],
        priority=task_priority.name.lower(),
    )
    routed_model = cost_decision.model
    logger.info(
        "COST_ROUTE: model=%s complexity=%s downgraded=%s budget=%.1f%% est_tokens=%d",
        routed_model,
        cost_decision.complexity.name,
        cost_decision.downgraded,
        cost_decision.budget_utilization_pct,
        cost_decision.estimated_tokens,
    )
    for reason in cost_decision.reasons:
        logger.debug("COST_ROUTE reason: %s", reason)
    slog.event(
        "cost_route.decision",
        trace_ctx,
        stem=stem,
        model=routed_model,
        complexity=cost_decision.complexity.name,
        downgraded=cost_decision.downgraded,
        budget_pct=cost_decision.budget_utilization_pct,
        task_class=routing["task_class"],
    )

    if routing["use_orchestrator"]:
        # Rollout pre-flight: health + rate-limit gating
        from agents.production_hardening import GracefulDegradation

        gd = GracefulDegradation()
        preflight = gd.check_orchestrator_available(routing["task_class"])
        if preflight.action != "proceed":
            logger.info(
                "ROLLOUT GATE: %s → %s (reason=%s, fallback=%s)",
                stem,
                preflight.action,
                preflight.reason,
                preflight.fallback or "worker",
            )
            routing["use_orchestrator"] = False
            routing["fallback_reason"] = f"rollout_gate:{preflight.reason}"

    if routing["use_orchestrator"]:
        logger.info("ORCHESTRATOR PATH: %s (class=%s, stage=%s)", stem, routing["task_class"], stage or "default")
        # Create PID file so production_hardening won't requeue this task
        orch_pid_file = RUNNING_DIR / f"{stem}.pid"
        RUNNING_DIR.mkdir(parents=True, exist_ok=True)
        orch_pid_file.write_text(str(os.getpid()), encoding="utf-8")
        try:
            from tools.orchestrator_adapter import execute_via_orchestrator

            orch_result = execute_via_orchestrator(stem, task_text, inprogress_path, routing=routing)
            # Verify artifacts using standard gate
            passed, messages = verify_artifacts(stem)
            for msg in messages:
                logger.info("VERIFY: %s", msg)
            # Orchestrator-level rejection (e.g. Stage C verifier) overrides artifact check
            if not orch_result.get("success", True):
                orch_error = orch_result.get("plan_summary", {}).get("error", "orchestrator_rejected")
                logger.warning("ORCHESTRATOR REJECTED: %s — %s", stem, orch_error)
                passed = False
            try:
                if passed:
                    done_path = inprogress_path.with_name(f"{stem}.md.done")
                    inprogress_path.rename(done_path)
                    logger.info("TASK SUCCEEDED (orchestrator): %s → %s", stem, done_path.name)
                else:
                    failed_path = inprogress_path.with_name(f"{stem}.md.failed")
                    inprogress_path.rename(failed_path)
                    logger.warning("TASK FAILED (orchestrator): %s → %s", stem, failed_path.name)
            except FileNotFoundError:
                logger.warning("LIFECYCLE RACE: %s .inprogress file already moved — skipping rename", stem)
            finally:
                if passed:
                    clear_checkpoint(stem)
                else:
                    increment_retry(stem)
            _session_mgr.record_task_completion(stem, success=passed)
            # Update research injection circuit breaker for proactive research tasks
            if stem.startswith("hb_proactive_") and "research" in stem:
                try:
                    from utils.heartbeat_rules import HeartbeatRulesEngine

                    _fail_reason = "; ".join(messages[:3]) if not passed and messages else ""
                    HeartbeatRulesEngine.record_research_injection_result(success=passed, failure_reason=_fail_reason)
                except Exception:
                    pass
            return
        except Exception as exc:
            logger.error("ORCHESTRATOR ERROR for %s: %s — falling back to worker", stem, exc)
            if routing["feature_flags"].get("fallback_to_worker", True):
                logger.info("FALLBACK: %s → direct worker dispatch", stem)
            else:
                failed_path = inprogress_path.with_name(f"{stem}.md.failed")
                inprogress_path.rename(failed_path)
                logger.error("TASK FAILED (no fallback): %s", stem)
                increment_retry(stem)
                _session_mgr.record_task_completion(stem, success=False)
                return
        finally:
            orch_pid_file.unlink(missing_ok=True)

    # --- Phase 5: Execution lane classification ---
    _lane = classify_lane(
        task_class=routing["task_class"],
        complexity=cost_decision.complexity,
        work_mode="",
    )
    _lane_config = get_lane_config(_lane, task_class=routing["task_class"])
    logger.info(
        "EXECUTION_LANE: %s (tool_profile=%s, skill_mode=%s, memory=%s, timeout=%ds)",
        _lane.value,
        _lane_config.tool_profile,
        _lane_config.skill_mode,
        _lane_config.memory_enabled,
        _lane_config.timeout_seconds,
    )

    # --- Skill activation ---
    all_skills = load_skills()
    selected = select_skills(task_text, all_skills)
    selected_names = [s.name for s in selected]
    logger.info("SKILLS SELECTED: %s", ", ".join(selected_names) or "(none)")

    skill_injection_path = WORK_DIR / f"skill_injection_{stem}.txt"
    append_prompt_content = render_append_prompt(selected, mode=_lane_config.skill_mode)
    if append_prompt_content:
        skill_injection_path.write_text(append_prompt_content, encoding="utf-8")
        logger.info(
            "SKILL INJECTION: %s (%d bytes)", skill_injection_path.name, len(append_prompt_content.encode("utf-8"))
        )

    # --- Session tracking (Phase 5) ---
    _session_mgr.record_task_start(stem)
    session_context = _session_mgr.build_context_injection(stem)

    # --- Memory retrieval via router (Phase 1 migration) ---
    memory_context = ""
    try:
        task_class = routing.get("task_class", "unknown")
        keywords = _extract_keywords(task_text) if _lane_config.memory_enabled else []
        if keywords:
            recall_result = memory_router.recall(
                query=" ".join(keywords[:5]),
                intent="pattern_retrieval",
                task_class=task_class,
                keywords=keywords,
                caller="watcher.dispatch_task",
                ctx=trace_ctx,
            )
            if recall_result.results:
                memory_context = format_retrieval_for_planner(recall_result.results)
                logger.info(
                    "MEMORY CONTEXT (routed): %d prior pattern(s) for class=%s keywords=%s",
                    recall_result.total_found,
                    task_class,
                    keywords[:5],
                )
    except Exception as exc:
        logger.warning("Memory retrieval failed (non-fatal): %s", exc)

    # --- Build prompt ---
    prompt = DISPATCH_PROMPT_TEMPLATE.format(
        task_path=inprogress_path,
        task_stem=stem,
        output_dir=OUTPUT_DIR,
        work_dir=WORK_DIR,
        logs_dir=LOGS_DIR,
    )
    context_prefix = ""
    if session_context:
        context_prefix += session_context + "\n\n"
        logger.info(
            "SESSION CONTEXT: injected %d bytes from session %s",
            len(session_context),
            _session_mgr.get_or_create_session().session_id,
        )
    if memory_context:
        context_prefix += memory_context + "\n\n"
    if context_prefix:
        prompt = context_prefix + prompt

    # --- Deterministic worker log (Phase 4.4: model from cost router) ---
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--output-format",
        "json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--model",
        routed_model,
    ]
    # Phase 2: Selective MCP tool loading
    _tool_args = get_allowed_tools_args(_lane_config.tool_profile or routing["task_class"])
    if _tool_args:
        cmd.extend(_tool_args)
    if append_prompt_content and skill_injection_path.exists():
        cmd += ["--append-system-prompt", append_prompt_content]
    cmd.append(prompt)
    worker_log = LOGS_DIR / f"worker_{stem}.log"
    LOGS_DIR.mkdir(exist_ok=True)

    skill_flag_note = f" --append-system-prompt <{len(selected_names)} skills>" if selected_names else ""
    logger.info("COMMAND: %s -p --verbose --dangerously-skip-permissions%s <prompt>", CLAUDE_BIN, skill_flag_note)
    logger.info("WORKER LOG: %s", worker_log)
    prompt_header = "\n".join(prompt.splitlines()[:20])
    logger.info("PROMPT (first 20 lines):\n%s", prompt_header)

    # --- Audit: task execution start ---
    _audit.log(
        "task.execution_start",
        {"task_stem": stem, "skills": selected_names, "routing": routing.get("task_class", "unknown")},
        correlation_id=task_correlation_id,
    )

    # --- Execute via async subprocess (Phase 4.3) ---
    # Strip CLAUDECODE so the child doesn't refuse to start
    child_env = os.environ.copy()
    child_env.pop("CLAUDECODE", None)
    # Propagate trace context to worker subprocess
    child_env.update(trace_ctx.to_env())

    slog.event("task.worker_start", trace_ctx, cmd=cmd[0], stem=stem)

    # Phase 6B.14: Update checkpoint to in_progress before subprocess starts
    update_checkpoint_status(stem, "in_progress")

    # --- Scheduler: track task start for calibration ---
    _sched_t0 = time.monotonic()
    _sched_block_id = None
    if _HAS_SCHEDULER:
        try:
            _sched_block_id = _get_current_block_id()
            if _sched_block_id:
                SCHEDULER_STATE_DIR.mkdir(parents=True, exist_ok=True)
                _sched_state = load_block_state(_sched_block_id, state_dir=SCHEDULER_STATE_DIR)
                if _sched_state is None:
                    # On-demand: create minimal BlockState so lifecycle hooks work
                    # even when shift generator didn't persist one.
                    from utils.scheduler.block import BlockPlan

                    _now_utc = datetime.now(timezone.utc)
                    _sched_state = BlockState(
                        block_id=_sched_block_id,
                        original_plan=BlockPlan(),
                        block_start_utc=_now_utc,
                        block_end_utc=_now_utc + timedelta(hours=8),
                    )
                    save_block_state(_sched_state, state_dir=SCHEDULER_STATE_DIR)
                    logger.info("SCHEDULER: created on-demand BlockState '%s'", _sched_block_id)
                on_task_start(_sched_state, task_id=stem)
                save_block_state(_sched_state, state_dir=SCHEDULER_STATE_DIR)
        except Exception as _sched_exc:
            logger.debug("Scheduler on_task_start failed (non-fatal): %s", _sched_exc)

    exit_code = await _execute_worker(
        stem=stem,
        cmd=cmd,
        worker_log=worker_log,
        selected_names=selected_names,
        skill_flag_note=skill_flag_note,
        attempt=1,
        max_attempts=MAX_SUPERVISOR_ATTEMPTS,
        child_env=child_env,
    )

    stdout_for_trace = ""
    if exit_code >= 0:
        # Read worker log tail for Langfuse tracing
        try:
            log_text = worker_log.read_text(encoding="utf-8")
            stdout_for_trace = log_text[-4000:]
        except Exception:
            pass
        trace_llm_call(
            name=f"task:{stem}",
            input_text=prompt[:4000],
            output_text=stdout_for_trace,
            model=routed_model,
            metadata={
                "task": stem,
                "exit_code": exit_code,
                "trace_id": trace_ctx.trace_id,
                "cost_complexity": cost_decision.complexity.name,
                "cost_downgraded": cost_decision.downgraded,
            },
        )

    # --- Verify artifacts (with reflexion retry) ---
    passed, messages = verify_artifacts(stem)
    for msg in messages:
        logger.info("VERIFY: %s", msg)

    # --- Reflexion loop: if contract failed, reflect and retry (bounded) ---
    if not passed and not _is_retry_task(stem) and MAX_SUPERVISOR_ATTEMPTS > 1:
        contract_ok, contract_errors = _quick_contract_check(stem)
        if not contract_ok and contract_errors:
            logger.info("REFLEXION: %s failed contract — attempting in-process retry", stem)
            slog.event("task.reflexion_start", trace_ctx, stem=stem, errors=contract_errors[:5])

            # Build a reflection prompt that includes what went wrong
            error_list = "\n".join(f"- {e}" for e in contract_errors[:10])
            output_file = _find_recent_output(stem)
            output_snippet = ""
            if output_file:
                raw = output_file.read_text(encoding="utf-8")
                output_snippet = raw[-2000:] if len(raw) > 2000 else raw

            reflection_prompt = (
                f"REFLEXION RETRY for task: {stem}\n\n"
                f"Your previous attempt produced output but FAILED contract validation.\n\n"
                f"CONTRACT ERRORS:\n{error_list}\n\n"
                f"PREVIOUS OUTPUT (tail):\n{output_snippet}\n\n"
                f"INSTRUCTIONS:\n"
                f"1. Analyze WHY the contract validation failed\n"
                f"2. Re-read the original task file if needed\n"
                f"3. Produce a corrected output with a valid ## CONTRACT block containing:\n"
                f"   - summary, verification, confidence (high/medium/low)\n"
                f"   - At least one action field (files_modified, tools_used, etc.)\n"
                f"4. Write the corrected output to OUTPUT/\n"
            )

            retry_cmd = [
                CLAUDE_BIN,
                "-p",
                "--output-format",
                "json",
                "--verbose",
                "--dangerously-skip-permissions",
                "--model",
                "claude-opus-4-6",
            ]
            # Phase 2: Fallback profile (all tools) for reflexion retries
            _retry_tool_args = get_allowed_tools_args("fallback")
            if _retry_tool_args:
                retry_cmd.extend(_retry_tool_args)
            retry_cmd.append(reflection_prompt)
            retry_exit = await _execute_worker(
                stem=stem,
                cmd=retry_cmd,
                worker_log=worker_log,
                selected_names=selected_names,
                skill_flag_note=skill_flag_note,
                attempt=2,
                max_attempts=MAX_SUPERVISOR_ATTEMPTS,
            )

            # Re-verify after reflexion retry
            passed, messages = verify_artifacts(stem)
            for msg in messages:
                logger.info("VERIFY (post-reflexion): %s", msg)

            slog.event(
                "task.reflexion_complete",
                trace_ctx,
                stem=stem,
                retry_exit_code=retry_exit,
                passed=passed,
                duration_ms=trace_ctx.elapsed_ms(),
            )

    # --- Test gate: run pytest after contract validation ---
    if passed:
        gate_result = await _run_test_gate(stem)
        test_passed = _apply_test_gate_result(stem, gate_result)
        if not test_passed:
            # Tests failed — don't block delivery, but log it
            # (confidence already downgraded to 'low' by _apply_test_gate_result)
            logger.warning("TEST GATE: %s — tests failed, delivering with low confidence", stem)

    # --- Skill execution analysis (non-fatal, offloaded to thread) ---
    _analysis_log_text = ""
    try:
        _analysis_log_text = worker_log.read_text(encoding="utf-8")[-8000:]
    except Exception:
        pass
    try:
        await asyncio.to_thread(
            _run_skill_analysis,
            stem=stem,
            task_text=task_text,
            selected_names=selected_names,
            log_text=_analysis_log_text,
            passed=passed,
            exit_code=exit_code,
        )
    except Exception:
        logger.warning("Skill analysis thread failed for %s (non-fatal)", stem, exc_info=True)

    # --- Finalize task lifecycle ---
    try:
        if passed:
            done_path = inprogress_path.with_name(f"{stem}.md.done")
            inprogress_path.rename(done_path)
            logger.info("TASK SUCCEEDED: %s → %s", stem, done_path.name)
            _audit.log(
                "task.completed", {"task_stem": stem, "exit_code": exit_code}, correlation_id=task_correlation_id
            )
            slog.event("task.completed", trace_ctx, stem=stem, exit_code=exit_code, duration_ms=trace_ctx.elapsed_ms())
        else:
            failed_path = inprogress_path.with_name(f"{stem}.md.failed")
            inprogress_path.rename(failed_path)
            logger.warning("TASK FAILED: %s → %s (missing artifacts)", stem, failed_path.name)
            _audit.log("task.failed", {"task_stem": stem, "exit_code": exit_code}, correlation_id=task_correlation_id)
            slog.event(
                "task.failed",
                trace_ctx,
                level="warn",
                stem=stem,
                exit_code=exit_code,
                duration_ms=trace_ctx.elapsed_ms(),
            )
    except FileNotFoundError:
        logger.warning("LIFECYCLE RACE: %s .inprogress file already moved — skipping rename", stem)
    finally:
        # Phase 6B.14: Always manage checkpoint — even if rename threw FileNotFoundError.
        # Previously, clear_checkpoint was inside the try block and skipped on race conditions,
        # causing orphaned checkpoints that triggered infinite re-dispatch loops.
        if passed:
            clear_checkpoint(stem)
        else:
            increment_retry(stem)

    # Update research injection circuit breaker for proactive research tasks
    if stem.startswith("hb_proactive_") and "research" in stem:
        try:
            from utils.heartbeat_rules import HeartbeatRulesEngine

            _fail_reason = "; ".join(messages[:3]) if not passed and messages else ""
            HeartbeatRulesEngine.record_research_injection_result(success=passed, failure_reason=_fail_reason)
        except Exception:
            pass

    # --- Scheduler: record execution and lifecycle ---
    if _HAS_SCHEDULER:
        try:
            _sched_duration_min = (time.monotonic() - _sched_t0) / 60.0
            # Create and log execution record for calibration.
            # Build WorkUnit from task frontmatter metadata (or defaults).
            _sched_wu = WorkUnit(
                id=stem,
                title=stem,
                estimated_expected_min=_task_meta.get("estimated_expected_min", 35.0),
                work_mode=_task_meta.get("work_mode", "admin"),
                task_class=_task_meta.get("task_class", "bounded"),
                commitment_level=_task_meta.get("commitment_level", "soft"),
            )
            _sched_outcome = "success" if passed else "fail"
            _sched_quality = 100.0 if passed else 0.0
            _sched_record = create_execution_record(
                work_unit=_sched_wu,
                actual_duration_min=_sched_duration_min,
                outcome=_sched_outcome,
                block_id=_sched_block_id or "",
                outcome_quality=_sched_quality,
            )
            SCHEDULER_STATE_DIR.mkdir(parents=True, exist_ok=True)
            _exec_log_path = SCHEDULER_STATE_DIR / "execution_log.jsonl"
            log_execution(_sched_record, log_path=_exec_log_path)
            logger.info(
                "SCHEDULER: execution logged — %s duration=%.1fmin outcome=%s",
                stem,
                _sched_duration_min,
                "success" if passed else "fail",
            )

            # Update block state with completion/failure
            if _sched_block_id:
                _sched_state = load_block_state(_sched_block_id, state_dir=SCHEDULER_STATE_DIR)
                if _sched_state:
                    if passed:
                        on_task_complete(
                            _sched_state,
                            task_id=stem,
                            actual_duration_min=_sched_duration_min,
                        )
                    else:
                        on_task_fail(_sched_state, task_id=stem, reason="verification_failed")
                    save_block_state(_sched_state, state_dir=SCHEDULER_STATE_DIR)
        except Exception as _sched_exc:
            logger.debug("Scheduler execution logging failed (non-fatal): %s", _sched_exc)

    # --- Scheduler: write vault diary entry for completed shift tasks ---
    if _HAS_SCHEDULER and stem.startswith("shift_"):

        def _write_scheduler_diary() -> None:
            vault_diary = Path("/home/nova/nova-vault/90-diary")
            vault_diary.mkdir(parents=True, exist_ok=True)
            diary_now = datetime.now(timezone.utc)
            diary_date = diary_now.strftime("%Y-%m-%d")
            diary_slug = re.sub(r"[^a-zA-Z0-9_-]", "-", stem)[:60].rstrip("-")
            diary_file = vault_diary / f"sched-{diary_date}-{diary_slug}.md"
            if diary_file.exists():
                return
            diary_duration = (time.monotonic() - _sched_t0) / 60.0
            diary_outcome = "success" if passed else "fail"
            diary_mode = _task_meta.get("work_mode", "unknown")
            diary_class = _task_meta.get("task_class", "unknown")
            diary_optimized = _task_meta.get("scheduler_optimized", "false")
            diary_block = _task_meta.get("shift_block", "?")
            diary_note = (
                f"---\n"
                f"type: diary\n"
                f'title: "Scheduler: {stem}"\n'
                f'date: "{diary_date}"\n'
                f"source: nova-core-scheduler\n"
                f"tags:\n"
                f'  - "#type/diary"\n'
                f'  - "#project/novacore"\n'
                f'  - "#system/scheduler"\n'
                f"---\n\n"
                f"## Scheduled Task Completed\n\n"
                f"| Field | Value |\n"
                f"|-------|-------|\n"
                f"| **Task** | `{stem}` |\n"
                f"| **Block** | {diary_block} |\n"
                f"| **Outcome** | {diary_outcome} |\n"
                f"| **Duration** | {diary_duration:.1f} min |\n"
                f"| **Work Mode** | {diary_mode} |\n"
                f"| **Task Class** | {diary_class} |\n"
                f"| **Optimized** | {diary_optimized} |\n"
                f"| **Completed** | {diary_now.strftime('%Y-%m-%d %H:%M UTC')} |\n"
            )
            diary_file.write_text(diary_note, encoding="utf-8")
            logger.info("SCHEDULER: diary written → %s", diary_file.name)

        try:
            await asyncio.to_thread(_write_scheduler_diary)
        except Exception as _diary_exc:
            logger.debug("Scheduler diary write failed (non-fatal): %s", _diary_exc)

    # --- Session completion recording (Phase 5) ---
    _session_mgr.record_task_completion(stem, success=passed)

    # --- Memory capture via router-based trigger engine (Phase 2 migration) ---
    # Legacy capture_direct_task_memory removed — all memory writes now go
    # through trigger_engine.fire() → router.ingest_event() → router.store().
    try:
        contract = _session_mgr._extract_task_summary(stem)
        task_class = routing.get("task_class", "unknown")
        summary_text = contract.get("summary", "").strip()
        files_changed = contract.get("files_changed", "")
        confidence = contract.get("confidence", "medium")
        event_type = "task_completed" if passed else "task_failed"

        if summary_text:
            trigger_result = trigger_engine.fire(
                trigger_class="task_lifecycle",
                event_type=event_type,
                source="watcher",
                title=f"{event_type}: {stem}"[:100],
                summary=summary_text[:500],
                caller="watcher.dispatch",
                ctx=trace_ctx,
                task_stem=stem,
                task_class=task_class,
                confidence=confidence,
                related_files=[f.strip() for f in files_changed.split(",") if f.strip() and f.strip() != "none"][:10],
                tags=[f"#class/{task_class}"],
            )
            if trigger_result.fired:
                logger.info(
                    "MEMORY TRIGGER: %s → stored=%s layer=%s",
                    stem,
                    trigger_result.stored,
                    trigger_result.assigned_layer,
                )
    except Exception as exc:
        logger.warning("Memory trigger failed (non-fatal): %s", exc)

    # --- Resolve open loops on task completion ---
    if passed:
        try:
            from agents.open_loop_tracker import LoopStore

            loop_store = LoopStore()
            active_loops = loop_store.get_active_loops()
            for loop in active_loops:
                if stem in loop.related_task_ids:
                    memory_router.resolve_open_loop(
                        loop.loop_id,
                        closure_reason=f"Task {stem} completed successfully",
                        closure_evidence=stem,
                        caller="watcher.dispatch",
                        ctx=trace_ctx,
                    )
                    logger.info("LOOP RESOLVED: %s (task %s completed)", loop.loop_id, stem)
        except Exception as exc:
            logger.warning("Open-loop resolution failed (non-fatal): %s", exc)


def _run_skill_analysis(
    stem: str,
    task_text: str,
    selected_names: list[str],
    log_text: str,
    passed: bool,
    exit_code: int,
):
    """Post-task skill execution analysis (non-fatal).

    Analyzes how skills performed during task execution and feeds results
    back into the skill evolution system.  Updates version-store stats and
    enqueues evolution suggestions if any are produced.

    Uses module-level ``_skill_version_store`` and ``_skill_evolution_queue``
    singletons to avoid per-call SQLite/JSON initialization overhead.

    This is a synchronous function — called from async via ``asyncio.to_thread``.
    """
    try:
        if not selected_names:
            return None

        store = _skill_version_store
        if store is None:
            return None

        from skills.evolution_queue import EvolutionRequest
        from skills.execution_analyzer import ExecutionAnalyzer

        analyzer = ExecutionAnalyzer(version_store=store)

        outcome = {"success": passed, "exit_code": exit_code}
        analysis = analyzer.analyze(
            task_id=stem,
            task_description=task_text[:2000],
            selected_skills=selected_names,
            execution_trace=log_text,
            outcome=outcome,
        )

        # Update skill stat counters (selections, executions, completions, etc.)
        analyzer.update_stats(analysis)

        # Persist the analysis for health tracking (includes pattern_hash for dedup)
        analyzer.store_analysis(analysis)

        # Enqueue evolution suggestions if any
        queue = _skill_evolution_queue
        if analysis.evolution_suggestions and queue is not None:
            for suggestion in analysis.evolution_suggestions:
                req = EvolutionRequest(
                    skill_id=suggestion.target_skill_id,
                    skill_name=suggestion.target_skill_name,
                    evolution_type=suggestion.type,
                    direction=suggestion.direction,
                    priority=suggestion.priority,
                    task_id=stem,
                )
                queue.enqueue(req)
            logger.info(
                "SKILL EVOLUTION: %d suggestions enqueued for %s",
                len(analysis.evolution_suggestions),
                stem,
            )

        logger.info(
            "SKILL ANALYSIS: %s — quality=%.2f skills=%d suggestions=%d",
            stem,
            analysis.overall_quality,
            len(analysis.skill_judgments),
            len(analysis.evolution_suggestions),
        )
        return analysis

    except Exception:
        logger.warning("Skill analysis failed for %s (non-fatal)", stem, exc_info=True)
        return None


PYTEST_TIMEOUT = 120  # seconds — hard timeout for the test gate


_test_gate_lock = asyncio.Lock()  # Serialize test gate runs to avoid concurrent pytest


async def _run_test_gate(stem: str) -> dict:
    """Run pytest as a quality gate after contract validation (async).

    Returns dict with keys:
      - status: "pass" | "fail" | "timeout" | "error"
      - summary: human-readable summary string
      - output: raw pytest output (truncated)
    """
    async with _test_gate_lock:
        logger.info("TEST GATE: running pytest for %s", stem)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pytest",
                "tests/",
                "-x",
                f"--timeout={PYTEST_TIMEOUT}",
                "-q",
                "--tb=short",
                cwd=str(BASE_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=PYTEST_TIMEOUT + 10,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                logger.warning("TEST GATE TIMEOUT: %s (exceeded %ds) — skipping gate", stem, PYTEST_TIMEOUT)
                return {"status": "timeout", "summary": f"pytest timed out after {PYTEST_TIMEOUT}s", "output": ""}

            stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
            combined = stdout + stderr
            truncated = combined[-2000:] if len(combined) > 2000 else combined

            if proc.returncode == 0:
                logger.info("TEST GATE PASSED: %s (exit 0)", stem)
                return {"status": "pass", "summary": "All tests passed", "output": truncated}
            else:
                logger.warning("TEST GATE FAILED: %s (exit %d)", stem, proc.returncode)
                return {"status": "fail", "summary": f"pytest exited with code {proc.returncode}", "output": truncated}

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("TEST GATE ERROR: %s — %s — skipping gate", stem, exc)
            return {"status": "error", "summary": f"pytest error: {exc}", "output": ""}


def _apply_test_gate_result(stem: str, gate_result: dict) -> bool:
    """Apply test gate result to the output file.

    Returns True if delivery should proceed, False if tests failed.
    On failure: appends failure summary and downgrades confidence to 'low'.
    On timeout/error: logs warning, returns True (don't block delivery).
    """
    if gate_result["status"] == "pass":
        return True

    if gate_result["status"] in ("timeout", "error"):
        # Don't block delivery on timeout or error
        logger.warning("TEST GATE SKIPPED: %s — %s", stem, gate_result["summary"])
        return True

    # status == "fail" — append failure summary and downgrade confidence
    output_file = _find_recent_output(stem)
    if not output_file:
        return False

    # Append test failure summary to output
    failure_block = (
        f"\n\n---\n## TEST GATE FAILURE\n\n**Status:** {gate_result['summary']}\n\n```\n{gate_result['output']}\n```\n"
    )
    with output_file.open("a", encoding="utf-8") as f:
        f.write(failure_block)

    # Downgrade confidence to 'low' in the CONTRACT block
    text = output_file.read_text(encoding="utf-8")
    import re

    text = re.sub(
        r"(confidence:\s*)(high|medium)",
        r"\1low",
        text,
        count=1,
    )
    output_file.write_text(text, encoding="utf-8")

    logger.warning("TEST GATE: %s — confidence downgraded to low, failure summary appended", stem)
    return False


def _check_partial_tasks() -> None:
    """Detect .partial files from previous interrupted runs and log them."""
    partials = list(TASKS_DIR.glob("*.md.partial"))
    for p in partials:
        stem = p.name.replace(".md.partial", "")
        logger.warning("PARTIAL TASK FOUND: %s — manual review recommended", stem)
        slog.event("task.partial_found", stem=stem, path=str(p))


def _resume_checkpointed_tasks() -> None:
    """Phase 6B.14: Re-queue incomplete tasks from checkpoint files on startup.

    Finds all checkpoints with retry_count < MAX_TASK_RETRIES, restores the
    task file to TASKS/ if possible (from .inprogress), and marks them ready
    for the next scan cycle to pick up.
    """
    try:
        incomplete = list_incomplete_checkpoints()
        if not incomplete:
            return

        logger.info("CHECKPOINT RESUME: found %d incomplete task(s) from prior run", len(incomplete))
        for cp in incomplete:
            stem = cp.task_id
            inprogress = TASKS_DIR / f"{stem}.md.inprogress"
            task_file = TASKS_DIR / f"{stem}.md"
            done_file = TASKS_DIR / f"{stem}.md.done"
            failed_file = TASKS_DIR / f"{stem}.md.failed"

            # If the task already completed (.done/.failed exists), clear stale checkpoint
            if done_file.exists() or failed_file.exists():
                terminal = ".done" if done_file.exists() else ".failed"
                logger.info(
                    "CHECKPOINT RESUME: %s already %s — clearing stale checkpoint",
                    stem,
                    terminal,
                )
                clear_checkpoint(stem)
                slog.event("checkpoint.stale_cleared", stem=stem, reason=f"already_{terminal}")
                continue

            # If the task file is already pending, just log and skip
            if task_file.exists():
                logger.info("CHECKPOINT RESUME: %s already pending in TASKS/ — skipping", stem)
                slog.event("checkpoint.resume_skip", stem=stem, reason="already_pending")
                continue

            # If .inprogress exists, rename back to .md for re-dispatch
            # M7 fix: wrap rename in try/except to handle TOCTOU race where
            # another process deletes the file between exists() and rename()
            if inprogress.exists():
                try:
                    inprogress.rename(task_file)
                except FileNotFoundError:
                    logger.warning(
                        "CHECKPOINT RESUME: %s — .inprogress vanished (race), skipping",
                        stem,
                    )
                    continue
                logger.info(
                    "CHECKPOINT RESUME: %s restored from .inprogress (retry %d)",
                    stem,
                    cp.retry_count,
                )
                slog.event(
                    "checkpoint.resumed",
                    stem=stem,
                    retry_count=cp.retry_count,
                    prior_status=cp.status,
                )
            else:
                # No task file found — checkpoint is orphaned, log and clear
                logger.warning(
                    "CHECKPOINT RESUME: %s — no task file found (.md or .inprogress), clearing orphan checkpoint",
                    stem,
                )
                clear_checkpoint(stem)
                slog.event("checkpoint.orphan_cleared", stem=stem)
    except Exception as exc:
        logger.warning("Checkpoint resume failed (non-fatal): %s", exc)


def _save_partial_progress(stem: str, inprogress_path: Path) -> None:
    """Save partial progress when a task is cancelled mid-execution."""
    try:
        partial_path = inprogress_path.with_name(f"{stem}.md.partial")
        if inprogress_path.exists():
            inprogress_path.rename(partial_path)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        report = (
            f"# Partial: {stem}\n\n"
            f"**Cancelled:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
            f"Task was cancelled during execution. Partial progress may exist in WORK/.\n"
        )
        report_path = OUTPUT_DIR / f"{stem}__{stamp}.partial.md"
        report_path.write_text(report, encoding="utf-8")
        logger.info("PARTIAL SAVED: %s → %s", stem, report_path.name)
        slog.event("task.partial_saved", stem=stem, report=str(report_path))
    except Exception as exc:
        logger.warning("Failed to save partial progress for %s: %s", stem, exc)


def _write_shutdown_state(pool_status: dict | None = None) -> None:
    """Write current state to STATE/shutdown_state.json for clean shutdown.

    Phase 6B.13: Persists in-flight task IDs and degradation tier so
    the next startup can detect whether the previous exit was clean.
    Never throws — shutdown must not fail on state persistence errors.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tier_name = "FULL"
        try:
            tier_name = _sh_get_tier().tier.name
        except Exception:
            pass
        active_tasks = []
        if pool_status and isinstance(pool_status.get("active_tasks"), list):
            active_tasks = pool_status["active_tasks"]
        state = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": "graceful_shutdown",
            "in_flight_task_ids": active_tasks,
            "degradation_tier": tier_name,
            "pid": os.getpid(),
        }
        shutdown_file = STATE_DIR / "shutdown_state.json"
        shutdown_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        logger.info("Shutdown state written: %s", shutdown_file)

        # Phase 6B.14: Save checkpoints for all in-flight tasks during shutdown
        for task_id in active_tasks:
            try:
                cp = load_checkpoint(task_id)
                if cp is not None:
                    cp.status = "partial_output"
                    save_checkpoint(cp)
                    logger.info("Shutdown checkpoint saved for in-flight task: %s", task_id)
            except Exception as cp_exc:
                logger.warning("Failed to save shutdown checkpoint for %s: %s", task_id, cp_exc)
    except Exception as exc:
        logger.warning("Failed to write shutdown state (non-fatal): %s", exc)


async def scan_and_enqueue(pool: AsyncWorkerPool) -> None:
    """Scan for pending tasks and enqueue them into the async worker pool."""
    pending = get_pending_tasks()

    # Expire stale deferrals (cooldown elapsed → eligible for retry)
    now = time.monotonic()
    expired = [k for k, t in _deferred.items() if now - t >= _DEFER_COOLDOWN_S]
    for k in expired:
        _deferred.pop(k, None)

    new_tasks = [t for t in pending if t.name not in _dispatched and t.name not in _deferred]

    # --- Checkpoint retry-cap guard: reject tasks that exhausted retries ---
    if new_tasks:
        capped: list[Path] = []
        for t in new_tasks:
            _cap_stem = _task_stem(t.name)
            _cap_cp = load_checkpoint(_cap_stem)
            if _cap_cp is not None and _cap_cp.retry_count >= MAX_TASK_RETRIES:
                _cap_failed = t.with_name(f"{_cap_stem}.md.failed")
                try:
                    t.rename(_cap_failed)
                    logger.warning(
                        "RETRY CAP: %s → %s (retry_count=%d >= MAX=%d)",
                        t.name,
                        _cap_failed.name,
                        _cap_cp.retry_count,
                        MAX_TASK_RETRIES,
                    )
                    slog.event("task.retry_exhausted", stem=_cap_stem, retry_count=_cap_cp.retry_count)
                except (OSError, FileNotFoundError) as exc:
                    logger.warning("RETRY CAP: rename failed for %s: %s", t.name, exc)
                capped.append(t)
        if capped:
            new_tasks = [t for t in new_tasks if t not in capped]

    if not new_tasks:
        logger.info("Scan complete — no new tasks.")
        return

    # Daily session circuit breaker — halt dispatch if daily cap reached
    if _HAS_SESSION_COUNTER:
        _sc_ok, _sc_reason = _session_can_dispatch()
        if not _sc_ok:
            logger.warning("DAILY SESSION CAP: %s — halting dispatch for today", _sc_reason)
            return

    # Quota-aware filtering — defers non-critical tasks under pressure
    if _HAS_QUOTA and _quota_emergency_brake():
        logger.warning("Quota emergency brake active — skipping all task dispatch this cycle")
        return

    if _HAS_QUOTA:
        accepted, quota_deferred = _quota_filter(new_tasks)
        if quota_deferred:
            logger.info(
                "Quota filter deferred %d task(s): %s",
                len(quota_deferred),
                ", ".join(t.name for t in quota_deferred),
            )
        new_tasks = accepted
        if not new_tasks:
            logger.info("Scan complete — all tasks deferred by quota filter.")
            return

    # Parse priorities and sort: highest priority first
    task_priorities: list[tuple[TaskPriority, Path]] = []
    for task in new_tasks:
        try:
            text = task.read_text(encoding="utf-8")[:2048]  # only need frontmatter
            prio = parse_task_priority(text)
        except Exception:
            prio = TaskPriority.NORMAL
        task_priorities.append((prio, task))

    task_priorities.sort(key=lambda x: x[0].value)

    logger.info(
        "Scan complete — %d new task(s) to enqueue. Priorities: %s",
        len(task_priorities),
        ", ".join(f"{t.name}={p.name}" for p, t in task_priorities),
    )

    for prio, task in task_priorities:
        if not _running:
            break
        stem = _task_stem(task.name)
        _dispatched.add(task.name)
        await pool.submit(task, stem=stem, priority=prio)

    pool_st = pool.status()
    slog.event(
        "pool.scan_complete",
        enqueued=len(task_priorities),
        queue_depth=pool_st["queue_depth"],
        active_workers=pool_st["active_workers"],
    )


async def _async_wait_for_wake(timeout: float) -> None:
    """Bridge threading.Event (_wake_event) to async wait."""
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_wake_event.wait, timeout),
            timeout=timeout + 1,
        )
    except asyncio.TimeoutError:
        pass
    finally:
        _wake_event.clear()


async def run() -> None:
    """Main async loop: poll TASKS/ every POLL_INTERVAL seconds.

    Phase 4.1: Uses watchdog to detect new files instantly.
    Phase 4.3: Async worker pool with bounded concurrency and priority queue.
    """
    global _running

    logger.info("Dispatcher started. Monitoring %s every %ds.", TASKS_DIR, POLL_INTERVAL)
    logger.info(
        "Claude binary: %s | Timeout: %ds | Artifact window: %ds | Max concurrent: %d",
        CLAUDE_BIN,
        TASK_TIMEOUT,
        ARTIFACT_WINDOW,
        MAX_CONCURRENT_TASKS,
    )

    # Phase 4.3 — check for partial tasks from previous interrupted runs
    _check_partial_tasks()

    # Phase 6B.14 — re-queue incomplete checkpointed tasks from prior run
    _resume_checkpointed_tasks()

    # Phase 4.3 — start async worker pool
    pool = AsyncWorkerPool(max_workers=MAX_CONCURRENT_TASKS, worker_fn=dispatch)
    pool.start()
    slog.event("pool.started", max_workers=MAX_CONCURRENT_TASKS)

    # Phase 4.3 — register signal handlers via the event loop
    loop = asyncio.get_running_loop()

    def _async_shutdown(signum: int) -> None:
        global _running
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — shutting down gracefully.", sig_name)
        _running = False
        _wake_event.set()

    loop.add_signal_handler(signal.SIGINT, _async_shutdown, signal.SIGINT)
    loop.add_signal_handler(signal.SIGTERM, _async_shutdown, signal.SIGTERM)

    # Phase 4.1 — start watchdog filesystem monitor
    file_watcher = TaskFileWatcher(TASKS_DIR, _wake_event)
    watchdog_ok = file_watcher.start()
    if watchdog_ok:
        logger.info("Phase 4.1: watchdog active — new tasks trigger immediate wakeup.")
        slog.event("task.watchdog_started", mode="event-driven", poll_fallback_s=POLL_INTERVAL)
    else:
        logger.info("Phase 4.1: watchdog unavailable — polling every %ds.", POLL_INTERVAL)
        slog.event("task.watchdog_fallback", mode="polling", poll_interval_s=POLL_INTERVAL)

    # Phase 6B.15: Signal systemd that startup is complete
    notify_ready()
    notify_status("Polling TASKS/")

    try:
        while _running:
            # Phase 6A: touch dead man's switch every scan cycle
            _sh_touch(component="watcher")

            # Staleness reaper — auto-mark old pending tasks as .done
            reap_stale_tasks()

            # Orphan recovery — reset stuck .inprogress tasks for re-dispatch
            recover_orphaned_tasks()

            # JIT shift generation — create upcoming shift blocks ~1hr before due
            await _maybe_run_jit_shift_generation()

            # Phase 6B: check degradation tier — skip dispatch in EMERGENCY
            _watcher_tier = _sh_get_tier().tier
            if _watcher_tier >= _DegradationTier.EMERGENCY:
                logger.warning("EMERGENCY degradation — skipping task dispatch (DMS still alive)")
            else:
                try:
                    await scan_and_enqueue(pool)
                except Exception as _scan_exc:
                    logger.exception("Error during scan cycle.")
                    _sh_record_error("watcher.scan_cycle", _scan_exc)

            # Phase 6B.15: Tell systemd we're still alive
            notify_watchdog()

            # Wait for watchdog trigger OR poll timeout — whichever comes first
            await _async_wait_for_wake(POLL_INTERVAL)
    finally:
        # Phase 6B.15: Signal systemd that shutdown has begun
        notify_stopping()
        notify_status("Shutting down...")
        logger.info("Shutting down worker pool...")
        await pool.shutdown(timeout=30)
        file_watcher.stop()
        pool_st = pool.status()
        slog.event(
            "pool.final_status",
            **pool_st,
        )

        # Phase 6B.13: Write shutdown state and touch dead man's switch
        _write_shutdown_state(pool_st)
        _sh_touch(component="watcher_clean_shutdown")
        logger.info("Clean shutdown state saved, dead man's switch touched.")

    logger.info("Dispatcher stopped.")


if __name__ == "__main__":
    asyncio.run(run())
