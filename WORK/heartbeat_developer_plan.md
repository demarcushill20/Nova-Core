# NovaCore Heartbeat — Detailed Developer Plan

**Author:** NovaCore Executive Agent
**Date:** 2026-03-05
**Task:** 0012
**Status:** Implementation-ready

---

## Executive Summary

This plan describes how to build a self-monitoring heartbeat system for NovaCore. The heartbeat runs every 30 minutes via a systemd timer, checks service health / disk / task queue / worker liveness, writes a `HEARTBEAT.md` status file, and sends a Telegram alert if anything is unhealthy. Total: **4 files to create**, **0 files to modify**.

---

## Architecture Overview

```
novacore-heartbeat.timer (systemd, every 30 min)
        │
        ▼
novacore-heartbeat.service (systemd oneshot)
        │
        ▼
heartbeat.py  (Python script, ~200 lines)
        │
        ├─► Check 7 health dimensions
        ├─► Write HEARTBEAT.md (overwrite each run)
        ├─► If unhealthy: POST to Telegram Bot API
        └─► If critical: inject synthetic repair task into TASKS/
```

**Key design decision:** The heartbeat is a *standalone script* that runs independently of the watcher. It does NOT modify `watcher.py`, `telegram_bot.py`, or `telegram_notifier.py`. This means:
- Zero risk of breaking existing dispatch flow
- Can be deployed/rolled back independently
- Crash in heartbeat never affects task processing

---

## Step-by-Step Implementation

### Step 1: Create `heartbeat.py` — The Health Check Script

**File:** `/home/nova/nova-core/heartbeat.py`
**Estimated lines:** ~220
**Dependencies:** stdlib only (`subprocess`, `pathlib`, `json`, `datetime`, `os`, `urllib.request`)
**No pip installs needed.**

#### 1.1 Constants and Configuration

```python
#!/usr/bin/env python3
"""NovaCore heartbeat — proactive health monitoring."""

from pathlib import Path
from datetime import datetime, timezone, timedelta
import subprocess, json, os, urllib.request, urllib.parse

BASE = Path("/home/nova/nova-core")
HEARTBEAT_FILE = BASE / "HEARTBEAT.md"
STATE_DIR = BASE / "STATE"
TASKS_DIR = BASE / "TASKS"
OUTPUT_DIR = BASE / "OUTPUT"
LOGS_DIR = BASE / "LOGS"

# Thresholds
DISK_WARN_PERCENT = 85          # warn if disk usage exceeds this
STALE_OUTPUT_MINUTES = 120      # flag if no OUTPUT in last 2 hours (only during active periods)
ORPHAN_INPROGRESS_MINUTES = 15  # flag .inprogress files older than this
MAX_PENDING_TASKS = 10          # warn if queue is too deep

SERVICES = [
    "novacore-watcher",
    "novacore-telegram",
    "novacore-telegram-notifier",
]
```

**Design notes:**
- System Python (`/usr/bin/python3`) — no venv dependency, matches watcher.py pattern
- All thresholds are constants at module top for easy tuning
- `urllib.request` for Telegram alerts — avoids `httpx`/`requests` dependency

#### 1.2 Health Check Functions

Each check returns a `dict` with `{name, ok: bool, detail: str}`.

```python
def check_service(name: str) -> dict:
    """Check if a systemd service is active."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=10,
        )
        active = result.stdout.strip() == "active"
        # Get PID and uptime for detail
        if active:
            info = subprocess.run(
                ["systemctl", "show", name, "--property=MainPID,ActiveEnterTimestamp"],
                capture_output=True, text=True, timeout=10,
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
```

```python
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
```

```python
def check_task_queue() -> dict:
    """Check for pending tasks and orphaned .inprogress files."""
    pending = list(TASKS_DIR.glob("*.md"))
    # Filter out lifecycle suffixes
    pending = [p for p in pending if not any(p.name.endswith(s) for s in
               (".inprogress", ".done", ".failed", ".cancelled"))]

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
```

