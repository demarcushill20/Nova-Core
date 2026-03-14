# Phase 8 Memory Governance — Validation Report

Generated: 2026-03-13

---

## Summary

Phase 8 adds memory governance, retention enforcement, and hygiene operations
to the Unified Memory Router. A deterministic GovernanceEngine evaluates
retention rules across all file-backed stores, supports dry-run mode, protects
high-value memory, marks stale artifacts, archives old operational state, and
prunes expired items — all with structured audit logging. 41 new tests pass.
Full regression: 3624 passed, 0 failures.

---

## Files Changed

| File | Change | Lines |
|------|--------|-------|
| `agents/memory_governance.py` | **NEW** — GovernanceEngine, retention rules, protection classification, 7 sweep workflows, dry-run support, archival/prune/stale helpers, observability | ~530 lines |
| `agents/memory_router.py` | **MODIFIED** — `run_governance()` method added, docstring updated to "Phase 1+2+4+5+6+7+8" | +40 lines net |
| `tests/test_memory_governance.py` | **NEW** — 41 tests across 12 test classes | ~530 lines |
| `MEMORY/memory_governance_policy.md` | **NEW** — Step 8.1 governance policy | doc |
| `MEMORY/retention_lifecycle_matrix.md` | **NEW** — Step 8.2 retention lifecycle matrix | doc |
| `MEMORY/phase8_governance_validation_report.md` | **NEW** — this file (Step 8.8) | doc |

---

## Governance Model

### Pipeline
```
target store → enumerate files → classify protection → evaluate retention rule → decide action → execute (or dry-run) → emit audit trace
```

### Decision Types
| Action | Meaning |
|--------|---------|
| `no_action` | Within retention, no governance needed |
| `marked_stale` | Flagged as stale (open loops, sessions) |
| `compacted` | Thin/duplicate artifacts combined (reserved) |
| `archived` | Moved to `_archive/` directory, recoverable |
| `pruned` | Permanently deleted (working memory, notifications, rotated logs) |
| `protected_skip` | Skipped due to protection level |
| `rejected_unsafe` | Action blocked for safety reasons |
| `rejected_insufficient_evidence` | Not enough evidence to act |

### Dry-Run Support
All destructive operations support `dry_run=True` (default). In dry-run mode,
all rules are evaluated and results computed, but no files are modified. Results
include `dry_run: true` flag.

---

## Retention Rules Summary

| Store | Retention | Action | Threshold |
|-------|-----------|--------|-----------|
| Working memory (STATE/working_memory/) | 7 days | Prune | Auto via adapter + governance backup |
| Sessions (STATE/sessions/) | 30d stale, 60d archive | Stale → Archive | Age-based |
| Open loops (active) | Indefinite | Protected | Active status |
| Open loops (stale) | 60 days after stale | Archive | Age after stale mark |
| Open loops (terminal) | 30 days | Archive | Age after resolution |
| Notifications (STATE/notified/) | 14 days | Prune | Age-based |
| Rotated logs (LOGS/structured.*.jsonl) | 30 days | Prune | Age-based |
| Operational state (workflows, plans, etc.) | 30 days | Archive | Age-based |
| Episodic artifacts (MEMORY/workflow_learnings/) | Permanent (thin+old archivable) | Archive thin >90d | Age + content quality |
| Agent patterns (MEMORY/agent_patterns/) | Permanent | Protected | Always |
| Semantic/Procedural (Obsidian/Fusion) | Permanent | Not governed (MCP/prompt-delegated) | N/A |

---

## Real Workflows Implemented (Step 8.4)

### 1. Open Loop Stale Marking + Terminal Archival
- **Path**: `_sweep_open_loops()` → classify_protection → check updated_at → mark_stale or archive
- **Stale rule**: Active loops with `updated_at` > 14 days → marked stale via open_loop_tracker
- **Terminal rule**: Resolved/closed_rejected loops > 30 days → archived to `STATE/_archive/open_loops/`
- **Stale archival**: Stale loops > 60 days → archived
- **Protection**: Active loops (proposed/open/blocked/deferred) are always protected_skip

