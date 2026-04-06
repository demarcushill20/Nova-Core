---
name: task-execution
description: Compact task execution rules
version: "1.0"
---

## Task Execution (Compact)

1. Read task file from TASKS/ before executing
2. Claim via atomic rename: `.md` -> `.md.inprogress`
3. Execute the task requirements
4. Write output to OUTPUT/ with timestamp
5. Complete: rename `.inprogress` -> `.done` (or `.failed`)
6. Never delete task files -- only rename
7. Skip state transitions if dispatched by watcher (it handles lifecycle)

**Output Contract** (required at end of every output):
```
## CONTRACT
summary: <one-line>
task_id: <stem>
status: done|failed
files_changed: <list>
verification: <what was checked>
confidence: high|medium|low
```
