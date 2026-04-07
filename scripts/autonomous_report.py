#!/usr/bin/env python3
"""NovaCore Autonomous Report — post-heartbeat decision digest.

Runs after EVERY heartbeat cycle to provide full visibility into
goal-driven autonomous decisions, explaining actions and thought processes.

Also supports standalone 2-hour digest mode via `main()`.

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
DECISION_OUTCOMES = STATE_DIR / "decision_outcomes.json"
INVESTIGATIONS = STATE_DIR / "investigations.json"

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


def collect_decision_history(count: int = 5) -> list[dict]:
    """Read recent autonomy decisions."""
    try:
        if DECISION_HISTORY.exists():
            data = json.loads(DECISION_HISTORY.read_text())
            if isinstance(data, list):
                return data[-count:]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def collect_decision_outcomes(count: int = 5) -> list[dict]:
    """Read recent decision outcome assessments."""
    try:
        if DECISION_OUTCOMES.exists():
            data = json.loads(DECISION_OUTCOMES.read_text())
            if isinstance(data, list):
                return data[-count:]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def collect_investigations(count: int = 3) -> list[dict]:
    """Read recent investigation reports."""
    try:
        if INVESTIGATIONS.exists():
            data = json.loads(INVESTIGATIONS.read_text())
            if isinstance(data, list):
                return data[-count:]
    except (json.JSONDecodeError, OSError):
        pass
    return []


# ── Report Formatting ──────────────────────────────────────────────────────────


def _trend_arrow(direction: str) -> str:
    return {"improving": "^", "degrading": "v", "stable": "="}.get(direction, "?")


def _decision_mode_explanation(mode: str) -> str:
    """Return a human-readable explanation of what a decision mode means."""
    explanations = {
        "monitor": "All systems nominal — no intervention needed. The engine is passively watching.",
        "research": "Knowledge gap detected — the system needs more information before acting.",
        "plan": "Low score detected with no existing remediation plan — creating one.",
        "execute": "Low score detected with an existing plan — executing the planned fix.",
        "repair": "Regression or critical failure detected — immediate corrective action.",
        "validate": "All dimensions healthy with recent improvements — verifying stability.",
    }
    return explanations.get(mode.lower(), f"Unknown mode: {mode}")


def _format_evaluation(dims: dict) -> list[str]:
    """Format step 2: dimension evaluation."""
    lines: list[str] = []
    yellow = [n for n, d in dims.items() if 40 <= d.get("score", 0) < 70]
    red = [n for n, d in dims.items() if d.get("score", 0) < 40]
    green = [n for n, d in dims.items() if d.get("score", 0) >= 70]
    degrading = [n for n, d in dims.items() if d.get("trend") == "degrading"]

    if red:
        names = ", ".join(d.replace("_", " ") for d in red)
        lines.append(f"- CRITICAL dimensions below 40: **{names}** — requires immediate attention")
    if yellow:
        names = ", ".join(d.replace("_", " ") for d in yellow)
        lines.append(f"- WARNING dimensions (40-70): **{names}** — needs improvement")
    if green:
        names = ", ".join(d.replace("_", " ") for d in green)
        lines.append(f"- HEALTHY dimensions (70+): **{names}** — operating normally")
    if degrading:
        names = ", ".join(d.replace("_", " ") for d in degrading)
        lines.append(f"- Degrading trends detected in: **{names}**")
    if not degrading and not red and not yellow:
        lines.append("- All dimensions healthy and stable — no concerns detected")
    return lines


def _format_actions(direct_action: dict, mode: str) -> list[str]:
    """Format step 4: actions taken."""
    lines: list[str] = []
    if direct_action:
        lines.append("**4. Actions Taken (what I did):**")
        lines.append("")
        summary = direct_action.get("summary", "")
        if summary:
            lines.append(f"- **Summary:** {summary}")
        critical = direct_action.get("critical_findings", 0)
        if critical:
            lines.append(f"- **Critical findings:** {critical}")
        actions = direct_action.get("actions_taken", [])
        for a in actions:
            lines.append(f"- {a}")
        if direct_action.get("alert_sent"):
            lines.append("- Telegram alert sent")
        if direct_action.get("escalated"):
            lines.append("- **ESCALATED** — flagged for human attention")
        if not actions and not summary:
            lines.append("- No direct actions were needed")
        lines.append("")
    elif mode.lower() == "monitor":
        lines.append("**4. Actions Taken:** None — monitoring only, no intervention needed.")
        lines.append("")
    return lines


def _format_thought_process(autonomy: dict, decisions: list[dict]) -> list[str]:
    """Generate a narrative 'Thought Process' section explaining autonomous reasoning."""
    lines: list[str] = []

    dec = autonomy.get("decision", {})
    mode = dec.get("mode", "monitor")
    reason = dec.get("reason", "")
    target = dec.get("target")
    dims = autonomy.get("dimensions", {})
    overall = autonomy.get("overall_score", 0)
    goal = autonomy.get("goal_tree", {})
    direct_action = autonomy.get("direct_action", {})

    lines.append("### Decision Reasoning")
    lines.append("")

    # Step 1: What the engine observed
    lines.append("**1. Observation (what I measured):**")
    lines.append("")
    lines.append(f"Overall autonomy score is **{overall}/100**. Dimensional breakdown:")
    for dim_name, dim_data in dims.items():
        score = dim_data.get("score", 0)
        trend = dim_data.get("trend", "stable")
        level = "GREEN" if score >= 70 else ("YELLOW" if score >= 40 else "RED")
        pretty = dim_name.replace("_", " ").title()
        lines.append(f"- **{pretty}**: {score:.0f}/100 ({level}, {_trend_arrow(trend)} {trend})")
    lines.append("")

    # Step 2: How the engine evaluated
    lines.append("**2. Evaluation (how I interpreted the scores):**")
    lines.append("")
    lines.extend(_format_evaluation(dims))
    lines.append("")

    # Step 3: What the engine decided
    lines.append("**3. Decision (what I chose to do):**")
    lines.append("")
    lines.append(f"- **Mode:** {mode.upper()}")
    lines.append(f"- **Explanation:** {_decision_mode_explanation(mode)}")
    if target:
        lines.append(f"- **Target dimension:** {target.replace('_', ' ').title()}")
    lines.append(f"- **Reason:** {reason}")
    lines.append("")

    # Step 4: What actions were taken
    lines.extend(_format_actions(direct_action, mode))

    # Step 5: Goal progress context
    if goal:
        completed = goal.get("completed", 0)
        total = goal.get("total", 0)
        actionable = goal.get("actionable", [])
        lines.append("**5. Goal Context:**")
        lines.append("")
        lines.append(f"- NovaTrade goal tree: **{completed}/{total}** sub-goals complete")
        if actionable:
            lines.append(f"- Next actionable sub-goals: {', '.join(actionable[:3])}")
        elif completed == total:
            lines.append("- All sub-goals complete — maintaining operational excellence")
        lines.append("")

    # Step 6: Recent decision pattern (are we stuck in a loop?)
    if len(decisions) >= 2:
        modes = [d.get("mode", "?") for d in decisions[-5:]]
        if len(set(modes)) == 1 and len(modes) >= 3:
            lines.append("**6. Pattern Alert:**")
            lines.append("")
            if modes[0] == "monitor":
                note = "this is expected for stable systems"
            else:
                note = "possible stuck loop, may need investigation"
            lines.append(f"- Last {len(modes)} decisions were all **{modes[0].upper()}** — {note}")
            lines.append("")

    return lines


def format_post_heartbeat_report(autonomy_snapshot: dict | None = None) -> str:  # noqa: C901
    """Format a detailed post-heartbeat report with full decision reasoning.

    This is the primary report format — runs after every heartbeat to give
    full visibility into what the autonomy engine observed, decided, and did.
    """
    now_utc = datetime.now(timezone.utc)
    now_ct = now_utc - timedelta(hours=5)  # CDT = UTC-5
    date_str = now_utc.strftime("%Y-%m-%d")
    time_str = now_ct.strftime("%I:%M %p CT")

    # Use passed-in snapshot if available, otherwise read from disk
    autonomy = autonomy_snapshot or collect_autonomy_scores()

    # Collect remaining data
    services = collect_service_status()
    disk = collect_disk_usage()
    tasks = collect_recent_tasks(hours=1)  # 1h window for per-heartbeat
    decisions = collect_decision_history(count=5)
    outcomes = collect_decision_outcomes(count=3)
    investigations = collect_investigations(count=2)

    # Build report
    lines: list[str] = []

    # Header
    lines.append(f"## NovaCore Post-Heartbeat Report — {date_str} {time_str}")
    lines.append("")

    # Quick status bar
    all_services_ok = all(s["ok"] for s in services)
    overall_health = "HEALTHY" if all_services_ok else "DEGRADED"
    if autonomy:
        score = autonomy.get("overall_score", 0)
        alert = autonomy.get("alert_level", "?")
        mode = autonomy.get("decision", {}).get("mode", "?").upper()
        lines.append(
            f"**Status:** {overall_health} | **Score:** {score}/100"
            f" ({alert}) | **Decision:** {mode}"
            f" | **Disk:** {disk['used_pct']}%"
        )
    else:
        lines.append(f"**Status:** {overall_health} | **Disk:** {disk['used_pct']}% | *Autonomy data unavailable*")
    lines.append("")

    # Services (compact)
    svc_parts = []
    for s in services:
        mark = "[OK]" if s["ok"] else "[FAIL]"
        svc_parts.append(f"{mark} {s['name']}")
    lines.append(f"**Services:** {' | '.join(svc_parts)}")
    lines.append("")

    # ── Core: Decision Reasoning ──────────────────────────────────────────
    if autonomy:
        lines.extend(_format_thought_process(autonomy, decisions))

    # ── Autonomy Dimensions Table ─────────────────────────────────────────
    if autonomy and autonomy.get("dimensions"):
        lines.append("### Dimension Scores")
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

    # ── Investigation Results ─────────────────────────────────────────────
    if investigations:
        lines.append("### Recent Investigations")
        lines.append("")
        for inv in investigations:
            inv_id = inv.get("investigation_id", "?")
            target = inv.get("target_dimension", "?").replace("_", " ").title()
            root = inv.get("root_cause", "unknown")
            confidence = inv.get("root_cause_confidence", "?")
            recommended = inv.get("recommended_action", "")
            escalated = inv.get("escalated", False)
            lines.append(f"**Investigation {inv_id}** — {target}")
            lines.append(f"- Root cause: {root} (confidence: {confidence})")
            if recommended:
                lines.append(f"- Recommended: {recommended[:150]}")
            if escalated:
                lines.append("- **ESCALATED** to human operator")
            actions = inv.get("actions_taken", [])
            if actions:
                for a in actions[:3]:
                    lines.append(f"- Action: {a[:120]}")
            lines.append("")

    # ── Decision Outcome Tracking ─────────────────────────────────────────
    if outcomes:
        lines.append("### Decision Effectiveness")
        lines.append("")
        for o in outcomes:
            dec_id = o.get("decision_id", "?")
            verdict = o.get("verdict", "pending")
            delta = o.get("delta", 0)
            mode = o.get("mode", "?")
            sign = "+" if delta > 0 else ""
            lines.append(f"- **{mode.upper()}** ({dec_id[:30]}): {verdict} ({sign}{delta:.1f} pts)")
        lines.append("")

    # ── Decision History (last 5) ─────────────────────────────────────────
    if decisions:
        lines.append("### Decision History (last 5)")
        lines.append("")
        for d in decisions:
            mode = d.get("mode", "?")
            reason = d.get("reason", "")[:120]
            ts = d.get("decided_at", "") or d.get("timestamp", "")
            target = d.get("target_dimension")
            confidence = d.get("confidence", "")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    ts_short = dt.strftime("%H:%M UTC")
                except (ValueError, TypeError):
                    ts_short = ts[:16]
            else:
                ts_short = "?"
            target_str = f" [{target.replace('_', ' ')}]" if target else ""
            conf_str = f" ({confidence})" if confidence else ""
            lines.append(f"- [{ts_short}] **{mode.upper()}**{target_str}{conf_str}: {reason}")
        lines.append("")

    # ── Task Activity ─────────────────────────────────────────────────────
    lines.append("### Task Activity (last 1h)")
    lines.append("")
    done_ct = len(tasks["recently_done"])
    fail_ct = len(tasks["recently_failed"])
    lines.append(
        f"- Pending: {tasks['pending']}"
        f" | In Progress: {tasks['in_progress']}"
        f" | Completed: {done_ct} | Failed: {fail_ct}"
    )

    if tasks["recently_done"]:
        for t in tasks["recently_done"][:5]:
            clean = re.sub(r"^\d+_", "", t).replace("_", " ")
            lines.append(f"  - Done: {clean}")
    if tasks["recently_failed"]:
        for t in tasks["recently_failed"][:3]:
            clean = re.sub(r"^\d+_", "", t).replace("_", " ")
            lines.append(f"  - FAILED: {clean}")
    lines.append("")

    # Footer
    lines.append("---")
    lines.append(f"*Post-heartbeat report generated at {now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}*")

    return "\n".join(lines)


def format_report() -> str:  # noqa: C901
    """Collect all data and format the autonomous report (legacy 2h digest)."""
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


def write_diary_note(body: str, prefix: str = "autonomous-report") -> Path:
    """Write the report as a diary note in NovaVault 90-diary/."""
    VAULT_DIARY.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    time_slug = now.strftime("%H%M")

    filename = f"{prefix}-{date_str}-{time_slug}.md"
    vault_path = VAULT_DIARY / filename

    # Dedup: if a report for this exact minute already exists, skip
    if vault_path.exists():
        log(f"Report already exists: {filename} — skipping")
        return vault_path

    frontmatter = f"""---
