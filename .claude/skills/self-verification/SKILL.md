---
name: self-verification
description: "Verify nova-core integrity: check directory structure, validate task states, confirm logs exist, and run health diagnostics."
---

# self-verification

## When to use

- After completing a task, to confirm outputs and logs are correct.
- Periodic health checks of the nova-core environment.
- When the watcher restarts or a failure is detected.
- Before reporting task completion.

## Rules / Safety

- Read-only by default. Do not modify files during verification unless repairing a known inconsistency.
- All checks scoped to `~/nova-core`.
- Report anomalies but do not auto-fix unless the fix is safe and deterministic.

## Workflow

1. Verify **required** directories exist: `TASKS/`, `OUTPUT/`, `LOGS/`.
2. Check for `WORK/` — required if it already exists in the repo, otherwise optional (do not fail).
3. Check **optional** directories (do not fail if missing): `MEMORY/`, `AGENTS/`, `SKILLS/`.
4. Verify `.claude/skills/` always exists (fail if missing).
5. Check for orphaned `.inprogress` tasks (started but never completed).
6. For each `.done` task, confirm a corresponding output file exists in `OUTPUT/`.
7. Confirm `LOGS/` contains recent entries (not stale beyond threshold).
8. Validate `CLAUDE.md` exists and is parseable.
9. Return a health report.

## Output format

```
[self-verification] HEALTH CHECK — <timestamp>
  Required dirs:  OK | MISSING: <list>        (TASKS/, OUTPUT/, LOGS/, WORK/ if present)
  Optional dirs:  OK | ABSENT: <list>         (MEMORY/, AGENTS/, SKILLS/ — informational only)
  .claude/skills: OK | MISSING                (always required)
  Orphaned tasks: NONE | <list>
  Output match:   OK | MISSING: <list>
  Logs freshness: OK | STALE (last entry: <date>)
  CLAUDE.md:      OK | MISSING
  Status:         PASS | FAIL
```
