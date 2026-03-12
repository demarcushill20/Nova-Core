---
name: gmail-triage
description: "Triage your Gmail inbox: summarize unread messages by priority, flag urgent items, and suggest actions. Use when the user says 'check my email', 'what's in my inbox', 'any urgent emails', 'triage my mail', or wants an overview of unread messages."
argument-hint: "[max-messages] [query-filter]"
disable-model-invocation: false
activation:
  keywords: [triage, inbox, unread, check email, check mail, urgent email, email summary, what's new]
  when:
    - User wants an overview of their inbox
    - Morning routine or start-of-day check-in
    - User asks about unread or urgent messages
    - Before a meeting to check for last-minute agenda changes
tool_doctrine:
  gmail_triage:
    workflow:
      - authenticate_first
      - read_only_never_modify
      - prioritize_by_sender_and_subject
      - summarize_dont_dump
      - flag_action_items
output_contract:
  required:
    - summary
    - total_unread
    - priority_items
    - action_taken
    - verification
    - confidence
---

# Gmail Triage

Inspired by [Google Workspace CLI gmail+triage](https://github.com/googleworkspace/cli).

## When to use

- Morning inbox check: "what's in my email?"
- Quick scan before a meeting
- Looking for urgent messages from specific people
- End-of-day inbox sweep

## When NOT to use

- Sending emails (use google-gmail)
- Reading a specific known email (use google-gmail)
- Searching for old emails (use google-gmail with search query)

## Workflow

### Step 1 — Authenticate

```bash
python3 tools/google_workspace.py auth status
```

If not authenticated, direct user to `python3 scripts/gw-auth.py`.

### Step 2 — Fetch unread messages

```bash
python3 tools/google_workspace.py gmail search "is:unread in:inbox" --max-results 20
```

For filtered triage:
```bash
python3 tools/google_workspace.py gmail search "is:unread newer_than:1d"
python3 tools/google_workspace.py gmail search "is:unread from:boss@example.com"
```

### Step 3 — Categorize by priority

Sort messages into three buckets:

| Priority | Criteria | Action |
|----------|----------|--------|
| **Urgent** | From known VIPs, contains "urgent"/"ASAP"/"deadline", replies to your sent mail | Flag for immediate attention |
| **Action needed** | Questions directed to user, requests, invitations, attachments | Queue for response |
| **FYI** | Newsletters, automated notifications, CC'd threads | Skim or skip |

### Step 4 — Summarize

For each message, extract: **Sender**, **Subject**, **Time**, **1-line snippet**.

Group by priority bucket. Present as a clean table.

### Step 5 — Suggest actions

For urgent/action-needed items, suggest:
- "Reply to X about Y"
- "Review attached document from Z"
- "Accept/decline meeting invite for Thursday"

## Tool Usage Rules

- **Read-only.** Never modify, archive, label, or delete messages during triage.
- **Summarize, don't dump.** Show sender + subject + snippet, not full message bodies.
- **Default to 20 messages.** User can override with argument.
- **Respect privacy.** Don't expose email bodies in full unless the user asks to read a specific message.

## Failure Handling

| Error | Action |
|-------|--------|
| "Not authenticated" | Direct to `python3 scripts/gw-auth.py` |
| No unread messages | Report "inbox zero" — confidence: high |
| API quota exceeded | Wait 10s, retry once |

## Outputs / Contract

```
## Gmail Triage Contract
summary: <X unread messages triaged, Y urgent>
total_unread: <count>
priority_items:
  - urgent: <count and brief list>
  - action_needed: <count>
  - fyi: <count>
suggested_actions:
  - <action 1>
  - <action 2>
action_taken: read-only inbox scan
verification: <confirmed via gmail search results>
confidence: <high | medium | low>
```
