# Working Memory Adapter Validation Report

Generated: 2026-03-13

---

## Problem

Phase 3 heartbeat triggers fire correctly but produce working-layer memory
objects that cannot persist. The Phase 2 `LAYER_ALLOWED_STORES` policy only
allows `fusion_memory` and `discard` for working-layer, and `FusionMemoryAdapter`
is a skeleton (prompt-delegated, not callable from Python). Result: heartbeat
triggers fire but store returns `stored=False`.

## Solution

Added `WorkingMemoryAdapter` — a file-based adapter that persists working-layer
events to `STATE/working_memory/` as compact JSON files with 7-day retention
and atomic writes. Registered as `state_working` in the router and layer policy.

### Design Decisions

- **STATE/ not MEMORY/**: Working-layer is transient scratch, not durable knowledge.
  `STATE/working_memory/` is the documented home for this per the store-to-layer mapping.
- **7-day retention with best-effort cleanup**: Matches the session file retention
  policy. Cleanup runs after each store (non-blocking).
- **Compact records**: Only essential fields stored (memory_id, timestamp, source,
  event_type, title, summary, current_layer, confidence, tags). No full CMO dump.
- **Atomic writes**: Uses tmp+rename pattern consistent with all other NovaCore writers.
- **`state_working` not added to other layers**: Only working-layer can write to
  this store. Episodic/semantic/procedural targeting `state_working` is rejected
  by the Phase 2 `check_store_layer_compatibility()` guard.

---

## Files Changed

| File | Change | Lines |
|------|--------|-------|
| `agents/memory_router.py` | Added `WorkingMemoryAdapter` class | +85 lines |
| `agents/memory_router.py` | Added `"state_working"` to `VALID_TARGET_STORES` | 1 line |
| `agents/memory_router.py` | Added `"state_working"` to `LAYER_ALLOWED_STORES["working"]` | 1 line |
| `agents/memory_router.py` | Registered `WorkingMemoryAdapter` in router default constructor | 1 line |
| `agents/memory_router.py` | Updated `_infer_target_store` to route working-layer → `state_working` | +3 lines |
| `agents/memory_router.py` | Added `scope="working"` to `_select_recall_adapters` | +2 lines |
| `tests/test_memory_router.py` | Added `TestWorkingMemoryAdapter` (5 tests) | +50 lines |
| `tests/test_memory_router.py` | Added `TestWorkingLayerRouterIntegration` (7 tests) | +85 lines |
| `tests/test_memory_router.py` | Added `test_working_allows_state_working` | +3 lines |
| `tests/test_memory_router.py` | Updated `test_session_replay_uses_fusion_and_working` | 3 lines |
| `tests/test_memory_triggers.py` | Updated `_make_engine` to include `WorkingMemoryAdapter` | 3 lines |
| `tests/test_memory_triggers.py` | Updated heartbeat test to verify `stored=True` | 6 lines |
| `MEMORY/store_to_layer_mapping.md` | Updated enforcement status table | 2 lines |

---

## Tests Run

```bash
# Router tests (Phase 1+2 + working adapter)
python3 -m pytest tests/test_memory_router.py -v
# 120 passed (was 107, +13 new)

# Trigger tests (Phase 3)
python3 -m pytest tests/test_memory_triggers.py -v
# 35 passed

# Full regression
python3 -m pytest tests/
# 3430 passed in 79.34s, 0 failures
```

### New Tests

| Test | Validates |
|------|-----------|
| `test_store_creates_json_file` | Adapter writes valid JSON with expected fields |
| `test_recall_empty_dir` | Recall on empty dir returns [] |
| `test_store_and_recall_roundtrip` | Write then read returns correct data |
| `test_is_available` | Adapter reports availability from STATE/ |
| `test_file_uses_atomic_write` | No .tmp files left behind |
| `test_working_allows_state_working` | Layer policy accepts working → state_working |
| `test_working_heartbeat_accepted_by_state_working` | End-to-end: heartbeat → ingest → store → state_working |
| `test_working_event_rejected_by_memory_file` | Phase 2 guard blocks working → memory_file |
| `test_working_event_rejected_by_obsidian_vault` | Phase 2 guard blocks working → obsidian_vault |
| `test_episodic_event_rejected_by_state_working` | Phase 2 guard blocks episodic → state_working |
| `test_working_infer_routes_to_state_working` | Target inference selects state_working for working-layer |
| `test_working_store_traces_layer_and_adapter` | Trace includes current_layer=working, adapter=state_working |
| `test_phase2_guardrails_unchanged_for_other_layers` | Episodic→memory_file OK, semantic→memory_file blocked |

---

## Phase 2 Guardrails Preserved

| Guard | Status |
|-------|--------|
| working → memory_file | **BLOCKED** (unchanged) |
| working → obsidian_vault | **BLOCKED** (unchanged) |
| working → state_working | **ALLOWED** (new) |
| working → fusion_memory | **ALLOWED** (unchanged, skeleton adapter) |
| working → discard | **ALLOWED** (unchanged) |
| episodic → state_working | **BLOCKED** (new guard) |
| semantic → state_working | **BLOCKED** (new guard) |
| procedural → state_working | **BLOCKED** (new guard) |
| All other layer/store combinations | **UNCHANGED** |

---

## Remaining Gaps

| Gap | Severity | Notes |
|-----|----------|-------|
| `FusionMemoryAdapter` still a skeleton | Low | `state_working` provides a real working-layer backend |
| No GC daemon for STATE/working_memory/ | Low | Best-effort cleanup on each write; 7-day files are small |
| `state_working` recall is chronological only, no keyword search | Low | Working-layer recall is for session replay, not semantic search |
