#!/usr/bin/env python3
"""NovaCore Daily Shift Schedule Generator.

Self-perpetuating schedule system that generates 8 shift-block task files
per day, leveraging the watcher's ``scheduled_at`` frontmatter mechanism.

Usage:
    python3 scripts/daily_shift_generator.py            # generate for tomorrow
    python3 scripts/daily_shift_generator.py --today     # generate for today
    python3 scripts/daily_shift_generator.py --dry-run   # preview without writing
    python3 scripts/daily_shift_generator.py --today --dry-run

The watcher reads TASKS/*.md, checks ``scheduled_at`` YAML frontmatter, and
dispatches when ``now >= scheduled_at``.  The watcher uses naive local time
(server is UTC).  Central Time conversions happen here.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path("/home/nova/nova-core")
TASKS = ROOT / "TASKS"
TASKS_COMPLETED = TASKS / "_completed"
OUTPUT = ROOT / "OUTPUT"
LOGS = ROOT / "LOGS"

# ── Timezone ─────────────────────────────────────────────────────────────────
CT = ZoneInfo("America/Chicago")  # Central Time (handles DST)
SERVER_TZ = ZoneInfo("UTC")  # Server is UTC

# ── Cascade Guard ───────────────────────────────────────────────────────────
MAX_LOOKAHEAD_DAYS = 2  # Never generate shifts more than 2 days into the future


# ── Shift Block Definitions ─────────────────────────────────────────────────
SHIFT_BLOCKS: list[dict] = [
    {
        "block": 1,
        "hour": 6,
        "minute": 30,
        "slug": "system_health",
        "title": "System Health & Continuity",
        "focus": "System Health",
        "instructions": """\
1. Check system health:
   - Read HEARTBEAT.md for current state
   - Verify all systemd services are running (watcher, telegram, telegram-notifier)
   - Run test suite: `python -m pytest tests/ -q --tb=short`

2. Review yesterday's outcomes:
   - Check OUTPUT/ for completed task results
   - Identify any failed or incomplete work
   - Note any issues that need follow-up

3. Continuity check:
   - Query Fusion Memory for last session checkpoint
   - Check open_threads and next_actions
   - Prioritize carrying forward any unfinished work""",
        "output_suffix": "health",
    },
    {
        "block": 2,
        "hour": 7,
        "minute": 15,
        "slug": "novatrade_progress",
        "title": "NovaTrade Progress",
        "focus": "NovaTrade",
        "instructions": """\
1. Check NovaTrade status:
   - Review novatrade/ directory for current state
   - Check if novacore-novatrade.service is running
   - Review any open NovaTrade issues or gaps

2. Push forward:
   - Pick the highest-impact NovaTrade item to advance
   - Fix execution gaps, implement next steps, or expand test coverage
   - Focus on items that move toward live trading readiness

