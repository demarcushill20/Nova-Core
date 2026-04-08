"""Daily session counter — hard cap on Claude subprocess spawns per UTC day.

Prevents runaway retry loops from burning through the token budget.
State is persisted to STATE/daily_session_count.json and auto-rolls at midnight UTC.

Budget classification (Phase 7B):
  - production: shift blocks dispatched by the scheduler (reserved pool)
  - research:   long-running analysis / web research tasks
  - test:       pytest / validation / code-review tasks
  - adhoc:      operator-initiated or unclassified tasks
  - carryover:  tasks scheduled on a prior UTC day dispatched today

Reserved-pool policy:
  Non-production tasks share (MAX_DAILY_SESSIONS - RESERVED_PRODUCTION) slots.
  Production tasks can use ANY slot up to MAX_DAILY_SESSIONS.
  Carryover tasks are further capped at MAX_CARRYOVER from the shared pool.
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
RESERVED_PRODUCTION = int(os.environ.get("NOVA_RESERVED_PRODUCTION", "15"))
MAX_CARRYOVER = int(os.environ.get("NOVA_MAX_CARRYOVER", "5"))

# Warning thresholds as fractions of MAX_DAILY_SESSIONS
WARNING_THRESHOLDS = [
    (0.70, "INFO"),
    (0.85, "WARNING"),
    (0.95, "CRITICAL"),
]

VALID_CATEGORIES = ("production", "research", "test", "adhoc", "carryover")

_lock = threading.Lock()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _empty_categories() -> dict:
    return {cat: 0 for cat in VALID_CATEGORIES}


def _load() -> dict:
    """Load current state, rolling to a new day if needed."""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {"date": _today_utc(), "count": 0, "categories": _empty_categories(), "warnings_emitted": []}

    # Roll to new day if date has changed
    if data.get("date") != _today_utc():
        data = {"date": _today_utc(), "count": 0, "categories": _empty_categories(), "warnings_emitted": []}

    # Backfill categories/warnings for state files created before Phase 7B
    if "categories" not in data:
        data["categories"] = _empty_categories()
    if "warnings_emitted" not in data:
        data["warnings_emitted"] = []

    return data


def _save(data: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("session_counter: failed to persist state: %s", exc)


def _check_warnings(data: dict) -> None:
    """Emit log warnings at threshold levels (once per level per day)."""
    count = data.get("count", 0)
    emitted = set(data.get("warnings_emitted", []))
    for threshold, level in WARNING_THRESHOLDS:
        threshold_count = int(MAX_DAILY_SESSIONS * threshold)
        label = f"{int(threshold * 100)}%"
        if count >= threshold_count and label not in emitted:
            cats = data.get("categories", {})
            breakdown = ", ".join(f"{k}={v}" for k, v in cats.items() if v > 0)
            msg = (
                f"SESSION BUDGET {label}: {count}/{MAX_DAILY_SESSIONS} sessions used (breakdown: {breakdown or 'none'})"
            )
            if level == "CRITICAL":
                logger.critical(msg)
            elif level == "WARNING":
                logger.warning(msg)
            else:
                logger.info(msg)
            emitted.add(label)
    data["warnings_emitted"] = sorted(emitted)


def _non_production_count(categories: dict) -> int:
    """Total sessions used by non-production categories."""
    return sum(v for k, v in categories.items() if k != "production")


def increment(category: str = "adhoc") -> int:
    """Atomically increment the daily session count. Returns the new count.

    Args:
        category: Budget category — one of VALID_CATEGORIES.
    """
    if category not in VALID_CATEGORIES:
        logger.warning("session_counter: unknown category %r, defaulting to 'adhoc'", category)
        category = "adhoc"

    with _lock:
        data = _load()
        data["count"] = data.get("count", 0) + 1
        cats = data.get("categories", _empty_categories())
        cats[category] = cats.get(category, 0) + 1
        data["categories"] = cats
        _check_warnings(data)
        _save(data)
        return data["count"]


def can_dispatch(category: str = "adhoc") -> tuple[bool, str]:
    """Check if a task of the given category can be dispatched.

    Budget rules:
      - Hard cap: total count < MAX_DAILY_SESSIONS (blocks everything)
      - Production: allowed if total < MAX_DAILY_SESSIONS (full pool)
      - Non-production: allowed if non_prod_count < (MAX - RESERVED_PRODUCTION)
      - Carryover: additionally capped at MAX_CARRYOVER

    Returns (True, "OK") if allowed, or (False, reason) if cap reached.
    """
    if category not in VALID_CATEGORIES:
        category = "adhoc"

    with _lock:
        data = _load()
        count = data.get("count", 0)
        cats = data.get("categories", _empty_categories())
        non_prod = _non_production_count(cats)
        shared_pool = MAX_DAILY_SESSIONS - RESERVED_PRODUCTION

        # Hard cap — nothing gets through
        if count >= MAX_DAILY_SESSIONS:
            return False, (
                f"Daily session hard cap reached: {count}/{MAX_DAILY_SESSIONS} "
                f"(production={cats.get('production', 0)}, non-prod={non_prod})"
            )

        # Production: full pool access
        if category == "production":
            return True, "OK"

        # Carryover: own sub-cap + shared pool check
        if category == "carryover":
            carryover_used = cats.get("carryover", 0)
            if carryover_used >= MAX_CARRYOVER:
                return False, (
                    f"Carryover cap reached: {carryover_used}/{MAX_CARRYOVER} (total={count}/{MAX_DAILY_SESSIONS})"
                )
            if non_prod >= shared_pool:
                return False, (
                    f"Non-production pool exhausted: {non_prod}/{shared_pool} "
                    f"(carryover={carryover_used}/{MAX_CARRYOVER}, total={count}/{MAX_DAILY_SESSIONS})"
                )
            return True, "OK"

        # Research / test / adhoc: shared pool
        if non_prod >= shared_pool:
            return False, (
                f"Non-production pool exhausted: {non_prod}/{shared_pool} "
                f"({category} blocked, production reserved={RESERVED_PRODUCTION}, "
                f"total={count}/{MAX_DAILY_SESSIONS})"
            )

        return True, "OK"


def get_count() -> int:
    """Read current count without incrementing."""
    with _lock:
        data = _load()
        return data.get("count", 0)


def get_status() -> dict:
    """Return full budget status for monitoring/operator display."""
    with _lock:
        data = _load()
        count = data.get("count", 0)
        cats = data.get("categories", _empty_categories())
        non_prod = _non_production_count(cats)
        shared_pool = MAX_DAILY_SESSIONS - RESERVED_PRODUCTION
        return {
            "date": data.get("date", _today_utc()),
            "total": count,
            "max": MAX_DAILY_SESSIONS,
            "pct_used": round(count / MAX_DAILY_SESSIONS * 100, 1) if MAX_DAILY_SESSIONS else 0,
            "categories": dict(cats),
            "production_remaining": MAX_DAILY_SESSIONS - count,
            "shared_pool_remaining": max(0, shared_pool - non_prod),
            "carryover_remaining": max(0, MAX_CARRYOVER - cats.get("carryover", 0)),
            "warnings_emitted": data.get("warnings_emitted", []),
        }
