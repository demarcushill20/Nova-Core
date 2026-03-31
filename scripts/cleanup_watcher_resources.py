#!/usr/bin/env python3
"""
cleanup_watcher_resources.py — Clean up stale resources consuming watcher cgroup capacity.

Targets:
  1. Orphaned MCP child processes (parent PID 1 or dead parent, running > 30 min)
  2. Completed .done/.failed task files older than 24 hours (archived to TASKS/archive/)

Designed to run every 2 hours via systemd timer (novacore-cleanup.timer).
"""

import datetime
import logging
import os
import shutil
import signal
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path("/home/nova/nova-core")
TASKS_DIR = BASE_DIR / "TASKS"
ARCHIVE_DIR = TASKS_DIR / "archive"
LOG_FILE = BASE_DIR / "LOGS" / "resource_cleanup.log"

# MCP process name patterns to check for orphans
MCP_PATTERNS = [
    "context7-mcp",
    "pinescript-mcp-server",
    "firecrawl-mcp",
    "sequential-thinking",
    "playwright",
]

# Thresholds
ORPHAN_AGE_MINUTES = 30
TASK_FILE_AGE_HOURS = 24

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("resource_cleanup")
logger.setLevel(logging.INFO)

# File handler
fh = logging.FileHandler(str(LOG_FILE))
fh.setLevel(logging.INFO)
fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s"))
logger.addHandler(fh)

# Console handler
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s"))
logger.addHandler(ch)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_process_info(pid: int) -> dict | None:
    """Read /proc/<pid> to get process metadata. Returns None if process gone."""
    proc = Path(f"/proc/{pid}")
    if not proc.exists():
        return None

    info = {"pid": pid}

    # Command line
    try:
        cmdline = (proc / "cmdline").read_text().replace("\x00", " ").strip()
        info["cmdline"] = cmdline
    except (OSError, PermissionError):
        info["cmdline"] = ""

    # Parent PID
    try:
        status_text = (proc / "status").read_text()
        for line in status_text.splitlines():
            if line.startswith("PPid:"):
                info["ppid"] = int(line.split(":")[1].strip())
                break
    except (OSError, PermissionError, ValueError):
        info["ppid"] = -1

    # Start time (use stat on /proc/<pid> as a proxy — it reflects process creation)
    try:
        info["start_time"] = (proc / "stat").stat().st_mtime
    except (OSError, PermissionError):
        # Fall back to the /proc/<pid> directory ctime
        try:
            info["start_time"] = proc.stat().st_mtime
        except (OSError, PermissionError):
            info["start_time"] = time.time()  # assume recent if unreadable

    return info


def is_parent_alive(ppid: int) -> bool:
    """Check whether the parent process is still running."""
    if ppid <= 1:
        # PID 0 (kernel) or 1 (init/systemd) means the real parent already died
        return False
    return Path(f"/proc/{ppid}").exists()


def find_matching_pids(patterns: list[str]) -> list[int]:
    """Use /proc to find PIDs whose cmdline matches any of the given patterns."""
    matching = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            # Skip PID 1, 2, and our own process
            if pid <= 2 or pid == os.getpid():
                continue
            try:
                cmdline = (entry / "cmdline").read_text().replace("\x00", " ").strip()
            except (OSError, PermissionError):
                continue
            for pattern in patterns:
                if pattern in cmdline:
                    matching.append(pid)
                    break
    except (OSError, PermissionError):
        pass
    return matching


