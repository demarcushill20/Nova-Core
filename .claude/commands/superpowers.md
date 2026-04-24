# Superpowers

Print the structured-development skills vendored from the Superpowers plugin into NovaCore, and when each one triggers.

## The 7 skills

| Skill | Trigger | Entry command |
|---|---|---|
| `brainstorming` | Before any creative engineering work — design a feature, add a component, modify behavior. Explores intent and options before implementation. | `/brainstorm` |
| `writing-plans` | After a design is approved — break the work into bite-sized, paste-ready tasks with exact file paths and code. | `/write-plan` |
| `systematic-debugging` | Any bug, test failure, or unexpected behavior — enforces 4-phase root-cause investigation with a 3-attempt escalation to the Critic agent. | `/debug` |
| `test-driven-development` | Implementing any feature or bugfix — enforces red-green-refactor and the Iron Law: no production code without a failing test first. | *auto-activates* |
| `using-git-worktrees` | Starting isolated feature work, or before `implementation-team` picks up a risky plan — creates a worktree with project setup and a clean test baseline. | `/worktree` |
| `finishing-a-development-branch` | Implementation complete, tests pass — runs pre-ship gates and presents merge / PR / keep / discard options before `/ship`. | *auto-activates* |
| `dispatching-parallel-agents` | 2+ independent tasks with no shared state — one focused subagent per problem domain, cost-optimized model selection. | *auto-activates* |

## The end-to-end flow

```
/brainstorm → design doc
  ↓
/write-plan → vault plan (via plan-tracker)
  ↓
/worktree → isolated branch + clean test baseline
  ↓
implementation-team → validate → implement → review → verify
  ↓
finishing-a-development-branch → merge/PR/keep/discard gate
  ↓
/ship → checkpoint → commit → push
```

Every step gates the next. You cannot build what you haven't designed, execute what you haven't planned, or ship what you haven't reviewed.

## NovaCore governance

These skills live alongside NovaCore's existing governance layer — they don't replace it. Plans are stored in the Obsidian vault via `plan-tracker`, debugging escalations route to the Critic agent (`AGENTS/critic/AGENT.md`) with root-cause findings logged to Fusion Memory, execution runs through `implementation-team`, and code review happens via `dual-code-review` (Codex + Opus) rather than upstream's single-model review.

## Provenance

Vendored from [`obra/superpowers`](https://github.com/obra/superpowers) @ tag v5.0.7 (sha `1f20bef3f59b85ad7b52718f822e37c4478a3ff5`), MIT-licensed.

Deviation list and re-pull procedure: `.claude/skills/_vendored/SUPERPOWERS.md`.

## Upstream items explicitly skipped

- `executing-plans`, `subagent-driven-development` — covered by `implementation-team`
- `requesting-code-review`, `receiving-code-review` — covered by `dual-code-review` (stronger, dual-model)
- `writing-skills` — covered by `skill-creator`
- `using-superpowers` — replaced by this command
- Visual Companion (browser HTML mockup infrastructure) — out of scope
- `EnterPlanMode` hook interception — NovaCore keeps its own plan governance via `implementation-team` + `plan-tracker`
