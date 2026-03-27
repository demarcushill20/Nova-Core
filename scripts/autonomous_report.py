#!/usr/bin/env python3
"""NovaCore Autonomous Report — 2-hour system status digest.

Runs as a systemd oneshot (triggered by novacore-report.timer every 2h).
Collects system health, autonomy scores, recent task activity, and
heartbeat trends, then writes a diary note to NovaVault 90-diary/.

Stdlib + existing novacore modules only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE = Path("/home/nova/nova-core")
STATE_DIR = BASE / "STATE"
TASKS_DIR = BASE / "TASKS"
OUTPUT_DIR = BASE / "OUTPUT"
LOGS_DIR = BASE / "LOGS"
HEARTBEAT_FILE = BASE / "HEARTBEAT.md"
METRICS_JSONL = STATE_DIR / "heartbeat_metrics.jsonl"
AUTONOMY_SNAPSHOT = STATE_DIR / "autonomy_snapshot.json"
DECISION_HISTORY = STATE_DIR / "decision_history.json"

VAULT_DIR = Path("/home/nova/nova-vault")
VAULT_DIARY = VAULT_DIR / "90-diary"

SERVICES = [
    "novacore-watcher",
    "novacore-telegram",
    "novacore-telegram-notifier",
]


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [report] {msg}", flush=True)


# ── Data Collection ────────────────────────────────────────────────────────────


def collect_service_status() -> list[dict]:
    """Check systemd service statuses."""
    results = []
    for svc in SERVICES:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True,
                text=True,
                timeout=10,
            )
            active = r.stdout.strip() == "active"
            if active:
                info = subprocess.run(
                    ["systemctl", "show", svc, "--property=MainPID,ActiveEnterTimestamp"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                props = dict(line.split("=", 1) for line in info.stdout.strip().splitlines() if "=" in line)
                uptime_str = props.get("ActiveEnterTimestamp", "?")
                results.append({"name": svc, "ok": True, "detail": f"active (since {uptime_str})"})
            else:
                results.append({"name": svc, "ok": False, "detail": r.stdout.strip()})
        except Exception as e:
            results.append({"name": svc, "ok": False, "detail": str(e)})
    return results


def collect_disk_usage() -> dict:
    """Get disk usage percentage."""
    try:
        st = os.statvfs(str(BASE))
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used_pct = round((1 - free / total) * 100, 1)
        free_gb = round(free / (1024**3), 1)
        return {"used_pct": used_pct, "free_gb": free_gb}
    except Exception:
        return {"used_pct": -1, "free_gb": -1}


def collect_autonomy_scores() -> dict | None:
    """Read the latest autonomy snapshot from STATE/."""
    try:
        if AUTONOMY_SNAPSHOT.exists():
            data = json.loads(AUTONOMY_SNAPSHOT.read_text())
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def collect_recent_tasks(hours: int = 2) -> dict:
    """Count tasks by state and list recently completed ones."""
    now = time.time()
    cutoff = now - (hours * 3600)

    pending = []
    in_progress = []
    recently_done = []
    recently_failed = []

    try:
        for f in TASKS_DIR.iterdir():
            if not f.is_file():
                continue
            name = f.name
            mtime = f.stat().st_mtime

            if name.endswith(".done"):
                if mtime >= cutoff:
                    stem = name.replace(".done", "").replace(".md", "")
                    recently_done.append(stem)
            elif name.endswith(".failed"):
                if mtime >= cutoff:
                    stem = name.replace(".failed", "").replace(".md", "")
                    recently_failed.append(stem)
            elif name.endswith(".inprogress"):
                stem = name.replace(".inprogress", "").replace(".md", "")
                in_progress.append(stem)
            elif name.endswith(".md"):
                pending.append(name.replace(".md", ""))
    except OSError:
        pass

    return {
        "pending": len(pending),
        "in_progress": len(in_progress),
        "recently_done": recently_done,
        "recently_failed": recently_failed,
    }


def collect_recent_outputs(hours: int = 2) -> list[str]:
    """List output files created in the last N hours."""
    now = time.time()
    cutoff = now - (hours * 3600)
    results = []
    try:
        for f in sorted(OUTPUT_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file() and f.stat().st_mtime >= cutoff:
                results.append(f.name)
    except OSError:
        pass
    return results[:20]  # cap at 20


def collect_heartbeat_trends(window: int = 12) -> dict:
    """Analyze last N heartbeat snapshots for availability and hotspots."""
    if not METRICS_JSONL.exists():
        return {"availability_pct": 100.0, "snapshots": 0, "hotspots": [], "regressions": []}
    try:
        lines = METRICS_JSONL.read_text().splitlines()
        entries = []
        for ln in lines[-window:]:
            if ln.strip():
                entries.append(json.loads(ln))
    except (json.JSONDecodeError, OSError):
        return {"availability_pct": 100.0, "snapshots": 0, "hotspots": [], "regressions": []}

    if not entries:
        return {"availability_pct": 100.0, "snapshots": 0, "hotspots": [], "regressions": []}

    healthy = sum(1 for e in entries if e.get("status") == "healthy")
    avail = round(healthy / len(entries) * 100, 1)

    fail_counts: dict[str, int] = {}
    for e in entries:
        for name in e.get("failed_names", []):
            fail_counts[name] = fail_counts.get(name, 0) + 1
    hotspots = sorted(fail_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # Regressions: failed in last 2, passed in prior entries
    regressions: list[str] = []
    if len(entries) >= 3:
        recent_fails: set[str] = set()
        for e in entries[-2:]:
            recent_fails.update(e.get("failed_names", []))
        prior_fails: set[str] = set()
        for e in entries[:-2]:
            prior_fails.update(e.get("failed_names", []))
        regressions = sorted(recent_fails - prior_fails)

    return {
        "availability_pct": avail,
        "snapshots": len(entries),
        "hotspots": hotspots,
        "regressions": regressions,
    }


def collect_decision_history() -> list[dict]:
    """Read recent autonomy decisions."""
    try:
        if DECISION_HISTORY.exists():
            data = json.loads(DECISION_HISTORY.read_text())
            if isinstance(data, list):
                return data[-5:]
    except (json.JSONDecodeError, OSError):
        pass
    return []


# ── Report Formatting ──────────────────────────────────────────────────────────


def _alert_emoji(level: str) -> str:
    """Map alert level to status indicator."""
    return {"GREEN": "GREEN", "YELLOW": "YELLOW", "RED": "RED"}.get(level.upper(), level)


def _trend_arrow(direction: str) -> str:
    return {"improving": "^", "degrading": "v", "stable": "="}.get(direction, "?")


def format_report() -> str:  # noqa: C901
    """Collect all data and format the autonomous report."""
    now_utc = datetime.now(timezone.utc)
    now_ct = now_utc - timedelta(hours=5)  # CDT = UTC-5
    date_str = now_utc.strftime("%Y-%m-%d")
    time_str = now_ct.strftime("%I:%M %p CT")

    # Collect data
    services = collect_service_status()
    disk = collect_disk_usage()
    autonomy = collect_autonomy_scores()
    tasks = collect_recent_tasks(hours=2)
    outputs = collect_recent_outputs(hours=2)
    trends = collect_heartbeat_trends(window=12)  # ~6h of 30-min heartbeats
    decisions = collect_decision_history()

    # Build report
    lines: list[str] = []

    # Header
    lines.append(f"## NovaCore Autonomous Report — {date_str} {time_str}")
    lines.append("")

    # Overall status
    all_services_ok = all(s["ok"] for s in services)
    overall_health = "HEALTHY" if all_services_ok else "DEGRADED"
    lines.append(f"**System Status:** {overall_health}")
    if autonomy:
        score = autonomy.get("overall_score", 0)
        alert = autonomy.get("alert_level", "?")
        lines.append(f"**Autonomy Score:** {score}/100 ({alert})")
    lines.append(f"**Heartbeat Availability (6h):** {trends['availability_pct']}% ({trends['snapshots']} checks)")
    lines.append(f"**Disk:** {disk['used_pct']}% used ({disk['free_gb']} GB free)")
    lines.append("")

    # Services
    lines.append("### Services")
    lines.append("")
    for s in services:
        mark = "[OK]" if s["ok"] else "[FAIL]"
        lines.append(f"- {mark} **{s['name']}**: {s['detail']}")
    lines.append("")

    # Autonomy dimensions
    if autonomy and autonomy.get("dimensions"):
        lines.append("### Autonomy Dimensions")
        lines.append("")
        lines.append("| Dimension | Score | Status | Trend |")
        lines.append("|-----------|-------|--------|-------|")
        for dim_name, dim_data in autonomy["dimensions"].items():
            score = dim_data.get("score", 0)
            trend = dim_data.get("trend", "stable")
            level = "GREEN" if score >= 70 else ("YELLOW" if score >= 40 else "RED")
            arrow = _trend_arrow(trend)
            pretty_name = dim_name.replace("_", " ").title()
            lines.append(f"| {pretty_name} | {score:.0f} | {level} | {arrow} {trend} |")
        lines.append("")

        # Decision mode
        dec = autonomy.get("decision", {})
        if dec:
            lines.append(f"**Decision Mode:** {dec.get('mode', '?').upper()} — {dec.get('reason', '')[:120]}")
            lines.append("")

        # Goal tree
        goal = autonomy.get("goal_tree", {})
        if goal:
            lines.append(f"**Goal Tree:** {goal.get('completed', 0)}/{goal.get('total', 0)} sub-goals complete")
            lines.append("")

    # Task activity
    lines.append("### Task Activity (last 2h)")
    lines.append("")
    lines.append(f"- **Pending:** {tasks['pending']}")
    lines.append(f"- **In Progress:** {tasks['in_progress']}")
    lines.append(f"- **Completed:** {len(tasks['recently_done'])}")
    lines.append(f"- **Failed:** {len(tasks['recently_failed'])}")

    if tasks["recently_done"]:
        lines.append("")
        lines.append("**Completed tasks:**")
        for t in tasks["recently_done"][:10]:
            # Clean up task name for readability
            clean = re.sub(r"^\d+_", "", t).replace("_", " ")
            lines.append(f"- {clean}")

    if tasks["recently_failed"]:
        lines.append("")
        lines.append("**Failed tasks:**")
        for t in tasks["recently_failed"][:5]:
            clean = re.sub(r"^\d+_", "", t).replace("_", " ")
            lines.append(f"- {clean}")
    lines.append("")

    # Recent outputs
    if outputs:
        lines.append("### Recent Outputs")
        lines.append("")
        for o in outputs[:10]:
            lines.append(f"- `{o}`")
        lines.append("")

    # Heartbeat trends
    if trends["hotspots"]:
        lines.append("### Health Hotspots")
        lines.append("")
        for name, count in trends["hotspots"]:
            lines.append(f"- **{name}**: {count} failures in last {trends['snapshots']} checks")
        lines.append("")

    if trends["regressions"]:
        lines.append("### New Regressions")
        lines.append("")
        for r in trends["regressions"]:
            lines.append(f"- {r}")
        lines.append("")

    # Recent decisions
    if decisions:
        lines.append("### Recent Autonomy Decisions")
        lines.append("")
        for d in decisions[-3:]:
            mode = d.get("mode", "?")
            reason = d.get("reason", "")[:100]
            ts = d.get("decided_at", "") or d.get("timestamp", "")
            if ts:
                # Show just time portion
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    ts_short = dt.strftime("%H:%M UTC")
                except (ValueError, TypeError):
                    ts_short = ts[:16]
            else:
                ts_short = "?"
            lines.append(f"- [{ts_short}] **{mode.upper()}**: {reason}")
        lines.append("")

    # Footer
    lines.append("---")
    lines.append(
        f"*Generated automatically by NovaCore autonomous reporting at {now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}*"
    )

    return "\n".join(lines)


# ── Vault Writer ───────────────────────────────────────────────────────────────


def write_diary_note(body: str) -> Path:
    """Write the report as a diary note in NovaVault 90-diary/."""
    VAULT_DIARY.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    time_slug = now.strftime("%H%M")

    filename = f"autonomous-report-{date_str}-{time_slug}.md"
    vault_path = VAULT_DIARY / filename

    # Dedup: if a report for this 2h window already exists, skip
    if vault_path.exists():
        log(f"Report already exists: {filename} — skipping")
        return vault_path

    frontmatter = f"""---
