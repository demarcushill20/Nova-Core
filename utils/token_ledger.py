"""Token consumption ledger — aggregation layer over STATE/max_plan_ledger.jsonl.

Provides rolling-window consumption metrics, per-caller breakdowns, burn-rate
projection, and weekly exhaustion estimation.  Designed as a lightweight
read/write overlay on the existing JSONL ledger used by max_plan_guard.

Thread-safe.  Stdlib-only.  All timestamps are UTC.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE = Path("/home/nova/nova-core")
LEDGER_FILE = BASE / "STATE" / "max_plan_ledger.jsonl"

# ---------------------------------------------------------------------------
# Known callers (for breakdown reports — unknown callers still recorded)
# ---------------------------------------------------------------------------
KNOWN_CALLERS = frozenset(
    {
        "heartbeat_agent",
        "watcher",
        "planning_cycle",
        "research_cycle",
        "reflexion",
    }
)

# Rolling windows used in the full consumption report.
REPORT_WINDOWS_HOURS = [1, 6, 24, 168]  # 1h, 6h, 24h, 7d

# Default weekly reset: Sunday 00:00 UTC (weekday 6 in Python's Monday=0 scheme).
DEFAULT_RESET_WEEKDAY = 6  # Sunday


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string to a timezone-aware datetime (UTC)."""
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    """Read all valid JSON lines from the ledger file.

    Silently skips blank lines and malformed JSON.  Returns an empty list
    if the file is missing or empty.
    """
    entries: list[dict[str, Any]] = []
    if not path.exists():
        return entries
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


def _filter_window(
    entries: list[dict[str, Any]],
    now: datetime,
    window_hours: float,
) -> list[dict[str, Any]]:
    """Return entries whose timestamp falls within *now - window_hours*."""
    cutoff = now - timedelta(hours=window_hours)
    result: list[dict[str, Any]] = []
    for entry in entries:
        ts = _parse_ts(entry.get("timestamp", ""))
        if ts is not None and ts >= cutoff:
            result.append(entry)
    return result


