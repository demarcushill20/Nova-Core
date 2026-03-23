"""Self-Heal Investigator — AI-driven warning investigation, diagnosis, and auto-fix.

Consumes warnings from utils/warning_router.py, collects diagnostic context,
attempts safe deterministic fixes, and escalates to Telegram when needed.

Design principles:
  - Auto-fix ONLY for safe, deterministic, reversible actions
  - Always collect context BEFORE acting
  - Log every action for audit trail
  - Escalate uncertainty to human via Telegram
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_DIR = Path(os.environ.get("NOVA_STATE_DIR", "STATE"))
LOGS_DIR = Path("LOGS")
INVESTIGATION_LOG = LOGS_DIR / "investigations.jsonl"
INVESTIGATION_DIR = STATE_DIR / "investigations"
MAX_INVESTIGATION_LOG = 500
MAX_CONTEXT_LINES = 50  # max log lines to collect per source

# Services we're allowed to restart
RESTARTABLE_SERVICES = {
    "novacore-watcher",
    "novacore-telegram",
    "novacore-telegram-notifier",
}

# Files we can safely clean up if stale
CLEARABLE_STATE_FILES = {
    "dead_man_switch.json",
}


@dataclass
class InvestigationResult:
    """Outcome of investigating a single warning."""
    warning_fingerprint: str
    warning_message: str
    timestamp: str = ""
    diagnosis: str = ""
    root_cause: str = ""
    action_taken: str = "none"  # "none", "auto_fixed", "escalated", "deferred"
    fix_description: str = ""
    fix_successful: bool | None = None
    escalated: bool = False
    escalation_message: str = ""
    context_collected: dict = field(default_factory=dict)
    investigation_id: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        if not self.investigation_id:
            self.investigation_id = f"inv_{int(time.time())}_{self.warning_fingerprint[:8]}"


# ---------------------------------------------------------------------------
# Context collectors — gather diagnostic data for a warning category
# ---------------------------------------------------------------------------

def _collect_service_context(service_name: str) -> dict:
    """Collect systemd service state + recent journal lines."""
    ctx: dict[str, Any] = {"service": service_name}
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True, text=True, timeout=5,
        )
        ctx["is_active"] = result.stdout.strip()
    except Exception as e:
        ctx["is_active_error"] = str(e)

    try:
        result = subprocess.run(
            ["journalctl", "-u", service_name, "-n", "20", "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=10,
        )
        ctx["recent_logs"] = result.stdout.strip().split("\n")[-MAX_CONTEXT_LINES:]
    except Exception as e:
        ctx["journal_error"] = str(e)

    return ctx


def _collect_error_context() -> dict:
    """Recent error history from self-healing."""
    ctx: dict[str, Any] = {}
    error_file = STATE_DIR / "error_history.jsonl"
    try:
        lines = error_file.read_text().strip().splitlines()
        recent = [json.loads(ln) for ln in lines[-10:]]
        ctx["recent_errors"] = recent
    except Exception:
        ctx["recent_errors"] = []
    return ctx


def _collect_resource_context() -> dict:
    """Disk, memory, process context."""
    ctx: dict[str, Any] = {}
    try:
        result = subprocess.run(
            ["df", "-h", "/home/nova"],
            capture_output=True, text=True, timeout=5,
        )
        ctx["disk"] = result.stdout.strip()
    except Exception:
        pass

    rss_file = STATE_DIR / "rss_history.jsonl"
    try:
        lines = rss_file.read_text().strip().splitlines()
        recent = [json.loads(ln) for ln in lines[-5:]]
        ctx["memory_trend"] = recent
    except Exception:
        ctx["memory_trend"] = []

    return ctx


def _collect_budget_context() -> dict:
    """Current budget/cost state."""
    ctx: dict[str, Any] = {}
    budgets_dir = STATE_DIR / "budgets"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    usage_file = budgets_dir / f"usage_{today}.json"
    try:
        ctx["daily_usage"] = json.loads(usage_file.read_text())
    except Exception:
        ctx["daily_usage"] = {}

    # Burn rate
    status_file = STATE_DIR / "max_plan_status.json"
    try:
        ctx["burn_rate"] = json.loads(status_file.read_text())
    except Exception:
        ctx["burn_rate"] = {}

    return ctx


def _collect_breaker_context() -> dict:
    """Circuit breaker states from registry."""
    ctx: dict[str, Any] = {}
    try:
        from utils.breaker_registry import get_all_breaker_states
        ctx["breaker_states"] = get_all_breaker_states()
    except Exception:
        # Fallback: check state file
        try:
            bf = STATE_DIR / "breaker_states.json"
            ctx["breaker_states"] = json.loads(bf.read_text()) if bf.exists() else {}
        except Exception:
            ctx["breaker_states"] = {}
    return ctx


def _collect_drift_context() -> dict:
    """Drift baseline and recent report."""
    ctx: dict[str, Any] = {}
    baseline_file = STATE_DIR / "drift_baseline.json"
    try:
        ctx["drift_baseline"] = json.loads(baseline_file.read_text())
    except Exception:
        ctx["drift_baseline"] = {}
    return ctx


def _collect_runaway_context() -> dict:
    """Max plan guard protection state."""
    ctx: dict[str, Any] = {}
    protection_file = STATE_DIR / "max_plan_protection.json"
    try:
        ctx["protection_state"] = json.loads(protection_file.read_text())
    except Exception:
        ctx["protection_state"] = {}

    ledger_file = STATE_DIR / "max_plan_ledger.jsonl"
    try:
        lines = ledger_file.read_text().strip().splitlines()
        recent = [json.loads(ln) for ln in lines[-20:]]
        ctx["recent_invocations"] = recent
    except Exception:
        ctx["recent_invocations"] = []

    return ctx


# Category → context collectors mapping
CONTEXT_COLLECTORS: dict[str, list] = {
    "heartbeat": [_collect_service_context, _collect_error_context, _collect_resource_context],
    "circuit_breaker": [_collect_breaker_context, _collect_error_context],
    "budget": [_collect_budget_context],
    "token_spike": [_collect_budget_context, _collect_runaway_context],
    "drift": [_collect_drift_context, _collect_error_context],
    "runaway": [_collect_runaway_context, _collect_budget_context],
    "service": [_collect_service_context, _collect_error_context],
    "memory": [_collect_resource_context],
    "risk": [_collect_error_context],
    "error_spike": [_collect_error_context, _collect_resource_context],
}


def collect_context(category: str, warning_context: dict) -> dict:
    """Gather all relevant diagnostic context for a warning category."""
    collected: dict[str, Any] = {"warning_data": warning_context}
    collectors = CONTEXT_COLLECTORS.get(category, [_collect_error_context])

    for collector in collectors:
        try:
            if collector == _collect_service_context:
                # Extract service name from warning context
                svc = warning_context.get("service") or warning_context.get("name", "")
                for known_svc in RESTARTABLE_SERVICES:
                    if known_svc in str(warning_context):
                        svc = known_svc
                        break
                if svc:
                    collected.update(collector(svc))
            else:
                collected.update(collector())
        except Exception as e:
            collected[f"collector_error_{collector.__name__}"] = str(e)

    return collected


# ---------------------------------------------------------------------------
# Auto-fix actions — safe, deterministic, reversible
# ---------------------------------------------------------------------------

def _try_restart_service(service_name: str) -> tuple[bool, str]:
    """Restart a failed systemd service. Returns (success, detail)."""
    if service_name not in RESTARTABLE_SERVICES:
        return False, f"Service {service_name} not in restart whitelist"

    # Check if actually failed first
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True, text=True, timeout=5,
        )
        state = result.stdout.strip()
        if state == "active":
            return True, f"Service {service_name} is already active"
    except Exception as e:
        return False, f"Failed to check service state: {e}"

    try:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", service_name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            # Verify it came back
            time.sleep(2)
            verify = subprocess.run(
                ["systemctl", "is-active", service_name],
                capture_output=True, text=True, timeout=5,
            )
            if verify.stdout.strip() == "active":
                return True, f"Restarted {service_name} successfully"
            else:
                return False, f"Restarted {service_name} but state is {verify.stdout.strip()}"
        else:
            return False, f"Restart failed: {result.stderr.strip()}"
    except Exception as e:
        return False, f"Restart exception: {e}"


def _try_clear_stale_state(filename: str) -> tuple[bool, str]:
    """Remove a stale state file if it's older than threshold."""
    if filename not in CLEARABLE_STATE_FILES:
        return False, f"File {filename} not in clearable whitelist"

    filepath = STATE_DIR / filename
    if not filepath.exists():
        return True, f"{filename} doesn't exist (already clean)"

    try:
        age = time.time() - filepath.stat().st_mtime
        if age < 600:  # < 10 min — not stale
            return False, f"{filename} is only {age:.0f}s old — not stale"
        filepath.unlink()
        return True, f"Cleared stale {filename} (age: {age:.0f}s)"
    except Exception as e:
        return False, f"Failed to clear {filename}: {e}"


