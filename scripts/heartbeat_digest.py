#!/usr/bin/env python3
"""Nova-Core Heartbeat Digest — twice-daily condensed summary to Telegram.

Reads STATE/heartbeat_metrics.jsonl (last 12 h), computes uptime / top failures,
extracts key sections from HEARTBEAT.md, and sends a compact Markdown digest
to the configured Telegram chat.

Intended to run via systemd timer at 08:30 and 20:30 ET.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Paths (relative to repo root)
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
METRICS_FILE = REPO / "STATE" / "heartbeat_metrics.jsonl"
HEARTBEAT_MD = REPO / "HEARTBEAT.md"
LOG_FILE = REPO / "LOGS" / "heartbeat_digest.log"

ET = ZoneInfo("America/New_York")
WINDOW_HOURS = 12


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_et() -> datetime:
    return datetime.now(tz=ET)


def _log(msg: str) -> None:
    """Append a timestamped one-liner to the digest log."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = _now_et().strftime("%Y-%m-%d %H:%M:%S %Z")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {msg}\n")


def _send_telegram(text: str) -> None:
    """Send a Markdown message to the configured Telegram chat."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("ALLOWED_CHAT_ID", "")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or ALLOWED_CHAT_ID not set")

    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
    ).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(url, data=data, method="POST")  # noqa: S310
    urllib.request.urlopen(req, timeout=15)  # noqa: S310


# ---------------------------------------------------------------------------
# Metrics loading
# ---------------------------------------------------------------------------


def _load_metrics(window_hours: int = WINDOW_HOURS) -> list[dict]:
    """Load metric lines from the JSONL file within the last *window_hours*."""
    if not METRICS_FILE.exists():
        return []

    cutoff_epoch = (datetime.now(tz=timezone.utc) - timedelta(hours=window_hours)).timestamp()
    entries: list[dict] = []
    with open(METRICS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("epoch", 0) >= cutoff_epoch:
                entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# HEARTBEAT.md extraction
# ---------------------------------------------------------------------------


def _extract_novatrade_status() -> str | None:
    """Pull a brief NovaTrade status line from HEARTBEAT.md."""
    if not HEARTBEAT_MD.exists():
        return None

    text = HEARTBEAT_MD.read_text(errors="replace")
    # Look for novatrade_signals or novatrade_watchdog check lines
    for line in text.splitlines():
        lower = line.lower()
        if "novatrade_signals" in lower:
            # e.g. "- [x] novatrade_signals: status=OK | signals: ..."
            parts = line.split(":", 1)
            if len(parts) > 1:
                return parts[1].strip()
    return None


def _extract_overall_status() -> str | None:
    """Pull the 'Overall:' line from HEARTBEAT.md."""
    if not HEARTBEAT_MD.exists():
        return None

    text = HEARTBEAT_MD.read_text(errors="replace")
    for line in text.splitlines():
        if line.strip().lower().startswith("overall:"):
            return line.strip().split(":", 1)[1].strip()
    return None


# ---------------------------------------------------------------------------
# Digest formatting
# ---------------------------------------------------------------------------


def _format_digest(entries: list[dict]) -> str:
    """Build the Telegram Markdown message from metric entries."""
    now = _now_et()
    ts_str = now.strftime("%Y-%m-%d %H:%M %Z")
    window_label = "morning" if now.hour < 14 else "evening"

    if not entries:
        return (
            "\U0001f4ca *Nova-Core Heartbeat Digest*\n"
            f"_{ts_str} — {window_label} window_\n\n"
            "No metrics available for the last 12 hours."
        )

    total = len(entries)
    healthy = sum(1 for e in entries if e.get("status") == "healthy")
    uptime_pct = round(healthy / total * 100, 1) if total else 0.0

    # Average duration
    durations = [e.get("duration_ms", 0) for e in entries if e.get("duration_ms")]
    avg_dur = round(sum(durations) / len(durations)) if durations else 0

    # Current status (latest entry)
    latest = entries[-1]
    current_status = latest.get("status", "unknown")
    status_emoji = "\u2705" if current_status == "healthy" else "\u26a0\ufe0f"

    # Top 3 most-failed check names
    fail_counter: Counter[str] = Counter()
    for e in entries:
        for name in e.get("failed_names", []):
            fail_counter[name] += 1
    top_failures = fail_counter.most_common(3)

    # Build message
    lines: list[str] = [
        "\U0001f4ca *Nova-Core Heartbeat Digest*",
        f"_{ts_str} — {window_label} window_",
        "",
        f"*System Health:* {status_emoji} {current_status}",
        f"*Uptime:* {uptime_pct}% ({healthy}/{total} checks passed)",
        f"*Avg Check Duration:* {avg_dur}ms",
    ]

    if top_failures:
        lines.append("")
        lines.append("*Top Issues:*")
        for name, count in top_failures:
            lines.append(f"\u2022 {name} — failed {count}x")

    # NovaTrade section
    nt_status = _extract_novatrade_status()
    if nt_status:
        lines.append("")
        lines.append(f"*NovaTrade:* {nt_status}")

    # Overall from HEARTBEAT.md (if different from metrics)
    overall = _extract_overall_status()
    if overall and overall.upper() != current_status.upper():
        lines.append(f"*HEARTBEAT.md:* {overall}")

    lines.append("")
    lines.append("_Next digest in ~12h_")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    # Validate env vars early
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("ALLOWED_CHAT_ID", "")
    if not token or not chat_id:
        msg = "ERROR: TELEGRAM_BOT_TOKEN or ALLOWED_CHAT_ID not set"
        print(msg, file=sys.stderr)
        _log(msg)
        return 1

    try:
        entries = _load_metrics()
        digest = _format_digest(entries)
        _send_telegram(digest)
        _log(f"OK: sent digest ({len(entries)} entries, {len(digest)} chars)")
        print(f"Digest sent ({len(entries)} entries)")
        return 0
    except Exception as exc:
        msg = f"ERROR: {exc}"
        print(msg, file=sys.stderr)
        _log(msg)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
