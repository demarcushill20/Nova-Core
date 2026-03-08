---
name: memory-store
description: "Store durable knowledge, decisions, and context into Fusion Memory (Pinecone + Neo4j + Redis). Auto-invoked when work produces insights, decisions, or context worth persisting beyond the current session."
disable-model-invocation: false
allowed-tools:
  - mcp__nova-memory__upsert_memory
  - mcp__nova-memory__bulk_upsert_memory
  - mcp__nova-memory__check_health
tool_doctrine:
  memory_writes:
    workflow:
      - classify_before_store
      - structured_metadata_always
      - one_concept_per_item
      - never_store_secrets
output_contract:
  required:
    - summary
    - ids_stored
    - memory_types
    - verification
    - confidence
---

# Memory Store

## When to use

- A decision was made that should survive across sessions (architecture choice, tool selection, config decision)
- Research findings need to be persisted for future retrieval
- The user explicitly asks to "remember this", "store this", or "save this to memory"
- A workflow produced reusable context (error patterns, debugging insights, environment facts)
- Bulk storing extracted knowledge from documents, conversations, or research

## When NOT to use

- Ephemeral scratch work that won't matter next session
- Storing secrets, API keys, credentials, or tokens — NEVER store these
- Storing raw file contents — memory is for distilled knowledge, not file backups
- Duplicating information already in the Obsidian vault — check first
- Logging or audit trails — use LOGS/ for those

## Inputs

- **content**: The knowledge to store (required). Should be a clear, self-contained statement.
- **memory_type**: Classification — `decision`, `research`, `context`, `pattern`, `scratch` (default: `scratch`)
- **project**: Project scope (e.g., `nova-core`, `fusion-memory`)
- **tags**: Categorization tags for retrieval
- **session_id**: Current session identifier (for graph linking)

## Workflow

### Step 1 — Classify the Knowledge

Before storing, determine what type of memory this is:

| Type | Use for | Example |
|------|---------|---------|
| `decision` | Choices that constrain future work | "Chose text-embedding-3-small over ada-002" |
| `research` | Findings from investigation | "Pinecone v8 removed Index import from top-level" |
| `context` | Environment facts, configurations | "VPS has python3 not python, Neo4j on port 7687" |
| `pattern` | Reusable techniques | "Over-fetch 5x from Pinecone, sort client-side" |
| `scratch` | Temporary/unclassified notes | Default if unsure |

### Step 2 — Structure the Content

Each memory item should be:
- **Self-contained**: Understandable without surrounding context
- **Atomic**: One concept per item. Split compound knowledge into separate items.
- **Retrievable**: Written so keyword search will find it later

Bad: "We did some stuff with Redis and Pinecone today"
Good: "Phase 6 uses Redis sorted sets (ZADD/ZREVRANGE) for O(log N) chronological queries, replacing Pinecone dummy-vector over-fetch"

### Step 3 — Attach Metadata

Always provide structured metadata:

```
Tool: mcp__nova-memory__upsert_memory
Args: {
  "content": "Clear, self-contained knowledge statement",
  "metadata": {
    "memory_type": "decision",
    "project": "nova-core",
    "tags": ["architecture", "embedding"],
    "session_id": "session-2026-03-08"
  }
}
```

System auto-injects: `event_seq`, `event_time`. Do not set these manually.

### Step 4 — Bulk Store When Appropriate

For 3+ related items, use `bulk_upsert_memory` to batch allocate consecutive event_seq numbers:

```
Tool: mcp__nova-memory__bulk_upsert_memory
Args: {
  "items": [
    {"content": "Item 1", "metadata": {"memory_type": "research", "project": "nova-core"}},
    {"content": "Item 2", "metadata": {"memory_type": "research", "project": "nova-core"}},
    {"content": "Item 3", "metadata": {"memory_type": "research", "project": "nova-core"}}
  ]
}
```

Maximum 500 items per bulk call.

### Step 5 — Verify Storage

After storing, confirm the upsert succeeded by checking the returned ID and status.

## Tool Usage Rules

- **Never store secrets.** No API keys, passwords, tokens, or credentials. Ever.
- **One concept per item.** Compound knowledge must be split into atomic items.
- **Always include memory_type.** Default is `scratch` but prefer explicit classification.
- **Always include project.** Scopes retrieval and prevents cross-project pollution.
- **Content is the primary retrieval signal.** Write content for future search — use keywords that your future self would query.
- **Check health first if unsure.** Use `check_health` before bulk operations to verify all backends are up.
- **Idempotent by ID.** Providing the same `id` twice updates the existing item. Omitting `id` generates a content-hash ID.

## Failure Handling

- If upsert returns an error, check `check_health` to identify which backend failed
- If Pinecone fails but Neo4j succeeds, the service auto-rolls back the Neo4j write — retry the full upsert
- If bulk_upsert returns `partial_success`, report which items failed (by index) and retry only those
- Never silently drop failed writes — always report failures in the contract

## Outputs / Contract

```
## Memory Store Contract
summary: <what was stored and why>
ids_stored:
  - <id_1>
  - <id_2>
memory_types: [decision, research, ...]
project: <project scope>
items_attempted: <N>
items_succeeded: <N>
verification: <confirmed via upsert response status>
confidence: <high | medium | low>
```

## Examples

### Example 1: Storing a decision

**Situation**: Chose text-embedding-3-small over ada-002

```
Tool: mcp__nova-memory__upsert_memory
Args: {
  "content": "Selected text-embedding-3-small as the embedding model for Fusion Memory MCP. 5x cheaper than ada-002 ($0.02 vs $0.10 per 1M tokens), better benchmark performance, same 1536 dimensions. Pinecone index configured with cosine metric at 1536 dims.",
  "metadata": {
    "memory_type": "decision",
    "project": "fusion-memory",
    "tags": ["embedding", "model-selection", "pinecone"]
  }
}
```

**Contract**:
```
summary: Stored embedding model selection decision (text-embedding-3-small)
ids_stored: [a1b2c3d4...]
memory_types: [decision]
project: fusion-memory
items_attempted: 1
items_succeeded: 1
verification: upsert returned status=success
confidence: high
```

### Example 2: Bulk storing research findings

**Situation**: Completed research on Pinecone SDK v8 changes

```
Tool: mcp__nova-memory__bulk_upsert_memory
Args: {
  "items": [
    {"content": "Pinecone SDK v8 removed top-level Index import. Use Pinecone().Index(name) instead.", "metadata": {"memory_type": "research", "project": "fusion-memory", "tags": ["pinecone", "sdk", "migration"]}},
    {"content": "Pinecone SDK v8 requires ServerlessSpec for index creation: pc.create_index(name, dimension, metric, spec=ServerlessSpec(cloud, region)).", "metadata": {"memory_type": "research", "project": "fusion-memory", "tags": ["pinecone", "sdk", "migration"]}},
    {"content": "System urllib3 < 2.0 causes latin-1 encoding errors with non-ASCII metadata in Pinecone upserts. Fix: pip install --upgrade urllib3.", "metadata": {"memory_type": "research", "project": "fusion-memory", "tags": ["pinecone", "urllib3", "debugging"]}}
  ]
}
```

**Contract**:
```
summary: Stored 3 Pinecone SDK v8 migration findings
ids_stored: [x1, x2, x3]
memory_types: [research]
project: fusion-memory
items_attempted: 3
items_succeeded: 3
verification: bulk_upsert returned status=success, 3/3 succeeded
confidence: high
```
