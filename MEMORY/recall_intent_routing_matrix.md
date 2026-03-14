# Recall Intent Routing Matrix

Phase 5 deliverable — maps each recall intent to adapters, blend mode,
and ranking emphasis.

Generated: 2026-03-13

---

## Routing Table

| Intent | Primary Adapters | Secondary Adapters | Blend Mode | Ranking Emphasis |
|--------|-----------------|-------------------|------------|-----------------|
| `temporal_recall` | state_working, memory_file | — | none | recency, confidence |
| `decision_recall` | obsidian_vault | memory_file | fallback | source_authority, confidence, recency |
| `procedural_recall` | memory_file, obsidian_vault | — | merge | source_authority, confidence, recency |
| `project_state_recall` | state_working, memory_file | obsidian_vault | fallback | recency, confidence |
| `user_preference_recall` | obsidian_vault | memory_file | fallback | source_authority, confidence |
| `factual_recall` | memory_file, obsidian_vault | — | merge | source_authority, confidence, recency |
| `open_loop_recall` | state_working, memory_file | obsidian_vault | fallback | recency, confidence |
| `relationship_entity_recall` | memory_file, obsidian_vault | — | merge | confidence, source_authority |

---

## Blend Modes

| Mode | Behavior |
|------|----------|
| `none` | Only primary adapters are queried. |
| `merge` | Primary adapters are queried; all results are merged and deduped. |
| `fallback` | Primary adapters are queried first. If empty, secondary adapters are queried. |

### Deduplication

When multiple adapters return results (merge or fallback), results are deduped by:
1. `memory_id` (if present)
2. `path` (for vault results)
3. `title:source` composite key

Higher-ranked duplicates are kept.

---

## Ranking Factors

| Factor | Weight | Source |
|--------|--------|--------|
| `recency` | 0.25 | Timestamp field, decay curve (1.0 at 0d → 0.1 at 30d+) |
| `confidence` | 0.20 | `confidence` field (high=1.0, medium=0.6, low=0.3) |
| `source_authority` | 0.25 | Adapter origin (vault=1.0, memory_file=0.7, fusion=0.6, working=0.3) |
| `relevance_score` | 0.20 | Adapter-native score, normalized to 0–1 |
| `promotion_level` | 0.10 | Memory layer (procedural=1.0, semantic=0.8, episodic=0.5, working=0.2) |

**Emphasis boost**: When a factor appears in the intent's `ranking_emphasis` list,
its weight is multiplied by 1.5×.

---

## Intent-by-Intent Detail

### temporal_recall
- **Use case**: "when did X happen?", "what changed recently?"
- **Primary**: state_working (transient events), memory_file (task artifacts)
- **Ranking**: Recency dominates. Most recent first.
- **Limitations**: FusionMemoryAdapter is skeleton — cannot query Redis timeline yet.

### decision_recall
- **Use case**: "why did we decide X?", "what was the ADR for Y?"
- **Primary**: obsidian_vault (ADRs, decision notes)
- **Fallback**: memory_file (if vault returns nothing)
- **Ranking**: Source authority (vault is most authoritative for decisions).
- **Limitations**: Fusion Memory decision-category memories not yet queryable from Python.

### procedural_recall
- **Use case**: "how do we do X?", "what pattern works for Y?"
- **Primary**: memory_file (workflow learnings, agent patterns) + obsidian_vault (pattern notes, playbooks)
- **Blend**: merge — combine results from both.
- **Ranking**: Source authority + confidence.
- **Example**: watcher.py pattern retrieval (legacy `intent="pattern_retrieval"`).

### project_state_recall
- **Use case**: "what is the status?", "current progress?"
- **Primary**: state_working + memory_file
- **Fallback**: obsidian_vault (implementation plans)
- **Ranking**: Recency dominates (state is temporal).

### user_preference_recall
- **Use case**: "user prefers X", "what style does the user want?"
- **Primary**: obsidian_vault (preference notes)
- **Fallback**: memory_file (if vault unavailable)
- **Ranking**: Source authority.
- **Limitations**: No dedicated user-preference store. Relies on vault note discovery.

### factual_recall
- **Use case**: "what is X?", "architecture of Y?"
- **Primary**: memory_file + obsidian_vault (broadest query)
- **Blend**: merge.
- **Ranking**: Source authority + confidence + recency.
- **Note**: This is the default fallback intent for ambiguous queries.

### open_loop_recall
- **Use case**: "what is unfinished?", "open threads?"
- **Primary**: state_working + memory_file
- **Fallback**: obsidian_vault
- **Ranking**: Recency.
- **Limitations**: No dedicated open-loop tracker. Relies on working memory + recent artifacts.

### relationship_entity_recall
- **Use case**: "what is related to X?", "dependencies of Y?"
- **Primary**: memory_file + obsidian_vault
- **Blend**: merge.
- **Ranking**: Confidence + source authority.
- **Limitations**: True graph traversal requires FusionMemory Neo4j (not yet callable from Python).

---

## Known Limitations

| Limitation | Affected Intents | Severity |
|-----------|-----------------|----------|
| FusionMemoryAdapter is skeleton (recall returns []) | temporal, decision, relationship_entity | Medium |
| No dedicated user-preference store | user_preference_recall | Low |
| No Redis timeline query from Python | temporal_recall | Medium |
| No Neo4j graph traversal from Python | relationship_entity_recall | Medium |
| Novelty/usage_history not tracked for ranking | All | Low |
| Obsidian vault_search availability varies | All vault-routed intents | Low |

---

## Scope Override

For backward compatibility, callers can pass `scope` to constrain to a single
adapter regardless of intent:

| Scope | Adapter |
|-------|---------|
| `memory_files` | memory_file |
| `vault` | obsidian_vault |
| `fusion` | fusion_memory |
| `working` | state_working |
| `all` (default) | Intent-based routing |