def _try_adjust_degradation(target_tier: str, reason: str) -> tuple[bool, str]:
    """Adjust degradation tier if current is worse than target."""
    try:
        from utils.self_healing import get_degradation_tier, set_degradation_tier, DegradationTier

        tier_map = {
            "FULL": DegradationTier.FULL,
            "REDUCED": DegradationTier.REDUCED,
            "MINIMAL": DegradationTier.MINIMAL,
            "EMERGENCY": DegradationTier.EMERGENCY,
        }
        current = get_degradation_tier()
        target = tier_map.get(target_tier)
        if target is None:
            return False, f"Unknown tier: {target_tier}"

        if current.tier == target:
            return True, f"Already at {target_tier}"

        set_degradation_tier(target, reason)
        return True, f"Adjusted degradation: {current.tier.name} → {target_tier}"
    except Exception as e:
        return False, f"Degradation adjustment failed: {e}"


# ---------------------------------------------------------------------------
# Diagnosis engine — pattern-match warnings to known issues
# ---------------------------------------------------------------------------

@dataclass
class DiagnosisResult:
    root_cause: str
    can_auto_fix: bool
    fix_action: str = ""    # "restart_service", "clear_state", "adjust_degradation", "none"
    fix_params: dict = field(default_factory=dict)
    confidence: str = "medium"
    escalation_reason: str = ""


