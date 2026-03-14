# Memory Governance Policy (Phase 8, Step 8.1)

## Purpose

This document defines how Nova-Core manages the lifecycle of its memory
artifacts — when to retain, compact, archive, mark stale, prune, or protect
memory objects across all file-backed stores.

## Definitions

- **Governance**: Explicit rules controlling the lifecycle of memory artifacts
  from creation to deletion or archival.
- **Hygiene**: Bounded, deterministic operations that enforce governance rules
  on file-backed stores.
- **Pruning**: Permanent removal of artifacts that meet documented criteria.
- **Compaction**: Replacing multiple thin artifacts with a single summary.
- **Archival**: Moving artifacts out of active stores into an archive directory.
- **Stale marking**: Flagging artifacts as stale without deleting them.
- **Protection**: Preventing artifacts from being auto-pruned or compacted.
- **Dry-run**: Evaluating governance rules and reporting what *would* happen
  without making any changes.

## Core Principles

1. **Explicit over implicit** — Every governance action must state which rule
   fired and why.
2. **Conservative destruction** — Pruning requires strong evidence. When in
   doubt, archive rather than delete.
3. **Dry-run first** — All destructive operations must support dry-run mode.
   Production governance runs should start with dry-run.
4. **Protected memory is sacred** — High-value artifacts (procedural, ADRs,
   active open loops, operator-authored) must never be auto-pruned.
5. **Audit everything** — Every governance action emits a structured log event
   with target, rule, action, and outcome.
6. **Layer-aware** — Governance rules respect the 4-layer model. Working-layer
   artifacts have short retention; procedural-layer artifacts are permanent.
7. **No silent deletion** — Every deletion must be traceable to a specific rule
   and logged before execution.
8. **Reversibility where practical** — Archive before prune when possible.
   Archival moves files to a recoverable location rather than deleting them.

## What Must Never Be Auto-Pruned

| Category | Examples | Reason |
|----------|----------|--------|
| Procedural memory | ADRs, agent patterns, debugging guides | Highest-value reusable knowledge |
| Semantic memory | Verified findings, stable facts | Consolidated truth |
| Active open loops | status in {proposed, open, blocked, deferred} | Represents live unresolved work |
| Operator-authored artifacts | Any artifact with source="operator" | Human judgment, not auto-generated |
| Recent high-value checkpoints | Checkpoints < 7 days old with importance ≥ 0.7 | Active operational context |

## What May Be Pruned

| Category | Conditions | Rule |
|----------|------------|------|
| Working memory files | Age > 7 days | Already enforced by WorkingMemoryAdapter |
| Old session files | Age > 30 days | Working-layer transient state |
| Resolved/rejected open loops | Terminal status + age > 30 days | No longer actionable |
| Stale open loops | Stale status + age > 60 days | Explicitly abandoned |
| Rotated log files | Age > 30 days | Operational logs, not memory |

## What May Be Archived (Not Deleted)

| Category | Conditions | Rule |
|----------|------------|------|
| Old episodic artifacts | Age > 90 days + low importance | Move to archive, keep index |
| Large STATE operational files | Age > 30 days + non-critical | Move to STATE/_archive/ |

## What May Be Compacted

| Category | Conditions | Rule |
|----------|------------|------|
| Thin working memory | Multiple items same event_type + thin summaries | Replace with single summary |
| Duplicate episodic artifacts | Same dedupe key within store | Keep newest, archive older |

## Stale Marking Policy

| Target | Stale Threshold | Action After Stale |
|--------|----------------|-------------------|
| Open loops | 14 days since last update (existing) | Stale status, eligible for prune after 60 days |
| Working memory | 7 days (existing auto-cleanup) | Pruned by WorkingMemoryAdapter |
| Session files | 30 days since creation | Eligible for archival |
| Episodic artifacts | Not stale-eligible | Permanent by layer contract |

## Dry-Run Behavior

When `dry_run=True`:
- All rules are evaluated normally
- Actions are computed and returned in results
- No file modifications occur
- Results include `dry_run: true` flag
- Log events include `dry_run: true`

## Confidence Requirements for Hygiene Actions

| Action | Min Confidence |
|--------|---------------|
| Mark stale | Any (rule-based, not confidence-dependent) |
| Archive | Low (conservative — moves, doesn't delete) |
| Compact | Medium (must verify thin/duplicate status) |
| Prune | High (must meet all rule criteria explicitly) |

## Operator Override

Operators can:
- Add `protected: true` to any artifact's JSON metadata to prevent governance
- Remove stale marks by updating status directly
- Override retention rules per-artifact

Governance code must check for `protected` field before any destructive action.

## Logging Requirements

Every governance action must emit a structured log event containing:
- `governance_action`: type of action taken
- `target_store`: which store was affected
- `target_path`: specific file or object
- `rule`: which retention rule fired
- `dry_run`: boolean
- `protection_status`: whether item was protected
- `outcome`: what happened
- `items_examined`: count of items reviewed
- `items_acted_on`: count of items changed
- `rejection_reason`: if skipped, why

## Recovery

- Archived files are moved to `STATE/_archive/` or `MEMORY/_archive/` with
  original path preserved in filename.
- Archive directories are never auto-cleaned.
- If an archive needs cleanup, it requires explicit operator action.
