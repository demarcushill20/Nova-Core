# Phase 0 Write Path Validation Report

Generated: 2026-03-13

---

## Write Path Summary

| # | Write Path | File | Function | Target Store | Validation | Enforcement | Prompt-Delegated | Risk |
|---|-----------|------|----------|-------------|-----------|------------|-----------------|------|
| 1 | Vault Create | tools/mcp_vault_server.py | vault_write() L1086 | Obsidian Vault | 10-step pipeline | STRONG | NO | LOW |
| 2 | Vault Update | tools/mcp_vault_server.py | vault_update() L1199 | Obsidian Vault | 8-step pipeline | STRONG | NO | LOW |
| 3 | Memory Artifact | agents/memory_engine.py | write_memory_artifact() L175 | MEMORY/ (JSON) | Schema + size + overwrite | STRONG | NO | LOW |
| 4 | Fusion Memory | heartbeat.py, watcher.py | upsert_memory (via prompt) | Pinecone/Neo4j/Redis | Text pattern match only | WEAK | YES | MEDIUM |
| 5 | Task Output | watcher.py | (delegated to Claude) | OUTPUT/ | Contract block check | WEAK | YES | LOW |
| 6 | Agent State | agents/blackboard.py | _write_json() L117 | STATE/ (JSON) | None | NONE | NO | LOW |
| 7 | Structured Logs | utils/structured_log.py | event() | LOGS/structured.jsonl | None | NONE | NO | NEGLIGIBLE |
| 8 | Audit Chain | utils/audit_log.py | log() | LOGS/audit/ | None | NONE | NO | NEGLIGIBLE |
| 9 | Repair Script | scripts/repair_vault_types.py | _apply_fix() L173 | Obsidian Vault | Fixability check + operator flag | MODERATE | NO | LOW |
| 10 | Session State | agents/session_manager.py | _persist() L242 | STATE/ (JSON) | None | NONE | NO | LOW |
| 11 | Metrics | watcher.py | _update_metrics() L351 | STATE/metrics.json | None | NONE | NO | NEGLIGIBLE |
| 12 | Budget Usage | agents/budget_enforcer.py | _save_daily_usage() L421 | STATE/ (JSON) | None | NONE | NO | NEGLIGIBLE |

---

## Detailed Path Analysis

### Path 1: Vault Create (STRONG)
- **10-step validation pipeline**: feature flag → path safety → folder restriction → overwrite check → frontmatter presence → source enforcement → schema validation → size limit → sensitive content scan → rate limit
- **Atomic write**: tmp file + rename
- **Audit logged**: `.nova-audit.log`
- **Phase 0 additions**: ADR type enum, implementation-plan optional field validation
- **Test coverage**: 28 tests in TestVaultWrite + 18 in TestValidateFrontmatter + 73 in standalone schema tests

### Path 2: Vault Update (STRONG)
- **8-step validation pipeline**: feature flag → path + existence → folder → section inputs → ownership protection → sensitive content → size post-append → rate limit
- **Ownership protection**: prevents updating operator-authored notes
- **Test coverage**: 18 tests in TestVaultUpdate + 3 in TestDetectNoteOwnership

### Path 3: Memory Artifact (STRONG)
- **Schema validation**: required fields, enum checks, ID format regex, size ≤ 32KB
- **Append-only**: rejects overwrites
- **Atomic write**: tmp + rename
- **Test coverage**: 10 validation + 6 write + 4 capture tests

### Path 4: Fusion Memory MCP (WEAK)
- **Fully prompt-delegated**: heartbeat.py and watcher.py inject prompts instructing Claude to call `upsert_memory`
- **Post-hoc validation**: text pattern matching (`"upsert_memory" in response and "success" in response`)
- **No schema validation**: whatever Claude writes is accepted by the MCP server
- **Risk**: Medium — Fusion Memory MCP server performs its own basic validation, but there's no schema enforcement matching vault standards
- **Recommended follow-up**: Phase 1 (Memory Router) should validate before delegation

### Path 5: Task Output (WEAK)
- **Prompt-delegated**: watcher dispatch prompt instructs Claude to write OUTPUT files
- **Contract validation**: checks required CONTRACT block in output
- **No content validation**: only structural check
- **Risk**: Low — output is human-reviewed

### Paths 6-12: State/Logs/Metrics (NONE)
- **No validation**: these are internal bookkeeping writes
- **All use atomic write patterns**: safe against partial writes
- **Risk**: Low to negligible — not durable knowledge stores

---

## Key Findings

1. **Strong paths (1-3)**: Vault writes and memory artifacts are fully validated with fail-closed enforcement. Phase 0 extended validation to cover ADR type and implementation-plan optional fields.

2. **Weak paths (4-5)**: Fusion Memory and OUTPUT writes are prompt-delegated. Validation is post-hoc text matching, not schema enforcement. This is the primary gap for Phase 1 to address.

3. **Unvalidated paths (6-12)**: Internal state, logs, metrics. These are not knowledge stores and do not require Phase 1 validation.

4. **All file writes use atomic patterns**: Every write path uses tmp + rename or append with lock. No partial write risk.

---

## Recommended Follow-Up by Phase

| Path | Phase 1 Action |
|------|---------------|
| Fusion Memory (4) | Route through Memory Router with schema validation before delegation |
| OUTPUT (5) | No action needed — contract validation is sufficient |
| Blackboard (6) | No action needed — internal state |
| Logs (7-8) | No action needed — advisory |
| Others (9-12) | No action needed |
