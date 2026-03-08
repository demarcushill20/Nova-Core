# Fusion Memory MCP — Chronological Memory Upgrade Plan

## Problem Statement

Pure semantic retrieval (Pinecone vectors + Neo4j graph + RRF + reranking) returns "most similar" results, not "most recent." When a user asks "what did we do last session?" the system returns semantically similar memories from any time period, not the actual latest events. This is a **retrieval policy + metadata problem**, not a model problem.

## Current Architecture (Baseline)

```
mcp_server.py (FastMCP, stdio)
  ├── query_memory      → MemoryService.perform_query()
  ├── upsert_memory     → MemoryService.perform_upsert()
  ├── bulk_upsert_memory→ MemoryService.perform_bulk_upsert()
  ├── delete_memory     → MemoryService.perform_delete()
  └── check_health      → MemoryService.check_health()

MemoryService pipeline:
  query:  embedding → parallel(Pinecone + Neo4j) → RRF merge → cross-encoder rerank
  upsert: embedding → parallel(Pinecone + Neo4j) with rollback on partial failure

Storage:
  Pinecone: 1536-dim vectors (text-embedding-ada-002), metadata includes {text, ...user metadata}
  Neo4j:    :base nodes with entity_id, text, flat metadata properties

QueryRouter: keyword-based → VECTOR | GRAPH | HYBRID (currently unused for routing, always runs both)

ID generation: MD5(content) or caller-provided
```

**What's missing:**
- No `event_time` or `event_seq` — memories have no ordering
- No session concept — "last session" is meaningless to the system
- No temporal retrieval path — all queries go through semantic similarity
- No dedicated recency tools — agents must guess from embeddings

---

## Implementation Plan: 6 Phases

Each phase is self-contained, testable, and builds on the previous.

---

### Phase 1: Write-Time Chronology Enforcement

**Goal:** Every memory item gets `event_time` + `event_seq` automatically at write time. No agent behavior changes required — the system enforces it.

**Files modified:**
- `app/services/memory_service.py` — inject chronology fields in `perform_upsert()` and `perform_bulk_upsert()`
- `app/config.py` — add `EVENT_SEQ_FILE` setting (path to sequence counter file)
- `MEMORY_SCHEMA.md` — update canonical schema

**New file:**
- `app/services/sequence_service.py` — monotonic sequence counter

#### Step 1.1: Sequence Service

Create `app/services/sequence_service.py`:

```python
class SequenceService:
    """Atomic monotonic sequence counter backed by a local file."""

    def __init__(self, seq_file: str = "/data/event_seq.counter"):
        self._seq_file = seq_file
        self._lock = asyncio.Lock()

    async def next_seq(self) -> int:
        """Returns the next monotonic sequence number (atomic)."""
        async with self._lock:
            current = self._read_counter()
            next_val = current + 1
            self._write_counter(next_val)
            return next_val

    async def next_batch(self, count: int) -> List[int]:
        """Returns `count` consecutive sequence numbers for bulk operations."""
        async with self._lock:
            current = self._read_counter()
            batch = list(range(current + 1, current + 1 + count))
            self._write_counter(current + count)
            return batch

    def current_seq(self) -> int:
        """Returns current counter value without incrementing."""
        return self._read_counter()
```

Why file-based first: Docker volume-persistent, zero dependencies. Redis upgrade in Phase 6.

#### Step 1.2: Auto-Inject Chronology in MemoryService

Modify `MemoryService.__init__()`:
```python
self.sequence_service = SequenceService(settings.EVENT_SEQ_FILE)
```

Modify `perform_upsert()` — after content validation, before embedding:
```python
# System-enforced chronology (cannot be forgotten)
metadata = metadata or {}
metadata["event_time"] = metadata.get("event_time") or datetime.now(timezone.utc).isoformat()
metadata["event_seq"] = await self.sequence_service.next_seq()
metadata["memory_type"] = metadata.get("memory_type", "scratch")
```