```python
def check_last_output() -> dict:
    """Check recency of last OUTPUT file (informational, not critical)."""
    outputs = sorted(OUTPUT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not outputs:
        return {"name": "last_output", "ok": True, "detail": "no outputs yet"}
    latest = outputs[0]
    age_min = (datetime.now(timezone.utc) -
               datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)).total_seconds() / 60
    detail = f"{latest.name} ({round(age_min)}min ago)"
    # This is informational — don't fail on stale output
    return {"name": "last_output", "ok": True, "detail": detail}
```

```python
def check_claude_binary() -> dict:
    """Check that the Claude CLI binary is accessible."""
    claude_path = Path("/usr/bin/claude")
    ok = claude_path.exists() and os.access(str(claude_path), os.X_OK)
    detail = "accessible" if ok else "NOT FOUND or not executable"
    return {"name": "claude_binary", "ok": ok, "detail": detail}
```

```python
def check_stale_workers() -> dict:
    """Check for PID files in STATE/running/ that point to dead processes."""
    running_dir = STATE_DIR / "running"
    if not running_dir.exists():
        return {"name": "stale_workers", "ok": True, "detail": "no running dir"}
    stale = []
    for pid_file in running_dir.glob("*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # signal 0 = existence check
        except ProcessLookupError:
            stale.append(f"{pid_file.stem} (pid {pid} dead)")
        except (ValueError, PermissionError):
            pass  # can't check, skip
    ok = len(stale) == 0
    detail = f"{len(stale)} stale" if stale else "all clean"
    if stale:
        detail += f": {', '.join(stale)}"
    return {"name": "stale_workers", "ok": ok, "detail": detail}
```

```python
def check_metrics() -> dict:
    """Check STATE/metrics.json for anomalous failure rates."""
    metrics_file = STATE_DIR / "metrics.json"
    if not metrics_file.exists():
        return {"name": "metrics", "ok": True, "detail": "no metrics file yet"}
    try:
        m = json.loads(metrics_file.read_text())
        failures = m.get("contract_failure", 0)
        successes = m.get("contract_success", 0)
        total = failures + successes
        if total == 0:
            return {"name": "metrics", "ok": True, "detail": "no executions recorded"}
        fail_rate = round(failures / total * 100, 1)
        ok = fail_rate < 50  # warn if more than half are failing
        return {"name": "metrics", "ok": ok, "detail": f"{fail_rate}% failure rate ({failures}/{total})"}
    except Exception as e:
        return {"name": "metrics", "ok": False, "detail": f"parse error: {e}"}
```

#### 1.3 HEARTBEAT.md Writer

```python
def write_heartbeat(checks: list[dict]) -> None:
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
```

**Design note:** The file is overwritten on every run (not appended). This keeps it small and always reflects current state. Historical data lives in logs.

#### 1.4 Telegram Alert (Unhealthy Only)

```python
def send_telegram_alert(checks: list[dict]) -> None:
    """Send Telegram message listing failed checks. Only called when unhealthy."""
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("WARN: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set, skipping alert")
        return

    failed = [c for c in checks if not c["ok"]]
    lines = ["⚠️ NovaCore Heartbeat — UNHEALTHY", ""]
    for c in failed:
        lines.append(f"❌ {c['name']}: {c['detail']}")

    text = "\n".join(lines)
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"WARN: Telegram alert failed: {e}")
```

