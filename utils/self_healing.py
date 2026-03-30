"""NovaCore Self-Healing Runtime — Phase 6A integration.

Provides 4 self-healing layers missing from the original stack:
  Layer 4: Graceful degradation tiers (FULL → REDUCED → MINIMAL → EMERGENCY)
  Layer 5: Memory monitoring (RSS tracking via resource module)
  Layer 6: Error classification (transient/permanent/resource/timeout)
  Layer 7: Dead man's switch (file-based heartbeat token)

Integrates with existing layers 1-3:
  Layer 1: Dead man's switch (nova_kill_switch.heartbeat_alive)
  Layer 2: Watchdog (systemd Restart=always)
  Layer 3: Audit trail (structured logging)

Stdlib only — no pip installs required. Thread-safe.
"""

import json
import os
import resource
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "STATE"

# Dead man's switch
DMS_FILE = STATE_DIR / "dead_man_switch.json"
DMS_STALE_SECONDS = 300  # 5 minutes — watcher should touch every poll cycle

# Memory monitoring
RSS_WARN_MB = int(os.environ.get("NOVA_RSS_WARN_MB", "2048"))
RSS_CRITICAL_MB = int(os.environ.get("NOVA_RSS_CRITICAL_MB", "3500"))
RSS_HISTORY_FILE = STATE_DIR / "rss_history.jsonl"
RSS_HISTORY_MAX_ENTRIES = 1000

# Degradation state
DEGRADATION_FILE = STATE_DIR / "degradation_state.json"

# Error classification
ERROR_HISTORY_FILE = STATE_DIR / "error_history.jsonl"
ERROR_HISTORY_MAX_ENTRIES = 500


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DegradationTier(IntEnum):
    """4-level graceful degradation.

    FULL      — all features active
    REDUCED   — non-essential features disabled (research, planning, proactive)
    MINIMAL   — only core task execution + health checks
    EMERGENCY — health checks only, no task execution
    """

    FULL = 0
    REDUCED = 1
    MINIMAL = 2
    EMERGENCY = 3


class ErrorCategory(IntEnum):
    """Error classification for retry/escalation decisions."""

    TRANSIENT = 0  # network glitch, temporary unavailability — retry
    TIMEOUT = 1  # operation took too long — retry with backoff
    RESOURCE = 2  # OOM, disk full, rate limit — wait, reduce load
    PERMANENT = 3  # bad input, auth failure, missing dependency — do not retry
    UNKNOWN = 4  # unclassifiable — retry once, then escalate


# ---------------------------------------------------------------------------
# Error Classifier
# ---------------------------------------------------------------------------

# Patterns for classification — lowercase matching
_TRANSIENT_PATTERNS = [
    "connection reset",
    "connection refused",
    "temporary failure",
    "service unavailable",
    "502",
    "503",
    "504",
    "etimedout",
    "econnreset",
    "econnrefused",
    "broken pipe",
    "network unreachable",
    "dns resolution",
    "ssl handshake",
    "retry",
    "temporarily",
]

_TIMEOUT_PATTERNS = [
    "timeout",
    "timed out",
    "deadline exceeded",
    "took too long",
    "asyncio.timeouterror",
    "task_timeout",
]

_RESOURCE_PATTERNS = [
    "out of memory",
    "oom",
    "memory",
    "cannot allocate",
    "no space left",
    "disk full",
    "quota exceeded",
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "resource exhausted",
    "memoryerror",
]

_PERMANENT_PATTERNS = [
    "permission denied",
    "not found",
    "404",
    "401",
    "403",
    "authentication",
    "authorization",
    "invalid",
    "malformed",
    "syntax error",
    "import error",
    "module not found",
    "filenotfounderror",
    "keyerror",
    "valueerror",
    "typeerror",
    "attributeerror",
]


@dataclass
class ClassifiedError:
    """Result of error classification."""

    category: ErrorCategory
    original_error: str
    should_retry: bool
    max_retries: int
    backoff_seconds: float
    reason: str