def diagnose(warning_msg: str, category: str, context: dict) -> DiagnosisResult:
    """Pattern-match a warning to a known root cause and suggest action."""

    msg_lower = warning_msg.lower()

    # Service failures → restart
    if category in ("heartbeat", "service"):
        for svc in RESTARTABLE_SERVICES:
            if svc in msg_lower or svc in str(context):
                svc_state = context.get("is_active", "unknown")
                if svc_state in ("inactive", "failed", "activating"):
                    return DiagnosisResult(
                        root_cause=f"Service {svc} is {svc_state}",
                        can_auto_fix=True,
                        fix_action="restart_service",
                        fix_params={"service_name": svc},
                        confidence="high",
                    )
                elif svc_state == "active":
                    return DiagnosisResult(
                        root_cause=f"Service {svc} is active now (transient failure)",
                        can_auto_fix=False,
                        fix_action="none",
                        confidence="medium",
                    )

    # Circuit breaker open → escalate (breakers auto-reset)
    if category == "circuit_breaker":
        breaker_name = context.get("warning_data", {}).get("breaker_name", "unknown")
        return DiagnosisResult(
            root_cause=f"Circuit breaker {breaker_name} tripped — dependency failures",
            can_auto_fix=False,
            fix_action="none",
            confidence="high",
            escalation_reason=f"Breaker {breaker_name} OPEN — investigate dependency",
        )

    # Budget alerts → escalate (don't auto-adjust budgets)
    if category in ("budget", "token_spike"):
        return DiagnosisResult(
            root_cause="Token/cost budget threshold exceeded",
            can_auto_fix=False,
            fix_action="none",
            confidence="high",
            escalation_reason="Budget alert — review usage patterns",
        )

    # Runaway loop → degrade to REDUCED to slow down
    if category == "runaway":
        severity = context.get("warning_data", {}).get("severity", "warning")
        if severity == "critical":
            return DiagnosisResult(
                root_cause="Critical runaway loop detected",
                can_auto_fix=True,
                fix_action="adjust_degradation",
                fix_params={"target_tier": "REDUCED", "reason": "Runaway loop — auto-throttle"},
                confidence="high",
            )
        return DiagnosisResult(
            root_cause="Possible runaway loop",
            can_auto_fix=False,
            fix_action="none",
            confidence="medium",
            escalation_reason="Possible runaway — monitor closely",
        )

    # Memory pressure → degrade
    if category == "memory":
        if "critical" in msg_lower:
            return DiagnosisResult(
                root_cause="Critical memory pressure",
                can_auto_fix=True,
                fix_action="adjust_degradation",
                fix_params={"target_tier": "MINIMAL", "reason": "Critical RSS — auto-degrade"},
                confidence="high",
            )
        return DiagnosisResult(
            root_cause="Elevated memory usage",
            can_auto_fix=False,
            fix_action="none",
            confidence="medium",
            escalation_reason="Memory trending high",
        )

    # Error spike → collect info and escalate
    if category == "error_spike":
        recent = context.get("recent_errors", [])
        permanent_count = sum(1 for e in recent if e.get("category") in ("PERMANENT", 3))
        if permanent_count >= 3:
            return DiagnosisResult(
                root_cause=f"Multiple permanent errors ({permanent_count}) — likely config or dependency issue",
                can_auto_fix=False,
                confidence="high",
                escalation_reason=f"{permanent_count} permanent errors — needs investigation",
            )
        return DiagnosisResult(
            root_cause="Elevated error rate — mostly transient",
            can_auto_fix=False,
            fix_action="none",
            confidence="medium",
        )

    # Drift → informational
    if category == "drift":
        return DiagnosisResult(
            root_cause="Behavioral drift from baseline",
            can_auto_fix=False,
            fix_action="none",
            confidence="medium",
            escalation_reason="Performance drift detected — review task patterns",
        )

    # Default: escalate unknowns
    return DiagnosisResult(
        root_cause="Unclassified warning",
        can_auto_fix=False,
        fix_action="none",
        confidence="low",
        escalation_reason=f"Unclassified: {warning_msg[:100]}",
    )


