#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASKS_DIR = ROOT / "TASKS"
OUTPUT_DIR = ROOT / "OUTPUT"
LOGS_DIR = ROOT / "LOGS"

def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")

def log(msg: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}\n"
    (LOGS_DIR / "runner.log").open("a", encoding="utf-8").write(line)

def ensure_dirs() -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

def process_task_file(task_path: Path) -> Path:
    content = task_path.read_text(encoding="utf-8")
    stamp = now_stamp()
    out_path = OUTPUT_DIR / f"{task_path.stem}__{stamp}.md"
    out_body = (
        f"# Output for: {task_path.name}\n\n"
        f"- Processed: {dt.datetime.now().isoformat(timespec='seconds')}\n"
        f"- Host: {os.uname().nodename}\n\n"
        f"## Task Content\n\n"
        f"{content}\n"
    )
    out_path.write_text(out_body, encoding="utf-8")
    return out_path

def main() -> int:
    ensure_dirs()
    task_files = sorted(TASKS_DIR.glob("*.md"))
    if not task_files:
        print("No task files found in TASKS/.")
        return 0

    log(f"Found {len(task_files)} task(s). Starting processing.")
    for task_path in task_files:
        try:
            log(f"Processing {task_path.name}")
            out_path = process_task_file(task_path)
            done_path = task_path.with_suffix(task_path.suffix + ".done")
            task_path.rename(done_path)
            log(f"Wrote {out_path.name} and marked task done -> {done_path.name}")
            print(f"Processed {task_path.name} -> {out_path.name}")
        except Exception as e:
            log(f"ERROR processing {task_path.name}: {e!r}")
            print(f"ERROR processing {task_path.name}: {e!r}")
    log("Processing complete.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