3. Test and verify:
   - Run NovaTrade-specific tests: `python -m pytest tests/ -k novatrade -q`
   - Verify any changes don't break existing functionality""",
        "output_suffix": "novatrade",
    },
    {
        "block": 3,
        "hour": 8,
        "minute": 0,
        "slug": "implementation",
        "title": "Implementation Work",
        "focus": "Implementation",
        "instructions": """\
1. Identify highest-impact work:
   - Check MEMORY.md for current plan status and open items
   - Look for items flagged as high priority or blocking other work
   - Consider what would have the biggest positive impact on the system

2. Implement:
   - Write clean, modular, well-tested code
   - Follow existing patterns and conventions in the codebase
   - Create tests alongside implementation

3. Validate:
   - Run affected test suites
   - Verify no regressions with: `python -m pytest tests/ -q --tb=short`""",
        "output_suffix": "implementation",
    },
    {
        "block": 4,
        "hour": 8,
        "minute": 45,
        "slug": "deep_research",
        "title": "Deep Research",
        "focus": "Research",
        "instructions": """\
1. Choose a research topic:
   - Pick something that advances NovaCore or NovaTrade capabilities
   - Consider: new libraries, architectural patterns, trading strategies,
     AI techniques, infrastructure improvements
   - Prefer topics that can translate into actionable improvements

2. Research deeply:
   - Use web search and documentation to gather information
   - Analyze trade-offs, compare approaches
   - Look for real-world production examples

3. Document findings:
   - Store key insights in Fusion Memory with category: research
   - Write a concise summary with actionable recommendations
   - Note any items that should become implementation tasks""",
        "output_suffix": "research",
    },
    {
        "block": 5,
        "hour": 9,
        "minute": 30,
        "slug": "free_will",
        "title": "Free Will / Autonomy",
        "focus": "Autonomy",
        "instructions": """\
1. Assess current state:
   - Look at the codebase, services, memory, and recent outputs
   - Consider what matters most right now based on current conditions
   - Trust your judgment — you have full context on the system

2. Act on your assessment:
   - Fix something that has been bothering you
   - Improve something you've noticed is suboptimal
   - Explore an idea you think has potential
   - Refactor code that could be cleaner
   - Build something new that would help

3. Document your choice:
   - Explain why you chose this particular action
   - Record the rationale in Fusion Memory""",
        "output_suffix": "autonomy",
    },
    {
        "block": 6,
        "hour": 10,
        "minute": 15,
        "slug": "testing_quality",
        "title": "Testing & Quality",
        "focus": "Quality",
        "instructions": """\
1. Coverage analysis:
   - Run: `python -m pytest tests/ --cov=. --cov-report=term-missing -q`
   - Identify modules with lowest coverage
   - Focus on critical runtime paths

2. Fix flaky tests:
   - Look for tests that intermittently fail
   - Fix timing dependencies, resource leaks, or ordering assumptions
   - Add proper mocking where external services are called

3. Expand coverage:
   - Write tests for untested edge cases and error paths
   - Add property-based tests (Hypothesis) where appropriate
   - Ensure new code from today's shifts has test coverage""",
        "output_suffix": "quality",
    },
    {
        "block": 7,
        "hour": 11,
        "minute": 0,
        "slug": "memory_hygiene",
        "title": "Memory & Knowledge Hygiene",
        "focus": "Memory",
        "instructions": """\
1. Memory consolidation:
   - Scan MEMORY/workflow_learnings/ for duplicates or stale entries
   - Merge overlapping learnings
   - Archive superseded entries to MEMORY/_archive/

2. Knowledge graph health:
   - Query Fusion Memory for orphaned or disconnected nodes
   - Strengthen relationships between related memories
   - Ensure recent decisions have proper context links

3. Vault sync:
   - Check Nova Vault for any items that should be reflected in memory
   - Update any stale vault entries
   - Ensure cross-system consistency""",
        "output_suffix": "memory",
    },
    {
        "block": 8,
        "hour": 11,
        "minute": 45,
        "slug": "session_wrap",
        "title": "Session Wrap & Tomorrow Prep",
        "focus": "Wrap-up",
        "instructions": """\
1. Summarize today's work:
   - Review all OUTPUT/shift_* files from today
   - List key accomplishments, decisions, and discoveries
   - Note any items that need follow-up tomorrow

2. Create session checkpoint:
   - Store checkpoint in Fusion Memory with:
     - open_threads: unfinished work items
     - next_actions: prioritized list for tomorrow
     - key_decisions: important choices made today

3. Generate tomorrow's schedule:
   - Run: `python3 scripts/daily_shift_generator.py`
   - Verify the generated task files look correct
   - This ensures the self-perpetuating schedule continues""",
        "output_suffix": "wrap",
    },
]

# ── Generator Task (self-perpetuation) ───────────────────────────────────────
GENERATOR_HOUR = 5
GENERATOR_MINUTE = 45

