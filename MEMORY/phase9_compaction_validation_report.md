# Phase 9 Compaction, Deduplication & Supersession — Validation Report

Generated: 2026-03-13

---

## Summary

Phase 9 adds deterministic duplicate detection, safe compaction, and explicit
supersession to the Nova-Core memory system. A CompactionEngine scans
file-backed stores, detects exact and near-duplicate artifacts using content
hashing and field comparison, compacts thin artifact groups with provenance
preservation, and marks older items as superseded without destructive deletion.
All operations support dry-run mode and emit structured audit logs. 51 new
tests pass. Full regression: 3675 passed, 0 failures.

---

## Files Changed

| File | Change | Lines |
|------|--------|-------|
| `agents/memory_compactor.py` | **NEW** — CompactionEngine, duplicate detection, supersession, compact_group, dedup workflow, protection checks, observability | ~580 lines |
| `agents/memory_router.py` | **MODIFIED** — `run_compaction()` method added, docstring updated to "Phase 1+2+4+5+6+7+8+9" | +50 lines net |
| `tests/test_memory_compactor.py` | **NEW** — 51 tests across 14 test classes | ~580 lines |
| `MEMORY/compaction_dedup_supersession_spec.md` | **NEW** — Step 9.1 policy document | doc |
| `MEMORY/deduplication_merge_matrix.md` | **NEW** — Step 9.2 merge eligibility matrix | doc |
| `MEMORY/phase9_compaction_validation_report.md` | **NEW** — this file (Step 9.8) | doc |

---

## Dedup Model

### Detection Pipeline
```
load artifacts → index by content_hash, title_key, workflow_key
  → find groups ≥2 → classify match type → emit DedupMatch results
```

### Match Types
| Class | Detection Method | Confidence | Action |
|-------|-----------------|------------|--------|
| `exact_duplicate` | SHA-256 content hash (event_type + title + summary[:100] + source) | High | Archive older copy |
| `near_duplicate` | Title key (project + normalized title) + same event_type | Medium | Compact if both thin; supersede otherwise |
| `supersession_candidate` | Same workflow_id, different content | Medium | Mark older as superseded |
| `rejected_ambiguous` | Same title key but different event_types | Low | No action, log rejection |
| `protected_skip` | Any protected artifact involved | N/A | Skip with traceable reason |

### Content Hashing
Reuses the same approach as consolidation dedup (Phase 6):
- `content_hash = SHA-256(event_type | title | summary[:100] | source)[:16]`
- `title_key = SHA-256(project | title.lower().strip())[:16]`
- `workflow_key = workflow_id field or extracted from artifact_id pattern`

---

## Merge / Supersession Rules

### Supersession
- The newer artifact sets `supersedes: <old_artifact_id>`
- The older artifact gets `promotion_status: "superseded"` + `superseded_by: <new_id>`
- Both artifacts are retained — supersession is metadata, not deletion
- Supports chained supersession (v3 → v2 → v1) without cycles
- Self-supersession is rejected
- Already-superseded pairs return `no_action`

### Compaction
- Multiple thin artifacts (summary < 20 chars) with same title → merged into single survivor
- Survivor selection: longest summary → highest confidence → newest mtime
- Survivor gets `provenance: "compaction"` + `compacted_from: [source_ids]`
- Source artifacts get `promotion_status: "superseded"` then archived
- Single-item groups return `no_action`
- Protected groups return `protected_skip`

### Exact Dedup
- Older copy marked `promotion_status: "superseded"` + `superseded_by: <newer_id>`
- Older copy archived to `_archive/` directory
- Newer copy kept in place unchanged

---

## Real Workflows Implemented (Step 9.5)

### 1. Workflow Learnings Dedup + Supersession
- **Path**: `_compact_workflow_learnings()` → load all `mem_*.json` → `detect_duplicates()` → process matches
- **Exact duplicates**: archived via `deduplicate_exact()`
- **Supersession candidates** (same workflow_id): processed via `supersede_artifact()`
- **Near-duplicates**: compact if both thin; supersede if not
- **Ambiguous matches**: rejected with reason

