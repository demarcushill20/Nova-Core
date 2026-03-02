---
name: task-execution
description: "Execute queued TASKS/*.md files, manage lifecycle transitions, and write results to OUTPUT/ while preserving audit integrity."
activation:
  keywords:
    - task
    - execute
    - dispatch
    - lifecycle
    - TASKS/
    - .inprogress
    - .done
    - .failed
  when:
    - New TASKS/*.md file detected
    - Explicit task execution requested
    - Queued task requires processing
tool_doctrine:
  runtime:
    workflow:
      - read_task_before_execute
      - atomic_state_transition
      - write_output_before_done
      - never_delete_task_files
output_contract:
  required:
    - summary
    - task_id
    - status
    - verification
---

# When To Use

- A new `.md` file appears in `TASKS/`.
- The watcher or user requests processing of a specific task.
- Re-processing a previously failed task.
- A task needs lifecycle management (state transitions, output writing).

# Workflow

1. **Discover** — scan `TASKS/*.md` for pending work.
2. **Read** — parse the task file for instructions before doing anything else.
3. **Claim** — atomically rename `TASKS/<name>.md` to `TASKS/<name>.inprogress`.
4. **Execute** — perform the work, delegating to other skills as needed (`file-ops`, `shell-ops`, `git-ops`, `self-verification`).
5. **Write output** — write results to `OUTPUT/<name>_<YYYYMMDD_HHMMSS>.md` before marking done.
6. **Complete** — rename `TASKS/<name>.inprogress` to `TASKS/<name>.done`.
7. **Log** — write summary to `LOGS/task_<name>.log`.

For lifecycle details, crash recovery, and naming conventions, see `reference.md`.

# Tool Usage Rules

- All operations must stay within `~/nova-core`.
- Always read the task file before executing — never act on filename alone.
- State transitions must be atomic renames, not copy-then-delete.
- Write the output file **before** renaming to `.done` — ensures output exists if the process crashes after rename.
- Never delete a task file. Only rename between lifecycle states.
- Never skip the `.inprogress` state — it signals to the watcher that the task is claimed.
- Log every major action to `LOGS/`.

# Verification

After every task execution:

1. Confirm the task file is in the correct terminal state (`.done` or `.failed`).
2. Confirm the output file exists in `OUTPUT/` with non-zero size.
3. Confirm the log file exists in `LOGS/` with execution details.
4. Verify no `.md` file with the same stem remains in `TASKS/` (would indicate a failed rename).

# Failure Handling

- **Task execution fails**: rename to `TASKS/<name>.failed`, log the error to `LOGS/`, and write a failure report to `OUTPUT/`.
- **Invalid task format**: rename to `.failed` with a log entry explaining the parse error. Do not attempt execution.
- **Crash during execution**: on restart, the watcher detects orphaned `.inprogress` files. These can be retried or manually triaged.
- **Output write fails**: keep the task as `.inprogress` (do not mark `.done`). Log the write failure.
- **Duplicate task stem**: append a counter or timestamp to avoid collision. Never overwrite existing output.

# Output Contract

Every task-execution must end with a machine-checkable contract:

```
## CONTRACT
summary: <one-line description of what was done>
task_id: <task stem, e.g., 0008_deploy_fix>
status: <done | failed>
verification: <how correctness was confirmed>
```

See `examples.md` for concrete instances.
