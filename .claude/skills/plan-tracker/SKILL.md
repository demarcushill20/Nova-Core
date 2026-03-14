---
name: plan-tracker
description: "Query, update, and manage Nova-Core implementation plans stored in the Obsidian vault. Provides a unified view of all plan statuses across any access point (Telegram, webapp, Claude Code)."
allowed-tools:
  - mcp__nova-vault__vault_search
  - mcp__nova-vault__vault_read
  - mcp__nova-vault__vault_update
  - mcp__nova-vault__vault_frontmatter
activation:
  keywords:
    - plan
    - implementation plan
    - plan status
    - roadmap
    - what are we working on
---

# Plan Tracker

Manage Nova-Core implementation plans stored in the Obsidian vault (`10-plans/plan-*.md`).

## Plan Note Schema

All plans use `type: implementation-plan` with these frontmatter fields:
- `status`: `backlog` | `active` | `completed` | `paused`
- `priority`: `high` | `medium` | `low`
- `progress`: `X/Y` (phases completed / total)
- `plan_id`: unique slug (e.g., `enhancement-v5`)
- `updated`: ISO date of last status change
- `confidence`: `high` | `medium` | `low`
- Tags: `#type/plan`, `#status/{status}`, `#project/nova-core`

Body contains `## Phases` with markdown checkboxes:
```markdown
- [x] **Phase 1: Name** — completed YYYY-MM-DD (commit)
- [ ] **Phase 2: Name** — description
```

## Querying Plans

### List all plans
```
vault_search query: "type/plan" folder: "00-inbox"
```

### Filter by status
```
vault_search query: "status/active"
vault_search query: "status/backlog"
vault_search query: "status/completed"
```

### Read a specific plan
```
vault_read path: "10-plans/plan-enhancement-v5.md"
```

## Updating Plans

### When a phase is completed
1. Read the plan note
2. Update the checkbox: `- [ ]` → `- [x]` with completion date
3. Update frontmatter: `progress`, `updated`, and `status` if all phases done
4. Use `vault_update` to append a completion log entry

### When creating a new plan
Use `vault_write` with the schema above. Plan ID must be unique. File naming: `10-plans/plan-{plan_id}.md`.

### Status transitions
- `backlog` → `active`: when work begins on Phase 1
- `active` → `paused`: when blocked or deprioritized
- `active` → `completed`: when all phases done (or remaining deferred)
- `paused` → `active`: when unblocked

## Current Plans (as of 2026-03-12)

| Plan | Status | Progress | Priority |
|------|--------|----------|----------|
| Enhancement Plan v5 | active | 3/7 | high |
| Dual Memory Integration | active | 2/6 | medium |
| Security Hardening (Lethal Trifecta) | completed | 3/4 | high |
| CEO Nova Telegram | completed | 7/7 | high |
| Phase 7 Multi-Agent | backlog | 0/5 | low |
| Agent Self-Diagnostics | backlog | 1/5 | medium |

## Output Format

When the user asks for plan status, return a concise table:

```
NOVA-CORE PLANS
===============
ACTIVE:
  Enhancement v5          3/7  [====>      ] Phases 1-3 done, next: Intelligent Heartbeat
  Dual Memory Integration 2/6  [==>        ] Skills built, needs validation

BACKLOG:
  Agent Self-Diagnostics  1/5  [=>         ] Circuit breakers done
  Phase 7 Multi-Agent     0/5  [           ] Not started

COMPLETED:
  Security Hardening      3/4  [========>  ] Phase 4 deferred (RAM)
  CEO Nova Telegram       7/7  [==========] All phases + 3 bonus
```
