#!/usr/bin/env python3
"""NovaCore Morning Schedule Generator.

Runs daily at 6:00 AM Central Time via systemd timer.
Generates a formatted daily schedule from:
  - Google Calendar events (today)
  - Unread Gmail messages (top priority)
  - Pending NovaCore tasks

Output: writes schedule to OUTPUT/ and sends via Telegram.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/home/nova/nova-core")
OUTPUT = ROOT / "OUTPUT"
TASKS = ROOT / "TASKS"
LOGS = ROOT / "LOGS"
GW_CLI = ROOT / "tools" / "google_workspace.py"

# Central Time offset (CT = UTC-6, CDT = UTC-5)
# We use America/Chicago for proper DST handling
CT = timezone(timedelta(hours=-5))  # CDT (March-November)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "").strip()


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [morning-schedule] {msg}"
    print(line, flush=True)
    try:
        log_path = LOGS / "morning_schedule.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run_gw(args: list[str], timeout: int = 30) -> dict | list | None:
    """Run google_workspace.py and parse JSON output."""
    cmd = [sys.executable, str(GW_CLI)] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ROOT),
        )
        # Try to parse stdout JSON even if returncode != 0 (stderr may have warnings)
        stdout = result.stdout.strip()
        if stdout:
            try:
                data = json.loads(stdout)
                # Check if the parsed result is an error response
                if isinstance(data, dict) and "error" in data:
                    log(f"GW API error ({' '.join(args)}): {data['error']}")
                    return None
                return data
            except json.JSONDecodeError:
                pass
        if result.returncode != 0:
            # Filter out FutureWarning noise from stderr
            err = "\n".join(
                line
                for line in result.stderr.strip().splitlines()
                if "FutureWarning" not in line and "warnings.warn" not in line
            ).strip()
            if err:
                log(f"GW CLI error ({' '.join(args)}): {err}")
            return None
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"GW CLI failed ({' '.join(args)}): {e}")
        return None


def get_calendar_events() -> list[dict]:
    """Fetch today's calendar events."""
    data = run_gw(["calendar", "list", "--days", "1"])
    if isinstance(data, dict) and "events" in data:
        return data["events"]
    if isinstance(data, list):
        return data
    return []


def get_unread_emails() -> list[dict]:
    """Fetch unread inbox messages."""
    data = run_gw(["gmail", "search", "is:unread in:inbox", "--max-results", "10"])
    if isinstance(data, dict) and "messages" in data:
        return data["messages"]
    if isinstance(data, list):
        return data
    return []


def get_pending_tasks() -> list[str]:
    """List pending task files."""
    tasks = []
    if TASKS.exists():
        for f in sorted(TASKS.iterdir()):
            if f.suffix == ".md" and not f.name.startswith("."):
                tasks.append(f.name)
    return tasks


def format_event_time(event: dict) -> str:
    """Extract and format event time."""
    start = event.get("start", {})
    dt_str = start.get("dateTime", start.get("date", ""))
    if not dt_str:
        return "All day"
    try:
        if "T" in dt_str:
            # Parse ISO datetime
            dt_str_clean = dt_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(dt_str_clean)
            return dt.strftime("%I:%M %p").lstrip("0")
        return dt_str  # date-only events
    except (ValueError, TypeError):
        return dt_str


def format_event_end_time(event: dict) -> str:
    """Extract and format event end time."""
    end = event.get("end", {})
    dt_str = end.get("dateTime", end.get("date", ""))
    if not dt_str:
        return ""
    try:
        if "T" in dt_str:
            dt_str_clean = dt_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(dt_str_clean)
            return dt.strftime("%I:%M %p").lstrip("0")
        return ""
    except (ValueError, TypeError):
        return ""


