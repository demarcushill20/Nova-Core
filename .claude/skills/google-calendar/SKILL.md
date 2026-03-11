---
name: google-calendar
description: "List, create, update, and delete Google Calendar events using the NovaCore Google Workspace CLI. Auto-invoked for scheduling tasks."
activation:
  keywords:
    - calendar
    - schedule
    - event
    - meeting
    - appointment
    - reminder
  when:
    - User asks about upcoming events or schedule
    - Task requires creating or modifying calendar events
    - Scheduling a meeting or reminder
tool_doctrine:
  calendar:
    workflow:
      - authenticate_first
      - list_before_modify
      - confirm_before_delete
      - use_iso_datetime
output_contract:
  required:
    - summary
    - action_taken
    - verification
    - confidence
---

# Calendar Skill

## When to use

- Checking upcoming events and schedule
- Creating new calendar events (meetings, reminders, deadlines)
- Updating existing event details
- Deleting cancelled events

## When NOT to use

- Task scheduling in NovaCore — use TASKS/ directory
- Cron jobs — use system cron
- Telegram reminders — use Telegram bot

## CLI Reference

All commands output JSON. The CLI is at `tools/google_workspace.py`.

### List upcoming events
```bash
python3 tools/google_workspace.py calendar list --days 7
python3 tools/google_workspace.py calendar list --days 30 --max-results 50
```

### Create an event
```bash
python3 tools/google_workspace.py calendar create \
  --title "Sprint Review" \
  --start "2026-03-15T10:00:00" \
  --end "2026-03-15T11:00:00" \
  --description "Review sprint deliverables" \
  --location "Zoom" \
  --attendees "alice@example.com,bob@example.com" \
  --timezone "America/New_York"
```

### Update an event
```bash
python3 tools/google_workspace.py calendar update <event_id> \
  --title "Updated Title" \
  --start "2026-03-15T14:00:00" \
  --end "2026-03-15T15:00:00"
```

### Delete an event
```bash
python3 tools/google_workspace.py calendar delete <event_id>
```

## Workflow

1. **Check auth** — run `python3 tools/google_workspace.py auth status`.
2. **List first** — when modifying events, first list to find the event ID.
3. **Execute operation** — run the appropriate calendar subcommand.
4. **Parse JSON output** — extract event summary, time, location, attendees.
5. **Format for user** — present events in a readable timeline format.

## Tool Usage Rules

- All times must be ISO 8601 format (e.g., `2026-03-15T10:00:00`).
- Default timezone is UTC. Always specify `--timezone` when the user expects local time.
- Always list events before attempting to update or delete (to get the event ID).
- Confirm with the user before deleting events.
- Default lookahead is 7 days. Use `--days 1` for today only.
- When creating events with attendees, they receive email invitations automatically.

## Failure Handling

| Error | Action |
|-------|--------|
| "Not authenticated" | Direct user to run `python3 scripts/gw-auth.py` |
| "Event not found" | Event may have been deleted; re-list to verify |
| "Invalid datetime" | Ensure ISO 8601 format with T separator |
| "Quota exceeded" | Calendar API allows 500 requests/100 seconds |
