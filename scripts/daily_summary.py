#!/usr/bin/env python3
"""Generate a daily summary task file for the NovaCore watcher.

Drops a task into TASKS/ that instructs Claude to:
1. Query Fusion Memory for all events from the past 24 hours
2. Query Obsidian vault for recent notes
3. Compile a summary of work done, things learned, and plans created
4. Save the summary to both memory systems

Intended to run via crontab at midnight daily.
"""

import datetime
from pathlib import Path

NOVA_CORE = Path("/home/nova/nova-core")
TASKS_DIR = NOVA_CORE / "TASKS"


def get_next_task_number() -> int:
    """Find the next available task number by scanning TASKS/."""
    existing = list(TASKS_DIR.glob("*"))
    max_num = 0
    for f in existing:
        name = f.name
        # Extract leading digits
        digits = ""
        for ch in name:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            max_num = max(max_num, int(digits))
    return max_num + 1


def main():
    TASKS_DIR.mkdir(parents=True, exist_ok=True)

    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    task_num = get_next_task_number()
    date_str = yesterday.strftime("%Y-%m-%d")

    task_content = f"""\
# Daily Summary Report — {date_str}

Generate a comprehensive daily summary report for {date_str}.

## Instructions

1. **Query Fusion Memory** — use `get_recent_events` (last 50 events) and
   `query_memory` with queries like "work done today", "decisions made",
   "patterns learned" to gather all activities from the past 24 hours.

2. **Query Obsidian Vault** — use `vault_search` and `vault_list` to find
   any notes created or modified in the past 24 hours
   (check 00-inbox/, 90-diary/, 30-adr/, 40-agent-patterns/,
   50-workflow-learnings/).

3. **Compile the report** with these sections:
   - **Work Completed**: Tasks executed, their outcomes, files created/modified
   - **Learnings & Insights**: New patterns, decisions, debug findings added to memory
   - **Plans Created**: Any plans, ADRs, or roadmaps drafted
   - **System Health**: Service status, errors encountered, metrics
   - **Open Threads**: Unfinished work carried over

4. **Save to both memory systems**:
   - Store in Fusion Memory via `upsert_memory` with category="daily_summary", tags=["daily-report", "{date_str}"]
   - Your daily summary is a compaction of the events you queried in step 1.
     To wire PLAN-0759 provenance edges, include a `_compacted_from` key in the
     `metadata` dict passed to `upsert_memory`. The events returned by
     `get_recent_events` each carry an `id` field (not `entity_id`). Shape:
     `{{"source_ids": [e["id"] for e in get_recent_events_result["events"]
     if e.get("id")][:20], "algorithm": "daily_summary",
     "reason": "daily roll-up"}}`.
     Truncate to the first 20 ids; there is no upstream cap — a >20-id payload
     would create 20+ edges.
     If step 1 returned zero events, omit the `_compacted_from` key entirely.
   - Write to Obsidian vault via `vault_write` at path `90-diary/{date_str}-daily-summary.md`

5. **Write the report** to OUTPUT/ as the task output.
"""

    task_filename = f"{task_num:04d}_daily_summary_{date_str}.md"
    task_path = TASKS_DIR / task_filename
    task_path.write_text(task_content, encoding="utf-8")
    print(f"Created: {task_path}")


if __name__ == "__main__":
    main()