**Design notes:**
- Uses `urllib.request` (stdlib) — no external dependencies
- Reads `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` from env (set via systemd `EnvironmentFile`)
- Only sends on failure — healthy beats are silent (matches OpenClaw's `HEARTBEAT_OK` pattern)
- Alerts are concise — only failed checks listed

#### 1.5 Optional: Synthetic Repair Task Injection

```python
def inject_repair_task(checks: list[dict]) -> None:
    """For critical failures, inject a self-repair task into TASKS/."""
    failed_services = [c for c in checks if c["name"].startswith("service:") and not c["ok"]]
    if not failed_services:
        return  # only inject for service failures

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"hb_{ts}_self_repair"
    task_path = TASKS_DIR / f"{stem}.md"

    names = ", ".join(c["name"].replace("service:", "") for c in failed_services)
    content = f"""# Heartbeat Self-Repair Task

The following services were detected as unhealthy by the heartbeat:
{names}

## Instructions
1. Check `journalctl -u <service> -n 50` for each failed service.
2. Attempt `sudo systemctl restart <service>` for each.
3. Verify the restart succeeded with `systemctl is-active <service>`.
4. Write results to OUTPUT.
"""
    task_path.write_text(content)
    print(f"Injected repair task: {task_path}")
```

**Design notes:**
- Only injected for service-level failures (not disk warnings or stale outputs)
- Task stem starts with `hb_` — clearly distinguishable from user tasks
- The watcher picks it up automatically on next poll cycle
- Rate-limiting: check if a recent `hb_*_self_repair` task already exists before injecting

#### 1.6 Main Entry Point

```python
def main():
    """Run all health checks, write HEARTBEAT.md, alert if unhealthy."""
    print(f"[heartbeat] Starting health check at {datetime.now(timezone.utc).isoformat()}")

    checks = []

    # Service checks
    for svc in SERVICES:
        checks.append(check_service(svc))

    # System checks
    checks.append(check_disk())
    checks.append(check_claude_binary())

    # Task/worker checks
    checks.append(check_task_queue())
    checks.append(check_last_output())
    checks.append(check_stale_workers())
    checks.append(check_metrics())

    # Write status file
    write_heartbeat(checks)

    all_ok = all(c["ok"] for c in checks)

    if all_ok:
        print("[heartbeat] All checks passed. HEALTHY.")
    else:
        print("[heartbeat] Some checks FAILED. Alerting...")
        send_telegram_alert(checks)
        inject_repair_task(checks)

    # Log to heartbeat log
    log_line = f"{datetime.now(timezone.utc).isoformat()} | {'HEALTHY' if all_ok else 'UNHEALTHY'} | {len([c for c in checks if not c['ok']])} failures\n"
    log_path = LOGS_DIR / "heartbeat.log"
    with open(log_path, "a") as f:
        f.write(log_line)

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

**Exit codes:**
- `0` = healthy (systemd logs `success`)
- `1` = unhealthy (systemd logs `failure` — visible in `systemctl status`)

---

### Step 2: Create systemd Timer Unit

**File:** `/etc/systemd/system/novacore-heartbeat.timer`

```ini
[Unit]
Description=NovaCore Heartbeat Timer
After=network-online.target

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
AccuracySec=1min

[Install]
WantedBy=timers.target
```

**Design notes:**
- `OnBootSec=2min` — first beat 2 minutes after boot (gives services time to start)
- `OnUnitActiveSec=30min` — then every 30 minutes (matches OpenClaw cadence)
- `AccuracySec=1min` — allow ±1min jitter for power efficiency
- No `Persistent=true` — missed beats during downtime don't need catching up

---

### Step 3: Create systemd Service Unit

**File:** `/etc/systemd/system/novacore-heartbeat.service`

```ini
[Unit]
Description=NovaCore Heartbeat Health Check
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /home/nova/nova-core/heartbeat.py
WorkingDirectory=/home/nova/nova-core
User=nova
EnvironmentFile=/etc/novacore/telegram.env
TimeoutStartSec=60

[Install]
WantedBy=multi-user.target
```

**Design notes:**
- `Type=oneshot` — runs once per timer trigger, exits. NOT a long-running daemon.
- `User=nova` — matches existing service pattern
- `EnvironmentFile=/etc/novacore/telegram.env` — reuses existing Telegram credentials
- `TimeoutStartSec=60` — kill if heartbeat hangs (all checks should complete in <10s)

---

### Step 4: Deploy and Enable

```bash
# 1. Copy heartbeat.py to nova-core (already there from Step 1)
chmod +x /home/nova/nova-core/heartbeat.py

# 2. Install systemd units (requires sudo)
sudo cp /path/to/novacore-heartbeat.service /etc/systemd/system/
sudo cp /path/to/novacore-heartbeat.timer /etc/systemd/system/
sudo systemctl daemon-reload

# 3. Enable and start
sudo systemctl enable novacore-heartbeat.timer
sudo systemctl start novacore-heartbeat.timer

# 4. Verify timer is active
systemctl list-timers novacore-heartbeat.timer

# 5. Manual test run
sudo systemctl start novacore-heartbeat.service
cat /home/nova/nova-core/HEARTBEAT.md
```

---

### Step 5: Verify

**Immediate verification checklist:**

| Check | Command | Expected |
|-------|---------|----------|
| Timer active | `systemctl is-active novacore-heartbeat.timer` | `active` |
| Manual run succeeds | `python3 heartbeat.py` (as nova) | Exit 0, HEARTBEAT.md written |
| HEARTBEAT.md exists | `cat HEARTBEAT.md` | Timestamped checklist |
| Log entry written | `tail -1 LOGS/heartbeat.log` | `HEALTHY` or `UNHEALTHY` line |
| Alert on failure | `sudo systemctl stop novacore-watcher && python3 heartbeat.py` | Telegram alert received |
| No alert on health | (all services running) `python3 heartbeat.py` | No Telegram message |
| Repair task injected | (stop a service, run heartbeat) `ls TASKS/hb_*` | Repair task file |

**30-minute soak test:**
- Let the timer fire at least twice
- Check `journalctl -u novacore-heartbeat.service` for both runs
- Check `LOGS/heartbeat.log` for two entries
- Verify no spurious Telegram alerts

---

## File Manifest

| File | Location | Action | Lines |
|------|----------|--------|-------|
| `heartbeat.py` | `/home/nova/nova-core/heartbeat.py` | **Create** | ~220 |
| `HEARTBEAT.md` | `/home/nova/nova-core/HEARTBEAT.md` | **Auto-generated** | ~15 |
| `novacore-heartbeat.timer` | `/etc/systemd/system/` | **Create** | 10 |
| `novacore-heartbeat.service` | `/etc/systemd/system/` | **Create** | 12 |

**Existing files modified: NONE.**

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Heartbeat false-positive alerts | Medium | Low (annoyance) | Conservative thresholds; only alert on service failures, not stale outputs |
| `inject_repair_task` flood | Low | Medium | Rate-limit: skip injection if `hb_*_self_repair.inprogress` already exists |
| Telegram env vars missing | Low | Low | Graceful fallback: print warning, skip alert, still write HEARTBEAT.md |
| Heartbeat itself crashes | Low | Low | `Type=oneshot` + timer retry on next cycle; no persistent state to corrupt |
| Disk full prevents HEARTBEAT.md write | Very Low | Low | Heartbeat detects disk pressure before it's 100% full |

---

## Future Enhancements (Not In Scope)

1. **Heartbeat history:** Append to `LOGS/heartbeat_history.jsonl` (structured, for trend analysis)
2. **Telegram /heartbeat command:** Add to `telegram_bot.py` to force an on-demand heartbeat
3. **Memory compaction trigger:** Heartbeat detects MEMORY/ exceeding size threshold → injects compaction task
4. **API quota tracking:** Add check for Anthropic API usage (requires key introspection endpoint)
5. **Dashboard endpoint:** Simple HTTP server exposing HEARTBEAT.md as JSON for external monitoring

---

## Implementation Order (Recommended)

```
Session 1 (30-60 min):
  ├─ Write heartbeat.py
  ├─ Manual test: python3 heartbeat.py
  ├─ Verify HEARTBEAT.md output
  └─ Verify Telegram alert (stop a service, run, restart)

Session 2 (15-30 min):
  ├─ Create systemd units
  ├─ Enable timer
  ├─ Soak test (let timer fire 2-3 times)
  └─ Add rate-limiting to inject_repair_task

Optional Session 3:
  └─ Add /heartbeat telegram command
```

**Total estimated effort:** ~1.5 hours of implementation + testing.
