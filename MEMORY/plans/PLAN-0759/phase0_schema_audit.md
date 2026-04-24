# Phase 0 Schema Audit Report (PLAN-0759)

**Date**: 2026-04-13T16:24:51.339260+00:00
**Script**: scripts/audit_neo4j_schema.py
**Neo4j instance**: bolt://localhost:7687 (neo4j:5.19, container nova_neo4j_db)
**Audit run by**: Sprint 2 implementer (PLAN-0759)

## Verified invariants (feed into Phase 1 Cypher)

- Primary memory node label: `:base`  ✓
- Primary key property: `entity_id` ✓
- Existing unique constraint on `(:base, entity_id)`: yes

  ```text
  {'id': 4, 'name': 'constraint_749c89fe', 'type': 'UNIQUENESS', 'entityType': 'NODE', 'labelsOrTypes': ['base'], 'properties': ['entity_id'], 'ownedIndex': 'constraint_749c89fe', 'propertyType': None}
  ```
- Existing `FOLLOWS` edges: 1 on ['Session'] -> ['Session']  (confirms `MEMORY_FOLLOWS` rename was necessary)

## Full enumeration

### Node labels (count)

| label   | count |
| ------- | ----: |
| base    | 824   |
| Session | 339   |

### Relationship types (count)

| type     | count |
| -------- | ----: |
| INCLUDES | 517   |
| FOLLOWS  | 1     |

### Constraints

```text
{'id': 7, 'name': 'constraint_6569abef', 'type': 'UNIQUENESS', 'entityType': 'NODE', 'labelsOrTypes': ['Session'], 'properties': ['session_id'], 'ownedIndex': 'constraint_6569abef', 'propertyType': None}
{'id': 4, 'name': 'constraint_749c89fe', 'type': 'UNIQUENESS', 'entityType': 'NODE', 'labelsOrTypes': ['base'], 'properties': ['entity_id'], 'ownedIndex': 'constraint_749c89fe', 'propertyType': None}
```

### Indexes

```text
{'id': 6, 'name': 'constraint_6569abef', 'state': 'ONLINE', 'populationPercent': 100.0, 'type': 'RANGE', 'entityType': 'NODE', 'labelsOrTypes': ['Session'], 'properties': ['session_id'], 'indexProvider': 'range-1.0', 'owningConstraint': 'constraint_6569abef', 'lastRead': neo4j.time.DateTime(2026, 4, 13, 13, 30, 27, 337000000, tzinfo=<UTC>), 'readCount': 1350}
{'id': 3, 'name': 'constraint_749c89fe', 'state': 'ONLINE', 'populationPercent': 100.0, 'type': 'RANGE', 'entityType': 'NODE', 'labelsOrTypes': ['base'], 'properties': ['entity_id'], 'indexProvider': 'range-1.0', 'owningConstraint': 'constraint_749c89fe', 'lastRead': neo4j.time.DateTime(2026, 4, 13, 13, 30, 27, 337000000, tzinfo=<UTC>), 'readCount': 3127}
{'id': 8, 'name': 'index_18816a9c', 'state': 'ONLINE', 'populationPercent': 100.0, 'type': 'RANGE', 'entityType': 'NODE', 'labelsOrTypes': ['Session'], 'properties': ['last_event_seq'], 'indexProvider': 'range-1.0', 'owningConstraint': None, 'lastRead': None, 'readCount': 0}
{'id': 5, 'name': 'index_2ad2a83f', 'state': 'ONLINE', 'populationPercent': 100.0, 'type': 'RANGE', 'entityType': 'NODE', 'labelsOrTypes': ['base'], 'properties': ['event_seq'], 'indexProvider': 'range-1.0', 'owningConstraint': None, 'lastRead': None, 'readCount': 0}
{'id': 1, 'name': 'index_343aff4e', 'state': 'ONLINE', 'populationPercent': 100.0, 'type': 'LOOKUP', 'entityType': 'NODE', 'labelsOrTypes': None, 'properties': None, 'indexProvider': 'token-lookup-1.0', 'owningConstraint': None, 'lastRead': neo4j.time.DateTime(2026, 4, 13, 16, 23, 31, 252000000, tzinfo=<UTC>), 'readCount': 1465}
{'id': 2, 'name': 'index_f7700477', 'state': 'ONLINE', 'populationPercent': 100.0, 'type': 'LOOKUP', 'entityType': 'RELATIONSHIP', 'labelsOrTypes': None, 'properties': None, 'indexProvider': 'token-lookup-1.0', 'owningConstraint': None, 'lastRead': neo4j.time.DateTime(2026, 4, 13, 16, 23, 39, 478000000, tzinfo=<UTC>), 'readCount': 19}
```

