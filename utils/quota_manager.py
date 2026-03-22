#!/usr/bin/env python3
"""Quota-aware scheduling for NovaCore autonomous operations.

Prevents afternoon shift failures by monitoring token usage and
dynamically adjusting task acceptance based on quota pressure.

The system operates in three tiers:
  1. Monitoring — reads STATE/quota_status.json for current usage
  2. Decision   — determines schedule mode (conservative/moderate/aggressive)
  3. Enforcement — filters tasks, defers non-critical work, emergency brake
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

# Paths
_BASE_DIR = Path(__file__).resolve().parent.parent
QUOTA_FILE = _BASE_DIR / "STATE" / "quota_status.json"
CT_TIMEZONE = timezone(timedelta(hours=-6))  # Central Time


class QuotaStatus(NamedTuple):
    """Current quota usage status."""

    tokens_used: int
    tokens_remaining: int
    reset_time: datetime
    usage_rate: float  # tokens per hour
    projected_exhaustion: datetime | None


class ScheduleMode(NamedTuple):
    """Schedule configuration for different quota states."""

    name: str
    max_tasks_per_scan: int  # max tasks to enqueue per scan cycle
    block_complexity: str  # 'full', 'reduced', 'minimal'
    defer_non_critical: bool


# Predefined schedule modes based on quota pressure
SCHEDULE_MODES: dict[str, ScheduleMode] = {
    "conservative": ScheduleMode("conservative", 3, "full", False),
    "moderate": ScheduleMode("moderate", 2, "reduced", False),
    "aggressive": ScheduleMode("aggressive", 1, "minimal", True),
}


def get_quota_status() -> QuotaStatus | None:
    """Read current quota status from tracking file.

    Returns None if the file doesn't exist or is malformed, which
    signals the caller to proceed without quota constraints.
    """
    if not QUOTA_FILE.exists():
        return None

    try:
        data = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
        return QuotaStatus(
            tokens_used=data["tokens_used"],
            tokens_remaining=data["tokens_remaining"],
            reset_time=datetime.fromisoformat(data["reset_time"]),
            usage_rate=data.get("usage_rate", 0.0),
            projected_exhaustion=(
                datetime.fromisoformat(data["projected_exhaustion"]) if data.get("projected_exhaustion") else None
            ),
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.warning("Failed to parse quota file %s: %s", QUOTA_FILE, exc)
        return None


def determine_schedule_mode(quota: QuotaStatus | None = None) -> str:
    """Determine appropriate schedule mode based on quota status.

    Returns one of: 'conservative', 'moderate', 'aggressive'.
    """
    if quota is None:
        quota = get_quota_status()
    if quota is None:
        return "conservative"  # No data → play it safe

    now = datetime.now(timezone.utc)
    hours_until_reset = (quota.reset_time - now).total_seconds() / 3600

    if hours_until_reset <= 0:
        return "conservative"  # Reset imminent

    total_tokens = quota.tokens_used + quota.tokens_remaining
    if total_tokens == 0:
        return "conservative"

    utilization = quota.tokens_used / total_tokens
    time_utilization = (24 - hours_until_reset) / 24

    # Pressure ratio: >1 means using tokens faster than time is passing
    pressure_ratio = utilization / max(time_utilization, 0.1)

    if pressure_ratio > 1.5:
        return "aggressive"
    elif pressure_ratio > 1.0:
        return "moderate"
    return "conservative"


def estimate_task_tokens(task_text: str) -> int:
    """Estimate token cost of a task from its content (first 2KB)."""
    text = task_text[:2048].lower()

    # High complexity indicators
    if any(
        kw in text
        for kw in [
            "implement",
            "research",
            "analyze",
            "architecture",
            "comprehensive",
            "deep research",
        ]
    ):
        return 10000

    # Medium
    if any(kw in text for kw in ["review", "update", "fix", "test", "validate"]):
        return 5000

    return 2000  # Minimal


def filter_tasks_for_quota(
    task_files: list[Path],
) -> tuple[list[Path], list[Path]]:
    """Filter tasks based on current quota pressure.

    Returns (accepted, deferred) task lists.
    """
    quota = get_quota_status()
    mode_name = determine_schedule_mode(quota)
    mode = SCHEDULE_MODES[mode_name]

    if not mode.defer_non_critical:
        # Conservative/moderate — accept up to max_tasks_per_scan
        return (
            task_files[: mode.max_tasks_per_scan],
            task_files[mode.max_tasks_per_scan :],
        )

    # Aggressive mode — only accept high-priority / critical tasks
    critical: list[Path] = []
    deferred_list: list[Path] = []

    for task_file in task_files:
        try:
            head = task_file.read_text(encoding="utf-8")[:2048]
        except (OSError, UnicodeDecodeError):
            deferred_list.append(task_file)
            continue

        if "priority: high" in head or "CRITICAL" in head:
            critical.append(task_file)
        else:
            deferred_list.append(task_file)

    # Even critical tasks capped at max_tasks_per_scan
    deferred_list.extend(critical[mode.max_tasks_per_scan :])
    return critical[: mode.max_tasks_per_scan], deferred_list


def check_emergency_brake() -> bool:
    """Return True if we should stop dispatching due to quota exhaustion."""
    quota = get_quota_status()
    if quota is None:
        return False

    # Hard stop if < 5k tokens remain
    if quota.tokens_remaining < 5000:
        logger.warning("Quota emergency brake: only %d tokens remaining", quota.tokens_remaining)
        return True

    # Hard stop if projected exhaustion < 1 hour
    if quota.projected_exhaustion:
        now = datetime.now(timezone.utc)
        hours_left = (quota.projected_exhaustion - now).total_seconds() / 3600
        if hours_left < 1.0:
            logger.warning(
                "Quota emergency brake: projected exhaustion in %.1f hours",
                hours_left,
            )
            return True

    return False


def update_quota_tracking(tokens_used: int) -> None:
    """Update quota tracking with new usage data."""
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    # Conservative estimate: 200k daily limit for Claude Max
    estimated_limit = 200_000
    tokens_remaining = max(0, estimated_limit - tokens_used)

    hours_elapsed = now.hour + (now.minute / 60.0)
    usage_rate = tokens_used / max(hours_elapsed, 1.0)

    projected_exhaustion = None
    if usage_rate > 0 and tokens_remaining > 0:
        hours_to_exhaustion = tokens_remaining / usage_rate
        projected_exhaustion = now + timedelta(hours=hours_to_exhaustion)

    quota_data = {
        "tokens_used": tokens_used,
        "tokens_remaining": tokens_remaining,
        "reset_time": tomorrow.isoformat(),
        "usage_rate": usage_rate,
        "projected_exhaustion": (projected_exhaustion.isoformat() if projected_exhaustion else None),
        "last_updated": now.isoformat(),
    }

    QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUOTA_FILE.write_text(json.dumps(quota_data, indent=2), encoding="utf-8")
