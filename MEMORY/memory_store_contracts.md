# Memory Store Contracts

Phase 0 deliverable — every memory store, its role, and its operational contract.

Generated: 2026-03-13
Source: Codebase audit cross-referenced with actual stored data.

---

## Store 1: Fusion Memory (nova-memory MCP)

**Backend**: Pinecone (vector search) + Neo4j (graph) + Redis (timeline)
**Access**: MCP server spawned via stdio (`run_mcp_local.sh`)
**Repo**: `/home/nova/Nova_AI_Fusion_Memory_MCP`

| Property | Value |
|----------|-------|
| **Purpose** | Cross-session semantic knowledge: decisions, research findings, patterns, context, daily summaries, session checkpoints |
| **Data type** | Free-text with structured metadata (category, project, session_id, tags, event_seq) |
| **Memory types** | scratch, decision, checkpoint, research, pattern, context, debug |
| **Retention** | Permanent (no TTL, no auto-expiry) |
| **Retrieval style** | Semantic vector search (Pinecone) fused with graph traversal (Neo4j), reranked. Also: timeline-ordered by event_seq (Redis). |
| **Write authority** | Any Claude subprocess via MCP tool calls (upsert_memory, bulk_upsert_memory, create_checkpoint) |
| **Promotion rules** | None — no automated promotion out of Fusion Memory. Memories stay where written. |
| **What should NEVER be stored** | Secrets/API keys, raw file contents (store summaries), ephemeral task state (use TASKS/ for that), large binary data |
| **Validation** | MCP server-side only. No pre-write validation from NovaCore Python code. |
| **Current status** | Active. All 6 phases complete. 117 events stored as of 2026-03-13. |

**Known gaps**:
- No deduplication — same research topic can produce multiple near-identical entries
- No compaction — old events are never consolidated or archived
- No quality scoring — all memories treated equally regardless of usefulness

---

## Store 2: Obsidian Vault (nova-vault MCP)

**Backend**: Markdown files with YAML frontmatter on local filesystem
**Access**: MCP server (`tools/mcp_vault_server.py`), synced via Obsidian Sync
**Root**: `/home/nova/nova-vault`

| Property | Value |
|----------|-------|
| **Purpose** | Curated durable knowledge: agent patterns, workflow learnings, research summaries, implementation plans, debugging guides |
| **Data type** | Markdown notes with typed YAML frontmatter |
| **Note types** | agent-pattern, workflow-learning, research-summary, implementation-plan, debugging-guide, inbox |
| **Retention** | Permanent (human-managed lifecycle, no auto-deletion) |
| **Retrieval style** | Keyword search (vault_search), direct path read (vault_read), frontmatter inspection (vault_frontmatter), folder listing (vault_list) |
| **Write authority** | Bounded: vault_write requires source=nova-core-memory, feature flag enabled, folder in writable set, schema validation pass, size ≤ 34KB, rate limit 10/5min |
| **Writable folders** | 00-inbox, 20-agent-patterns, 30-workflow-learnings, 40-research, 70-debugging |
| **Read-only folders** | 10-adrs, 10-plans, 50-playbooks, 60-project, 80-references, 90-diary, _meta |
| **Promotion rules** | Workflow-learning → agent-pattern (requires 2+ converging learnings, 40% keyword overlap) |
| **What should NEVER be stored** | Secrets, runtime state, raw logs, ephemeral task data, large attachments |
| **Validation** | 9-step fail-closed pipeline (see vault_schema_spec.md) |
| **Current status** | Active. ~65 notes across 9 folders. Shared with human operator. |

**Known gaps**:
- implementation-plan type missing validation for status/priority/progress fields used by plan-tracker skill
- No date format validation (ISO 8601 not enforced)
- No ID format validation (pattern_id prefix "ap-", research_id prefix "rs-" not enforced)
- 10-plans/ folder is not writable — plan notes created in 00-inbox/ then manually moved by operator

---

## Store 3: File-Based Memory (MEMORY/ directory)

**Backend**: JSON files on local filesystem
**Access**: Direct Python file I/O via agents/memory_engine.py
**Root**: `/home/nova/nova-core/MEMORY/`

| Property | Value |
|----------|-------|
| **Purpose** | Machine-readable workflow learnings and agent patterns used for planner context injection |
| **Data type** | Structured JSON (MemoryArtifact schema) |
| **Subdirectories** | workflow_learnings/, agent_patterns/ |
| **Retention** | Permanent (append-only, no auto-deletion, no compaction) |
| **Retrieval style** | Load all artifacts, score by task_class match + keyword overlap + confidence + recency, return top 5 |
| **Write authority** | capture_workflow_memory() and capture_direct_task_memory() via write_memory_artifact() |
| **Promotion rules** | Workflow learnings can be promoted to vault notes via workflow_promoter.py |
| **What should NEVER be stored** | Secrets, raw file contents, runtime state, conversation logs |
| **Validation** | validate_memory_artifact(): required fields, enum checks, ID format regex, size ≤ 32KB, append-only |
| **Current status** | Active. 11 artifacts in workflow_learnings/, 0 in agent_patterns/. |

