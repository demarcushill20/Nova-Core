---
name: memory-recall
description: "Retrieve knowledge from Fusion Memory using semantic search, temporal queries, or session replay. Auto-invoked when tasks need prior decisions, context, research findings, or session history."
disable-model-invocation: false
allowed-tools:
  - mcp__nova-memory__query_memory
  - mcp__nova-memory__get_recent_events
  - mcp__nova-memory__get_last_checkpoint
  - mcp__nova-memory__get_session_events
  - mcp__nova-memory__check_health
tool_doctrine:
  memory_reads:
    workflow:
      - choose_retrieval_mode
      - targeted_queries_first
      - synthesize_dont_dump
      - cite_memory_ids
activation:
  keywords:
    - recall
    - remember
    - prior session
    - previous session
    - where were we
    - what did we do
output_contract:
  required:
    - summary
    - retrieval_mode
    - results_returned
    - sources
    - confidence
---

# Memory Recall

## When to use

- Resuming work from a prior session — "where were we?", "what did we do last?"
- Recalling a past decision — "what embedding model did we choose?"
- Checking if something was already researched — "did we look into Redis Streams?"
- Finding prior context about a topic — "what do we know about the Pinecone SDK?"
- Getting recent activity — "what happened in the last 10 events?"
- Replaying a session — "show me everything from session X"

## When NOT to use

- Searching external/public information — use web-research instead
- Reading current file contents — use Read tool directly
- Checking Obsidian vault notes — use reading-obsidian-memory instead
- Storing new knowledge — use memory-store instead

## Inputs

- **query**: What to search for (required for semantic search)
- **mode**: Retrieval strategy — `semantic`, `temporal`, `session`, `checkpoint` (default: auto-detect)
- **n**: Number of results for temporal queries (default: 20, max: 200)
- **project**: Filter by project scope
- **filters**: Optional metadata filters (category, tags, memory_type, since_seq, since_time)

## Retrieval Modes

### Auto-Detection (Default)

The query router automatically detects intent:

| Query pattern | Detected mode | Tool used |
|--------------|---------------|-----------|
| "what is X", "explain Y", "how does Z work" | SEMANTIC | `query_memory` |
| "last session", "most recent", "where were we" | TEMPORAL | `get_recent_events` |
| "latest changes to X", "recent decisions about Y" | TEMPORAL_SEMANTIC | `query_memory` (auto-routed) |
| "what happened in session abc-123" | SESSION | `get_session_events` |
| "last checkpoint", "where did we leave off" | CHECKPOINT | `get_last_checkpoint` |

### Manual Mode Override

When auto-detection is insufficient, pick the mode explicitly:

**Semantic** — when you need conceptual similarity matching:
```
Tool: mcp__nova-memory__query_memory
Args: {
  "query": "embedding model selection rationale",
  "top_k_final": 5
}
```

**Temporal** — when you need chronological ordering, no semantic matching:
```
Tool: mcp__nova-memory__get_recent_events
Args: {
  "n": 10,
  "project": "nova-core",
  "memory_type": "decision"
}
```

**Session replay** — when you need everything from one session:
```
Tool: mcp__nova-memory__get_session_events
Args: {
  "session_id": "session-2026-03-08",
  "limit": 50
}
```

**Checkpoint** — when you need the latest session boundary:
```
Tool: mcp__nova-memory__get_last_checkpoint
Args: {
  "project": "nova-core"
}
```

## Workflow

### Step 1 — Identify Retrieval Strategy

Ask: "Am I looking for _what_ (semantic) or _when_ (temporal)?"

- **What**: Use `query_memory` — finds conceptually relevant memories regardless of when they were stored
- **When**: Use `get_recent_events` — finds the N most recent events by chronological order
- **Where we left off**: Use `get_last_checkpoint` — finds the most recent session boundary
- **Specific session**: Use `get_session_events` — replays all events from a named session

### Step 2 — Execute Targeted Query

Start narrow. Prefer specific queries over broad ones:

- Good: `"text-embedding-3-small selection decision"` (specific, keyword-rich)
- Bad: `"what do we know"` (too broad, will match everything)

For temporal queries, use filters to reduce noise:
- `project` — scope to relevant project
- `memory_type` — e.g., only `decision` items
- `since_seq` — only events after a known sequence number

