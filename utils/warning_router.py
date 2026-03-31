"""Unified warning router — collects warnings from all NovaCore subsystems
and queues them for AI-driven investigation.

Sources: heartbeat, circuit breakers, budget enforcer, drift detector,
         error classifier, max plan guard, ops monitor, self-healing.

Consumers: SelfHealInvestigator (agents/self_heal_investigator.py)
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_DIR = Path(os.environ.get("NOVA_STATE_DIR", "STATE"))
QUEUE_FILE = STATE_DIR / "warning_queue.jsonl"
ARCHIVE_FILE = STATE_DIR / "warning_archive.jsonl"
COOLDOWN_FILE = STATE_DIR / "warning_cooldown.json"
MAX_QUEUE_SIZE = 200
MAX_ARCHIVE_SIZE = 2000
DEFAULT_COOLDOWN_SECONDS = 300  # 5 min dedup window per fingerprint


class WarningSeverity(IntEnum):
    INFO = 0
    WARNING = 1
    CRITICAL = 2


class WarningCategory(str):
    """Semantic category tags."""

    HEARTBEAT = "heartbeat"
    CIRCUIT_BREAKER = "circuit_breaker"
    BUDGET = "budget"
    DRIFT = "drift"
    RUNAWAY = "runaway"
    SERVICE = "service"
    MEMORY = "memory"
    RISK = "risk"
    ERROR_SPIKE = "error_spike"
    TOKEN_SPIKE = "token_spike"
    CUSTOM = "custom"


@dataclass
class Warning:
    """A single warning event queued for investigation."""

    timestamp: str
    source: str  # e.g. "heartbeat", "circuit_breaker", "budget_enforcer"
    severity: int  # WarningSeverity value
    category: str  # WarningCategory value
    message: str  # human-readable summary
    context: dict = field(default_factory=dict)  # extra data for investigation
    fingerprint: str = ""  # dedup key (auto-computed if empty)
    investigated: bool = False
    investigation_id: str = ""

    def __post_init__(self):
        if not self.fingerprint:
            self.fingerprint = _compute_fingerprint(self.source, self.category, self.message)


def _compute_fingerprint(source: str, category: str, message: str) -> str:
    """Stable fingerprint for dedup — same source+category+message core."""
    # Strip numbers/timestamps from message for stable grouping
    core = f"{source}:{category}:{message[:120]}"
    return hashlib.md5(core.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Cooldown (dedup)
# ---------------------------------------------------------------------------


def _load_cooldowns() -> dict[str, float]:
    try:
        return json.loads(COOLDOWN_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cooldowns(cd: dict[str, float]) -> None:
    COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_FILE.write_text(json.dumps(cd))


def _is_cooled_down(fingerprint: str, cooldown_secs: float = DEFAULT_COOLDOWN_SECONDS) -> bool:
    """True if this fingerprint was seen recently (skip it)."""
    cd = _load_cooldowns()
    last_seen = cd.get(fingerprint, 0)
    return (time.time() - last_seen) < cooldown_secs


def _mark_seen(fingerprint: str) -> None:
    cd = _load_cooldowns()
    cd[fingerprint] = time.time()
    # Prune entries older than 1 hour
    cutoff = time.time() - 3600
    cd = {k: v for k, v in cd.items() if v > cutoff}
    _save_cooldowns(cd)


# ---------------------------------------------------------------------------
# Queue operations
# ---------------------------------------------------------------------------


def _read_queue() -> list[dict]:
    try:
        lines = QUEUE_FILE.read_text().strip().splitlines()
        return [json.loads(ln) for ln in lines if ln.strip()]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_queue(entries: list[dict]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Keep only most recent MAX_QUEUE_SIZE
    entries = entries[-MAX_QUEUE_SIZE:]
    QUEUE_FILE.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _append_archive(entries: list[dict]) -> None:
    ARCHIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    try:
        lines = ARCHIVE_FILE.read_text().strip().splitlines()
        existing = [json.loads(ln) for ln in lines if ln.strip()]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    existing.extend(entries)
    existing = existing[-MAX_ARCHIVE_SIZE:]
    ARCHIVE_FILE.write_text("\n".join(json.dumps(e) for e in existing) + "\n")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def emit_warning(warning: Warning, cooldown_secs: float = DEFAULT_COOLDOWN_SECONDS) -> bool:
    """Add a warning to the investigation queue.

    Returns True if queued, False if deduplicated (cooldown).
    """
    if _is_cooled_down(warning.fingerprint, cooldown_secs):
        return False

    _mark_seen(warning.fingerprint)
    queue = _read_queue()
    queue.append(asdict(warning))
    _write_queue(queue)
    return True


def emit(
    source: str,
    category: str,
    message: str,
    severity: int = WarningSeverity.WARNING,
    context: dict | None = None,
    cooldown_secs: float = DEFAULT_COOLDOWN_SECONDS,
) -> bool:
    """Convenience shorthand for emit_warning."""
    from datetime import datetime, timezone

    w = Warning(
        timestamp=datetime.now(timezone.utc).isoformat(),
        source=source,
        severity=severity,
        category=category,
        message=message,
        context=context or {},
    )
    return emit_warning(w, cooldown_secs)


def get_pending_warnings() -> list[Warning]:
    """Return all uninvestigated warnings, sorted by severity (highest first)."""
    queue = _read_queue()
    pending = [e for e in queue if not e.get("investigated")]
    pending.sort(key=lambda e: -e.get("severity", 0))
    return [Warning(**e) for e in pending]


def mark_investigated(fingerprint: str, investigation_id: str = "") -> None:
    """Mark a warning as investigated and archive it."""
    queue = _read_queue()
    remaining = []
    archived = []
    for entry in queue:
        if entry.get("fingerprint") == fingerprint:
            entry["investigated"] = True
            entry["investigation_id"] = investigation_id
            archived.append(entry)
        else:
            remaining.append(entry)
    _write_queue(remaining)
    if archived:
        _append_archive(archived)


def get_queue_stats() -> dict[str, Any]:
    """Summary stats for monitoring."""
    queue = _read_queue()
    pending = [e for e in queue if not e.get("investigated")]
    return {
        "total_queued": len(queue),
        "pending": len(pending),
        "by_severity": {
            "critical": sum(1 for e in pending if e.get("severity") == WarningSeverity.CRITICAL),
            "warning": sum(1 for e in pending if e.get("severity") == WarningSeverity.WARNING),
            "info": sum(1 for e in pending if e.get("severity") == WarningSeverity.INFO),
        },
        "by_category": _count_by(pending, "category"),
    }


def _count_by(entries: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in entries:
        val = e.get(key, "unknown")
        counts[val] = counts.get(val, 0) + 1
    return counts


def clear_queue() -> int:
    """Archive and clear all entries. Returns count cleared."""
    queue = _read_queue()
    if queue:
        _append_archive(queue)
    _write_queue([])
    return len(queue)


# ---------------------------------------------------------------------------
# Source-specific collectors — call from existing subsystems
# ---------------------------------------------------------------------------


def collect_from_heartbeat(checks: list[dict]) -> int:
    """Emit warnings for failed heartbeat checks."""
    count = 0
    for check in checks:
        if not check.get("ok", True):
            severity = WarningSeverity.CRITICAL if "service" in check.get("name", "") else WarningSeverity.WARNING
            queued = emit(
                source="heartbeat",
                category=WarningCategory.HEARTBEAT,
                message=f"Health check failed: {check.get('name', '?')} — {check.get('detail', 'no detail')}",
                severity=severity,
                context=check,
            )
            if queued:
                count += 1
    return count


def collect_from_circuit_breakers(breaker_states: dict[str, str]) -> int:
    """Emit warnings for open circuit breakers. breaker_states: {name: state_str}"""
    count = 0
    for name, state in breaker_states.items():
        if state == "OPEN":
            queued = emit(
                source="circuit_breaker",
                category=WarningCategory.CIRCUIT_BREAKER,
                message=f"Circuit breaker OPEN: {name}",
                severity=WarningSeverity.CRITICAL,
                context={"breaker_name": name, "state": state},
            )
            if queued:
                count += 1
    return count


def collect_from_budget(alerts: list[str]) -> int:
    """Emit warnings from budget enforcer alerts."""
    count = 0
    for alert_msg in alerts:
        severity = WarningSeverity.CRITICAL if "90%" in alert_msg else WarningSeverity.WARNING
        category = WarningCategory.TOKEN_SPIKE if "token" in alert_msg.lower() else WarningCategory.BUDGET
        queued = emit(
            source="budget_enforcer",
            category=category,
            message=alert_msg,
            severity=severity,
        )
        if queued:
            count += 1
    return count


def collect_from_drift(drift_report: dict) -> int:
    """Emit warnings from drift detector signals."""
    if not drift_report.get("drift_detected"):
        return 0
    count = 0
    for signal in drift_report.get("signals", []):
        severity = WarningSeverity.CRITICAL if signal.get("severity") == "critical" else WarningSeverity.WARNING
        queued = emit(
            source="drift_detector",
            category=WarningCategory.DRIFT,
            message=signal.get("message", "Drift detected"),
            severity=severity,
            context=signal,
        )
        if queued:
            count += 1
    return count


def collect_from_runaway(detection: dict) -> int:
    """Emit warnings from runaway loop detection."""
    if not detection.get("detected"):
        return 0
    severity = WarningSeverity.CRITICAL if detection.get("severity") == "critical" else WarningSeverity.WARNING
    reasons = detection.get("reasons", [])
    queued = emit(
        source="max_plan_guard",
        category=WarningCategory.RUNAWAY,
        message=f"Runaway detected: {'; '.join(reasons[:3])}",
        severity=severity,
        context=detection,
    )
    return 1 if queued else 0


def collect_from_error_summary(summary: dict) -> int:
    """Emit warning if error rate is spiking."""
    total = summary.get("total", 0)
    if total < 5:
        return 0
    retry_eligible = summary.get("retry_eligible", 0)
    by_cat = summary.get("by_category", {})
    permanent = by_cat.get("PERMANENT", 0) + by_cat.get("3", 0)

    count = 0
    if total >= 10:
        queued = emit(
            source="error_classifier",
            category=WarningCategory.ERROR_SPIKE,
            message=f"Error spike: {total} errors in {summary.get('window_minutes', 60)}m ({permanent} permanent, {retry_eligible} retriable)",
            severity=WarningSeverity.CRITICAL if permanent >= 3 else WarningSeverity.WARNING,
            context=summary,
        )
        if queued:
            count += 1
    return count


def collect_from_memory(snapshot: dict) -> int:
    """Emit warning if memory usage is concerning."""
    if snapshot.get("critical"):
        return (
            1
            if emit(
                source="memory_monitor",
                category=WarningCategory.MEMORY,
                message=f"CRITICAL memory: {snapshot.get('rss_mb', '?')}MB RSS",
                severity=WarningSeverity.CRITICAL,
                context=snapshot,
            )
            else 0
        )
    elif snapshot.get("warning"):
        return (
            1
            if emit(
                source="memory_monitor",
                category=WarningCategory.MEMORY,
                message=f"High memory: {snapshot.get('rss_mb', '?')}MB RSS",
                severity=WarningSeverity.WARNING,
                context=snapshot,
            )
            else 0
        )
    return 0