**Known gaps**:
- No cleanup/archival mechanism — artifacts accumulate forever
- No deduplication — same task can produce duplicate artifacts (observed: mem_0418 appears twice)
- Retrieval scoring is keyword-based, not semantic

---

## Store 4: Session State (STATE/ directory)

**Backend**: JSON files on local filesystem
**Access**: Direct Python file I/O via multiple modules
**Root**: `/home/nova/nova-core/STATE/`

| Property | Value |
|----------|-------|
| **Purpose** | Runtime execution state — NOT durable knowledge. Tracks active tasks, sessions, workflows, metrics, goals, conversation context. |
| **Data type** | Structured JSON (various schemas per subdirectory) |
| **Subdirectories** | sessions/, agents/, workflows/, delegations/, leases/, budgets/, config/, conversations/, conversation_recap/, intents/, running/, cancel/, notified/, plans/, policies/, improvement_runs/, archive/, audits/, ceo_delegated/ |
| **Retention** | sessions/: 7 days (cleanup_old_sessions). working_memory: 1 day. recent_completions: 4 hours. Others: permanent until manual cleanup. |
| **Retrieval style** | Direct file read by known path. No search. |
| **Write authority** | Various modules — session_manager, blackboard, watcher, telegram modules |
| **Promotion rules** | Session summaries → injected into next task prompt (not a store promotion) |
| **What should NEVER be stored** | Durable knowledge (use MEMORY/ or vault instead), secrets |
| **Validation** | Minimal — best-effort writes, errors silently passed |
| **Current status** | Active. ~17 subdirectories, dozens of files. |

**Note**: STATE/ is explicitly NOT a memory store. It is runtime state. It should not be routed
through the future memory router. Listed here for completeness to prevent confusion.

---

## Store 5: Audit Logs (LOGS/ directory)

**Backend**: JSONL files on local filesystem
**Access**: Direct Python file I/O via utils/audit_log.py and utils/structured_log.py
**Root**: `/home/nova/nova-core/LOGS/`

| Property | Value |
|----------|-------|
| **Purpose** | Immutable audit trail — security events, task execution, pattern feedback |
| **Data type** | Hash-chained JSONL (audit_log), structured JSONL (structured_log), append-only JSONL (pattern_feedback) |
| **Key files** | audit/audit_YYYY-MM-DD.jsonl, structured.jsonl, pattern_feedback.jsonl, watcher.log, heartbeat_agent.log |
| **Retention** | Permanent (no auto-rotation configured yet — logrotate planned in Phase 6) |
| **Retrieval style** | Sequential scan, hash chain verification |
| **Write authority** | AuditLogger (hash-chained), slog (structured events), pattern_feedback logger |
| **Promotion rules** | None — logs are read-only after write |
| **What should NEVER be stored** | Full prompt contents (truncate to 4000 chars), raw API responses, secrets |
| **Validation** | Audit log: SHA-256 hash chain integrity. Others: none. |
| **Current status** | Active. |

**Note**: LOGS/ is NOT a memory store. It is an audit/observability system. Listed here
because pattern_feedback.jsonl has memory-adjacent purpose (tracking which retrieved patterns
were actually useful). This feedback data could inform future memory quality scoring.

---

## Cross-Store Relationships

```
Task completion
  │
  ├─→ MEMORY/workflow_learnings/*.json    (immediate, validated)
  │     │
  │     └─→ Obsidian 30-workflow-learnings/   (if eligible, vault_validated)
  │           │
  │           └─→ Obsidian 20-agent-patterns/ (if 2+ converging, vault_validated)
  │
  ├─→ Fusion Memory (upsert_memory)       (prompt-delegated, no pre-validation)
  │
  ├─→ STATE/sessions/*.json               (runtime tracking, best-effort)
  │
  └─→ LOGS/ (audit, structured, pattern feedback)

Heartbeat cycle
  │
  ├─→ Fusion Memory (upsert_memory)       (prompt-delegated research/planning results)
  ├─→ Obsidian 40-research/ or 00-inbox/  (prompt-delegated vault_write)
  └─→ HEARTBEAT.md                        (status dashboard)
```

---

## Boundary Rules (for future router design)

1. **STATE/ and LOGS/ are OUT OF SCOPE** for the memory router. They are runtime/audit systems.
2. **Fusion Memory, Obsidian Vault, and MEMORY/ are IN SCOPE** — these are the three durable
   knowledge stores that the router should mediate.
3. **Prompt-delegated writes** (Fusion Memory + vault writes from heartbeat/watcher prompts)
   are the hardest to route because they happen inside Claude subprocess execution, not in
   Python code. Routing these will require either:
   - Changing the prompts to go through a router MCP tool instead of direct store calls, OR
   - Accepting that prompt-delegated writes bypass the router and adding post-hoc reconciliation.