def _sum_tokens(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate token counts and invocation count from a list of entries."""
    input_tokens = 0
    output_tokens = 0
    count = 0
    for entry in entries:
        input_tokens += int(entry.get("input_tokens", 0) or 0)
        output_tokens += int(entry.get("output_tokens", 0) or 0)
        count += 1
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "count": count,
    }


# ---------------------------------------------------------------------------
# Lock — module-level so all public functions are thread-safe.
# ---------------------------------------------------------------------------
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_invocation(
    caller: str,
    input_tokens: int,
    output_tokens: int,
    model: str,
    duration_secs: float,
    task_id: str = "",
) -> dict[str, Any]:
    """Append a new invocation record to the ledger and return it.

    The record is a superset of what max_plan_guard writes, adding the
    ``input_tokens`` and ``output_tokens`` fields.
    """
    now = datetime.now(timezone.utc)
    record: dict[str, Any] = {
        "timestamp": now.isoformat(),
        "caller": caller,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "model": model,
        "duration_secs": float(duration_secs),
        "task_id": task_id,
    }
    with _lock:
        LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    return record


def get_rolling_consumption(window_hours: float) -> dict[str, Any]:
    """Return aggregated token consumption for the given rolling window.

    Returns::

        {
            "window_hours": <float>,
            "input_tokens": <int>,
            "output_tokens": <int>,
            "total_tokens": <int>,
            "count": <int>,
        }
    """
    now = datetime.now(timezone.utc)
    with _lock:
        entries = _read_ledger(LEDGER_FILE)
    windowed = _filter_window(entries, now, window_hours)
    result = _sum_tokens(windowed)
    result["window_hours"] = window_hours
    return result


def get_caller_breakdown(window_hours: float) -> dict[str, dict[str, Any]]:
    """Return per-caller token breakdown for the given window.

    Returns a dict keyed by caller name.  Each value has the same shape
    as :func:`get_rolling_consumption` (minus ``window_hours``).
    """
    now = datetime.now(timezone.utc)
    with _lock:
        entries = _read_ledger(LEDGER_FILE)
    windowed = _filter_window(entries, now, window_hours)

    buckets: dict[str, list[dict[str, Any]]] = {}
    for entry in windowed:
        caller = entry.get("caller", "unknown")
        buckets.setdefault(caller, []).append(entry)

    breakdown: dict[str, dict[str, Any]] = {}
    for caller, caller_entries in sorted(buckets.items()):
        breakdown[caller] = _sum_tokens(caller_entries)
    return breakdown


def get_burn_rate() -> dict[str, Any]:
    """Return token burn rate averaged over the last 6 hours.

    Returns::

        {
            "input_tokens_per_hour": <float>,
            "output_tokens_per_hour": <float>,
            "total_tokens_per_hour": <float>,
            "invocations_per_hour": <float>,
            "window_hours": 6,
        }
    """
    window = 6.0
    consumption = get_rolling_consumption(window)
    count = consumption["count"]
    if count == 0:
        return {
            "input_tokens_per_hour": 0.0,
            "output_tokens_per_hour": 0.0,
            "total_tokens_per_hour": 0.0,
            "invocations_per_hour": 0.0,
            "window_hours": window,
        }
    return {
        "input_tokens_per_hour": consumption["input_tokens"] / window,
        "output_tokens_per_hour": consumption["output_tokens"] / window,
        "total_tokens_per_hour": consumption["total_tokens"] / window,
        "invocations_per_hour": count / window,
        "window_hours": window,
    }


def _next_weekly_reset(
    now: datetime,
    reset_weekday: int = DEFAULT_RESET_WEEKDAY,
) -> datetime:
    """Return the next weekly reset boundary after *now*."""
    days_ahead = (reset_weekday - now.weekday()) % 7
    if days_ahead == 0 and (now.hour, now.minute, now.second) != (0, 0, 0):
        days_ahead = 7
    reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    return reset


def project_weekly_exhaustion(
    weekly_cap_tokens: int,
    reset_weekday: int = DEFAULT_RESET_WEEKDAY,
) -> datetime | None:
    """Project when the weekly token cap will be exhausted.

    Uses the 6h burn rate to extrapolate forward.  Returns the projected
    exhaustion datetime (UTC) or ``None`` if the projection shows the cap
    will *not* be hit before the next weekly reset.
    """
    now = datetime.now(timezone.utc)
    burn = get_burn_rate()
    rate = burn["total_tokens_per_hour"]
    if rate <= 0:
        return None  # no burn → no exhaustion

    # Tokens consumed since last reset
    last_reset = _next_weekly_reset(now, reset_weekday) - timedelta(days=7)
    if last_reset > now:
        # Edge case: we're exactly at a reset boundary.
        last_reset -= timedelta(days=7)

    with _lock:
        entries = _read_ledger(LEDGER_FILE)

    consumed = 0
    for entry in entries:
        ts = _parse_ts(entry.get("timestamp", ""))
        if ts is not None and ts >= last_reset:
            consumed += int(entry.get("input_tokens", 0) or 0)
            consumed += int(entry.get("output_tokens", 0) or 0)

    remaining = weekly_cap_tokens - consumed
    if remaining <= 0:
        return now  # already exhausted

    hours_left = remaining / rate
    exhaustion = now + timedelta(hours=hours_left)

    next_reset = _next_weekly_reset(now, reset_weekday)
    if exhaustion >= next_reset:
        return None  # will reset before exhaustion

    return exhaustion


def get_consumption_report(
    weekly_cap_tokens: int | None = None,
    reset_weekday: int = DEFAULT_RESET_WEEKDAY,
) -> dict[str, Any]:
    """Build a full consumption report.

    Includes rolling windows (1h, 6h, 24h, 7d), per-caller breakdown
    for each window, burn rate, and optional weekly exhaustion projection.
    """
    windows: dict[str, Any] = {}
    for wh in REPORT_WINDOWS_HOURS:
        key = f"{wh}h" if wh < 168 else "7d"
        windows[key] = {
            "consumption": get_rolling_consumption(wh),
            "caller_breakdown": get_caller_breakdown(wh),
        }

    burn = get_burn_rate()

    exhaustion: str | None = None
    if weekly_cap_tokens is not None:
        dt = project_weekly_exhaustion(weekly_cap_tokens, reset_weekday)
        if dt is not None:
            exhaustion = dt.isoformat()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "windows": windows,
        "burn_rate": burn,
        "weekly_exhaustion_projection": exhaustion,
        "weekly_cap_tokens": weekly_cap_tokens,
    }
