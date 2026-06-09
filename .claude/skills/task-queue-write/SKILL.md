---
name: task-queue-write
description: "Write follow-up tasks into the TASKS/ queue for the watcher to dispatch. Use when work should continue in a future session."
activation:
  keywords:
    - queue task
    - enqueue
    - follow-up task
    - schedule task
    - write task
    - create task
    - TASKS/ write
  when:
    - Current work produces follow-up items that should run in a separate session
    - A plan step generates downstream tasks
    - Research surfaces actionable work that should be queued
    - Operator asks to queue or schedule a task
---

# When To Use

- You need to queue work for the watcher to pick up in a future dispatch cycle.
- A task produces follow-up work that should run independently.
- The operator asks you to create/queue/schedule a task.

Do NOT use for:
- Work you can finish in the current session — just do it.
- Tasks that are already pending in TASKS/ (dedup will catch this, but check first).

# How To Enqueue

Run the CLI tool:

```bash
python3 /home/nova/nova-core/tools/enqueue_task.py "Task Title" "Task body describing what to do"
```

Options:
- `--priority high|medium|low` (default: medium)
- `--category research|plan|execute|repair|validate` (default: empty)
- `--source <identifier>` (default: claude-session)
- `--skip-dedup` — bypass duplicate detection
- `--body-file /path/to/file.md` — read body from a file (for long task descriptions)

The tool handles:
- Auto-incrementing sequence numbers (NNNN_ prefix)
- Duplicate detection (skips if a matching pending task exists)
- Atomic writes (tempfile + os.replace)
- YAML frontmatter with metadata

# Examples

Simple follow-up:
```bash
python3 /home/nova/nova-core/tools/enqueue_task.py \
  "Review backtest results" \
  "Review the cross-validation output in OUTPUT/0974_* and summarize divergences across engines." \
  --priority medium --category research
```

High-priority repair:
```bash
python3 /home/nova/nova-core/tools/enqueue_task.py \
  "Fix watcher checkpoint race condition" \
  "The checkpoint write in watcher.py has a TOCTOU race. See LOGS/watcher.log for the stack trace." \
  --priority high --category repair
```

Long body from file:
```bash
python3 /home/nova/nova-core/tools/enqueue_task.py \
  "Execute migration plan" \
  --body-file /home/nova/nova-core/WORK/migration_plan.md \
  --priority high --category execute
```

# Verification

After enqueuing, confirm:
1. The tool printed `CREATED: /home/nova/nova-core/TASKS/NNNN_slug.md`
2. The file exists in TASKS/ with correct content

# Notes

- The watcher picks up new .md files within ~1 second (watchdog) or ~60 seconds (polling fallback).
- Tasks created during a watcher-dispatched session will be picked up in the next dispatch cycle.
- The `source: claude-session` tag distinguishes these from autonomy-engine or heartbeat tasks.
