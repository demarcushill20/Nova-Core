---
name: google-gmail
description: "Search, read, and send Gmail messages using the NovaCore Google Workspace CLI. Auto-invoked when tasks involve email."
activation:
  keywords:
    - gmail
    - email
    - inbox
    - send email
    - mail
    - unread
  when:
    - User asks to check, search, read, or send email
    - Task requires sending a notification or report via email
    - Email content needs to be retrieved for context
tool_doctrine:
  gmail:
    workflow:
      - authenticate_first
      - search_before_read
      - confirm_before_send
      - cap_output_length
output_contract:
  required:
    - summary
    - action_taken
    - verification
    - confidence
---

# Gmail Skill

## When to use

- Searching emails by sender, subject, date, label, or keyword
- Reading specific email messages or threads
- Sending emails (reports, notifications, summaries)
- Listing Gmail labels
- Browsing email threads

## When NOT to use

- Telegram messages — use the Telegram bot
- Calendar invites — use google-calendar skill
- File attachments in Drive — use google-drive skill

## CLI Reference

All commands output JSON. The CLI is at `tools/google_workspace.py`.

### Search emails
```bash
python3 tools/google_workspace.py gmail search "from:user@example.com newer_than:7d"
python3 tools/google_workspace.py gmail search "subject:invoice is:unread"
python3 tools/google_workspace.py gmail search "has:attachment filename:pdf"
```

### Read a specific email
```bash
python3 tools/google_workspace.py gmail read <message_id>
```

### Send email
```bash
python3 tools/google_workspace.py gmail send \
  --to "recipient@example.com" \
  --subject "NovaCore Daily Report" \
  --body "Report content here..."
```

### List labels
```bash
python3 tools/google_workspace.py gmail labels
```

### Search threads
```bash
python3 tools/google_workspace.py gmail threads "project update" --max-results 5
```

## Workflow

1. **Check auth** — run `python3 tools/google_workspace.py auth status` to verify credentials are valid.
2. **Execute operation** — run the appropriate gmail subcommand.
3. **Parse JSON output** — all commands return structured JSON with status, counts, and data.
4. **Summarize for user** — extract key fields (sender, subject, snippet) rather than dumping raw JSON.

## Tool Usage Rules

- Always confirm with the user before sending emails unless explicitly pre-authorized.
- Gmail search uses the same query syntax as the Gmail web UI (from:, to:, subject:, is:unread, newer_than:, has:attachment, etc.)
- Message bodies are capped at 10,000 characters. For longer emails, note the truncation.
- Never expose raw email headers or metadata beyond From/To/Subject/Date.
- When searching, default to `--max-results 10` unless the user needs more.

## Failure Handling

| Error | Action |
|-------|--------|
| "Not authenticated" | Direct user to run `python3 scripts/gw-auth.py` |
| "Token expired" | CLI auto-refreshes; if it fails, re-run auth |
| "Quota exceeded" | Wait and retry; Gmail API allows 250 quota units/second |
| "Message not found" | Verify message ID; it may have been deleted |

## Gmail Search Query Cheatsheet

| Query | Meaning |
|-------|---------|
| `from:user@example.com` | From specific sender |
| `to:me` | Sent directly to you |
| `subject:invoice` | Subject contains "invoice" |
| `is:unread` | Unread messages |
| `is:starred` | Starred messages |
| `newer_than:7d` | Within last 7 days |
| `older_than:1m` | Older than 1 month |
| `has:attachment` | Has attachments |
| `filename:pdf` | Has PDF attachment |
| `label:important` | Has label |
| `in:inbox` | In inbox |
| `in:sent` | In sent folder |