Modify `perform_bulk_upsert()` — allocate batch sequence numbers:
```python
seq_batch = await self.sequence_service.next_batch(len(normalized_items))
for i, item in enumerate(normalized_items):
    item["metadata"]["event_time"] = item["metadata"].get("event_time") or now_iso
    item["metadata"]["event_seq"] = seq_batch[i]
```

#### Step 1.3: Pinecone Metadata Indexing

Pinecone metadata filtering requires fields to be indexed. `event_seq` (integer) and `event_time` (string) will be stored as metadata and are filterable by default in Pinecone's serverless/starter indexes.

No Pinecone schema change needed — metadata fields are automatically indexed.

#### Step 1.4: Neo4j Property Addition

In `graph_client.py`, the `upsert_graph_data()` already unpacks `**metadata` into node properties. `event_seq` and `event_time` will be stored automatically.

Add a Neo4j index for ordering queries:
```python
async def _ensure_constraints(self):
    # ... existing entity_id constraint ...
    # Add index for temporal ordering
    await session.run(
        f"CREATE INDEX IF NOT EXISTS FOR (n:{NEO4J_NODE_LABEL}) ON (n.event_seq)"
    )
```

#### Step 1.5: Schema Update

Update `MEMORY_SCHEMA.md` canonical fields:

| Field | Type | Required | Injected By |
|---|---|---|---|
| `event_time` | string (ISO 8601 UTC) | **yes** | **system** (auto) |
| `event_seq` | integer (monotonic) | **yes** | **system** (auto) |
| `memory_type` | string enum | yes | system default `scratch` |
| `thread_id` | string | recommended | caller |
| `session_id` | string | recommended | caller |
| `agent_id` | string | recommended | caller |
| `project` | string | recommended | caller |

**Tests:**
- Upsert without `event_time` → system injects it
- Upsert with explicit `event_time` → system preserves it
- `event_seq` always system-assigned (never caller-provided)
- Bulk upsert: all items get consecutive `event_seq` values
- Sequence survives server restart (file-persisted)
- Concurrent upserts get strictly ordered sequences

**Acceptance:** Every memory in Pinecone and Neo4j has `event_seq` + `event_time` in metadata. No write can bypass this.

---

### Phase 2: Session Checkpoint System

**Goal:** Sessions become first-class objects. "What did we do last session?" becomes a deterministic query, not semantic guessing.

**Files modified:**
- `mcp_server.py` — add `create_checkpoint` and `get_last_checkpoint` tools
- `app/services/memory_service.py` — add checkpoint methods

#### Step 2.1: Checkpoint Schema

A checkpoint is a regular memory item with `memory_type = "checkpoint"` and extra required fields:

```python
CHECKPOINT_REQUIRED_FIELDS = {
    "session_id",        # stable session identifier
    "session_summary",   # 3-7 bullet summary of what changed
}

CHECKPOINT_OPTIONAL_FIELDS = {
    "started_at",        # ISO 8601
    "ended_at",          # ISO 8601
    "open_threads",      # list of strings
    "next_actions",      # list of strings
    "project",           # e.g. "NovaTrade"
    "thread_id",         # conversation grouping
}
```

`event_seq` and `event_time` are auto-injected by Phase 1. The checkpoint also records `last_event_seq` — a snapshot of the highest `event_seq` at checkpoint creation time.

#### Step 2.2: create_checkpoint MCP Tool

```python
@mcp.tool()
async def create_checkpoint(
    ctx: Context,
    session_id: str,
    session_summary: str,
    started_at: Optional[str] = None,
    ended_at: Optional[str] = None,
    open_threads: Optional[List[str]] = None,
    next_actions: Optional[List[str]] = None,
    project: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Creates a session checkpoint — a structured summary of a completed session.
    System auto-injects event_time, event_seq, and last_event_seq.
    """
```

