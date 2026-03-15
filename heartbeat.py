#!/usr/bin/env python3
"""NovaCore heartbeat — proactive health monitoring.

Runs as a systemd oneshot (triggered by novacore-heartbeat.timer every 30min).
Checks service health, disk, task queue, and worker liveness.
Writes HEARTBEAT.md, alerts via Telegram on failure, optionally injects repair tasks.

Stdlib only — no pip installs required.
"""

import json
import os
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from utils.structured_log import slog
    from utils.trace_context import TraceContext
except ImportError:
    slog = None  # type: ignore[assignment]
    TraceContext = None  # type: ignore[assignment,misc]

# --- Configuration -----------------------------------------------------------

BASE = Path("/home/nova/nova-core")
HEARTBEAT_FILE = BASE / "HEARTBEAT.md"
STATE_DIR = BASE / "STATE"
TASKS_DIR = BASE / "TASKS"
OUTPUT_DIR = BASE / "OUTPUT"
LOGS_DIR = BASE / "LOGS"

DISK_WARN_PERCENT = 85
ORPHAN_INPROGRESS_MINUTES = 15
MAX_PENDING_TASKS = 10
BACKUP_DIR = Path("/home/nova/backups")
BACKUP_MAX_AGE_HOURS = 26  # alert if no backup in ~1 day
LOG_SIZE_WARN_MB = 50
STATE_DIR_WARN_FILES = 50000
GW_TOKEN_FILE = Path.home() / ".config" / "nova-core" / "google" / "token.json"

SERVICES = [
    "novacore-watcher",
    "novacore-telegram",
    "novacore-telegram-notifier",
]


# --- Health checks -----------------------------------------------------------