### Step 3 — Evaluate Results

Check result quality before synthesizing:
- Are results relevant to the question?
- If semantic results are poor, try rephrasing with different keywords
- If temporal results are empty, the memory store may not have data for that scope — report this clearly

### Step 4 — Synthesize Findings

Do NOT dump raw results. Extract and present the relevant knowledge:
- Lead with the answer
- Reference memory IDs for traceability
- Note confidence based on result relevance and coverage

### Step 5 — Chain If Needed

If initial retrieval is insufficient:
1. Rephrase and re-query (different keywords)
2. Broaden scope (remove project filter, increase top_k)
3. Switch modes (temporal → semantic, or vice versa)
4. Report no findings if nothing relevant exists — do not fabricate

Maximum 3 retrieval attempts per invocation.

## Tool Usage Rules

- **Read-only.** This skill never writes, updates, or deletes memories.
- **Synthesize, don't dump.** Never return raw JSON to the user. Extract and present relevant knowledge.
- **Cite memory IDs.** Reference `id` fields so findings are traceable.
- **Bounded retrieval.** Maximum 3 query attempts per invocation. Maximum 200 events per temporal query.
- **No fabrication.** Only report findings from actual memory content. If nothing relevant is found, say so.
- **Project scoping.** Always include `project` filter when the query is project-specific.
- **Prefer temporal for recency.** "What did we just do" → `get_recent_events`, not `query_memory`.

## Failure Handling

- If `query_memory` returns 0 results: rephrase with different keywords, try without filters
- If temporal queries return empty: the memory store has no events for that scope — this is normal for new projects
- If `check_health` shows a backend is down: report which component is unavailable and what retrieval modes are affected
- If results are all low-relevance: report "no strong matches found" with confidence: low

## Outputs / Contract

```
## Memory Recall Contract
summary: <what was found, in 1-2 sentences>
retrieval_mode: <semantic | temporal | session | checkpoint>
query: <the query or parameters used>
results_returned: <N>
sources:
  - id: <memory_id>  type: <memory_type>  relevance: <high|medium|low>
  - ...
confidence: <high | medium | low> — <justification>
```

## Examples

### Example 1: Semantic recall of a decision

**User**: "What embedding model are we using?"

**Retrieval**:
```
Tool: mcp__nova-memory__query_memory
Args: {"query": "embedding model selection", "top_k_final": 5}
```

**Contract**:
```
summary: Found decision to use text-embedding-3-small (5x cheaper than ada-002, 1536 dims)
retrieval_mode: semantic
query: "embedding model selection"
results_returned: 3
sources:
  - id: a1b2c3  type: decision  relevance: high
  - id: d4e5f6  type: research  relevance: medium
confidence: high — direct match on decision record
```

### Example 2: Temporal — resuming work

**User**: "Where were we?"

**Retrieval**:
```
Tool: mcp__nova-memory__get_last_checkpoint
Args: {"project": "nova-core"}
```
Then:
```
Tool: mcp__nova-memory__get_recent_events
Args: {"n": 5, "project": "nova-core"}
```

**Contract**:
```
summary: Last checkpoint was session-2026-03-07 (completed Phase 5 graph time model). 5 events since then, all Phase 6 Redis timeline work.
retrieval_mode: checkpoint + temporal
query: last checkpoint + 5 recent events
results_returned: 6
sources:
  - id: cp-001  type: checkpoint  relevance: high
  - id: evt-45  type: scratch  relevance: medium
confidence: high — checkpoint provides clear session boundary
```

### Example 3: Session replay

**User**: "Show me everything from yesterday's session"

**Retrieval**:
```
Tool: mcp__nova-memory__get_session_events
Args: {"session_id": "session-2026-03-07", "limit": 50}
```

**Contract**:
```
summary: Session session-2026-03-07 contained 12 events: 8 research, 3 decisions, 1 checkpoint
retrieval_mode: session
query: session_id=session-2026-03-07
results_returned: 12
sources:
  - id: evt-33  type: research  relevance: high
  - id: evt-34  type: decision  relevance: high
  - ... (10 more)
confidence: high — graph traversal returned complete session
```
