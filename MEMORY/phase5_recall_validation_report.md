# Phase 5 Intent-Aware Recall — Validation Report

Generated: 2026-03-13

---

## Summary

Phase 5 adds intent-aware recall to the Unified Memory Router. Every recall
query is classified into one of 8 intent types, routed to intent-appropriate
adapters, ranked by multi-factor scoring, and annotated with retrieval
explanation metadata. Legacy intents are auto-mapped for backward compatibility.
55 new tests pass. Full regression: 3488 passed, 0 failures.

---

## Files Changed

| File | Change | Lines |
|------|--------|-------|
| `agents/recall_intent.py` | **NEW** — intent classifier, routing policy, ranking, dedup, explanation metadata | 457 lines |
| `agents/memory_router.py` | **MODIFIED** — recall() rewritten for Phase 5 intent-aware pipeline | +60 lines net |
| `agents/memory_router.py` | Docstring updated to "Phase 1+2+4+5" | header |
| `tests/test_recall_intent.py` | **NEW** — 55 tests across 12 test classes | 476 lines |
| `tests/test_memory_router.py` | **MODIFIED** — 1 trace test updated for legacy intent mapping | 1 line |
| `MEMORY/intent_aware_recall_spec.md` | **NEW** — Step 5.1 intent model spec | doc |
| `MEMORY/recall_intent_routing_matrix.md` | **NEW** — Step 5.3 routing matrix | doc |
| `MEMORY/phase5_recall_validation_report.md` | **NEW** — this file (Step 5.8) | doc |

---

## Intent Model Implemented

8 intent types, all reachable via keyword classification:

| Intent | Classification Signals | Routing |
|--------|----------------------|---------|
| temporal_recall | when_did, recently, timeline, history, yesterday, last_period | state_working + memory_file |
| decision_recall | decided, decision, why_did_we, tradeoff, ADR, rationale | obsidian_vault → memory_file fallback |
| procedural_recall | how_to, pattern, playbook, step_by_step, workflow, fix | memory_file + obsidian_vault (merge) |
| project_state_recall | status, current_state, progress, active_plan | state_working + memory_file → vault fallback |
| user_preference_recall | prefer, user_wants, config_style | obsidian_vault → memory_file fallback |
| factual_recall | what_is, architecture, schema | memory_file + obsidian_vault (merge) |
| open_loop_recall | unfinished, open_thread, todo, pending, blocked | state_working + memory_file → vault fallback |
| relationship_entity_recall | related_to, connected, dependency, linked_to | memory_file + obsidian_vault (merge) |

---

## Routing Matrix Summary

| Blend Mode | Intents Using It |
|-----------|-----------------|
| merge | procedural, factual, relationship_entity |
| fallback | decision, project_state, user_preference, open_loop |
| none | temporal |

---

## Blended Retrieval Implemented

Three blend modes are implemented and tested:

1. **merge** — Query all primary adapters, combine results, deduplicate, rank.
   Used for procedural_recall, factual_recall, relationship_entity_recall.

2. **fallback** — Query primary adapters first. If empty, query secondary.
   Used for decision_recall, project_state_recall, user_preference_recall, open_loop_recall.

3. **none** — Query primary adapters only. Used for temporal_recall.

Deduplication uses memory_id → path → title:source composite key. Higher-ranked
duplicate is kept.

---

## Ranking Factors Used

| Factor | Weight | Source | Degradation |
|--------|--------|--------|-------------|
| recency | 0.25 | timestamp field, decay curve | Missing timestamp → 0.3 |
| confidence | 0.20 | confidence field | Missing → 0.5 (medium) |
| source_authority | 0.25 | adapter name | Unknown adapter → 0.5 |
| relevance_score | 0.20 | adapter-native score, normalized | No score → 0.1 |
| promotion_level | 0.10 | current_layer field | Unknown layer → 0.5 |

Emphasis boost: factors in the intent's `ranking_emphasis` list get 1.5× weight.

---

## Migrated Recall Path

### watcher.py pattern retrieval (production)

**Path**: `watcher.py:865` → `memory_router.recall(intent="pattern_retrieval")`

**Before Phase 5**: Legacy intent "pattern_retrieval" selected only
MemoryFileAdapter. No ranking beyond adapter-native `_relevance_score`.

