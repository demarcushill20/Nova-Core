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
            capture_output=True, text=True, timeout=10,
        )
        active = result.stdout.strip() == "active"
        if active:
            info = subprocess.run(
                ["systemctl", "show", name,
                 "--property=MainPID,ActiveEnterTimestamp"],
                capture_output=True, text=True, timeout=10,
            )
            props = dict(
                line.split("=", 1)
                for line in info.stdout.strip().splitlines()
                if "=" in line
            )
            pid = props.get("MainPID", "?")
            since = props.get("ActiveEnterTimestamp", "?")
            detail = f"active (pid {pid}, since {since})"
        else:
            detail = f"NOT ACTIVE ({result.stdout.strip()})"
        return {"name": f"service:{name}", "ok": active, "detail": detail}
    except Exception as e:
        return {"name": f"service:{name}", "ok": False,
                "detail": f"check failed: {e}"}


def check_disk() -> dict:
    """Check disk usage on the partition containing ~/nova-core."""
    try:
        st = os.statvfs(str(BASE))
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used_pct = round((1 - free / total) * 100, 1)
        free_gb = round(free / (1024**3), 1)
        ok = used_pct < DISK_WARN_PERCENT
        return {"name": "disk", "ok": ok,
                "detail": f"{used_pct}% used ({free_gb}GB free)"}
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
    pending = [
        p for p in TASKS_DIR.glob("*.md")
        if not any(p.name.endswith(s) for s in lifecycle_suffixes)
    ]

    inprogress = list(TASKS_DIR.glob("*.inprogress"))
    now = datetime.now(timezone.utc)
    orphaned = []
    for ip in inprogress:
        age_min = (
            now - datetime.fromtimestamp(ip.stat().st_mtime, tz=timezone.utc)
        ).total_seconds() / 60
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
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not outputs:
        return {"name": "last_output", "ok": True, "detail": "no outputs yet"}

    latest = outputs[0]
    age_min = (
        datetime.now(timezone.utc)
        - datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
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
            return {"name": "metrics", "ok": True,
                    "detail": "no executions recorded"}
        fail_rate = round(failures / total * 100, 1)
        ok = fail_rate < 50
        return {"name": "metrics", "ok": ok,
                "detail": f"{fail_rate}% failure rate ({failures}/{total})"}
    except Exception as e:
        return {"name": "metrics", "ok": False,
                "detail": f"parse error: {e}"}


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
    failed_services = [
        c for c in checks
        if c["name"].startswith("service:") and not c["ok"]
    ]
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

    names = ", ".join(
        c["name"].replace("service:", "") for c in failed_services
    )
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
ACTIVE_HOURS_START = int(os.environ.get("HEARTBEAT_ACTIVE_START", "6"))
ACTIVE_HOURS_END = int(os.environ.get("HEARTBEAT_ACTIVE_END", "23"))

# Model for heartbeat reasoning — Haiku for cost efficiency (~$0.01/cycle)
HEARTBEAT_MODEL = os.environ.get("HEARTBEAT_MODEL", "claude-haiku-4-5-20251001")
HEARTBEAT_TIMEOUT = 90  # seconds

CHECKLIST_FILE = BASE / "HEARTBEAT_CHECKLIST.md"
HEARTBEAT_AGENT_LOG = LOGS_DIR / "heartbeat_agent.log"


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
        p for p in TASKS_DIR.glob("*.md")
        if not any(p.name.endswith(s)
                   for s in (".inprogress", ".done", ".failed", ".cancelled"))
    ]
    if pending:
        parts.append(f"\n## Pending Tasks ({len(pending)})")
        now = datetime.now(timezone.utc)
        for p in sorted(pending, key=lambda x: x.stat().st_mtime):
            age_min = (now - datetime.fromtimestamp(
                p.stat().st_mtime, tz=timezone.utc)).total_seconds() / 60
            parts.append(f"  - {p.stem} ({round(age_min)}min old)")

    # Recent failed tasks (last 2 hours)
    failed = list(TASKS_DIR.glob("*.failed"))
    now = datetime.now(timezone.utc)
    recent_failed = []
    for f in failed:
        age_hr = (now - datetime.fromtimestamp(
            f.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
        if age_hr < 2:
            recent_failed.append(f)
    if recent_failed:
        parts.append(f"\n## Recently Failed Tasks ({len(recent_failed)})")
        for f in recent_failed:
            parts.append(f"  - {f.stem}")

    # Recent outputs (last 4 hours)
    outputs = sorted(
        OUTPUT_DIR.glob("*.md"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    recent_outputs = []
    for o in outputs[:10]:
        age_hr = (now - datetime.fromtimestamp(
            o.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
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
            goals = json.loads(goals_file.read_text())
            active = [g for g in goals if g.get("status") != "done"]
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
                parts.append(f"\n## Last Agent Action")
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
        print(f"[heartbeat-agent] Outside active hours "
              f"({ACTIVE_HOURS_START}-{ACTIVE_HOURS_END} UTC), skipping")
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
        result = subprocess.run(
            [claude_bin, "-p", "--model", HEARTBEAT_MODEL,
             "--dangerously-skip-permissions", prompt],
            capture_output=True, text=True, timeout=HEARTBEAT_TIMEOUT,
            cwd=str(BASE),
        )
        response = result.stdout.strip()

        if not response:
            _log_agent("EMPTY_RESPONSE — agent returned nothing")
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
    json_match = re.search(r'\[[\s\S]*?\]', text)
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
    # Rate-limit: max 2 proactive tasks per heartbeat cycle
    existing = list(TASKS_DIR.glob("hb_proactive_*.md"))
    recent = [p for p in existing
              if not any(p.name.endswith(s)
                         for s in (".done", ".failed", ".cancelled"))]
    if len(recent) >= 2:
        print("[heartbeat-agent] Rate limit: 2 proactive tasks already pending")
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


# --- Main --------------------------------------------------------------------


def main() -> int:
    """Run all health checks, write HEARTBEAT.md, alert if unhealthy."""
    print(f"[heartbeat] Starting health check at "
          f"{datetime.now(timezone.utc).isoformat()}")

    checks = []

    for svc in SERVICES:
        checks.append(check_service(svc))

    checks.append(check_disk())
    checks.append(check_claude_binary())
    checks.append(check_task_queue())
    checks.append(check_last_output())
    checks.append(check_stale_workers())
    checks.append(check_metrics())

    write_heartbeat(checks)

    all_ok = all(c["ok"] for c in checks)

    # --- Phase 7.6: multi-agent heartbeat ---
    try:
        from agents.observability import run_multiagent_heartbeat, Severity
        ma_report = run_multiagent_heartbeat()
        ma_ok = ma_report.overall == Severity.HEALTHY
        checks.append({
            "name": "multi_agent_health",
            "ok": ma_ok,
            "detail": (f"{ma_report.overall}: "
                       f"{len(ma_report.findings)} finding(s), "
                       f"{ma_report.metrics.active_workflows} active workflow(s)"),
        })
        if not ma_ok:
            all_ok = False
        print(f"[heartbeat] Multi-agent: {ma_report.overall} "
              f"({len(ma_report.findings)} findings)")
    except Exception as e:
        print(f"[heartbeat] Multi-agent check failed (non-fatal): {e}")
        checks.append({
            "name": "multi_agent_health",
            "ok": True,
            "detail": f"check skipped: {e}",
        })

    # --- Phase 7.7: production hardening maintenance ---
    try:
        from agents.production_hardening import run_production_hardening
        ph_result = run_production_hardening()
        cleanup = ph_result.get("cleanup", {})
        if isinstance(cleanup, dict):
            archived = (len(cleanup.get("archived_workflows", []))
                        + len(cleanup.get("archived_agents", [])))
            cleaned = (len(cleanup.get("cleaned_leases", []))
                       + len(cleanup.get("cleaned_tmp", [])))
            if archived or cleaned:
                print(f"[heartbeat] Hardening: archived={archived} "
                      f"cleaned={cleaned}")
    except Exception as e:
        print(f"[heartbeat] Production hardening failed (non-fatal): {e}")

    # Always send heartbeat pulse to Telegram
    send_telegram_heartbeat(checks)

    if all_ok:
        print("[heartbeat] All checks passed. HEALTHY.")
    else:
        print("[heartbeat] Some checks FAILED. Alerting...")
        inject_repair_task(checks)

    # --- LLM-driven proactive heartbeat ---
    try:
        _run_heartbeat_agent(checks)
    except Exception as e:
        print(f"[heartbeat-agent] Failed (non-fatal): {e}")

    # Append to heartbeat log
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fail_count = len([c for c in checks if not c["ok"]])
    log_line = (
        f"{datetime.now(timezone.utc).isoformat()} | "
        f"{'HEALTHY' if all_ok else 'UNHEALTHY'} | "
        f"{fail_count} failures\n"
    )
    with open(LOGS_DIR / "heartbeat.log", "a") as f:
        f.write(log_line)

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
