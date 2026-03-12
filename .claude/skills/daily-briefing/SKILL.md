---
name: daily-briefing
description: "Morning briefing: today's calendar, unread email summary, and pending NovaCore tasks in one view. Use when the user says 'good morning', 'daily brief', 'what's on today', 'standup', or wants a start-of-day overview combining calendar, email, and tasks."
argument-hint: ""
disable-model-invocation: false
activation:
  keywords: [good morning, daily brief, briefing, standup, what's on today, start of day, morning update, daily summary]
  when:
    - Start of day or morning check-in
    - User wants a combined overview of calendar + email + tasks
    - Before standup or daily planning
    - User greets with "good morning" or similar
tool_doctrine:
  daily_briefing:
    workflow:
      - authenticate_first
      - read_only_across_all_services
      - calendar_then_email_then_tasks
      - concise_actionable_format
      - highlight_conflicts_and_urgency
output_contract:
  required:
    - summary
    - meetings_today
    - unread_count
    - pending_tasks
    - action_taken
    - verification
    - confidence
---

# Daily Briefing

Inspired by [Google Workspace CLI workflow+standup-report](https://github.com/googleworkspace/cli).

## When to use

- First thing in the morning
- Before daily standup
- "What do I have today?"
- General start-of-day orientation

## When NOT to use

- Detailed email reading (use google-gmail or gmail-triage)
- Modifying calendar or tasks (use specific service skills)
- Weekly planning (use weekly-digest)

## Workflow

### Step 1 — Today's calendar

```bash
python3 tools/google_workspace.py calendar list --days 1
```

Extract: event title, time, location, attendees count.
Flag any scheduling conflicts (overlapping events).

### Step 2 — Inbox snapshot

```bash
python3 tools/google_workspace.py gmail search "is:unread in:inbox" --max-results 10
```

Count total unread. Flag urgent items (VIP senders, "urgent" in subject).

### Step 3 — Pending NovaCore tasks

```bash
ls -la TASKS/*.md 2>/dev/null
```

List any pending task files with their status (from filename pattern).

### Step 4 — Compose the briefing

```
## Daily Briefing — [Date]

### Calendar ([N] events)
| Time | Event | Location |
|------|-------|----------|
| 09:00 | Sprint Review | Zoom |
| 14:00 | 1:1 with Alice | Room 3B |

[Flag: 2pm and 2:30pm events overlap!]

### Inbox ([N] unread)
- [Urgent] Bob: "Server down — need help ASAP"
- Alice: "Q1 report draft attached"
- Newsletter: "Weekly tech digest"

### Tasks ([N] pending)
- TASKS/0420_deploy_staging.md (pending)
- TASKS/0421_review_pr.md (in_progress)

### Suggested Focus
1. Reply to Bob's urgent email
2. Prep for Sprint Review (09:00)
3. Review Q1 report from Alice
```

## Tool Usage Rules

- **Read-only across all services.** Never modify calendar, email, or tasks.
- **Keep it brief.** This is a scan, not a deep dive. One line per item.
- **Calendar first.** Time-sensitive items take priority.
- **Flag conflicts.** If two events overlap, call it out explicitly.
- **Max 10 emails.** Don't list every unread message — just the top 10 with priority sorting.

## Failure Handling

| Error | Action |
|-------|--------|
| Calendar not authenticated | Show email + tasks only, note calendar unavailable |
| Gmail not authenticated | Show calendar + tasks only, note email unavailable |
| No events today | Report "clear calendar" |
| No unread email | Report "inbox zero" |
| No pending tasks | Report "task queue empty" |

## Outputs / Contract

```
## Daily Briefing Contract
summary: <date — X meetings, Y unread, Z tasks>
meetings_today: <count>
calendar_conflicts: <count or "none">
unread_count: <count>
urgent_emails: <count>
pending_tasks: <count>
action_taken: read-only scan of calendar + gmail + TASKS/
verification: <confirmed via API responses>
confidence: <high | medium | low>
```
