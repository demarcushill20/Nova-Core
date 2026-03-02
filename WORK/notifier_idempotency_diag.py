#!/usr/bin/env python3
"""Diagnostic: verify notifier idempotency — each OUTPUT file has at most one marker."""

import sys
sys.path.insert(0, "/home/nova/nova-core")

from pathlib import Path

OUTPUT = Path("/home/nova/nova-core/OUTPUT")
NOTIFIED_DIR = Path("/home/nova/nova-core/STATE/notified")


def main():
    # 1. List all tg_*.md output files
    outputs = sorted(OUTPUT.glob("tg_*.md"))
    markers = {m.stem.removesuffix(".notified"): m for m in NOTIFIED_DIR.glob("*.notified")} if NOTIFIED_DIR.exists() else {}

    print(f"OUTPUT files (tg_*): {len(outputs)}")
    print(f"Marker files:        {len(markers)}")
    print()

    dupes = 0
    missing = 0
    for f in outputs:
        name = f.name
        marker = NOTIFIED_DIR / f"{name}.notified"
        # Check if marker file exists
        has_marker = marker.exists()
        # Check if there are multiple marker files for same base (shouldn't happen)
        status = "OK (marker)" if has_marker else "NO MARKER (unsent or new)"
        print(f"  [{status:30s}] {name}")
        if not has_marker:
            missing += 1

    # Check for duplicate markers (same filename, multiple .notified files)
    seen = {}
    if NOTIFIED_DIR.exists():
        for m in NOTIFIED_DIR.glob("*.notified"):
            base = m.name.removesuffix(".notified")
            seen.setdefault(base, []).append(m)
        for base, files in seen.items():
            if len(files) > 1:
                print(f"  DUPLICATE MARKERS: {base} -> {len(files)} files!")
                dupes += 1

    print()
    print(f"Results: {len(outputs)} outputs, {missing} without marker, {dupes} duplicates")
    if dupes == 0:
        print("PASS: No duplicate markers found.")
    else:
        print("FAIL: Duplicate markers detected!")


if __name__ == "__main__":
    main()
