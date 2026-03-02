---
name: task-execution
description: "Read tasks from TASKS/, execute them, write results to OUTPUT/, and manage task lifecycle (.md -> .inprogress -> .done/.failed)."
activation:
  keywords:
    - task
    - execute
    - dispatch
    - lifecycle
---

# task-execution

## When to use

- A new `.md` file appears in `TASKS/`.
- The watcher or user requests processing of a specific task.
- Re-processing a previously failed task.

## Rules / Safety

- All operations must stay within `~/nova-core`.
- Rename task to `.inprogress` before starting work.
- On success, rename to `.done` and write output to `OUTPUT/`.
- On failure, rename to `.failed` and log the error to `LOGS/`.
- Never delete a task file; only rename it.
- Log every major action to `LOGS/`.

## Workflow

1. List `TASKS/*.md` to find pending work.
2. Parse the task file for instructions.
3. Rename `TASKS/<name>.md` → `TASKS/<name>.inprogress`.
4. Execute the task, delegating to other skills as needed via `/file-ops`, `/shell-ops`, `/git-ops`, `/self-verification`.
5. Write results to `OUTPUT/<name>_<timestamp>.md`.
6. Rename `TASKS/<name>.inprogress` → `TASKS/<name>.done`.
7. Log summary to `LOGS/task_<name>.log`.

## Output format

```
OUTPUT/<task_stem>_<YYYYMMDD_HHMMSS>.md
```

Contents: task title, status (success/failure), summary of actions taken, any artifacts produced.