# ---------------------------------------------------------------------------
# Fix executor
# ---------------------------------------------------------------------------

FIX_ACTIONS = {
    "restart_service": _try_restart_service,
    "clear_state": _try_clear_stale_state,
    "adjust_degradation": _try_adjust_degradation,
}


def execute_fix(diagnosis: DiagnosisResult) -> tuple[bool, str]:
    """Execute the recommended fix action. Returns (success, detail)."""
    if not diagnosis.can_auto_fix or diagnosis.fix_action == "none":
        return False, "No auto-fix available"

    handler = FIX_ACTIONS.get(diagnosis.fix_action)
    if not handler:
        return False, f"Unknown fix action: {diagnosis.fix_action}"

    try:
        if diagnosis.fix_action == "restart_service":
            return handler(diagnosis.fix_params["service_name"])
        elif diagnosis.fix_action == "clear_state":
            return handler(diagnosis.fix_params["filename"])
        elif diagnosis.fix_action == "adjust_degradation":
            return handler(
                diagnosis.fix_params["target_tier"],
                diagnosis.fix_params.get("reason", "auto-fix"),
            )
        else:
            return False, f"No executor for: {diagnosis.fix_action}"
    except Exception as e:
        return False, f"Fix execution error: {e}"


# ---------------------------------------------------------------------------
# Telegram escalation
# ---------------------------------------------------------------------------

