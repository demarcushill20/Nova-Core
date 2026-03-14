# Open-Loop Tracking Specification (Phase 7, Step 7.1)

## Purpose

Open-loop tracking gives Nova-Core explicit visibility into unresolved work,
pending decisions, blocked tasks, and project continuity state. Instead of
relying on implicit artifacts scattered across sessions, open loops are
first-class objects with lifecycle state, provenance, and structured metadata.

## What Is an Open Loop?

An **open loop** is any piece of work that has been identified but not yet
completed or resolved. Examples:

- A task that failed and needs investigation
- A plan with steps that haven't been executed
- A session that ended with unfinished threads
- A decision that was deferred for later
- A blocked dependency that needs follow-up

## Design Principles

1. **Conservative creation** — Only create loops from clear signals (task failures,
   explicit open threads, plan follow-ups). Never from weak or ambiguous events.
2. **Explicit resolution** — Loops are only resolved with stated evidence. No
   automatic closure without clear reason.
3. **Lifecycle tracking** — Every status change is appended to a history log.
4. **Deduplication** — Content-based dedupe prevents the same unresolved work
   from spawning multiple loops.
5. **Anti-spam** — Input quality gates reject trivially short or low-quality
   loop descriptions.
6. **File-based persistence** — Loops are stored as individual JSON files in
   `STATE/open_loops/`, using atomic tmp+rename writes.

## Open Loop Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| loop_id | str | Yes | Unique ID (ol-{timestamp}-{hash}) |
| title | str | Yes | Short description |
| summary | str | Yes | Detailed description of what's unresolved |
| source | str | Yes | Who/what identified this loop |
| project | str | Yes | Related project (default: nova-core) |
| status | str | Yes | Current lifecycle status |
| opened_at | str | Yes | ISO timestamp of creation |
| updated_at | str | Yes | ISO timestamp of last update |
| due_hint | str | No | Informal time sensitivity |
| blocker | str | No | What's blocking progress |
| owner | str | No | Actor responsible |
| related_task_ids | list | No | Related task stems |
| related_files | list | No | Related file paths |
| related_memories | list | No | Related memory IDs |
| confidence | str | Yes | Confidence level (low/medium/high) |
| closure_reason | str | No | Why resolved/rejected (≥5 chars required) |
| closure_evidence | str | No | Evidence of resolution |
| tags | list | No | Categorization tags |
| history | list | Yes | Audit trail of status changes |

## Anti-Spam Safeguards

| Check | Threshold | Effect |
|-------|-----------|--------|
| Title length | < 5 chars | Rejected (insufficient evidence) |
| Summary length | < 10 chars | Rejected (insufficient evidence) |
| Duplicate detection | SHA-256 of project|title | Rejected if active match exists |

## Integration Points

- **Router**: `track_open_loop()`, `detect_open_loops()`, `resolve_open_loop()`, `get_open_loops()`
- **Recall**: `open_loop_recall` intent injects active loops into recall results
- **Event detection**: `detect_loop_from_event()` for automatic loop creation from runtime events
- **Observability**: All operations emit slog trace events
