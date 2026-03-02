#!/usr/bin/env python3
"""Diagnostic: list PID files in STATE/running/ and check if PIDs are alive."""

import os
from pathlib import Path

RUNNING_DIR = Path("/home/nova/nova-core/STATE/running")


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def main() -> None:
    if not RUNNING_DIR.exists():
        print(f"{RUNNING_DIR} does not exist.")
        return

    pid_files = sorted(RUNNING_DIR.glob("*.pid"))
    if not pid_files:
        print("No PID files found — no tasks currently running.")
        return

    print(f"{'PID FILE':<60} {'PID':>8}  {'STATUS'}")
    print("-" * 80)
    for pf in pid_files:
        try:
            pid = int(pf.read_text(encoding="utf-8").strip())
            alive = pid_is_alive(pid)
            status = "ALIVE" if alive else "STALE (not running)"
        except (ValueError, OSError) as e:
            pid = -1
            status = f"ERROR ({e})"
        print(f"{pf.name:<60} {pid:>8}  {status}")


if __name__ == "__main__":
    main()
