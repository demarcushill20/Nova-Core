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

    # Always send heartbeat pulse to Telegram
    send_telegram_heartbeat(checks)

    if all_ok:
        print("[heartbeat] All checks passed. HEALTHY.")
    else:
        print("[heartbeat] Some checks FAILED. Alerting...")
        inject_repair_task(checks)

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
