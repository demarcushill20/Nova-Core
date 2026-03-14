# Retention Lifecycle Matrix (Phase 8, Step 8.2)

## Overview

This matrix defines retention targets, governance eligibility, and protection
levels for every major memory type, layer, and file-backed store in Nova-Core.

---

## By Memory Layer

### Layer 1: Working (Transient)

| Store | Path | Retention Target | Compaction | Archival | Stale | Prune | Protection | Trigger |
|-------|------|-----------------|------------|---------|-------|-------|------------|---------|
| Working Memory | STATE/working_memory/wm_*.json | 7 days | No | No | Auto at 7d | Auto at 7d | None | WorkingMemoryAdapter._cleanup_stale() on store() |
| Sessions | STATE/sessions/ses_*.json | 30 days | No | Yes (>30d) | At 30d | At 60d if archived | None | Governance sweep |
| Heartbeat checkpoints | STATE/working_memory/ | 7 days | No | No | Auto at 7d | Auto at 7d | None | Same as working memory |

**Anti-examples**: Do NOT prune session files less than 7 days old. Do NOT compact working memory — it's already ephemeral.

### Layer 2: Episodic (Permanent, Compactable)

| Store | Path | Retention Target | Compaction | Archival | Stale | Prune | Protection | Trigger |
|-------|------|-----------------|------------|---------|-------|-------|------------|---------|
| Workflow learnings | MEMORY/workflow_learnings/mem_*.json | Permanent | Thin dedup | Archive >90d low-importance | Not stale-eligible | Only duplicates | Medium | Governance sweep |
| Obsidian inbox | 00-inbox/ | Operator-managed | No | No | No | No | High (operator) | Manual |
| Obsidian research | 40-research/ | Permanent | No | No | No | No | High | Manual |

**Anti-examples**: Do NOT auto-delete episodic artifacts. Do NOT compact artifacts with importance ≥ 0.5. Do NOT touch Obsidian-managed files.

**Known limitations**: Obsidian vault is MCP-managed (read-only from governance). Fusion Memory episodic items cannot be governed from Python (prompt-delegated).

### Layer 3: Semantic (Permanent, Protected)

| Store | Path | Retention Target | Compaction | Archival | Stale | Prune | Protection | Trigger |
|-------|------|-----------------|------------|---------|-------|-------|------------|---------|
| Obsidian learnings | 30-workflow-learnings/ | Permanent | No | No | No | No | **Protected** | None |
| Fusion Memory pattern | Pinecone/Neo4j | Permanent | No | No | No | No | **Protected** | None |

**Anti-examples**: NEVER auto-prune semantic memory. NEVER compact without operator approval.

**Known limitations**: Fusion Memory cannot be governed from Python. Obsidian vault is MCP-managed.

### Layer 4: Procedural (Permanent, Highest Protection)

| Store | Path | Retention Target | Compaction | Archival | Stale | Prune | Protection | Trigger |
|-------|------|-----------------|------------|---------|-------|-------|------------|---------|
| Agent patterns (file) | MEMORY/agent_patterns/mem_*.json | Permanent | No | No | No | **Never** | **Protected** | None |
| Obsidian patterns | 20-agent-patterns/ | Permanent | No | No | No | **Never** | **Protected** | None |
| Obsidian ADRs | 10-adrs/ | Permanent | No | No | No | **Never** | **Protected** | None |
| Obsidian debugging | 70-debugging/ | Permanent | No | No | No | **Never** | **Protected** | None |

**Anti-examples**: NEVER auto-delete procedural memory. Superseded patterns are kept with superseded flag, not deleted.

---

## By Specialized Object Type

### Open Loops (STATE/open_loops/)

| Status | Retention Target | Stale Eligible | Prune Eligible | Archive Eligible | Protection |
|--------|-----------------|---------------|---------------|-----------------|------------|
| proposed | Indefinite while active | Yes (14d) | No | No | **Protected** (active) |
| open | Indefinite while active | Yes (14d) | No | No | **Protected** (active) |
| blocked | Indefinite while active | Yes (14d) | No | No | **Protected** (active) |
| deferred | Indefinite while active | Yes (14d) | No | No | **Protected** (active) |
| stale | 60 days after stale mark | N/A (already stale) | Yes (60d after stale) | Yes | Low |
| resolved | 30 days | No | Yes (30d) | Yes | None |
| closed_rejected | 30 days | No | Yes (30d) | Yes | None |

### Consolidation Summaries (stored via router)

| Type | Layer | Retention | Protection |
|------|-------|-----------|------------|
| Session summary | episodic | Permanent | Medium |
| Task batch summary | episodic | Permanent | Medium |
| Failure incident | episodic | Permanent | Medium |
| Working checkpoint | working | 7 days | None |
| Heartbeat summary | working | 7 days | None |
| Plan summary | episodic | Permanent | Medium |
| Pattern candidate | not stored | N/A | N/A |

### Operational State (STATE/)

| Store | Path | Retention Target | Governance |
|-------|------|-----------------|------------|
| Workflows | STATE/workflows/*.json | 30 days | Archive old |
| Plans | STATE/plans/*.json | 30 days | Archive old |
| Improvement runs | STATE/improvement_runs/ | 30 days | Archive old |
| Intents | STATE/intents/ | 30 days | Archive old |
| Activation log | STATE/activation_log.jsonl | 90 days | Truncate old entries |
| Task audit | STATE/task_audit.jsonl | 90 days | Truncate old entries |
| Tool audit | STATE/tool_audit.jsonl | 90 days | Truncate old entries |
| Notifications | STATE/notified/ | 14 days | Prune old |
| Config | STATE/config/ | Permanent | **Protected** |
| Budgets | STATE/budgets/ | Permanent | **Protected** |

### Structured Logs (LOGS/)

| Store | Path | Retention Target | Governance |
|-------|------|-----------------|------------|
| Current log | LOGS/structured.jsonl | Active (rotated at 10MB) | None |
| Rotated logs | LOGS/structured.*.jsonl | 30 days | Prune old rotations |

---

## Protection Level Summary

| Level | Meaning | Auto-Prune | Auto-Archive | Auto-Compact |
|-------|---------|------------|-------------|-------------|
| **Protected** | Never auto-modified | No | No | No |
| **High** | Operator-managed, governance skips | No | No | No |
| **Medium** | Retained long-term, compaction possible with evidence | No | Possible | Possible |
| **Low** | Standard retention rules apply | Yes (after threshold) | Yes | Yes |
| **None** | Fully governed by retention rules | Yes | Yes | Yes |

---

## Known Limitations

| Store | Limitation |
|-------|-----------|
| Fusion Memory (Pinecone/Neo4j/Redis) | Cannot be governed from Python; prompt-delegated |
| Obsidian Vault (all folders) | MCP-managed read-only; governance cannot modify |
| STATE/ misc files | Many operational files have no schema; governance acts on age + path only |
| Append-only JSONL logs | Truncation requires rewrite; not yet implemented |
