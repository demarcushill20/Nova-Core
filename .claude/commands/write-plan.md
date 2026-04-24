# Write Plan

Invoke the `writing-plans` skill to turn an approved design into a bite-sized implementation plan.

## Usage

```
/write-plan <path-to-design-doc or short feature description>
```

## What it does

- Maps out file structure (what gets created, modified, tested) before defining tasks
- Breaks the work into 2–5-minute tasks, each with exact file paths + full code + verification commands
- Writes the plan to the Obsidian vault at `10-plans/plan-<plan_id>.md` via `plan-tracker` (status: `backlog`)
- Optionally queues a `TASKS/<plan_id>.md` entry for the task pipeline
- Hands off to `implementation-team` for execution

## Prerequisite

A spec or design doc should exist. If it doesn't, run `/brainstorm` first — don't plan against a vague idea.

## See also

- `.claude/skills/writing-plans/SKILL.md` — full skill contract
- `.claude/skills/plan-tracker/SKILL.md` — vault plan schema + status transitions
- `implementation-team` — the orchestration skill that executes the plan
