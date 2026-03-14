# Phase 4 Candidate Evaluation — Validation Report

Generated: 2026-03-13

---

## Summary

Phase 4 adds deterministic memory candidate evaluation to the Unified Memory
Router. Every candidate is scored (importance, novelty, durability) and receives
an explicit action decision (reject, working_only, keep, promotable) before
adapter dispatch. 36 new tests pass. Full regression: 3466 passed, 0 failures.

---

## Files Changed

| File | Change | Lines |
|------|--------|-------|
| `agents/memory_evaluator.py` | **NEW** — evaluation module with `evaluate_candidate()`, `EvalDecision`, 3 score helpers | 318 lines |
| `agents/memory_router.py` | Added evaluation step in `store()` between validation and target inference | +45 lines |
| `agents/memory_router.py` | Store trace now includes `eval_action`, `eval_reason`, `importance_score`, `promotion_eligibility` | +8 lines |
| `tests/test_memory_evaluator.py` | **NEW** — 36 tests across 11 test classes | 481 lines |
| `MEMORY/memory_candidate_evaluation_spec.md` | **NEW** — Step 4.1 evaluation policy spec | doc |
| `MEMORY/storage_decision_matrix.md` | **NEW** — Step 4.2 decision matrix | doc |
| `MEMORY/phase4_candidate_validation_report.md` | **NEW** — this file (Step 4.7) | doc |

---

## Tests Run

```bash
# Phase 4 evaluator tests
python3 -m pytest tests/test_memory_evaluator.py -v
# 36 passed in 0.57s

# Full regression
python3 -m pytest tests/
# 3466 passed in 72.72s, 0 failures
```

### New Test Classes (36 tests)

| Class | Tests | Validates |
|-------|-------|-----------|
| `TestImportanceScore` | 5 | Event type, confidence, content density scoring |
| `TestNoveltyScore` | 2 | Outcome vs transient novelty differentiation |
| `TestDurabilityScore` | 4 | Layer baseline, event type modifier, bounds |
| `TestRejectDecision` | 2 | Empty/sparse content rejected |
| `TestWorkingOnlyDecision` | 4 | Transient events, low-importance episodic, thin summaries |
| `TestKeepDecision` | 3 | Standard keeps, durable event handling, already-promoted |
| `TestPromotableDecision` | 3 | High-value outcomes, plan grades, low-confidence blockers |
| `TestPromotionBlocker` | 2 | Blocker reasons for low importance and transient types |
| `TestEvalDecisionStructure` | 3 | Valid actions, dict fields, numeric scores |
| `TestRouterEvaluationIntegration` | 6 | Scores applied to CMO, reject prevents storage, layer downgrade routing, Phase 2 guardrails preserved |
| `TestEvaluationTracing` | 2 | Store and rejection traces include eval fields |

---

## Phase 4 Step Completion

| Step | Deliverable | Status |
|------|-------------|--------|
| 4.1 | Evaluation policy spec | **Done** — `MEMORY/memory_candidate_evaluation_spec.md` |
| 4.2 | Storage decision matrix | **Done** — `MEMORY/storage_decision_matrix.md` |
| 4.3 | Evaluation logic | **Done** — `agents/memory_evaluator.py` |
| 4.4 | Router integration | **Done** — evaluation in `store()` path |
| 4.5 | Pollution prevention | **Done** — reject/working_only rules block low-value storage |
| 4.6 | Trace output | **Done** — eval_action, eval_reason, importance_score in slog traces |
| 4.7 | Tests + validation report | **Done** — 36 tests, this report |

---

## Phase 2 Guardrails Preserved

| Guard | Status |
|-------|--------|
| semantic → memory_file | **BLOCKED** (unchanged) |
| working → obsidian_vault | **BLOCKED** (unchanged) |
| episodic → state_working | **BLOCKED** (unchanged) |
| All existing layer/store combinations | **UNCHANGED** |

Phase 2 `check_store_layer_compatibility()` runs after evaluation. Evaluation
adjusts `current_layer` before Phase 2 checks, so the two systems compose
correctly without bypass.

---

## Evaluation Examples (from test data)

| Input | Importance | Action | Reason |
|-------|-----------|--------|--------|
| heartbeat_cycle, low confidence, short summary | 0.16 | working_only | transient_event |
| task_completed, high confidence, 200+ char summary | 0.54 | keep_and_mark_promotable | promotable_outcome |
| task_completed, low confidence, "Done." summary | 0.22 | working_only | low_importance |
| workflow_learning_promoted, semantic layer | 0.70 | keep_in_current_layer | durable_event |
| empty title + empty summary | 0.16 | reject | content_too_sparse |
| task_completed, high confidence, "Short." summary | 0.44 | working_only | thin_summary |

---

## Known Limitations

| Limitation | Severity | Notes |
|-----------|----------|-------|
| Novelty is event-type based only, no dedup against existing memories | Low | True novelty requires Phase 5+ retrieval-augmented evaluation |
| No per-project or per-source threshold tuning | Low | Single global policy is sufficient for current scale |
| Promotion eligibility is flag-only, no automatic promotion | By design | Promotion requires Phase 5+ promotion pipeline |
| No user-preference override for evaluation decisions | Low | Could add `force_keep` field to CMO if needed |
