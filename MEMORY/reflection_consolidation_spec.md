# Reflection & Consolidation Spec

Phase 6 deliverable — defines what reflection and consolidation mean in
Nova-Core, their allowed inputs/outputs, and safety constraints.

Generated: 2026-03-13

---

## Definitions

### Reflection
Scoped consolidation cycle: consolidate recent working memory + scan for
pattern candidates. Not freeform introspection. Invoked via `router.reflect()`.

### Consolidation
Evidence-based transformation of accumulated lower-layer memory into
higher-value artifacts. Takes a window of events and produces:
- summaries (compress many items into one)
- checkpoints (mark session boundaries)
- promotion candidates (flag patterns for review)

Consolidation is deterministic. No LLM calls, no heuristic guessing.

---

## Allowed Inputs

| Input Source | Adapter | Layer | Use |
|-------------|---------|-------|-----|
| STATE/working_memory/ | state_working | working | Heartbeat cycles, session events |
| MEMORY/workflow_learnings/ | memory_file | episodic | Task completions, plans |
| Session data | SessionManager | working | Task records with CONTRACT summaries |

### What may NOT be used as consolidation input
- Obsidian vault notes (curated by operator — read-only for consolidation)
- Fusion Memory entries (prompt-delegated, not callable from Python)
- External system state (API responses, CI results)
- User conversations or messages

---

## Allowed Outputs

| Output Type | Layer | Store | Conditions |
|-------------|-------|-------|------------|
| Session summary | episodic | memory_file | ≥1 task with substance |
| Working memory checkpoint | working | state_working | ≥2 unique events |
| Task batch summary | episodic | memory_file | ≥2 completed tasks |
| Failure incident summary | episodic | memory_file | ≥2 failures |
| Heartbeat operational summary | working | state_working | ≥3 unique cycles |
| Plan execution summary | episodic | memory_file | ≥1 plan step |
| Pattern candidate (metadata only) | — | not stored | ≥3 evidence items |

### What must NEVER be generated automatically
- Semantic-layer memories (require operator review via promotion)
- Procedural-layer memories (require promotion pipeline)
- Obsidian vault notes (require vault_write + schema validation)
- ADRs or architecture decisions
- User preference overrides

---

## Confidence Requirements

| Output Type | Minimum Confidence | Criteria |
|-------------|-------------------|----------|
| Session summary | low (accepted) | Any completed tasks |
| Checkpoint | medium | All inputs are structured |
| Task batch summary | medium | Majority completed |
| Failure incident | medium | ≥2 failures with content |
| Promotion candidate | high | ≥3 high-confidence evidence items |

---

## Deduplication / Anti-Amplification Rules

### Content-hash deduplication
Before consolidation, all window items are deduped by content hash
(event_type + title + summary prefix + source). Identical items are reduced
to one. If dedup reduces below minimum window size, consolidation is rejected.

### Anti-amplification checks (run after dedup)

| Check | Blocks If | Applies To |
|-------|-----------|------------|
| Heartbeat noise | >80% of items are heartbeat_cycle | Non-heartbeat windows |
| All low confidence | Every item has confidence="low" | All windows |
| Uniform thin content | All items same event_type AND avg summary <20 chars | ≥3 items |

### What anti-amplification prevents
- Repeated heartbeat noise becoming many summary artifacts
- Same task batch generating duplicate checkpoints
- Low-confidence failures becoming episodic "truth"
- One weak pattern becoming a promotion candidate

---

## Layer Transition Expectations

Consolidation produces artifacts at the SAME or LOWER layer as inputs.
It never promotes directly.

| Input Layer | Output Layer | Allowed? |
|-------------|-------------|----------|
| working → working | checkpoint | YES |
| working → episodic | session summary | YES (via router store + evaluation) |
| episodic → episodic | task batch summary | YES |
| episodic → semantic | NO | Requires explicit promotion pipeline |
| any → procedural | NO | Requires promotion + operator review |

---

## Rejection Conditions

Consolidation returns `rejected_insufficient_evidence` when:
1. Window has fewer items than the minimum for its type
2. After dedup, unique items are below minimum
3. Anti-amplification check fails
4. Window-specific quality check fails (e.g., no completed tasks in task_batch)

---

## Module

`agents/memory_consolidator.py` — single-file module. Exports:
- `consolidate_window(spec) → ConsolidationResult`
- `summarize_session(session_data, working_memory) → ConsolidationResult`
- `extract_pattern_candidates(items, min_evidence) → list[PatternCandidate]`
- `dedupe_window_items(items) → (deduped, removed_count)`
- `check_anti_amplification(items, window_type) → list[str]`
- `WindowSpec`, `ConsolidationResult`, `PatternCandidate` dataclasses
