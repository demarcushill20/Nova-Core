#!/usr/bin/env python3
"""NovaCore Task Watcher & Execution Dispatcher.

Monitors TASKS/ for pending .md files, dispatches each to a
non-interactive Claude subprocess, and verifies output artifacts
before marking tasks as done.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from utils.audit_log import get_audit_logger
from utils.dlp_gate import dlp
from utils.file_watcher import TaskFileWatcher
from utils.langfuse_tracing import trace_llm_call
from utils.structured_log import slog
from utils.task_validator import audit_task_execution, validate_task_content
from utils.trace_context import TraceContext

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
LOG_FILE = LOGS_DIR / "watcher.log"
POLL_INTERVAL = 60  # seconds between scans
TASK_TIMEOUT = 14400  # max seconds per task execution (4 hours)
ARTIFACT_WINDOW = 600  # seconds — OUTPUT file must be this recent
MAX_SUPERVISOR_ATTEMPTS = 2  # total attempts per task (1 original + up to 1 retry)

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


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# --- Duplicate-prevention state ---
_dispatched: set[str] = set()


# --- Helpers ---
def _task_stem(task_name: str) -> str:
    """'0004_foo.md' -> '0004_foo', '0004_foo.md.inprogress' -> '0004_foo'."""
    stem = task_name
    for suffix in (".inprogress", ".md"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


def _find_recent_output(task_stem: str) -> Path | None:
    """Return the newest OUTPUT file whose name contains task_stem,
    created within the last ARTIFACT_WINDOW seconds. None if missing."""
    if not OUTPUT_DIR.exists():
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ARTIFACT_WINDOW)
    matches = sorted(
        (p for p in OUTPUT_DIR.iterdir() if task_stem in p.name and p.suffix == ".md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        return None
    newest = matches[0]
    mtime = datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)
    return newest if mtime >= cutoff else None


# --- Core logic ---
def get_pending_tasks() -> list[Path]:
    """Return sorted list of pending .md task files.

    Ignores: .done, .failed, .inprogress, .cancelled
    """
    if not TASKS_DIR.exists():
        return []
    return sorted(
        p
        for p in TASKS_DIR.iterdir()
        if p.suffix == ".md"
        and not p.name.endswith(".md.done")
        and not p.name.endswith(".md.failed")
        and not p.name.endswith(".md.inprogress")
        and not p.name.endswith(".md.cancelled")
    )


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

    Returns the path to the created retry task file.
    """
    retry_stem = f"{stem}__retry1"
    retry_path = TASKS_DIR / f"{retry_stem}.md"

    error_text = "\n".join(f"- {e}" for e in errors)
    warning_text = "\n".join(f"- {w}" for w in warnings) if warnings else "(none)"

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


