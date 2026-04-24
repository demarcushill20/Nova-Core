---
id: ADR-0759
title: Associative Linking lives in Fusion Memory MCP, not nova-core/agents
status: Accepted
date: 2026-04-13
parent_plan: 00-inbox/associative-linking-full-implementation-plan-v2.md
related:
  - /home/nova/Nova_AI_Fusion_Memory_MCP/app/services/memory_service.py
  - /home/nova/Nova_AI_Fusion_Memory_MCP/app/services/graph_client.py
  - /home/nova/nova-core/agents/memory_router.py
tags: [associative-linking, fusion-memory, architecture, plan-0759]
---

# ADR-0759 — Associative Linking integration location

## 1. Status

**Accepted** — 2026-04-13.

## 2. Context

PLAN-0759 (the v2 Associative Linking implementation plan, vault path
`00-inbox/associative-linking-full-implementation-plan-v2.md`) introduces a set
of background "linker" components that, on every memory write, will compute and
persist additional graph edges between the new memory and prior memories
(similarity, entity-overlap, temporal adjacency, co-occurrence, task-heuristic,
and provenance edges). It also adds an associative-recall path on top of those
edges.

The v1 of the same plan proposed wiring these linkers into
`nova-core/agents/memory_router.py` (the Nova-Core side adapter for Fusion
Memory). During plan validation that location was rejected as not mechanically
implementable. This ADR records the decision on **where** the linker code
actually lives, **how** it is invoked, and the two graph-schema corrections
that fall out of the validation work — so that Phases 1+ of PLAN-0759 build on
a verified foundation instead of the v1 plan's illustrative-only Cypher.

## 3. Decision

All linker components introduced by PLAN-0759 —

- `edge_service`
- `similarity_linker`
- `entity_linker`
- `temporal_linker`
- `cooccurrence`
- `task_heuristic`
- `associative_recall`
- `provenance`

— live **inside** the Fusion Memory MCP repository under

```
/home/nova/Nova_AI_Fusion_Memory_MCP/app/services/associations/
```

(The `associations/` package is **not** created in this sprint; that is a
Phase 1 deliverable. This ADR only records the location decision.)

They are invoked from inside `MemoryService.perform_upsert()` in
`/home/nova/Nova_AI_Fusion_Memory_MCP/app/services/memory_service.py`, where
the embedding vector and the freshly-committed graph node are both already in
scope.

`nova-core/agents/memory_router.py` is **not** the home of the linker logic.
It only gains thin opt-in pass-through kwargs on its existing surface (e.g.
`expand_graph: bool = False` on `recall(...)`, defaulting to off) so that
nova-core callers can request associative-recall behavior without nova-core
needing to know how the graph traversal works.

## 4. Exact hook point in `perform_upsert`

Verified by reading
`/home/nova/Nova_AI_Fusion_Memory_MCP/app/services/memory_service.py` on
2026-04-13:

| Anchor | Line | Notes |
| --- | --- | --- |
| `async def perform_upsert(...)` | 806 | Matches plan validator's claim. |
| Embedding generation (`get_embedding` via `asyncio.to_thread`) | 838 | Matches. |
| Pinecone upsert call site | 756–758 | Lives inside the helper `_persist_memory_item()` (line ~728), which `perform_upsert()` calls at line 890. The plan validator's "line 758" cite corresponds to the `metadata` argument line of `pinecone_client.upsert_vector(item_id, embedding, pinecone_meta)`. |
| Neo4j upsert call site (`graph_client.upsert_graph_data`) | 767–769 | Also inside `_persist_memory_item()`. The "line 768" cite lands on the `(item_id, content, metadata)` argument line. |
| Both-success log line (`Successfully upserted ID ... to both Pinecone and Graph.`) | 778 | This is where both stores are confirmed durable. |
| Existing session-link block (`Phase 5: Auto-link event to session`) | 897 | Best-effort, post-persist, fire-after style. |

