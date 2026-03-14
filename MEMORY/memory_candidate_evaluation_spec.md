# Memory Candidate Evaluation Spec

Phase 4 deliverable — defines the deterministic policy for evaluating memory candidates before persistence.

Generated: 2026-03-13

---

## Purpose

Every memory candidate passing through the router's `store()` path is evaluated
before adapter dispatch. Evaluation answers four questions:

1. **Is this worth storing at all?** (reject vs. accept)
2. **Which layer is appropriate?** (may downgrade from candidate layer)
3. **Is it promotion-eligible?** (can it move to a higher layer later?)
4. **What blocks promotion, if anything?** (explicit blocker reason)

## Design Principles

- **Deterministic, not probabilistic**: No ML, no LLM calls, no heuristic guessing.
  Given the same inputs, evaluation always produces the same output.
- **Policy-based**: Rules are explicit, ordered, and inspectable.
- **Fail-closed for quality**: When in doubt, downgrade or reject. Low-value
  memories pollute recall; it's better to lose a marginal candidate than to
  store noise.
- **Scores are diagnostic, not decisive**: The three scores (importance, novelty,
  durability) inform the decision rules but are not thresholds on their own.
  The rules combine scores with event type, layer, and content signals.

## Evaluation Position in Pipeline

```
ingest → validate CMO → ★ evaluate_candidate() ★ → infer target store → adapter dispatch → trace
```

Evaluation runs **after** CMO validation (schema correctness) and **before**
target store inference (adapter selection). This ensures:
- Invalid CMOs are caught before evaluation (no wasted scoring)
- Evaluation can adjust `current_layer` before the router selects an adapter
- Rejected candidates never reach adapter dispatch

## Three Scores

### Importance (0.0–1.0)
How significant is this event for the project?

| Signal | Weight |
|--------|--------|
| Event type base (durable=0.8, outcome=0.6, transient=0.2, other=0.4) | 50% |
| Confidence weight (high=1.0, medium=0.6, low=0.3) | 30% |
| Content density bonus (≥200 chars=0.2, ≥50 chars=0.1, else=0.0) | 20% |

### Novelty (0.0–1.0)
How fresh is this information? (Phase 4 conservative: event-type only.)

| Event Category | Score |
|---------------|-------|
| Outcome events (task_completed, plan_created, etc.) | 0.7 |
| Durable events (workflow_learning_promoted, etc.) | 0.5 |
| Transient events (heartbeat_cycle, session_end) | 0.2 |
| Other | 0.4 |

True novelty detection (comparing against existing memories) requires Phase 5+
retrieval-augmented evaluation.

### Durability (0.0–1.0)
How long-lived is the value of this memory?

| Signal | Contribution |
|--------|-------------|
| Layer baseline (working=0.1, episodic=0.4, semantic=0.7, procedural=0.9) | Base |
| Event type modifier (durable=+0.3, outcome=+0.1, other=+0.0) | Additive |
| Confidence modifier (weight × 0.1) | Additive |

## Thresholds

| Threshold | Value | Purpose |
|-----------|-------|---------|
| `_MIN_EPISODIC_SUMMARY_LEN` | 20 chars | Below this, episodic candidate is downgraded to working |
| `_MIN_EPISODIC_IMPORTANCE` | 0.3 | Below this, episodic candidate is downgraded to working |
| `_MIN_PROMOTABLE_IMPORTANCE` | 0.5 | Below this, candidate is not promotion-eligible |
| `_MIN_PROMOTABLE_DURABILITY` | 0.5 | Below this, candidate is not promotion-eligible |

## Event Type Classification

| Category | Event Types | Characteristics |
|----------|------------|----------------|
| Transient | `heartbeat_cycle`, `session_end` | Short-lived, repeating, low novelty |
| Outcome | `task_completed`, `task_failed`, `plan_created`, `plan_revised`, `research_completed`, `bug_fixed`, `code_changed` | First-occurrence results, higher importance |
| Durable | `workflow_learning_promoted`, `agent_pattern_promoted`, `decision_made`, `user_preference` | Verified knowledge, already deduplicated |

---

## Module

`agents/memory_evaluator.py` — single-file, no external dependencies beyond
`CanonicalMemoryObject` (duck-typed). Exposes:

- `evaluate_candidate(obj) → EvalDecision`
- `EvalDecision` dataclass (action, reason, 3 scores, promotion fields, adjusted_layer)
- `VALID_EVAL_ACTIONS` frozenset
- Score helpers: `_compute_importance()`, `_compute_novelty()`, `_compute_durability()`
