# Memory Health Dashboard

Generated: 2026-03-13T15:17:23Z

---

## Layer Distribution

| Layer | Count |
|-------|-------|
| Working | 11 |
| Episodic | 30 |
| Semantic | 0 (not measurable from file system) |
| Procedural | 0 |
| **Total (file-backed)** | **41** |

## Store File Counts

| Store | Count |
|-------|-------|
| agent_patterns | 0 |
| open_loops | 1 |
| sessions | 8 |
| workflow_learnings | 30 |
| working_memory | 11 |

## Open Loops

| Status | Count |
|--------|-------|
| Proposed | 0 |
| Open | 1 |
| Blocked | 0 |
| Deferred | 0 |
| Stale | 0 |
| Resolved | 0 |
| Closed/Rejected | 0 |
| **Active** | **1** |
| **Terminal** | **0** |

## Event Counters (from structured log)

| Metric | Value |
|--------|-------|
| Store calls | 1122 |
| Store successes | 722 |
| Store rejections | 62 |
| **Rejection rate** | **5.5%** |
| Recall calls | 385 |
| Recall with results | 194 |
| **Recall success rate** | **50.4%** |
| Promotion eligible | 235 |
| **Promotion rate** | **20.9%** |
| Consolidation calls | 59 |
| Consolidation successes | 0 |
| Governance sweeps | 51 |
| Compaction sweeps | 39 |
| Open loops created | 67 |
| Open loops resolved | 11 |
| Schema validation failures | 75 |

## Operational Health

| Metric | Value |
|--------|-------|
| Structured log size | 2,633,761 bytes |
| Session files | 8 |
| Notification files | 425 |

## Known Blind Spots

- Fusion Memory items not countable (prompt-delegated)
- Obsidian vault items not countable (MCP-managed)
- Recall latency not instrumented
- Semantic layer count unavailable from file system