GENERATOR_TEMPLATE = """\
---
scheduled_at: {scheduled_at}
priority: critical
type: schedule_generator
shift_date: {shift_date}
---

# Daily Schedule Generator

Run the daily shift generator to create today's schedule:

```bash
python3 scripts/daily_shift_generator.py --today
```

This task is auto-generated to perpetuate the daily shift schedule.
If this task fires, it means Block 8 from the previous day did not
generate today's schedule — this is the safety net.
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    """Log to stdout and LOGS/shift_generator.log."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [shift-gen] {msg}"
    print(line, flush=True)
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / "shift_generator.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def ct_to_server_naive(ct_dt: datetime) -> datetime:
    """Convert a Central-Time-aware datetime to a naive datetime in server local time (UTC).

    The watcher compares ``scheduled_at`` against ``datetime.now()`` (naive, server-local).
    The server is UTC, so we convert CT -> UTC and strip tzinfo.
    """
    utc_dt = ct_dt.astimezone(SERVER_TZ)
    return utc_dt.replace(tzinfo=None)


def ct_time_str(hour: int, minute: int) -> str:
    """Format hour:minute as a human-readable CT time string."""
    period = "AM" if hour < 12 else "PM"
    display_hour = hour if hour <= 12 else hour - 12
    if display_hour == 0:
        display_hour = 12
    return f"{display_hour}:{minute:02d} {period} CT"


def next_business_day(from_date: date) -> date:
    """Return the next business day (Mon-Fri) after from_date.

    If from_date is Friday, returns Monday. Saturday returns Monday.
    Sunday returns Monday.  Otherwise returns from_date + 1 day.
    """
    d = from_date + timedelta(days=1)
    # 5 = Saturday, 6 = Sunday
    while d.weekday() in (5, 6):
        d += timedelta(days=1)
    return d


def get_yesterday_context() -> str:
    """Read recent OUTPUT/ files to build context from yesterday's work."""
    if not OUTPUT.exists():
        return "No previous output files found."

    now_utc = datetime.now(timezone.utc)
    yesterday_start = now_utc - timedelta(hours=36)  # generous window

    recent_outputs: list[tuple[float, Path]] = []
    for p in OUTPUT.iterdir():
        if p.suffix != ".md":
            continue
        try:
            mtime = p.stat().st_mtime
            if mtime >= yesterday_start.timestamp():
                recent_outputs.append((mtime, p))
        except OSError:
            continue

    if not recent_outputs:
        return "No recent output files found in the last 36 hours."

    # Sort newest first, take top 15
    recent_outputs.sort(key=lambda x: x[0], reverse=True)
    recent_outputs = recent_outputs[:15]

    lines = []
    for mtime, p in recent_outputs:
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        ts_str = dt.astimezone(CT).strftime("%Y-%m-%d %H:%M CT")
        # Extract first meaningful line from the file as a summary
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            summary = _extract_file_summary(text, p.name)
            lines.append(f"- **{p.name}** ({ts_str}): {summary}")
        except OSError:
            lines.append(f"- **{p.name}** ({ts_str})")

    return "\n".join(lines)


def _extract_file_summary(text: str, filename: str) -> str:
    """Extract a one-line summary from an output file."""
    # Look for # heading
    for line in text.splitlines()[:20]:
        stripped = line.strip()
        if stripped.startswith("# ") and len(stripped) > 3:
            return stripped[2:].strip()[:120]
    # Fallback: first non-empty, non-frontmatter line
    in_frontmatter = False
    for line in text.splitlines()[:30]:
        stripped = line.strip()
        if stripped == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if stripped and not stripped.startswith("#"):
            return stripped[:120]
    return "(see file)"


def cleanup_old_shifts(target_date: date | None = None) -> int:
    """Move completed shift task files to TASKS/_completed/.

    Only moves .done files that match the shift_YYYYMMDD pattern.
    If target_date is given, only cleans shifts older than that date.
    Returns count of files moved.
    """
    TASKS_COMPLETED.mkdir(parents=True, exist_ok=True)
    moved = 0

    # Match shift_YYYYMMDD_N_slug.md.done and shift_gen_YYYYMMDD.md.done
    pattern = re.compile(r"^shift_(?:gen_)?(\d{8})_")

    for p in TASKS.iterdir():
        if not p.name.endswith(".done"):
            continue
        m = pattern.match(p.name)
        if not m:
            continue

        if target_date is not None:
            try:
                file_date = datetime.strptime(m.group(1), "%Y%m%d").date()
                if file_date >= target_date:
                    continue  # don't clean today or future
            except ValueError:
                continue

        dest = TASKS_COMPLETED / p.name
        try:
            shutil.move(str(p), str(dest))
            moved += 1
        except OSError as e:
            log(f"Failed to move {p.name}: {e}")

    return moved


