# Direct Memory Call Migration Map

Phase 1 deliverable — inventory of all direct memory backend calls with migration status.

Generated: 2026-03-13

---

## Migration Policy

All new memory code MUST use `agents.memory_router.router` instead of
importing backend modules directly. Existing direct calls are marked legacy
and will be migrated incrementally.

```python
# LEGACY — do not add new code using this pattern
from agents.memory_engine import retrieve_related_patterns
patterns = retrieve_related_patterns(task_class, keywords)

# PREFERRED — use the router
from agents.memory_router import router
result = router.recall(query, intent="pattern_retrieval", task_class=task_class, keywords=keywords)
```

---

## Migration Status

### File-Based Memory (agents/memory_engine.py)

| File | Function/Call | Line(s) | Operation | Priority | Status | Blocker |
|------|-------------|---------|-----------|----------|--------|---------|
| watcher.py | `retrieve_related_patterns()` | L19, L863 | read | P0 | **MIGRATED (Phase 1)** | — |
| watcher.py | `format_retrieval_for_planner()` | L20, L865 | read (format) | P0 | **MIGRATED (Phase 1)** | — |
| watcher.py | `capture_direct_task_memory()` | L21, L1120-1125 | write | P1 | Legacy | Requires store() integration testing |

### Obsidian Vault (tools/mcp_vault_server.py)

| File | Function/Call | Line(s) | Operation | Priority | Status | Blocker |
|------|-------------|---------|-----------|----------|--------|---------|
| planner/vault_context.py | `vault_search()` | L146, L165 | read | P1 | Legacy | Needs VaultAdapter.recall() integration |
| planner/pattern_retriever.py | `vault_search()` | L135, L144 | read | P1 | Legacy | Needs VaultAdapter.recall() integration |
| planner/pattern_retriever.py | `vault_read()` | L181, L186 | read | P1 | Legacy | Needs VaultAdapter full read support |
| planner/workflow_promoter.py | `vault_validate()` | L296, L308-311 | validate | P2 | Legacy | Requires promote() implementation |
| planner/workflow_promoter.py | `vault_write()` | L296, L335-339 | write | P2 | Legacy | Requires promote() implementation |
| planner/pattern_promoter.py | `vault_validate()` | L414, L421-425 | validate | P2 | Legacy | Requires promote() implementation |
| planner/pattern_promoter.py | `vault_write()` | L414, L443-447 | write | P2 | Legacy | Requires promote() implementation |

### Fusion Memory MCP (prompt-delegated)

| File | Context | Line(s) | Operation | Priority | Status | Blocker |
|------|---------|---------|-----------|----------|--------|---------|
| heartbeat.py | Research cycle | L979-1028 | write | P3 | Legacy | Prompt-delegated; cannot migrate without SDK access |
| heartbeat.py | Planning cycle | L1305-1355 | write | P3 | Legacy | Same |
| watcher.py | Dispatch prompt | L218-223 | write | P3 | Legacy | Same |
| scripts/daily_summary.py | Daily report | L46-74 | write | P3 | Legacy | Same |

---

## Summary

| Priority | Count | Status |
|----------|-------|--------|
| P0 | 2 | **Both migrated in Phase 1** |
| P1 | 4 | Legacy — migrate in Phase 2 |
| P2 | 4 | Legacy — migrate in Phase 3+ (requires promote()) |
| P3 | 4 | Legacy — cannot migrate (prompt-delegated) |
| **Total** | **14** | **2 migrated, 12 remaining** |

---

## Deprecation Notes

### For reviewers writing new code:

1. **Do not** import `retrieve_related_patterns` or `format_retrieval_for_planner`
   from `agents.memory_engine` in new code — use `router.recall()` instead.
2. **Do not** import `vault_search`, `vault_read`, or `vault_write` from
   `tools.mcp_vault_server` in new code — use the router.
3. **Do not** add new `upsert_memory` instructions in prompts without documenting
   the bypass in this migration map.
4. Existing imports remain valid for backward compatibility during migration.
