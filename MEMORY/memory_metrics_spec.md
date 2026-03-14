# Memory Metrics Specification (Phase 10, Step 10.1)

## Purpose

This document defines the core metric set for Nova-Core memory health,
observability, and operational trust. Each metric is grounded in real
data sources from Phases 1–9.

---

## Metric Catalog

### 1. candidate_count
- **Definition**: Total CanonicalMemoryObject instances evaluated by the router
- **Source**: Count of `memory.router.store` slog events
- **Computation**: Count all store events in structured log
- **Type**: Snapshot (cumulative from log)
- **Blind spots**: Prompt-delegated writes to Fusion Memory are not counted
- **Why it matters**: Shows overall memory ingestion volume

### 2. store_count_by_layer
- **Definition**: Number of persisted artifacts per memory layer
- **Source**: File system scan of MEMORY/ and STATE/ directories
- **Computation**: Glob + classify by path (working=STATE/working_memory, episodic=MEMORY/workflow_learnings, procedural=MEMORY/agent_patterns)
- **Type**: Snapshot
- **Blind spots**: Fusion Memory and Obsidian vault items not countable from Python
- **Why it matters**: Shows layer distribution health

### 3. rejection_rate
- **Definition**: Fraction of store() calls that resulted in stored=False
- **Source**: `memory.router.store` slog events where stored=false, or StoreResult.stored field
- **Computation**: rejections / total_store_calls
- **Type**: Derived from log
- **Blind spots**: Rejections during ingest_event before store() not counted
- **Why it matters**: High rejection rate may indicate noisy input or overly strict evaluation

### 4. duplicate_rate
- **Definition**: Fraction of artifacts flagged as exact or near-duplicates
- **Source**: CompactionSweepSummary.duplicates_found / items_examined
- **Computation**: Run compaction sweep in dry-run mode
- **Type**: Snapshot (on-demand)
- **Blind spots**: Cross-store duplicates (Fusion Memory) not detectable
- **Why it matters**: Rising duplicates indicate dedup pipeline gaps

### 5. promotion_rate
- **Definition**: Fraction of candidates marked promotion-eligible
- **Source**: Count of store events with promotion_eligibility="eligible"
- **Computation**: eligible_count / total_store_calls
- **Type**: Derived from log
- **Blind spots**: Actual promotion to Obsidian requires MCP; only eligibility tracked
- **Why it matters**: Too high = noisy promotion; too low = knowledge stagnation

### 6. recall_success_rate
- **Definition**: Fraction of recall() calls that returned ≥1 result
- **Source**: `memory.router.recall` slog events
- **Computation**: events_with_results / total_recall_calls
- **Type**: Derived from log
- **Blind spots**: Result quality not measurable without human evaluation
- **Why it matters**: Low success rate indicates recall pipeline degradation

### 7. stale_recall_rate
- **Definition**: Fraction of recall results that are stale (>30 days old)
- **Source**: Recall results with old timestamps
- **Computation**: Requires timestamp inspection on recall results
- **Type**: Derived (expensive; sample-based)
- **Blind spots**: Not all recall results include age metadata
- **Why it matters**: High stale rate indicates governance not running often enough

### 8. average_recall_latency
- **Definition**: Mean time for recall() to complete
- **Source**: Not currently instrumented (slog timestamps only mark event time)
- **Computation**: Would require start/end timing in recall()
- **Type**: Not yet available
- **Blind spots**: Full blind spot — timing not instrumented in current code
- **Why it matters**: Latency regression indicates store scaling issues

### 9. open_loops_created
- **Definition**: Count of loops created via track_open_loop() or detect_open_loops()
- **Source**: `memory.router.track_open_loop` + `memory.router.detect_open_loop` slog events where loop_action="loop_created"
- **Computation**: Count matching events
- **Type**: Snapshot (cumulative)
- **Blind spots**: None for file-backed loops
- **Why it matters**: Shows how much unresolved work is being captured

### 10. open_loops_closed
- **Definition**: Count of loops resolved or rejected
- **Source**: `memory.router.resolve_open_loop` slog events
- **Computation**: Count events where loop_action contains "resolved" or "rejected"
- **Type**: Snapshot (cumulative)
- **Blind spots**: Manual file edits not tracked
- **Why it matters**: Low close rate indicates growing unresolved work

### 11. diary_generation_success_rate
- **Definition**: Fraction of generate_diary() calls that produced output
- **Source**: `memory.router.summarize_session` slog events (diary is alias)
- **Computation**: events_with_status="ok" / total_calls
- **Type**: Derived from log
- **Blind spots**: generate_diary delegates to summarize_session; no separate event
- **Why it matters**: Diary failure indicates consolidation pipeline issues

### 12. adr_candidate_count
- **Definition**: Number of pattern candidates generated with promotion_eligible=True
- **Source**: `memory.router.extract_patterns` slog events
- **Computation**: Count events where candidates_found > 0
- **Type**: Snapshot (cumulative)
- **Blind spots**: Actual ADR creation requires operator triage in Obsidian
- **Why it matters**: Shows whether the system is detecting reusable patterns

### 13. schema_validation_failures
- **Definition**: Count of CanonicalMemoryObject validation failures
- **Source**: `memory.router.store` slog events where stored=false and validation_errors present
- **Computation**: Count events with validation failures
- **Type**: Snapshot (cumulative)
- **Blind spots**: Validation in memory_engine (separate path) not logged to slog
- **Why it matters**: Rising failures indicate upstream schema drift

### 14. governance_action_rate
- **Definition**: Fraction of examined artifacts where governance took action
- **Source**: SweepSummary.items_acted_on / items_examined
- **Computation**: Run governance sweep
- **Type**: Snapshot (on-demand)
- **Blind spots**: Only file-backed stores; Fusion Memory/Obsidian excluded
- **Why it matters**: Shows whether governance is keeping stores bounded

### 15. compaction_efficiency
- **Definition**: Ratio of compacted/superseded items to examined items
- **Source**: CompactionSweepSummary
- **Computation**: (items_compacted + items_superseded) / items_examined
- **Type**: Snapshot (on-demand)
- **Blind spots**: Only MEMORY/ and STATE/working_memory/
- **Why it matters**: Shows whether memory clutter is being reduced

---

## Metrics Not Yet Measurable

| Metric | Reason | Path to Enable |
|--------|--------|---------------|
| Recall latency | No timing instrumentation in recall() | Add start/end timing wrapper |
| Fusion Memory counts | Prompt-delegated, no Python API | Requires MCP query tool |
| Obsidian vault counts | MCP-managed | Use vault_list MCP if available |
| Cross-store duplicates | Detection only, no resolution path | Manual triage |
| Recall quality (semantic) | No relevance feedback loop | Requires human evaluation |
