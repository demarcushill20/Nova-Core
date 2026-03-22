#!/usr/bin/env python3
"""State hygiene: trim unbounded JSONL files and clean stale STATE/ artifacts.

NovaCore accumulates state in several append-only JSONL files and directories
that have no built-in rotation. This script trims them to sane retention
windows, preventing unbounded disk growth during autonomous operation.

Usage:
    python3 scripts/state_hygiene.py              # dry-run (default)
    python3 scripts/state_hygiene.py --apply       # actually trim
    python3 scripts/state_hygiene.py --apply --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

logger = logging.getLogger("state_hygiene")

BASE = Path(os.environ.get("NOVA_BASE", Path(__file__).resolve().parent.parent))
STATE = BASE / "STATE"
LOGS = BASE / "LOGS"

# Retention policies (lines to keep = most recent N entries)
JSONL_RETENTION: dict[str, int] = {
    "heartbeat_metrics.jsonl": 200,  # ~2 days at 5-min intervals
    "max_plan_ledger.jsonl": 300,  # ~10 days of plan operations
    "tool_audit.jsonl": 500,  # ~3 days of tool calls
    "rss_history.jsonl": 200,  # ~2 days at 5-min intervals
}

# Directory retention: max age in days for files
DIR_RETENTION: dict[str, int] = {
    "notified": 3,  # notification receipts older than 3 days
    "improvement_runs": 7,  # improvement run records older than 7 days
}

# Working memory archive: max size in bytes before trimming
ARCHIVE_MAX_BYTES = 512 * 1024  # 512 KB


class TrimResult(NamedTuple):
    target: str
    before_lines: int
    after_lines: int
    bytes_saved: int


class CleanResult(NamedTuple):
    target: str
    files_removed: int
    bytes_saved: int


def trim_jsonl(path: Path, keep_lines: int, apply: bool) -> TrimResult | None:
    """Trim a JSONL file to keep only the last N lines."""
    if not path.exists():
        logger.debug("Skipping %s (not found)", path)
        return None

    lines = path.read_text().splitlines()
    total = len(lines)

    if total <= keep_lines:
        logger.debug("Skipping %s (%d lines <= %d limit)", path.name, total, keep_lines)
        return None

    before_size = path.stat().st_size
    kept = lines[-keep_lines:]

    if apply:
        # Atomic write: write to temp file then rename
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(kept) + "\n")
            os.replace(tmp, path)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        after_size = path.stat().st_size
    else:
        after_size = sum(len(line) + 1 for line in kept)

    return TrimResult(
        target=str(path.relative_to(BASE)),
        before_lines=total,
        after_lines=keep_lines,
        bytes_saved=before_size - after_size,
    )


def clean_old_files(directory: Path, max_age_days: int, apply: bool) -> CleanResult | None:
    """Remove files older than max_age_days from a directory."""
    if not directory.exists() or not directory.is_dir():
        logger.debug("Skipping %s (not found)", directory)
        return None

    cutoff = time.time() - (max_age_days * 86400)
    files_to_remove = []
    bytes_to_free = 0

    for f in directory.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            files_to_remove.append(f)
            bytes_to_free += f.stat().st_size

    if not files_to_remove:
        logger.debug("Skipping %s (no files older than %d days)", directory.name, max_age_days)
        return None

    if apply:
        for f in files_to_remove:
            f.unlink()

    return CleanResult(
        target=str(directory.relative_to(BASE)),
        files_removed=len(files_to_remove),
        bytes_saved=bytes_to_free,
    )


def trim_archive_json(path: Path, max_bytes: int, apply: bool) -> TrimResult | None:
    """Trim a JSON array file by removing oldest entries until under max_bytes."""
    if not path.exists():
        return None

    before_size = path.stat().st_size
    if before_size <= max_bytes:
        logger.debug("Skipping %s (%d bytes <= %d limit)", path.name, before_size, max_bytes)
        return None

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError):
        logger.warning("Cannot parse %s — skipping", path)
        return None

    if not isinstance(data, list):
        logger.debug("Skipping %s (not a JSON array)", path.name)
        return None

    before_count = len(data)
    # Remove oldest entries (front of array) until under budget
    while len(data) > 1:
        candidate = json.dumps(data, separators=(",", ":"))
        if len(candidate.encode()) <= max_bytes:
            break
        data.pop(0)

    if apply:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        after_size = path.stat().st_size
    else:
        after_size = len(json.dumps(data, separators=(",", ":")).encode())

    return TrimResult(
        target=str(path.relative_to(BASE)),
        before_lines=before_count,
        after_lines=len(data),
        bytes_saved=before_size - after_size,
    )


def run_hygiene(apply: bool = False, verbose: bool = False) -> dict:
    """Run all state hygiene operations. Returns summary dict."""
    results: dict[str, Any] = {"trimmed": [], "cleaned": [], "skipped": []}
    total_saved = 0

    # 1. Trim JSONL state files
    for filename, keep in JSONL_RETENTION.items():
        path = STATE / filename
        result = trim_jsonl(path, keep, apply)
        if result:
            results["trimmed"].append(result)
            total_saved += result.bytes_saved
            logger.info(
                "%s %s: %d → %d lines (saved %s)",
                "Trimmed" if apply else "Would trim",
                result.target,
                result.before_lines,
                result.after_lines,
                _fmt_bytes(result.bytes_saved),
            )
        else:
            results["skipped"].append(filename)

    # 2. Clean old directory entries
    for dirname, max_age in DIR_RETENTION.items():
        dirpath = STATE / dirname
        clean_result = clean_old_files(dirpath, max_age, apply)
        if clean_result:
            results["cleaned"].append(clean_result)
            total_saved += clean_result.bytes_saved
            logger.info(
                "%s %s: %d files (saved %s)",
                "Cleaned" if apply else "Would clean",
                clean_result.target,
                clean_result.files_removed,
                _fmt_bytes(clean_result.bytes_saved),
            )
        else:
            results["skipped"].append(dirname)

    # 3. Trim working_memory_archive.json
    archive = STATE / "working_memory_archive.json"
    archive_result = trim_archive_json(archive, ARCHIVE_MAX_BYTES, apply)
    if archive_result:
        results["trimmed"].append(archive_result)
        total_saved += archive_result.bytes_saved
        logger.info(
            "%s %s: %d → %d entries (saved %s)",
            "Trimmed" if apply else "Would trim",
            archive_result.target,
            archive_result.before_lines,
            archive_result.after_lines,
            _fmt_bytes(archive_result.bytes_saved),
        )
    else:
        results["skipped"].append("working_memory_archive.json")

    results["total_bytes_saved"] = total_saved
    results["mode"] = "applied" if apply else "dry-run"
    results["timestamp"] = datetime.now(timezone.utc).isoformat()

    return results


def _fmt_bytes(n: int) -> str:
    """Format bytes as human-readable string."""
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    else:
        return f"{n / (1024 * 1024):.1f} MB"


def main() -> None:
    parser = argparse.ArgumentParser(description="NovaCore state hygiene")
    parser.add_argument("--apply", action="store_true", help="Actually trim (default: dry-run)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s  %(message)s")

    results = run_hygiene(apply=args.apply, verbose=args.verbose)

    print(f"\n{'=' * 50}")
    print(f"State Hygiene — {results['mode']}")
    print(f"{'=' * 50}")

    if results["trimmed"]:
        print("\nTrimmed:")
        for r in results["trimmed"]:
            if isinstance(r, TrimResult):
                print(f"  {r.target}: {r.before_lines} → {r.after_lines} entries ({_fmt_bytes(r.bytes_saved)} saved)")

    if results["cleaned"]:
        print("\nCleaned:")
        for r in results["cleaned"]:
            if isinstance(r, CleanResult):
                print(f"  {r.target}: {r.files_removed} files removed ({_fmt_bytes(r.bytes_saved)} saved)")

    if results["skipped"]:
        print(f"\nSkipped (within limits): {', '.join(results['skipped'])}")

    print(f"\nTotal savings: {_fmt_bytes(results['total_bytes_saved'])}")

    if not args.apply and results["total_bytes_saved"] > 0:
        print("\nRun with --apply to execute these changes.")


if __name__ == "__main__":
    main()
