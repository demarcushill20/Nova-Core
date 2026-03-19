"""Max-plan usage monitoring, burn-rate analysis, and circuit-breaker protection.

Tracks all Claude CLI invocations initiated by NovaCore, computes rolling
burn-rate metrics, detects runaway loops, and activates graduated protection
modes to prevent plan exhaustion.

Design:
  - Append-only JSONL ledger in STATE/max_plan_ledger.jsonl
  - Auth-mode confirmation via ``claude auth status`` (cached, infrequent)
  - Rolling burn-rate windows: 15m / 60m / 6h
  - Runaway loop detection: same-caller, same-task, failure-retry
  - Protection modes: normal → caution → protection → critical_lockdown
  - Operator-readable status artifact in STATE/max_plan_status.json

Stdlib-only in production paths — no pip installs required.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE = Path("/home/nova/nova-core")
STATE_DIR = BASE / "STATE"
LEDGER_FILE = STATE_DIR / "max_plan_ledger.jsonl"
STATUS_FILE = STATE_DIR / "max_plan_status.json"
AUTH_CACHE_FILE = STATE_DIR / "max_plan_auth_cache.json"
PROTECTION_STATE_FILE = STATE_DIR / "max_plan_protection.json"


# ---------------------------------------------------------------------------
# Configuration — all thresholds in one place
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GuardConfig:
    """All configurable thresholds for the Max-plan guard."""

    # Burn-rate multiplier thresholds (vs 24h baseline)
    caution_burn_multiplier: float = 2.0
    protection_burn_multiplier: float = 4.0
    critical_burn_multiplier: float = 8.0

    # Absolute call-count thresholds (per window)
    caution_calls_60m: int = 15
    protection_calls_60m: int = 25
    critical_calls_60m: int = 40
    caution_calls_15m: int = 8
    protection_calls_15m: int = 15

    # Runaway detection
    same_caller_threshold_60m: int = 10
    same_task_threshold_60m: int = 5
    failure_retry_threshold_60m: int = 5

    # Auth check cache TTL (seconds)
    auth_cache_ttl: int = 3600  # 1 hour

    # Ledger rotation: keep last N entries in memory
    ledger_memory_limit: int = 5000

    # Protection mode auto-recovery cooldown (seconds)
    recovery_cooldown: int = 1800  # 30 min of calm before stepping down


# Singleton config (override via set_config for testing)
_config = GuardConfig()


def get_config() -> GuardConfig:
    return _config


def set_config(cfg: GuardConfig) -> None:
    global _config
    _config = cfg


# ---------------------------------------------------------------------------
# Protection modes
# ---------------------------------------------------------------------------
class ProtectionMode(IntEnum):
    NORMAL = 0
    CAUTION = 1
    PROTECTION = 2
    CRITICAL_LOCKDOWN = 3


_PROTECTION_DESCRIPTIONS = {
    ProtectionMode.NORMAL: "All automation enabled",
    ProtectionMode.CAUTION: "Elevated usage — monitoring closely",
    ProtectionMode.PROTECTION: "Non-essential automation suppressed",
    ProtectionMode.CRITICAL_LOCKDOWN: "Only health checks and operator tasks allowed",
}


# ---------------------------------------------------------------------------
# Auth mode detection
# ---------------------------------------------------------------------------
@dataclass
class AuthStatus:
    auth_mode: str  # "max_plan_login", "api_key_mode", "unknown"
    logged_in: bool
    subscription_type: str
    email: str
    api_key_present: bool
    raw: dict[str, Any] = field(default_factory=dict)
    checked_at: str = ""
    error: str = ""


def check_auth_mode(force: bool = False) -> AuthStatus:
    """Detect auth mode via ``claude auth status`` (cached)."""
    cfg = get_config()
    now = time.time()

    # Try cache first
    if not force and AUTH_CACHE_FILE.exists():
        try:
            cached = json.loads(AUTH_CACHE_FILE.read_text())
            if now - cached.get("_ts", 0) < cfg.auth_cache_ttl:
                cached.pop("_ts", None)
                return AuthStatus(**cached)
        except (json.JSONDecodeError, OSError, TypeError):
            pass

    status = _run_auth_check()

    # Cache result
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        cache_data = asdict(status)
        cache_data["_ts"] = now
        AUTH_CACHE_FILE.write_text(json.dumps(cache_data), encoding="utf-8")
    except OSError:
        pass

    return status


def _run_auth_check() -> AuthStatus:
    """Actually invoke ``claude auth status`` and parse output."""
    api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
    claude_bin = os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude")

    try:
        result = subprocess.run(
            [claude_bin, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        raw = json.loads(result.stdout.strip())
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, OSError) as e:
        return AuthStatus(
            auth_mode="unknown",
            logged_in=False,
            subscription_type="unknown",
            email="",
            api_key_present=api_key_present,
            checked_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )

    logged_in = raw.get("loggedIn", False)
    sub_type = raw.get("subscriptionType", "unknown")
    auth_method = raw.get("authMethod", "unknown")

    if sub_type == "max" and auth_method == "claude.ai" and logged_in:
        mode = "max_plan_login"
    elif api_key_present:
        mode = "api_key_mode"
    else:
        mode = "unknown"

    return AuthStatus(
        auth_mode=mode,
        logged_in=logged_in,
        subscription_type=sub_type,
        email=raw.get("email", ""),
        api_key_present=api_key_present,
        raw=raw,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Invocation ledger
# ---------------------------------------------------------------------------
@dataclass
class InvocationRecord:
    timestamp: str
    caller: str  # e.g. "watcher", "heartbeat_agent", "research_cycle", "telegram", "orchestrator"
    component: str  # e.g. "watcher._execute_worker", "heartbeat._run_heartbeat_agent"
    task_id: str
    model: str
    mode: str  # "non_interactive"
    success: bool | None  # None = in-flight
    duration_secs: float | None
    automated: bool  # True = system-initiated, False = operator-initiated
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Thread-safe ledger writer
_ledger_lock = threading.Lock()


def record_invocation(
    caller: str,
    component: str,
    task_id: str = "",
    model: str = "",
    mode: str = "non_interactive",
    success: bool | None = None,
    duration_secs: float | None = None,
    automated: bool = True,
    extra: dict[str, Any] | None = None,
) -> InvocationRecord:
    """Append a Claude invocation record to the ledger. Thread-safe."""
    rec = InvocationRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        caller=caller,
        component=component,
        task_id=task_id,
        model=model,
        mode=mode,
        success=success,
        duration_secs=duration_secs,
        automated=automated,
        extra=extra or {},
    )

    with _ledger_lock:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with open(LEDGER_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec.to_dict(), separators=(",", ":")) + "\n")
        except OSError:
            pass  # non-fatal — don't break caller

    return rec


def read_ledger(max_age_hours: float = 24.0) -> list[dict[str, Any]]:
    """Read recent ledger entries (up to max_age_hours old)."""
    if not LEDGER_FILE.exists():
        return []

    cutoff = time.time() - (max_age_hours * 3600)
    entries: list[dict[str, Any]] = []

    try:
        with open(LEDGER_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("timestamp", "")
                    ts = datetime.fromisoformat(ts_str).timestamp() if ts_str else 0
                    if ts >= cutoff:
                        entries.append(rec)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass

    return entries


# ---------------------------------------------------------------------------
# Rolling burn-rate analysis
# ---------------------------------------------------------------------------
@dataclass
class BurnRateMetrics:
    """Computed usage health metrics."""

    claude_calls_last_15m: int = 0
    claude_calls_last_60m: int = 0
    claude_calls_last_6h: int = 0
    rolling_baseline_calls_per_hour: float = 0.0
    burn_rate_multiplier: float = 0.0
    unique_callers_last_60m: int = 0
    dominant_caller_last_60m: str = ""
    dominant_caller_count: int = 0
    repeated_same_caller_max: int = 0
    repeated_same_task_max: int = 0
    repeated_failure_count: int = 0
    total_entries_24h: int = 0
    anomaly_score: float = 0.0  # 0.0 = normal, 1.0+ = anomalous

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _completed_only(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter ledger entries to only meaningful completed invocations.

    Excludes:
    - Start/in-flight entries (``duration_secs: null``, ``success: null``).
    - Sub-second entries (< 1.0s) — these are housekeeping no-ops like the
      orchestrator classifier returning instantly when no tasks exist, or
      heartbeat bookkeeping.  Real Claude calls take several seconds minimum.

    Only entries representing actual work are counted for burn-rate and
    runaway analysis.
    """
    return [e for e in entries if (e.get("duration_secs") or 0) >= 1.0]


