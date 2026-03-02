#!/usr/bin/env python3
"""Diagnostic: verify every OUTPUT file produces a non-empty summary."""

import sys
sys.path.insert(0, "/home/nova/nova-core")

from pathlib import Path
from telegram_notifier import parse_task_report

OUTPUT = Path("/home/nova/nova-core/OUTPUT")

def main():
    files = sorted(OUTPUT.glob("*.md"))
    if not files:
        print("No OUTPUT files found.")
        return

    passed = 0
    failed = 0

    for f in files:
        txt = f.read_text(encoding="utf-8", errors="replace")
        info = parse_task_report(txt, output_name=f.name)
        summary = info.get("summary")
        has_summary = bool(summary and summary != "(no summary available)")

        status = "OK" if has_summary else "EMPTY"
        if has_summary:
            passed += 1
        else:
            failed += 1

        preview = (summary or "")[:80].replace("\n", " ")
        print(f"[{status:5s}] {f.name}")
        print(f"        summary: {preview!r}")
        print()

    print(f"--- RESULTS: {passed} OK / {failed} EMPTY / {passed + failed} total ---")
    if failed:
        print("WARNING: some files still have no summary.")
    else:
        print("ALL files produced a non-empty summary.")


if __name__ == "__main__":
    main()
