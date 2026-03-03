#!/usr/bin/env python3
"""NovaCore Task Watcher & Execution Dispatcher.

Monitors TASKS/ for pending .md files, dispatches each to a
non-interactive Claude subprocess, and verifies output artifacts
before marking tasks as done.
"""

import os
import signal
import subprocess
import sys
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tools.contracts import validate_contract
from tools.skills import load_skills, select_skills, render_append_prompt

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
POLL_INTERVAL = 60   # seconds between scans
TASK_TIMEOUT = 300   # max seconds per task execution
ARTIFACT_WINDOW = 600  # seconds — OUTPUT file must be this recent

CLAUDE_BIN = "/usr/bin/claude"

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

6. SELF-CHECK (mandatory before exiting):
   - List the contents of {output_dir}/ and confirm your report file exists.
   - List the contents of {work_dir}/ and confirm any work artifacts exist.
   - If any required file is missing, create it NOW before exiting.

Begin immediately. Do not ask questions or wait for prompts."""

# --- Ensure directories exist ---
for _d in (TASKS_DIR, OUTPUT_DIR, WORK_DIR, LOGS_DIR, STATE_DIR, CANCEL_DIR, RUNNING_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Logging setup ---
logger = logging.getLogger("watcher")
logger.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S")

file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# --- Shutdown handling ---
_running = True


def _shutdown(signum, _frame):
    global _running
    sig_name = signal.Signals(signum).name
    logger.info("Received %s — shutting down gracefully.", sig_name)
    _running = False


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
        (p for p in OUTPUT_DIR.iterdir()
         if task_stem in p.name and p.suffix == ".md"),
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
        p for p in TASKS_DIR.iterdir()
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


def _create_retry_task(stem: str, output_file: Path, errors: list[str],
                       warnings: list[str]) -> Path:
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
   - `verification`: describe how the result was verified (re-run tests,
     check file existence, inspect output, etc.)
   - `confidence`: a value of `low`, `medium`, or `high` (or a float 0.0–1.0)
   - At least ONE action-detail field from: `files_changed`,
     `commands_executed`, `git_commands_executed`, `task_id`, `status`,
     `checks_performed`
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
        if not contract_ok:
            passed = False
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


def _maybe_create_retry(stem: str, output_file: Path,
                        contract_msgs: list[str]):
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
    errors = [m.replace("  contract error: ", "")
              for m in contract_msgs if m.startswith("  contract error:")]
    warnings = [m.replace("  contract warning: ", "")
                for m in contract_msgs if m.startswith("  contract warning:")]

    retry_path = _create_retry_task(stem, output_file, errors, warnings)
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


def dispatch(task_path: Path):
    """Dispatch a single task to a non-interactive Claude subprocess."""
    task_name = task_path.name
    stem = _task_stem(task_name)

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
        task_text = inprogress_path.read_text(encoding="utf-8")[:50 * 1024]
    except Exception as exc:
        logger.warning("Could not read task file for skill selection: %s", exc)
        task_text = ""

    # --- Skill activation ---
    all_skills = load_skills()
    selected = select_skills(task_text, all_skills)
    selected_names = [s.name for s in selected]
    logger.info("SKILLS SELECTED: %s", ", ".join(selected_names) or "(none)")

    skill_injection_path = WORK_DIR / f"skill_injection_{stem}.txt"
    append_prompt_content = render_append_prompt(selected)
    if append_prompt_content:
        skill_injection_path.write_text(append_prompt_content, encoding="utf-8")
        logger.info("SKILL INJECTION: %s (%d bytes)",
                     skill_injection_path.name, len(append_prompt_content.encode("utf-8")))

    # --- Build prompt ---
    prompt = DISPATCH_PROMPT_TEMPLATE.format(
        task_path=inprogress_path,
        task_stem=stem,
        output_dir=OUTPUT_DIR,
        work_dir=WORK_DIR,
        logs_dir=LOGS_DIR,
    )

    # --- Deterministic worker log ---
    cmd = [CLAUDE_BIN, "-p", "--verbose", "--dangerously-skip-permissions"]
    if append_prompt_content and skill_injection_path.exists():
        cmd += ["--append-system-prompt", append_prompt_content]
    cmd.append(prompt)
    worker_log = LOGS_DIR / f"worker_{stem}.log"
    LOGS_DIR.mkdir(exist_ok=True)

    skill_flag_note = f" --append-system-prompt <{len(selected_names)} skills>" if selected_names else ""
    logger.info("COMMAND: %s -p --verbose --dangerously-skip-permissions%s <prompt>",
                CLAUDE_BIN, skill_flag_note)
    logger.info("WORKER LOG: %s", worker_log)
    prompt_header = "\n".join(prompt.splitlines()[:20])
    logger.info("PROMPT (first 20 lines):\n%s", prompt_header)

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
        wf.write(f"=== COMMAND: {CLAUDE_BIN} -p --verbose --dangerously-skip-permissions{skill_flag_note} <prompt> ===\n\n")

    try:
        # Strip CLAUDECODE so the child doesn't refuse to start
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
                wf.write(f"\n=== EXIT CODE: -1 (timeout) ===\n")
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

    except Exception as exc:
        end_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        logger.exception("EXECUTION ERROR: %s", stem)
        with open(worker_log, "a") as wf:
            wf.write(f"\n=== EXCEPTION: {exc} ===\n")
            wf.write(f"=== EXIT CODE: -1 (error) ===\n")
            wf.write(f"=== END: {end_utc} ===\n")

    finally:
        # Always clean up PID file
        pid_file.unlink(missing_ok=True)
        logger.info("PID file removed: %s", pid_file)

    # --- Verify artifacts ---
    passed, messages = verify_artifacts(stem)
    for msg in messages:
        logger.info("VERIFY: %s", msg)

    # --- Finalize task lifecycle ---
    if passed:
        done_path = inprogress_path.with_name(f"{stem}.md.done")
        inprogress_path.rename(done_path)
        logger.info("TASK SUCCEEDED: %s → %s", stem, done_path.name)
    else:
        failed_path = inprogress_path.with_name(f"{stem}.md.failed")
        inprogress_path.rename(failed_path)
        logger.warning("TASK FAILED: %s → %s (missing artifacts)", stem, failed_path.name)


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
    """Main loop: poll TASKS/ every POLL_INTERVAL seconds."""
    logger.info("Dispatcher started. Monitoring %s every %ds.", TASKS_DIR, POLL_INTERVAL)
    logger.info("Claude binary: %s | Timeout: %ds | Artifact window: %ds",
                CLAUDE_BIN, TASK_TIMEOUT, ARTIFACT_WINDOW)

    while _running:
        try:
            scan_and_dispatch()
        except Exception:
            logger.exception("Error during scan cycle.")
        for _ in range(POLL_INTERVAL):
            if not _running:
                break
            time.sleep(1)

    logger.info("Dispatcher stopped.")


if __name__ == "__main__":
    run()
