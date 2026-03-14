# Unified Memory Router Specification

Phase 1 deliverable — single internal gateway for all memory operations.

Generated: 2026-03-13

---

## 1. Purpose

The Memory Router is the central abstraction through which all memory reads,
writes, promotions, and lifecycle operations flow. It replaces direct calls to
individual memory backends (Fusion Memory MCP, Obsidian Vault MCP, file-based
MEMORY/) with a single API surface that:

1. Normalizes input/output via the CanonicalMemoryObject schema
2. Selects the appropriate backend adapter based on operation intent
3. Validates before persistence (fail-closed for writes)
4. Emits structured trace logs for every operation
5. Provides a migration path from legacy direct calls

---

## 2. Router Responsibilities

| Responsibility | Description |
|---------------|------------|
| **Ingest** | Accept a raw event and normalize it into a CanonicalMemoryObject |
| **Route** | Decide which backend adapter should handle the operation |
| **Recall** | Retrieve relevant memories from one or more backends |
| **Store** | Persist a validated memory object to the selected backend |
| **Promote** | Move a memory from a lower layer to a higher one (e.g., MEMORY/ → vault) |
| **Checkpoint** | Create a session checkpoint in Fusion Memory |
| **Trace** | Emit structured logs for every router operation |
| **Validate** | Enforce schema and safety rules before any write |

---

## 3. Public API

### Core Methods

```python
class MemoryRouter:
    # --- Phase 1: Fully functional ---
    def recall(self, query: str, *, intent: str, scope: str = "all",
               task_class: str = "", keywords: list[str] | None = None,
               max_results: int = 5) -> RecallResult
    def store(self, obj: CanonicalMemoryObject) -> StoreResult
    def ingest_event(self, event: dict) -> CanonicalMemoryObject

    # --- Phase 1: Skeleton (structured not-implemented) ---
    def promote(self, memory_id: str, target_layer: str) -> PromoteResult
    def checkpoint(self, summary: str, open_threads: list[str],
                   next_actions: list[str], project: str = "nova-core") -> dict
    def consolidate(self, window: str = "24h") -> dict
    def summarize_session(self, session_id: str) -> dict
    def track_open_loop(self, description: str, project: str = "nova-core") -> dict
    def extract_patterns(self, window: str = "7d") -> dict
    def generate_diary(self, session_id: str) -> dict
```

### Return Types

```python
@dataclass
class RecallResult:
    results: list[dict]        # Retrieved memory items
    sources_queried: list[str] # Which adapters were queried
    total_found: int
    query: str
    intent: str
    trace_id: str

@dataclass
class StoreResult:
    stored: bool
    store_used: str            # Which adapter stored the object
    path_or_id: str            # File path or memory ID
    validation_errors: list[str]
    rejection_reason: str | None
    trace_id: str

@dataclass
class PromoteResult:
    promoted: bool
    source_store: str
    target_store: str
    reason: str
    trace_id: str
```

---

## 4. Input/Output Contracts

### Recall Contract
- **Input**: query string + intent + optional scope/task_class/keywords
- **Output**: RecallResult with bounded results list
- **Intent values**: "pattern_retrieval", "vault_context", "prior_decision", "session_replay", "general"
- **Scope values**: "all", "memory_files", "vault", "fusion"
- **Behavior**: Router selects adapter(s) based on intent, merges results, caps at max_results
- **Failure mode**: Fail-open — returns empty results on adapter errors

### Store Contract
- **Input**: CanonicalMemoryObject (must pass validation)
- **Output**: StoreResult indicating success/failure
- **Routing logic**: target_store field selects adapter; if null, router infers from event_type
- **Failure mode**: Fail-closed — invalid objects are rejected with reasons
- **Validation**: Schema validation occurs BEFORE adapter dispatch

### Ingest Contract
- **Input**: Raw event dict from any source (watcher, heartbeat, promoter, etc.)
- **Output**: CanonicalMemoryObject with required fields populated
- **Behavior**: Normalizes field names, assigns memory_id, sets defaults
- **Failure mode**: Raises ValueError on missing required source fields

---

## 5. Adapter Boundaries

Each adapter wraps one backend and exposes a uniform interface:

```python
class MemoryAdapter(Protocol):
    name: str
    def recall(self, query: str, **kwargs) -> list[dict]
    def store(self, obj: CanonicalMemoryObject) -> StoreResult
    def is_available(self) -> bool
```

### Phase 1 Adapters