def kill_orphaned_mcp_processes() -> list[dict]:
    """Find and kill orphaned MCP processes older than ORPHAN_AGE_MINUTES."""
    killed = []
    now = time.time()
    age_threshold = ORPHAN_AGE_MINUTES * 60

    pids = find_matching_pids(MCP_PATTERNS)
    logger.info("Found %d candidate MCP processes to evaluate", len(pids))

    for pid in pids:
        info = get_process_info(pid)
        if info is None:
            continue  # already gone

        age_seconds = now - info.get("start_time", now)
        ppid = info.get("ppid", -1)
        cmdline = info.get("cmdline", "")

        # Safety checks: only kill if clearly orphaned AND old enough
        if age_seconds < age_threshold:
            logger.info(
                "SKIP pid=%d (age=%.0fm < %dm): %s",
                pid,
                age_seconds / 60,
                ORPHAN_AGE_MINUTES,
                cmdline[:120],
            )
            continue

        if is_parent_alive(ppid):
            logger.info(
                "SKIP pid=%d (parent %d alive, age=%.0fm): %s",
                pid,
                ppid,
                age_seconds / 60,
                cmdline[:120],
            )
            continue

        # Orphaned and old — kill it
        logger.info(
            "KILL pid=%d (ppid=%d dead/init, age=%.0fm): %s",
            pid,
            ppid,
            age_seconds / 60,
            cmdline[:120],
        )
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "age_min": round(age_seconds / 60, 1),
                    "cmdline": cmdline[:200],
                }
            )
        except ProcessLookupError:
            logger.info("  pid=%d already gone before kill", pid)
        except PermissionError:
            logger.warning("  pid=%d permission denied (owned by another user?)", pid)
        except OSError as e:
            logger.warning("  pid=%d kill failed: %s", pid, e)

    # Give SIGTERM'd processes a moment, then SIGKILL stragglers
    if killed:
        time.sleep(3)
        for entry in killed:
            pid = entry["pid"]
            if Path(f"/proc/{pid}").exists():
                logger.info("SIGKILL pid=%d (did not exit after SIGTERM)", pid)
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass

    return killed


def archive_old_task_files() -> list[str]:
    """Move .done and .failed task files older than TASK_FILE_AGE_HOURS to archive."""
    archived = []
    now = time.time()
    age_threshold = TASK_FILE_AGE_HOURS * 3600

    if not TASKS_DIR.exists():
        logger.info("TASKS directory does not exist: %s", TASKS_DIR)
        return archived

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    for f in TASKS_DIR.iterdir():
        if not f.is_file():
            continue
        if not (f.name.endswith(".done") or f.name.endswith(".failed")):
            continue

        try:
            file_age = now - f.stat().st_mtime
        except OSError:
            continue

        if file_age < age_threshold:
            logger.info(
                "SKIP %s (age=%.1fh < %dh)",
                f.name,
                file_age / 3600,
                TASK_FILE_AGE_HOURS,
            )
            continue

        dest = ARCHIVE_DIR / f.name
        # Avoid overwriting: append timestamp if collision
        if dest.exists():
            stem = f.stem
            suffix = f.suffix
            ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            dest = ARCHIVE_DIR / f"{stem}_{ts}{suffix}"

        try:
            shutil.move(str(f), str(dest))
            logger.info("ARCHIVE %s -> %s", f.name, dest.name)
            archived.append(f.name)
        except OSError as e:
            logger.warning("Failed to archive %s: %s", f.name, e)

    return archived


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    logger.info("=" * 60)
    logger.info("Resource cleanup started")
    logger.info("=" * 60)

    # Phase 1: Kill orphaned MCP processes
    logger.info("--- Phase 1: Orphaned MCP process cleanup ---")
    killed = kill_orphaned_mcp_processes()

    # Phase 2: Archive old task files
    logger.info("--- Phase 2: Stale task file archival ---")
    archived = archive_old_task_files()

    # Summary
    logger.info("--- Summary ---")
    logger.info("Processes killed: %d", len(killed))
    for entry in killed:
        logger.info(
            "  PID %-7d (age %6.1fm)  %s",
            entry["pid"],
            entry["age_min"],
            entry["cmdline"][:100],
        )
    logger.info("Task files archived: %d", len(archived))
    for name in archived:
        logger.info("  %s", name)
    logger.info("Resource cleanup complete")
    logger.info("")

    # Print summary to stdout (useful for systemd journal)
    print(f"\nResource cleanup complete: {len(killed)} processes killed, {len(archived)} files archived.")


if __name__ == "__main__":
    main()