Implementation in MemoryService:
```python
async def create_checkpoint(self, session_id, session_summary, **kwargs):
    last_seq = self.sequence_service.current_seq()
    metadata = {
        "memory_type": "checkpoint",
        "session_id": session_id,
        "session_summary": session_summary,
        "last_event_seq": last_seq,
        **{k: v for k, v in kwargs.items() if v is not None},
    }
    content = f"Session checkpoint: {session_id}\n\n{session_summary}"
    return await self.perform_upsert(content=content, metadata=metadata)
```

#### Step 2.3: get_last_checkpoint MCP Tool

```python
@mcp.tool()
async def get_last_checkpoint(
    ctx: Context,
    project: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Retrieves the most recent session checkpoint. Deterministic — does not use
    semantic search. Returns the checkpoint with the highest event_seq where
    memory_type == "checkpoint".
    """
```

Implementation: Pinecone metadata filter query:
```python
async def get_last_checkpoint(self, project=None, thread_id=None):
    filter_dict = {"memory_type": {"$eq": "checkpoint"}}
    if project:
        filter_dict["project"] = {"$eq": project}
    if thread_id:
        filter_dict["thread_id"] = {"$eq": thread_id}

    # Fetch top-K checkpoints, sort client-side by event_seq
    # Use a dummy query vector (zero vector or average embedding) with filter
    results = self.pinecone_client.query_vector(
        query_vector=[0.0] * 1536,  # dummy — we only care about filter
        top_k=20,
        filter=filter_dict,
    )
    if not results:
        return None
    # Sort by event_seq descending, return the latest
    results.sort(key=lambda r: r.get("metadata", {}).get("event_seq", 0), reverse=True)
    return results[0]
```

**Note:** Pinecone requires a vector for queries even with metadata filters. Using a zero vector with heavy reliance on the filter is a known pattern. Alternative: store checkpoints in a lightweight sidecar (see Phase 6 Redis).

**Tests:**
- Create checkpoint → verify `last_event_seq` matches current counter
- Create 3 checkpoints → `get_last_checkpoint` returns the one with highest `event_seq`
- Filter by project → only matching checkpoints returned
- No checkpoints exist → returns null/empty
- Checkpoint includes all system-injected fields (`event_time`, `event_seq`)

**Acceptance:** "What did we do last session?" is answered by fetching the latest checkpoint — deterministic, no embedding similarity involved.

---

### Phase 3: Temporal Retrieval Tools

**Goal:** Add dedicated MCP tools for time-ordered retrieval. Agents can explicitly request "last N events" without semantic similarity contaminating the results.

**Files modified:**
- `mcp_server.py` — add `get_recent_events` tool
- `app/services/memory_service.py` — add temporal query method
- `app/services/pinecone_client.py` — add filtered query helper

#### Step 3.1: get_recent_events MCP Tool

```python
@mcp.tool()
async def get_recent_events(
    ctx: Context,
    n: int = 20,
    project: Optional[str] = None,
    thread_id: Optional[str] = None,
    memory_type: Optional[str] = None,
    since_seq: Optional[int] = None,
    since_time: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Retrieves the N most recent memory events, ordered by event_seq (descending).
    Does NOT use semantic similarity — purely metadata-driven.

    Optional filters: project, thread_id, memory_type, since_seq, since_time.
    """
```

#### Step 3.2: Implementation Strategy

Pinecone supports metadata filtering but not sorting. Strategy:

```python
async def get_recent_events(self, n=20, **filters):
    # Build Pinecone metadata filter
    filter_dict = {}
    if filters.get("project"):
        filter_dict["project"] = {"$eq": filters["project"]}
    if filters.get("thread_id"):
        filter_dict["thread_id"] = {"$eq": filters["thread_id"]}
    if filters.get("memory_type"):
        filter_dict["memory_type"] = {"$eq": filters["memory_type"]}
    if filters.get("since_seq"):
        filter_dict["event_seq"] = {"$gte": filters["since_seq"]}
    if filters.get("since_time"):
        filter_dict["event_time"] = {"$gte": filters["since_time"]}

    # Over-fetch from Pinecone (fetch more than n, sort client-side)
    OVER_FETCH_FACTOR = 5
    raw_results = self.pinecone_client.query_vector(
        query_vector=[0.0] * 1536,  # dummy vector — filter-only query
        top_k=min(n * OVER_FETCH_FACTOR, 10000),
        filter=filter_dict if filter_dict else None,
    )

    # Sort by event_seq descending (client-side)
    raw_results.sort(
        key=lambda r: r.get("metadata", {}).get("event_seq", 0),
        reverse=True,
    )

    # Return top n
    return raw_results[:n]
```