def check_service(name: str) -> dict:
    """Check if a systemd service is active."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        active = result.stdout.strip() == "active"
        if active:
            info = subprocess.run(
                ["systemctl", "show", name, "--property=MainPID,ActiveEnterTimestamp"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            props = dict(line.split("=", 1) for line in info.stdout.strip().splitlines() if "=" in line)
            pid = props.get("MainPID", "?")
            since = props.get("ActiveEnterTimestamp", "?")
            detail = f"active (pid {pid}, since {since})"
        else:
            detail = f"NOT ACTIVE ({result.stdout.strip()})"
        return {"name": f"service:{name}", "ok": active, "detail": detail}
    except Exception as e:
        return {"name": f"service:{name}", "ok": False, "detail": f"check failed: {e}"}


def check_disk() -> dict:
    """Check disk usage on the partition containing ~/nova-core."""
    try:
        st = os.statvfs(str(BASE))
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used_pct = round((1 - free / total) * 100, 1)
        free_gb = round(free / (1024**3), 1)
        ok = used_pct < DISK_WARN_PERCENT
        return {"name": "disk", "ok": ok, "detail": f"{used_pct}% used ({free_gb}GB free)"}
    except Exception as e:
        return {"name": "disk", "ok": False, "detail": f"check failed: {e}"}


def check_claude_binary() -> dict:
    """Check that the Claude CLI binary is accessible."""
    claude_path = Path(os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude"))
    ok = claude_path.exists() and os.access(str(claude_path), os.X_OK)
    detail = "accessible" if ok else "NOT FOUND or not executable"
    return {"name": "claude_binary", "ok": ok, "detail": detail}


def check_task_queue() -> dict:
    """Check for pending tasks and orphaned .inprogress files."""
    if not TASKS_DIR.exists():
        return {"name": "task_queue", "ok": True, "detail": "no TASKS dir"}

    lifecycle_suffixes = (".inprogress", ".done", ".failed", ".cancelled")
    pending = [p for p in TASKS_DIR.glob("*.md") if not any(p.name.endswith(s) for s in lifecycle_suffixes)]

    inprogress = list(TASKS_DIR.glob("*.inprogress"))
    now = datetime.now(timezone.utc)
    orphaned = []
    for ip in inprogress:
        age_min = (now - datetime.fromtimestamp(ip.stat().st_mtime, tz=timezone.utc)).total_seconds() / 60
        if age_min > ORPHAN_INPROGRESS_MINUTES:
            orphaned.append(ip.name)

    ok = len(pending) <= MAX_PENDING_TASKS and len(orphaned) == 0
    detail = f"{len(pending)} pending, {len(inprogress)} in-progress"
    if orphaned:
        detail += f", ORPHANED: {', '.join(orphaned)}"
    return {"name": "task_queue", "ok": ok, "detail": detail}


def check_last_output() -> dict:
    """Check recency of last OUTPUT file (informational, not critical)."""
    if not OUTPUT_DIR.exists():
        return {"name": "last_output", "ok": True, "detail": "no OUTPUT dir"}

    outputs = sorted(
        OUTPUT_DIR.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not outputs:
        return {"name": "last_output", "ok": True, "detail": "no outputs yet"}

    latest = outputs[0]
    age_min = (
        datetime.now(timezone.utc) - datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
    ).total_seconds() / 60
    detail = f"{latest.name} ({round(age_min)}min ago)"
    return {"name": "last_output", "ok": True, "detail": detail}


def check_stale_workers() -> dict:
    """Check for PID files in STATE/running/ pointing to dead processes."""
    running_dir = STATE_DIR / "running"
    if not running_dir.exists():
        return {"name": "stale_workers", "ok": True, "detail": "no running dir"}

    stale = []
    for pid_file in running_dir.glob("*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
        except ProcessLookupError:
            stale.append(f"{pid_file.stem} (pid {pid} dead)")
        except (ValueError, PermissionError):
            pass

    ok = len(stale) == 0
    detail = "all clean" if ok else f"{len(stale)} stale: {', '.join(stale)}"
    return {"name": "stale_workers", "ok": ok, "detail": detail}


def check_state_files() -> dict:
    """Validate structure of critical state files (e.g., goals.json)."""
    issues = []
    goals_file = STATE_DIR / "goals.json"
    if goals_file.exists():
        try:
            data = json.loads(goals_file.read_text())
            if isinstance(data, list):
                issues.append("goals.json is bare array (expected dict wrapper)")
            elif isinstance(data, dict):
                if "goals" not in data or "next_id" not in data:
                    issues.append("goals.json missing 'goals' or 'next_id' keys")
            else:
                issues.append("goals.json has unexpected type")
        except json.JSONDecodeError as e:
            issues.append(f"goals.json invalid JSON: {e}")
        except OSError as e:
            issues.append(f"goals.json read error: {e}")

    ok = len(issues) == 0
    detail = "all valid" if ok else "; ".join(issues)
    return {"name": "state_files", "ok": ok, "detail": detail}


def check_metrics() -> dict:
    """Check STATE/metrics.json for anomalous failure rates."""
    metrics_file = STATE_DIR / "metrics.json"
    if not metrics_file.exists():
        return {"name": "metrics", "ok": True, "detail": "no metrics file yet"}
    try:
        data = json.loads(metrics_file.read_text())
        if not isinstance(data, dict):
            return {"name": "metrics", "ok": True, "detail": "empty metrics"}
        cf = data.get("contract_failure", 0)
        cs = data.get("contract_success", 0)
        failures = cf.get("_total", 0) if isinstance(cf, dict) else cf
        successes = cs.get("_total", 0) if isinstance(cs, dict) else cs
        total = failures + successes
        if total == 0:
            return {"name": "metrics", "ok": True, "detail": "no executions recorded"}
        fail_rate = round(failures / total * 100, 1)
        ok = fail_rate < 50
        return {"name": "metrics", "ok": ok, "detail": f"{fail_rate}% failure rate ({failures}/{total})"}
    except Exception as e:
        return {"name": "metrics", "ok": False, "detail": f"parse error: {e}"}


def check_google_workspace() -> dict:
    """Check that the Google Workspace OAuth token is valid."""
    if not GW_TOKEN_FILE.exists():
        return {"name": "google_workspace", "ok": True, "detail": "not configured (no token)"}
    try:
        data = json.loads(GW_TOKEN_FILE.read_text())
        has_refresh = bool(data.get("refresh_token"))
        expiry = data.get("expiry", "")
        if expiry:
            # Token file stores expiry as ISO string
            exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if exp_dt < now and not has_refresh:
                return {"name": "google_workspace", "ok": False, "detail": "token expired, no refresh token"}
        detail = "token valid" if has_refresh else "token present (no refresh)"
        return {"name": "google_workspace", "ok": has_refresh, "detail": detail}
    except Exception as e:
        return {"name": "google_workspace", "ok": False, "detail": f"token check failed: {e}"}


def check_backup() -> dict:
    """Verify a recent backup exists in /home/nova/backups/."""
    if not BACKUP_DIR.exists():
        return {"name": "backup", "ok": False, "detail": f"backup dir missing: {BACKUP_DIR}"}

    tarballs = sorted(BACKUP_DIR.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not tarballs:
        return {"name": "backup", "ok": False, "detail": "no backups found"}

    latest = tarballs[0]
    age_hr = (
        datetime.now(timezone.utc) - datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
    ).total_seconds() / 3600
    size_mb = round(latest.stat().st_size / (1024**2), 1)
    ok = age_hr < BACKUP_MAX_AGE_HOURS
    detail = f"{latest.name} ({round(age_hr, 1)}h ago, {size_mb}MB)"
    if not ok:
        detail += " — STALE"
    return {"name": "backup", "ok": ok, "detail": detail}


def check_ruff() -> dict:
    """Run ruff on staged/tracked Python files and report violation count."""
    try:
        result = subprocess.run(
            ["ruff", "check", "--select", "E,F,W", "--statistics", "--quiet", str(BASE)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return {"name": "ruff", "ok": True, "detail": "0 violations"}
        lines = result.stdout.strip().splitlines()
        count = len(lines)
        detail = f"{count} violation(s)"
        if lines:
            detail += f" — top: {lines[0][:80]}"
        # Informational, not blocking — ok=True but report count
        return {"name": "ruff", "ok": True, "detail": detail}
    except FileNotFoundError:
        return {"name": "ruff", "ok": True, "detail": "ruff not installed"}
    except subprocess.TimeoutExpired:
        return {"name": "ruff", "ok": True, "detail": "timed out (30s)"}
    except Exception as e:
        return {"name": "ruff", "ok": True, "detail": f"check failed: {e}"}


def check_memory_systems() -> dict:
    """Check connectivity to Fusion Memory and Obsidian Vault."""
    issues = []

    # Fusion Memory: check if the MCP server entry point exists
    fusion_server = Path.home() / "Nova_AI_Fusion_Memory_MCP" / "mcp_server.py"
    if not fusion_server.exists():
        issues.append("Fusion Memory mcp_server.py missing")

    # Obsidian Vault: check if vault directory exists and has content
    vault_dir = Path("/home/nova/nova-vault")
    if not vault_dir.exists():
        issues.append("Obsidian vault dir missing")
    else:
        meta_dir = vault_dir / "_meta"
        if not meta_dir.exists() or not list(meta_dir.glob("*.md")):
            issues.append("Obsidian vault _meta/ empty or missing")

    ok = len(issues) == 0
    detail = "both reachable" if ok else "; ".join(issues)
    return {"name": "memory_systems", "ok": ok, "detail": detail}


def check_log_sizes() -> dict:
    """Check for oversized log files that need rotation."""
    large = []
    if LOGS_DIR.exists():
        for log_file in LOGS_DIR.iterdir():
            if log_file.is_file():
                size_mb = log_file.stat().st_size / (1024**2)
                if size_mb > LOG_SIZE_WARN_MB:
                    large.append(f"{log_file.name} ({round(size_mb, 1)}MB)")

    ok = len(large) == 0
    if ok:
        total_mb: float = 0
        if LOGS_DIR.exists():
            total_mb = sum(f.stat().st_size for f in LOGS_DIR.iterdir() if f.is_file()) / (1024**2)
        detail = f"all under {LOG_SIZE_WARN_MB}MB (total: {round(total_mb, 1)}MB)"
    else:
        detail = f"{len(large)} oversized: {', '.join(large)}"
    return {"name": "log_sizes", "ok": ok, "detail": detail}


def check_state_bloat() -> dict:
    """Check for STATE/ subdirectories with excessive file counts."""
    bloated = []
    if STATE_DIR.exists():
        for subdir in STATE_DIR.iterdir():
            if subdir.is_dir():
                try:
                    count = sum(1 for _ in subdir.iterdir())
                    if count > STATE_DIR_WARN_FILES:
                        bloated.append(f"{subdir.name}/ ({count} files)")
                except PermissionError:
                    pass

    ok = len(bloated) == 0
    detail = "all clean" if ok else f"bloated: {', '.join(bloated)}"
    return {"name": "state_bloat", "ok": ok, "detail": detail}


def check_pip_audit() -> dict:
    """Run pip-audit monthly. Only runs on the 1st and 15th of the month."""
    now = datetime.now(timezone.utc)
    if now.day not in (1, 15):
        return {"name": "pip_audit", "ok": True, "detail": f"skipped (runs on 1st/15th, today is {now.day}th)"}
    try:
        result = subprocess.run(
            ["pip-audit", "--format", "json", "--progress-spinner", "off"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout) if result.stdout.strip() else []
            if not data:
                return {"name": "pip_audit", "ok": True, "detail": "0 vulnerabilities"}
            vuln_count = len(data) if isinstance(data, list) else 0
            return {"name": "pip_audit", "ok": vuln_count == 0, "detail": f"{vuln_count} vulnerable package(s)"}
        else:
            # pip-audit returns non-zero when vulnerabilities found
            detail = result.stdout[:200] if result.stdout else result.stderr[:200]
            return {"name": "pip_audit", "ok": False, "detail": f"vulnerabilities found: {detail}"}
    except FileNotFoundError:
        return {"name": "pip_audit", "ok": True, "detail": "pip-audit not installed"}
    except subprocess.TimeoutExpired:
        return {"name": "pip_audit", "ok": True, "detail": "timed out (120s)"}
    except Exception as e:
        return {"name": "pip_audit", "ok": True, "detail": f"check failed: {e}"}


# --- Output ------------------------------------------------------------------


def write_heartbeat(checks: list) -> None:
    """Write HEARTBEAT.md with timestamped checklist."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# NovaCore Heartbeat",
        f"Last check: {now}",
        "",
    ]
    for c in checks:
        mark = "x" if c["ok"] else " "
        lines.append(f"- [{mark}] {c['name']}: {c['detail']}")

    all_ok = all(c["ok"] for c in checks)
    lines.append("")
    lines.append(f"Overall: {'HEALTHY' if all_ok else 'UNHEALTHY'}")
    lines.append("")

    HEARTBEAT_FILE.write_text("\n".join(lines) + "\n")


# --- Alerting ----------------------------------------------------------------


