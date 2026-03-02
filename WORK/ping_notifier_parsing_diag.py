#!/usr/bin/env python3
"""Diagnostic: exercise telegram_notifier parsing against all OUTPUT files.

Reports parsing hits/misses per field and identifies edge-case failures.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow importing from nova-core root
sys.path.insert(0, str(Path("/home/nova/nova-core")))

from telegram_notifier import parse_task_report, compute_metrics, worker_log_for_output

OUTPUT = Path("/home/nova/nova-core/OUTPUT")


def run_diag() -> dict:
    results = []
    files = sorted(OUTPUT.glob("*.md"), key=lambda p: p.name)

    for f in files:
        md = f.read_text(encoding="utf-8", errors="replace")
        info = parse_task_report(md, output_name=f.name)
        metrics = compute_metrics(f)
        wlog = worker_log_for_output(f, task_id=info.get("task_id"))

        entry = {
            "file": f.name,
            "is_tg": f.name.startswith("tg_"),
            "parsed": info,
            "metrics": metrics,
            "worker_log": str(wlog) if wlog else None,
            "issues": [],
        }

        # --- Check each field ---
        if not info["task_id"]:
            entry["issues"].append("task_id: NOT PARSED")
        if not info["completed"]:
            entry["issues"].append("completed: NOT PARSED")
        if not info["summary"]:
            entry["issues"].append("summary: NOT PARSED")
        if not info["files"]:
            entry["issues"].append("files: EMPTY (no files extracted)")

        # Metrics checks (only for tg_ files)
        if entry["is_tg"]:
            if metrics["latency_sec"] is None:
                entry["issues"].append("metrics: latency could not be computed")
            if metrics["queued_ts"] is None:
                entry["issues"].append("metrics: queued_ts not parsed from filename")

        results.append(entry)

    return {"total": len(files), "results": results}


def format_report(diag: dict) -> str:
    lines = [
        "# Notifier Parsing Diagnostic Report",
        "",
        f"Total output files scanned: {diag['total']}",
        "",
    ]

    ok_count = 0
    issue_count = 0

    for r in diag["results"]:
        tag = "tg" if r["is_tg"] else "sys"
        lines.append(f"## [{tag}] {r['file']}")
        lines.append("")
        p = r["parsed"]
        lines.append(f"- task_id:   {p['task_id'] or '(none)'}")
        lines.append(f"- completed: {p['completed'] or '(none)'}")
        lines.append(f"- summary:   {(p['summary'] or '(none)')[:80]}...")
        lines.append(f"- files:     {p['files'] or '(none)'}")

        m = r["metrics"]
        lines.append(f"- queued_ts: {m['queued_ts'] or 'n/a'}")
        lines.append(f"- done_ts:   {m['done_ts'] or 'n/a'}")
        lines.append(f"- latency:   {m['latency_sec']}s" if m["latency_sec"] is not None else "- latency:   n/a")
        lines.append(f"- size:      {m['size_bytes']} bytes")
        lines.append(f"- worker_log: {r['worker_log'] or '(not found)'}")

        if r["issues"]:
            issue_count += 1
            lines.append(f"- **ISSUES:** {'; '.join(r['issues'])}")
        else:
            ok_count += 1
            lines.append("- Status: ALL FIELDS PARSED OK")
        lines.append("")

    lines.append("---")
    lines.append(f"**Summary:** {ok_count} fully parsed, {issue_count} with issues, {diag['total']} total")
    return "\n".join(lines)


if __name__ == "__main__":
    diag = run_diag()
    report = format_report(diag)
    print(report)