#### Step 3.3: Neo4j Temporal Query (parallel path)

Add to `graph_client.py`:

```python
async def query_recent_events(self, n=20, filters=None):
    """Retrieve N most recent nodes ordered by event_seq DESC."""
    where_clauses = []
    params = {"limit": n}

    if filters:
        if filters.get("project"):
            where_clauses.append("n.project = $project")
            params["project"] = filters["project"]
        if filters.get("memory_type"):
            where_clauses.append("n.memory_type = $memory_type")
            params["memory_type"] = filters["memory_type"]
        if filters.get("since_seq"):
            where_clauses.append("n.event_seq >= $since_seq")
            params["since_seq"] = filters["since_seq"]

    where_str = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    cypher = f"""
    MATCH (n:{NEO4J_NODE_LABEL})
    {where_str}
    RETURN n.entity_id AS id, n.text AS text, n.event_seq AS event_seq,
           n.event_time AS event_time, n.memory_type AS memory_type, n AS props
    ORDER BY n.event_seq DESC
    LIMIT $limit
    """
    # ... execute and return ...
```

Neo4j can do real ORDER BY, making it the more reliable temporal query backend.

**Tests:**
- Insert 50 events → `get_recent_events(n=10)` returns the 10 with highest `event_seq`
- Filter by project → only matching events
- Filter by `since_seq` → only events after that sequence
- Events are in strict `event_seq` descending order
- Empty result when no events match filter

**Acceptance:** `get_recent_events` returns strictly ordered results. Zero semantic similarity involved.

---

### Phase 4: Temporal-First Query Router

**Goal:** Upgrade `QueryRouter` to detect recency-intent queries and route them through temporal retrieval first, semantic second. The existing `query_memory` tool becomes context-aware.

**Files modified:**
- `query_router.py` — add `TEMPORAL` routing mode + recency keyword detection
- `app/services/memory_service.py` — implement two-stage retrieval in `perform_query()`

#### Step 4.1: Add TEMPORAL Routing Mode

```python
class RoutingMode(Enum):
    VECTOR = auto()
    GRAPH = auto()
    HYBRID = auto()
    TEMPORAL = auto()         # NEW: recency-first retrieval
    TEMPORAL_SEMANTIC = auto() # NEW: temporal window + semantic refinement
```

Add temporal keywords:
```python
self.temporal_keywords = [
    "last", "latest", "most recent", "recently", "just did",
    "before we ended", "previous session", "what did we do",
    "last time", "earlier today", "yesterday", "last session",
    "what happened", "last thing", "final", "end of session",
    "current state", "where were we", "pick up where",
    "continuation", "resume", "catch up",
]
```

Route priority: temporal keywords detected → `TEMPORAL` (or `TEMPORAL_SEMANTIC` if both temporal + semantic keywords present).

#### Step 4.2: Two-Stage Retrieval in perform_query()

```python
async def perform_query(self, query_text, top_k_vector=50, top_k_final=15):
    routing_mode = self.query_router.route(query_text)

    if routing_mode == RoutingMode.TEMPORAL:
        # Stage 1: Pure temporal retrieval
        return await self._temporal_query(top_k=top_k_final)

    elif routing_mode == RoutingMode.TEMPORAL_SEMANTIC:
        # Stage 1: Get temporal window
        recent = await self._temporal_query(top_k=top_k_vector)
        if not recent:
            # Fallback to full semantic
            return await self._semantic_query(query_text, top_k_vector, top_k_final)

        # Stage 2: Semantic search WITHIN the temporal window
        min_seq = min(r["metadata"]["event_seq"] for r in recent)
        return await self._semantic_query(
            query_text, top_k_vector, top_k_final,
            filter={"event_seq": {"$gte": min_seq}}
        )

    else:
        # Existing behavior: full semantic pipeline
        return await self._semantic_query(query_text, top_k_vector, top_k_final)
```

