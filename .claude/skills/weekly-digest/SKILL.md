---
name: weekly-digest
description: "Weekly digest: this week's meetings, email volume, completed tasks, and upcoming deadlines. Use when the user says 'weekly summary', 'week in review', 'what happened this week', or wants a retrospective overview for planning."
argument-hint: ""
disable-model-invocation: false
activation:
  keywords: [weekly digest, weekly summary, week in review, this week, weekly report, what happened this week, week recap]
  when:
    - End-of-week review or planning
    - Monday morning planning for the week ahead
    - User asks for a weekly summary or recap
    - Weekly retrospective or standup
tool_doctrine:
  weekly_digest:
    workflow:
      - authenticate_first
      - read_only_across_all_services
      - calendar_then_email_then_tasks_then_git
      - aggregate_dont_list_everything
      - compare_to_baseline
output_contract:
  required:
    - summary
    - meetings_count
    - email_stats
    - tasks_completed
    - action_taken
    - verification
    - confidence
---

# Weekly Digest

Inspired by [Google Workspace CLI workflow+weekly-digest](https://github.com/googleworkspace/cli).

## When to use

- Friday afternoon wrap-up
- Monday morning week planning
- "What did I do this week?"
- Preparing a status update for stakeholders

## When NOT to use

- Daily check-in (use daily-briefing)
- Detailed email reading (use google-gmail)
- Real-time inbox triage (use gmail-triage)

## Workflow

### Step 1 — This week's calendar

```bash
python3 tools/google_workspace.py calendar list --days 7
```

Count meetings, total meeting hours, identify busiest day.

### Step 2 — Email volume

```bash
python3 tools/google_workspace.py gmail search "newer_than:7d in:inbox" --max-results 50
python3 tools/google_workspace.py gmail search "newer_than:7d in:sent" --max-results 50
```

Count: received, sent, unread remaining.

### Step 3 — Completed NovaCore tasks

```bash
ls -la OUTPUT/ | grep "$(date +%Y-%m)" | tail -20
```

Count tasks completed this week from OUTPUT/ timestamps.

### Step 4 — Git activity

```bash
git log --oneline --since="7 days ago" --no-merges | head -20
```

Count commits, summarize key changes.

### Step 5 — Compose the digest

```
## Weekly Digest — [Week of Date]

### Calendar
- **[N] meetings** ([X] hours total)
- Busiest day: [Day] ([Y] meetings)
- Next week preview: [N] meetings scheduled

### Email
- **[N] received** / **[M] sent** / **[K] unread remaining**
- Top senders: [list top 3 by volume]

### NovaCore Work
- **[N] tasks completed**
- **[M] commits pushed**
- Key deliverables:
  - [deliverable 1]
  - [deliverable 2]

### Upcoming Deadlines
- [deadline 1 — date]
- [deadline 2 — date]

### Suggested Focus for Next Week
1. [based on unread email backlog]
2. [based on upcoming calendar density]
3. [based on open tasks]
```

## Tool Usage Rules

- **Read-only.** Never modify any data.
- **Aggregate, don't enumerate.** Show counts and trends, not every individual item.
- **7-day window.** Always look back exactly one week.
- **Include git activity.** NovaCore work is tracked in git, not just TASKS/.
- **Forward-looking.** Include next week's meeting count for planning.

## Failure Handling

| Error | Action |
|-------|--------|
| Calendar not authenticated | Skip calendar section, note it |
| Gmail not authenticated | Skip email section, note it |
| No tasks completed | Report "no tasks completed this week" |
| Git log empty | Report "no commits this week" |

## Outputs / Contract

```
## Weekly Digest Contract
summary: <week of [date] — X meetings, Y emails, Z tasks completed>
meetings_count: <count>
meeting_hours: <total hours>
email_stats:
  received: <count>
  sent: <count>
  unread: <count>
tasks_completed: <count>
commits: <count>
action_taken: read-only scan of calendar + gmail + OUTPUT/ + git log
verification: <confirmed via API responses and file system>
confidence: <high | medium | low>
```