**Drift note.** The plan validator described lines 758/768 as if they were
inside `perform_upsert` directly. They are actually inside the private helper
`_persist_memory_item()`, which `perform_upsert` calls. The line numbers
themselves are correct; only the framing was slightly off. This does not
affect the hook decision because `perform_upsert` is still the public entry
point and is still the right place to dispatch associative linking — the hook
fires *after* `_persist_memory_item()` returns success.

**Hook window.** The correct hook point for async associative-link dispatch is
**after the `success = await self._persist_memory_item(...)` call returns at
line 890**, and **before the existing Phase 5 session-linking block at line
897** — i.e. inside the half-open interval (778, 897] from the validator's
framing, with line 890 being the concrete insertion line.

**Dispatch contract.** The link dispatch **must** be fire-and-forget via
`asyncio.create_task(...)`. It **must not** be awaited on the hot path. The
linker task receives the already-computed `embedding`, the persisted
`item_id`, and the (post-chronology-injection) `metadata` dict. Failures in
the linker task must be logged but must never affect the return value of
`perform_upsert()`.

## 5. Rejected alternative — putting linkers in `nova-core/agents/`

The v1 of PLAN-0759 proposed putting the linker components in
`nova-core/agents/` and hooking them inside
`FusionMemoryAdapter.store(...)` in `agents/memory_router.py`. Verified by
reading `/home/nova/nova-core/agents/memory_router.py` on 2026-04-13:

- `FusionMemoryAdapter.store(self, obj: CanonicalMemoryObject)` is defined at
  **line 880**.
- It is a **synchronous** method (`def`, not `async def`).
- It builds a `metadata` dict and calls
  `_run_async(self._service.perform_upsert(content=..., memory_id=...,
  metadata=...))` at **line 908**, i.e. it crosses the sync→async boundary by
  blocking on a private executor.
- It does **not** receive or compute an embedding vector. Embedding generation
  is hidden inside `MemoryService.perform_upsert()` at line 838 of
  `memory_service.py` and never escapes that function.

Three problems follow:

1. **No embedding access.** Similarity and MMR-style linkers need the dense
   vector. Re-embedding from `agents/memory_router.py` would either duplicate
   the OpenAI call (cost + drift risk) or break the contract that there is
   exactly one canonical embedding per stored memory.
2. **Wrong concurrency model.** `FusionMemoryAdapter.store()` is sync and
   blocks on `_run_async`. Adding `asyncio.create_task(...)` from inside a
   synchronous wrapper would either run on the wrong loop or be silently
   garbage-collected. Fire-and-forget linker dispatch from this surface is not
   safe.
3. **Cross-repo coupling.** Linker logic in `nova-core/agents/` would need
   access to `graph_client`, `pinecone_client`, and the embedding service,
   which are private modules of the Fusion Memory MCP. Exposing them across
   the repo boundary inverts the existing dependency direction.

For these reasons the v1 hook location is rejected as not mechanically
implementable, and the linkers move to the Fusion Memory MCP side (Section 3).

## 6. Edge-type naming — `MEMORY_FOLLOWS`, not `FOLLOWS`

PLAN-0759 originally proposed `(:Memory)-[:FOLLOWS]->(:Memory)` for temporal
adjacency edges between memories. That is rejected. Verified by reading
`/home/nova/Nova_AI_Fusion_Memory_MCP/app/services/graph_client.py` on
2026-04-13: the relationship type `FOLLOWS` is **already in use** for session
chaining. From `link_session_follows` (defined at line 423), the Cypher body
contains:

```cypher
MATCH (curr:Session {session_id: $current_id})
MATCH (prev:Session {session_id: $previous_id})
MERGE (curr)-[:FOLLOWS]->(prev)
```

— the `MERGE (curr)-[:FOLLOWS]->(prev)` line is at **line 442**.

Reusing `FOLLOWS` for memory-to-memory temporal adjacency would:

- Poison `MATCH ()-[r:FOLLOWS]->()` edge-stat queries (would now mix Session
  and Memory edges in the same count).
- Make rollback-by-type unsafe — a `MATCH ()-[r:FOLLOWS]->() DELETE r` to
  unwind a bad linker run would delete the existing Session chain.
- Make traversal queries ambiguous about endpoint types.