def compute_burn_rate() -> BurnRateMetrics:
    """Compute usage health metrics from the local invocation ledger."""
    entries = _completed_only(read_ledger(max_age_hours=24.0))
    now = time.time()

    def _age_secs(e: dict) -> float:
        ts_str = e.get("timestamp", "")
        try:
            return now - datetime.fromisoformat(ts_str).timestamp()
        except (ValueError, TypeError):
            return float("inf")

    # Window buckets
    last_15m = [e for e in entries if _age_secs(e) <= 900]
    last_60m = [e for e in entries if _age_secs(e) <= 3600]
    last_6h = [e for e in entries if _age_secs(e) <= 21600]

    # Baseline: calls per hour over 24h (excluding last hour to avoid counting current spike)
    baseline_entries = [e for e in entries if _age_secs(e) > 3600]
    baseline_hours = (
        max(
            1.0,
            min(
                23.0,
                (
                    now
                    - min(
                        (datetime.fromisoformat(e["timestamp"]).timestamp() for e in baseline_entries),
                        default=now,
                    )
                )
                / 3600,
            ),
        )
        if baseline_entries
        else 1.0
    )
    baseline_per_hour = len(baseline_entries) / baseline_hours if baseline_hours > 0 else 0

    # Burn rate: current 60m rate vs baseline
    current_rate = len(last_60m)  # calls in last hour
    burn_mult = current_rate / max(baseline_per_hour, 0.5)  # floor at 0.5 to avoid div-by-zero spike

    # Caller analysis (60m window)
    caller_counts: dict[str, int] = {}
    task_counts: dict[str, int] = {}
    failure_count = 0

    for e in last_60m:
        c = e.get("caller", "unknown")
        caller_counts[c] = caller_counts.get(c, 0) + 1
        t = e.get("task_id", "")
        if t:
            task_counts[t] = task_counts.get(t, 0) + 1
        if e.get("success") is False:
            failure_count += 1

    dominant_caller = max(caller_counts, key=lambda k: caller_counts[k], default="") if caller_counts else ""
    dominant_count = caller_counts.get(dominant_caller, 0)

    # Anomaly score: weighted combination
    anomaly = 0.0
    cfg = get_config()
    if burn_mult > cfg.caution_burn_multiplier:
        anomaly += min(burn_mult / cfg.critical_burn_multiplier, 1.0)
    if len(last_60m) > cfg.caution_calls_60m:
        anomaly += min(len(last_60m) / cfg.critical_calls_60m, 1.0)
    if dominant_count > cfg.same_caller_threshold_60m:
        anomaly += 0.5

    return BurnRateMetrics(
        claude_calls_last_15m=len(last_15m),
        claude_calls_last_60m=len(last_60m),
        claude_calls_last_6h=len(last_6h),
        rolling_baseline_calls_per_hour=round(baseline_per_hour, 2),
        burn_rate_multiplier=round(burn_mult, 2),
        unique_callers_last_60m=len(caller_counts),
        dominant_caller_last_60m=dominant_caller,
        dominant_caller_count=dominant_count,
        repeated_same_caller_max=max(caller_counts.values(), default=0),
        repeated_same_task_max=max(task_counts.values(), default=0),
        repeated_failure_count=failure_count,
        total_entries_24h=len(entries),
        anomaly_score=round(anomaly, 2),
    )


