# Storage Decision Matrix

Phase 4 deliverable — maps evaluation outcomes to storage actions.

Generated: 2026-03-13

---

## Decision Rules (evaluated in order)

| # | Condition | Action | Adjusted Layer | Promotion | Reason Tag |
|---|-----------|--------|---------------|-----------|------------|
| 1 | title < 5 chars AND summary < 5 chars | `reject` | — | ineligible | `content_too_sparse` |
| 2 | event_type ∈ transient | `working_only` | working | conditional¹ | `transient_event` |
| 3 | event_type ∈ durable | `keep_in_current_layer` | — | already_promoted² | `durable_event` |
| 4 | current_layer = episodic AND importance < 0.3 | `working_only` | working | ineligible | `low_importance` |
| 5 | current_layer = episodic AND summary < 20 chars | `working_only` | working | ineligible | `thin_summary` |
| 6 | event_type ∈ outcome AND importance ≥ 0.5 AND durability ≥ 0.5 | `keep_and_mark_promotable` | — | eligible | `promotable_outcome` |
| 7 | (default) | `keep_in_current_layer` | — | conditional³ | `standard_keep` |

¹ Eligible if importance ≥ 0.5, otherwise ineligible with blocker "transient event type"
² `already_promoted` if current_layer ∈ {semantic, procedural}, otherwise `needs_review`
³ Eligible if importance ≥ 0.5 AND durability ≥ 0.5, otherwise ineligible with blocker

---

## Action Definitions

| Action | Effect on Storage | Effect on CMO | Route |
|--------|------------------|---------------|-------|
| `reject` | Not stored. Router returns `stored=False` with `evaluation_rejected` reason. | Scores written to CMO fields. | Exits store() early. |
| `working_only` | Stored in working layer only. | `current_layer` and `memory_layer_candidate` set to "working". `layer_reason` set. | Routes to `state_working` adapter. |
| `keep_in_current_layer` | Stored in original candidate layer. | Scores written. Promotion metadata set. | Normal adapter dispatch. |
| `keep_and_mark_promotable` | Stored in original layer, flagged for future promotion. | `promotion_eligibility` = "eligible", no blocker. | Normal adapter dispatch. |

---

## Score → Decision Flow

```
                    ┌─────────────┐
                    │ CMO arrives  │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │ title+summary│──── both < 5 chars ──→ REJECT
                    │ length check │
                    └──────┬──────┘
                           │ passes
                    ┌──────▼──────┐
                    │ transient?   │──── yes ──→ WORKING_ONLY
                    │ event type   │
                    └──────┬──────┘
                           │ no
                    ┌──────▼──────┐
                    │ durable?     │──── yes ──→ KEEP (already promoted)
                    │ event type   │
                    └──────┬──────┘
                           │ no
                    ┌──────▼──────┐
                    │ episodic +   │──── importance < 0.3 ──→ WORKING_ONLY
                    │ low quality? │──── summary < 20ch  ──→ WORKING_ONLY
                    └──────┬──────┘
                           │ passes
                    ┌──────▼──────┐
                    │ outcome +    │──── importance ≥ 0.5
                    │ high value?  │──── durability ≥ 0.5 ──→ PROMOTABLE
                    └──────┬──────┘
                           │ no
                    ┌──────▼──────┐
                    │  DEFAULT     │──→ KEEP_IN_CURRENT_LAYER
                    └─────────────┘
```

---

## Promotion Eligibility Rules

| Condition | Eligibility | Blocker |
|-----------|------------|---------|
| importance ≥ 0.5 AND durability ≥ 0.5 AND event ∈ outcome | `eligible` | None |
| importance < 0.5 | `ineligible` | "importance below promotable threshold" |
| durability < 0.5 | `ineligible` | "durability below promotable threshold" |
| event ∈ transient AND importance < 0.5 | `ineligible` | "transient event type" |
| event ∈ durable AND layer ∈ {semantic, procedural} | `already_promoted` | None |
| event ∈ durable AND layer ∉ {semantic, procedural} | `needs_review` | None |
| action = reject | `ineligible` | "empty content" |

---

## Phase 2 Guardrails Interaction

Evaluation runs **before** layer/store compatibility checks. The sequence:

1. Evaluation may adjust `current_layer` (e.g., episodic → working)
2. Router infers target store based on (possibly adjusted) `current_layer`
3. Phase 2 `check_store_layer_compatibility()` validates the final combination

This means:
- A `working_only` downgrade from episodic changes the routing path
- Phase 2 guards still block invalid combinations (e.g., semantic → memory_file)
- Evaluation cannot override Phase 2 — it only adjusts the candidate before Phase 2 runs
