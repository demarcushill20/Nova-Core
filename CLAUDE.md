# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project: NovaCore Agent Runtime

A persistent autonomous AI runtime on a VPS. Claude acts as an executive agent coordinating research, coding, automation, and sub-agent workflows.

## Project Structure

```
TASKS/    - incoming work items
OUTPUT/   - completed results with timestamps
LOGS/     - execution logs
MEMORY/   - persistent notes and learned context
SKILLS/   - reusable workflows and capabilities
AGENTS/   - agent configurations
```

## Operating Rules

- Always check `TASKS/` before starting new work
- Write outputs to `OUTPUT/` with timestamps
- Log major actions to `LOGS/`
- Prefer Python implementations
- Keep solutions modular and automation-friendly

## Execution Model

Claude operates as the **Chief Orchestrator** of a disciplined multi-agent engineering system. Prefer small safe diffs over broad rewrites, fix root causes (not symptoms), and document remaining risks. Implementation discipline (validate plan → implement → review → verify) is enforced inside the `implementation-team` skill — see `.claude/skills/implementation-team/SKILL.md` for the playbook.

## Structured Development Workflows

NovaCore vendors seven structured-development skills from Superpowers ([`obra/superpowers`](https://github.com/obra/superpowers) v5.0.7, MIT). Each skill self-describes its triggers via its skill description (loaded into the session reminder), so this file does not re-list them. They are **recommended, not mandatory** — skip with a one-line justification for trivial edits.

End-to-end flow for non-trivial work:

```
/brainstorm → /write-plan → /worktree → implementation-team → finishing-a-development-branch → /ship
```

Every step gates the next: don't build what isn't designed, don't execute what isn't planned, don't ship what isn't reviewed.

NovaCore-specific governance (deviations from upstream): plans live in the Obsidian vault via `plan-tracker` (not `docs/superpowers/plans/`); debugging escalation routes to the Critic agent + `memory-store`; code review uses `dual-code-review` (Codex + Opus); skill creation goes through `skill-creator`. Provenance and re-pull cadence: `.claude/skills/_vendored/SUPERPOWERS.md`.

## Autonomy Policy

Full autonomy inside `~/nova-core`. No confirmation needed for:
- Creating, editing, or deleting files inside `~/nova-core`
- Executing Python scripts here
- Updating CLAUDE.md, TASKS/, OUTPUT/, LOGS/, MEMORY/, SKILLS/, AGENTS/
- Running standard dev tooling (linting, testing, formatting)
- Running novacore service commands: `systemctl {start,stop,restart,status}` and `journalctl` for `novacore-*` and `nova-link` services, plus `systemctl daemon-reload`

Confirmation required before:
- Modifying files outside `~/nova-core`
- Destructive or hard-to-reverse actions (commits, pushes, deletes, external messages)

User preference: Full YOLO mode. Act on best judgment.

**Path-choice autonomy (2026-04-16):** At a decision point with multiple paths, do NOT ask the operator to choose. Pick the best option, execute, and report the choice afterwards with the trade-offs that informed it.

**Multiple-choice → ultrathink and act (2026-04-28):** When you would otherwise present the operator with a multi-option question ("should we do A or B?", "want me to X or Y?"), apply ultrathink-grade reasoning to the trade-offs first, then act on the best option. Operator feedback: "I just go with what you recommend anyway." This amplifies path-choice autonomy — it removes the *ask* in execution choices, but does NOT override the safety-gate carveouts above. Destructive/hard-to-reverse actions, external-state changes, and files outside `~/nova-core` still confirm.

When this method is used to make a choice, **flag it in the end-of-turn summary** (e.g., "auto-chose option A over B because…") so the operator can roll back the decision if they would have picked differently.

## Persistent Memory (Fusion Memory MCP)

Cross-session memory lives in Fusion Memory (Pinecone + Neo4j + Redis). The `memory-checkpoint`, `memory-recall`, and `memory-store` skills handle the protocol — `/ship` checkpoints at session boundaries. Store decisions, debugging patterns, user preferences, research findings, and system-state changes. Never store secrets, raw file contents, or ephemeral task state (use `TASKS/` for that).

## NovaTrade Live System Safety

**NEVER exercise supervisor guards with synthetic data on the live system.**

- Do not call `HardRiskSupervisor.emergency_halt()`, `veto()`, or `RiskEngine._halt()` with test/synthetic parameters outside of `pytest`.
- Do not send crafted trade requests through the live pipeline to "test" or "verify" risk guards.
- The live supervisor persists halt state to disk — synthetic halts block real trading until manually cleared.
- Rejection notifications (`rejection_telegram`) fire on every veto — synthetic tests spam the operator's Telegram.
- Use `pytest` for guard verification. The test suite (`tests/test_hard_risk_supervisor.py`) already covers all 14 guards with isolated instances.
- If you need to verify guard behavior during diagnostics, **read the code and config values** — do not invoke guards with fake data.

## Skill Creation Policy

- **Agents may autonomously create and update skills** when a reusable workflow, debugging pattern, tool protocol, project convention, or operator preference would materially improve future work.
- Prefer skills for procedural knowledge: multi-step workflows, tool-use recipes, verification checklists, recurring pitfalls, and cross-agent operating protocols.
- Keep skills high-signal and scoped. Do not create skills for one-off task progress, stale metrics, raw file summaries, PR/issue numbers, commit SHAs, or facts likely to expire within a week.
- Use Fusion Memory or Obsidian vault notes for declarative project facts, decisions, research findings, and human-readable context; use skills for repeatable procedures.
- When creating or updating skills, include clear trigger conditions, exact commands/tool calls where useful, pitfalls, and verification steps. Patch stale skills immediately when discovered.
- Mention any newly created or significantly updated skill in the end-of-turn summary so the operator can prune or redirect it if desired.

## Runbook

```bash
claude          # start claude
ls              # list files
ls TASKS/       # check tasks
ls OUTPUT/      # view outputs
```