### 2. Working Memory Dedup
- **Path**: `_compact_working_memory()` → load all `wm_*.json` → `detect_duplicates()` → exact dedup only
- Working memory only gets exact dedup (no merge/supersession for transient data)

### 3. Group Compaction (callable standalone)
- **Path**: `compact_group(artifacts, archive_dir)` → select survivor → archive rest with provenance
- Used by workflow learnings for thin near-duplicate groups
- Preserves `compacted_from` list on survivor

### 4. Supersession (callable standalone)
- **Path**: `supersede_artifact(surviving_path, superseded_path)` → update metadata on both
- Used for workflow_id-based version chains
- Supports dry-run mode

---

## Provenance Handling

| Operation | Provenance Mechanism |
|-----------|---------------------|
| Exact dedup | `superseded_by` on archived copy; `provenance_links` in result |
| Supersession | `supersedes` on survivor; `superseded_by` + `promotion_status: "superseded"` on older |
| Compaction | `compacted_from: [ids]` + `provenance: "compaction"` on survivor; `superseded_by` on sources |
| All operations | `CompactionResult.provenance_links` lists affected artifact IDs |

Supersession chains are acyclic by construction:
- Self-supersession is rejected
- Already-superseded pairs return no_action
- Chain traversal: newest → supersedes → supersedes → oldest

---

## Bad-Merge Safeguards (Step 9.6)

| Safeguard | Implementation |
|-----------|---------------|
| Different event_types | `rejected_ambiguous` — never merge semantically different artifact types |
| Protected artifacts | `protected_skip` — procedural, operator-authored, active loops, explicit flag |
| Unreadable metadata | `rejected_unsafe` — cannot parse JSON → skip |
| Self-supersession | `rejected_unsafe` — same artifact ID |
| Cross-project match | Different `title_key` due to project prefix |
| Active open loops | `_is_merge_protected()` checks loop status |
| Agent patterns | Path-based protection check |

### Protection Classification
| Condition | Result |
|-----------|--------|
| `metadata["protected"] == true` | Protected (skip) |
| Path contains `agent_patterns` | Protected (procedural layer) |
| Active open loop status | Protected |
| `source == "operator"` | Protected |
| `metadata is None` | Protected (unreadable) |
| Default | Not protected |

---

## Observability (Step 9.7)

### Structured Log Events

| slog Event | Emitted By | Key Fields |
|------------|------------|------------|
| `memory.compaction.sweep` | `run_compaction()` | sweep_id, dry_run, items_examined, duplicates_found, items_compacted, items_superseded, items_protected |
| `memory.compaction.workflow` | Each workflow method | target_store, dry_run, items_examined, duplicates_deduped, items_compacted, items_superseded, items_protected |
| `memory.router.compaction` | `router.run_compaction()` | caller, dry_run, items_examined, duplicates_found, items_compacted, items_superseded |

### Audit in Results
Every `CompactionResult` contains:
- `action`: what happened
- `target_store`: which store
- `surviving_path`: path of kept artifact
- `affected_paths`: paths of modified/archived artifacts
- `rule_name`: which rule fired
- `dry_run`: boolean
- `match_class`: type of match detected
- `provenance_links`: IDs of related artifacts
- `rejection_reason`: why skipped (if applicable)

---

## Tests Run

```bash
# Phase 9 compaction tests
python3 -m pytest tests/test_memory_compactor.py -v
# 51 passed in 0.65s

# Router tests (backward compat)
python3 -m pytest tests/test_memory_router.py -v
# 122 passed

# Full regression (excluding pre-existing heartbeat timeout)
python3 -m pytest tests/ --ignore=tests/test_heartbeat.py
# 3675 passed in 23.90s
```

### Test Classes (51 tests)

