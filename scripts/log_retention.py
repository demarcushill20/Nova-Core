#!/usr/bin/env python3
"""Log retention and disk hygiene for NovaCore.

Implements automated cleanup policies:
- Structured logs: compress after 3 days, delete after 14 days
- Progress history: rotate when >10MB
- .mypy_cache: remove if >200MB
- LOGS/backups: delete compressed logs older than 30 days

Run via cron or heartbeat: python3 scripts/log_retention.py [--dry-run]
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE / "LOGS"
STATE_DIR = BASE / "STATE"
BACKUPS_DIR = LOGS_DIR / "backups"

# Policy thresholds (seconds)
STRUCTURED_COMPRESS_AGE = 3 * 86400  # 3 days → compress
STRUCTURED_DELETE_AGE = 14 * 86400  # 14 days → delete compressed
BACKUP_DELETE_AGE = 30 * 86400  # 30 days → delete from backups
PROGRESS_ROTATE_SIZE = 10 * 1024 * 1024  # 10 MB → rotate
MYPY_CACHE_LIMIT = 200 * 1024 * 1024  # 200 MB → clear

# STATE sub-directory retention (seconds)
INVESTIGATIONS_MAX_AGE = 3 * 86400  # 3 days → compress & archive
NOTIFIED_MAX_AGE = 7 * 86400  # 7 days → delete (tiny marker files)
IMPROVEMENT_RUNS_MAX_AGE = 7 * 86400  # 7 days → delete


def _file_age(p: Path) -> float:
    """Seconds since file was last modified."""
    try:
        return time.time() - p.stat().st_mtime
    except OSError:
        return 0


def _dir_size(p: Path) -> int:
    """Total bytes in directory tree."""
    total = 0
    if not p.is_dir():
        return 0
    for f in p.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def compress_old_structured_logs(dry_run: bool = False) -> list[str]:
    """Compress structured.*.jsonl files older than 3 days."""
    actions = []
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    for p in sorted(LOGS_DIR.glob("structured.*.jsonl")):
        age = _file_age(p)
        if age > STRUCTURED_COMPRESS_AGE:
            dest = BACKUPS_DIR / f"{p.name}.gz"
            if dry_run:
                actions.append(f"[dry-run] compress {p.name} → backups/")
            else:
                with open(p, "rb") as f_in, gzip.open(dest, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                p.unlink()
                actions.append(f"compressed {p.name} → backups/ (age {age / 86400:.1f}d)")
    return actions


def prune_old_backups(dry_run: bool = False) -> list[str]:
    """Delete compressed backups older than 30 days."""
    actions: list[str] = []
    if not BACKUPS_DIR.is_dir():
        return actions

    for p in sorted(BACKUPS_DIR.glob("*.gz")):
        age = _file_age(p)
        if age > BACKUP_DELETE_AGE:
            size_kb = p.stat().st_size / 1024
            if dry_run:
                actions.append(f"[dry-run] delete {p.name} ({size_kb:.0f}KB, age {age / 86400:.0f}d)")
            else:
                p.unlink()
                actions.append(f"deleted {p.name} ({size_kb:.0f}KB, age {age / 86400:.0f}d)")
    return actions


def rotate_progress_history(dry_run: bool = False) -> list[str]:
    """Rotate progress_history.json when it exceeds size threshold."""
    actions: list[str] = []
    ph = STATE_DIR / "progress_history.json"
    if not ph.exists():
        return actions

    try:
        size = ph.stat().st_size
    except OSError:
        return actions

    if size < PROGRESS_ROTATE_SIZE:
        return actions

    size_mb = size / (1024 * 1024)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_name = f"progress_history_{ts}.json.gz"
    archive_dir = STATE_DIR / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / archive_name

    if dry_run:
        actions.append(f"[dry-run] rotate progress_history.json ({size_mb:.1f}MB)")
        return actions

    # Read, keep last 500 entries, archive the rest
    try:
        data = json.loads(ph.read_text())
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict) and "entries" in data:
            entries = data["entries"]
        else:
            entries = []

        # Archive full file
        with gzip.open(dest, "wt") as f:
            json.dump(data, f)

        # Keep only last 500 entries
        if len(entries) > 500:
            kept = entries[-500:]
            if isinstance(data, dict):
                data["entries"] = kept
                data["_rotated_at"] = ts
                data["_archived_to"] = str(dest)
            else:
                kept.append({"_rotated_at": ts, "_archived_to": str(dest)})
                data = kept
            ph.write_text(json.dumps(data))
            actions.append(
                f"rotated progress_history.json: {len(entries)}→{len(kept)} entries "
                f"({size_mb:.1f}MB→{ph.stat().st_size / (1024 * 1024):.1f}MB), "
                f"archived to {archive_name}"
            )
        else:
            actions.append(f"progress_history.json has {len(entries)} entries (<500), skipped rotation")
    except (json.JSONDecodeError, OSError) as exc:
        actions.append(f"ERROR rotating progress_history.json: {exc}")
    return actions


def clear_mypy_cache(dry_run: bool = False) -> list[str]:
    """Clear .mypy_cache if over size limit."""
    actions: list[str] = []
    cache_dir = BASE / ".mypy_cache"
    if not cache_dir.is_dir():
        return actions

    size = _dir_size(cache_dir)
    if size < MYPY_CACHE_LIMIT:
        return actions

    size_mb = size / (1024 * 1024)
    if dry_run:
        actions.append(f"[dry-run] clear .mypy_cache ({size_mb:.0f}MB)")
    else:
        shutil.rmtree(cache_dir, ignore_errors=True)
        actions.append(f"cleared .mypy_cache ({size_mb:.0f}MB)")
    return actions


def prune_old_investigations(dry_run: bool = False) -> list[str]:
    """Archive investigation files older than INVESTIGATIONS_MAX_AGE.

    Investigations accumulate at ~2 files per heartbeat (~every 10min).
    After 3 days they have no debugging value. Compress into daily archives.
    """
    actions: list[str] = []
    inv_dir = STATE_DIR / "investigations"
    if not inv_dir.is_dir():
        return actions

    archive_dir = STATE_DIR / "_archive" / "investigations"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Group old files by date prefix
    old_files: dict[str, list[Path]] = {}
    for p in inv_dir.iterdir():
        if not p.is_file() or not p.name.endswith(".json"):
            continue
        age = _file_age(p)
        if age > INVESTIGATIONS_MAX_AGE:
            # Extract date from mtime for grouping
            mtime = p.stat().st_mtime
            date_key = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y%m%d")
            old_files.setdefault(date_key, []).append(p)

    for date_key, files in sorted(old_files.items()):
        dest = archive_dir / f"investigations_{date_key}.jsonl.gz"
        total_kb = sum(f.stat().st_size for f in files) / 1024
        if dry_run:
            actions.append(f"[dry-run] archive {len(files)} investigations from {date_key} ({total_kb:.0f}KB)")
            continue
        # Append all JSON objects into a single JSONL gzip
        with gzip.open(dest, "at") as gz:
            for f in sorted(files, key=lambda x: x.name):
                try:
                    gz.write(f.read_text().strip() + "\n")
                except OSError:
                    pass
        for f in files:
            try:
                f.unlink()
            except OSError:
                pass
        actions.append(f"archived {len(files)} investigations from {date_key} ({total_kb:.0f}KB) → {dest.name}")

    return actions


def prune_old_notified(dry_run: bool = False) -> list[str]:
    """Delete notification marker files older than NOTIFIED_MAX_AGE.

    These are tiny files (~47 bytes) that track whether OUTPUT was sent to
    Telegram. Once older than a week they serve no purpose.
    """
    actions: list[str] = []
    notified_dir = STATE_DIR / "notified"
    if not notified_dir.is_dir():
        return actions

    old_count = 0
    for p in notified_dir.iterdir():
        if not p.is_file():
            continue
        if _file_age(p) > NOTIFIED_MAX_AGE:
            old_count += 1
            if not dry_run:
                try:
                    p.unlink()
                except OSError:
                    pass

    if old_count > 0:
        verb = "[dry-run] would delete" if dry_run else "deleted"
        actions.append(f"{verb} {old_count} old notification markers (>{NOTIFIED_MAX_AGE // 86400}d)")
    return actions


def prune_old_improvement_runs(dry_run: bool = False) -> list[str]:
    """Delete skill improvement run artifacts older than IMPROVEMENT_RUNS_MAX_AGE.

    These are per-run JSON snapshots from the skill optimizer. After a week,
    the optimizer has either succeeded or moved on.
    """
    actions: list[str] = []
    imp_dir = STATE_DIR / "improvement_runs"
    if not imp_dir.is_dir():
        return actions

    old_count = 0
    total_kb: float = 0
    for p in imp_dir.iterdir():
        if not p.is_file():
            continue
        if _file_age(p) > IMPROVEMENT_RUNS_MAX_AGE:
            old_count += 1
            try:
                total_kb += p.stat().st_size / 1024
            except OSError:
                pass
            if not dry_run:
                try:
                    p.unlink()
                except OSError:
                    pass

    if old_count > 0:
        verb = "[dry-run] would delete" if dry_run else "deleted"
        actions.append(
            f"{verb} {old_count} old improvement runs ({total_kb:.0f}KB, >{IMPROVEMENT_RUNS_MAX_AGE // 86400}d)"
        )
    return actions


def run(dry_run: bool = False) -> dict:
    """Execute all retention policies. Returns summary dict."""
    all_actions = []
    freed_before = shutil.disk_usage("/").free

    all_actions.extend(compress_old_structured_logs(dry_run))
    all_actions.extend(prune_old_backups(dry_run))
    all_actions.extend(rotate_progress_history(dry_run))
    all_actions.extend(clear_mypy_cache(dry_run))
    all_actions.extend(prune_old_investigations(dry_run))
    all_actions.extend(prune_old_notified(dry_run))
    all_actions.extend(prune_old_improvement_runs(dry_run))

    freed_after = shutil.disk_usage("/").free
    freed_mb = (freed_after - freed_before) / (1024 * 1024) if not dry_run else 0

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "actions": all_actions,
        "actions_count": len(all_actions),
        "freed_mb": round(freed_mb, 1),
        "disk_free_pct": round(shutil.disk_usage("/").free / shutil.disk_usage("/").total * 100, 1),
    }
    return summary


def main():
    parser = argparse.ArgumentParser(description="NovaCore log retention")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"=== Log Retention {'(DRY RUN) ' if args.dry_run else ''}===")
        print(f"Timestamp: {result['timestamp']}")
        print(f"Actions: {result['actions_count']}")
        for a in result["actions"]:
            print(f"  • {a}")
        if not args.dry_run:
            print(f"Freed: {result['freed_mb']:.1f} MB")
        print(f"Disk free: {result['disk_free_pct']:.1f}%")


if __name__ == "__main__":
    main()