# ---------------------------------------------------------------------------
# Runaway loop detection
# ---------------------------------------------------------------------------
@dataclass
class RunawayDetection:
    detected: bool = False
    reasons: list[str] = field(default_factory=list)
    severity: str = "info"  # info, warning, critical

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_runaway() -> RunawayDetection:
    """Detect runaway automation patterns in the invocation ledger."""
    cfg = get_config()
    entries = _completed_only(read_ledger(max_age_hours=1.0))
    now = time.time()

    def _age_secs(e: dict) -> float:
        try:
            return now - datetime.fromisoformat(e.get("timestamp", "")).timestamp()
        except (ValueError, TypeError):
            return float("inf")

    last_60m = [e for e in entries if _age_secs(e) <= 3600]
    reasons: list[str] = []
    severity = "info"

    # 1. Same caller repeated excessively
    caller_counts: dict[str, int] = {}
    for e in last_60m:
        c = e.get("caller", "unknown")
        caller_counts[c] = caller_counts.get(c, 0) + 1
    for caller, count in caller_counts.items():
        if count >= cfg.same_caller_threshold_60m:
            reasons.append(f"runaway_same_caller: {caller} ({count} calls in 60m)")
            severity = "warning"

    # 2. Same task repeated excessively
    task_counts: dict[str, int] = {}
    for e in last_60m:
        t = e.get("task_id", "")
        if t:
            task_counts[t] = task_counts.get(t, 0) + 1
    for task, count in task_counts.items():
        if count >= cfg.same_task_threshold_60m:
            reasons.append(f"runaway_same_task: {task} ({count} calls in 60m)")
            severity = "critical" if count >= cfg.same_task_threshold_60m * 2 else "warning"

    # 3. Failure retry loop
    failure_callers: dict[str, int] = {}
    for e in last_60m:
        if e.get("success") is False:
            c = e.get("caller", "unknown")
            failure_callers[c] = failure_callers.get(c, 0) + 1
    for caller, count in failure_callers.items():
        if count >= cfg.failure_retry_threshold_60m:
            reasons.append(f"runaway_failure_retry_loop: {caller} ({count} failures in 60m)")
            severity = "critical"

    # 4. Heartbeat itself causing excessive Claude work
    # Normal operation: heartbeat every 30m (2/hr) + planning_cycle (1-2) +
    # orchestrator classifier (2-3) = 5-7 calls/hr is healthy.
    # Threshold of 10 catches genuine runaway loops while avoiding false
    # positives from normal heartbeat + planning cadence.
    hb_callers = {"heartbeat_agent", "research_cycle", "planning_cycle", "memory_maintenance"}
    hb_count = sum(1 for e in last_60m if e.get("caller", "") in hb_callers)
    if hb_count >= 10:  # genuine runaway — normal cadence peaks at ~7
        reasons.append(f"runaway_heartbeat_loop: {hb_count} heartbeat-family calls in 60m")
        severity = "warning" if severity != "critical" else severity

    # 5. Overall abnormal spike
    if len(last_60m) >= cfg.critical_calls_60m:
        reasons.append(f"abnormal_usage_spike: {len(last_60m)} total calls in 60m")
        severity = "critical"
    elif len(last_60m) >= cfg.protection_calls_60m:
        reasons.append(f"abnormal_usage_spike: {len(last_60m)} total calls in 60m")
        severity = "warning" if severity != "critical" else severity

    return RunawayDetection(
        detected=bool(reasons),
        reasons=reasons,
        severity=severity,
    )


