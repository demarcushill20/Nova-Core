#!/usr/bin/env python3
"""Task archive: move terminal-state task files (.done, .failed) to TASKS/archive/.

The TASKS/ directory accumulates completed and failed task files indefinitely.
This script archives old ones to reduce directory noise while preserving history.

Usage:
    python3 scripts/task_archive.py              # dry-run (default)
    python3 scripts/task_archive.py --apply       # actually move files
    python3 scripts/task_archive.py --apply --days 7   # archive tasks older than 7 days
    python3 scripts/task_archive.py --stats       # show directory statistics only
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("task_archive")

BASE = Path(os.environ.get("NOVA_BASE", Path(__file__).resolve().parent.parent))
TASKS = BASE / "TASKS"
ARCHIVE = TASKS / "archive"

# Terminal lifecycle suffixes eligible for archival
TERMINAL_SUFFIXES = (".done", ".failed", ".cancelled")

# Never archive these (in-progress work or pending tasks)
EXCLUDED_SUFFIXES = (".md", ".inprogress")


@dataclass
class ArchiveStats:
    scanned: int = 0
    archived: int = 0
    skipped: int = 0
    bytes_moved: int = 0
    by_suffix: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def get_task_stats() -> dict[str, int]:
    """Return counts of tasks by lifecycle state."""
    stats: dict[str, int] = {
        "pending (.md)": 0,
        "in-progress": 0,
        "done": 0,
        "failed": 0,
        "cancelled": 0,
        "other": 0,
        "archived": 0,
        "total": 0,
    }

    for p in TASKS.iterdir():
        if p.is_dir():
            if p.name == "archive":
                stats["archived"] = sum(1 for _ in p.iterdir() if _.is_file())
            elif p.name == "_completed":
                stats["archived"] += sum(1 for _ in p.iterdir() if _.is_file())
            continue

        stats["total"] += 1
        name = p.name

        if name.endswith(".inprogress"):
            stats["in-progress"] += 1
        elif name.endswith(".done"):
            stats["done"] += 1
        elif name.endswith(".failed"):
            stats["failed"] += 1
        elif name.endswith(".cancelled"):
            stats["cancelled"] += 1
        elif name.endswith(".md"):
            stats["pending (.md)"] += 1
        else:
            stats["other"] += 1

    return stats


def archive_tasks(max_age_days: int = 3, apply: bool = False) -> ArchiveStats:
    """Archive terminal-state tasks older than max_age_days.

    Args:
        max_age_days: Only archive tasks whose mtime is older than this many days.
        apply: If False (default), dry-run only. If True, actually move files.

    Returns:
        ArchiveStats with summary of what was (or would be) done.
    """
    stats = ArchiveStats()
    cutoff = time.time() - (max_age_days * 86400)

    if apply:
        ARCHIVE.mkdir(parents=True, exist_ok=True)

    for p in sorted(TASKS.iterdir()):
        if p.is_dir():
            continue

        stats.scanned += 1

        # Only archive terminal-state files
        is_terminal = any(p.name.endswith(sfx) for sfx in TERMINAL_SUFFIXES)
        if not is_terminal:
            stats.skipped += 1
            continue

        # Only archive files older than the cutoff
        try:
            mtime = p.stat().st_mtime
        except OSError:
            stats.errors.append(f"Cannot stat {p.name}")
            continue

        if mtime >= cutoff:
            stats.skipped += 1
            continue

        # Determine suffix for reporting
        for sfx in TERMINAL_SUFFIXES:
            if p.name.endswith(sfx):
                stats.by_suffix[sfx] = stats.by_suffix.get(sfx, 0) + 1
                break

        file_size = p.stat().st_size
        dest = ARCHIVE / p.name

        if apply:
            try:
                # Handle name collision in archive (shouldn't happen, but be safe)
                if dest.exists():
                    # Append a counter
                    stem = dest.stem
                    suffix = dest.suffix
                    counter = 1
                    while dest.exists():
                        dest = ARCHIVE / f"{stem}_{counter}{suffix}"
                        counter += 1

                p.rename(dest)
                stats.archived += 1
                stats.bytes_moved += file_size
                logger.info("Archived: %s → archive/%s", p.name, dest.name)
            except OSError as e:
                stats.errors.append(f"Failed to archive {p.name}: {e}")
        else:
            stats.archived += 1
            stats.bytes_moved += file_size
            logger.debug("Would archive: %s (%s)", p.name, _fmt_bytes(file_size))

    return stats


def consolidate_completed(apply: bool = False) -> int:
    """Move files from TASKS/_completed/ into TASKS/archive/ for a single archive location."""
    completed_dir = TASKS / "_completed"
    if not completed_dir.exists():
        return 0

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    moved = 0

    for p in completed_dir.iterdir():
        if not p.is_file():
            continue

        dest = ARCHIVE / p.name
        if apply:
            try:
                if dest.exists():
                    stem = dest.stem
                    suffix = dest.suffix
                    counter = 1
                    while dest.exists():
                        dest = ARCHIVE / f"{stem}_{counter}{suffix}"
                        counter += 1
                p.rename(dest)
                moved += 1
            except OSError as e:
                logger.warning("Failed to move %s: %s", p.name, e)
        else:
            moved += 1

    # Remove empty _completed dir if apply mode
    if apply and completed_dir.exists() and not any(completed_dir.iterdir()):
        completed_dir.rmdir()
        logger.info("Removed empty _completed/ directory")

    return moved


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    else:
        return f"{n / (1024 * 1024):.1f} MB"


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive completed/failed NovaCore tasks")
    parser.add_argument("--apply", action="store_true", help="Actually move files (default: dry-run)")
    parser.add_argument("--days", type=int, default=3, help="Archive tasks older than N days (default: 3)")
    parser.add_argument("--stats", action="store_true", help="Show directory statistics only")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s  %(message)s")

    # Always show current stats
    print(f"\n{'=' * 50}")
    print("TASKS/ Directory Statistics")
    print(f"{'=' * 50}")
    task_stats = get_task_stats()
    for key, value in task_stats.items():
        print(f"  {key:20s}: {value:5d}")

    if args.stats:
        return

    # Archive old terminal-state tasks
    print(f"\n{'=' * 50}")
    mode_label = "APPLY" if args.apply else "DRY-RUN"
    print(f"Task Archive — {mode_label} (threshold: {args.days} days)")
    print(f"{'=' * 50}")

    result = archive_tasks(max_age_days=args.days, apply=args.apply)

    print(f"\n  Scanned:  {result.scanned}")
    print(f"  Archived: {result.archived}")
    print(f"  Skipped:  {result.skipped}")
    print(f"  Space:    {_fmt_bytes(result.bytes_moved)}")

    if result.by_suffix:
        print("\n  By state:")
        for sfx, count in sorted(result.by_suffix.items()):
            print(f"    {sfx:15s}: {count}")

    if result.errors:
        print(f"\n  Errors ({len(result.errors)}):")
        for err in result.errors:
            print(f"    {err}")

    # Consolidate _completed/ into archive/
    completed_count = consolidate_completed(apply=args.apply)
    if completed_count:
        action = "Moved" if args.apply else "Would move"
        print(f"\n  {action} {completed_count} files from _completed/ → archive/")

    if not args.apply and (result.archived > 0 or completed_count > 0):
        print("\nRun with --apply to execute these changes.")

    # Post-archive stats
    if args.apply and result.archived > 0:
        print(f"\n{'=' * 50}")
        print("Post-Archive Statistics")
        print(f"{'=' * 50}")
        post_stats = get_task_stats()
        for key, value in post_stats.items():
            print(f"  {key:20s}: {value:5d}")


if __name__ == "__main__":
    main()
