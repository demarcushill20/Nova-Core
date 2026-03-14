# Store-to-Layer Mapping

Phase 2 deliverable — maps actual Nova-Core stores and components to the 4-layer model.

Generated: 2026-03-13

---

## Mapping Table

| Store / Component | Current Role | Assigned Layer(s) | Write Authority | Retrieval Mode | Authoritative? | Notes |
|---|---|---|---|---|---|---|
| STATE/sessions/ | Runtime session tracking | **working** | session_manager | Direct path read | YES | 7-day retention. Not durable knowledge. |
| STATE/working_memory/ | Conversation scratch | **working** | telegram modules | Direct path read | YES | 1-day retention. |
| STATE/conversations/ | Conversation context | **working** | telegram modules | Direct path read | YES | Transient context. |
| STATE/* (all others) | Runtime coordination | **working** | Various | Direct path read | YES | Workflows, delegations, budgets, leases — all runtime state. |
| MEMORY/workflow_learnings/*.json | Task completion artifacts | **episodic** | memory_engine | Keyword search + score | YES | Records of specific completed workflows. |
| Fusion Memory (scratch, checkpoint) | Session data, checkpoints | **working** | Claude subprocess (prompt-delegated) | Semantic search | TRANSITIONAL | Checkpoints are working-layer but stored in a permanent store. |
| Fusion Memory (decision, research, context) | Decisions and findings | **episodic** | Claude subprocess (prompt-delegated) | Semantic search | TRANSITIONAL | Episodic events stored with no layer tag. |
| Fusion Memory (pattern) | Detected patterns | **semantic** | Claude subprocess (prompt-delegated) | Semantic search | TRANSITIONAL | Pattern-category memories are stable facts. |
| Obsidian 30-workflow-learnings/ | Promoted workflow notes | **semantic** | workflow_promoter via vault_write | vault_search | YES | Promoted from episodic (MEMORY/). Schema-validated. |
| Obsidian 40-research/ | Research reports | **episodic** → **semantic** | heartbeat prompt + vault_write | vault_search | TRANSITIONAL | Mix: heartbeat-generated (episodic) vs. consolidated (semantic). |
| Obsidian 00-inbox/ | Incoming uncategorized | **episodic** | heartbeat, router | vault_search | YES | Triage destination. May be promoted. |
| Obsidian 20-agent-patterns/ | Proven agent patterns | **procedural** | pattern_promoter via vault_write | vault_search, pattern_retriever | YES | Promoted from 2+ converging learnings. Highest-value layer. |
| Obsidian 10-adrs/ | Architecture decisions | **procedural** | Operator only (read-only folder) | vault_read | YES | Operator-managed. Authoritative architectural record. |
| Obsidian 70-debugging/ | Debugging playbooks | **procedural** | Router / operator | vault_search | YES | Proven troubleshooting procedures. |
| MEMORY/agent_patterns/*.json | File-based agent patterns | **procedural** | memory_engine | Keyword search + score | YES | Parallel to vault 20-agent-patterns/ (different format). |

---

## Mixed-Purpose Stores (Documented Problems)

### Fusion Memory mixes working + episodic + semantic

**Problem**: Fusion Memory stores checkpoints (working), task decisions (episodic),
and stable patterns (semantic) in the same backend with no layer distinction.
A checkpoint and a proven pattern have the same retrieval weight.

**Impact**: Recall from Fusion Memory returns a mix of transient and stable memories.
No way to filter by layer in the current MCP API.

**Mitigation**: Fusion Memory writes are prompt-delegated and cannot be layer-enforced
from Python. This is documented as a Phase 2 enforcement gap. The router's
`metadata.category` field (scratch/decision/pattern/etc.) partially maps to layers
but is not rigorous.

### Obsidian 40-research/ mixes episodic and semantic

**Problem**: Some research notes are single heartbeat-generated reports (episodic),
while others are consolidated findings promoted by the operator (semantic).

**Impact**: Low. Research notes are read-only after creation. The frontmatter
`source` field distinguishes: `nova-core-memory` (automatic, likely episodic)
vs. `operator` (curated, likely semantic).

**Mitigation**: Future promotion should set `current_layer` in the note's metadata.
For now, the `source` field provides a reasonable heuristic.

---

## Layer → Allowed Stores

| Layer | Allowed Stores | Disallowed Stores |
|-------|---------------|-------------------|
| working | STATE/*, Fusion Memory (checkpoint/scratch) | Obsidian Vault (never store transient state in vault) |
| episodic | MEMORY/workflow_learnings/, Fusion Memory (decision/research), Obsidian 00-inbox/, Obsidian 40-research/ | STATE/* (wrong direction), Obsidian 20-agent-patterns/ (must be promoted) |
| semantic | Obsidian 30-workflow-learnings/, Obsidian 40-research/ (consolidated), Fusion Memory (pattern/context) | STATE/*, MEMORY/workflow_learnings/ (that's episodic) |
| procedural | Obsidian 20-agent-patterns/, Obsidian 10-adrs/, Obsidian 70-debugging/, MEMORY/agent_patterns/ | STATE/*, Obsidian 00-inbox/ (must be curated, not inbox) |

---

## Enforcement Status

| Store | Layer Enforcement | Status |
|-------|------------------|--------|
| Router-managed stores | Schema-enforced via `current_layer` field | **Phase 2 enforced** |
| WorkingMemoryAdapter (STATE/working_memory/) | Layer-enforced: only `working` layer allowed | **Phase 3 enforced** |
| Obsidian Vault (vault_write) | Not layer-enforced (schema checks type, not layer) | Gap — documented |
| Fusion Memory | Not layer-enforced (prompt-delegated) | Gap — cannot enforce from Python |
| STATE/* (non-router paths) | Not routed through memory router (out of scope) | By design |
| MEMORY/ via memory_engine | Layer-inferred from write target | **Phase 2 enforced** via router |