type: diary
title: "NovaCore Post-Heartbeat Report — {date_str} {time_slug}"
date: "{date_str}"
source: nova-core-heartbeat-report
tags:
  - "#type/diary"
  - "#project/novacore"
  - "#report/heartbeat"
  - "#report/autonomous"
related:
  - "[[moc-operations]]"
---
"""
    note = (
        frontmatter
        + "\nup:: [[moc-operations]]\n\n"
        + body
        + "\n\n## Related Notes\n\n- [[moc-operations]] -- system operations\n"
    )
    vault_path.write_text(note, encoding="utf-8")
    log(f"Wrote diary note: {vault_path}")
    return vault_path


# ── Entry Points ──────────────────────────────────────────────────────────────

REPORT_LOCK = STATE_DIR / "autonomous_report.lock"
REPORT_COOLDOWN_S = 1500  # minimum 25 minutes between reports (legacy)


def _acquire_report_lock(cooldown_s: int = REPORT_COOLDOWN_S) -> bool:
    """Prevent concurrent/rapid-fire report generation via lockfile + cooldown."""
    try:
        if REPORT_LOCK.exists():
            try:
                last_ts = float(REPORT_LOCK.read_text().strip())
                if time.time() - last_ts < cooldown_s:
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


def run_post_heartbeat_report(autonomy_snapshot: dict | None = None) -> int:
    """Generate a post-heartbeat report with full decision reasoning.

    Called by heartbeat.py after every autonomy cycle. Uses a shorter
    cooldown (20 min) to allow one report per heartbeat (30 min interval)
    while preventing duplicate runs.
    """
    t0 = time.monotonic()
    log("Starting post-heartbeat report generation")

    # 20-minute cooldown — heartbeats are 30 min apart, so this allows
    # one report per heartbeat while blocking accidental double-runs
    if not _acquire_report_lock(cooldown_s=1200):
        return 0  # cooldown — not an error

    try:
        body = format_post_heartbeat_report(autonomy_snapshot)
        vault_path = write_diary_note(body, prefix="heartbeat-report")
        elapsed = round((time.monotonic() - t0) * 1000)
        log(f"Post-heartbeat report complete in {elapsed}ms -> {vault_path.name}")

        # Also write a copy to OUTPUT/ for audit trail
        now = datetime.now(timezone.utc)
        output_name = f"heartbeat_report_{now.strftime('%Y%m%d_%H%M%S')}.md"
        output_path = OUTPUT_DIR / output_name
        output_path.write_text(body, encoding="utf-8")
        log(f"Output copy: {output_path.name}")

        return 0
    except Exception as e:
        log(f"ERROR: {e}")
        import traceback

        traceback.print_exc()
        return 1


def main() -> int:
    """Legacy 2-hour digest mode (standalone execution)."""
    t0 = time.monotonic()
    log("Starting autonomous report generation")

    if not _acquire_report_lock():
        return 0  # cooldown — not an error

    try:
        body = format_report()
        vault_path = write_diary_note(body)
        elapsed = round((time.monotonic() - t0) * 1000)
        log(f"Report complete in {elapsed}ms -> {vault_path.name}")

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
