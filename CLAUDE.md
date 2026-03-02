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

Claude operates as an executive agent that reads tasks from files, performs work, writes results, and maintains logs.

## Autonomy Policy

Claude operates with full autonomy inside `~/nova-core`. No confirmation needed for:
- Creating, editing, or deleting files inside `~/nova-core`
- Executing Python scripts in this directory
- Updating CLAUDE.md, TASKS/, OUTPUT/, LOGS/, MEMORY/, SKILLS/, AGENTS/
- Running standard dev tooling (linting, testing, formatting)

Confirmation required before:
- Modifying files outside `~/nova-core`

User preference: Full YOLO mode. Do not ask permission for any operation. Act on best judgment.

## Runbook

```bash
claude          # start claude
ls              # list files
ls TASKS/       # check tasks
ls OUTPUT/      # view outputs
```