#### Step 4.3: Backward Compatibility

The existing `query_memory` tool API stays identical. The routing change is internal. Users see better results for recency queries without any API changes.

**Tests:**
- "What did we do last session?" → routes to `TEMPORAL`
- "Explain our memory system" → routes to `HYBRID` (unchanged)
- "What's the latest change to the pipeline?" → routes to `TEMPORAL_SEMANTIC`
- Temporal query returns events sorted by `event_seq` DESC
- Temporal-semantic returns semantically relevant events from the recent window
- No regression on existing semantic queries

**Acceptance:** Recency queries automatically use temporal-first retrieval. Existing semantic queries are unaffected.

---

### Phase 5: Graph Time Model

**Goal:** Add `Session` nodes and temporal edges to Neo4j, enabling graph-based session traversal and relationship-aware chronological queries.

**Files modified:**
- `app/services/graph_client.py` — add Session node CRUD and temporal edges
- `app/services/memory_service.py` — link events to sessions during upsert

#### Step 5.1: Session Node Schema

```cypher
// Session node
(:Session {
    session_id: "session_abc123",
    started_at: "2026-03-08T10:00:00Z",
    ended_at: "2026-03-08T12:30:00Z",
    last_event_seq: 142,
    summary: "Implemented chronological memory...",
    project: "NovaTrade",
    thread_id: "thread_001"
})

// Relationships
(:Session)-[:INCLUDES]->(:base)           // session contains events
(:Session)-[:FOLLOWS]->(:Session)         // session ordering chain
(:base)-[:MENTIONS]->(:Entity)            // existing entity extraction
```

#### Step 5.2: Graph Client Additions

```python
async def create_session_node(self, session_id, started_at, **kwargs):
    """Create or merge a Session node."""
    cypher = """
    MERGE (s:Session {session_id: $session_id})
    SET s += $props
    RETURN s.session_id AS id
    """

async def link_event_to_session(self, event_id, session_id):
    """Create INCLUDES edge from Session to event node."""
    cypher = """
    MATCH (s:Session {session_id: $session_id})
    MATCH (e:base {entity_id: $event_id})
    MERGE (s)-[:INCLUDES]->(e)
    """

async def link_session_follows(self, current_session_id, previous_session_id):
    """Create FOLLOWS edge between sessions for ordering."""
    cypher = """
    MATCH (curr:Session {session_id: $current_id})
    MATCH (prev:Session {session_id: $previous_id})
    MERGE (curr)-[:FOLLOWS]->(prev)
    """

async def get_session_events(self, session_id, limit=50):
    """Get all events in a session, ordered by event_seq."""
    cypher = """
    MATCH (s:Session {session_id: $session_id})-[:INCLUDES]->(e:base)
    RETURN e.entity_id AS id, e.text AS text, e.event_seq AS seq,
           e.event_time AS time, e.memory_type AS type
    ORDER BY e.event_seq DESC
    LIMIT $limit
    """

async def get_latest_session(self, project=None):
    """Get the most recent Session node by last_event_seq."""
    where = "WHERE s.project = $project" if project else ""
    cypher = f"""
    MATCH (s:Session)
    {where}
    RETURN s
    ORDER BY s.last_event_seq DESC
    LIMIT 1
    """
```

#### Step 5.3: Auto-Link Events to Sessions

In `MemoryService.perform_upsert()`, if `session_id` is provided in metadata:
```python
if metadata.get("session_id"):
    await self.graph_client.link_event_to_session(
        event_id=item_id, session_id=metadata["session_id"]
    )
```

#### Step 5.4: Session Chain on Checkpoint