type: diary
title: "NovaCore Autonomous Report — {date_str} {time_slug}"
date: "{date_str}"
source: nova-core-report
tags:
  - "#type/diary"
  - "#project/novacore"
  - "#report/autonomous"
---
"""
    note = frontmatter + "\n" + body + "\n"
    vault_path.write_text(note, encoding="utf-8")
    log(f"Wrote diary note: {vault_path}")
    return vault_path


# ── Main ───────────────────────────────────────────────────────────────────────

REPORT_LOCK = STATE_DIR / "autonomous_report.lock"
REPORT_COOLDOWN_S = 1500  # minimum 25 minutes between reports


def _acquire_report_lock() -> bool:
    """Prevent concurrent/rapid-fire report generation via lockfile + cooldown."""
    try:
        if REPORT_LOCK.exists():
            try:
                last_ts = float(REPORT_LOCK.read_text().strip())
                if time.time() - last_ts < REPORT_COOLDOWN_S:
                    age_m = int((time.time() - last_ts) / 60)
                    log(f"Report cooldown active ({age_m}m since last) — skipping")
                    return False
            except (ValueError, OSError):
                pass
        REPORT_LOCK.write_text(str(time.time()))
        return True
    except OSError as e:
        log(f"Lock error: {e}")
        return False


def main() -> int:
    t0 = time.monotonic()
    log("Starting autonomous report generation")

    if not _acquire_report_lock():
        return 0  # cooldown — not an error

    try:
        body = format_report()
        vault_path = write_diary_note(body)
        elapsed = round((time.monotonic() - t0) * 1000)
        log(f"Report complete in {elapsed}ms → {vault_path.name}")

        # Also write a copy to OUTPUT/ for audit trail
        now = datetime.now(timezone.utc)
        output_name = f"autonomous_report_{now.strftime('%Y%m%d_%H%M%S')}.md"
        output_path = OUTPUT_DIR / output_name
        output_path.write_text(body, encoding="utf-8")
        log(f"Output copy: {output_path.name}")

        return 0
    except Exception as e:
        log(f"ERROR: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
