---
name: retrieving-task-patterns
description: "Targeted retrieval of prior task-class patterns, workflow learnings, and reusable examples from the synced Obsidian vault. Invoked when planning or execution benefits from knowing how Nova-Core handled similar work before."
disable-model-invocation: false
allowed-tools:
  - mcp__nova-vault__vault_search
  - mcp__nova-vault__vault_read
  - mcp__nova-vault__vault_list
  - mcp__nova-vault__vault_frontmatter
---

# Retrieving Task Patterns

## When to use

- Planning a task and similar prior task-class examples would improve consistency or speed
- The user asks how Nova-Core handled something similar before
- A recurring task type (deploy, refactor, research, skill creation) may have established patterns
- Debugging a workflow failure where past anti-patterns or caveats were documented
- Building a new skill or agent and prior patterns provide a reusable template

## When NOT to use

- Broad general memory search — use **reading-obsidian-memory** instead
- Looking up current runtime state — use `STATE/` files
- Checking task status or logs — use `TASKS/`, `LOGS/`
- Writing or promoting patterns to the vault — use a write-oriented skill
- The question is about external/public information — use **web-research**
- General architecture decisions not tied to a task class — use **reading-obsidian-memory**

## Inputs

- **task_class**: The type of work being planned (e.g., "skill creation", "MCP server", "multi-agent rollout", "bug fix", "research task"). Required.
- **context**: Optional additional context narrowing what patterns are useful (e.g., "read-only skill with MCP tools").

## Retrieval Workflow

1. **Classify the task** — identify the task class or workflow type. Be specific: "creating a read-only MCP skill" not "building something".

2. **Search for matching patterns** — use `vault_search` with focused terms derived from the task class. Try 2-3 queries with varied phrasing:
   - task-class keywords (e.g., "skill creation pattern")
   - workflow-learning keywords (e.g., "lesson learned skill")
   - folder-scoped keywords targeting `30-workflow-learnings`, `20-agent-patterns`, `70-debugging`

3. **Inspect metadata before reading** — use `vault_frontmatter` on promising results. Check tags, task_class, and date fields. Skip notes that are clearly irrelevant or too old to be useful.

4. **Read only the most relevant notes** — use `vault_read` on the top 1-3 matches. Do NOT speculatively read notes. Do NOT read more than 5 notes total.

5. **Extract reusable patterns** — from each relevant note, extract:
   - **Pattern**: what worked and should be repeated
   - **Anti-pattern**: what failed or was abandoned and why
   - **Caveats**: conditions, edge cases, or gotchas
   - **Template fragments**: reusable structures, contracts, or workflows

6. **Return bounded guidance** — synthesize findings into planner-friendly output. Keep it actionable and concise. Do not dump raw note content.

## Safe-Source Rules

- **STATE/ is authoritative for runtime truth.** Never use an Obsidian note to determine current task state, service health, or flag status.
- **Obsidian patterns are advisory, not mandatory.** They capture what worked before, but current repo and runtime truth always wins if there is a conflict.
- **Patterns are reusable memory, not live state.** They may be outdated. Treat them as "best known prior approach" not "current requirement".
- **Human-authored notes are first-class.** Playbooks, ADRs, and diary entries may contain human-defined patterns. Respect and cite them.
- **Degrade gracefully.** If the vault is unavailable, report it and continue without pattern data. Never block on vault access.

## Tool Usage Rules

- **Read-only.** Never write, update, or delete vault notes.
- **Bounded retrieval.** Never read more than 5 notes per invocation. Prefer 1-3.
- **Search first, list second.** Always use `vault_search` before `vault_list`. Only list a folder if search yields no results and you need to browse `30-workflow-learnings` or `20-agent-patterns` directly.
- **Prefer pattern-rich folders.** Prioritize results from:
  - `30-workflow-learnings` — task execution lessons
  - `20-agent-patterns` — agent behavior conventions
  - `70-debugging` — debugging insights and solutions
  - `10-adrs` — architectural decisions (weight highly)
- **Respect human folders.** Notes from `50-playbooks`, `60-project`, `90-diary` may contain useful patterns but are human-authored. Cite and respect them.
- **No fabrication.** Only report patterns found in actual vault content. Never invent note titles, content, or patterns.

## Output Contract

Every response using this skill MUST include:

```
## Task Pattern Findings

**Task class**: <identified task class>

### Patterns
- <pattern 1>: <description>
- ...

### Anti-patterns
- <anti-pattern 1>: <what to avoid and why>
- ... (or "none found")

### Caveats
- <caveat 1>: <condition or gotcha>
- ... (or "none found")

### Sources
- `path/to/note.md` — relevance summary
- ...

### Retrieval Log
| # | Tool | Query/Path | Result |
|---|------|------------|--------|
| 1 | vault_search | "skill creation pattern" | 2 hits |
| 2 | vault_read | "30-workflow-learnings/skill-patterns.md" | relevant |

### Confidence
<high / medium / low> — <1-sentence justification>
```

## Examples

### Example 1: Planning a new skill

**Task class**: "skill creation"
**Context**: "creating a read-only MCP skill"

**Retrieval log**:
| # | Tool | Query/Path | Result |
|---|------|------------|--------|
| 1 | vault_search | "skill creation" | 3 hits |
| 2 | vault_search | "MCP skill pattern" | 1 hit |
| 3 | vault_frontmatter | "30-workflow-learnings/skill-creation-checklist.md" | tags: skill, pattern |
| 4 | vault_read | "30-workflow-learnings/skill-creation-checklist.md" | relevant |

**Patterns**: Skills follow SKILL.md + reference/ structure. Always include output contract. Define activation and non-activation criteria explicitly.

**Anti-patterns**: Avoid overly broad activation — skills that fire too often add noise.

**Caveats**: Skill tool_doctrine teaches behavior not authorization. Check existing skills for overlap before creating a new one.

**Confidence**: high — clear pattern note with established conventions.

### Example 2: Debugging a task failure

**Task class**: "task execution debugging"
**Context**: "task stuck in .inprogress state"

**Retrieval log**:
| # | Tool | Query/Path | Result |
|---|------|------------|--------|
| 1 | vault_search | "task stuck inprogress" | 1 hit |
| 2 | vault_search | "task lifecycle failure" | 2 hits |
| 3 | vault_read | "70-debugging/orphaned-inprogress.md" | relevant |

**Patterns**: Orphaned .inprogress files are usually caused by worker crashes. Check `STATE/running/<stem>.pid` for stale PIDs.

**Anti-patterns**: Do not manually rename to .done without verifying output exists.

**Caveats**: PID files may reference dead processes. Always signal-check before assuming a task is still running.

**Confidence**: medium — pattern note exists but may not cover all failure modes.