When `create_checkpoint()` is called, auto-link to previous session:
```python
prev_session = await self.graph_client.get_latest_session(project)
if prev_session:
    await self.graph_client.link_session_follows(session_id, prev_session["session_id"])
```

**Tests:**
- Create session → node exists in Neo4j with correct properties
- Upsert event with `session_id` → INCLUDES edge exists
- Create 3 sessions → FOLLOWS chain is correct
- `get_session_events` returns events in `event_seq` order
- `get_latest_session` returns the most recent session
- Session without events → returns empty event list

**Acceptance:** Sessions are graph-native objects with traversable relationships. "Show me last session's events" is a single graph query.

---

### Phase 6: Redis Timeline Store (Optional Enhancement)

**Goal:** Add Redis as a dedicated timeline index for strict ordering, atomic sequence generation, and fast recency queries. Pinecone remains the semantic store; Redis becomes the chronological source of truth.

**Files modified:**
- `app/config.py` — add Redis settings
- `app/services/sequence_service.py` — upgrade to Redis-backed atomic INCR
- `docker-compose.yml` — add Redis service

**New file:**
- `app/services/redis_timeline.py` — timeline index service

#### Step 6.1: Docker Compose Addition

```yaml
redis:
  image: redis:7-alpine
  container_name: nova_redis
  ports:
    - "6379:6379"
  volumes:
    - redis_data:/data
  command: redis-server --appendonly yes  # AOF persistence
  restart: unless-stopped
  networks:
    nova_network:
      aliases:
        - redis_db
```

#### Step 6.2: Redis-Backed Sequence Service

Replace file-based counter with Redis `INCR`:
```python
class RedisSequenceService:
    def __init__(self, redis_url):
        self._redis = aioredis.from_url(redis_url)

    async def next_seq(self, scope="global") -> int:
        return await self._redis.incr(f"nova:event_seq:{scope}")

    async def next_batch(self, count, scope="global") -> List[int]:
        pipe = self._redis.pipeline()
        for _ in range(count):
            pipe.incr(f"nova:event_seq:{scope}")
        results = await pipe.execute()
        return results

    async def current_seq(self, scope="global") -> int:
        val = await self._redis.get(f"nova:event_seq:{scope}")
        return int(val) if val else 0
```

Advantages over file: truly atomic under concurrency, no file locking, sub-ms latency.

#### Step 6.3: Redis Timeline Index

```python
class RedisTimeline:
    """Append-only event timeline in Redis sorted sets."""

    async def record_event(self, event_seq, memory_id, metadata_summary):
        """Add event to the timeline sorted set (score = event_seq)."""
        await self._redis.zadd(
            f"nova:timeline:{scope}",
            {json.dumps({"id": memory_id, **metadata_summary}): event_seq}
        )

    async def get_recent(self, n=20, scope="global"):
        """Get N most recent events by event_seq (O(log N) + O(K))."""
        return await self._redis.zrevrange(
            f"nova:timeline:{scope}", 0, n - 1, withscores=True
        )

    async def get_since_seq(self, since_seq, scope="global"):
        """Get all events after a given event_seq."""
        return await self._redis.zrangebyscore(
            f"nova:timeline:{scope}", since_seq, "+inf", withscores=True
        )

    async def record_checkpoint(self, session_id, last_event_seq, summary):
        """Store checkpoint in a dedicated sorted set."""
        await self._redis.zadd(
            f"nova:checkpoints:{scope}",
            {json.dumps({"session_id": session_id, "summary": summary}): last_event_seq}
        )

    async def get_last_checkpoint(self, scope="global"):
        """Get the most recent checkpoint (O(1))."""
        results = await self._redis.zrevrange(
            f"nova:checkpoints:{scope}", 0, 0, withscores=True
        )
        return results[0] if results else None
```

#### Step 6.4: Integration

Update `MemoryService`:
```python
async def perform_upsert(self, content, memory_id=None, metadata=None):
    # ... existing logic ...
    # After successful Pinecone + Neo4j persist:
    if self.redis_timeline:
        await self.redis_timeline.record_event(
            event_seq=metadata["event_seq"],
            memory_id=item_id,
            metadata_summary={"type": metadata.get("memory_type"), "project": metadata.get("project")},
        )
```