def _send_telegram(text: str) -> None:
    """Send a message to the configured Telegram chat."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("ALLOWED_CHAT_ID", "")
    if not token or not chat_id:
        print("WARN: TELEGRAM_BOT_TOKEN or ALLOWED_CHAT_ID not set, skipping alert")
        return

    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"WARN: Telegram send failed: {e}")


def send_telegram_alert(checks: list) -> None:
    """Send Telegram message listing failed checks. Only called when unhealthy."""
    failed = [c for c in checks if not c["ok"]]
    lines = ["⚠️ NovaCore Heartbeat — UNHEALTHY", ""]
    for c in failed:
        lines.append(f"❌ {c['name']}: {c['detail']}")
    _send_telegram("\n".join(lines))


def send_telegram_heartbeat(checks: list) -> None:
    """Send a compact heartbeat pulse to Telegram on every run."""
    all_ok = all(c["ok"] for c in checks)
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    fail_count = len([c for c in checks if not c["ok"]])

    if all_ok:
        text = f"💚 Heartbeat {now} — HEALTHY ({len(checks)}/{len(checks)} checks passed)"
    else:
        failed = [c for c in checks if not c["ok"]]
        lines = [f"🔴 Heartbeat {now} — UNHEALTHY ({fail_count} failed)"]
        for c in failed:
            lines.append(f"  ❌ {c['name']}: {c['detail']}")
        text = "\n".join(lines)

    _send_telegram(text)


# --- Self-repair -------------------------------------------------------------


def inject_repair_task(checks: list) -> None:
    """For service failures, inject a self-repair task into TASKS/."""
    failed_services = [c for c in checks if c["name"].startswith("service:") and not c["ok"]]
    if not failed_services:
        return

    # Rate-limit: skip if a recent repair task is already in-progress
    existing = list(TASKS_DIR.glob("hb_*_self_repair.md*"))
    in_progress = [p for p in existing if p.name.endswith(".inprogress")]
    if in_progress:
        print(f"SKIP repair injection — already in-progress: {in_progress[0].name}")
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    stem = f"hb_{ts}_self_repair"
    task_path = TASKS_DIR / f"{stem}.md"

    names = ", ".join(c["name"].replace("service:", "") for c in failed_services)
    content = (
        "# Heartbeat Self-Repair Task\n\n"
        "The following services were detected as unhealthy by the heartbeat:\n"
        f"{names}\n\n"
        "## Instructions\n"
        "1. Check `journalctl -u <service> -n 50` for each failed service.\n"
        "2. Attempt `sudo systemctl restart <service>` for each.\n"
        "3. Verify the restart succeeded with `systemctl is-active <service>`.\n"
        "4. Write results to OUTPUT.\n"
    )
    task_path.write_text(content)
    print(f"Injected repair task: {task_path}")


# --- LLM-driven proactive heartbeat ------------------------------------------

# Active hours (UTC) — only run LLM heartbeat during these hours
ACTIVE_HOURS_START = int(os.environ.get("HEARTBEAT_ACTIVE_START", "0"))
ACTIVE_HOURS_END = int(os.environ.get("HEARTBEAT_ACTIVE_END", "24"))

# Model for heartbeat reasoning
HEARTBEAT_MODEL = os.environ.get("HEARTBEAT_MODEL", "claude-opus-4-6")
HEARTBEAT_TIMEOUT = 90  # seconds

CHECKLIST_FILE = BASE / "HEARTBEAT_CHECKLIST.md"
HEARTBEAT_AGENT_LOG = LOGS_DIR / "heartbeat_agent.log"

# Autonomous research cycle configuration
RESEARCH_TIMEOUT = 14400  # 4 hours for deep research
RESEARCH_COOLDOWN_MINUTES = 55  # skip if ran less than 55 min ago
RESEARCH_COOLDOWN_FILE = STATE_DIR / "last_research_cycle.json"
RESEARCH_LOG = LOGS_DIR / "research_cycle.log"

# Planning cycle configuration — runs every 3rd active cycle
PLANNING_TIMEOUT = 14400  # 4 hours for planning
PLANNING_COOLDOWN_MINUTES = 170  # ~every 3 hours (timer fires every 30 min)
PLANNING_COOLDOWN_FILE = STATE_DIR / "last_planning_cycle.json"
PLANNING_LOG = LOGS_DIR / "planning_cycle.log"

# Memory maintenance configuration — runs periodically
MEMORY_MAINTENANCE_COOLDOWN_MINUTES = 360  # every 6 hours
MEMORY_MAINTENANCE_COOLDOWN_FILE = STATE_DIR / "last_memory_maintenance.json"


def _gather_extended_state(checks: list) -> str:
    """Collect system state summary for the LLM heartbeat agent."""
    parts = []

    # Deterministic check results
    parts.append("## Deterministic Health Checks")
    for c in checks:
        mark = "PASS" if c["ok"] else "FAIL"
        parts.append(f"  [{mark}] {c['name']}: {c['detail']}")

    # Pending tasks (with age)
    pending = [
        p
        for p in TASKS_DIR.glob("*.md")
        if not any(p.name.endswith(s) for s in (".inprogress", ".done", ".failed", ".cancelled"))
    ]
    if pending:
        parts.append(f"\n## Pending Tasks ({len(pending)})")
        now = datetime.now(timezone.utc)
        for p in sorted(pending, key=lambda x: x.stat().st_mtime):
            age_min = (now - datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)).total_seconds() / 60
            parts.append(f"  - {p.stem} ({round(age_min)}min old)")

    # Recent failed tasks (last 2 hours)
    failed = list(TASKS_DIR.glob("*.failed"))
    now = datetime.now(timezone.utc)
    recent_failed = []
    for f in failed:
        age_hr = (now - datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
        if age_hr < 2:
            recent_failed.append(f)
    if recent_failed:
        parts.append(f"\n## Recently Failed Tasks ({len(recent_failed)})")
        for f in recent_failed:
            parts.append(f"  - {f.stem}")

    # Recent outputs (last 4 hours)
    outputs = sorted(
        OUTPUT_DIR.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    recent_outputs = []
    for o in outputs[:10]:
        age_hr = (now - datetime.fromtimestamp(o.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
        if age_hr < 4:
            recent_outputs.append((o, age_hr))
    if recent_outputs:
        parts.append(f"\n## Recent Outputs ({len(recent_outputs)} in last 4h)")
        for o, age in recent_outputs:
            parts.append(f"  - {o.stem} ({round(age, 1)}h ago)")

    # Goals
    goals_file = STATE_DIR / "goals.json"
    if goals_file.exists():
        try:
            data = json.loads(goals_file.read_text())
            # Handle both dict-wrapped and bare-array formats
            if isinstance(data, dict):
                goals_list = data.get("goals", [])
            elif isinstance(data, list):
                goals_list = data
            else:
                goals_list = []
            active = [g for g in goals_list if g.get("status") != "done"]
            if active:
                parts.append(f"\n## Active Goals ({len(active)})")
                for g in active:
                    parts.append(f"  - [{g.get('id', '?')}] {g.get('text', '?')}")
        except Exception:
            pass

    # Last heartbeat agent action (to avoid repeating)
    if HEARTBEAT_AGENT_LOG.exists():
        try:
            lines = HEARTBEAT_AGENT_LOG.read_text().strip().splitlines()
            if lines:
                parts.append("\n## Last Agent Action")
                parts.append(f"  {lines[-1][:200]}")
        except Exception:
            pass

    return "\n".join(parts)


def _run_heartbeat_agent(checks: list) -> None:
    """LLM-driven heartbeat: reads checklist + state, decides whether to act.

    Runs AFTER deterministic health checks. Only during active hours.
    Uses Haiku for cost efficiency.
    """
    current_hour = datetime.now(timezone.utc).hour
    if not (ACTIVE_HOURS_START <= current_hour < ACTIVE_HOURS_END):
        print(f"[heartbeat-agent] Outside active hours ({ACTIVE_HOURS_START}-{ACTIVE_HOURS_END} UTC), skipping")
        return

    if not CHECKLIST_FILE.exists():
        print("[heartbeat-agent] No HEARTBEAT_CHECKLIST.md, skipping")
        return

    checklist = CHECKLIST_FILE.read_text()
    system_state = _gather_extended_state(checks)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    prompt = (
        f"You are Nova, running a periodic heartbeat check.\n\n"
        f"Current time: {now}\n\n"
        f"## Your Checklist\n{checklist}\n\n"
        f"## Current System State\n{system_state}\n\n"
        f"Review each checklist item against the system state. "
        f"Only flag things that genuinely need attention — no false alarms. "
        f"If nothing needs attention, respond with exactly: HEARTBEAT_OK"
    )

    claude_bin = os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude")

    try:
        child_env = os.environ.copy()
        child_env.pop("CLAUDECODE", None)
        child_env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        result = subprocess.run(
            [claude_bin, "-p", "--model", HEARTBEAT_MODEL, "--dangerously-skip-permissions", prompt],
            capture_output=True,
            text=True,
            timeout=HEARTBEAT_TIMEOUT,
            cwd=str(BASE),
            env=child_env,
        )
        response = result.stdout.strip()

        if not response:
            stderr_hint = result.stderr.strip()[:200] if result.stderr else ""
            _log_agent(
                f"EMPTY_RESPONSE — agent returned nothing"
                f" (exit={result.returncode}"
                f"{f', stderr={stderr_hint}' if stderr_hint else ''})"
            )
            return

        if "HEARTBEAT_OK" in response:
            _log_agent("HEARTBEAT_OK")
            print("[heartbeat-agent] All clear — HEARTBEAT_OK")
            return

        # Agent flagged something — parse and act
        _log_agent(f"ACTION: {response[:300]}")
        print(f"[heartbeat-agent] Action needed: {response[:200]}")
        _handle_agent_actions(response)

    except subprocess.TimeoutExpired:
        _log_agent("TIMEOUT")
        print(f"[heartbeat-agent] Timed out after {HEARTBEAT_TIMEOUT}s")
    except FileNotFoundError:
        _log_agent(f"CLAUDE_NOT_FOUND: {claude_bin}")
        print(f"[heartbeat-agent] Claude binary not found: {claude_bin}")
    except Exception as e:
        _log_agent(f"ERROR: {e}")
        print(f"[heartbeat-agent] Error: {e}")


def _handle_agent_actions(response: str) -> None:
    """Parse agent response and execute actions (notify or create task)."""
    # Try to extract JSON actions from the response
    actions = _extract_json_actions(response)

    if actions:
        for action in actions:
            action_type = action.get("type", "")
            if action_type == "notify":
                msg = action.get("message", "")
                if msg:
                    _send_telegram(f"🤖 Nova Heartbeat Agent:\n{msg}")
            elif action_type == "task":
                title = action.get("title", "heartbeat_proactive")
                body = action.get("body", "")
                _inject_proactive_task(title, body)
    else:
        # No structured JSON — treat the whole response as a notification
        _send_telegram(f"🤖 Nova Heartbeat Agent:\n{response[:500]}")


def _extract_json_actions(text: str) -> list | None:
    """Try to extract a JSON action array from the agent's response."""
    import re

    # Look for JSON array in the response (possibly in a code block)
    json_match = re.search(r"\[[\s\S]*?\]", text)
    if json_match:
        try:
            data = json.loads(json_match.group())
            if isinstance(data, list) and all(isinstance(d, dict) for d in data):
                return data
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _inject_proactive_task(title: str, body: str) -> None:
    """Create a proactive task file for the watcher to pick up."""
    # Rate-limit: max 4 pending proactive tasks (allows research pipeline to build up)
    existing = list(TASKS_DIR.glob("hb_proactive_*.md"))
    recent = [p for p in existing if not any(p.name.endswith(s) for s in (".done", ".failed", ".cancelled"))]
    if len(recent) >= 4:
        print("[heartbeat-agent] Rate limit: 4 proactive tasks already pending")
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    import re

    slug = re.sub(r"[^a-zA-Z0-9]+", "_", title.strip()).strip("_").lower()[:50]
    stem = f"hb_proactive_{ts}_{slug}"
    task_path = TASKS_DIR / f"{stem}.md"
    content = f"# Heartbeat Proactive Task\n\n{body}" if body else f"# {title}"
    task_path.write_text(content)
    print(f"[heartbeat-agent] Injected proactive task: {task_path.name}")


