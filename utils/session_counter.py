"""Daily session counter — hard cap on Claude subprocess spawns per UTC day.

Prevents runaway retry loops from burning through the token budget.
State is persisted to STATE/daily_session_count.json and auto-rolls at midnight UTC.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).resolve().parent.parent / "STATE"
STATE_FILE = STATE_DIR / "daily_session_count.json"

MAX_DAILY_SESSIONS = int(os.environ.get("NOVA_MAX_DAILY_SESSIONS", "40"))

_lock = threading.Lock()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    """Load current state, rolling to a new day if needed."""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {"date": _today_utc(), "count": 0}

    # Roll to new day if date has changed
    if data.get("date") != _today_utc():
        data = {"date": _today_utc(), "count": 0}

    return data


def _save(data: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:
        logger.warning("session_counter: failed to persist state: %s", exc)


def increment() -> int:
    """Atomically increment the daily session count. Returns the new count."""
    with _lock:
        data = _load()
        data["count"] = data.get("count", 0) + 1
        _save(data)
        return data["count"]


def can_dispatch() -> tuple[bool, str]:
    """Check if we're under the daily session cap.

    Returns (True, "OK") if allowed, or (False, reason) if cap reached.
    """
    with _lock:
        data = _load()
        count = data.get("count", 0)
        if count >= MAX_DAILY_SESSIONS:
            return False, f"Daily session cap reached: {count}/{MAX_DAILY_SESSIONS}"
        return True, "OK"


def get_count() -> int:
    """Read current count without incrementing."""
    with _lock:
        data = _load()
        return data.get("count", 0)