def build_shift_task(block: dict, shift_date: date, context: str) -> tuple[str, str]:
    """Build a single shift-block task file.

    Returns (filename, content).
    """
    n = block["block"]
    slug = block["slug"]
    filename = f"shift_{shift_date:%Y%m%d}_{n}_{slug}.md"

    # Build scheduled_at in server local time (naive, UTC)
    ct_dt = datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        block["hour"],
        block["minute"],
        tzinfo=CT,
    )
    server_dt = ct_to_server_naive(ct_dt)
    scheduled_at = server_dt.strftime("%Y-%m-%dT%H:%M:%S")

    time_str = ct_time_str(block["hour"], block["minute"])

    content = f"""\
---
scheduled_at: {scheduled_at}
priority: high
shift_block: {n}
shift_date: {shift_date}
type: shift_block
---

# Shift Block {n}: {block["title"]}

**Date:** {shift_date} | **Time:** {time_str} | **Focus:** {block["focus"]}

## Context from Previous Day

{context}

## Instructions

{block["instructions"]}

## Output

Write results to OUTPUT/shift_{shift_date:%Y%m%d}_{n}_{block["output_suffix"]}.md
"""
    return filename, content


def build_generator_task(target_date: date) -> tuple[str, str]:
    """Build the self-perpetuating generator task for the given date.

    This fires at 5:45 AM CT as a safety net in case Block 8 didn't
    generate the schedule.
    """
    filename = f"shift_gen_{target_date:%Y%m%d}.md"

    ct_dt = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        GENERATOR_HOUR,
        GENERATOR_MINUTE,
        tzinfo=CT,
    )
    server_dt = ct_to_server_naive(ct_dt)
    scheduled_at = server_dt.strftime("%Y-%m-%dT%H:%M:%S")

    content = GENERATOR_TEMPLATE.format(
        scheduled_at=scheduled_at,
        shift_date=target_date,
    )
    return filename, content


def build_telegram_summary(shift_date: date, files: list[str]) -> str:
    """Build a Telegram notification summarizing the generated schedule."""
    ct_now = datetime.now(timezone.utc).astimezone(CT)
    day_name = shift_date.strftime("%A, %B %d")

    lines = [
        f"Daily Shift Schedule — {day_name}",
        "",
    ]

    for block in SHIFT_BLOCKS:
        time_str = ct_time_str(block["hour"], block["minute"])
        lines.append(f"  {block['block']}. {time_str} — {block['title']}")

    lines.append("")
    lines.append(f"  + Generator safety net at {ct_time_str(GENERATOR_HOUR, GENERATOR_MINUTE)}")
    lines.append("")
    lines.append(f"{len(files)} task files written to TASKS/")
    lines.append(f"Generated {ct_now.strftime('%H:%M CT')}")

    return "\n".join(lines)