def classify_error(error: str | Exception) -> ClassifiedError:
    """Classify an error and return retry guidance.

    Uses pattern matching against known error signatures.
    """
    text = str(error).lower()

    # Check patterns in priority order (most specific first)
    for pattern in _TIMEOUT_PATTERNS:
        if pattern in text:
            return ClassifiedError(
                category=ErrorCategory.TIMEOUT,
                original_error=str(error),
                should_retry=True,
                max_retries=2,
                backoff_seconds=30.0,
                reason=f"timeout pattern: '{pattern}'",
            )

    for pattern in _RESOURCE_PATTERNS:
        if pattern in text:
            return ClassifiedError(
                category=ErrorCategory.RESOURCE,
                original_error=str(error),
                should_retry=True,
                max_retries=1,
                backoff_seconds=60.0,
                reason=f"resource pattern: '{pattern}'",
            )

    for pattern in _PERMANENT_PATTERNS:
        if pattern in text:
            return ClassifiedError(
                category=ErrorCategory.PERMANENT,
                original_error=str(error),
                should_retry=False,
                max_retries=0,
                backoff_seconds=0.0,
                reason=f"permanent pattern: '{pattern}'",
            )

    for pattern in _TRANSIENT_PATTERNS:
        if pattern in text:
            return ClassifiedError(
                category=ErrorCategory.TRANSIENT,
                original_error=str(error),
                should_retry=True,
                max_retries=3,
                backoff_seconds=5.0,
                reason=f"transient pattern: '{pattern}'",
            )

    return ClassifiedError(
        category=ErrorCategory.UNKNOWN,
        original_error=str(error),
        should_retry=True,
        max_retries=1,
        backoff_seconds=10.0,
        reason="no pattern matched — unknown error",
    )