# ---------------------------------------------------------------------------
# Protection mode state machine
# ---------------------------------------------------------------------------
@dataclass
class ProtectionState:
    mode: ProtectionMode = ProtectionMode.NORMAL
    reason: str = ""
    activated_at: str = ""
    last_escalation: str = ""
    last_deescalation: str = ""
    history: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mode"] = self.mode.name
        return d


def load_protection_state() -> ProtectionState:
    """Load persisted protection state from STATE/."""
    if not PROTECTION_STATE_FILE.exists():
        return ProtectionState()
    try:
        data = json.loads(PROTECTION_STATE_FILE.read_text())
        mode = ProtectionMode[data.get("mode", "NORMAL")]
        return ProtectionState(
            mode=mode,
            reason=data.get("reason", ""),
            activated_at=data.get("activated_at", ""),
            last_escalation=data.get("last_escalation", ""),
            last_deescalation=data.get("last_deescalation", ""),
            history=data.get("history", [])[-20:],  # keep last 20 transitions
        )
    except (json.JSONDecodeError, OSError, KeyError):
        return ProtectionState()


def save_protection_state(state: ProtectionState) -> None:
    """Persist protection state."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PROTECTION_STATE_FILE.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    except OSError:
        pass


def evaluate_protection_mode(
    burn: BurnRateMetrics,
    runaway: RunawayDetection,
    auth: AuthStatus | None = None,
) -> ProtectionState:
    """Evaluate current metrics and determine protection mode.

    Transitions are deterministic:
    - normal → caution: burn_rate > 2x OR calls_60m > 15
    - caution → protection: burn_rate > 4x OR calls_60m > 25 OR runaway detected
    - protection → critical_lockdown: burn_rate > 8x OR calls_60m > 40 OR critical runaway
    - step-down: requires sustained calm (below caution thresholds) for recovery_cooldown
    """
    cfg = get_config()
    current = load_protection_state()
    now_iso = datetime.now(timezone.utc).isoformat()
    new_mode = ProtectionMode.NORMAL
    reasons: list[str] = []

    # --- Escalation rules ---
    # Auth drift
    if auth and auth.auth_mode != "max_plan_login" and auth.auth_mode != "unknown":
        reasons.append(f"auth_mode_drift: {auth.auth_mode}")
        new_mode = max(new_mode, ProtectionMode.CAUTION)

    # Critical lockdown triggers
    if burn.burn_rate_multiplier >= cfg.critical_burn_multiplier:
        reasons.append(f"burn_rate_critical: {burn.burn_rate_multiplier}x")
        new_mode = max(new_mode, ProtectionMode.CRITICAL_LOCKDOWN)
    if burn.claude_calls_last_60m >= cfg.critical_calls_60m:
        reasons.append(f"calls_60m_critical: {burn.claude_calls_last_60m}")
        new_mode = max(new_mode, ProtectionMode.CRITICAL_LOCKDOWN)
    if runaway.severity == "critical":
        reasons.append(f"runaway_critical: {'; '.join(runaway.reasons[:3])}")
        new_mode = max(new_mode, ProtectionMode.CRITICAL_LOCKDOWN)

    # Protection triggers
    if burn.burn_rate_multiplier >= cfg.protection_burn_multiplier:
        reasons.append(f"burn_rate_high: {burn.burn_rate_multiplier}x")
        new_mode = max(new_mode, ProtectionMode.PROTECTION)
    if burn.claude_calls_last_60m >= cfg.protection_calls_60m:
        reasons.append(f"calls_60m_high: {burn.claude_calls_last_60m}")
        new_mode = max(new_mode, ProtectionMode.PROTECTION)
    if runaway.detected and runaway.severity == "warning":
        reasons.append(f"runaway_warning: {'; '.join(runaway.reasons[:3])}")
        new_mode = max(new_mode, ProtectionMode.PROTECTION)

    # Caution triggers
    if burn.burn_rate_multiplier >= cfg.caution_burn_multiplier:
        reasons.append(f"burn_rate_elevated: {burn.burn_rate_multiplier}x")
        new_mode = max(new_mode, ProtectionMode.CAUTION)
    if burn.claude_calls_last_60m >= cfg.caution_calls_60m:
        reasons.append(f"calls_60m_elevated: {burn.claude_calls_last_60m}")
        new_mode = max(new_mode, ProtectionMode.CAUTION)
    if burn.claude_calls_last_15m >= cfg.caution_calls_15m:
        reasons.append(f"calls_15m_elevated: {burn.claude_calls_last_15m}")
        new_mode = max(new_mode, ProtectionMode.CAUTION)

    # --- De-escalation: only step down one level at a time, with cooldown ---
    if new_mode < current.mode:
        # Check if enough calm time has passed
        last_esc = current.last_escalation or current.activated_at
        if last_esc:
            try:
                esc_ts = datetime.fromisoformat(last_esc).timestamp()
                if time.time() - esc_ts < cfg.recovery_cooldown:
                    # Not enough calm time — hold current mode
                    new_mode = max(new_mode, ProtectionMode(current.mode - 1))
            except (ValueError, TypeError):
                pass

    # Build updated state
    reason_str = "; ".join(reasons) if reasons else "all clear"
    state = ProtectionState(
        mode=new_mode,
        reason=reason_str,
        activated_at=current.activated_at if new_mode > ProtectionMode.NORMAL else "",
        last_escalation=current.last_escalation,
        last_deescalation=current.last_deescalation,
        history=current.history[-19:],
    )

    if new_mode > current.mode:
        state.activated_at = now_iso
        state.last_escalation = now_iso
        state.history.append({"time": now_iso, "from": current.mode.name, "to": new_mode.name, "reason": reason_str})
    elif new_mode < current.mode:
        state.last_deescalation = now_iso
        state.history.append({"time": now_iso, "from": current.mode.name, "to": new_mode.name, "reason": reason_str})

    save_protection_state(state)
    return state


# ---------------------------------------------------------------------------
# Gate: should a non-essential automated task proceed?
# ---------------------------------------------------------------------------
def should_allow_task(
    caller: str,
    task_id: str = "",
    is_operator: bool = False,
    is_essential: bool = False,
) -> tuple[bool, str]:
    """Check if a Claude invocation should proceed given current protection mode.

    Returns (allowed, reason).
    Essential tasks (health checks) and operator-initiated tasks always allowed.
    """
    if is_operator or is_essential:
        return True, "allowed: operator/essential override"

    state = load_protection_state()

    if state.mode == ProtectionMode.NORMAL:
        return True, "allowed: normal mode"

    if state.mode == ProtectionMode.CAUTION:
        return True, f"allowed: caution mode (monitoring — {state.reason})"

    if state.mode == ProtectionMode.PROTECTION:
        # Block non-essential automated work
        non_essential_callers = {"research_cycle", "planning_cycle", "memory_maintenance", "proactive_seed"}
        if caller in non_essential_callers:
            return False, f"blocked: protection mode — {state.reason}"
        return True, f"allowed: protection mode (caller={caller} is essential)"

    if state.mode == ProtectionMode.CRITICAL_LOCKDOWN:
        # Only health/heartbeat and operator
        essential_callers = {"heartbeat_agent"}
        if caller in essential_callers:
            return True, f"allowed: critical lockdown (essential caller={caller})"
        return False, f"blocked: critical lockdown — {state.reason}"

    return True, "allowed: unknown mode (safe fallback)"


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------
@dataclass
class UsageAlert:
    severity: str  # "info", "warning", "critical"
    title: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_alerts(
    auth: AuthStatus,
    burn: BurnRateMetrics,
    runaway: RunawayDetection,
    protection: ProtectionState,
    prev_protection_mode: ProtectionMode | None = None,
) -> list[UsageAlert]:
    """Generate alerts for notable usage conditions."""
    alerts: list[UsageAlert] = []

    # Auth mode drift
    if auth.auth_mode != "max_plan_login" and not auth.error:
        alerts.append(
            UsageAlert(
                severity="warning",
                title="Auth mode drift",
                detail=f"Expected max_plan_login, got {auth.auth_mode}",
            )
        )

    # Auth error
    if auth.error:
        alerts.append(
            UsageAlert(
                severity="info",
                title="Auth check failed",
                detail=auth.error[:200],
            )
        )

    # Burn rate
    cfg = get_config()
    if burn.burn_rate_multiplier >= cfg.critical_burn_multiplier:
        alerts.append(
            UsageAlert(
                severity="critical",
                title="Critical burn rate",
                detail=f"{burn.burn_rate_multiplier}x baseline ({burn.claude_calls_last_60m} calls/60m)",
            )
        )
    elif burn.burn_rate_multiplier >= cfg.protection_burn_multiplier:
        alerts.append(
            UsageAlert(
                severity="warning",
                title="High burn rate",
                detail=f"{burn.burn_rate_multiplier}x baseline ({burn.claude_calls_last_60m} calls/60m)",
            )
        )
    elif burn.burn_rate_multiplier >= cfg.caution_burn_multiplier:
        alerts.append(
            UsageAlert(
                severity="info",
                title="Elevated burn rate",
                detail=f"{burn.burn_rate_multiplier}x baseline ({burn.claude_calls_last_60m} calls/60m)",
            )
        )

    # Runaway
    if runaway.detected:
        alerts.append(
            UsageAlert(
                severity=runaway.severity,
                title="Runaway loop detected",
                detail="; ".join(runaway.reasons[:3]),
            )
        )

    # Protection mode transition
    if prev_protection_mode is not None and protection.mode != prev_protection_mode:
        direction = "ESCALATED" if protection.mode > prev_protection_mode else "DE-ESCALATED"
        alerts.append(
            UsageAlert(
                severity="warning" if protection.mode > ProtectionMode.NORMAL else "info",
                title=f"Protection mode {direction}",
                detail=f"{prev_protection_mode.name} → {protection.mode.name}: {protection.reason}",
            )
        )

    return alerts


# ---------------------------------------------------------------------------
# Heartbeat integration
# ---------------------------------------------------------------------------
def check_max_plan_usage() -> dict[str, Any]:
    """Heartbeat check function. Returns a check dict compatible with heartbeat.py."""
    try:
        auth = check_auth_mode()
        burn = compute_burn_rate()
        runaway = detect_runaway()

        prev_state = load_protection_state()
        prev_mode = prev_state.mode
        protection = evaluate_protection_mode(burn, runaway, auth)

        alerts = generate_alerts(auth, burn, runaway, protection, prev_mode)

        # Determine health
        ok = protection.mode <= ProtectionMode.CAUTION and not any(a.severity == "critical" for a in alerts)

        detail_parts = [
            f"mode={protection.mode.name}",
            f"auth={auth.auth_mode}",
            f"calls_15m={burn.claude_calls_last_15m}",
            f"calls_60m={burn.claude_calls_last_60m}",
            f"burn={burn.burn_rate_multiplier}x",
        ]
        if runaway.detected:
            detail_parts.append(f"runaway={runaway.reasons[0][:50]}")
        if alerts:
            crit = sum(1 for a in alerts if a.severity == "critical")
            warn = sum(1 for a in alerts if a.severity == "warning")
            if crit:
                detail_parts.append(f"critical_alerts={crit}")
            if warn:
                detail_parts.append(f"warning_alerts={warn}")

        # Write operator status artifact
        _write_operator_status(auth, burn, runaway, protection, alerts)

        return {
            "name": "max_plan_usage",
            "ok": ok,
            "detail": ", ".join(detail_parts),
            "alerts": [a.to_dict() for a in alerts],
            "protection_mode": protection.mode.name,
            "burn_rate": burn.to_dict(),
            "runaway": runaway.to_dict(),
        }

    except Exception as e:
        return {
            "name": "max_plan_usage",
            "ok": True,
            "detail": f"check skipped: {e}",
        }


# ---------------------------------------------------------------------------
# Operator status artifact
# ---------------------------------------------------------------------------
def _write_operator_status(
    auth: AuthStatus,
    burn: BurnRateMetrics,
    runaway: RunawayDetection,
    protection: ProtectionState,
    alerts: list[UsageAlert],
) -> None:
    """Write human-readable status to STATE/max_plan_status.json."""
    entries = read_ledger(max_age_hours=24.0)

    # Top callers in 24h
    caller_counts: dict[str, int] = {}
    for e in entries:
        c = e.get("caller", "unknown")
        caller_counts[c] = caller_counts.get(c, 0) + 1
    top_callers = sorted(caller_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    # Last 10 high-usage events (entries in last 2 hours)
    now = time.time()
    recent = [e for e in entries if now - datetime.fromisoformat(e.get("timestamp", "1970-01-01")).timestamp() < 7200]
    last_10 = recent[-10:] if recent else []

    status = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "current_mode": protection.mode.name,
        "mode_description": _PROTECTION_DESCRIPTIONS.get(protection.mode, ""),
        "protection_reason": protection.reason,
        "auth_mode": auth.auth_mode,
        "subscription_type": auth.subscription_type,
        "burn_rate": burn.to_dict(),
        "runaway_detected": runaway.detected,
        "runaway_reasons": runaway.reasons,
        "alerts": [a.to_dict() for a in alerts],
        "top_callers_24h": [{"caller": c, "count": n} for c, n in top_callers],
        "last_10_events": last_10,
        "recommended_action": _recommend_action(protection, alerts),
    }

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps(status, indent=2), encoding="utf-8")
    except OSError:
        pass


def _recommend_action(protection: ProtectionState, alerts: list[UsageAlert]) -> str:
    """Generate a one-line operator recommendation."""
    if protection.mode == ProtectionMode.CRITICAL_LOCKDOWN:
        return "URGENT: Check for runaway loops. Non-essential automation is blocked. Review ledger."
    if protection.mode == ProtectionMode.PROTECTION:
        return "Non-essential automation suppressed. Monitor burn rate. Will auto-recover when calm."
    if protection.mode == ProtectionMode.CAUTION:
        return "Usage elevated. No action needed unless it continues to climb."
    if any(a.severity == "warning" for a in alerts):
        return "Minor alerts present. Monitor."
    return "All clear. No action needed."


# ---------------------------------------------------------------------------
# Ledger maintenance
# ---------------------------------------------------------------------------
def prune_ledger(max_age_hours: float = 72.0) -> int:
    """Remove ledger entries older than max_age_hours. Returns count removed."""
    if not LEDGER_FILE.exists():
        return 0

    cutoff = time.time() - (max_age_hours * 3600)
    kept: list[str] = []
    removed = 0

    try:
        with open(LEDGER_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("timestamp", "")
                    ts = datetime.fromisoformat(ts_str).timestamp() if ts_str else 0
                    if ts >= cutoff:
                        kept.append(json.dumps(rec, separators=(",", ":")))
                    else:
                        removed += 1
                except (json.JSONDecodeError, ValueError):
                    kept.append(line)  # keep unparseable lines
    except OSError:
        return 0

    if removed > 0:
        with _ledger_lock:
            try:
                with open(LEDGER_FILE, "w", encoding="utf-8") as f:
                    for line in kept:
                        f.write(line + "\n")
            except OSError:
                pass

    return removed
