# Intent-Aware Recall Spec

Phase 5 deliverable — defines the recall intent model, classification, and
retrieval explanation metadata.

Generated: 2026-03-13

---

## What Is a Recall Intent?

A recall intent is a classification of *why* a caller is querying memory.
Different intents need different retrieval strategies:

- A "when did X happen?" query needs chronological search.
- A "how do we do X?" query needs pattern/playbook retrieval.
- A "what was decided about X?" query needs ADR/decision search.

Phase 5 makes this explicit: every recall query is classified into one of
8 intent types, which then drives adapter selection, blending, and ranking.

---

## Phase 5 Intent Enum

| Intent | Meaning | Example Query |
|--------|---------|--------------|
| `temporal_recall` | Timeline queries. When did X happen? | "when did we fix the auth bug?" |
| `decision_recall` | Decision/rationale queries. What was decided? | "why did we choose the router pattern?" |
| `procedural_recall` | How-to queries. Patterns, playbooks, methods. | "how should we validate vault schemas?" |
| `project_state_recall` | Current state queries. Status, progress. | "what is the current status of Phase 5?" |
| `user_preference_recall` | User preference queries. Style, config. | "what does the user prefer for code style?" |
| `factual_recall` | Stable knowledge queries. Architecture, schema. | "what is the memory router architecture?" |
| `open_loop_recall` | Unfinished work queries. Open threads, TODOs. | "what tasks are still pending?" |
| `relationship_entity_recall` | Entity relationship queries. Links, deps. | "what is related to the memory router?" |

### Anti-examples (NOT intents)

- "store this memory" → not a recall, it's a store operation
- "delete the old pattern" → not a recall, it's a mutation
- "validate the CMO schema" → not a recall, it's a validation check

---

## Classification Approach

**Deterministic, rule-based.** No ML, no LLM inference.

Signal extraction: regex patterns scan the query string for intent-specific
keywords. Each match produces a signal. The intent with the most signals wins.

### Confidence Levels

| Signals | Dominance | Confidence |
|---------|-----------|------------|
| ≥ 3 matching | any | high |
| 2 matching | any | medium |
| 1 matching | unique winner | medium |
| 1 matching | tied with another | low |
| 0 matching | — | low (fallback) |

### Fallback Behavior

When no keyword signals match, the classifier falls back to `factual_recall`
with `confidence="low"` and `fallback_used=True`. This is conservative: factual
recall queries memory_file + obsidian_vault (the broadest non-working scope).

### Ambiguity Handling

When two intents tie on signal count, the classifier picks the first by
alphabetical order (deterministic) and sets `confidence="low"`. Callers can
check `confidence` and decide whether to trust the classification.

---

## Caller-Provided vs. Inferred Intent

| Scenario | Behavior |
|----------|----------|
| Caller provides valid Phase 5 intent | Used directly, confidence="high", `caller_override=True` |
| Caller provides legacy Phase 1-4 intent | Mapped to Phase 5 equivalent, `legacy_mapped=True` |
| Caller provides invalid intent | Logged as warning, falls through to inference |
| No caller intent | Inference runs on query string |

### Legacy Intent Mapping

| Legacy Intent | Phase 5 Intent |
|--------------|---------------|
| `pattern_retrieval` | `procedural_recall` |
| `vault_context` | `factual_recall` |
| `prior_decision` | `decision_recall` |
| `session_replay` | `temporal_recall` |
| `general` | `factual_recall` |

---

## Retrieval Explanation Metadata

Every recall result includes a `_recall_explanation` dict:

```python
{
    "matched_intent": "procedural_recall",
    "matched_source": "memory_file",
    "why_matched": "keyword/content match (relevance=3.50)",
    "why_ranked_high": "authoritative source (memory_file); high confidence",
    "ranking_factors_used": {
        "recency": 0.5,
        "confidence": 1.0,
        "source_authority": 0.7,
        "relevance_score": 0.7,
        "promotion_level": 0.5,
    },
    "degradation_notes": []
}
```

### Fields

| Field | Purpose |
|-------|---------|
| `matched_intent` | Which intent this result was retrieved for |
| `matched_source` | Which adapter/store provided this result |
| `why_matched` | How this result matched the query (keyword, score, chronological) |
| `why_ranked_high` | Why this result is ranked above others |
| `ranking_factors_used` | Numeric breakdown of ranking factors |
| `degradation_notes` | Any fallback or mapping that occurred |

---

## Module

`agents/recall_intent.py` — single-file module. Exports:

- `classify_intent(query, caller_intent, context) → IntentClassification`
- `get_routing_plan(intent, scope_override, max_results) → RoutingPlan`
- `rank_results(results, classification, plan) → list[dict]`
- `dedupe_results(results) → list[dict]`
- `compute_ranking_score(result, emphasis) → (float, dict)`
- `VALID_RECALL_INTENTS` frozenset
- `IntentClassification`, `RoutingPlan`, `RecallExplanation` dataclasses
