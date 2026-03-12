---
name: email-to-task
description: "Convert a Gmail message into a NovaCore task file in TASKS/. Use when the user says 'make this a task', 'create a task from that email', 'turn that email into a task', or wants to queue email-based work into the NovaCore task pipeline."
argument-hint: "[message-id-or-search-query]"
disable-model-invocation: false
activation:
  keywords: [email to task, make this a task, task from email, convert email, queue this email, email action item]
  when:
    - User wants to convert an email into actionable work
    - Email contains a request that needs tracking
    - Triaging inbox and queuing action items as tasks
tool_doctrine:
  email_to_task:
    workflow:
      - authenticate_first
      - read_email_first
      - extract_actionable_content
      - confirm_before_creating_task
      - write_to_tasks_directory
output_contract:
  required:
    - summary
    - source_email
    - task_file
    - action_taken
    - verification
    - confidence
---

# Email to Task

Inspired by [Google Workspace CLI workflow+email-to-task](https://github.com/googleworkspace/cli).

## When to use

- After triaging inbox: "turn Bob's email into a task"
- Converting an email request into tracked NovaCore work
- Queuing follow-up actions from email threads
- Batch converting multiple emails into tasks

## When NOT to use

- Creating tasks from scratch (just write to TASKS/ directly)
- Email that's purely informational with no action needed
- Tasks that already exist in TASKS/

## Workflow

### Step 1 — Find the email

If given a message ID:
```bash
python3 tools/google_workspace.py gmail read <message_id>
```

If given a search query:
```bash
python3 tools/google_workspace.py gmail search "<query>" --max-results 5
```

Present matches and let user confirm which email to convert.

### Step 2 — Extract task content

From the email, extract:
- **Title**: Derive from subject line (clean up Re:/Fwd: prefixes)
- **Description**: Summarize the actionable request (not the full email body)
- **Priority**: Infer from urgency signals (ASAP, deadline mentions, VIP sender)
- **Due date**: Extract if mentioned in the email body
- **Source reference**: Email message ID for traceability

### Step 3 — Generate task file

Determine the next task number:
```bash
ls TASKS/*.md 2>/dev/null | sort -t_ -k1 -n | tail -1
```

Create the task file following NovaCore format:

```markdown
# [Task Title derived from email subject]

**Source**: Gmail message [message_id] from [sender] on [date]
**Priority**: [high | medium | low]
**Due**: [date if found, otherwise "unset"]

## Description

[Summarized actionable content from the email]

## Original Context

From: [sender]
Subject: [subject]
Date: [date]
Snippet: [first 200 chars of body]

## Acceptance Criteria

- [ ] [Inferred from email request]
- [ ] Reply to sender confirming completion
```

### Step 4 — Write to TASKS/

Write the file as `TASKS/NNNN_<slug>.md` where NNNN is the next sequence number.

### Step 5 — Confirm

Report the created task file path and contents summary.

## Tool Usage Rules

- **Always read the email first.** Never create a task from just a subject line.
- **Summarize, don't copy.** The task description should be the actionable essence, not the full email.
- **Confirm before writing.** Show the draft task content before creating the file.
- **Include source reference.** Always link back to the original message ID.
- **One task per email.** If an email contains multiple requests, create separate tasks.
- **Follow TASKS/ naming convention.** Sequential numbering with descriptive slug.

## Failure Handling

| Error | Action |
|-------|--------|
| Email not found | Report "message not found" and suggest search query |
| Gmail not authenticated | Direct to `python3 scripts/gw-auth.py` |
| TASKS/ directory missing | Create it |
| Email is purely informational | Report "no actionable content found" — don't create task |

## Outputs / Contract

```
## Email to Task Contract
summary: <created task [NNNN] from email "[subject]" by [sender]>
source_email:
  message_id: <id>
  from: <sender>
  subject: <subject>
  date: <date>
task_file: <TASKS/NNNN_slug.md>
priority: <high | medium | low>
due_date: <date or "unset">
action_taken: read email → extracted action items → wrote task file
verification: <task file exists and contains source reference>
confidence: <high | medium | low>
```
