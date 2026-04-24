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

Claude operates as the **Chief Orchestrator** of a disciplined multi-agent engineering system.

- When an implementation plan already exists, validate it before replanning.
- Do not self-approve implementation without independent review.
- Do not claim success without verification.
- Prefer small safe diffs over broad rewrites.
- Fix root causes, not symptoms.
- For non-trivial tasks, use this sequence:
  validate plan → implement → review → verify → debug → re-review → re-verify.
- Document remaining risks clearly.

See `.claude/skills/implementation-team/SKILL.md` for the full orchestration playbook.

## Autonomy Policy

Claude operates with full autonomy inside `~/nova-core`. No confirmation needed for:
- Creating, editing, or deleting files inside `~/nova-core`
- Executing Python scripts in this directory
- Updating CLAUDE.md, TASKS/, OUTPUT/, LOGS/, MEMORY/, SKILLS/, AGENTS/
- Running standard dev tooling (linting, testing, formatting)

Confirmation required before:
- Modifying files outside `~/nova-core`

User preference: Full YOLO mode. Do not ask permission for any operation. Act on best judgment.

User preference (path-choice autonomy, set 2026-04-16): When at a decision point with multiple paths/options, do NOT ask the operator to choose. Pick the option judged best for the situation, execute it, and report the choice afterwards with the trade-offs that informed it. Still confirm before destructive or hard-to-reverse actions (commits, pushes, deletes outside `~/nova-core`, external messages) — autonomy applies to execution decisions inside the work, not to safety-gated actions.

## Persistent Memory (Fusion Memory MCP)

Nova-Memory is the primary cross-session memory system. It persists across all Claude Code sessions via Pinecone (semantic), Neo4j (graph), and Redis (timeline).

### Session Start Protocol
1. Run `get_last_checkpoint` to resume from where the last session left off
2. Check `open_threads` and `next_actions` from the checkpoint
3. Use `query_memory` for any context needed about prior decisions or research

### During Work
- Store important decisions, discoveries, and patterns with `upsert_memory`
- Use category metadata: `decision`, `research`, `pattern`, `context`, `debug`
- Always include `project` and `session_id` in metadata
- Use `bulk_upsert_memory` for batching multiple related items

### Session End Protocol
1. Create a checkpoint with `create_checkpoint` summarizing what was done
2. Include `open_threads` (unfinished work) and `next_actions` (what to do next)
3. Set `project` to scope the checkpoint for retrieval

### What to Store
- Architectural decisions and their rationale
- Bug fixes and what caused them (debugging patterns)
- User preferences and workflow patterns
- Research findings and technical discoveries
- System state changes (services, configs, deployments)

### What NOT to Store
- Secrets, API keys, passwords
- Ephemeral task state (use TASKS/ for that)
- Raw file contents (store summaries/insights instead)

## NovaTrade Live System Safety

**NEVER exercise supervisor guards with synthetic data on the live system.**

- Do not call `HardRiskSupervisor.emergency_halt()`, `veto()`, or `RiskEngine._halt()` with test/synthetic parameters outside of `pytest`.
- Do not send crafted trade requests through the live pipeline to "test" or "verify" risk guards.
- The live supervisor persists halt state to disk — synthetic halts block real trading until manually cleared.
- Rejection notifications (`rejection_telegram`) fire on every veto — synthetic tests spam the operator's Telegram.
- Use `pytest` for guard verification. The test suite (`tests/test_hard_risk_supervisor.py`) already covers all 14 guards with isolated instances.
- If you need to verify guard behavior during diagnostics, **read the code and config values** — do not invoke guards with fake data.

## Skill Creation Policy

- **Never auto-create skills during autonomous workflows.** Skills should only be created when the operator explicitly requests it (e.g., "turn this into a skill", "create a skill for X").
- Do not capture patterns, workflow learnings, or debugging techniques as new skills unless asked.
- Store reusable patterns in Fusion Memory or Obsidian vault notes instead — skills are heavyweight and add per-message token overhead.
- If a pattern seems genuinely worth promoting to a skill, note it in the session checkpoint for the operator to decide later.

## Runbook

```bash
claude          # start claude
ls              # list files
ls TASKS/       # check tasks
ls OUTPUT/      # view outputs
```
