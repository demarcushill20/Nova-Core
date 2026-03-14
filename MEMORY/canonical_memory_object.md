# Canonical Memory Object Schema

Phase 0 deliverable — one normalized internal representation for all memory candidates,
regardless of which store they ultimately land in.

Generated: 2026-03-13

---

## Purpose

This schema defines the internal object that the future memory router will process.
Any event that might become a durable memory passes through this shape before
the router decides where (and whether) to persist it.

It is intentionally a superset — not every field applies to every memory event.
Fields that don't apply should be null, not omitted.

---

## Schema Definition

```python
@dataclass
class CanonicalMemoryObject:
    # --- Identity ---
    memory_id: str            # Unique ID. Format: "cm-{source_abbrev}-{timestamp}"
                              # Example: "cm-wl-1773383475", "cm-hb-1773360439"
    timestamp: str            # ISO 8601 UTC. When the memory-worthy event occurred.
                              # Example: "2026-03-13T05:30:00Z"

    # --- Source & Provenance ---
    source: str               # Which component created this candidate.
                              # Enum: "watcher", "heartbeat", "telegram", "orchestrator",
                              #        "promoter", "operator", "daily_summary"
    source_file: str | None   # File path that triggered creation (e.g., task file, output file)
    source_function: str | None  # Function name that created this candidate
    event_type: str           # What kind of event produced this memory.
                              # Enum: "task_completed", "task_failed", "research_completed",
                              #        "plan_created", "plan_revised", "code_changed",
                              #        "pattern_detected", "decision_made", "bug_fixed",
                              #        "user_preference", "heartbeat_cycle", "session_end",
                              #        "conversation_insight", "workflow_learning_promoted",
                              #        "agent_pattern_promoted"
    provenance: str           # How this memory was created.
                              # Enum: "automatic", "prompt_delegated", "operator_requested",
                              #        "promotion", "consolidation"

    # --- Content ---
    title: str                # Short descriptive title (≤ 100 chars)
    summary: str              # One-paragraph summary of the memory content
    content: str | None       # Full content (for vault notes, research reports, etc.)
                              # May be null for lightweight episodic events.
    entities: list[str]       # Named entities mentioned (projects, tools, people, files)
                              # Example: ["watcher.py", "Langfuse", "Phase 4"]
    tags: list[str]           # Categorical tags for filtering and retrieval
                              # Example: ["#type/research", "#project/nova-core"]

    # --- Classification ---
    memory_layer_candidate: str  # Which memory layer this belongs to.
                                 # Enum: "working", "episodic", "semantic", "procedural"
                                 # See Phase 2 (4-layer model) for definitions.
    target_store: str | None     # Which store the router should write to. Set by router.
                                 # Enum: "fusion_memory", "obsidian_vault", "memory_file",
                                 #        "discard", null (not yet routed)

    # --- Scoring (set by candidate scorer, Phase 4) ---
    importance_score: float | None   # 0.0–1.0. How important is this memory?
    novelty_score: float | None      # 0.0–1.0. How new/unique is this information?
    durability_score: float | None   # 0.0–1.0. How long will this remain relevant?
    confidence: str                  # Enum: "high", "medium", "low"
                                     # How confident are we in this memory's accuracy?

    # --- Relationships ---
    related_task_ids: list[str]      # Task stems this memory relates to.
                                     # Example: ["0042_implement_feature"]
    related_files: list[str]         # Files created/modified in the originating event.
                                     # Example: ["utils/file_watcher.py"]
    related_project: str             # Project scope. Default: "nova-core"
    supersedes: str | None           # memory_id of a previous memory this replaces.
                                     # Used for plan revisions, corrected decisions, etc.

    # --- Lifecycle ---
    promotion_status: str            # Enum: "candidate", "persisted", "promoted",
                                     #        "rejected", "superseded", "archived"
    open_loop_status: str | None     # If this memory represents unfinished work:
                                     # Enum: "open", "resolved", "stale", null
    rejection_reason: str | None     # If promotion_status == "rejected", why?
                                     # Example: "duplicate", "low_score", "schema_violation"
```

---

## Field Mapping to Existing Stores

### → Fusion Memory (upsert_memory)

| Canonical Field | Fusion Memory Field | Notes |
|----------------|--------------------|----|
| memory_id | id | Direct mapping |
| summary | text | Fusion Memory stores text as primary content |
| event_type | metadata.category | Maps to: research, decision, pattern, context, debug |
| related_project | metadata.project | Direct mapping |
| tags | metadata.tags | Direct mapping |
| timestamp | metadata.event_time | Auto-set by MCP server |
| — | metadata.event_seq | Auto-incremented by MCP server |
| — | metadata.memory_type | scratch, decision, checkpoint |
| — | metadata.session_id | Session scope |

### → Obsidian Vault (vault_write)

| Canonical Field | Vault Frontmatter Field | Notes |
|----------------|------------------------|----|
| title | title | Direct mapping |
| confidence | confidence | Direct mapping |
| tags | tags | Direct mapping |
| event_type | type | Maps to: agent-pattern, workflow-learning, etc. |
| provenance | source | "automatic"/"promotion" → "nova-core-memory" |
| content | body (below frontmatter) | Full markdown content |
| related_task_ids | (not mapped) | Could go in body or custom field |
| related_files | (not mapped) | Could go in body |

### → File-Based Memory (MEMORY/*.json)

| Canonical Field | MemoryArtifact Field | Notes |
|----------------|---------------------|----|
| memory_id | artifact_id | Format differs: mem_<wf>_<ts> vs cm-<src>-<ts> |
| summary | task_summary | Direct mapping |
| entities | key_decisions, successful_patterns | Split across multiple fields |
| confidence | confidence | Direct mapping |
| related_task_ids | workflow_id | Single value in current schema |
| related_files | (derived from contract) | Not stored directly |

---

## Usage Guidelines

1. **This schema is a specification, not yet implemented in code.**
   Phase 0 documents it. Phase 1 will implement it as the router's internal format.

2. **Not every field must be populated.** Nullable fields should be null when not applicable.
   Required fields (memory_id, timestamp, source, event_type, title, summary, confidence,
   memory_layer_candidate, promotion_status, related_project) must always be set.

3. **Scoring fields are null until Phase 4** (Candidate Scoring). Until then, all candidates
   are treated equally. The router should not block on null scores.

4. **The memory_layer_candidate is a suggestion, not a final decision.** The router or scorer
   may override it based on content analysis.

5. **supersedes creates a linked chain.** When a plan is revised, the new version's `supersedes`
   field points to the previous version's memory_id. This enables timeline traversal.

6. **open_loop_status is Phase 7 territory.** Set to null until open-loop tracking is implemented.
   Included in the schema now to avoid a breaking migration later.

---

## Required Fields Summary

| Field | Required | Default |
|-------|----------|---------|
| memory_id | YES | — |
| timestamp | YES | — |
| source | YES | — |
| source_file | no | null |
| source_function | no | null |
| event_type | YES | — |
| provenance | YES | — |
| title | YES | — |
| summary | YES | — |
| content | no | null |
| entities | no | [] |
| tags | no | [] |
| memory_layer_candidate | YES | — |
| target_store | no | null |
| importance_score | no | null |
| novelty_score | no | null |
| durability_score | no | null |
| confidence | YES | "medium" |
| related_task_ids | no | [] |
| related_files | no | [] |
| related_project | YES | "nova-core" |
| supersedes | no | null |
| promotion_status | YES | "candidate" |
| open_loop_status | no | null |
| rejection_reason | no | null |
