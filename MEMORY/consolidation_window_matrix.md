# Consolidation Window Matrix

Phase 6 deliverable — defines supported consolidation windows, their inputs,
outputs, and constraints.

Generated: 2026-03-13

---

## Window Types

### session
- **Purpose**: Consolidate a watcher session (task batch) into an episodic summary
- **Input source**: SessionManager task records, optional working memory entries
- **Time bounds**: Session lifetime (typically 2 hours max)
- **Input layer**: working + episodic (task records)
- **Output type**: Session summary artifact
- **Output layer**: episodic
- **Min items**: 1 task
- **Promotion candidates**: No (session summaries are episodic records)
- **Example**: "Session ses_123: 3 completed, 0 failed. [completed] task_1: Implemented vault validation; [completed] task_2: Wrote tests"
- **Anti-example**: A session with only heartbeat events (blocked by heartbeat_noise check)
- **Limitations**: Cannot access archived sessions from disk automatically (requires SessionManager)

### working_memory
- **Purpose**: Compress recent working-memory entries into a checkpoint
- **Input source**: STATE/working_memory/ via WorkingMemoryAdapter.recall()
- **Time bounds**: Recent entries (7-day retention window)
- **Input layer**: working
- **Output type**: Working memory checkpoint
- **Output layer**: working
- **Min items**: 2 unique items
- **Promotion candidates**: No (working layer cannot promote directly)
- **Example**: "Working memory checkpoint: 8 events (heartbeat_cycle=5, session_end=2, task_completed=1)"
- **Anti-example**: 5 identical heartbeat entries (deduped to 1, rejected as too small)
- **Limitations**: Chronological scan only, no keyword filtering

### task_batch
- **Purpose**: Consolidate related task completions into a batch summary
- **Input source**: MEMORY/workflow_learnings/ via MemoryFileAdapter.recall()
- **Time bounds**: Recent artifacts (typically last 7 days)
- **Input layer**: episodic
- **Output type**: Task batch summary
- **Output layer**: episodic
- **Min items**: 2 items, at least 1 completed
- **Promotion candidates**: Yes, if ≥3 high-confidence completions
- **Example**: "Task batch: 4 tasks completed. Implemented vault validation; Wrote comprehensive tests; Created documentation; Fixed edge case"
- **Anti-example**: 3 heartbeat events in a task_batch window (no completed tasks → rejected)
- **Limitations**: Keyword-based retrieval, not semantic; task_class matching depends on caller providing keywords

### failure_window
- **Purpose**: Consolidate repeated failures into an incident summary
- **Input source**: MEMORY/workflow_learnings/ (task_failed events)
- **Time bounds**: Recent artifacts
- **Input layer**: episodic
- **Output type**: Failure incident summary
- **Output layer**: episodic
- **Min items**: 2 failures
- **Promotion candidates**: No (failures are records, not patterns)
- **Example**: "Failure pattern: 3 failures detected. Connection timeout to API; API endpoint unreachable; Service returned 503"
- **Anti-example**: Single failure (rejected — not a pattern)
- **Limitations**: Cannot distinguish related vs. unrelated failures beyond event_type grouping

### heartbeat
- **Purpose**: Consolidate heartbeat cycles into an operational health summary
- **Input source**: STATE/working_memory/ (heartbeat_cycle events)
- **Time bounds**: Recent entries
- **Input layer**: working
- **Output type**: Heartbeat operational summary
- **Output layer**: working
- **Min items**: 3 unique cycles
- **Promotion candidates**: No
- **Example**: "Heartbeat window: 12 cycles, 12 healthy, 0 unhealthy. Status: HEALTHY."
- **Anti-example**: 3 identical heartbeat entries (deduped to 1 → rejected; or uniform_thin → anti-amplification blocked)
- **Limitations**: Health assessment is keyword-based (looks for HEALTHY/UNHEALTHY in title/summary)

### plan_execution
- **Purpose**: Consolidate plan step outcomes into a plan summary
- **Input source**: MEMORY/workflow_learnings/ or direct input
- **Time bounds**: Plan execution period
- **Input layer**: episodic
- **Output type**: Plan execution summary
- **Output layer**: episodic
- **Min items**: 1 plan step
- **Promotion candidates**: Yes, if high confidence and ≥3 steps
- **Example**: "Plan execution summary (plan_001): 5 steps. Step 1 completed; Step 2 done; ..."
- **Anti-example**: Empty plan with no executed steps
- **Limitations**: plan_id extracted from first item only

---

## Minimum Window Sizes

| Window Type | Min Items | After Dedupe |
|------------|-----------|-------------|
| session | 1 | 1 |
| working_memory | 2 | 2 |
| task_batch | 2 | 2 |
| failure_window | 2 | 2 |
| heartbeat | 3 | 3 |
| plan_execution | 1 | 1 |

---

## Anti-Amplification Matrix

| Check | session | working_memory | task_batch | failure_window | heartbeat | plan_execution |
|-------|---------|---------------|------------|---------------|-----------|---------------|
| Heartbeat noise (>80%) | BLOCKS | BLOCKS | BLOCKS | BLOCKS | skipped | BLOCKS |
| All low confidence | BLOCKS | BLOCKS | BLOCKS | BLOCKS | BLOCKS | BLOCKS |
| Uniform thin content | BLOCKS | BLOCKS | BLOCKS | BLOCKS | BLOCKS | BLOCKS |

---

## Output Layer Mapping

| Window Type | Output Layer | Why |
|------------|-------------|-----|
| session | episodic | Session records are episodic by nature |
| working_memory | working | Checkpoint stays in working layer |
| task_batch | episodic | Task outcomes are episodic records |
| failure_window | episodic | Failure incidents are episodic |
| heartbeat | working | Operational state is transient |
| plan_execution | episodic | Plan outcomes are episodic records |
