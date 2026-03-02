# task-execution Examples

## Example 1: Successful Task Execution

**Scenario:** Task `0010_generate_report.md` requests generating a status report.

**Workflow:**

1. Watcher detects `TASKS/0010_generate_report.md`.
2. Read the task file — instructions say "Generate a system status report."
3. Rename to `TASKS/0010_generate_report.inprogress`.
4. Execute: gather system info, write report content.
5. Write output to `OUTPUT/0010_generate_report_20260302_143012.md`.
6. Rename to `TASKS/0010_generate_report.done`.
7. Log summary to `LOGS/task_0010_generate_report.log`.

**Output Contract:**

```
## CONTRACT
summary: Generated system status report with disk, memory, and service data
task_id: 0010_generate_report
status: done
verification: Output file exists in OUTPUT/ with non-zero size; task file is .done
```

---

## Example 2: Task Failure

**Scenario:** Task `0011_deploy_hotfix.md` requires a git push, but the remote is unreachable.

**Workflow:**

1. Watcher detects `TASKS/0011_deploy_hotfix.md`.
2. Read the task — instructions say "Push latest commit to origin."
3. Rename to `TASKS/0011_deploy_hotfix.inprogress`.
4. Execute: `git push origin main` fails with "Could not resolve host."
5. Write failure report to `OUTPUT/0011_deploy_hotfix_20260302_150030.md`.
6. Rename to `TASKS/0011_deploy_hotfix.failed`.
7. Log error to `LOGS/task_0011_deploy_hotfix.log`.

**Output Contract:**

```
## CONTRACT
summary: Failed to push — remote host unreachable
task_id: 0011_deploy_hotfix
status: failed
verification: Task file is .failed; error logged to LOGS/; failure report written to OUTPUT/
```

---

## Example 3: Retry Scenario

**Scenario:** Task `0011_deploy_hotfix.failed` is retried after network recovery.

**Workflow:**

1. Operator renames `TASKS/0011_deploy_hotfix.failed` back to `TASKS/0011_deploy_hotfix.md`.
2. Watcher detects the `.md` file on next cycle.
3. Read the task — same instructions as before.
4. Rename to `TASKS/0011_deploy_hotfix.inprogress`.
5. Execute: `git push origin main` succeeds this time.
6. Write output to `OUTPUT/0011_deploy_hotfix_20260302_160045.md` (new timestamp, does not overwrite previous failure report).
7. Rename to `TASKS/0011_deploy_hotfix.done`.
8. Log success to `LOGS/task_0011_deploy_hotfix.log`.

**Output Contract:**

```
## CONTRACT
summary: Retry succeeded — pushed latest commit to origin after network recovery
task_id: 0011_deploy_hotfix
status: done
verification: git log confirms push; output file written; task file is .done; previous failure report preserved
```

---

## Example 4: Interrupted Task Recovery

**Scenario:** Worker crashes while executing `0012_build_index.inprogress`.

**Workflow:**

1. Watcher restarts and scans `TASKS/`.
2. Finds `TASKS/0012_build_index.inprogress` with no running worker process.
3. Logs the orphan: "Orphaned .inprogress detected: 0012_build_index".
4. Watcher does **not** auto-retry (prevents crash loops).
5. Operator investigates and renames back to `TASKS/0012_build_index.md` to retry.
6. On next cycle, watcher picks it up and executes normally.

**Output Contract:**

```
## CONTRACT
summary: Recovered orphaned task — operator renamed to .md, watcher re-executed successfully
task_id: 0012_build_index
status: done
verification: Task file is .done; output file exists; no duplicate .inprogress remains
```

---

## Example 5: Invalid Task Format

**Scenario:** Task `0013_empty.md` is an empty file with no instructions.

**Workflow:**

1. Watcher detects `TASKS/0013_empty.md`.
2. Read the task file — contents are empty (0 bytes).
3. Rename to `TASKS/0013_empty.inprogress`.
4. Parse fails — no actionable instructions found.
5. Write failure report to `OUTPUT/0013_empty_20260302_170000.md` explaining the parse error.
6. Rename to `TASKS/0013_empty.failed`.
7. Log: "Task 0013_empty has no actionable content."

**Output Contract:**

```
## CONTRACT
summary: Marked invalid — task file is empty with no actionable instructions
task_id: 0013_empty
status: failed
verification: Task file is .failed; failure report explains empty content; no execution attempted
```