| Adapter | Backend | Wraps | Status |
|---------|---------|-------|--------|
| `MemoryFileAdapter` | MEMORY/*.json | agents/memory_engine.py functions | Fully functional |
| `VaultAdapter` | Obsidian Vault | tools/mcp_vault_server.py functions | Fully functional (recall); store via existing vault_write |
| `FusionMemoryAdapter` | Fusion Memory MCP | MCP tool calls | Skeleton — prompt-delegated, cannot wrap directly |

### Adapter Rules
- Adapters are internal to the router — callers never import them directly
- Each adapter handles its own error recovery (fail-open for reads, fail-closed for writes)
- Adapters must not import each other
- Backend-specific knowledge (file paths, vault folders, MCP schemas) stays inside the adapter

---

## 6. Error Handling Model

| Operation | Failure Mode | Rationale |
|-----------|-------------|-----------|
| recall | Fail-open | Missing context should not block task execution |
| store | Fail-closed | Invalid data must not be persisted |
| ingest_event | Raise on missing required | Caller must provide minimum context |
| promote | Fail-closed | Promotion is a durable action with audit trail |
| checkpoint | Fail-open | Checkpoint failure should not block session end |

All errors are logged via structured tracing before propagation.

---

## 7. Tracing/Logging Model

Every router operation emits a structured log event via `utils.structured_log.slog`:

```python
slog.event("memory.router.<operation>", ctx,
    operation="recall",
    caller="watcher.dispatch_task",
    intent="pattern_retrieval",
    query="implement vault schema...",
    adapter_selected="memory_file",
    results_count=3,
    latency_ms=12,
    validation_outcome="pass",  # or "rejected"
    rejection_reason=None,
)
```

Event naming convention: `memory.router.{recall|store|ingest|promote|checkpoint}`

Trace context (TraceContext) is optional but encouraged. When not provided,
the router creates a minimal context for the operation.

---

## 8. Backward-Compatibility Strategy

### Phase 1 Approach: Wrap, Don't Replace

Legacy functions (`retrieve_related_patterns`, `capture_direct_task_memory`,
`vault_search`, `vault_write`) continue to work unchanged. The router provides
a parallel path. Migration is incremental:

1. **Phase 1**: One recall path migrated (watcher pattern retrieval)
2. **Phase 2+**: Additional paths migrated as router proves stable
3. **Future**: Legacy functions deprecated and eventually removed

### Compatibility Guarantees
- All existing imports from `agents.memory_engine` continue to work
- All existing imports from `tools.mcp_vault_server` continue to work
- All existing imports from `planner.vault_context` and `planner.pattern_retriever` continue to work
- No behavior changes in any un-migrated code path
- Router can be disabled entirely by not importing it — zero impact on legacy paths

---

## 9. Migration Strategy for Legacy Direct Calls

| Priority | Call Site | Migration Path |
|----------|----------|---------------|
| **P0 (Phase 1)** | watcher.py `retrieve_related_patterns` | Migrate to `router.recall(intent="pattern_retrieval")` |
| **P1 (Phase 2)** | watcher.py `capture_direct_task_memory` | Migrate to `router.store()` |
| **P1 (Phase 2)** | planner/vault_context.py `vault_search` | Migrate to `router.recall(intent="vault_context")` |
| **P1 (Phase 2)** | planner/pattern_retriever.py `vault_search` + `vault_read` | Migrate to `router.recall(intent="pattern_retrieval")` |
| **P2 (Phase 3+)** | planner/workflow_promoter.py `vault_write` | Migrate to `router.promote()` |
| **P2 (Phase 3+)** | planner/pattern_promoter.py `vault_write` | Migrate to `router.promote()` |
| **P3 (Phase 4+)** | heartbeat.py prompt-delegated calls | Cannot be directly migrated (prompt-delegated) |

---

## 10. Phase 1 Method Status

| Method | Phase 1 Status | Notes |
|--------|---------------|-------|
| `recall()` | **Fully functional** | Routes to MemoryFileAdapter and/or VaultAdapter based on intent |
| `store()` | **Fully functional** | Routes to MemoryFileAdapter; validates via existing pipelines |
| `ingest_event()` | **Fully functional** | Normalizes raw events into CanonicalMemoryObject |
| `promote()` | Skeleton | Returns structured "not_implemented" result |
| `checkpoint()` | Skeleton | Returns structured "not_implemented" result |
| `consolidate()` | Skeleton | Returns structured "not_implemented" result |
| `summarize_session()` | Skeleton | Returns structured "not_implemented" result |
| `track_open_loop()` | Skeleton | Returns structured "not_implemented" result |
| `extract_patterns()` | Skeleton | Returns structured "not_implemented" result |
| `generate_diary()` | Skeleton | Returns structured "not_implemented" result |