**Decision.** Memory-to-memory temporal edges introduced by PLAN-0759 use the
relationship type **`MEMORY_FOLLOWS`**, distinct from the existing `FOLLOWS`.
Any per-edge metadata (delta seconds, source linker, confidence) attaches to
the `MEMORY_FOLLOWS` relationship, not to `FOLLOWS`.

## 7. Node label correction — `:base {entity_id}`, not `:Memory {memory_id}`

PLAN-0759 Step 1.2 contains illustrative Cypher that writes against
`(:Memory {memory_id: $id})`. The plan itself is explicit that this is a
placeholder ("substitute actual label/property names verified in Step 0.2").
This ADR records the verified values so Phase 1 does not regress to the
placeholder names.

Verified by reading
`/home/nova/Nova_AI_Fusion_Memory_MCP/app/services/graph_client.py` on
2026-04-13:

- The node label constant is defined at line 20:
  `NEO4J_NODE_LABEL = "base"  # Using lowercase as seen in Nova_AI.py graph_task`.
- The unique constraint enforced at startup, in `_ensure_constraints` (line
  91), is built at line 97 as:
  `f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{NEO4J_NODE_LABEL}) REQUIRE n.entity_id IS UNIQUE"`,
  which expands to:
  `CREATE CONSTRAINT IF NOT EXISTS FOR (n:base) REQUIRE n.entity_id IS UNIQUE`.
- The actual write path in `upsert_graph_data` (defined at line 139) issues:
  ```cypher
  MERGE (n:base {entity_id: $node_id})
  SET n += $props
  RETURN n.entity_id AS id
  ```
  — the `MERGE (n:base {entity_id: $node_id})` line is at **line 169** (with
  the surrounding f-string starting at line 168).

So the canonical Fusion Memory node identity is **`(:base {entity_id})`**, not
`(:Memory {memory_id})`. Every Cypher example in PLAN-0759 Phase 1 must be
rewritten against `:base` and `entity_id` before it ships. This ADR is the
authoritative reference for that rename.

(Plan validator originally cited "lines 76–91 and 168" for the constraint
text. The actual constraint is constructed at line 97 inside
`_ensure_constraints` which begins at line 91; line 168 is the f-string for
the `MERGE` Cypher in `upsert_graph_data`. The constraint text and label/key
identification are unchanged; only the line numbers are slightly more precise
here.)

## 8. Consequences

- **Linker code is Fusion-MCP-native.** The new
  `app/services/associations/` package (Phase 1) is owned by the Fusion
  Memory MCP repository, has direct access to the embedding vector,
  `graph_client`, `pinecone_client`, and the Pydantic `Settings` object, and
  is dispatched fire-and-forget from `perform_upsert()` so that write latency
  for callers does not regress.
- **Cross-repo surface stays thin.** `nova-core/agents/memory_router.py` only
  gains opt-in pass-through kwargs (e.g. `expand_graph=False` on `recall`).
  No linker logic, no embedding handling, no Cypher lives in nova-core.
- **Phase 0.2 schema audit is still required.** This ADR locks the *known*
  invariants (`:base`, `entity_id`, `MEMORY_FOLLOWS`) but PLAN-0759 Phase 0.2
  must still walk the full Neo4j schema (every existing label, every existing
  relationship type, every existing constraint/index) and confirm no other
  collisions exist before Phase 1 writes its first edge.
- **Phase 1 Cypher rewrite.** Every Cypher snippet in PLAN-0759 Phase 1 —
  similarity edges, entity edges, temporal edges, recall traversals — must be
  rewritten against `(:base {entity_id})` and `MEMORY_FOLLOWS` before being
  merged. The illustrative `(:Memory {memory_id})` snippets in the v2 plan
  are not directly executable.
- **Feature-flag scaffolding only in Sprint 1.** Sprint 1 (this work) only
  adds 8 default-`False` `ASSOC_*` flags to `app/config.py` and a unit test
  asserting they all default `False`. Zero behavior change. No `associations/`
  package yet. No hook wiring in `perform_upsert`. Those land in Phase 1
  under their respective flags.
