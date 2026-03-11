#!/usr/bin/env python3
"""NovaCore daily backup script.

Creates timestamped tar.gz of critical state directories,
rotates old backups (keeps last 7), verifies integrity,
and alerts via Telegram on failure.
"""

import datetime
import os
import sys
import tarfile
from pathlib import Path

# --- Configuration ---
NOVA_CORE = Path("/home/nova/nova-core")
BACKUP_DIR = Path("/home/nova/backups")
LOG_FILE = NOVA_CORE / "LOGS" / "backup.log"
KEEP_DAYS = 7

# Directories/files to back up (relative to NOVA_CORE)
BACKUP_TARGETS = [
    "STATE",
    "AGENTS",
    "SKILLS",
    "CLAUDE.md",
    "scripts",
    "tools",
]


def log(msg: str) -> None:
    """Append timestamped message to backup log and stdout."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def send_telegram_alert(text: str) -> None:
    """Send Telegram alert using env vars. Fails silently if not configured."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ALLOWED_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        log("WARN: Telegram credentials not set, skipping alert")
        return
    try:
        import httpx
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        with httpx.Client(timeout=25) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
        log("Telegram alert sent successfully")
    except Exception as e:
        log(f"WARN: Telegram alert failed: {e}")


def create_backup() -> Path:
    """Create a timestamped tar.gz backup of critical directories."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_name = f"novacore_backup_{ts}.tar.gz"
    archive_path = BACKUP_DIR / archive_name

    log(f"Creating backup: {archive_path}")

    with tarfile.open(archive_path, "w:gz") as tar:
        for target in BACKUP_TARGETS:
            full_path = NOVA_CORE / target
            if full_path.exists():
                tar.add(str(full_path), arcname=target)
                log(f"  Added: {target}")
            else:
                log(f"  SKIP (not found): {target}")

    return archive_path


def verify_backup(archive_path: Path) -> tuple[bool, int]:
    """Verify backup integrity by listing contents. Returns (ok, file_count)."""
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            members = tar.getnames()
        file_count = len(members)
        log(f"Integrity check PASSED: {file_count} entries")
        return True, file_count
    except Exception as e:
        log(f"Integrity check FAILED: {e}")
        return False, 0


def rotate_backups() -> int:
    """Delete backups older than KEEP_DAYS. Returns number deleted."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=KEEP_DAYS)
    deleted = 0
    for f in sorted(BACKUP_DIR.glob("novacore_backup_*.tar.gz")):
        mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime, tz=datetime.timezone.utc)
        if mtime < cutoff:
            f.unlink()
            log(f"Rotated (deleted): {f.name}")
            deleted += 1
    return deleted


def main() -> int:
    log("=" * 60)
    log("NovaCore backup starting")

    try:
        # Create backup
        archive_path = create_backup()
        size_mb = archive_path.stat().st_size / (1024 * 1024)
        log(f"Backup size: {size_mb:.2f} MB")

        # Verify integrity
        ok, file_count = verify_backup(archive_path)
        if not ok:
            msg = f"BACKUP FAILED: Integrity check failed for {archive_path.name}"
            log(msg)
            send_telegram_alert(f"[NovaCore] {msg}")
            return 1

        # Rotate old backups
        deleted = rotate_backups()
        remaining = len(list(BACKUP_DIR.glob("novacore_backup_*.tar.gz")))
        log(f"Rotation: deleted {deleted}, keeping {remaining} backups")

        # Success summary
        log(f"BACKUP SUCCESS: {archive_path.name} ({size_mb:.2f} MB, {file_count} entries)")
        log("=" * 60)
        return 0

    except Exception as e:
        msg = f"BACKUP FAILED: {e}"
        log(msg)
        send_telegram_alert(f"[NovaCore] {msg}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