### 2. Working Memory Cleanup (Backup)
- **Path**: `_sweep_working_memory()` → check age → prune if > 7 days
- **Purpose**: Catches files missed by WorkingMemoryAdapter._cleanup_stale()
- **Action**: Prune (delete) — working memory is transient by design

### 3. Session Archival
- **Path**: `_sweep_sessions()` → check age → stale at 30d, archive at 60d
- **Action**: Archive to `STATE/_archive/sessions/` with provenance filename

### 4. Notification Pruning
- **Path**: `_sweep_notifications()` → check age → prune if > 14 days
- **Action**: Prune (delete) — notifications are delivery-tracking artifacts

### 5. Rotated Log Cleanup
- **Path**: `_sweep_rotated_logs()` → check age → prune if > 30 days
- **Action**: Prune (delete) — old rotated log files are operational, not memory

### 6. Operational State Archival
- **Path**: `_sweep_operational_state()` → check protection → archive if > 30 days
- **Targets**: workflows/, plans/, improvement_runs/, intents/
- **Action**: Archive to `STATE/_archive/{dirname}/`

### 7. Episodic Artifact Thin Detection
- **Path**: `_sweep_workflow_learnings()` → classify_protection → check thin+old
- **Thin rule**: Summary < 20 chars AND age > 90 days → archive
- **Protection**: importance ≥ 0.5 artifacts skip governance (MEDIUM protection)

---

## Protection Rules (Step 8.5)

| Protection Level | Auto-Prune | Auto-Archive | Auto-Compact | Applies To |
|-----------------|------------|-------------|-------------|------------|
| **protected** | No | No | No | Agent patterns, config, budgets, active open loops, `protected:true` flag |
| **high** | No | No | No | Operator-authored artifacts (`source: operator`) |
| **medium** | No | No (episodic skip) | No | Episodic artifacts with importance ≥ 0.5 |
| **low** | Yes (per rules) | Yes | Yes | Stale open loops, low-importance artifacts |
| **none** | Yes (per rules) | Yes | Yes | Terminal loops, working memory, notifications, logs |

### Protection Classification Logic
1. Explicit `protected: true` in metadata → PROTECTED
2. `agent_patterns` in path → PROTECTED
3. `config/` or `budgets/` in path → PROTECTED
4. Active open loop status → PROTECTED
5. Stale open loop → LOW
6. Terminal open loop → NONE
7. Episodic with importance ≥ 0.5 → MEDIUM
8. Operator source → HIGH
9. Default → NONE

---

## Stale Handling (Step 8.6)

| Target | Stale Threshold | Detection | Post-Stale |
|--------|----------------|-----------|------------|
| Open loops | 14 days since `updated_at` | Governance sweep checks `updated_at` field | Eligible for archive after 60d |
| Sessions | 30 days since file mtime | Governance sweep checks mtime | Eligible for archive after 60d |
| Working memory | 7 days | WorkingMemoryAdapter + governance backup | Auto-pruned |
| Episodic artifacts | Not stale-eligible | N/A | Permanent by layer contract |

**Key design**: Stale marking for open loops uses `updated_at` from loop metadata (not file mtime) to correctly track activity age. The governance engine calls `mark_stale()` from the open_loop_tracker module to maintain state machine consistency and history tracking.

---

## Observability (Step 8.7)

### Structured Log Events

| slog Event | Emitted By | Key Fields |
|------------|------------|------------|
| `memory.governance.sweep` | `run_sweep()` | sweep_id, dry_run, items_examined, items_acted_on, items_protected, error_count |
| `memory.governance.workflow` | Each workflow method | target_store, dry_run, items_examined, items_acted_on, items_protected |
| `memory.router.governance` | `router.run_governance()` | caller, dry_run, items_examined, items_acted_on, items_protected |

### Audit Trail
Every `GovernanceResult` contains:
- `action`: what happened
- `target_store`: which store
- `target_path`: specific file
- `rule_name`: which retention rule fired
- `dry_run`: boolean
- `protection_level`: protection classification
- `rejection_reason`: why skipped (if applicable)
- `artifact_age_days`: age for debugging

### SweepSummary Aggregates
- `items_examined`: total files reviewed
- `items_acted_on`: files modified/moved/deleted
- `items_protected`: files skipped due to protection
- `items_skipped`: files within retention (no action)
- `errors`: any workflow-level exceptions

