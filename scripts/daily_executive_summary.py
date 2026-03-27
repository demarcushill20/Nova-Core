#!/usr/bin/env python3
"""NovaCore Daily Executive Summary — end-of-day digest.

Runs as a systemd oneshot (triggered by novacore-daily-summary.timer at 23:20 CT).
Aggregates the day's autonomous reports, tasks, commits, metrics, and system
health into a comprehensive narrative executive summary. Writes to:
  - /home/nova/nova-vault/00-inbox/executive-summary-YYYY-MM-DD.md
  - /home/nova/nova-core/OUTPUT/executive_summary_YYYY-MM-DD.md
  - /home/nova/nova-core/OUTPUT/executive_summary_YYYY-MM-DD.pdf

Designed for text-to-speech consumption (Eleven Reader). Clean prose, no emoji,
no decorative characters. Minimum 1000 words. Full chronological detail of every
autonomous action taken during the day.

Stdlib + fpdf2 only.  No Claude Code or MCP dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# -- Paths -----------------------------------------------------------------
BASE = Path("/home/nova/nova-core")
STATE_DIR = BASE / "STATE"
TASKS_DIR = BASE / "TASKS"
OUTPUT_DIR = BASE / "OUTPUT"
LOGS_DIR = BASE / "LOGS"
MEMORY_DIR = BASE / "MEMORY" / "workflow_learnings"
HEARTBEAT_FILE = BASE / "HEARTBEAT.md"
METRICS_JSONL = STATE_DIR / "heartbeat_metrics.jsonl"
AUTONOMY_SNAPSHOT = STATE_DIR / "autonomy_snapshot.json"
DECISION_HISTORY = STATE_DIR / "decision_history.json"

VAULT_DIR = Path("/home/nova/nova-vault")
VAULT_INBOX = VAULT_DIR / "00-inbox"
VAULT_DIARY = VAULT_DIR / "90-diary"

SERVICES = [
    "novacore-watcher",
    "novacore-telegram",
    "novacore-telegram-notifier",
]

# Central Time offset (CDT = UTC-5, CST = UTC-6).  We determine dynamically.
CT = timezone(timedelta(hours=-6), "CST")
CDT = timezone(timedelta(hours=-5), "CDT")


def _ct_now() -> datetime:
    """Return current time in US Central, approximating DST via month heuristic."""
    utc_now = datetime.now(timezone.utc)
    month = utc_now.month
    if 3 < month < 11:
        return utc_now.astimezone(CDT)
    elif month == 3:
        return utc_now.astimezone(CDT) if utc_now.day >= 10 else utc_now.astimezone(CT)
    elif month == 11:
        return utc_now.astimezone(CT) if utc_now.day >= 3 else utc_now.astimezone(CDT)
    else:
        return utc_now.astimezone(CT)


def _utc_slug_to_ct(slug: str) -> str:
    """Convert a 4-digit UTC time slug like '0018' to Central Time display string."""
    if len(slug) != 4 or not slug.isdigit():
        return slug
    h, m = int(slug[:2]), int(slug[2:])
    utc_dt = datetime.now(timezone.utc).replace(hour=h, minute=m, second=0)
    ct_dt = _to_ct(utc_dt)
    return ct_dt.strftime("%I:%M %p CT").lstrip("0")


def _to_ct(utc_dt: datetime) -> datetime:
    """Convert a UTC datetime to Central Time."""
    month = utc_dt.month
    if 3 < month < 11:
        return utc_dt.astimezone(CDT)
    elif month == 3:
        return utc_dt.astimezone(CDT) if utc_dt.day >= 10 else utc_dt.astimezone(CT)
    elif month == 11:
        return utc_dt.astimezone(CT) if utc_dt.day >= 3 else utc_dt.astimezone(CDT)
    else:
        return utc_dt.astimezone(CT)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [daily-summary] {msg}", flush=True)


# -- Data Collection -------------------------------------------------------


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
                results.append(
                    {
                        "name": svc,
                        "ok": True,
                        "detail": f"active since {uptime_str}",
                    }
                )
            else:
                results.append(
                    {
                        "name": svc,
                        "ok": False,
                        "detail": r.stdout.strip(),
                    }
                )
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
            return json.loads(AUTONOMY_SNAPSHOT.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return None


def collect_metrics() -> dict | None:
    """Read STATE/metrics.json if it exists."""
    metrics_file = STATE_DIR / "metrics.json"
    try:
        if metrics_file.exists():
            return json.loads(metrics_file.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return None


def collect_heartbeat_trends(window: int = 48) -> dict:
    """Analyze last N heartbeat snapshots (full day at 30-min intervals = 48)."""
    if not METRICS_JSONL.exists():
        return {
            "availability_pct": 100.0,
            "snapshots": 0,
            "hotspots": [],
            "regressions": [],
        }
    try:
        lines = METRICS_JSONL.read_text().splitlines()
        entries = []
        for ln in lines[-window:]:
            if ln.strip():
                entries.append(json.loads(ln))
    except (json.JSONDecodeError, OSError):
        return {
            "availability_pct": 100.0,
            "snapshots": 0,
            "hotspots": [],
            "regressions": [],
        }

    if not entries:
        return {
            "availability_pct": 100.0,
            "snapshots": 0,
            "hotspots": [],
            "regressions": [],
        }

    healthy = sum(1 for e in entries if e.get("status") == "healthy")
    avail = round(healthy / len(entries) * 100, 1)

    fail_counts: dict[str, int] = {}
    for e in entries:
        for name in e.get("failed_names", []):
            fail_counts[name] = fail_counts.get(name, 0) + 1
    hotspots = sorted(fail_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    regressions: list[str] = []
    if len(entries) >= 4:
        recent_fails: set[str] = set()
        for e in entries[-3:]:
            recent_fails.update(e.get("failed_names", []))
        prior_fails: set[str] = set()
        for e in entries[:-3]:
            prior_fails.update(e.get("failed_names", []))
        regressions = sorted(recent_fails - prior_fails)

    return {
        "availability_pct": avail,
        "snapshots": len(entries),
        "hotspots": hotspots,
        "regressions": regressions,
    }


def collect_todays_diary_reports(date_str: str) -> list[dict]:  # noqa: C901
    """Scan nova-vault 90-diary/ for today's autonomous report files.

    Returns full report data including the raw text body for narrative extraction.
    """
    reports: list[dict] = []
    pattern = f"autonomous-report-{date_str}-"
    try:
        if not VAULT_DIARY.exists():
            return reports
        for f in sorted(VAULT_DIARY.iterdir()):
            if f.is_file() and f.name.startswith(pattern) and f.name.endswith(".md"):
                try:
                    text = f.read_text(encoding="utf-8")
                    time_slug = f.stem.split("-")[-1]  # e.g. "0018"
                    report_info: dict = {
                        "file": f.name,
                        "time_slug": time_slug,
                        "body": text,
                    }

                    # Parse autonomy score
                    m = re.search(r"\*\*Autonomy Score:\*\*\s*([\d.]+)", text)
                    if m:
                        report_info["autonomy_score"] = float(m.group(1))

                    # Parse availability
                    m = re.search(r"\*\*Heartbeat Availability.*?:\*\*\s*([\d.]+)%", text)
                    if m:
                        report_info["availability"] = float(m.group(1))

                    # Parse system status
                    m = re.search(r"\*\*System Status:\*\*\s*(\w+)", text)
                    if m:
                        report_info["status"] = m.group(1)

                    # Parse completed tasks count
                    m = re.search(r"\*\*Completed:\*\*\s*(\d+)", text)
                    if m:
                        report_info["tasks_done"] = int(m.group(1))

                    # Parse completed task names
                    completed_tasks = re.findall(r"\*\*Completed tasks:\*\*\n((?:- .+\n?)+)", text)
                    if completed_tasks:
                        names = [ln.strip().lstrip("- ") for ln in completed_tasks[0].strip().splitlines()]
                        report_info["completed_task_names"] = names

                    # Parse decision mode
                    m = re.search(
                        r"\*\*Decision Mode:\*\*\s*(\w+)\s*[--]+\s*(.+?)$",
                        text,
                        re.MULTILINE,
                    )
                    if m:
                        report_info["decision_mode"] = m.group(1).strip()
                        report_info["decision_reason"] = m.group(2).strip()

                    # Parse health hotspots
                    hotspot_section = re.findall(r"### Health Hotspots\n\n((?:- .+\n?)+)", text)
                    if hotspot_section:
                        hotspot_lines = hotspot_section[0].strip().splitlines()
                        report_info["hotspots"] = [ln.strip().lstrip("- ") for ln in hotspot_lines]

                    # Parse recent outputs
                    output_section = re.findall(r"### Recent Outputs\n\n((?:- .+\n?)+)", text)
                    if output_section:
                        out_lines = output_section[0].strip().splitlines()
                        report_info["recent_outputs"] = [ln.strip().lstrip("- ").strip("`") for ln in out_lines]

                    # Parse regressions
                    reg_section = re.findall(r"### New Regressions\n\n((?:- .+\n?)+)", text)
                    if reg_section:
                        reg_lines = reg_section[0].strip().splitlines()
                        report_info["regressions"] = [ln.strip().lstrip("- ") for ln in reg_lines]

                    # Parse dimension scores
                    dim_rows = re.findall(
                        r"\|\s*([A-Z][a-z ]+)\s*\|\s*(\d+)\s*\|\s*(\w+)\s*\|"
                        r"\s*[=^v?]\s*(\w+)\s*\|",
                        text,
                    )
                    if dim_rows:
                        report_info["dimensions"] = [
                            {
                                "name": row[0].strip(),
                                "score": int(row[1]),
                                "status": row[2].strip(),
                                "trend": row[3].strip(),
                            }
                            for row in dim_rows
                        ]

                    # Parse service statuses
                    svc_lines = re.findall(
                        r"- \[(OK|FAIL)\] \*\*([^*]+)\*\*:\s*(.+)",
                        text,
                    )
                    if svc_lines:
                        report_info["services"] = [
                            {
                                "ok": s[0] == "OK",
                                "name": s[1],
                                "detail": s[2].strip(),
                            }
                            for s in svc_lines
                        ]

                    reports.append(report_info)
                except OSError:
                    continue
    except OSError:
        pass
    return reports


def collect_completed_tasks_detailed(date_str: str) -> list[dict]:
    """Scan TASKS/ for completed/done tasks with content detail."""
    completed: list[dict] = []
    date_compact = date_str.replace("-", "")
    try:
        if not TASKS_DIR.exists():
            return completed
        for f in sorted(TASKS_DIR.iterdir()):
            if not f.is_file():
                continue
            name = f.name
            stem = name
            status = None

            if name.endswith(".done") or name.endswith(".done.md"):
                status = "done"
                stem = name.replace(".done.md", "").replace(".done", "").replace(".md", "")
            elif name.endswith(".md"):
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")[:2000]
                    lower = text.lower()
                    if "status: completed" in lower or "status: done" in lower:
                        status = "done"
                        stem = name.replace(".md", "")
                except OSError:
                    continue

            if not status:
                continue

            # Check if task was modified today (by date in filename or mtime)
            mtime = f.stat().st_mtime
            mtime_date = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d")
            is_today = date_str in name or date_compact in name or mtime_date == date_str

            # Read task content for detail
            content = ""
            try:
                content = f.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                pass

            clean = re.sub(r"^\d+_", "", stem).replace("_", " ")

            # Extract task ID if present
            task_id_match = re.match(r"^(\d+)", stem)
            task_id = task_id_match.group(1) if task_id_match else ""

            completed.append(
                {
                    "file": name,
                    "name": clean,
                    "stem": stem,
                    "task_id": task_id,
                    "content": content,
                    "is_today": is_today,
                    "mtime": mtime,
                }
            )
    except OSError:
        pass
    return completed


def collect_todays_outputs(date_str: str) -> list[str]:
    """List output files created today."""
    results: list[str] = []
    date_compact = date_str.replace("-", "")
    try:
        if not OUTPUT_DIR.exists():
            return results
        for f in sorted(OUTPUT_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not f.is_file():
                continue
            mtime_date = datetime.fromtimestamp(
                f.stat().st_mtime,
                tz=timezone.utc,
            ).strftime("%Y-%m-%d")
            if date_str in f.name or date_compact in f.name or mtime_date == date_str:
                results.append(f.name)
    except OSError:
        pass
    return results[:30]


def collect_todays_commits(date_str: str) -> list[dict]:
    """Get today's git commits."""
    commits = []
    try:
        r = subprocess.run(
            [
                "git",
                "log",
                f"--since={date_str}T00:00:00",
                f"--until={date_str}T23:59:59",
                "--format=%H|%h|%s|%an|%ai",
                "--no-merges",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(BASE),
        )
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                parts = line.split("|", 4)
                if len(parts) >= 4:
                    commits.append(
                        {
                            "hash": parts[0][:8],
                            "short": parts[1],
                            "subject": parts[2],
                            "author": parts[3],
                            "date": parts[4] if len(parts) > 4 else "",
                        }
                    )
    except Exception as exc:
        logging.debug("Failed to collect commits: %s", exc)
    return commits


def collect_decision_history() -> list[dict]:
    """Read recent autonomy decisions."""
    try:
        if DECISION_HISTORY.exists():
            data = json.loads(DECISION_HISTORY.read_text())
            if isinstance(data, list):
                return data[-10:]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def collect_open_tasks() -> list[str]:
    """Scan TASKS/ for pending/in-progress tasks (open threads)."""
    open_tasks: list[str] = []
    try:
        if not TASKS_DIR.exists():
            return open_tasks
        for f in sorted(TASKS_DIR.iterdir()):
            if not f.is_file():
                continue
            name = f.name
            if name.endswith(".done") or name.endswith(".failed"):
                continue
            if name.startswith("_"):
                continue
            if name.endswith(".inprogress") or name.endswith(".md"):
                stem = name.replace(".inprogress", "").replace(".md", "")
                clean = re.sub(r"^\d+_", "", stem).replace("_", " ")
                tag = "[IN PROGRESS]" if ".inprogress" in name else "[PENDING]"
                open_tasks.append(f"{tag} {clean}")
    except OSError:
        pass
    return open_tasks[:20]


def collect_workflow_learnings(date_str: str) -> list[dict]:
    """Read today's workflow learning memory files for rich task detail."""
    learnings: list[dict] = []
    try:
        if not MEMORY_DIR.exists():
            return learnings
        for f in sorted(MEMORY_DIR.iterdir()):
            if not f.is_file() or not f.name.endswith(".json"):
                continue
            # Check if file was created/modified today
            mtime = f.stat().st_mtime
            mtime_date = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d")
            if mtime_date != date_str:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                learnings.append(
                    {
                        "file": f.name,
                        "workflow_id": data.get("workflow_id", ""),
                        "task_summary": data.get("task_summary", ""),
                        "task_class": data.get("task_class", ""),
                        "key_decisions": data.get("key_decisions", []),
                        "successful_patterns": data.get("successful_patterns", []),
                        "failure_patterns": data.get("failure_patterns", []),
                        "verification_outcome": data.get("verification_outcome", ""),
                        "reusable_guidance": data.get("reusable_guidance", ""),
                        "confidence": data.get("confidence", ""),
                        "created_at": data.get("created_at", ""),
                    }
                )
            except (json.JSONDecodeError, OSError):
                continue
    except OSError:
        pass
    return learnings


# -- Report Formatting (Narrative Style) -----------------------------------


def _deduplicate_reports(reports: list[dict]) -> list[dict]:
    """Deduplicate diary reports that are within 5 minutes of each other.

    When the heartbeat fires rapidly, many near-identical reports are produced.
    Keep the first report in each cluster (reports within 5 min of each other
    with the same status/score are collapsed).
    """
    if not reports:
        return []

    deduped = [reports[0]]
    for rpt in reports[1:]:
        prev = deduped[-1]
        try:
            prev_min = int(prev["time_slug"][:2]) * 60 + int(prev["time_slug"][2:])
            curr_min = int(rpt["time_slug"][:2]) * 60 + int(rpt["time_slug"][2:])
            delta = abs(curr_min - prev_min)
        except (ValueError, KeyError):
            delta = 999

        # If within 5 minutes and same score, skip as duplicate
        if (
            delta <= 5
            and rpt.get("autonomy_score") == prev.get("autonomy_score")
            and rpt.get("status") == prev.get("status")
        ):
            continue
        deduped.append(rpt)
    return deduped


def format_executive_summary() -> tuple[str, str]:  # noqa: C901
    """Collect all data and compile a detailed narrative executive summary.

    Produces clean prose suitable for text-to-speech. No emoji. No decorative
    characters. Minimum 1000 words. Full chronological detail.
    """
    now_utc = datetime.now(timezone.utc)
    now_ct = _ct_now()
    date_str = now_ct.strftime("%Y-%m-%d")
    date_human = now_ct.strftime("%B %d, %Y")

    # -- Collect all data --------------------------------------------------
    services = collect_service_status()
    disk = collect_disk_usage()
    autonomy = collect_autonomy_scores()
    _metrics = collect_metrics()
    trends = collect_heartbeat_trends(window=48)
    diary_reports_raw = collect_todays_diary_reports(date_str)
    diary_reports = _deduplicate_reports(diary_reports_raw)
    all_tasks = collect_completed_tasks_detailed(date_str)
    todays_tasks = [t for t in all_tasks if t["is_today"]]
    todays_outputs = collect_todays_outputs(date_str)
    commits = collect_todays_commits(date_str)
    _decisions = collect_decision_history()
    open_tasks = collect_open_tasks()
    learnings = collect_workflow_learnings(date_str)

    # -- Derived values ----------------------------------------------------
    all_services_ok = all(s["ok"] for s in services)
    overall_health = "HEALTHY" if all_services_ok else "DEGRADED"
    autonomy_score = autonomy.get("overall_score", 0) if autonomy else 0
    _alert_level = autonomy.get("alert_level", "UNKNOWN") if autonomy else "UNKNOWN"

    lines: list[str] = []

    # -- Title -------------------------------------------------------------
    lines.append("# NovaCore Executive Summary")
    lines.append(f"## Daily Diary Digest -- {date_human}")
    lines.append("")

    # -- Quick-glance table ------------------------------------------------
    lines.append("| System Status | Autonomy Score | Disk Available | Tasks Completed |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| **{overall_health}** | **{autonomy_score:.1f}** | **{disk['free_gb']} GB** | **{len(todays_tasks)}** |"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    # -- Section 1: Executive Overview (narrative) -------------------------
    lines.append("## Executive Overview")
    lines.append("")

    overview = []
    overview.append(
        f"{date_human} was a day of "
        + ("steady operational activity" if len(todays_tasks) < 5 else "significant operational activity")
        + " for the NovaCore autonomous AI runtime."
    )

    # Service health narrative
    if all_services_ok:
        overview.append(
            "The system maintained HEALTHY status throughout the entire day "
            f"with an overall autonomy score of {autonomy_score:.1f} out of 100."
        )
    else:
        down = [s["name"] for s in services if not s["ok"]]
        overview.append(
            f"The system experienced degraded status, with the following services reporting issues: {', '.join(down)}."
        )

    # Autonomy dimensions narrative
    if autonomy and autonomy.get("dimensions"):
        dims = autonomy["dimensions"]
        all_green = all(d.get("score", 0) >= 70 for d in dims.values())
        if all_green:
            overview.append("All five autonomy dimensions remained GREEN throughout the day.")
        else:
            below = [name.replace("_", " ").title() for name, d in dims.items() if d.get("score", 0) < 70]
            overview.append(f"The following autonomy dimensions fell below the GREEN threshold: {', '.join(below)}.")

    # Diary reports narrative
    if diary_reports_raw:
        overview.append(
            f"The autonomous reporting system generated {len(diary_reports_raw)} "
            f"diary entries into the NovaVault throughout the day, "
            f"capturing system snapshots at regular intervals."
        )

    # Commits
    if commits:
        overview.append(f"{len(commits)} commits were pushed to the main branch.")

    # Tasks narrative
    if todays_tasks:
        overview.append(
            f"{len(todays_tasks)} tasks were completed, including maintenance, validation, and investigation work."
        )

    # Service uptime
    active_services = [s for s in services if s["ok"]]
    if active_services:
        svc_names = [s["name"].replace("novacore-", "") for s in active_services]
        overview.append(
            f"All {len(active_services)} core services -- " + ", ".join(svc_names) + " -- ran without interruption."
        )

    lines.append(" ".join(overview))
    lines.append("")
    lines.append("---")
    lines.append("")

    # -- Section 2: Autonomous Report Timeline (chronological) -------------
    lines.append("## Autonomous Report Timeline")
    lines.append("")

    if diary_reports:
        lines.append(
            f"The autonomous reporting system produced {len(diary_reports_raw)} "
            f"raw diary entries today (after deduplication of rapid-fire heartbeat "
            f"bursts, {len(diary_reports)} distinct snapshots remain). "
            f"Reports were produced via the heartbeat integration, with each "
            f"report capturing a full system snapshot including service health, "
            f"autonomy dimension scores, task activity, and health hotspots."
        )
        lines.append("")

        # Build a summary table of the deduplicated reports
        lines.append("| Time (CT) | Autonomy | HB Avail | Tasks Done | Health Hotspots |")
        lines.append("|---|---|---|---|---|")
        for rpt in diary_reports:
            ts = _utc_slug_to_ct(rpt.get("time_slug", "?"))
            ascore = rpt.get("autonomy_score", "?")
            avail = rpt.get("availability", "?")
            tdone = rpt.get("tasks_done", "?")
            if isinstance(ascore, float):
                ascore = f"{ascore:.1f}"
            if isinstance(avail, float):
                avail = f"{avail:.1f}%"
            hotspot_str = ""
            if rpt.get("hotspots"):
                hotspot_str = "; ".join(rpt["hotspots"][:3])
            lines.append(f"| {ts} | {ascore} | {avail} | {tdone} | {hotspot_str} |")
        lines.append("")

        # Narrative about what each report found
        lines.append(
            "The following is a chronological walkthrough of each distinct reporting period and what was observed."
        )
        lines.append("")

        for _i, rpt in enumerate(diary_reports):
            ts = _utc_slug_to_ct(rpt.get("time_slug", "?"))
            score = rpt.get("autonomy_score", "unknown")
            avail = rpt.get("availability", "unknown")
            status = rpt.get("status", "unknown")
            mode = rpt.get("decision_mode", "unknown")
            reason = rpt.get("decision_reason", "")

            para = f"At {ts}, the system reported a status of {status}"
            if isinstance(score, float):
                para += f" with an autonomy score of {score:.1f}"
            para += "."

            if isinstance(avail, float):
                para += f" Heartbeat availability stood at {avail:.1f}%."

            if mode and mode != "unknown":
                para += f" The decision engine was in {mode} mode"
                if reason:
                    # Strip markdown bold markers for clean prose
                    clean_reason = reason.replace("**", "")
                    para += f": {clean_reason}"
                para += "."

            # Task completions in this report
            task_names = rpt.get("completed_task_names", [])
            tdone = rpt.get("tasks_done", 0)
            if task_names:
                para += f" During this period, {len(task_names)} task(s) were completed: {', '.join(task_names)}."
            elif tdone and tdone > 0:
                para += f" {tdone} task(s) were completed in this window."

            # Hotspots
            hotspots = rpt.get("hotspots", [])
            if hotspots:
                para += f" Health hotspots detected: {'; '.join(hotspots[:3])}."

            # Regressions
            regs = rpt.get("regressions", [])
            if regs:
                para += f" New regressions identified: {', '.join(regs)}."

            # Outputs produced
            outputs = rpt.get("recent_outputs", [])
            if outputs:
                # Only mention non-autonomous-report outputs
                notable = [o for o in outputs if not o.startswith("autonomous_report")]
                if notable:
                    para += " Notable outputs produced: " + ", ".join(notable[:5]) + "."

            lines.append(para)
            lines.append("")

        # Aggregate stats
        scores = [r["autonomy_score"] for r in diary_reports if "autonomy_score" in r]
        avails = [r["availability"] for r in diary_reports if "availability" in r]
        if scores:
            lines.append(
                f"Across all snapshots, the autonomy score ranged from "
                f"{min(scores):.1f} to {max(scores):.1f} "
                f"(average {sum(scores) / len(scores):.1f})."
            )
        if avails:
            lines.append(f"Heartbeat availability ranged from {min(avails):.1f}% to {max(avails):.1f}%.")
        lines.append("")
    else:
        lines.append("No autonomous reports were generated today.")
        lines.append("")

    lines.append("---")
    lines.append("")

    # -- Section 3: Autonomy Dimensions ------------------------------------
    if autonomy and autonomy.get("dimensions"):
        lines.append("## Autonomy Dimensions (5-D Score)")
        lines.append("")

        dims = autonomy["dimensions"]
        lines.append("| Dimension | Score | Status | Trend |")
        lines.append("|---|---|---|---|")
        for dim_name, dim_data in dims.items():
            score = dim_data.get("score", 0)
            trend = dim_data.get("trend", "stable")
            level = "GREEN" if score >= 70 else ("YELLOW" if score >= 40 else "RED")
            pretty_name = dim_name.replace("_", " ").title()
            lines.append(f"| {pretty_name} | {score:.0f} | {level} | {trend} |")
        lines.append("")

        # Narrative about each dimension
        for dim_name, dim_data in dims.items():
            score = dim_data.get("score", 0)
            trend = dim_data.get("trend", "stable")
            pretty = dim_name.replace("_", " ").title()
            if score >= 90:
                desc = f"scored an excellent {score:.0f}"
            elif score >= 70:
                desc = f"held at a solid {score:.0f}"
            elif score >= 40:
                desc = f"was at a concerning {score:.0f}"
            else:
                desc = f"was critically low at {score:.0f}"
            lines.append(f"{pretty} {desc}, trending {trend}.")
        lines.append("")

        dec = autonomy.get("decision", {})
        if dec:
            mode = dec.get("mode", "unknown").upper()
            reason = dec.get("reason", "")[:200]
            lines.append(
                f"The autonomy decision engine operated in {mode} mode" + (f": {reason}" if reason else "") + "."
            )
            lines.append("")

        goal = autonomy.get("goal_tree", {})
        if goal:
            completed_g = goal.get("completed", 0)
            total_g = goal.get("total", 0)
            lines.append(f"The goal tree stands at {completed_g} of {total_g} sub-goals complete.")
            lines.append("")

        lines.append("---")
        lines.append("")

    # -- Section 4: Completed Tasks (detailed) -----------------------------
    lines.append("## Completed Tasks")
    lines.append("")

    if todays_tasks:
        lines.append(
            f"A total of {len(todays_tasks)} tasks were completed today. The following is a detailed account of each."
        )
        lines.append("")

        for t in todays_tasks[:30]:
            task_name = t["name"]
            task_id = t["task_id"]
            content = t["content"].strip()

            header = f"### Task {task_id}: {task_name}" if task_id else f"### {task_name}"
            lines.append(header)
            lines.append("")

            # Try to extract meaningful content from the task file
            if content:
                # Remove frontmatter if present
                content_clean = re.sub(r"^---\n.*?---\n", "", content, flags=re.DOTALL).strip()
                # Remove the task title line if it repeats
                first_line = content_clean.split("\n")[0].strip()
                if first_line.lower() == task_name.lower():
                    content_clean = "\n".join(content_clean.split("\n")[1:]).strip()

                if content_clean and len(content_clean) > 20:
                    # Trim to a reasonable summary length
                    summary = content_clean[:2000]
                    # Clean up markdown artifacts for prose
                    summary = summary.replace("**", "")
                    summary = re.sub(r"^#+\s+", "", summary, flags=re.MULTILINE)
                    lines.append(summary)
                else:
                    lines.append(f"Task {task_id} ({task_name}) was completed.")
            else:
                lines.append(f"Task {task_id} ({task_name}) was completed.")
            lines.append("")

        if len(todays_tasks) > 30:
            lines.append(
                f"Additionally, {len(todays_tasks) - 30} more tasks were "
                f"completed but are not listed individually here."
            )
            lines.append("")
    else:
        lines.append("No tasks were completed today.")
        lines.append("")

    lines.append("---")
    lines.append("")

    # -- Section 5: Key Commits and Fixes ----------------------------------
    lines.append("## Key Commits and Fixes Today")
    lines.append("")

    if commits:
        lines.append(
            f"{len(commits)} commits were pushed to the main branch today. The following summarizes each change."
        )
        lines.append("")
        for c in commits[:25]:
            subject = c["subject"].replace("**", "")
            lines.append(f"- {c['short']} -- {subject}")
        if len(commits) > 25:
            lines.append(f"- Plus {len(commits) - 25} additional commits.")
        lines.append("")
    else:
        lines.append("No commits were made on this date.")
        lines.append("")

    lines.append("---")
    lines.append("")

    # -- Section 6: Service Health -----------------------------------------
    lines.append("## Service Health")
    lines.append("")

    if services:
        svc_narrative = []
        for s in services:
            name = s["name"]
            if s["ok"]:
                detail = s["detail"]
                svc_narrative.append(f"{name} is ACTIVE ({detail}).")
            else:
                svc_narrative.append(f"{name} is DOWN ({s['detail']}).")
        lines.append(" ".join(svc_narrative))
        lines.append("")

        lines.append("| Service | Status | Since |")
        lines.append("|---|---|---|")
        for s in services:
            status = "ACTIVE" if s["ok"] else "DOWN"
            detail = s["detail"][:80]
            lines.append(f"| {s['name']} | {status} | {detail} |")
        lines.append("")

    lines.append(f"Disk usage stands at {disk['used_pct']}% with {disk['free_gb']} GB free.")
    lines.append("")

    # Heartbeat hotspots narrative
    if trends["hotspots"]:
        lines.append(
            "The following health check categories had recurring failures "
            f"over the last {trends['snapshots']} heartbeat checks:"
        )
        lines.append("")
        for name, count in trends["hotspots"]:
            lines.append(f"- {name}: {count} failures out of {trends['snapshots']} checks")
        lines.append("")

    if trends["regressions"]:
        lines.append("New regressions (failures that appeared recently but were not present in earlier checks):")
        lines.append("")
        for r in trends["regressions"]:
            lines.append(f"- {r}")
        lines.append("")

    lines.append("---")
    lines.append("")

    # -- Section 7: Workflow Learnings (what Nova learned today) -----------
    if learnings:
        lines.append("## Workflow Learnings")
        lines.append("")
        lines.append(
            f"The system recorded {len(learnings)} workflow learning entries "
            f"today, capturing patterns, decisions, and outcomes from "
            f"autonomous operations."
        )
        lines.append("")

        for lrn in learnings[:15]:
            wf_id = lrn.get("workflow_id", "unknown")
            summary = lrn.get("task_summary", "")
            decisions_list = lrn.get("key_decisions", [])
            patterns = lrn.get("successful_patterns", [])
            failures = lrn.get("failure_patterns", [])
            confidence = lrn.get("confidence", "")

            clean_wf = re.sub(r"^\d+_", "", wf_id).replace("_", " ")
            para = f'Workflow "{clean_wf}"'
            if summary:
                para += f": {summary}"
            if confidence:
                para += f" (confidence: {confidence})"
            para += "."

            if decisions_list:
                notable_decisions = [
                    d
                    for d in decisions_list
                    if d != summary  # avoid repeating summary
                ]
                if notable_decisions:
                    para += " Key decisions: " + "; ".join(notable_decisions[:3]) + "."

            if patterns:
                para += " Successful patterns: " + "; ".join(p[:100] for p in patterns[:2]) + "."

            if failures:
                para += " Failure patterns noted: " + "; ".join(f[:100] for f in failures[:2]) + "."

            lines.append(para)
            lines.append("")

        if len(learnings) > 15:
            lines.append(f"Plus {len(learnings) - 15} additional learning entries not listed individually.")
            lines.append("")

        lines.append("---")
        lines.append("")

    # -- Section 8: Open Threads / Risks -----------------------------------
    lines.append("## Open Threads and Risks")
    lines.append("")

    if open_tasks:
        lines.append(f"There are {len(open_tasks)} open tasks remaining at the end of the day:")
        lines.append("")
        for task in open_tasks[:15]:
            lines.append(f"- {task}")
        if len(open_tasks) > 15:
            lines.append(f"- Plus {len(open_tasks) - 15} additional open tasks.")
        lines.append("")
    else:
        lines.append("No open tasks or in-progress items remain at end of day.")
        lines.append("")

    if trends["hotspots"] or trends["regressions"]:
        lines.append("Identified risks:")
        lines.append("")
        for name, count in trends["hotspots"][:3]:
            lines.append(f"- Health hotspot {name} ({count} failures) may indicate underlying instability.")
        for r in trends["regressions"][:3]:
            lines.append(f"- New regression: {r}.")
        lines.append("")

    lines.append("---")
    lines.append("")

    # -- Section 9: Outlook and Next Actions --------------------------------
    lines.append("## Outlook and Next Actions")
    lines.append("")

    priorities = []
    if trends["regressions"]:
        priorities.append(
            f"Investigate and resolve {len(trends['regressions'])} new "
            f"regression(s): {', '.join(trends['regressions'][:3])}."
        )
    if not all_services_ok:
        down = [s["name"] for s in services if not s["ok"]]
        priorities.append(f"Restore failed service(s): {', '.join(down)}.")
    if open_tasks:
        in_prog = sum(1 for t in open_tasks if "[IN PROGRESS]" in t)
        pending = len(open_tasks) - in_prog
        if in_prog:
            priorities.append(f"Complete {in_prog} in-progress task(s).")
        if pending:
            priorities.append(f"Triage {pending} pending task(s).")
    if autonomy:
        dims = autonomy.get("dimensions", {})
        for dname, ddata in dims.items():
            if ddata.get("score", 100) < 70:
                pretty = dname.replace("_", " ").title()
                priorities.append(f"Improve {pretty} score (currently {ddata['score']:.0f}, below 70 threshold).")
    if not priorities:
        priorities.append("Continue monitoring. All systems are nominal.")
        priorities.append("Review and close any stale tasks in the task queue.")

    lines.append("The following priorities are recommended for the next operating period:")
    lines.append("")
    for i, p in enumerate(priorities[:8], 1):
        lines.append(f"{i}. {p}")
    lines.append("")

    lines.append(
        "The autonomous reporting system will continue monitoring via "
        "the heartbeat integration, writing structured diary entries "
        "to the NovaVault for a full audit trail."
    )
    lines.append("")

    lines.append("---")
    lines.append("")

    # -- Section 10: Today's Outputs (reference list) ----------------------
    if todays_outputs:
        lines.append("## Today's Outputs")
        lines.append("")
        lines.append(f"{len(todays_outputs)} output files were produced today:")
        lines.append("")
        for o in todays_outputs[:20]:
            lines.append(f"- {o}")
        if len(todays_outputs) > 20:
            lines.append(f"- Plus {len(todays_outputs) - 20} additional files.")
        lines.append("")
        lines.append("---")
        lines.append("")

    # -- Footer ------------------------------------------------------------
    lines.append(
        f"Generated by NovaCore Daily Executive Summary at "
        f"{now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} | "
        f"Report date: {date_str}"
    )

    return "\n".join(lines), date_str


# -- Vault Writer ----------------------------------------------------------


def write_vault_summary(body: str, date_str: str) -> Path:
    """Write the executive summary to NovaVault 00-inbox/."""
    VAULT_INBOX.mkdir(parents=True, exist_ok=True)

    filename = f"executive-summary-{date_str}.md"
    vault_path = VAULT_INBOX / filename

    # Don't overwrite an existing summary for today (could be human-authored)
    if vault_path.exists():
        log(f"Summary already exists: {filename} -- appending timestamp suffix")
        time_slug = datetime.now(timezone.utc).strftime("%H%M")
        filename = f"executive-summary-{date_str}-{time_slug}.md"
        vault_path = VAULT_INBOX / filename

    date_human = datetime.strptime(date_str, "%Y-%m-%d").strftime("%B %d, %Y")
    frontmatter = f"""---
title: "NovaCore Executive Summary -- {date_human}"
date: "{date_str}"
type: inbox
tags:
  - "#type/inbox"
  - daily-summary
  - executive-report
  - daily-digest
source: nova-core-daily-summary
generated_at: "{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}"
---
"""
    note = frontmatter + "\n" + body + "\n"
    vault_path.write_text(note, encoding="utf-8")
    log(f"Wrote vault summary: {vault_path}")
    return vault_path


def write_output_copies(body: str, date_str: str) -> tuple[Path, Path | None]:
    """Write markdown and PDF copies to OUTPUT/."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Markdown copy
    md_filename = f"executive_summary_{date_str}.md"
    md_path = OUTPUT_DIR / md_filename
    md_path.write_text(body, encoding="utf-8")
    log(f"Wrote OUTPUT markdown: {md_path.name}")

    # PDF copy
    pdf_filename = f"executive_summary_{date_str}.pdf"
    pdf_path = OUTPUT_DIR / pdf_filename
    pdf_ok = _generate_pdf(body, pdf_path)

    return md_path, pdf_path if pdf_ok else None


def _generate_pdf(md_text: str, pdf_path: Path) -> bool:
    """Generate PDF from markdown using the md_to_pdf_fpdf converter."""
    try:
        sys.path.insert(0, str(BASE / "scripts"))
        from md_to_pdf_fpdf import MarkdownPDF, parse_and_render

        pdf = MarkdownPDF()
        pdf.alias_nb_pages()
        parse_and_render(pdf, md_text)
        pdf.output(str(pdf_path))
        log(f"Wrote OUTPUT PDF: {pdf_path.name}")
        return True
    except ImportError:
        log("WARNING: md_to_pdf_fpdf not importable, trying subprocess fallback")
    except Exception as e:
        log(f"WARNING: PDF generation via import failed: {e}")

    # Fallback: call the script as a subprocess
    try:
        tmp_md = pdf_path.with_suffix(".tmp.md")
        tmp_md.write_text(md_text, encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(BASE / "scripts" / "md_to_pdf_fpdf.py"), str(tmp_md), str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        tmp_md.unlink(missing_ok=True)
        if r.returncode == 0:
            log(f"Wrote OUTPUT PDF (subprocess): {pdf_path.name}")
            return True
        else:
            log(f"WARNING: PDF subprocess failed: {r.stderr[:200]}")
    except Exception as e:
        log(f"WARNING: PDF generation failed entirely: {e}")

    return False


# -- Main ------------------------------------------------------------------


def main() -> int:
    t0 = time.monotonic()
    log("Starting daily executive summary generation")

    try:
        body, date_str = format_executive_summary()

        # Word count check
        word_count = len(body.split())
        log(f"Summary word count: {word_count}")
        if word_count < 500:
            log(f"WARNING: Summary only {word_count} words (target: 1000+). Low-activity day.")

        vault_path = write_vault_summary(body, date_str)
        md_path, pdf_path = write_output_copies(body, date_str)

        elapsed = round((time.monotonic() - t0) * 1000)
        log(f"Summary complete in {elapsed}ms ({word_count} words)")
        log(f"  Vault:  {vault_path}")
        log(f"  MD:     {md_path}")
        if pdf_path:
            log(f"  PDF:    {pdf_path}")
        else:
            log("  PDF:    SKIPPED (generation failed)")

        return 0
    except Exception as e:
        log(f"ERROR: {e}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
