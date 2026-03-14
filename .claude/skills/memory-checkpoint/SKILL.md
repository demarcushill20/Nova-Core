---
name: memory-checkpoint
description: "Create session checkpoints in Fusion Memory to mark session boundaries, enable session resumption, and build the temporal graph chain. Invoke at natural session boundaries or when the user says they're done for now."
disable-model-invocation: false
allowed-tools:
  - mcp__nova-memory__create_checkpoint
  - mcp__nova-memory__get_last_checkpoint
  - mcp__nova-memory__get_recent_events
  - mcp__nova-memory__check_health
tool_doctrine:
  checkpoints:
    workflow:
      - gather_session_context
      - summarize_before_checkpoint
      - link_to_previous_session
      - include_next_actions
activation:
  keywords:
    - checkpoint
    - session boundary
    - save session
    - wrap up
    - save progress
output_contract:
  required:
    - summary
    - checkpoint_id
    - session_id
    - last_event_seq
    - verification
    - confidence
---

# Memory Checkpoint

## When to use

- End of a work session — user says "that's it for now", "save progress", "let's wrap up"
- Natural milestone — completed a significant phase, feature, or investigation
- Before context compaction — to preserve session state before the conversation window compresses
- User explicitly requests: "checkpoint", "save session", "create a checkpoint"
- Switching between major topics or projects within a session

## When NOT to use

- Mid-task with no clear boundary — wait for a natural stopping point
- Storing individual facts — use memory-store instead
- Every minor action — checkpoints mark boundaries, not individual events

## Inputs

- **session_id**: Unique session identifier (required). Convention: `session-YYYY-MM-DD` or `session-YYYY-MM-DD-N` for multiple per day.
- **session_summary**: What was accomplished (required). Should be 2-5 sentences covering key outcomes.
- **project**: Project scope (strongly recommended)
- **open_threads**: Unfinished work items (optional but valuable for resumption)
- **next_actions**: What should happen next (optional but valuable for resumption)

## Workflow

### Step 1 — Gather Session Context

Before creating the checkpoint, understand what happened this session:

```
Tool: mcp__nova-memory__get_last_checkpoint
Args: {"project": "nova-core"}
```

