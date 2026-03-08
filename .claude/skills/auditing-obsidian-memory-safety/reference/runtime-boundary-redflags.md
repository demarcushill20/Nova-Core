# Runtime-Boundary Red Flags

## Purpose

This checklist defines content patterns that indicate runtime/operational state being stored in Obsidian. Obsidian stores durable guidance and reusable memory, not live orchestration truth. Any proposed vault write containing these patterns should be rejected or flagged for human review.

## Red Flags — Reject or Flag

### Live task/execution state

| Pattern | Why it's wrong | Belongs in |
|---------|---------------|------------|
| `status: running` / `status: queued` / `status: pending` | Live execution state changes constantly | `STATE/running/`, `TASKS/*.inprogress` |
| `pid: <number>` | Process IDs are ephemeral | `STATE/running/<stem>.pid` |
| `started_at: <recent-timestamp>` as a live value | Tracks current execution, not historical | `LOGS/worker_<stem>.log` |
| `last_heartbeat: <timestamp>` | Ephemeral liveness signal | `HEARTBEAT_MULTIAGENT.md` |

### Workflow control data

| Pattern | Why it's wrong | Belongs in |
|---------|---------------|------------|
| `next_step: "<step>"` | Transient execution routing | Orchestrator in-memory state |
| `retry_count: <n>` | Transient retry tracking | `LOGS/` or in-memory |
| `current_phase: "<phase>"` | Live progress indicator | `STATE/` or `LOGS/` |
| `blocked_by: "<live-task>"` | Active dependency tracking | Task file metadata |
| `waiting_for: "<resource>"` | Live wait state | Orchestrator state |

### Budget and resource tracking

| Pattern | Why it's wrong | Belongs in |
|---------|---------------|------------|
| `approved_budget: <amount>` | Operational approval state | Runtime config or STATE/ |
| `remaining_credits: <n>` | Live counter | Runtime state |
| `lease_expiry: <timestamp>` | Ephemeral resource lease | Runtime state |
| `rate_limit_remaining: <n>` | Live API state | In-memory or LOGS/ |

### Execution locks and queues

| Pattern | Why it's wrong | Belongs in |
|---------|---------------|------------|
| `lock_holder: "<agent>"` | Ephemeral mutual exclusion | `STATE/locks/` |
| `queue_position: <n>` | Transient ordering | Task queue state |
| `assigned_to: "<worker>"` | Live work assignment | Task file or STATE/ |

### Ephemeral file references

| Pattern | Why it's wrong | Belongs in |
|---------|---------------|------------|
| `/tmp/` paths | Temporary files do not persist | Reference the output, not the temp path |
| `STATE/running/<stem>.pid` | Active process tracking | Becomes stale immediately |
| `TASKS/<stem>.inprogress` | Active task state | Reference the completed task instead |

## Acceptable Content

These are NOT red flags and should not trigger rejection:

| Content | Why it's fine |
|---------|---------------|
| `date: "2026-03-08"` | Historical date, not live state |
| `date_created: "2026-03-08"` | Creation timestamp (static) |
| `workflow_id: "session_33"` | Reference to completed workflow |
| `verification_outcome: "approved"` | Final outcome, not live status |
| `confidence: "high"` | Static assessment |
| `[[related-note-title]]` | Vault cross-reference |
| `task_class: "research"` | Classification, not live state |
| `OUTPUT/0042_result.md` | Reference to completed output |
| `TASKS/0042_task.md` | Reference to task definition (not .inprogress) |

## Decision Rule

- If ANY red flag is found in frontmatter → **reject**
- If a red flag is found only in body text as a quoted example or documentation → **warn** (may be acceptable if clearly illustrative)
- If red flags are found in body text as live references → **reject**
- When uncertain → **flag for human review**