**After Phase 5**: Legacy intent auto-mapped to "procedural_recall".
Routes to memory_file + obsidian_vault (merge blend). Results ranked by
multi-factor scoring. Each result includes `_recall_explanation` metadata.

**Backward compatibility**: The call site in watcher.py requires zero changes.
The legacy intent string is accepted and mapped transparently. Results are
a superset of the previous behavior (same memory_file results, plus vault
results if available, all ranked).

**Verification**: `TestRouterIntentAwareRecall.test_legacy_intent_works` confirms
the mapped intent and backward-compatible result format.

---

## Tests Run

```bash
# Phase 5 recall intent tests
python3 -m pytest tests/test_recall_intent.py -v
# 55 passed in 0.59s

# Router tests (Phase 1+2+5 backward compat)
python3 -m pytest tests/test_memory_router.py -v
# 120 passed in 0.97s

# Full regression (excluding pre-existing heartbeat timeout)
python3 -m pytest tests/ --ignore=tests/test_heartbeat.py
# 3488 passed in 23.59s
```

### New Test Classes (55 tests)

| Class | Tests | Validates |
|-------|-------|-----------|
| TestIntentClassification | 10 | Keyword-based classification for all 8 intents |
| TestCallerOverride | 4 | Explicit Phase 5 intent, legacy mapping, invalid fallthrough |
| TestFallbackBehavior | 4 | Empty query, no-match, whitespace, ambiguous tie |
| TestRoutingPlan | 9 | Adapter selection per intent, scope override, every intent routed |
| TestMultiFactorRanking | 7 | Confidence, recency, source authority, emphasis boost, bounds |
| TestRankResults | 3 | Sort order, explanation metadata, fallback notes |
| TestDeduplication | 5 | Same memory_id, same path, different results, empty keys |
| TestRouterIntentAwareRecall | 10 | Legacy compat, Phase 5 intents, scope, ranking, traces, blending |
| TestRecallExplanation | 1 | to_dict() completeness |
| TestIntentClassificationStructure | 2 | to_dict(), intent always valid |

---

## Phase 5 Step Completion

| Step | Deliverable | Status |
|------|-------------|--------|
| 5.1 | Intent model spec | **Done** — `MEMORY/intent_aware_recall_spec.md` |
| 5.2 | Intent classification | **Done** — `agents/recall_intent.py` classify_intent() |
| 5.3 | Routing rules by intent | **Done** — `MEMORY/recall_intent_routing_matrix.md` + get_routing_plan() |
| 5.4 | Blended retrieval | **Done** — merge, fallback, none modes + dedupe_results() |
| 5.5 | Multi-factor ranking | **Done** — compute_ranking_score() with 5 factors |
| 5.6 | Retrieval explanation metadata | **Done** — RecallExplanation on every result |
| 5.7 | Real recall path migrated | **Done** — watcher.py pattern_retrieval (auto-mapped) |
| 5.8 | Tests + validation report | **Done** — 55 tests, this report |

---

## Verification Checklist

| Requirement | Status |
|-------------|--------|
| Every recall is intent-classified or falls back | **PASS** — classify_intent() always returns valid intent |
| Retrieval paths vary by intent | **PASS** — 8 intents × different adapter selections |
| At least one blended retrieval path is real and tested | **PASS** — procedural_recall merge, decision_recall fallback |
| Results ranked by more than similarity | **PASS** — 5-factor ranking |
| Retrieval explanation metadata exists | **PASS** — _recall_explanation on every result |
| A real production recall path is migrated | **PASS** — watcher.py pattern_retrieval |
| Tests validate Phase 5 behavior | **PASS** — 55 tests |
| No Phase 6+ work pulled in | **PASS** — no reflection, no autonomous promotion |

---

## Remaining Gaps

| Gap | Severity | Notes |
|-----|----------|-------|
| FusionMemoryAdapter returns [] | Medium | Fusion Memory is prompt-delegated, not callable from Python. Affects temporal, decision, relationship intents. |
| No usage_history tracking | Low | Phase 5 ranking includes recency + confidence but not "how often was this recalled before" |
| VaultAdapter availability varies | Low | vault_search may fail if MCP server is down. Fail-open behavior preserved. |
| No per-intent result format normalization | Low | Different adapters return different dict shapes. Phase 5 adds ranking/explanation metadata on top. |
| Heartbeat test timeout | Pre-existing | Unrelated to Phase 5. |