def send_telegram(text: str) -> None:
    """Send a message via Telegram using the same env vars as telegram_notifier.py."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ALLOWED_CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        log("Telegram credentials not set — skipping notification")
        return

    try:
        import httpx
    except ImportError:
        log("httpx not installed — skipping Telegram notification")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    # Chunk if needed
    max_len = 3500
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > max_len:
        cut = remaining.rfind("\n", 0, max_len)
        if cut < 500:
            cut = max_len
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)

    try:
        with httpx.Client(timeout=25) as client:
            for chunk in chunks:
                payload = {"chat_id": chat_id, "text": chunk}
                r = client.post(url, json=payload)
                r.raise_for_status()
                time.sleep(0.25)
        log(f"Telegram notification sent ({len(chunks)} chunk(s))")
    except Exception as e:
        log(f"Telegram send failed: {e}")


def check_existing_shifts(shift_date: date) -> list[str]:
    """Check if shift tasks already exist for the given date.

    Returns list of existing filenames.
    """
    date_str = shift_date.strftime("%Y%m%d")
    existing = []
    if TASKS.exists():
        for p in TASKS.iterdir():
            if (p.name.startswith(f"shift_{date_str}_") and p.suffix == ".md") or p.name == f"shift_gen_{date_str}.md":
                existing.append(p.name)
    return existing


# ── Main ─────────────────────────────────────────────────────────────────────


def generate_schedule(shift_date: date, dry_run: bool = False) -> None:
    """Generate the full shift schedule for the given date."""
    log(f"Generating shift schedule for {shift_date} (dry_run={dry_run})")

    # Lookahead guard: prevent cascade of tasks far into the future
    today = datetime.now(timezone.utc).astimezone(CT).date()
    if (shift_date - today).days > MAX_LOOKAHEAD_DAYS:
        log(
            f"SKIPPED: target date {shift_date} is more than {MAX_LOOKAHEAD_DAYS} days "
            f"ahead of today ({today}). Cascade guard prevented generation."
        )
        return

    # Check for existing shifts
    existing = check_existing_shifts(shift_date)
    if existing:
        log(f"Found {len(existing)} existing shift files for {shift_date}:")
        for f in sorted(existing):
            log(f"  - {f}")
        if not dry_run:
            log("Skipping generation — shifts already exist. Delete existing files first to regenerate.")
            return

    # Gather context from yesterday
    context = get_yesterday_context()

    # Build all task files
    files_to_write: list[tuple[str, str]] = []

    for block in SHIFT_BLOCKS:
        filename, content = build_shift_task(block, shift_date, context)
        files_to_write.append((filename, content))

    # Build the generator task for the NEXT business day (self-perpetuation)
    next_day = next_business_day(shift_date)
    gen_filename, gen_content = build_generator_task(next_day)
    gen_path = TASKS / gen_filename
    if gen_path.exists():
        log(f"Generator task already exists: {gen_filename} — skipping to prevent cascade")
    else:
        files_to_write.append((gen_filename, gen_content))

    if dry_run:
        print(f"\n{'=' * 60}")
        print(f"DRY RUN — Schedule for {shift_date}")
        print(f"{'=' * 60}\n")
        for filename, content in files_to_write:
            print(f"--- {filename} ---")
            # Show just the frontmatter and title
            lines = content.split("\n")
            for i, line in enumerate(lines):
                if i > 0 and line.startswith("# "):
                    print(line)
                    break
                print(line)
            print(f"  (... {len(content)} chars total)\n")
        print(f"Total: {len(files_to_write)} files")
        print(f"Next day generator: {gen_filename} (for {next_day})")
        return

    # Write files
    TASKS.mkdir(parents=True, exist_ok=True)
    written_files: list[str] = []

    for filename, content in files_to_write:
        path = TASKS / filename
        path.write_text(content, encoding="utf-8")
        written_files.append(filename)
        log(f"  Written: {filename}")

    log(f"Generated {len(written_files)} shift files for {shift_date}")

    # Clean up old completed shift tasks
    moved = cleanup_old_shifts(target_date=shift_date)
    if moved:
        log(f"Archived {moved} completed shift task(s) to TASKS/_completed/")

    # Send Telegram summary
    summary = build_telegram_summary(shift_date, written_files)
    send_telegram(summary)

    log("Schedule generation complete")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate daily shift schedule task files for NovaCore watcher.",
    )
    parser.add_argument(
        "--today",
        action="store_true",
        help="Generate schedule for today instead of tomorrow",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be generated without writing files",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Generate for a specific date (YYYY-MM-DD)",
    )
    args = parser.parse_args()

    if args.date:
        try:
            shift_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"Invalid date format: {args.date} (expected YYYY-MM-DD)", file=sys.stderr)
            sys.exit(1)
    elif args.today:
        # "Today" in Central Time
        ct_now = datetime.now(timezone.utc).astimezone(CT)
        shift_date = ct_now.date()
    else:
        # Next business day from today (Central Time)
        ct_now = datetime.now(timezone.utc).astimezone(CT)
        shift_date = next_business_day(ct_now.date())

    generate_schedule(shift_date, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
