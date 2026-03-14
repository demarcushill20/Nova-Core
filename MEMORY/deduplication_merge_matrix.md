# Deduplication & Merge Matrix (Phase 9, Step 9.2)

## Overview

This matrix defines dedup, merge, compaction, and supersession eligibility
for every major memory type in Nova-Core.

---

## Working Memory (STATE/working_memory/)

| Property | Value |
|----------|-------|
| Dedup eligible | **Yes** — exact hash match |
| Merge eligible | **No** — transient, prune instead |
| Compaction eligible | **Yes** — multiple thin entries of same event_type |
| Supersession eligible | **No** — ephemeral, not versioned |
| Protection | None |
| Surviving artifact | Newest by mtime |
| Provenance | N/A (pruned, not merged) |
| Anti-examples | Do NOT merge heartbeat entries across sessions |

---

## Session Summaries (MEMORY/workflow_learnings/ via consolidation)

| Property | Value |
|----------|-------|
| Dedup eligible | **Yes** — same workflow_id + session content |
| Merge eligible | **Yes** — same session_id, keep richer summary |
| Compaction eligible | **Yes** — repeated thin summaries for same workflow |
| Supersession eligible | **Yes** — newer summary supersedes older |
| Protection | Medium (importance ≥ 0.5 skips compaction) |
| Surviving artifact | Longest summary, highest confidence, newest timestamp |
| Provenance | `supersedes: <old_id>`, `compacted_from: [ids]` |
| Anti-examples | Do NOT merge summaries from different workflows |

---

## Checkpoint Artifacts (STATE/working_memory/)

| Property | Value |
|----------|-------|
| Dedup eligible | **Yes** — exact hash match |
| Merge eligible | **No** — working layer, prune old instead |
| Compaction eligible | **No** — already ephemeral |
| Supersession eligible | **No** — stateless checkpoints |
| Protection | None |
| Surviving artifact | Newest by mtime |
| Provenance | N/A |
| Anti-examples | Do NOT attempt to merge checkpoints across sessions |

---

## Plan Summaries (MEMORY/workflow_learnings/)

| Property | Value |
|----------|-------|
| Dedup eligible | **Yes** — same workflow_id + plan content |
| Merge eligible | **Yes** — newer plan revision supersedes older |
| Compaction eligible | **Yes** — repeated thin plan artifacts |
| Supersession eligible | **Yes** — plan_revised supersedes plan_created |
| Protection | Medium |
| Surviving artifact | Most recent revision, richest content |
| Provenance | `supersedes: <old_plan_id>` |
| Anti-examples | Do NOT merge plans from different projects |

---

## Open Loop Records (STATE/open_loops/)

| Property | Value |
|----------|-------|
| Dedup eligible | **Yes** — existing dedupe_key (project + title hash) |
| Merge eligible | **No** — each loop is a distinct lifecycle object |
| Compaction eligible | **No** — lifecycle integrity matters |
| Supersession eligible | **No** — loops are tracked, not versioned |
| Protection | **Protected** while active |
| Surviving artifact | N/A — dedupe prevents creation, not post-hoc merge |
| Provenance | N/A |
| Anti-examples | NEVER merge two open loops, even if titles are similar |

---

## Episodic Artifacts (MEMORY/workflow_learnings/)

| Property | Value |
|----------|-------|
| Dedup eligible | **Yes** — content hash or workflow_id match |
| Merge eligible | **Yes** — same workflow_id, content overlap |
| Compaction eligible | **Yes** — thin artifacts of same event_type |
| Supersession eligible | **Yes** — newer replaces older for same work |
| Protection | Medium (importance ≥ 0.5 skips compaction) |
| Surviving artifact | Richest content, highest confidence |
| Provenance | `supersedes`, `compacted_from` |
| Anti-examples | Do NOT merge artifacts with different event_types |

---

## Semantic Memory (Obsidian 30-workflow-learnings/, Fusion Memory)

| Property | Value |
|----------|-------|
| Dedup eligible | Detection only — cannot modify (MCP/prompt-delegated) |
| Merge eligible | **No** — operator-managed |
| Compaction eligible | **No** — verified knowledge |
| Supersession eligible | **No** — requires operator |
| Protection | **Protected** |
| Surviving artifact | N/A |
| Provenance | N/A |
| Anti-examples | NEVER auto-merge semantic memories |

---

## Procedural Memory (MEMORY/agent_patterns/, Obsidian ADRs/patterns)

| Property | Value |
|----------|-------|
| Dedup eligible | Detection only — flag but do not act |
| Merge eligible | **Never** |
| Compaction eligible | **Never** |
| Supersession eligible | **Never** auto — operator only |
| Protection | **Protected** |
| Surviving artifact | N/A |
| Provenance | N/A |
| Anti-examples | NEVER auto-merge ADRs or agent patterns |

---

## Notifications (STATE/notified/)

| Property | Value |
|----------|-------|
| Dedup eligible | **Yes** — exact content match |
| Merge eligible | **No** — prune instead |
| Compaction eligible | **No** — prune old ones |
| Supersession eligible | **No** — transient |
| Protection | None |
| Surviving artifact | N/A (pruned by governance) |
| Provenance | N/A |
| Anti-examples | Do NOT merge notifications — just prune old ones |

---

## Consolidation Summaries (produced by memory_consolidator)

| Property | Value |
|----------|-------|
| Dedup eligible | **Yes** — same consolidation window re-run |
| Merge eligible | **Yes** — newer consolidation supersedes older |
| Compaction eligible | **No** — already compacted |
| Supersession eligible | **Yes** — re-consolidation supersedes prior |
| Protection | Medium |
| Surviving artifact | Newest consolidation output |
| Provenance | `supersedes`, `provenance: "consolidation"` |
| Anti-examples | Do NOT compact consolidation outputs further |

---

## Known Limitations

| Store | Limitation |
|-------|-----------|
| Fusion Memory | Cannot deduplicate (prompt-delegated, no Python API) |
| Obsidian Vault | Cannot modify (MCP read-only to compaction) |
| Cross-store duplicates | Detection only; resolution requires manual triage |