def _log_agent(message: str) -> None:
    """Append to heartbeat agent log."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(HEARTBEAT_AGENT_LOG, "a") as f:
        f.write(f"[{ts}] {message}\n")


# --- Memory Maintenance Scheduler (Phase 8) -----------------------------------


def _memory_maintenance_cooldown_ok() -> bool:
    """Check if enough time has passed since the last memory maintenance run."""
    if not MEMORY_MAINTENANCE_COOLDOWN_FILE.exists():
        return True
    try:
        data = json.loads(MEMORY_MAINTENANCE_COOLDOWN_FILE.read_text())
        last_run = data.get("last_run_utc", "")
        if not last_run:
            return True
        last_dt = datetime.fromisoformat(last_run)
        age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
        return age_min >= MEMORY_MAINTENANCE_COOLDOWN_MINUTES
    except Exception:
        return True


def _run_memory_maintenance() -> None:
    """Run periodic memory maintenance: governance sweeps, consolidation, compaction.

    Phase 8: Scheduled maintenance jobs. Runs every 6 hours via heartbeat timer.
    All operations are fail-open — maintenance failures never block heartbeat.

    Safety note: Governance and compaction run with dry_run=False (active mode).
    This is intentional — the module defaults are dry_run=True for API safety,
    but heartbeat explicitly opts into real maintenance. Actions taken:
    - Governance: marks stale loops (>14d), archives terminal loops (>30d)
    - Compaction: removes detected duplicates, cleans supersession chains
    """
    if not _memory_maintenance_cooldown_ok():
        print("[memory-maintenance] Cooldown not met, skipping")
        return

    print("[memory-maintenance] Starting scheduled maintenance (active mode, dry_run=False)...")
    results: dict[str, str] = {}

    # 1. Governance sweeps (retention, stale loop detection, protection enforcement)
    try:
        from agents.memory_governance import GovernanceEngine

        gov = GovernanceEngine()
        sweep = gov.run_sweep(dry_run=False)
        results["governance"] = f"ok ({sweep.items_acted_on} actions, {sweep.items_protected} protected)"
        print(f"[memory-maintenance] Governance: {sweep.items_acted_on} actions")
    except Exception as exc:
        results["governance"] = f"error: {exc}"
        print(f"[memory-maintenance] Governance failed: {exc}")

    # 2. Memory consolidation (window-based compression of working memory)
    try:
        from agents.memory_consolidator import WindowSpec, consolidate_window
        from agents.memory_router import WorkingMemoryAdapter

        adapter = WorkingMemoryAdapter()
        items = adapter.recall("", max_results=50)
        if items:
            spec = WindowSpec(
                window_type="working_memory",
                source_items=items,
            )
            consol = consolidate_window(spec)
            results["consolidation"] = (
                f"ok ({consol.dedupe_removed} deduped from {consol.input_count}, action={consol.action})"
            )
            print(f"[memory-maintenance] Consolidation: {consol.action}, {consol.dedupe_removed} deduped")
        else:
            results["consolidation"] = "ok (no items to consolidate)"
    except Exception as exc:
        results["consolidation"] = f"error: {exc}"
        print(f"[memory-maintenance] Consolidation failed: {exc}")

    # 3. Memory compaction (duplicate detection, supersession chain cleanup)
    try:
        from agents.memory_compactor import CompactionEngine

        compactor = CompactionEngine()
        compact = compactor.run_compaction(dry_run=False)
        results["compaction"] = f"ok ({compact.items_compacted} compacted, {compact.duplicates_found} dupes)"
        print(f"[memory-maintenance] Compaction: {compact.items_compacted} compacted")
    except Exception as exc:
        results["compaction"] = f"error: {exc}"
        print(f"[memory-maintenance] Compaction failed: {exc}")

    # Update cooldown
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_MAINTENANCE_COOLDOWN_FILE.write_text(
        json.dumps(
            {
                "last_run_utc": datetime.now(timezone.utc).isoformat(),
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )

    print(f"[memory-maintenance] Complete: {results}")


# --- Autonomous Research Cycle -----------------------------------------------


def _research_cooldown_ok() -> bool:
    """Check if enough time has passed since the last research cycle."""
    if not RESEARCH_COOLDOWN_FILE.exists():
        return True
    try:
        data = json.loads(RESEARCH_COOLDOWN_FILE.read_text())
        last_run = data.get("last_run_utc", "")
        if not last_run:
            return True
        last_dt = datetime.fromisoformat(last_run)
        age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
        return age_min >= RESEARCH_COOLDOWN_MINUTES
    except Exception:
        return True


def _update_research_cooldown(topic: str, success: bool) -> None:
    """Record that a research cycle ran."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "last_run_utc": datetime.now(timezone.utc).isoformat(),
        "topic": topic[:200],
        "success": success,
    }
    RESEARCH_COOLDOWN_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _log_research(message: str) -> None:
    """Append to research cycle log."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(RESEARCH_LOG, "a") as f:
        f.write(f"[{ts}] {message}\n")


def _scan_codebase() -> str:
    """Scan the nova-core codebase and return a structured snapshot."""
    parts = []

    # Recent git log (last 15 commits)
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-15", "--no-decorate"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(BASE),
        )
        if result.stdout.strip():
            parts.append("### Recent Commits (last 15)")
            parts.append(result.stdout.strip())
    except Exception:
        pass

    # File tree — top-level + key directories
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(BASE),
        )
        if result.stdout.strip():
            files = result.stdout.strip().splitlines()
            # Group by top-level directory
            dirs: dict[str, list[str]] = {}
            for f in files:
                top = f.split("/")[0] if "/" in f else "(root)"
                dirs.setdefault(top, []).append(f)
            parts.append(f"\n### File Tree ({len(files)} tracked files)")
            for d in sorted(dirs):
                parts.append(f"  {d}/ — {len(dirs[d])} files")
            # List Python files explicitly (these are the codebase)
            py_files = [f for f in files if f.endswith(".py")]
            parts.append(f"\n### Python Modules ({len(py_files)})")
            for f in py_files:
                parts.append(f"  {f}")
    except Exception:
        pass

    # Code stats — lines of Python
    try:
        total_lines = 0
        for py in BASE.rglob("*.py"):
            if ".venv" in str(py) or "__pycache__" in str(py):
                continue
            try:
                total_lines += len(py.read_text().splitlines())
            except Exception:
                pass
        parts.append("\n### Code Stats")
        parts.append(f"  Total Python lines: {total_lines:,}")
    except Exception:
        pass

    # Recent file changes (modified in last 24h)
    try:
        now = datetime.now(timezone.utc)
        recently_modified = []
        for py in BASE.rglob("*.py"):
            if ".venv" in str(py) or "__pycache__" in str(py):
                continue
            age_hr = (now - datetime.fromtimestamp(py.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
            if age_hr < 24:
                recently_modified.append((py.relative_to(BASE), round(age_hr, 1)))
        if recently_modified:
            parts.append("\n### Recently Modified (last 24h)")
            for f, age in sorted(recently_modified, key=lambda x: x[1]):  # type: ignore[assignment]
                parts.append(f"  {f} ({age}h ago)")
    except Exception:
        pass

    # Uncommitted changes
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(BASE),
        )
        if result.stdout.strip():
            lines = result.stdout.strip().splitlines()
            parts.append(f"\n### Uncommitted Changes ({len(lines)})")
            for line in lines[:20]:
                parts.append(f"  {line}")
    except Exception:
        pass

    return "\n".join(parts) if parts else "(codebase scan failed)"


def _build_research_prompt() -> str:
    """Build the comprehensive research cycle prompt."""
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_str = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y%m%d-%H%M%S")

    # Gather recent OUTPUT file names for topic deduplication
    recent_topics = []
    if OUTPUT_DIR.exists():
        for f in sorted(OUTPUT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            recent_topics.append(f.stem)
    recent_str = "\n".join(f"  - {t}" for t in recent_topics) or "  (none)"

    # Gather active goals
    goals_str = "(no active goals)"
    goals_file = STATE_DIR / "goals.json"
    if goals_file.exists():
        try:
            data = json.loads(goals_file.read_text())
            goals_list = data.get("goals", []) if isinstance(data, dict) else data
            active = [g for g in goals_list if g.get("status") != "done"]
            if active:
                goals_str = "\n".join(f"  - [{g.get('id', '?')}] {g.get('text', '?')}" for g in active)
        except Exception:
            pass

    # Scan the codebase
    codebase_snapshot = _scan_codebase()

    return f"""\
