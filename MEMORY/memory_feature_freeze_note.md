# Memory Feature Freeze

**Status**: ACTIVE
**Effective**: 2026-03-13
**Expires**: When Phase 1 (Unified Memory Router) is complete and validated.

---

## What is frozen

Until routing and trigger discipline are stabilized, do NOT add:

1. **New memory stores** — no new directories, databases, or persistence targets
2. **New memory skills** — no new .claude/skills/ for memory operations
3. **New promotion types** — no new pathways for memory escalation between stores
4. **New reflection agents** — no automated memory consolidation or pattern extraction jobs
5. **New memory event types** — no new categories in Fusion Memory metadata

## What is NOT frozen

The following are permitted during the freeze:

- **Bug fixes** to existing memory code paths
- **Schema fixes** (vault frontmatter validation gaps — this is Phase 0 work)
- **Documentation** of existing memory behavior
- **Read-path improvements** (better retrieval scoring, context injection bounds)
- **Validation hardening** on existing write paths
- **Test coverage** for existing memory code

## Why this freeze exists

The current memory system has three architectural gaps that must be resolved before
any new memory features are safe to add:

1. **No automatic triggering** — checkpoints do not auto-generate diary entries,
   session ends do not auto-promote patterns, no periodic ADR surfacing
2. **Vault schema violations** — implementation-plan notes missing field validation,
   plan-tracker skill expects fields the MCP server doesn't enforce
3. **Unified recall not wired** — agents call nova-memory and nova-vault directly
   instead of going through a unified router

Adding new memory features on top of these gaps risks:
- Writing to the wrong store
- Bypassing validation
- Creating duplicate or conflicting memories
- Making the eventual router migration harder

## Enforcement

This freeze is advisory. There is no automated enforcement mechanism.
All memory-related PRs and task implementations should reference this note
and justify any new memory work against the freeze criteria.

## Exit criteria

This freeze lifts when:
- [ ] Phase 0 (Foundation) is complete — all deliverables verified
- [ ] Phase 1 (Unified Memory Router) spec exists and is approved
- [ ] At least one write path has been migrated to the router
- [ ] Direct memory calls are identified and migration plan exists