def _send_telegram(text: str) -> bool:
    """Send message via Telegram API."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return False

    try:
        import urllib.request
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def escalate_to_telegram(result: InvestigationResult) -> bool:
    """Send investigation escalation to Telegram."""
    lines = [
        f"🔍 *Self-Heal Investigation*",
        f"",
        f"⚠️ *Warning:* {result.warning_message[:200]}",
        f"🔎 *Diagnosis:* {result.diagnosis}",
        f"🎯 *Root Cause:* {result.root_cause}",
    ]

    if result.action_taken == "auto_fixed":
        lines.append(f"✅ *Auto-fixed:* {result.fix_description}")
        if not result.fix_successful:
            lines.append(f"❌ *Fix failed — needs manual review*")
    elif result.action_taken == "escalated":
        lines.append(f"📢 *Escalated:* {result.escalation_message}")
    else:
        lines.append(f"ℹ️ *Action:* {result.action_taken}")

    text = "\n".join(lines)
    return _send_telegram(text)


# ---------------------------------------------------------------------------
# Investigation log
# ---------------------------------------------------------------------------

def _log_investigation(result: InvestigationResult) -> None:
    """Append investigation result to JSONL log."""
    INVESTIGATION_LOG.parent.mkdir(parents=True, exist_ok=True)
    INVESTIGATION_DIR.mkdir(parents=True, exist_ok=True)

    entry = asdict(result)

    # Append to JSONL
    try:
        existing = []
        if INVESTIGATION_LOG.exists():
            lines = INVESTIGATION_LOG.read_text().strip().splitlines()
            existing = lines[-MAX_INVESTIGATION_LOG:]
        existing.append(json.dumps(entry))
        INVESTIGATION_LOG.write_text("\n".join(existing) + "\n")
    except Exception:
        pass

    # Write individual investigation file
    try:
        inv_file = INVESTIGATION_DIR / f"{result.investigation_id}.json"
        inv_file.write_text(json.dumps(entry, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main investigation pipeline
# ---------------------------------------------------------------------------

def investigate_warning(warning) -> InvestigationResult:
    """Full investigation pipeline for a single warning.

    Args:
        warning: Warning dataclass from warning_router

    Returns:
        InvestigationResult with diagnosis, action taken, and outcome
    """
    from utils.warning_router import mark_investigated

    # 1. Initialize result
    result = InvestigationResult(
        warning_fingerprint=warning.fingerprint,
        warning_message=warning.message,
    )

    # 2. Collect diagnostic context
    ctx = collect_context(warning.category, warning.context)
    result.context_collected = ctx

    # 3. Diagnose
    diag = diagnose(warning.message, warning.category, ctx)
    result.diagnosis = diag.root_cause
    result.root_cause = diag.root_cause

    # 4. Auto-fix if safe
    if diag.can_auto_fix:
        success, detail = execute_fix(diag)
        result.action_taken = "auto_fixed"
        result.fix_description = detail
        result.fix_successful = success

        # Escalate if fix failed
        if not success:
            result.escalated = True
            result.escalation_message = f"Auto-fix failed: {detail}"
            escalate_to_telegram(result)
        else:
            # Notify success for critical fixes
            if warning.severity >= 2:  # CRITICAL
                escalate_to_telegram(result)
    elif diag.escalation_reason:
        # 5. Escalate
        result.action_taken = "escalated"
        result.escalated = True
        result.escalation_message = diag.escalation_reason
        escalate_to_telegram(result)
    else:
        result.action_taken = "deferred"

    # 6. Log and mark investigated
    _log_investigation(result)
    mark_investigated(warning.fingerprint, result.investigation_id)

    return result


def investigate_all_pending() -> list[InvestigationResult]:
    """Process all pending warnings in the queue.

    Returns list of investigation results.
    """
    from utils.warning_router import get_pending_warnings

    warnings = get_pending_warnings()
    results = []

    for warning in warnings:
        try:
            result = investigate_warning(warning)
            results.append(result)
        except Exception as e:
            # Don't let one bad investigation block others
            err_result = InvestigationResult(
                warning_fingerprint=warning.fingerprint,
                warning_message=warning.message,
                diagnosis=f"Investigation failed: {e}",
                action_taken="escalated",
                escalated=True,
                escalation_message=f"Investigation error: {e}",
            )
            _log_investigation(err_result)
            results.append(err_result)

    return results


# ---------------------------------------------------------------------------
# Health check — heartbeat compatible
# ---------------------------------------------------------------------------

def check_investigator_health() -> dict:
    """Heartbeat-compatible health check for the investigator itself."""
    from utils.warning_router import get_queue_stats

    stats = get_queue_stats()
    pending = stats.get("pending", 0)
    critical = stats.get("by_severity", {}).get("critical", 0)

    ok = pending < 20 and critical < 5
    detail = f"{pending} pending warnings ({critical} critical)"

    return {
        "name": "self_heal_investigator",
        "ok": ok,
        "detail": detail,
        "stats": stats,
    }