You are Nova, an autonomous AI agent. This is your RESEARCH CYCLE.
Current time: {ts}

YOUR MISSION: Scan your codebase AND both memory systems for context, then
conduct deep research on a topic of your choice, create a research report,
and save it to BOTH of your memory systems. You have 10 minutes.

═══════════════════════════════════════════════════════════════
STEP 1: CONTEXT GATHERING — scan codebase + both memory systems
═══════════════════════════════════════════════════════════════

1a. Query Fusion Memory for prior research:
    - Call `get_last_checkpoint` to see session state
    - Call `query_memory` with query="research topics completed nova-core"
    - Call `query_memory` with query="knowledge gaps improvement areas"

1b. Scan Obsidian Vault for existing research:
    - Call `vault_list` on path "40-research" to see what exists
    - Call `vault_search` with query="research" to find research notes

1c. Codebase snapshot (your own code — look for gaps, patterns, opportunities):
{codebase_snapshot}

1d. Review recent outputs (DO NOT repeat these topics):
{recent_str}

1e. Active goals to align research with:
{goals_str}

═══════════════════════════════════════════════════════════════
STEP 2: CHOOSE A RESEARCH TOPIC
═══════════════════════════════════════════════════════════════

Pick ONE topic that:
- Has NOT been researched before (check memory + recent outputs above)
- Aligns with an active goal when possible
- Is informed by the codebase scan — what does nova-core need right now?
- Is actionable — results should improve nova-core
- Has enough depth for a substantive report

Topic categories (pick one, go deep):
- New MCP servers or tools for agent capabilities
- Autonomous agent architecture patterns (self-healing, reflection, planning)
- Production hardening for AI agent runtimes
- Memory/RAG innovations and knowledge graph techniques
- Code quality and testing automation for AI-generated code
- Security practices for autonomous AI systems
- Monitoring and observability for agent runtimes
- Task scheduling and workflow orchestration
- Self-improvement and meta-learning in AI agents
- Cost optimization for LLM-powered systems

═══════════════════════════════════════════════════════════════
STEP 3: DEEP WEB RESEARCH (use multiple tools)
═══════════════════════════════════════════════════════════════

- Run 3-5 search queries using `brave_web_search` and/or `tavily_search`
- Use `tavily_research` for at least one deep synthesis query
- Fetch 2-3 authoritative full pages using `fetch`
- Cross-reference findings across multiple sources
- Prefer sources from 2025-2026

═══════════════════════════════════════════════════════════════
STEP 4: WRITE OUTPUT REPORT
═══════════════════════════════════════════════════════════════

Create file: /home/nova/nova-core/OUTPUT/hb_research_{stamp}.md

Include:
- # Title with topic and date
- ## Executive Summary (2-3 paragraphs)
- ## Key Findings (numbered, detailed)
- ## Recommendations for Nova-Core (specific, actionable)
- ## Sources (URLs with brief descriptions)
- ## CONTRACT block (required — see below)

═══════════════════════════════════════════════════════════════
STEP 5: SAVE TO FUSION MEMORY (MANDATORY — do not skip)
═══════════════════════════════════════════════════════════════

Call `upsert_memory` with these exact parameters:
- content: Dense summary of findings (500-1000 chars)
- id: "research_{date_str}_<topic_slug>"
- metadata: {{
    "category": "research",
    "project": "nova-core",
    "topic": "<the research topic>",
    "date": "{date_str}",
    "confidence": "high",
    "source": "heartbeat"
  }}

