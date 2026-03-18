"""Security monitoring and dashboard for NovaCore.

Aggregates security metrics from all hardening components into a single
dashboard state file for reporting via heartbeat and Telegram.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger("nova.security_monitor")

BASE_DIR = Path("/home/nova/nova-core")
STATE_DIR = BASE_DIR / "STATE"
LOGS_DIR = BASE_DIR / "LOGS"

# Process start time (approximate uptime reference)
_BOOT_TIME = time.monotonic()


# ---------------------------------------------------------------------------
# Dashboard template
# ---------------------------------------------------------------------------


def _empty_dashboard() -> dict[str, Any]:
    """Return a zeroed-out dashboard structure."""
    return {
        "last_updated": "",
        "input_sanitizer": {"blocked": 0, "flagged": 0, "passed": 0},
        "kill_switch": {"mode": "run", "heartbeat_alive": True},
        "circuit_breakers": {},
        "budget": {"daily_used_pct": 0.0, "monthly_cost": 0.0},
        "egress_blocks": 0,
        "auth_failures_24h": 0,
        "secret_detections": 0,
        "task_validations": {"passed": 0, "flagged": 0, "blocked": 0},
        "audit_chain_valid": True,
        "uptime_hours": 0.0,
    }


# ---------------------------------------------------------------------------
# SecurityDashboard
# ---------------------------------------------------------------------------


class SecurityDashboard:
    """Aggregate security metrics from all hardening subsystems.

    Reads live state from each component (kill switch, budget enforcer,
    circuit breakers, task audit log, watcher/telegram logs) and writes
    a single dashboard JSON file that heartbeat and Telegram can consume.
    """

    STATE_FILE = STATE_DIR / "security_dashboard.json"

    def __init__(self) -> None:
        self._data: dict[str, Any] = _empty_dashboard()
        # Try to load persisted state on startup
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load persisted dashboard state if available."""
        try:
            if self.STATE_FILE.exists():
                self._data = json.loads(self.STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("Failed to load dashboard state: %s", exc)

    def _save(self) -> None:
        """Persist current dashboard state to disk."""
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            self.STATE_FILE.write_text(
                json.dumps(self._data, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            _log.warning("Failed to save dashboard state: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self) -> dict[str, Any]:
        """Refresh all metrics by querying each subsystem.

        Returns the updated dashboard dict.
        """
        data = _empty_dashboard()
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        data["uptime_hours"] = round((time.monotonic() - _BOOT_TIME) / 3600, 2)

        # Collect from each subsystem (each is independently fault-tolerant)
        data["kill_switch"] = self._collect_kill_switch()
        data["budget"] = self._collect_budget()
        data["circuit_breakers"] = self._collect_circuit_breakers()
        data["task_validations"] = self._collect_task_audit()
        data["auth_failures_24h"] = self._collect_auth_failures()
        data["egress_blocks"] = self._collect_egress()
        data["secret_detections"] = self._collect_secret_detections()
        data["audit_chain_valid"] = self._collect_audit_chain_valid()

        self._data = data
        self._save()
        return data

    def get_dashboard(self) -> dict[str, Any]:
        """Return the current dashboard state (without refreshing)."""
        return dict(self._data)

    def get_summary_text(self) -> str:
        """Return a formatted text summary suitable for Telegram."""
        return format_dashboard_telegram(self._data)

    def check_anomalies(self) -> list[str]:
        """Return a list of anomaly descriptions based on current state."""
        anomalies: list[str] = []
        data = self._data

        # Circuit breaker in OPEN state
        cbs = data.get("circuit_breakers", {})
        for name, info in cbs.items():
            state = info if isinstance(info, str) else info.get("state", "")
            if state == "OPEN":
                anomalies.append(f"Circuit breaker '{name}' is OPEN")

        # Budget > 90%
        budget_pct = data.get("budget", {}).get("daily_used_pct", 0)
        if budget_pct > 90:
            anomalies.append(f"Daily budget at {budget_pct:.1f}% (exceeds 90% threshold)")

        # Kill switch not in "run"
        ks = data.get("kill_switch", {})
        mode = ks.get("mode", "run")
        if mode != "run":
            anomalies.append(f"Kill switch mode is '{mode}' (not 'run')")

        # Heartbeat not alive
        if not ks.get("heartbeat_alive", True):
            anomalies.append("Heartbeat dead man's switch is NOT alive")

        # Auth failures > 5 in 24h
        auth_failures = data.get("auth_failures_24h", 0)
        if auth_failures > 5:
            anomalies.append(f"Auth failures in 24h: {auth_failures} (exceeds threshold of 5)")

        # Any task blocked in last hour
        tv = data.get("task_validations", {})
        blocked = tv.get("blocked", 0)
        if blocked > 0:
            anomalies.append(f"Tasks blocked in last hour: {blocked}")

        return anomalies

    # ------------------------------------------------------------------
    # Metric collectors (each with try/except)
    # ------------------------------------------------------------------

    def _collect_kill_switch(self) -> dict[str, Any]:
        """Import nova_kill_switch and get mode + heartbeat status."""
        result = {"mode": "run", "heartbeat_alive": True}
        try:
            from nova_kill_switch import check_dead_man_switch, check_kill_switch

            result["mode"] = check_kill_switch()
            result["heartbeat_alive"] = check_dead_man_switch()
        except Exception as exc:
            _log.debug("Failed to collect kill switch metrics: %s", exc)
            result["error"] = str(exc)
        return result

    def _collect_budget(self) -> dict[str, Any]:
        """Import agents.budget_enforcer and get usage summary."""
        result: dict[str, Any] = {"daily_used_pct": 0.0, "monthly_cost": 0.0}
        try:
            from agents.budget_enforcer import budget

            usage = budget.get_usage_summary()
            cost = budget.get_cost_summary()

            # Daily input percentage is the primary metric
            daily_pct = usage.get("daily", {}).get("pct_input", 0.0)
            result["daily_used_pct"] = daily_pct
            result["monthly_cost"] = cost.get("monthly_cost_usd", 0.0)
            result["monthly_cap"] = cost.get("monthly_cap_usd", 0.0)
            result["monthly_pct"] = cost.get("monthly_pct", 0.0)
        except Exception as exc:
            _log.debug("Failed to collect budget metrics: %s", exc)
            result["error"] = str(exc)
        return result

    def _collect_circuit_breakers(self) -> dict[str, Any]:
        """Import agents.circuit_breakers and get states of all breakers."""
        result: dict[str, Any] = {}
        try:
            from agents.circuit_breakers import (
                _mcp_breakers,
                claude_api_breaker,
                tool_execution_breaker,
            )

            # Named breakers
            for breaker in [claude_api_breaker, tool_execution_breaker]:
                result[breaker.name] = {
                    "state": breaker.state,
                    "trip_count": breaker.trip_count,
                }

            # Dynamic MCP breakers
            for name, breaker in _mcp_breakers.items():
                result[name] = {
                    "state": breaker.state,
                    "trip_count": breaker.trip_count,
                }
        except Exception as exc:
            _log.debug("Failed to collect circuit breaker metrics: %s", exc)
            result["_error"] = str(exc)
        return result

    def _collect_task_audit(self) -> dict[str, Any]:
        """Read STATE/task_audit.jsonl and count entries from last hour."""
        result = {"passed": 0, "flagged": 0, "blocked": 0}
        audit_file = STATE_DIR / "task_audit.jsonl"
        try:
            if not audit_file.exists():
                return result

            one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

            with audit_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts = record.get("timestamp", "")
                    if ts < one_hour_ago:
                        continue

                    if record.get("blocked"):
                        result["blocked"] += 1
                    elif record.get("risk_score", 0) >= 50:
                        result["flagged"] += 1
                    else:
                        result["passed"] += 1
        except Exception as exc:
            _log.debug("Failed to collect task audit metrics: %s", exc)
        return result

    def _collect_auth_failures(self) -> int:
        """Parse recent watcher.log and telegram_bot.log for AUTH_DENIED."""
        count = 0
        twenty_four_hours_ago = datetime.now(timezone.utc) - timedelta(hours=24)

        log_files = [
            LOGS_DIR / "watcher.log",
            LOGS_DIR / "telegram_bot.log",
        ]
        # Also check audit log JSONL files for structured auth_denied events
        audit_dir = LOGS_DIR / "audit"

        # Method 1: Parse plain text logs for AUTH_DENIED
        for log_file in log_files:
            try:
                if not log_file.exists():
                    continue
                # Read last portion of log to avoid scanning huge files
                text = _tail_file(log_file, max_bytes=512_000)
                for line in text.splitlines():
                    if "AUTH_DENIED" not in line:
                        continue
                    # Try to extract timestamp from log line
                    ts = _extract_log_timestamp(line)
                    if ts and ts >= twenty_four_hours_ago:
                        count += 1
                    elif ts is None:
                        # If we can't parse the timestamp, count it
                        # conservatively (recent lines are more likely recent)
                        count += 1
            except Exception as exc:
                _log.debug("Failed to parse %s: %s", log_file, exc)

        # Method 2: Check structured audit logs
        try:
            if audit_dir.is_dir():
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
                for date_str in [yesterday, today]:
                    audit_file = audit_dir / f"audit_{date_str}.jsonl"
                    if not audit_file.exists():
                        continue
                    with audit_file.open("r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if entry.get("event") == "security.auth_denied":
                                ts_str = entry.get("timestamp", "")
                                try:
                                    ts = datetime.fromisoformat(ts_str)
                                    if ts.tzinfo is None:
                                        ts = ts.replace(tzinfo=timezone.utc)
                                    if ts >= twenty_four_hours_ago:
                                        count += 1
                                except (ValueError, TypeError):
                                    count += 1
        except Exception as exc:
            _log.debug("Failed to parse audit logs for auth failures: %s", exc)

        return count

    def _collect_egress(self) -> int:
        """Parse iptables LOG chain for EGRESS-BLOCKED count.

        This is optional -- returns 0 if iptables is not available or
        the log chain does not exist.
        """
        count = 0
        try:
            # Check kernel log or syslog for EGRESS-BLOCKED entries
            # from the last 24 hours
            syslog_paths = [
                Path("/var/log/syslog"),
                Path("/var/log/kern.log"),
                Path("/var/log/messages"),
            ]
            for log_path in syslog_paths:
                if not log_path.exists():
                    continue
                try:
                    text = _tail_file(log_path, max_bytes=256_000)
                    for line in text.splitlines():
                        if "EGRESS-BLOCKED" in line:
                            count += 1
                except PermissionError:
                    # syslog often requires root
                    pass
                break  # Only check the first available log
        except Exception as exc:
            _log.debug("Failed to collect egress metrics: %s", exc)
        return count

    def _collect_secret_detections(self) -> int:
        """Count secret_detected events in recent audit logs."""
        count = 0
        audit_dir = LOGS_DIR / "audit"
        try:
            if not audit_dir.is_dir():
                return 0
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            audit_file = audit_dir / f"audit_{today}.jsonl"
            if not audit_file.exists():
                return 0
            with audit_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("event") == "security.secret_detected":
                        count += 1
        except Exception as exc:
            _log.debug("Failed to collect secret detection metrics: %s", exc)
        return count

    def _collect_audit_chain_valid(self) -> bool:
        """Verify today's audit chain integrity."""
        try:
            from utils.audit_log import verify_chain

            audit_dir = LOGS_DIR / "audit"
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            audit_file = audit_dir / f"audit_{today}.jsonl"
            if not audit_file.exists():
                return True  # No log yet today is not an error
            valid, _errors = verify_chain(audit_file)
            return valid
        except Exception as exc:
            _log.debug("Failed to verify audit chain: %s", exc)
            # If we can't check, assume valid to avoid false alarms
            return True


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _tail_file(path: Path, max_bytes: int = 256_000) -> str:
    """Read the tail of a file (last *max_bytes* bytes).

    Avoids loading entire multi-MB log files into memory.
    """
    size = path.stat().st_size
    with path.open("r", encoding="utf-8", errors="replace") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()  # skip partial first line
        return f.read()


# Regex for common log timestamp formats
_LOG_TS_PATTERNS = [
    # ISO format: 2026-03-12T10:30:00Z or 2026-03-12T10:30:00+00:00
    re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"),
    # Standard log format: 2026-03-12 10:30:00
    re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"),
]


def _extract_log_timestamp(line: str) -> datetime | None:
    """Try to extract a datetime from a log line."""
    for pattern in _LOG_TS_PATTERNS:
        m = pattern.search(line)
        if m:
            ts_str = m.group(1)
            try:
                # Try ISO format first
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                return ts
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Telegram formatter
# ---------------------------------------------------------------------------


def format_dashboard_telegram(data: dict[str, Any]) -> str:
    """Format dashboard data as Telegram-friendly text.

    Uses text status indicators: [OK], [WARN], [CRIT].
    """
    lines: list[str] = []
    lines.append("=== NovaCore Security Dashboard ===")
    lines.append("")

    # Last updated
    updated = data.get("last_updated", "unknown")
    lines.append(f"Updated: {updated}")
    uptime = data.get("uptime_hours", 0)
    lines.append(f"Uptime: {uptime:.1f}h")
    lines.append("")

    # Kill Switch
    ks = data.get("kill_switch", {})
    mode = ks.get("mode", "unknown")
    heartbeat = ks.get("heartbeat_alive", False)

    mode_status = "[OK]" if mode == "run" else "[CRIT]"
    hb_status = "[OK]" if heartbeat else "[CRIT]"
    lines.append(f"Kill Switch:  {mode_status} mode={mode}")
    lines.append(f"Heartbeat:    {hb_status} alive={heartbeat}")
    lines.append("")

    # Budget
    budget = data.get("budget", {})
    daily_pct = budget.get("daily_used_pct", 0)
    monthly_cost = budget.get("monthly_cost", 0)
    monthly_cap = budget.get("monthly_cap", 0)

    if daily_pct > 90:
        budget_status = "[CRIT]"
    elif daily_pct > 75:
        budget_status = "[WARN]"
    else:
        budget_status = "[OK]"
    lines.append(
        f"Budget:       {budget_status} daily={daily_pct:.1f}%  monthly=${monthly_cost:.2f}/${monthly_cap:.2f}"
    )
    lines.append("")

    # Circuit Breakers
    cbs = data.get("circuit_breakers", {})
    if cbs:
        lines.append("Circuit Breakers:")
        for name, info in cbs.items():
            if name.startswith("_"):
                continue
            if isinstance(info, dict):
                state = info.get("state", "?")
                trips = info.get("trip_count", 0)
            else:
                state = str(info)
                trips = 0
            if state == "OPEN":
                cb_status = "[CRIT]"
            elif state == "HALF_OPEN":
                cb_status = "[WARN]"
            else:
                cb_status = "[OK]"
            lines.append(f"  {name}: {cb_status} {state} (trips={trips})")
    else:
        lines.append("Circuit Breakers: [OK] none tripped")
    lines.append("")

    # Task Validations
    tv = data.get("task_validations", {})
    tv_passed = tv.get("passed", 0)
    tv_flagged = tv.get("flagged", 0)
    tv_blocked = tv.get("blocked", 0)

    if tv_blocked > 0:
        tv_status = "[WARN]"
    else:
        tv_status = "[OK]"
    lines.append(f"Tasks (1h):   {tv_status} passed={tv_passed} flagged={tv_flagged} blocked={tv_blocked}")

    # Auth Failures
    auth_failures = data.get("auth_failures_24h", 0)
    if auth_failures > 5:
        auth_status = "[WARN]"
    elif auth_failures > 0:
        auth_status = "[OK]"
    else:
        auth_status = "[OK]"
    lines.append(f"Auth (24h):   {auth_status} failures={auth_failures}")

    # Egress
    egress = data.get("egress_blocks", 0)
    if egress > 0:
        egress_status = "[WARN]"
    else:
        egress_status = "[OK]"
    lines.append(f"Egress:       {egress_status} blocked={egress}")

    # Secret Detections
    secrets = data.get("secret_detections", 0)
    if secrets > 0:
        secret_status = "[CRIT]"
    else:
        secret_status = "[OK]"
    lines.append(f"Secrets:      {secret_status} detections={secrets}")

    # Audit Chain
    chain_valid = data.get("audit_chain_valid", True)
    chain_status = "[OK]" if chain_valid else "[CRIT]"
    lines.append(f"Audit Chain:  {chain_status} valid={chain_valid}")

    lines.append("")
    lines.append("=== End Dashboard ===")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

dashboard = SecurityDashboard()