| Class | Tests | Validates |
|-------|-------|-----------|
| TestCompactionStructures | 4 | Valid actions, DedupMatch.to_dict(), CompactionResult.to_dict(), SweepSummary.to_dict() |
| TestContentHashing | 7 | Same/different hash, title normalization, project separation, workflow_key extraction |
| TestMergeProtection | 6 | Protected flag, agent patterns, active loops, operator-authored, normal unprotected, None metadata |
| TestExactDuplicateDetection | 3 | Two identical detected, different content no match, single item no match |
| TestNearDuplicateDetection | 2 | Same title different content → near_duplicate, different event_types → rejected_ambiguous |
| TestSupersessionDetection | 1 | Same workflow_id different content → supersession_candidate |
| TestSupersessionMetadata | 5 | Sets metadata correctly, dry-run preserves, protected skips, self-supersession rejected, already-superseded no_action |
| TestExactDedupWorkflow | 3 | Archives older, dry-run preserves, rejects non-exact |
| TestCompactGroup | 4 | Thin pair compacted, keeps richest, single no_action, protected skips |
| TestWorkflowLearningsCompaction | 3 | Exact dedup, supersession candidates, empty store |
| TestWorkingMemoryCompaction | 2 | Exact dedup, different content no dedup |
| TestFullCompactionSweep | 3 | Aggregates correctly, empty no errors, dry_run propagated |
| TestBadMergePrevention | 3 | Different types rejected, protected skipped, unreadable rejected |
| TestRouterCompactionIntegration | 2 | Router delegates correctly, convenience function works |
| TestProvenancePreservation | 3 | Dedup provenance links, compaction source IDs, supersession chain integrity |

---

## Phase 9 Step Completion

| Step | Deliverable | Status |
|------|-------------|--------|
| 9.1 | Compaction/dedup/supersession policy | **Done** — `MEMORY/compaction_dedup_supersession_spec.md` |
| 9.2 | Deduplication/merge matrix | **Done** — `MEMORY/deduplication_merge_matrix.md` |
| 9.3 | Duplicate detection | **Done** — content hash, title key, workflow key |
| 9.4 | Supersession metadata | **Done** — `supersedes`, `superseded_by`, `promotion_status: "superseded"` |
| 9.5 | Real compaction workflows | **Done** — workflow learnings dedup+supersession, working memory dedup, group compaction |
| 9.6 | Bad merge prevention | **Done** — protection checks, event_type mismatch rejection, self-supersession block |
| 9.7 | Observability | **Done** — 3 slog event types, structured CompactionResult, SweepSummary |
| 9.8 | Tests + validation report | **Done** — 51 tests, this report |

---

## Verification Checklist

| Requirement | Status |
|-------------|--------|
| Documented compaction/dedup/supersession policy exists | **PASS** |
| Documented merge matrix exists | **PASS** |
| At least 2–4 real workflows end-to-end | **PASS** — 4 workflows |
| Supersession metadata is explicit and traceable | **PASS** — supersedes, superseded_by, promotion_status |
| Provenance is preserved | **PASS** — compacted_from, provenance_links, archive |
| Unsafe merges are actively prevented | **PASS** — 7 safeguard categories |
| Operations are observable and auditable | **PASS** — slog events + structured results |
| Tests validate Phase 9 behavior | **PASS** — 51 tests |
| No Phase 10+ work pulled in | **PASS** |
| Full regression passes | **PASS** — 3675 passed, 0 failures |

---

## Remaining Gaps

| Gap | Severity | Notes |
|-----|----------|-------|
| Fusion Memory not compactable | By design | Prompt-delegated, no Python API |
| Obsidian vault not compactable | By design | MCP-managed, read-only to compaction |
| No automatic compaction scheduling | Low | Compaction invoked explicitly by callers |
| No semantic similarity detection | By design | Phase 9 uses deterministic field comparison, not embeddings |
| No cross-store dedup resolution | Low | Detection possible but resolution requires manual triage |
| No recall-time dedup filtering | Low | Superseded artifacts still appear in recall (Phase 10+ concern) |
| Heartbeat test timeout | Pre-existing | Unrelated to Phase 9 |