def build_schedule() -> str:
    """Build the full morning schedule markdown."""
    now = datetime.now(timezone.utc)
    # Get CT date
    try:
        import zoneinfo

        ct_tz = zoneinfo.ZoneInfo("America/Chicago")
        ct_now = now.astimezone(ct_tz)
    except (ImportError, KeyError):
        ct_now = now + timedelta(hours=-6)  # fallback to CST

    date_str = ct_now.strftime("%A, %B %d, %Y")
    lines = [f"# Daily Schedule — {date_str}", ""]

    # --- Calendar ---
    events = get_calendar_events()
    if events:
        lines.append(f"## Calendar ({len(events)} event{'s' if len(events) != 1 else ''})")
        lines.append("")
        lines.append("| Time | Event | Location |")
        lines.append("|------|-------|----------|")
        for ev in events:
            title = ev.get("summary", "(no title)")
            location = ev.get("location", "—")
            start_time = format_event_time(ev)
            end_time = format_event_end_time(ev)
            time_range = f"{start_time}"
            if end_time:
                time_range += f" – {end_time}"
            lines.append(f"| {time_range} | {title} | {location} |")
        lines.append("")
    else:
        lines.append("## Calendar")
        lines.append("")
        lines.append("Clear calendar today.")
        lines.append("")

    # --- Email ---
    emails = get_unread_emails()
    if emails:
        lines.append(f"## Inbox ({len(emails)} unread)")
        lines.append("")
        for em in emails[:10]:
            sender = em.get("from", em.get("sender", "Unknown"))
            subject = em.get("subject", "(no subject)")
            # Truncate long subjects
            if len(subject) > 80:
                subject = subject[:77] + "..."
            lines.append(f"- **{sender}**: {subject}")
        lines.append("")
    else:
        lines.append("## Inbox")
        lines.append("")
        lines.append("Inbox zero.")
        lines.append("")

    # --- Tasks ---
    tasks = get_pending_tasks()
    if tasks:
        lines.append(f"## Pending Tasks ({len(tasks)})")
        lines.append("")
        for t in tasks[:15]:
            status = "pending"
            if t.endswith(".inprogress"):
                status = "in progress"
            elif t.endswith(".failed"):
                status = "failed"
            lines.append(f"- `{t}` ({status})")
        if len(tasks) > 15:
            lines.append(f"- ...and {len(tasks) - 15} more")
        lines.append("")
    else:
        lines.append("## Pending Tasks")
        lines.append("")
        lines.append("Task queue empty.")
        lines.append("")

    # --- Footer ---
    lines.append("---")
    lines.append(f"*Generated {ct_now.strftime('%Y-%m-%d %H:%M CT')} by NovaCore Morning Schedule*")

    return "\n".join(lines)


def send_telegram(text: str) -> None:
    """Send schedule via Telegram."""
    if not BOT_TOKEN or not CHAT_ID:
        log("Telegram credentials not set — skipping notification")
        return

    try:
        import httpx
    except ImportError:
        log("httpx not installed — skipping Telegram notification")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    # Chunk if needed (Telegram limit ~4096 chars)
    max_len = 3500
    chunks = []
    remaining = text.strip()
    while len(remaining) > max_len:
        cut = remaining.rfind("\n", 0, max_len)
        if cut < 500:
            cut = max_len
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)

    with httpx.Client(timeout=25) as client:
        for chunk in chunks:
            payload = {"chat_id": CHAT_ID, "text": chunk}
            r = client.post(url, json=payload)
            r.raise_for_status()
            time.sleep(0.25)

    log(f"Telegram notification sent ({len(chunks)} chunk(s))")


def main() -> None:
    log("Morning schedule generation started")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)

    schedule = build_schedule()

    # Write to OUTPUT/
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_file = OUTPUT / f"morning_schedule_{ts}.md"
    output_file.write_text(schedule, encoding="utf-8")
    log(f"Schedule written to {output_file.name}")

    # Send via Telegram
    try:
        send_telegram(schedule)
    except Exception as e:
        log(f"Telegram send failed: {e}")

    log("Morning schedule generation complete")


if __name__ == "__main__":
    main()