═══════════════════════════════════════════════════════════════
STEP 6: SAVE TO OBSIDIAN VAULT (MANDATORY — do not skip)
═══════════════════════════════════════════════════════════════

Call `vault_write` with these exact parameters:
- path: "40-research/<topic-slug>-{date_str}.md"
- frontmatter (MUST match this schema exactly):
    type: "research-summary"
    research_id: "rs-<topic-slug>-{date_str}"
    title: "<Research Title>"
    topic: "<topic category>"
    date_researched: "{date_str}"
    sources_count: <integer — number of sources>
    confidence: "high"
    source: "nova-core-memory"
    tags:
      - "#type/research"
      - "<topic-tag>"
      - "heartbeat-research"
- body: Full research content (findings + recommendations + sources)

IMPORTANT: The frontmatter must include `source: "nova-core-memory"` and
tags must include "#type/research". Without these, vault_write will reject.

═══════════════════════════════════════════════════════════════
STEP 7: SELF-CHECK
═══════════════════════════════════════════════════════════════

Before exiting, verify:
1. OUTPUT file exists at /home/nova/nova-core/OUTPUT/hb_research_{stamp}.md
2. upsert_memory returned success
3. vault_write returned success
If any failed, retry ONCE.

## CONTRACT (at end of OUTPUT report)
summary: <one-line description>
files_changed: OUTPUT/hb_research_{stamp}.md
verification: OUTPUT exists, upsert_memory success, vault_write success
confidence: high

BEGIN NOW. Start with Step 1 — query your memory systems."""


def _run_research_cycle() -> None:
    """Autonomous research cycle: query memories, pick topic, research, save.

    Runs a full Claude session with 600s timeout. Only during active hours.
    Respects cooldown to avoid running back-to-back.
    """
    # Paused 2026-03-14: in active build mode, research cycles are not needed.
    # Planning cycle remains active. Flip this to False to re-enable.
    RESEARCH_CYCLE_PAUSED = True
    if RESEARCH_CYCLE_PAUSED:
        print("[research-cycle] Paused (RESEARCH_CYCLE_PAUSED=True), skipping")
        return

    current_hour = datetime.now(timezone.utc).hour
    if not (ACTIVE_HOURS_START <= current_hour < ACTIVE_HOURS_END):
        print(f"[research-cycle] Outside active hours ({ACTIVE_HOURS_START}-{ACTIVE_HOURS_END} UTC), skipping")
        return

    if not _research_cooldown_ok():
        print("[research-cycle] Cooldown active — skipping (ran recently)")
        return

    print("[research-cycle] Starting autonomous research cycle...")
    prompt = _build_research_prompt()

    claude_bin = os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude")

    try:
        child_env = os.environ.copy()
        child_env.pop("CLAUDECODE", None)
        child_env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        result = subprocess.run(
            [claude_bin, "-p", "--model", HEARTBEAT_MODEL, "--dangerously-skip-permissions", prompt],
            capture_output=True,
            text=True,
            timeout=RESEARCH_TIMEOUT,
            cwd=str(BASE),
            env=child_env,
        )
        response = result.stdout.strip()

        if not response:
            stderr_hint = result.stderr.strip()[:200] if result.stderr else ""
            _log_research(
                f"EMPTY_RESPONSE (exit={result.returncode}{f', stderr={stderr_hint}' if stderr_hint else ''})"
            )
            _update_research_cooldown("unknown", success=False)
            print(f"[research-cycle] Empty response from Claude (exit={result.returncode})")
            return

        # Extract topic from response for logging
        topic = "unknown"
        for line in response.splitlines()[:30]:
            if line.startswith("#") and len(line) > 3:
                topic = line.lstrip("# ").strip()[:100]
                break

        # Check vault write outcome from subprocess output
        response_lower = response.lower()
        vault_ok = "vault_write" in response_lower and (
            "success" in response_lower or "accepted" in response_lower or "written" in response_lower
        )
        vault_failed = "vault_write" in response_lower and (
            "error" in response_lower
            or "rejected" in response_lower
            or "failed" in response_lower
            or "invalid" in response_lower
        )
        memory_ok = "upsert_memory" in response_lower and ("success" in response_lower or "stored" in response_lower)

        # Log persistence outcomes independently
        if vault_ok:
            _log_research("VAULT_WRITE: success")
        elif vault_failed:
            _log_research("VAULT_WRITE: FAILED — check vault audit log")
        else:
            _log_research("VAULT_WRITE: unknown (not detected in output)")

        if memory_ok:
            _log_research("FUSION_MEMORY: success")
        else:
            _log_research("FUSION_MEMORY: unknown (not detected in output)")

        _log_research(f"COMPLETED: {topic}")
        _update_research_cooldown(topic, success=True)
        print(f"[research-cycle] Completed: {topic}")

        # Build status summary for notification
        sinks = []
        if vault_ok:
            sinks.append("vault ✓")
        elif vault_failed:
            sinks.append("vault ✗")
        if memory_ok:
            sinks.append("memory ✓")
        sink_str = f" [{', '.join(sinks)}]" if sinks else ""
        _send_telegram(f"🔬 Research Cycle Complete:\n{topic}{sink_str}")

    except subprocess.TimeoutExpired:
        _log_research(f"TIMEOUT after {RESEARCH_TIMEOUT}s")
        _update_research_cooldown("timeout", success=False)
        print(f"[research-cycle] Timed out after {RESEARCH_TIMEOUT}s")
    except FileNotFoundError:
        _log_research(f"CLAUDE_NOT_FOUND: {claude_bin}")
        print(f"[research-cycle] Claude binary not found: {claude_bin}")
    except Exception as e:
        _log_research(f"ERROR: {e}")
        _update_research_cooldown("error", success=False)
        print(f"[research-cycle] Error: {e}")


# --- Autonomous Planning Cycle -----------------------------------------------


def _planning_cooldown_ok() -> bool:
    """Check if enough time has passed since the last planning cycle."""
    if not PLANNING_COOLDOWN_FILE.exists():
        return True
    try:
        data = json.loads(PLANNING_COOLDOWN_FILE.read_text())
        last_run = data.get("last_run_utc", "")
        if not last_run:
            return True
        last_dt = datetime.fromisoformat(last_run)
        age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
        return age_min >= PLANNING_COOLDOWN_MINUTES
    except Exception:
        return True


def _update_planning_cooldown(plan_title: str, success: bool) -> None:
    """Record that a planning cycle ran."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "last_run_utc": datetime.now(timezone.utc).isoformat(),
        "plan_title": plan_title[:200],
        "success": success,
    }
    PLANNING_COOLDOWN_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _log_planning(message: str) -> None:
    """Append to planning cycle log."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(PLANNING_LOG, "a") as f:
        f.write(f"[{ts}] {message}\n")


def _build_planning_prompt() -> str:
    """Build the planning cycle prompt — create or revise implementation plans."""
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_str = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y%m%d-%H%M%S")

    # Reuse the codebase scan
    codebase_snapshot = _scan_codebase()

    # Gather recent OUTPUT file names
    recent_outputs = []
    if OUTPUT_DIR.exists():
        for f in sorted(OUTPUT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            recent_outputs.append(f.stem)
    recent_str = "\n".join(f"  - {t}" for t in recent_outputs) or "  (none)"

    # Gather active goals
    goals_str = "(no active goals)"
    goals_file = STATE_DIR / "goals.json"
    if goals_file.exists():
        try:
            data = json.loads(goals_file.read_text())
            goals_list = data.get("goals", []) if isinstance(data, dict) else data
            active = [g for g in goals_list if g.get("status") != "done"]
            if active:
                goals_str = "\n".join(f"  - [{g.get('id', '?')}] {g.get('text', '?')}" for g in active)
        except Exception:
            pass

    return f"""\
You are Nova, an autonomous AI agent. This is your PLANNING CYCLE.
Current time: {ts}

