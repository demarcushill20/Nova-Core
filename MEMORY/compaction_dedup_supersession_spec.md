# Compaction, Deduplication & Supersession Specification (Phase 9, Step 9.1)

## Purpose

This document defines when and how Nova-Core detects duplicate memories,
compacts repeated artifacts, and marks older items as superseded — without
losing useful signal or corrupting high-value knowledge.

## Definitions

- **Exact duplicate**: Two artifacts with identical content hash
  (event_type + title + summary[:100] + source). Byte-level equivalence.
- **Near-duplicate**: Two artifacts with the same dedupe key
  (project + title normalized) or high field overlap but not byte-identical.
- **Merge candidate**: Two or more near-duplicates where the newer artifact
  clearly supersedes the older, and a single surviving artifact captures
  all useful signal.
- **Supersession candidate**: An artifact that should be marked as replaced
  by a newer version, while both records are retained for provenance.
- **Compaction**: Replacing N thin/redundant artifacts with a single
  summary that preserves provenance links.

## What Counts as an Exact Duplicate

Two artifacts are exact duplicates when:
- `_content_hash(a) == _content_hash(b)` where content hash =
  SHA-256 of `(event_type + title + summary[:100] + source)`
- They reside in the same store (cross-store matches are near-duplicates)

Action: Keep the **newest** (by timestamp or file mtime). Archive or remove
the older copy. No merge needed — content is identical.

## What Counts as a Near-Duplicate

Two artifacts are near-duplicates when:
- Same normalized title (lowercased, stripped) AND same project, OR
- Same workflow_id (for MemoryArtifact files), OR
- Same content hash across different stores

Near-duplicates require additional evidence before acting:
- If both have high importance (≥ 0.5): flag as **supersession candidate**
- If one is thin (summary < 20 chars): the thin copy is compaction-eligible
- If they differ in meaningful content: **rejected_ambiguous** — do not merge

## What Qualifies for Compaction

Compaction applies to groups of low-value, repetitive artifacts:
- Multiple thin artifacts (summary < 20 chars) of the same event_type
- Repeated session/plan summaries for the same workflow_id
- Duplicate working-memory entries missed by adapter cleanup
- Repeated notification/transient artifacts

Compaction produces a single surviving artifact that:
- Has `provenance: "compaction"`
- Includes `compacted_from: [list of source artifact IDs]`
- Preserves the best title, longest summary, highest confidence
- Archives the source artifacts (does not delete them)

## What Qualifies for Supersession

Supersession applies when a newer artifact explicitly replaces an older one:
- Plan revision supersedes prior plan version (same workflow_id)
- Corrected decision supersedes prior decision
- Updated session summary supersedes earlier summary for same session
- Promoted pattern supersedes the episodic evidence it was promoted from

The surviving (newer) artifact:
- Sets `supersedes: <old_memory_id>` pointing to the artifact it replaces
- The superseded artifact gets `promotion_status: "superseded"`
- Both artifacts are retained — supersession is metadata, not deletion

## What Must Never Be Auto-Merged

| Category | Reason |
|----------|--------|
| Procedural memory (ADRs, agent patterns) | Highest-value; operator-managed |
| Semantic memory (verified findings) | Consolidated truth; merge requires operator |
| Active open loops | Live unresolved work; may have distinct contexts |
| Operator-authored artifacts (source="operator") | Human judgment |
| Artifacts with `protected: true` | Explicit protection flag |
| Artifacts across different projects | May represent distinct work |
| Artifacts with different event_types | Different semantic meaning |

## Provenance Requirements

Every compaction or supersession must produce:
1. `provenance: "compaction"` or `provenance: "consolidation"` on survivor
2. `compacted_from: [...]` list of source artifact IDs (compaction)
3. `supersedes: <memory_id>` on the surviving artifact (supersession)
4. `promotion_status: "superseded"` on the replaced artifact
5. Structured log event with all parties identified

## Rollback Safety

- Compacted source artifacts are **archived**, not deleted
- Superseded artifacts retain full content with status change only
- Archive directory: `MEMORY/_archive/` or `STATE/_archive/`
- No compaction or supersession is irreversible at the file level

## Confidence Requirements

| Action | Min Confidence |
|--------|---------------|
| Exact duplicate removal | Any (deterministic hash match) |
| Near-duplicate flagging | Any (deterministic field match) |
| Compaction | Medium (verified thin/duplicate status) |
| Supersession | Medium (clear version relationship) |
| Cross-store merge | High (must verify identity across stores) |

## Rejection Conditions

| Condition | Result |
|-----------|--------|
| Protected artifact involved | `protected_skip` |
| Different event_types | `rejected_ambiguous` |
| Different projects | `rejected_ambiguous` |
| Both high importance, content differs | `rejected_ambiguous` |
| Active open loop | `protected_skip` |
| Semantic or procedural layer | `protected_skip` |
| Ambiguous title match | `rejected_ambiguous` |

## Dry-Run Support

All compaction/dedup/supersession operations support `dry_run=True`:
- Rules are evaluated, matches are found, actions are computed
- No files are modified
- Results include full action plan for review
