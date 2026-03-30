#!/usr/bin/env python3
"""Auto-deploy: detect stale NovaCore services and restart them.

Designed to run as a systemd timer (novacore-auto-deploy.timer) WITHOUT
NoNewPrivileges so it can call ``systemctl restart``.

Usage:
    python3 scripts/auto_deploy.py            # check + restart stale services
    python3 scripts/auto_deploy.py --dry-run  # check only, no restarts
    python3 scripts/auto_deploy.py --check    # exit 0=fresh, 1=stale (for monitoring)
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure nova-core is on the import path
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from utils.service_staleness import check_all_staleness, restart_stale_services  # noqa: E402

LOGS_DIR = BASE / "LOGS"
LOG_FILE = LOGS_DIR / "auto_deploy.log"

# Optional Telegram notification (reuse existing env vars)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except Exception:  # noqa: S110
        pass  # Best-effort logging — don't crash if log dir is unavailable


def _send_telegram(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import urllib.parse
        import urllib.request

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode(
            {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
            }
        ).encode()
        req = urllib.request.Request(url, data=data, method="POST")  # noqa: S310
        urllib.request.urlopen(req, timeout=10)  # noqa: S310
    except Exception as e:
        _log(f"Telegram send failed: {e}")


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    check_only = "--check" in sys.argv

    if check_only:
        reports = check_all_staleness()
        stale = [r for r in reports if r.is_stale]
        if stale:
            for r in stale:
                print(f"STALE: {r.service} ({r.commits_since_start} commits, {r.stale_hours:.1f}h)")
            return 1
        print("All services up to date.")
        return 0

    results = restart_stale_services(dry_run=dry_run)

    any_restart_failed = False
    messages: list[str] = []

    for report, restart in results:
        if restart is None:
            _log(f"FRESH: {report.service} — {report.detail}")
            continue

        action = "DRY-RUN" if dry_run else ("RESTARTED" if restart.success else "RESTART-FAILED")
        _log(
            f"{action}: {report.service} — {report.commits_since_start} commit(s), "
            f"{report.stale_hours:.1f}h stale — {restart.detail}"
        )

        if not restart.success:
            any_restart_failed = True

        if not dry_run:
            messages.append(
                f"{'✅' if restart.success else '❌'} {report.service}: "
                f"{restart.detail} ({report.commits_since_start} commit(s) behind)"
            )

    if messages:
        header = "🔄 Auto-deploy:" if not any_restart_failed else "⚠️ Auto-deploy (partial failure):"
        _send_telegram(f"{header}\n" + "\n".join(messages))

    if any_restart_failed:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