def _execute_worker(
    stem: str,
    cmd: list[str],
    worker_log: Path,
    selected_names: list[str],
    skill_flag_note: str,
    attempt: int,
    max_attempts: int,
) -> int:
    """Execute a Claude worker subprocess.

    Returns the process exit code (-1 on timeout/error).
    Appends to *worker_log*; on attempt > 1, writes a retry separator first.
    """
    logger.info("EXECUTION STARTED: %s (attempt %d/%d)", stem, attempt, max_attempts)
    start_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    exit_code = -1
    pid_file = RUNNING_DIR / f"{stem}.pid"

    log_mode = "w" if attempt == 1 else "a"
    with open(worker_log, log_mode) as wf:
        if attempt > 1:
            wf.write(f"\n{'=' * 60}\n")
            wf.write(f"=== SUPERVISOR RETRY: attempt {attempt}/{max_attempts} ===\n")
            wf.write(f"{'=' * 60}\n")
        wf.write(f"=== WORKER LOG: {stem} ===\n")
        wf.write(f"=== START: {start_utc} ===\n")
        wf.write(f"=== SKILLS: {', '.join(selected_names) or '(none)'} ===\n")
        wf.write(
            f"=== COMMAND: {CLAUDE_BIN} -p --verbose --dangerously-skip-permissions{skill_flag_note} <prompt> ===\n\n"
        )

    try:
        child_env = os.environ.copy()
        child_env.pop("CLAUDECODE", None)

        proc = subprocess.Popen(
            cmd,
            cwd="/home/nova/nova-core",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env,
        )

        pid_file.write_text(str(proc.pid), encoding="utf-8")
        logger.info("Worker PID %d written to %s", proc.pid, pid_file)

        try:
            stdout, stderr = proc.communicate(timeout=TASK_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            end_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            logger.error("EXECUTION TIMEOUT: %s (exceeded %ds)", stem, TASK_TIMEOUT)
            with open(worker_log, "a") as wf:
                wf.write(f"=== TIMEOUT after {TASK_TIMEOUT}s ===\n")
                if stdout:
                    wf.write("\n=== STDOUT (partial) ===\n")
                    wf.write(stdout)
                if stderr:
                    wf.write("\n=== STDERR (partial) ===\n")
                    wf.write(stderr)
                wf.write("\n=== EXIT CODE: -1 (timeout) ===\n")
                wf.write(f"=== END: {end_utc} ===\n")
            return -1

        exit_code = proc.returncode
        end_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(worker_log, "a") as wf:
            wf.write("=== STDOUT ===\n")
            wf.write(stdout or "(empty)\n")
            wf.write("\n=== STDERR ===\n")
            wf.write(stderr or "(empty)\n")
            wf.write(f"\n=== EXIT CODE: {exit_code} ===\n")
            wf.write(f"=== END: {end_utc} ===\n")

        logger.info("Claude exited with code %d for %s", exit_code, stem)
        logger.info("Worker log: %s (%d bytes)", worker_log, worker_log.stat().st_size)

    except Exception as exc:
        end_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        logger.exception("EXECUTION ERROR: %s", stem)
        with open(worker_log, "a") as wf:
            wf.write(f"\n=== EXCEPTION: {exc} ===\n")
            wf.write("=== EXIT CODE: -1 (error) ===\n")
            wf.write(f"=== END: {end_utc} ===\n")

    finally:
        pid_file.unlink(missing_ok=True)
        logger.info("PID file removed: %s", pid_file)

    return exit_code


def dispatch(task_path: Path):
    """Dispatch a single task to a non-interactive Claude subprocess."""
    task_name = task_path.name
    stem = _task_stem(task_name)

    # --- Phase 1.2: Kill switch check ---
    ks_mode = check_kill_switch()
    if ks_mode != MODE_RUN:
        logger.info("KILL_SWITCH: mode=%s — skipping task %s", ks_mode, stem)
        return

    # --- Phase 2.2: Budget check before spawning worker ---
    try:
        from agents.budget_enforcer import budget

        can_go, budget_msg = budget.can_proceed()
        if not can_go:
            logger.warning("BUDGET_EXCEEDED: %s — deferring task %s", budget_msg, stem)
            return
    except ImportError:
        pass

    # --- Guard: skip if already dispatched or in-progress ---
    if task_name in _dispatched:
        return
    inprogress_path = task_path.with_name(f"{stem}.md.inprogress")
    if inprogress_path.exists():
        logger.info("Skipping %s — .inprogress file exists.", task_name)
        return

    # --- Claim: atomic rename to .inprogress ---
    _dispatched.add(task_name)
    task_path.rename(inprogress_path)
    logger.info("TASK DETECTED: %s → renamed to %s", task_name, inprogress_path.name)

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
        return

    # --- Read task text for skill selection (cap 50 KB) ---
    try:
        task_text = inprogress_path.read_text(encoding="utf-8")[: 50 * 1024]
    except Exception as exc:
        logger.warning("Could not read task file for skill selection: %s", exc)
        task_text = ""

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
            _session_mgr.record_task_completion(stem, success=passed)
            return
        except Exception as exc:
            logger.error("ORCHESTRATOR ERROR for %s: %s — falling back to worker", stem, exc)
            if routing["feature_flags"].get("fallback_to_worker", True):
                logger.info("FALLBACK: %s → direct worker dispatch", stem)
            else:
                failed_path = inprogress_path.with_name(f"{stem}.md.failed")
                inprogress_path.rename(failed_path)
                logger.error("TASK FAILED (no fallback): %s", stem)
                _session_mgr.record_task_completion(stem, success=False)
                return

    # --- Skill activation ---
    all_skills = load_skills()
    selected = select_skills(task_text, all_skills)
    selected_names = [s.name for s in selected]
    logger.info("SKILLS SELECTED: %s", ", ".join(selected_names) or "(none)")

    skill_injection_path = WORK_DIR / f"skill_injection_{stem}.txt"
    append_prompt_content = render_append_prompt(selected)
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
        keywords = _extract_keywords(task_text)
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

    # --- Deterministic worker log ---
    cmd = [CLAUDE_BIN, "-p", "--verbose", "--dangerously-skip-permissions", "--model", "claude-opus-4-6"]
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

    # --- Execute: always create worker log, even on failure ---
    logger.info("EXECUTION STARTED: %s", stem)
    start_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    exit_code = -1
    pid_file = RUNNING_DIR / f"{stem}.pid"

    # Create the log file immediately so it exists during execution
    with open(worker_log, "w") as wf:
        wf.write(f"=== WORKER LOG: {stem} ===\n")
        wf.write(f"=== START: {start_utc} ===\n")
        wf.write(f"=== SKILLS: {', '.join(selected_names) or '(none)'} ===\n")
        cmd_str = f"{CLAUDE_BIN} -p --verbose --dangerously-skip-permissions{skill_flag_note}"
        wf.write(f"=== COMMAND: {cmd_str} <prompt> ===\n\n")

    try:
        # Strip CLAUDECODE so the child doesn't refuse to start
        child_env = os.environ.copy()
        child_env.pop("CLAUDECODE", None)
        # Propagate trace context to worker subprocess
        child_env.update(trace_ctx.to_env())

        slog.event("task.worker_start", trace_ctx, cmd=cmd[0], pid_file=str(pid_file))

        proc = subprocess.Popen(
            cmd,
            cwd="/home/nova/nova-core",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env,
        )

        # Write PID file so telegram /cancel can SIGTERM this process
        pid_file.write_text(str(proc.pid), encoding="utf-8")
        logger.info("Worker PID %d written to %s", proc.pid, pid_file)

        try:
            stdout, stderr = proc.communicate(timeout=TASK_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            end_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            logger.error("EXECUTION TIMEOUT: %s (exceeded %ds)", stem, TASK_TIMEOUT)
            with open(worker_log, "a") as wf:
                wf.write(f"=== TIMEOUT after {TASK_TIMEOUT}s ===\n")
                if stdout:
                    wf.write("\n=== STDOUT (partial) ===\n")
                    wf.write(stdout)
                if stderr:
                    wf.write("\n=== STDERR (partial) ===\n")
                    wf.write(stderr)
                wf.write("\n=== EXIT CODE: -1 (timeout) ===\n")
                wf.write(f"=== END: {end_utc} ===\n")
            # skip the normal log-write below
            exit_code = -1
            stdout = None

        if stdout is not None:
            exit_code = proc.returncode
            end_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            with open(worker_log, "a") as wf:
                wf.write("=== STDOUT ===\n")
                wf.write(stdout or "(empty)\n")
                wf.write("\n=== STDERR ===\n")
                wf.write(stderr or "(empty)\n")
                wf.write(f"\n=== EXIT CODE: {exit_code} ===\n")
                wf.write(f"=== END: {end_utc} ===\n")

            logger.info("Claude exited with code %d for %s", exit_code, stem)
            logger.info("Worker log: %s (%d bytes)", worker_log, worker_log.stat().st_size)

            # --- Langfuse LLM call tracing for per-task cost tracking ---
            trace_llm_call(
                name=f"task:{stem}",
                input_text=prompt[:4000],  # truncate to avoid huge payloads
                output_text=(stdout or "")[:4000],
                model="claude-opus-4-6",
                metadata={"task": stem, "exit_code": exit_code, "trace_id": trace_ctx.trace_id},
            )

    except Exception as exc:
        end_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        logger.exception("EXECUTION ERROR: %s", stem)
        with open(worker_log, "a") as wf:
            wf.write(f"\n=== EXCEPTION: {exc} ===\n")
            wf.write("=== EXIT CODE: -1 (error) ===\n")
            wf.write(f"=== END: {end_utc} ===\n")

    finally:
        # Always clean up PID file
        pid_file.unlink(missing_ok=True)
        logger.info("PID file removed: %s", pid_file)

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
                "--verbose",
                "--dangerously-skip-permissions",
                "--model",
                "claude-opus-4-6",
                reflection_prompt,
            ]
            retry_exit = _execute_worker(
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
        gate_result = _run_test_gate(stem)
        test_passed = _apply_test_gate_result(stem, gate_result)
        if not test_passed:
            # Tests failed — don't block delivery, but log it
            # (confidence already downgraded to 'low' by _apply_test_gate_result)
            logger.warning("TEST GATE: %s — tests failed, delivering with low confidence", stem)

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


PYTEST_TIMEOUT = 120  # seconds — hard timeout for the test gate


def _run_test_gate(stem: str) -> dict:
    """Run pytest as a quality gate after contract validation.

    Returns dict with keys:
      - status: "pass" | "fail" | "timeout" | "error"
      - summary: human-readable summary string
      - output: raw pytest output (truncated)
    """
    logger.info("TEST GATE: running pytest for %s", stem)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-x", f"--timeout={PYTEST_TIMEOUT}", "-q", "--tb=short"],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=PYTEST_TIMEOUT + 10,  # outer timeout slightly longer
        )
        # Truncate output to avoid bloating the output file
        combined = (result.stdout or "") + (result.stderr or "")
        truncated = combined[-2000:] if len(combined) > 2000 else combined

        if result.returncode == 0:
            logger.info("TEST GATE PASSED: %s (exit 0)", stem)
            return {"status": "pass", "summary": "All tests passed", "output": truncated}
        else:
            logger.warning("TEST GATE FAILED: %s (exit %d)", stem, result.returncode)
            return {"status": "fail", "summary": f"pytest exited with code {result.returncode}", "output": truncated}

    except subprocess.TimeoutExpired:
        logger.warning("TEST GATE TIMEOUT: %s (exceeded %ds) — skipping gate", stem, PYTEST_TIMEOUT)
        return {"status": "timeout", "summary": f"pytest timed out after {PYTEST_TIMEOUT}s", "output": ""}
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


def scan_and_dispatch():
    """Scan for pending tasks and dispatch each sequentially."""
    pending = get_pending_tasks()
    new_tasks = [t for t in pending if t.name not in _dispatched]

    if not new_tasks:
        logger.info("Scan complete — no new tasks.")
        return

    logger.info("Scan complete — %d new task(s) to dispatch.", len(new_tasks))
    for task in new_tasks:
        if not _running:
            break
        dispatch(task)


def run():
    """Main loop: poll TASKS/ every POLL_INTERVAL seconds.

    Phase 4.1: Uses watchdog to detect new files instantly.
    Falls back to pure polling if watchdog is unavailable.
    """
    logger.info("Dispatcher started. Monitoring %s every %ds.", TASKS_DIR, POLL_INTERVAL)
    logger.info("Claude binary: %s | Timeout: %ds | Artifact window: %ds", CLAUDE_BIN, TASK_TIMEOUT, ARTIFACT_WINDOW)

    # Phase 4.1 — start watchdog filesystem monitor
    file_watcher = TaskFileWatcher(TASKS_DIR, _wake_event)
    watchdog_ok = file_watcher.start()
    if watchdog_ok:
        logger.info("Phase 4.1: watchdog active — new tasks trigger immediate wakeup.")
        slog.event("task.watchdog_started", mode="event-driven", poll_fallback_s=POLL_INTERVAL)
    else:
        logger.info("Phase 4.1: watchdog unavailable — polling every %ds.", POLL_INTERVAL)
        slog.event("task.watchdog_fallback", mode="polling", poll_interval_s=POLL_INTERVAL)

    try:
        while _running:
            try:
                scan_and_dispatch()
            except Exception:
                logger.exception("Error during scan cycle.")

            # Wait for watchdog trigger OR poll timeout — whichever comes first
            _wake_event.wait(timeout=POLL_INTERVAL)
            _wake_event.clear()
    finally:
        file_watcher.stop()

    logger.info("Dispatcher stopped.")


if __name__ == "__main__":
    run()