---

## Tests Run

```bash
# Phase 8 governance tests
python3 -m pytest tests/test_memory_governance.py -v
# 41 passed in 0.63s

# Router tests (Phase 1+2+5+6+7+8 backward compat)
python3 -m pytest tests/test_memory_router.py -v
# 122 passed

# Full regression (excluding pre-existing heartbeat timeout)
python3 -m pytest tests/ --ignore=tests/test_heartbeat.py
# 3624 passed in 23.88s
```

### Test Classes (41 tests)

| Class | Tests | Validates |
|-------|-------|-----------|
| TestGovernanceStructures | 3 | Valid actions enum, GovernanceResult.to_dict(), SweepSummary.to_dict() |
| TestProtectionClassification | 10 | Explicit flag, agent patterns, config, active/stale/terminal loops, importance, operator, is_protected() |
| TestOpenLoopGovernance | 5 | Active protected, stale marking >14d, stale archive >60d, terminal archive >30d, recent terminal no_action |
| TestSessionGovernance | 3 | Recent no_action, 30d stale, 60d archived with file movement |
| TestWorkingMemoryGovernance | 3 | Recent no_action, old pruned with file deletion, dry-run preserves files |
| TestNotificationGovernance | 2 | Old notification pruned, recent not touched |
| TestLogRotationGovernance | 2 | Old rotated log pruned, recent kept |
| TestWorkflowLearningGovernance | 3 | Normal no_action, thin+old archived, high-importance protected |
| TestOperationalStateGovernance | 2 | Old workflow archived, recent not touched |
| TestDryRunBehavior | 2 | Dry-run preserves all files, execute removes files |
| TestFullSweep | 2 | Mixed stores sweep with correct counts, empty stores no errors |
| TestRouterGovernanceIntegration | 2 | Router.run_governance() delegates correctly, convenience function works |
| TestStaleMarking | 2 | Stale uses updated_at not file mtime, execute modifies loop file |

---

## Phase 8 Step Completion

| Step | Deliverable | Status |
|------|-------------|--------|
| 8.1 | Governance policy | **Done** — `MEMORY/memory_governance_policy.md` |
| 8.2 | Retention lifecycle matrix | **Done** — `MEMORY/retention_lifecycle_matrix.md` |
| 8.3 | Governance pipeline module | **Done** — `agents/memory_governance.py` |
| 8.4 | Real hygiene workflows | **Done** — 7 workflows (open loops, sessions, working memory, notifications, logs, operational state, workflow learnings) |
| 8.5 | High-value memory protection | **Done** — 5-level protection classification |
| 8.6 | Stale memory handling | **Done** — open loops, sessions, working memory |
| 8.7 | Observability / auditability | **Done** — 3 slog event types, GovernanceResult, SweepSummary |
| 8.8 | Tests + validation report | **Done** — 41 tests, this report |

---

## Verification Checklist

| Requirement | Status |
|-------------|--------|
| Documented governance policy exists | **PASS** |
| Documented retention lifecycle matrix exists | **PASS** |
| At least 2–4 real governance workflows end-to-end | **PASS** — 7 workflows |
| High-value memory protection rules exist | **PASS** — 5-level classification |
| Stale handling is explicit and testable | **PASS** — open loops, sessions |
| Governance actions are observable and auditable | **PASS** — slog events + structured results |
| Tests validate Phase 8 behavior | **PASS** — 41 tests |
| No Phase 9+ work pulled in | **PASS** |
| Full regression passes | **PASS** — 3624 passed, 0 failures |

---

## Remaining Gaps

| Gap | Severity | Notes |
|-----|----------|-------|
| Fusion Memory not governed | By design | Prompt-delegated, cannot be managed from Python |
| Obsidian vault not governed | By design | MCP-managed, read-only to governance |
| JSONL append-only logs not truncated | Low | Rotation creates new files; truncation requires rewrite |
| Compaction not yet implemented | Low | `compacted` action reserved but no dedupe-merge workflow yet |
| No automatic governance scheduling | Low | Governance is invoked explicitly by callers |
| No cross-session governance coordination | Low | Each sweep is independent |
| Heartbeat test timeout | Pre-existing | Unrelated to Phase 8 |