def record_error(caller: str, error: str | Exception, task_id: str = "") -> ClassifiedError:
    """Classify an error and append to error history ledger."""
    classified = classify_error(error)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "caller": caller,
        "task_id": task_id,
        "category": classified.category.name,
        "should_retry": classified.should_retry,
        "reason": classified.reason,
        "error": classified.original_error[:500],
    }
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(ERROR_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        _prune_jsonl(ERROR_HISTORY_FILE, ERROR_HISTORY_MAX_ENTRIES)
    except Exception:  # noqa: S110
        pass  # Never throw from error recorder
    return classified


def get_error_summary(minutes: int = 60) -> dict:
    """Summarize errors from the last N minutes."""
    cutoff = time.time() - (minutes * 60)
    counts: dict[str, int] = {}
    total = 0
    retry_count = 0
    try:
        if not ERROR_HISTORY_FILE.exists():
            return {"total": 0, "by_category": {}, "retry_eligible": 0, "window_minutes": minutes}
        with open(ERROR_HISTORY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = datetime.fromisoformat(entry["ts"]).timestamp()
                    if ts >= cutoff:
                        cat = entry.get("category", "UNKNOWN")
                        counts[cat] = counts.get(cat, 0) + 1
                        total += 1
                        if entry.get("should_retry"):
                            retry_count += 1
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
    except Exception:  # noqa: S110
        pass
    return {"total": total, "by_category": counts, "retry_eligible": retry_count, "window_minutes": minutes}


# ---------------------------------------------------------------------------
# Memory Monitor
# ---------------------------------------------------------------------------


@dataclass
class MemorySnapshot:
    """Point-in-time memory measurement."""

    rss_mb: float
    rss_peak_mb: float
    timestamp: str
    pid: int
    healthy: bool
    warning: bool
    critical: bool
    detail: str


def get_memory_snapshot() -> MemorySnapshot:
    """Take a memory snapshot using resource module (no external deps)."""
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # ru_maxrss is in KB on Linux
        rss_kb = usage.ru_maxrss
        rss_mb = rss_kb / 1024.0

        # Current RSS from /proc/self/status (more accurate for current usage)
        current_rss_mb = rss_mb
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        current_rss_mb = int(line.split()[1]) / 1024.0
                        break
        except Exception:  # noqa: S110
            pass  # Fall back to ru_maxrss

        warning = current_rss_mb >= RSS_WARN_MB
        critical = current_rss_mb >= RSS_CRITICAL_MB
        healthy = not warning and not critical

        if critical:
            detail = f"CRITICAL: {current_rss_mb:.0f}MB RSS (limit {RSS_CRITICAL_MB}MB)"
        elif warning:
            detail = f"WARNING: {current_rss_mb:.0f}MB RSS (warn at {RSS_WARN_MB}MB)"
        else:
            detail = f"{current_rss_mb:.0f}MB RSS (ok)"

        return MemorySnapshot(
            rss_mb=round(current_rss_mb, 1),
            rss_peak_mb=round(rss_mb, 1),
            timestamp=datetime.now(timezone.utc).isoformat(),
            pid=os.getpid(),
            healthy=healthy,
            warning=warning,
            critical=critical,
            detail=detail,
        )
    except Exception as e:
        return MemorySnapshot(
            rss_mb=0.0,
            rss_peak_mb=0.0,
            timestamp=datetime.now(timezone.utc).isoformat(),
            pid=os.getpid(),
            healthy=True,
            warning=False,
            critical=False,
            detail=f"measurement failed: {e}",
        )


def record_memory_snapshot(snapshot: MemorySnapshot | None = None) -> MemorySnapshot:
    """Record a memory snapshot to the RSS history ledger."""
    if snapshot is None:
        snapshot = get_memory_snapshot()
    entry = {
        "ts": snapshot.timestamp,
        "rss_mb": snapshot.rss_mb,
        "peak_mb": snapshot.rss_peak_mb,
        "pid": snapshot.pid,
        "warning": snapshot.warning,
        "critical": snapshot.critical,
    }
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(RSS_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        _prune_jsonl(RSS_HISTORY_FILE, RSS_HISTORY_MAX_ENTRIES)
    except Exception:  # noqa: S110
        pass
    return snapshot


def get_memory_trend(entries: int = 10) -> list[dict]:
    """Get the last N memory snapshots from the history ledger."""
    results: list[dict] = []
    try:
        if not RSS_HISTORY_FILE.exists():
            return results
        with open(RSS_HISTORY_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-entries:]:
            line = line.strip()
            if line:
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:  # noqa: S110
        pass
    return results


# ---------------------------------------------------------------------------
# Dead Man's Switch
# ---------------------------------------------------------------------------


def touch_dead_man_switch(component: str = "watcher") -> None:
    """Touch the dead man's switch file to signal liveness.

    Should be called every poll cycle by the watcher and every heartbeat.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "component": component,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "epoch": time.time(),
            "pid": os.getpid(),
        }
        DMS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except Exception:  # noqa: S110
        pass  # Never throw from dead man's switch


def check_dead_man_switch() -> dict:
    """Check if the dead man's switch has been touched recently.

    Returns a health check dict compatible with heartbeat.py pattern.
    """
    try:
        if not DMS_FILE.exists():
            return {
                "name": "dead_man_switch",
                "ok": False,
                "detail": "no switch file — watcher may not be touching DMS",
            }
        data = json.loads(DMS_FILE.read_text(encoding="utf-8"))
        epoch = data.get("epoch", 0)
        age_seconds = time.time() - epoch
        component = data.get("component", "unknown")
        pid = data.get("pid", "?")

        if age_seconds > DMS_STALE_SECONDS:
            return {
                "name": "dead_man_switch",
                "ok": False,
                "detail": (
                    f"STALE: last touch {age_seconds:.0f}s ago by {component} "
                    f"(pid {pid}, threshold {DMS_STALE_SECONDS}s)"
                ),
            }
        return {
            "name": "dead_man_switch",
            "ok": True,
            "detail": f"alive: {component} (pid {pid}) touched {age_seconds:.0f}s ago",
        }
    except Exception as e:
        return {"name": "dead_man_switch", "ok": False, "detail": f"check failed: {e}"}


# ---------------------------------------------------------------------------
# Degradation Tier Management
# ---------------------------------------------------------------------------


@dataclass
class DegradationState:
    """Persisted degradation state."""

    tier: DegradationTier = DegradationTier.FULL
    reason: str = ""
    since: str = ""
    transition_history: list[dict] = field(default_factory=list)


_degradation_lock = threading.Lock()


def _load_degradation_state() -> DegradationState:
    """Load degradation state from disk."""
    try:
        if DEGRADATION_FILE.exists():
            data = json.loads(DEGRADATION_FILE.read_text(encoding="utf-8"))
            return DegradationState(
                tier=DegradationTier(data.get("tier", 0)),
                reason=data.get("reason", ""),
                since=data.get("since", ""),
                transition_history=data.get("transition_history", [])[-20:],
            )
    except Exception:  # noqa: S110
        pass
    return DegradationState()


def _save_degradation_state(state: DegradationState) -> None:
    """Persist degradation state to disk."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "tier": int(state.tier),
            "tier_name": state.tier.name,
            "reason": state.reason,
            "since": state.since,
            "transition_history": state.transition_history[-20:],
        }
        DEGRADATION_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except Exception:  # noqa: S110
        pass


def get_degradation_tier() -> DegradationState:
    """Get current degradation tier."""
    with _degradation_lock:
        return _load_degradation_state()


def evaluate_degradation(
    memory: MemorySnapshot | None = None,
    error_summary: dict | None = None,
    dms_ok: bool = True,
) -> DegradationState:
    """Evaluate and potentially change the degradation tier.

    Inputs:
        memory: Current memory snapshot
        error_summary: Error summary from get_error_summary()
        dms_ok: Dead man's switch healthy

    Escalation rules:
        - Memory critical → EMERGENCY
        - Memory warning + high error rate → MINIMAL
        - Memory warning OR high error rate → REDUCED
        - DMS stale → REDUCED (something is stuck)
        - All clear for 10 min → de-escalate one step

    Returns the new state (may be unchanged).
    """
    with _degradation_lock:
        state = _load_degradation_state()
        old_tier = state.tier
        now_iso = datetime.now(timezone.utc).isoformat()

        if memory is None:
            memory = get_memory_snapshot()
        if error_summary is None:
            error_summary = get_error_summary(minutes=15)

        error_rate = error_summary.get("total", 0)
        high_errors = error_rate >= 10  # 10+ errors in 15 minutes

        # Escalation logic (highest severity first)
        new_tier = DegradationTier.FULL

        if memory.critical:
            new_tier = DegradationTier.EMERGENCY
        elif memory.warning and high_errors:
            new_tier = DegradationTier.MINIMAL
        elif memory.warning or high_errors or not dms_ok:
            new_tier = DegradationTier.REDUCED

        # De-escalation: only drop one tier at a time, require stability
        if new_tier < old_tier:
            # Check if current tier was triggered by budget constraints
            # Budget-triggered degradations should not be auto-de-escalated
            if state.reason and "budget exceeded" in state.reason.lower():
                new_tier = old_tier  # Hold at current tier - budget issue must be resolved explicitly
            else:
                # Check if we've been at old_tier for at least 10 minutes
                if state.since:
                    try:
                        since_ts = datetime.fromisoformat(state.since).timestamp()
                        if time.time() - since_ts < 600:  # 10 minutes
                            new_tier = old_tier  # Hold, not enough calm time
                    except (ValueError, TypeError):
                        pass
                # Only drop one level at a time
                if new_tier < old_tier - 1:
                    new_tier = DegradationTier(old_tier - 1)

        if new_tier != old_tier:
            direction = "ESCALATED" if new_tier > old_tier else "DE-ESCALATED"
            reason = _build_degradation_reason(memory, error_summary, dms_ok, direction)
            state.transition_history.append(
                {
                    "from": old_tier.name,
                    "to": new_tier.name,
                    "direction": direction,
                    "reason": reason,
                    "ts": now_iso,
                }
            )
            state.tier = new_tier
            state.reason = reason
            state.since = now_iso
            _save_degradation_state(state)
        elif not state.since:
            # First evaluation — set initial state
            state.since = now_iso
            state.reason = "initial evaluation"
            _save_degradation_state(state)

        return state


def _build_degradation_reason(
    memory: MemorySnapshot,
    error_summary: dict,
    dms_ok: bool,
    direction: str,
) -> str:
    """Build a human-readable reason for tier transition."""
    parts = [direction]
    if memory.critical:
        parts.append(f"memory critical ({memory.rss_mb:.0f}MB)")
    elif memory.warning:
        parts.append(f"memory warning ({memory.rss_mb:.0f}MB)")
    if error_summary.get("total", 0) >= 10:
        parts.append(f"high errors ({error_summary['total']} in {error_summary.get('window_minutes', 15)}min)")
    if not dms_ok:
        parts.append("dead man switch stale")
    if direction == "DE-ESCALATED":
        parts.append("conditions improved")
    return " | ".join(parts)


def set_degradation_tier(tier: DegradationTier, reason: str = "") -> DegradationState:
    """Set degradation tier directly (e.g. from circuit breaker callback).

    Thread-safe.  Records the transition in history.
    """
    with _degradation_lock:
        state = _load_degradation_state()
        old_tier = state.tier
        if tier == old_tier:
            return state
        now_iso = datetime.now(timezone.utc).isoformat()
        direction = "ESCALATED" if tier > old_tier else "DE-ESCALATED"
        state.transition_history.append(
            {
                "from": old_tier.name,
                "to": tier.name,
                "direction": direction,
                "reason": reason,
                "ts": now_iso,
            }
        )
        state.tier = tier
        state.reason = reason
        state.since = now_iso
        _save_degradation_state(state)
        return state


# ---------------------------------------------------------------------------
# Circuit Breaker → Degradation Mapping
# ---------------------------------------------------------------------------

_CRITICAL_BREAKERS = frozenset({"claude_api", "metaapi", "mcp"})


def evaluate_circuit_breakers(open_breakers: list[str]) -> DegradationTier:
    """Suggest degradation tier based on which circuit breakers are open.

    Rules:
        - No breakers open                    → FULL
        - 1 critical breaker (mcp/claude_api) → REDUCED
        - 1 critical breaker (metaapi)        → MINIMAL
        - 2+ critical breakers open           → EMERGENCY
        - Only non-critical breakers open      → FULL
    """
    critical_open = [b for b in open_breakers if b in _CRITICAL_BREAKERS]

    if len(critical_open) >= 2:
        return DegradationTier.EMERGENCY

    if len(critical_open) == 1:
        name = critical_open[0]
        if name == "metaapi":
            return DegradationTier.MINIMAL
        # mcp or claude_api
        return DegradationTier.REDUCED

    return DegradationTier.FULL


def should_allow_feature(feature: str) -> bool:
    """Check if a feature is allowed under current degradation tier.

    Feature categories:
        essential   — always allowed (health checks, heartbeat)
        core        — blocked in EMERGENCY only (task execution)
        standard    — blocked in MINIMAL and EMERGENCY (notifications, memory ops)
        optional    — blocked in REDUCED, MINIMAL, EMERGENCY (research, planning, proactive)
    """
    tier = get_degradation_tier().tier
    feature_lower = feature.lower()

    # Essential features: always allowed
    essential = {"health_check", "heartbeat", "dead_man_switch", "telegram_alert", "kill_switch"}
    if feature_lower in essential:
        return True

    # Core features: blocked only in EMERGENCY
    core = {"task_execution", "watcher", "contract_validation", "retry"}
    if feature_lower in core:
        return tier < DegradationTier.EMERGENCY

    # Standard features: blocked in MINIMAL and above
    standard = {"notification", "memory_maintenance", "drift_detection", "multi_agent"}
    if feature_lower in standard:
        return tier < DegradationTier.MINIMAL

    # Optional features: blocked in REDUCED and above
    optional = {"research", "planning", "proactive_seed", "self_improvement", "skill_optimizer"}
    if feature_lower in optional:
        return tier < DegradationTier.REDUCED

    # Unknown features: treat as standard
    return tier < DegradationTier.MINIMAL


# ---------------------------------------------------------------------------
# Self-Healing Runtime (orchestrator)
# ---------------------------------------------------------------------------


@dataclass
class SelfHealingStatus:
    """Aggregated self-healing status for heartbeat reporting."""

    ok: bool
    degradation_tier: str
    degradation_reason: str
    memory_rss_mb: float
    memory_healthy: bool
    memory_warning: bool
    memory_critical: bool
    dead_man_switch_ok: bool
    errors_last_15m: int
    error_categories: dict
    detail: str
    alerts: list[dict] = field(default_factory=list)


def check_self_healing() -> dict:
    """Run all self-healing checks and return heartbeat-compatible dict.

    This is the main integration point for heartbeat.py.
    """
    try:
        # 1. Memory snapshot
        memory = get_memory_snapshot()
        record_memory_snapshot(memory)

        # 2. Dead man's switch
        dms = check_dead_man_switch()
        dms_ok = dms["ok"]

        # 3. Error summary
        errors = get_error_summary(minutes=15)

        # 4. Evaluate degradation
        deg_state = evaluate_degradation(memory=memory, error_summary=errors, dms_ok=dms_ok)

        # 5. Build alerts
        alerts: list[dict] = []
        if memory.critical:
            alerts.append({"severity": "critical", "title": "Memory critical", "detail": memory.detail})
        elif memory.warning:
            alerts.append({"severity": "warning", "title": "Memory warning", "detail": memory.detail})

        if not dms_ok:
            alerts.append({"severity": "warning", "title": "Dead man switch stale", "detail": dms["detail"]})

        if errors["total"] >= 10:
            alerts.append(
                {
                    "severity": "warning",
                    "title": f"High error rate ({errors['total']} in 15m)",
                    "detail": f"categories: {errors['by_category']}",
                }
            )

        if deg_state.tier >= DegradationTier.REDUCED:
            alerts.append(
                {
                    "severity": "warning" if deg_state.tier < DegradationTier.EMERGENCY else "critical",
                    "title": f"Degradation: {deg_state.tier.name}",
                    "detail": deg_state.reason,
                }
            )

        # 6. Build result
        ok = memory.healthy and dms_ok and errors["total"] < 10 and deg_state.tier == DegradationTier.FULL

        detail_parts = [
            f"tier={deg_state.tier.name}",
            f"rss={memory.rss_mb:.0f}MB",
            f"dms={'ok' if dms_ok else 'STALE'}",
            f"errors_15m={errors['total']}",
        ]
        if alerts:
            detail_parts.append(f"alerts={len(alerts)}")

        return {
            "name": "self_healing",
            "ok": ok,
            "detail": " | ".join(detail_parts),
            "alerts": alerts,
            "degradation_tier": deg_state.tier.name,
            "memory_rss_mb": memory.rss_mb,
        }

    except Exception as e:
        return {"name": "self_healing", "ok": True, "detail": f"check skipped: {e}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prune_jsonl(filepath: Path, max_entries: int) -> None:
    """Keep only the last max_entries lines in a JSONL file."""
    try:
        with open(filepath, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > max_entries * 1.5:  # Only prune when 50% over
            with open(filepath, "w", encoding="utf-8") as f:
                f.writelines(lines[-max_entries:])
    except Exception:  # noqa: S110
        pass
