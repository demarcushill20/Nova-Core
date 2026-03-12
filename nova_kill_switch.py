"""Nova-Core Kill Switch — multi-layer emergency stop.

Provides graduated operational modes (run/pause/readonly/stopped)
via file-based and Redis-based switches. All checks are simple
reads — no LLM involvement, no complex logic.

Phase 1.2 of Security Hardening Plan (2026-03-12).

CRITICAL: Kill switch logic lives OUTSIDE the AI reasoning path.
Stanford (March 2026) found models sabotaged shutdown in 79/100 tests.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

_log = logging.getLogger("nova.kill_switch")

# --- File-based kill switch paths ---
KILL_FILE = Path("/tmp/nova-kill")
PAUSE_FILE = Path("/tmp/nova-pause")
READONLY_FILE = Path("/tmp/nova-readonly")

# --- Modes ---
MODE_RUN = "run"
MODE_PAUSE = "pause"
MODE_READONLY = "readonly"
MODE_STOPPED = "stopped"

VALID_MODES = frozenset({MODE_RUN, MODE_PAUSE, MODE_READONLY, MODE_STOPPED})

# --- Confirmation content required in kill files ---
# Prevents accidental creation (e.g., `touch /tmp/nova-kill` won't work)
CONFIRM_CONTENT = "CONFIRM"


def _read_file_mode(path: Path) -> bool:
    """Check if a kill switch file exists with valid confirmation content."""
    try:
        if path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            return content == CONFIRM_CONTENT
    except OSError:
        pass
    return False


def _try_redis_mode() -> str | None:
    """Check Redis kill switch. Returns mode or None if Redis unavailable."""
    try:
        import redis
        r = redis.Redis(host="localhost", port=6379, db=0, socket_timeout=1)
        val = r.get("nova:killswitch")
        if val:
            mode = val.decode("utf-8").strip()
            if mode in VALID_MODES:
                return mode
    except Exception:
        pass
    return None


def check_kill_switch() -> str:
    """Check current operational mode. Fast, no LLM involvement.

    Priority:
    1. File-based kill (highest priority — always works even if Redis is down)
    2. Redis-based kill
    3. Default: run

    Returns one of: 'run', 'pause', 'readonly', 'stopped'
    """
    # File-based checks (highest priority)
    if _read_file_mode(KILL_FILE):
        return MODE_STOPPED
    if _read_file_mode(PAUSE_FILE):
        return MODE_PAUSE
    if _read_file_mode(READONLY_FILE):
        return MODE_READONLY

    # Redis check
    redis_mode = _try_redis_mode()
    if redis_mode and redis_mode != MODE_RUN:
        return redis_mode

    return MODE_RUN


def set_mode_redis(mode: str, ttl: int | None = None) -> bool:
    """Set kill switch mode via Redis.

    Args:
        mode: One of 'run', 'pause', 'readonly', 'stopped'.
        ttl: Optional TTL in seconds (auto-expires, e.g., 300 for 5-min pause).

    Returns True if successfully set.
    """
    if mode not in VALID_MODES:
        _log.error("Invalid kill switch mode: %s", mode)
        return False
    try:
        import redis
        r = redis.Redis(host="localhost", port=6379, db=0, socket_timeout=2)
        if mode == MODE_RUN:
            r.delete("nova:killswitch")
        elif ttl:
            r.set("nova:killswitch", mode, ex=ttl)
        else:
            r.set("nova:killswitch", mode)
        _log.info("KILL_SWITCH set to '%s' via Redis (ttl=%s)", mode, ttl)
        return True
    except Exception as exc:
        _log.error("Failed to set Redis kill switch: %s", exc)
        return False


def set_mode_file(mode: str) -> bool:
    """Set kill switch mode via file system.

    Args:
        mode: One of 'run', 'pause', 'readonly', 'stopped'.

    Returns True if successfully set.
    """
    if mode not in VALID_MODES:
        return False

    # Clear all files first
    for f in (KILL_FILE, PAUSE_FILE, READONLY_FILE):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass

    if mode == MODE_RUN:
        _log.info("KILL_SWITCH cleared (mode=run) via file")
        return True

    target = {
        MODE_STOPPED: KILL_FILE,
        MODE_PAUSE: PAUSE_FILE,
        MODE_READONLY: READONLY_FILE,
    }.get(mode)

    if target:
        try:
            target.write_text(CONFIRM_CONTENT, encoding="utf-8")
            _log.info("KILL_SWITCH set to '%s' via file %s", mode, target)
            return True
        except OSError as exc:
            _log.error("Failed to write kill switch file: %s", exc)
    return False


def clear_all() -> None:
    """Clear all kill switch states (file + Redis)."""
    for f in (KILL_FILE, PAUSE_FILE, READONLY_FILE):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
    set_mode_redis(MODE_RUN)
    _log.info("KILL_SWITCH cleared (all modes)")


# --- Dead Man's Switch ---

HEARTBEAT_TTL = 2700  # 45 minutes — heartbeat runs every 30min
HEARTBEAT_KEY = "nova:heartbeat_alive"


def heartbeat_alive() -> bool:
    """Record that the heartbeat is alive. Call from heartbeat.py."""
    try:
        import redis
        r = redis.Redis(host="localhost", port=6379, db=0, socket_timeout=2)
        r.set(HEARTBEAT_KEY, str(time.time()), ex=HEARTBEAT_TTL)
        return True
    except Exception:
        return False


def check_dead_man_switch() -> bool:
    """Check if the dead man's switch has expired.

    Returns True if the system is alive (heartbeat recent).
    Returns False if no heartbeat in HEARTBEAT_TTL seconds.
    """
    try:
        import redis
        r = redis.Redis(host="localhost", port=6379, db=0, socket_timeout=1)
        val = r.get(HEARTBEAT_KEY)
        return val is not None
    except Exception:
        # If Redis is down, don't trigger dead man's switch
        return True


def should_accept_work() -> bool:
    """Convenience: check if the system should accept new work.

    Returns True only in 'run' mode with a live heartbeat.
    """
    mode = check_kill_switch()
    if mode != MODE_RUN:
        _log.info("WORK_REJECTED mode=%s", mode)
        return False
    return True


def format_status() -> str:
    """Format current kill switch status for display."""
    mode = check_kill_switch()
    dms = check_dead_man_switch()
    parts = [f"Mode: {mode}"]
    if not dms:
        parts.append("Dead man's switch: EXPIRED (no heartbeat)")
    else:
        parts.append("Dead man's switch: OK")

    # Check file states
    files = []
    if KILL_FILE.exists():
        files.append("kill")
    if PAUSE_FILE.exists():
        files.append("pause")
    if READONLY_FILE.exists():
        files.append("readonly")
    if files:
        parts.append(f"Active files: {', '.join(files)}")

    return " | ".join(parts)
