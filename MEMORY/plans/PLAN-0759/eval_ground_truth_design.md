# PLAN-0759 Step 0.4 — Eval Ground-Truth Design

**Sprint**: 3 (Phase 0 close-out)
**Date**: 2026-04-13
**Status**: decision locked — LLM-as-judge (Option C)
**Author**: Implementer sub-agent (Sprint 3)

## Decision

Ground truth for the associative-recall evaluation harness is **LLM-as-judge**,
pinned to `claude-sonnet-4-6` at `temperature=0` with `max_tokens=256`. The
judge grades `(query, candidate_memory)` pairs against a simple 5-point rubric
(1.0 = directly answers, 0.0 = irrelevant) and returns a JSON object of the
form `{"score": <float in [0,1]>, "reasoning": <str>}`.

This decision is encoded in code at
`Nova_AI_Fusion_Memory_MCP/tests/eval/llm_judge.py` (the `DEFAULT_MODEL`
constant and the `_RUBRIC` prompt template).

## Rejected alternatives

### (A) Hand-labeled ground truth

Rejected on effort grounds. A statistically-meaningful hand-labeled set would
need ~50 queries × 10 candidates = 500 labels. The operator has explicitly
declined any hand-labeling for Phase 0; this cost belongs in a future phase
if the LLM-as-judge signal turns out to be too noisy to gate on.

### (B) Pseudo-labels from existing chain edges (`SUPERSEDES`, `COMPACTED_FROM`)

Rejected because **the live DB has zero `SUPERSEDES` and zero `COMPACTED_FROM`
edges**. The Sprint 2 audit confirmed this (see
`phase0_schema_audit.md`, "Associative-linking edge type baseline" table —
all 9 candidate edges at 0). There is literally no pseudo-label signal to
derive from the existing graph, so this option was a non-starter on this
instance.

### (D) Cross-encoder scorer (e.g. ms-marco)

Rejected on domain mismatch. nova-core memories are unstructured
decision / debug / context / research / scratch notes, not web Q&A pairs
like the training distribution of the common cross-encoder checkpoints
(`cross-encoder/ms-marco-*`). The existing Fusion MCP reranker already uses
`cross-encoder/ms-marco-MiniLM-L-6-v2`, and the whole point of the eval is
to validate the rerank + associative-recall stack *against* a stronger
independent judge, not to validate it against its own lineage.

## Phase 6 gate (from PLAN-0759)

An associative-linking change passes the gate iff, evaluated on the fixed
query set with the pinned judge:

    recall@10_after  - recall@10_before  >= +0.05
                         OR
    MRR_after        - MRR_before        >= +0.05

The `>=` semantics are pinned in code at
`tests/eval/associative_recall_eval.py:GATE_DELTA = 0.05` and the comparison
is in `compare_baselines()`. A unit test
(`test_compare_baselines_boundary_recall_delta_at_gate`) asserts that an
exact `GATE_DELTA` improvement satisfies the gate.

## Cost model

- ~50 queries in the eval set (upper bound; Phase 6 prep will pick the
  exact number)
- 10 candidates per query at `k=10`
- 1 judge call per `(query, candidate)` pair → ~500 judge calls
- Per call: rubric (~200 tokens) + query + candidate (1000 chars ≈ 250 tokens)
  in, ~100 tokens out
- Rough total: 500 × ~500 in + 500 × ~100 out ≈ 250k input + 50k output tokens
- Current `claude-sonnet-4-6` public pricing (≈$3/M in, ≈$15/M out):
  `250k × $3/M + 50k × $15/M = $0.75 + $0.75 = $1.50` per full eval run.
  Budget estimate in this memo is ~$0.30–$1.50; actual Phase 6 cost depends
  on the final query count and rubric token overhead.

## Reproducibility strategy

1. **Model pin.** `DEFAULT_MODEL = "claude-sonnet-4-6"` is pinned in
   `llm_judge.py` and echoed into every `judge_relevance` return dict, so
   every score is traceable to the model id that produced it.
2. **Temperature pin.** `DEFAULT_TEMPERATURE = 0.0` on every call. Any
   drift in a rerun should be attributable to a model-version change, not
   sampling noise.
3. **Save every judge response to disk.** Phase 6 is expected to persist
   the raw `(query, candidate_id, score, reasoning, model, timestamp)`
   records alongside the aggregated metrics so a reviewer can audit any
   individual judgement after the fact.
4. **Baseline filenames embed the model pin.** `tests/eval/baselines/.gitkeep`
   documents the convention:
   `baseline_<yyyymmdd>_<model-id>.json`. When Anthropic rolls a model
   revision, the next baseline gets a new filename rather than silently
   overwriting the old one.
5. **Known risk: model drift.** Anthropic does not guarantee stable outputs
   across minor model revisions. Phase 6 must treat any baseline older than
   N months (TBD by the operator) as suspect and re-run it on the current
   pin before gating a new change.

## Sprint 3 scope (what was built)

- `tests/eval/llm_judge.py` — `LLMJudge` class (constructor enforces API
  key, `judge_relevance()` parses + validates JSON, raises on bad input)
- `tests/eval/associative_recall_eval.py` — `EvalQuery` / `EvalResult`
  dataclasses, `run_eval()` runner, `save_baseline` / `load_baseline` /
  `compare_baselines` helpers
- `tests/eval/test_llm_judge.py` — 12 hermetic unit tests (no live API)
- `tests/eval/test_associative_recall_eval.py` — 27 hermetic unit tests
  covering the recall / MRR math and the gate logic
- `tests/eval/baselines/.gitkeep` — directory marker and filename convention

## Explicitly NOT in Sprint 3

- No initial query set. That's Phase 6 prep.
- No live judge calls. The Sprint 3 test run mocks the anthropic client
  entirely — zero real API calls, zero cost.
- No real baseline. The `tests/eval/baselines/` directory is empty apart
  from the `.gitkeep` marker.
