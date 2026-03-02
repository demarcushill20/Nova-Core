# task-execution Reference

Detailed rules, lifecycle semantics, and operational guidelines for the task-execution skill.

## Task Lifecycle States

```
.md  ──→  .inprogress  ──→  .done
                         └──→  .failed
                         └──→  .cancelled
                         └──→  .skip
```

| State | Meaning |
|---|---|
| `.md` | Pending — queued and waiting for pickup |
| `.inprogress` | Claimed — a worker is actively executing |
| `.done` | Completed successfully — output written |
| `.failed` | Execution failed — error logged |
| `.cancelled` | Manually cancelled by user or supervisor |
| `.skip` | Deliberately skipped (e.g., duplicate, superseded) |

Transitions are **one-way**. A `.done` task is never moved back to `.md`. To retry, create a new task file or manually rename `.failed` back to `.md`.

## Atomic Rename Philosophy

State transitions use filesystem renames (`os.rename` / `Path.rename`), not copy-then-delete. This ensures:

- **Atomicity** — the transition either completes fully or not at all. No partial states.
- **No data loss** — the task file content is preserved across all transitions.
- **Crash safety** — if the process dies mid-operation, the file is in exactly one state.

The rename operation is the **commit point** of each state transition. Everything before it is preparation; everything after is cleanup.

## Crash Recovery

When the watcher restarts, it scans for orphaned `.inprogress` files:

- An `.inprogress` file with no running worker indicates a crash during execution.
- The watcher logs the orphan and leaves it for manual triage.
- To retry: rename `.inprogress` back to `.md`. The watcher will pick it up on the next cycle.
- To abandon: rename to `.failed` and log the reason.

The watcher does **not** automatically retry orphaned tasks — this prevents infinite retry loops on tasks that crash the worker.

## Output File Naming Convention

```
OUTPUT/<task_stem>_<YYYYMMDD_HHMMSS>.md
```

Examples:
- `OUTPUT/0008_deploy_fix_20260302_143012.md`
- `OUTPUT/0009_log_triage_20260302_150045.md`

Rules:
- Timestamp is UTC, generated at the moment the output is written.
- The task stem is the filename without extension (e.g., `0008_deploy_fix` from `0008_deploy_fix.md`).
- If a file with the same name already exists, append a counter: `_2`, `_3`, etc.

## Output File Structure

```markdown
# Task: <task_stem>
**Status:** success | failure
**Executed:** <UTC timestamp>

## Summary
<what was accomplished or why it failed>

## Actions Taken
- <action 1>
- <action 2>

## Artifacts
- <path to any created files>

## CONTRACT
summary: <one-line>
task_id: <stem>
status: <done | failed>
verification: <confirmation method>
```

## Idempotency Discipline

- Reading a task file is always safe to repeat.
- The `.inprogress` rename is the idempotency boundary — once renamed, the task is claimed.
- If a task's work is naturally idempotent (e.g., writing a config file), re-execution on retry is safe.
- If a task's work is **not** idempotent (e.g., sending a message, creating a resource), the task should check for prior completion before re-executing.
- Output files are append-only by convention — a retry writes a new output file with a new timestamp, not overwriting the previous one.

## Never-Deletion Policy

Task files are **never deleted** from `TASKS/`. They accumulate as an audit trail:

- `.done` files prove work was completed.
- `.failed` files prove failure was recorded.
- `.cancelled` and `.skip` files prove deliberate decisions.

Cleanup of old task files (if disk space becomes a concern) is a manual operator decision, never automated.

## Task File Format

A valid task file is a Markdown document in `TASKS/` with a `.md` extension. The minimum requirement is:

```markdown
# Task Title

<instructions for the worker>
```

Optional frontmatter is supported but not required:

```markdown
---
priority: high
tags: [deploy, hotfix]
---

# Deploy Hotfix

<instructions>
```

If the file cannot be parsed as meaningful instructions, it should be marked `.failed` with a log entry explaining the parse error.