`get_recent_events` can now use Redis (O(log N)) instead of Pinecone dummy-vector queries:
```python
async def get_recent_events(self, n=20, **filters):
    if self.redis_timeline:
        return await self.redis_timeline.get_recent(n, scope=filters.get("project", "global"))
    else:
        return await self._pinecone_temporal_fallback(n, **filters)  # Phase 3 logic
```

**Tests:**
- Redis INCR produces strictly monotonic sequences under concurrent writes
- Timeline sorted set returns events in correct order
- `get_recent(10)` returns exactly the 10 most recent events
- `get_since_seq(50)` returns only events with seq > 50
- Checkpoint stored and retrieved correctly
- Graceful fallback if Redis unavailable (use Phase 1 file-based counter)

**Acceptance:** Redis is the authoritative timeline. Recency queries are O(log N) instead of scanning Pinecone with dummy vectors.

---

## Phase Dependency Graph

```
Phase 1 (Write-Time Enforcement)
    ↓
Phase 2 (Session Checkpoints)      ← depends on Phase 1 (needs event_seq)
    ↓
Phase 3 (Temporal Retrieval Tools) ← depends on Phase 1 (queries event_seq)
    ↓
Phase 4 (Temporal-First Router)    ← depends on Phase 3 (uses temporal queries)
    ↓
Phase 5 (Graph Time Model)        ← depends on Phase 2 (needs sessions)
    ↓
Phase 6 (Redis Timeline)          ← optional, enhances Phase 1 + 3
```

Phases 5 and 6 are independent of each other and can be done in parallel or either-order.

---

## New MCP Tool Surface (After All Phases)

| Tool | Phase | Type | Description |
|---|---|---|---|
| `query_memory` | existing | semantic | Fused retrieval (now with temporal routing) |
| `upsert_memory` | existing | write | Now auto-injects `event_time` + `event_seq` |
| `bulk_upsert_memory` | existing | write | Now auto-injects chronology for all items |
| `delete_memory` | existing | write | Unchanged |
| `check_health` | existing | health | Add sequence counter + Redis status |
| `create_checkpoint` | Phase 2 | write | Create session checkpoint |
| `get_last_checkpoint` | Phase 2 | read | Fetch most recent checkpoint |
| `get_recent_events` | Phase 3 | read | Fetch N events by `event_seq` order |

---

## Minimum Viable Fix (Phases 1–3 Only)

If you want the fastest path to solving "what did we do last?":

1. **Phase 1** — every memory gets `event_seq` + `event_time` (1-2 hours)
2. **Phase 2** — session checkpoints exist (1-2 hours)
3. **Phase 3** — `get_recent_events` tool works (1-2 hours)

This alone eliminates the "Sonnet pulled a semantically similar but older memory" failure mode. Phases 4-6 are refinements.

---

## Migration Notes

- **Existing memories without `event_seq`**: Will have `event_seq = None` in metadata. Temporal queries should handle this gracefully (treat as `event_seq = 0` or exclude from temporal results).
- **Backfill option**: Write a one-time migration script that assigns `event_seq` to existing memories based on their `timestamp` field (from MEMORY_SCHEMA), maintaining relative ordering.
- **Zero downtime**: All changes are additive. Existing `query_memory` behavior is unchanged unless the router detects temporal intent.

---

## CONTRACT

```
plan: fusion_memory_chronological_upgrade
status: DRAFTED
phases: 6
minimum_viable: phases 1-3
new_mcp_tools: 3 (create_checkpoint, get_last_checkpoint, get_recent_events)
new_services: 2 (SequenceService, RedisTimeline)
new_routing_modes: 2 (TEMPORAL, TEMPORAL_SEMANTIC)
graph_additions: Session nodes, INCLUDES edges, FOLLOWS chain
external_deps_added: Redis (Phase 6 only, optional)
backward_compatible: yes (all changes additive)
```
