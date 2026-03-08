---
skill: memory-checkpoint-to-diary
version: "1.0"
trigger: auto
description: >
  Generates an Obsidian diary entry from a Fusion Memory session checkpoint.
  Writes to 00-inbox/ with diary tags for operator triage to 90-diary/.
  Invoke after create_checkpoint completes, or manually to backfill.

allowed-tools:
  # Fusion Memory — read checkpoint and session data
  - mcp__nova-memory__get_last_checkpoint
  - mcp__nova-memory__get_session_events
  - mcp__nova-memory__get_recent_events
  # Obsidian Vault — search for dupes, validate, write
  - mcp__nova-vault__vault_search
  - mcp__nova-vault__vault_validate
  - mcp__nova-vault__vault_write

tool_doctrine:
  checkpoint_is_source: >
    The checkpoint from Fusion Memory is the single source of truth for
    the diary entry. Do not fabricate or embellish beyond what the
    checkpoint and session events contain.
  enrich_with_events: >
    After retrieving the checkpoint, call get_session_events to get the
    full event list for the session. This populates the Key Events table
    in the diary entry.
  dedup_before_write: >
    Always vault_search for the session_id before writing. If a diary
    entry for this session already exists in 00-inbox/ or 90-diary/,
    skip the write and report "already exists".
  validate_before_write: >
    Always call vault_validate before vault_write. Never write an
    invalid note.
  inbox_not_diary: >
    Write to 00-inbox/ with type "inbox", not to 90-diary/ which is
    human-managed. Tag with #action/move-to-diary so the operator
    knows to move it.
  bounded_events: >
    Include at most 20 events in the Key Events table. If the session
    has more, include the 20 most recent and note the total count.

output_contract:
  required_sections:
    - diary_status: >
        created: vault path of new diary entry
        skipped: reason (already exists, no checkpoint found, etc.)
        failed: error description
    - session_id: The session that was diarized
    - event_count: Number of events included in the diary
  format: >
    Brief confirmation message. The diary note itself is in Obsidian.
---

# Checkpoint-to-Diary Bridge

Generate an Obsidian diary entry from the most recent Fusion Memory
session checkpoint.

## Workflow

```
1. GET CHECKPOINT
   - Call get_last_checkpoint(project) to retrieve latest checkpoint
   - If no checkpoint found, report "no checkpoint" and stop
   - Extract: session_id, summary, open_threads, next_actions,
     last_event_seq, started_at, ended_at

2. DEDUP CHECK
   - Call vault_search(session_id) to check if diary already exists
   - If found in 00-inbox/ or 90-diary/, report "already exists" and stop

3. GET SESSION EVENTS
   - Call get_session_events(session_id, limit=20)
   - Build Key Events table from returned events
   - Note total count if more than 20

4. COMPOSE DIARY NOTE
   - Frontmatter: type=inbox, title, date, session metadata, diary tags
   - Body: Summary, Open Threads, Next Actions, Key Events table

5. VALIDATE
   - Call vault_validate(frontmatter) to check schema
   - If invalid, fix and retry once, then fail

6. WRITE
   - Call vault_write(path, frontmatter, body)
   - Path format: 00-inbox/diary-YYYY-MM-DD-<session-slug>.md
   - Report success with vault path
```

## Diary Note Template

### Frontmatter
```yaml
---
type: inbox
title: "Session Diary: <session_id>"
date: "<YYYY-MM-DD>"
source: nova-core-memory
tags:
  - "#type/inbox"
  - "#action/move-to-diary"
  - "#project/<project>"
---
```

### Body
```markdown
## Session Summary

<session_summary from checkpoint>

## Open Threads

- <thread 1>
- <thread 2>
(or "None" if empty)

## Next Actions

- <action 1>
- <action 2>
(or "None" if empty)

## Key Events

| Seq | Type | Content |
|-----|------|---------|
| 17  | checkpoint | Session checkpoint: ... |
| 16  | scratch | Pinecone metadata filters... |
| 15  | scratch | The query router uses... |

*<N> events in this session. Showing most recent 20.*

## Metadata

- **Session ID**: <session_id>
- **Project**: <project>
- **Last Event Seq**: <last_event_seq>
- **Source**: Fusion Memory checkpoint
```

## Rules

1. Never write to `90-diary/` — it is human-managed. Always use `00-inbox/`.
2. Never fabricate content — only use data from checkpoint and session events.
3. Always dedup — one diary entry per session_id.
4. Always validate before writing.
5. Truncate event content to 80 characters in the Key Events table.
6. If checkpoint has no open_threads or next_actions, write "None".
7. Max 6 tool calls per invocation.

## Failure Handling

| Situation | Action |
|-----------|--------|
| No checkpoint found | Report "no checkpoint for project" |
| Diary already exists | Report "diary for session X already exists" with path |
| Validation fails | Fix frontmatter and retry once |
| Vault write fails | Report error, do not retry |
| Session events empty | Write diary without Key Events table |
| Fusion Memory unavailable | Report health issue, suggest check_health |
