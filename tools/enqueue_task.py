#!/usr/bin/env python3
"""CLI tool for writing tasks into the TASKS/ queue.

Usage:
    python3 tools/enqueue_task.py "Task Title" "Task body text"
    python3 tools/enqueue_task.py "Task Title" "Body" --priority high --category execute
    python3 tools/enqueue_task.py "Task Title" --body-file /path/to/body.md
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TASKS_DIR = Path(__file__).resolve().parent.parent / "TASKS"


def next_sequence() -> int:
    if not TASKS_DIR.exists():
        return 1
    max_seq = 0
    for f in TASKS_DIR.iterdir():
        m = re.match(r"^(\d+)_", f.name)
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def slugify(text: str, max_len: int = 80) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", text.strip())[:max_len].strip("_")


def has_pending(slug_fragment: str) -> bool:
    if not TASKS_DIR.exists():
        return False
    frag = slug_fragment.lower()
    return any(frag in f.name.lower() and f.suffix in (".md", ".inprogress") for f in TASKS_DIR.iterdir())


def enqueue(
    title: str,
    body: str,
    *,
    priority: str = "medium",
    category: str = "",
    source: str = "claude-session",
    skip_dedup: bool = False,
) -> Path | None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)

    slug = slugify(title)
    if not skip_dedup and has_pending(slug):
        print(f"SKIP: duplicate pending task matching '{slug}'", file=sys.stderr)
        return None

    seq = next_sequence()
    filename = f"{seq:04d}_{slug}.md"
    path = TASKS_DIR / filename

    now = datetime.now(timezone.utc).isoformat()
    frontmatter = (
        f"---\n"
        f"priority: {priority}\n"
        f"category: {category}\n"
        f"auto_execute: true\n"
        f'generated_at: "{now}"\n'
        f"source: {source}\n"
        f"---\n\n"
    )
    content = frontmatter + f"# {title}\n\n{body}\n"

    fd, tmp = tempfile.mkstemp(dir=str(TASKS_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    print(f"CREATED: {path}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Enqueue a task into TASKS/")
    parser.add_argument("title", help="Task title")
    parser.add_argument("body", nargs="?", default="", help="Task body text")
    parser.add_argument("--body-file", help="Read body from a file instead")
    parser.add_argument("--priority", default="medium", choices=["high", "medium", "low"])
    parser.add_argument("--category", default="", help="Task category (research/plan/execute/repair/validate)")
    parser.add_argument("--source", default="claude-session", help="Source identifier")
    parser.add_argument("--skip-dedup", action="store_true", help="Skip duplicate detection")
    args = parser.parse_args()

    body = args.body
    if args.body_file:
        body = Path(args.body_file).read_text()

    if not body.strip():
        print("ERROR: body is empty (provide as argument or via --body-file)", file=sys.stderr)
        sys.exit(1)

    result = enqueue(
        args.title,
        body,
        priority=args.priority,
        category=args.category,
        source=args.source,
        skip_dedup=args.skip_dedup,
    )
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