### `:base` property sample (10 nodes)

Union of property keys across 10 sampled nodes: `['category', 'ended_at', 'entity_id', 'event_seq', 'event_time', 'last_event_seq', 'memory_type', 'next_actions', 'open_threads', 'project', 'session_id', 'session_summary', 'started_at', 'tags', 'text', 'thread_id']`

- sample 1: `['entity_id', 'event_seq', 'event_time', 'memory_type', 'project', 'text']`
- sample 2: `['entity_id', 'event_seq', 'event_time', 'memory_type', 'project', 'text']`
- sample 3: `['category', 'entity_id', 'event_seq', 'event_time', 'memory_type', 'project', 'session_id', 'tags', 'text']`
- sample 4: `['category', 'entity_id', 'event_seq', 'event_time', 'memory_type', 'project', 'session_id', 'tags', 'text']`
- sample 5: `['category', 'entity_id', 'event_seq', 'event_time', 'memory_type', 'project', 'session_id', 'tags', 'text']`
- sample 6: `['ended_at', 'entity_id', 'event_seq', 'event_time', 'last_event_seq', 'memory_type', 'next_actions', 'open_threads', 'project', 'session_id', 'session_summary', 'started_at', 'text', 'thread_id']`
- sample 7: `['category', 'entity_id', 'event_seq', 'event_time', 'memory_type', 'project', 'session_id', 'tags', 'text']`
- sample 8: `['category', 'entity_id', 'event_seq', 'event_time', 'memory_type', 'project', 'session_id', 'tags', 'text']`
- sample 9: `['category', 'entity_id', 'event_seq', 'event_time', 'memory_type', 'project', 'session_id', 'tags', 'text']`
- sample 10: `['category', 'entity_id', 'event_seq', 'event_time', 'memory_type', 'project', 'session_id', 'tags', 'text']`

## Associative-linking edge type baseline

| edge type      | count | pre-existing? |
| -------------- | ----: | ------------- |
| SIMILAR_TO     | 0     | no (expected) |
| MEMORY_FOLLOWS | 0     | no (expected) |
| MENTIONS       | 0     | no (expected) |
| PROMOTED_FROM  | 0     | no (expected) |
| SUPERSEDES     | 0     | no (expected) |
| COMPACTED_FROM | 0     | no (expected) |
| CAUSED_BY      | 0     | no (expected) |
| RELATED_TASK   | 0     | no (expected) |
| CO_OCCURS      | 0     | no (expected) |

## Conclusions

- ADR-0759 node-label decision confirmed on the wire: the `:base` label exists, `entity_id` is present on sampled nodes, and a unique constraint on `(:base, entity_id)` is in force.
- Existing `FOLLOWS` collision confirmed: 1 edges exist in the graph (['Session'] -> ['Session']). Renaming the PLAN-0759 temporal edge to `MEMORY_FOLLOWS` (ADR-0759 §6) is necessary and correct.
- No unexpected PLAN-0759 edge types pre-exist. Phase 1 Cypher is clear to proceed with `(:base {entity_id})` as the node identity and `MEMORY_FOLLOWS` as the temporal-adjacency edge.
- **Pre-existing relationship type outside PLAN-0759 scope: `INCLUDES`** — 517 edges on `Session → base`, created by the existing Fusion Memory session-event linking in `graph_client.py` (`create_session_includes_event()`). This is not a collision with any of the 9 PLAN-0759 candidate edge types (all of which are `base ↔ base`), but Phase 1 graph-traversal recall code that does an untyped `MATCH (m)-[r]-()` walk on a `:base` node may encounter `INCLUDES` edges to `Session` nodes. Phase 1 traversal must filter by the `VALID_EDGE_TYPES` frozenset from the v2 plan, OR filter by node label (`:base` → `:base` only), OR both. The associative recall engine must return memory nodes only — `:Session` nodes should be kept as path metadata if they appear, or excluded entirely.