YOUR MISSION: Scan your codebase AND both memory systems for full context,
then either CREATE a new phased implementation plan or REVISE an existing
plan based on new research findings. Save the plan to BOTH memory systems.
You have 10 minutes.

═══════════════════════════════════════════════════════════════
STEP 1: DEEP CONTEXT GATHERING
═══════════════════════════════════════════════════════════════

1a. Query Fusion Memory for existing plans and recent research:
    - Call `get_last_checkpoint` to see session state
    - Call `query_memory` with query="implementation plan nova-core phases"
    - Call `query_memory` with query="recent research findings discoveries"
    - Call `query_memory` with query="enhancement plan revision"

1b. Scan Obsidian Vault for existing plans and patterns:
    - Call `vault_search` with query="implementation plan"
    - Call `vault_search` with query="enhancement"
    - Call `vault_list` on path "40-research" to see recent research

1c. Codebase snapshot (your own code — understand what exists):
{codebase_snapshot}

1d. Recent outputs (research reports to build plans from):
{recent_str}

1e. Active goals:
{goals_str}

═══════════════════════════════════════════════════════════════
STEP 2: DECIDE — CREATE NEW or REVISE EXISTING
═══════════════════════════════════════════════════════════════

Based on your context scan, decide:

A) CREATE a new plan if:
   - No current plan exists, OR
   - The existing plan is fully completed, OR
   - New research has revealed a completely new direction

B) REVISE an existing plan if:
   - A current plan exists but new research has been done since last revision
   - Some phases are complete and need updating
   - Priorities have shifted based on new findings

When revising: read the existing plan fully, note what's done, what's
changed, and what new research suggests. Don't start from scratch.

═══════════════════════════════════════════════════════════════
STEP 3: BUILD THE PLAN
═══════════════════════════════════════════════════════════════

Your plan MUST follow this structure:

# Nova-Core Enhancement Plan v<N> — {date_str}

## Vision
One paragraph describing the overall direction.

## Current State
What's built, what's working, what's missing. Reference the codebase scan.

## Phase-by-Phase Implementation

### Phase <N>: <Name> (priority: high/medium/low)
**Goal:** What this phase achieves
**Prerequisites:** What must be done first
**Steps:**
1. Step with specific file paths and code changes
2. Step with specific commands or configurations
3. Step with verification criteria
**Estimated complexity:** small/medium/large
**Success criteria:** How to know it's done

(Repeat for each phase — aim for 3-7 phases)

## Research Gaps
Topics that need research before certain phases can start.

## Quick Wins
Small improvements (< 30 min each) that can be done immediately.

═══════════════════════════════════════════════════════════════
STEP 4: WRITE OUTPUT REPORT
═══════════════════════════════════════════════════════════════

Create file: /home/nova/nova-core/OUTPUT/hb_plan_{stamp}.md

Include the full plan plus a ## CONTRACT block at the end.

═══════════════════════════════════════════════════════════════
STEP 5: SAVE TO FUSION MEMORY (MANDATORY)
═══════════════════════════════════════════════════════════════

Call `upsert_memory` with:
- content: Plan summary with phase names and priorities (500-1000 chars)
- id: "plan_{date_str}_<plan_slug>"
- metadata: {{
    "category": "decision",
    "project": "nova-core",
    "topic": "enhancement_plan",
    "date": "{date_str}",
    "confidence": "high",
    "source": "heartbeat",
    "plan_version": "<version number>"
  }}

═══════════════════════════════════════════════════════════════
STEP 6: SAVE TO OBSIDIAN VAULT (MANDATORY)
═══════════════════════════════════════════════════════════════

Call `vault_write` with EXACTLY this frontmatter (do NOT change the type or source):
- path: "00-inbox/plan-nova-core-{stamp}.md"
- frontmatter:
    type: "implementation-plan"
    plan_id: "plan-nova-core-{stamp}"
    title: "Nova-Core Enhancement Plan"
    date_created: "{date_str}"
    confidence: "high"
    source: "nova-core-memory"
    tags:
      - "#type/plan"
      - "planning"
      - "heartbeat-planning"
- body: Full plan content

CRITICAL: You MUST use type: "implementation-plan" and source: "nova-core-memory".
Any other values will be rejected by schema validation.

═══════════════════════════════════════════════════════════════
STEP 7: SELF-CHECK
═══════════════════════════════════════════════════════════════

Verify:
1. OUTPUT file exists at /home/nova/nova-core/OUTPUT/hb_plan_{stamp}.md
2. upsert_memory returned success
3. vault_write returned success
If any failed, retry ONCE.

## CONTRACT (at end of OUTPUT report)
summary: <one-line: created or revised plan>
files_changed: OUTPUT/hb_plan_{stamp}.md
verification: OUTPUT exists, upsert_memory success, vault_write success
confidence: high