This tells you:
- The previous session boundary (where this session started)
- The `last_event_seq` of the previous checkpoint (events since then are this session's work)

Then get recent events since the last checkpoint:
```
Tool: mcp__nova-memory__get_recent_events
Args: {"n": 20, "project": "nova-core"}
```

### Step 2 — Compose the Summary

Write a session summary that answers:
1. **What was the goal?** — "Implement Phase 6 Redis timeline"
2. **What was accomplished?** — "Added Redis sorted sets, dual-backend SequenceService, 29 tests"
3. **What's the current state?** — "All 188 tests passing, committed and pushed"

Keep it 2-5 sentences. Factual, not narrative.

### Step 3 — Identify Open Threads and Next Actions

**Open threads** — work that was started but not finished:
- "Redis fakeredis tests skipped (14 tests need fakeredis package)"
- "Reranker model loading shows deprecation warning"

**Next actions** — concrete next steps for the next session:
- "Connect Fusion Memory MCP to Claude Code"
- "Run integration test with live Pinecone index"

### Step 4 — Create the Checkpoint

```
Tool: mcp__nova-memory__create_checkpoint
Args: {
  "session_id": "session-2026-03-08",
  "session_summary": "Completed Fusion Memory Phases 3-6: temporal retrieval, query routing, graph time model, and Redis timeline store. All 188 tests passing across 6 phases. Committed and pushed all changes.",
  "project": "nova-core",
  "open_threads": ["14 Redis tests skipped without fakeredis"],
  "next_actions": ["Connect MCP server to Claude Code", "End-to-end integration test"]
}
```

The system automatically:
- Snapshots `last_event_seq` (current counter value)
- Creates a Neo4j Session node
- Links FOLLOWS edge to the previous session's checkpoint
- Records in Redis timeline

### Step 5 — Verify the Chain

After creating, confirm the checkpoint was stored and linked:

```
Tool: mcp__nova-memory__get_last_checkpoint
Args: {"project": "nova-core"}
```

Verify:
- The returned checkpoint has your `session_id`
- `last_event_seq` is populated and greater than the previous checkpoint's value

## Tool Usage Rules

- **One checkpoint per session boundary.** Don't create redundant checkpoints for the same session.
- **Session IDs must be unique.** Convention: `session-YYYY-MM-DD` or `session-YYYY-MM-DD-N`.
- **Summaries must be factual.** State what was done, not what was attempted or planned.
- **Always include project.** Enables scoped checkpoint retrieval.
- **Open threads and next_actions are optional but strongly recommended.** They're the primary value for session resumption.
- **Check the previous checkpoint first.** Ensures the FOLLOWS chain stays connected and your summary covers the gap.

## Failure Handling

- If `create_checkpoint` fails, check `check_health` — the checkpoint depends on Pinecone, Neo4j, and the SequenceService
- If the previous checkpoint can't be retrieved, create the checkpoint anyway — the system handles missing FOLLOWS gracefully
- If `session_id` or `session_summary` is empty/whitespace, the service rejects it — provide valid values

## Outputs / Contract

```
## Checkpoint Contract
summary: <what was checkpointed>
checkpoint_id: <returned memory ID>
session_id: <session identifier>
last_event_seq: <sequence number snapshot>
previous_session: <previous session_id or "none (first checkpoint)">
open_threads: [list or "none"]
next_actions: [list or "none"]
verification: <confirmed via get_last_checkpoint retrieval>
confidence: <high | medium | low>
```

## Examples

### Example 1: End-of-day checkpoint

**Situation**: Completed Phases 3-6 of Fusion Memory upgrade

```
Tool: mcp__nova-memory__get_last_checkpoint
Args: {"project": "fusion-memory"}
→ Previous: session-2026-03-07, last_event_seq=24

Tool: mcp__nova-memory__create_checkpoint
Args: {
  "session_id": "session-2026-03-08",
  "session_summary": "Completed Fusion Memory Phases 3-6: temporal retrieval tools, temporal-first query router, graph time model (Session nodes + FOLLOWS chain), and Redis timeline store with dual-backend SequenceService. All 188 tests passing. README updated.",
  "project": "fusion-memory",
  "open_threads": ["14 fakeredis tests skipped", "Reranker deprecation warning"],
  "next_actions": ["Connect MCP to Claude Code", "Add OpenAI credits for embedding calls", "Integration test"]
}
→ Returns: id=cp-xyz, status=success, session_id=session-2026-03-08

Tool: mcp__nova-memory__get_last_checkpoint
Args: {"project": "fusion-memory"}
→ Confirms: session-2026-03-08, last_event_seq=48
```

**Contract**:
```
summary: Checkpointed Phase 3-6 completion with 188 tests passing
checkpoint_id: cp-xyz
session_id: session-2026-03-08
last_event_seq: 48
previous_session: session-2026-03-07
open_threads: [14 fakeredis tests skipped, reranker deprecation warning]
next_actions: [Connect MCP to Claude Code, add OpenAI credits, integration test]
verification: confirmed via get_last_checkpoint — returned correct session_id and event_seq
confidence: high
```

### Example 2: Mid-session milestone checkpoint

**Situation**: Switching from backend work to frontend work

```
Tool: mcp__nova-memory__create_checkpoint
Args: {
  "session_id": "session-2026-03-08-backend",
  "session_summary": "Completed all backend API endpoints. 12 new routes, 45 tests passing. Database migrations applied.",
  "project": "nova-core",
  "next_actions": ["Build frontend dashboard", "Wire up API client"]
}
```

**Contract**:
```
summary: Checkpointed backend completion before switching to frontend
checkpoint_id: cp-abc
session_id: session-2026-03-08-backend
last_event_seq: 67
previous_session: session-2026-03-08
open_threads: none
next_actions: [Build frontend dashboard, Wire up API client]
verification: confirmed via get_last_checkpoint
confidence: high
```