BEGIN NOW. Start with Step 1 — gather context from memory and codebase."""


def _run_planning_cycle() -> None:
    """Autonomous planning cycle: scan context, create or revise plans.

    Runs every 3rd active cycle (~90 min). Same self-awareness layers as
    research, but output is an implementation plan instead of a report.
    """
    current_hour = datetime.now(timezone.utc).hour
    if not (ACTIVE_HOURS_START <= current_hour < ACTIVE_HOURS_END):
        print(f"[planning-cycle] Outside active hours ({ACTIVE_HOURS_START}-{ACTIVE_HOURS_END} UTC), skipping")
        return

    if not _planning_cooldown_ok():
        print("[planning-cycle] Cooldown active — skipping (ran recently)")
        return

    print("[planning-cycle] Starting autonomous planning cycle...")
    prompt = _build_planning_prompt()

    claude_bin = os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude")

    try:
        child_env = os.environ.copy()
        child_env.pop("CLAUDECODE", None)
        child_env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        result = subprocess.run(
            [claude_bin, "-p", "--model", HEARTBEAT_MODEL, "--dangerously-skip-permissions", prompt],
            capture_output=True,
            text=True,
            timeout=PLANNING_TIMEOUT,
            cwd=str(BASE),
            env=child_env,
        )
        response = result.stdout.strip()

        if not response:
            stderr_hint = result.stderr.strip()[:200] if result.stderr else ""
            _log_planning(
                f"EMPTY_RESPONSE (exit={result.returncode}{f', stderr={stderr_hint}' if stderr_hint else ''})"
            )
            _update_planning_cooldown("unknown", success=False)
            print("[planning-cycle] Empty response from Claude")
            return

        # Extract plan title from response
        plan_title = "unknown"
        for line in response.splitlines()[:30]:
            if line.startswith("#") and len(line) > 3:
                plan_title = line.lstrip("# ").strip()[:100]
                break

        # Check vault write outcome from subprocess output
        response_lower = response.lower()
        vault_ok = "vault_write" in response_lower and (
            "success" in response_lower or "accepted" in response_lower or "written" in response_lower
        )
        vault_failed = "vault_write" in response_lower and (
            "error" in response_lower
            or "rejected" in response_lower
            or "failed" in response_lower
            or "invalid" in response_lower
        )
        memory_ok = "upsert_memory" in response_lower and ("success" in response_lower or "stored" in response_lower)

        # Log persistence outcomes independently
        if vault_ok:
            _log_planning("VAULT_WRITE: success")
        elif vault_failed:
            _log_planning("VAULT_WRITE: FAILED — check vault audit log")
        else:
            _log_planning("VAULT_WRITE: unknown (not detected in output)")

        if memory_ok:
            _log_planning("FUSION_MEMORY: success")
        else:
            _log_planning("FUSION_MEMORY: unknown (not detected in output)")

        _log_planning(f"COMPLETED: {plan_title}")
        _update_planning_cooldown(plan_title, success=True)
        print(f"[planning-cycle] Completed: {plan_title}")

        # Build status summary for notification
        sinks = []
        if vault_ok:
            sinks.append("vault ✓")
        elif vault_failed:
            sinks.append("vault ✗")
        if memory_ok:
            sinks.append("memory ✓")
        sink_str = f" [{', '.join(sinks)}]" if sinks else ""
        _send_telegram(f"📋 Planning Cycle Complete:\n{plan_title}{sink_str}")

    except subprocess.TimeoutExpired:
        _log_planning(f"TIMEOUT after {PLANNING_TIMEOUT}s")
        _update_planning_cooldown("timeout", success=False)
        print(f"[planning-cycle] Timed out after {PLANNING_TIMEOUT}s")
    except FileNotFoundError:
        _log_planning(f"CLAUDE_NOT_FOUND: {claude_bin}")
        print(f"[planning-cycle] Claude binary not found: {claude_bin}")
    except Exception as e:
        _log_planning(f"ERROR: {e}")
        _update_planning_cooldown("error", success=False)
        print(f"[planning-cycle] Error: {e}")


# --- Main --------------------------------------------------------------------


def main() -> int:
    """Run all health checks, write HEARTBEAT.md, alert if unhealthy."""
    print(f"[heartbeat] Starting health check at {datetime.now(timezone.utc).isoformat()}")

    # Structured tracing for this heartbeat cycle
    hb_ctx = TraceContext.new("heartbeat") if TraceContext is not None else None
    if slog and hb_ctx:
        slog.event("heartbeat.started", hb_ctx)

    # --- Phase 1.2: Kill switch + dead man's switch ---
    try:
        from nova_kill_switch import MODE_RUN, MODE_STOPPED, check_kill_switch, heartbeat_alive

        heartbeat_alive()  # Refresh dead man's switch TTL (45 min)
        ks_mode = check_kill_switch()
        if ks_mode == MODE_STOPPED:
            print(f"[heartbeat] Kill switch ACTIVE (mode={ks_mode}) — skipping all work")
            return 0
        if ks_mode != MODE_RUN:
            print(f"[heartbeat] Kill switch mode={ks_mode} — running health checks only")
    except Exception as e:
        print(f"[heartbeat] Kill switch check failed (non-fatal): {e}")

    checks = []

    for svc in SERVICES:
        checks.append(check_service(svc))

    checks.append(check_disk())
    checks.append(check_claude_binary())
    checks.append(check_task_queue())
    checks.append(check_last_output())
    checks.append(check_stale_workers())
    checks.append(check_state_files())
    checks.append(check_metrics())
    checks.append(check_google_workspace())
    checks.append(check_backup())
    checks.append(check_ruff())
    checks.append(check_memory_systems())
    checks.append(check_log_sizes())
    checks.append(check_state_bloat())
    checks.append(check_pip_audit())

    # --- Drift detection (observability Phase 3) ---
    try:
        from utils.drift_detector import detect_drift

        drift = detect_drift(window_hours=24.0)
        if drift.drift_detected:
            signals_str = "; ".join(s.message for s in drift.signals[:3])
            checks.append(
                {
                    "name": "drift_detection",
                    "ok": False,
                    "detail": f"{len(drift.signals)} signal(s): {signals_str}",
                }
            )
        else:
            checks.append(
                {
                    "name": "drift_detection",
                    "ok": True,
                    "detail": f"no drift ({drift.window_tasks} tasks in window)",
                }
            )
    except Exception as e:
        checks.append({"name": "drift_detection", "ok": True, "detail": f"check skipped: {e}"})

    write_heartbeat(checks)

    all_ok = all(c["ok"] for c in checks)

    # --- Phase 7.6: multi-agent heartbeat ---
    try:
        from agents.observability import Severity, run_multiagent_heartbeat

        ma_report = run_multiagent_heartbeat()
        ma_ok = ma_report.overall == Severity.HEALTHY
        checks.append(
            {
                "name": "multi_agent_health",
                "ok": ma_ok,
                "detail": (
                    f"{ma_report.overall}: "
                    f"{len(ma_report.findings)} finding(s), "
                    f"{ma_report.metrics.active_workflows} active workflow(s)"
                ),
            }
        )
        if not ma_ok:
            all_ok = False
        print(f"[heartbeat] Multi-agent: {ma_report.overall} ({len(ma_report.findings)} findings)")
    except Exception as e:
        print(f"[heartbeat] Multi-agent check failed (non-fatal): {e}")
        checks.append(
            {
                "name": "multi_agent_health",
                "ok": True,
                "detail": f"check skipped: {e}",
            }
        )

    # --- Phase 7.7: production hardening maintenance ---
    try:
        from agents.production_hardening import run_production_hardening

        ph_result = run_production_hardening()
        cleanup = ph_result.get("cleanup", {})
        if isinstance(cleanup, dict):
            archived = len(cleanup.get("archived_workflows", [])) + len(cleanup.get("archived_agents", []))
            cleaned = len(cleanup.get("cleaned_leases", [])) + len(cleanup.get("cleaned_tmp", []))
            if archived or cleaned:
                print(f"[heartbeat] Hardening: archived={archived} cleaned={cleaned}")
    except Exception as e:
        print(f"[heartbeat] Production hardening failed (non-fatal): {e}")

    # Always send heartbeat pulse to Telegram
    send_telegram_heartbeat(checks)

    fail_names = [c["name"] for c in checks if not c["ok"]]
    if all_ok:
        print("[heartbeat] All checks passed. HEALTHY.")
        if slog and hb_ctx:
            slog.event("heartbeat.healthy", hb_ctx, checks=len(checks), duration_ms=hb_ctx.elapsed_ms())
    else:
        print("[heartbeat] Some checks FAILED. Alerting...")
        inject_repair_task(checks)
        if slog and hb_ctx:
            slog.event(
                "heartbeat.unhealthy",
                hb_ctx,
                level="warn",
                checks=len(checks),
                failed=fail_names,
                duration_ms=hb_ctx.elapsed_ms(),
            )

    # --- Phase 3: Automatic memory trigger for heartbeat cycle ---
    try:
        from agents.memory_triggers import trigger_engine

        hb_summary = f"Heartbeat: {len(checks)} checks, {len(fail_names)} failed"
        if fail_names:
            hb_summary += f" ({', '.join(fail_names[:5])})"
        trigger_engine.fire(
            trigger_class="session_boundary",
            event_type="heartbeat_cycle",
            source="heartbeat",
            title=f"heartbeat_cycle: {'HEALTHY' if all_ok else 'UNHEALTHY'}"[:100],
            summary=hb_summary[:500],
            caller="heartbeat.main",
            ctx=hb_ctx,
            confidence="high" if all_ok else "medium",
            tags=["#heartbeat"] + ([f"#failed/{f}" for f in fail_names[:3]] if fail_names else []),
        )
    except Exception as exc:
        print(f"[heartbeat] Memory trigger failed (non-fatal): {exc}")

    # --- LLM-driven proactive heartbeat ---
    try:
        _run_heartbeat_agent(checks)
    except Exception as e:
        print(f"[heartbeat-agent] Failed (non-fatal): {e}")

    # --- Memory maintenance scheduler (Phase 8) ---
    try:
        _run_memory_maintenance()
    except Exception as e:
        print(f"[memory-maintenance] Failed (non-fatal): {e}")

    # --- Autonomous cycles: research (hourly) + planning (every 3 hours) ---
    # Both run independently when their cooldowns are met.
    try:
        _run_research_cycle()
    except Exception as e:
        print(f"[research-cycle] Failed (non-fatal): {e}")

    try:
        _run_planning_cycle()
    except Exception as e:
        print(f"[planning-cycle] Failed (non-fatal): {e}")

    # Append to heartbeat log
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fail_count = len([c for c in checks if not c["ok"]])
    log_line = (
        f"{datetime.now(timezone.utc).isoformat()} | {'HEALTHY' if all_ok else 'UNHEALTHY'} | {fail_count} failures\n"
    )
    with open(LOGS_DIR / "heartbeat.log", "a") as f:
        f.write(log_line)

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
