---
name: writing-agent-patterns
description: "Promote stable, repeatable Nova-Core methods into structured Obsidian agent-pattern notes. Invoked when a technique has proven reusable across multiple tasks and is mature enough to preserve as a durable pattern."
disable-model-invocation: false
allowed-tools:
  - mcp__nova-vault__vault_search
  - mcp__nova-vault__vault_validate
  - mcp__nova-vault__vault_write
  - mcp__nova-vault__vault_read
  - mcp__nova-vault__vault_frontmatter
activation:
  keywords:
    - agent pattern
    - write pattern
    - promote method
    - codify pattern
---

# Writing Agent Patterns

## When to use

- Multiple workflow learnings or execution outcomes point to the same repeatable method
- A planner, coder, research, critic, or verifier technique has proven stable across 2+ tasks
- A method is mature enough to be preserved as reusable guidance for future task planning
- Nova-Core should codify a proven approach so future executions can retrieve and apply it
- A pattern extracted from several sessions is worth making durable and searchable

## When NOT to use

- One-off task outcome — use `capturing-workflow-learnings` instead
- Weak, noisy, or speculative lesson — wait for more evidence
- Live runtime state — use STATE/ files instead
- Arbitrary note editing or broad vault writes — not this skill's purpose
- The method has only been observed once — one datapoint is not a pattern
- Creating research summaries, debugging guides, or inbox notes — different note types
- Modifying human-authored notes — never allowed

## How this differs from `capturing-workflow-learnings`

| Property | capturing-workflow-learnings | writing-agent-patterns |
|----------|------------------------------|------------------------|
| **Trigger** | One workflow completes | Multiple workflows confirm a method |
| **Confidence bar** | Medium — single verified outcome | High — repeated, stable success |
| **Content** | What happened, what worked/failed | How to do it, when to apply, when not to |
| **Target folder** | `30-workflow-learnings/` | `20-agent-patterns/` |
| **Note type** | `workflow-learning` | `agent-pattern` |
| **Durability** | Per-execution record | Cross-execution reusable method |

## Inputs

- **method**: Description of the stable technique or approach to capture. Required.
- **agent_role**: Which agent role this pattern applies to (`research`, `coder`, `critic`, `verifier`, `planner`, `memory`). Required.
- **evidence**: Evidence that the method is reusable — prior tasks, sessions, or learnings that confirm it. Required.
- **task_classes**: Which task classes benefit from this pattern (list from: `research`, `code_impl`, `code_review`, `system`, `simple`). Required.

## Pattern-Promotion Workflow

### Step 1 — Assess pattern maturity

Before writing, verify:
- **Repeated success**: Has this method worked across 2+ independent tasks or sessions?
- **Stable behavior**: Is the method consistent, not evolving rapidly between uses?
- **Agent-role clarity**: Is it clear which agent role owns this pattern?
- **Non-obvious value**: Does it encode knowledge that isn't immediately obvious from the task description?

If any check fails, stop. Report "Pattern not mature enough" and recommend tracking it as a workflow learning instead.

### Step 2 — Search for existing patterns

Use `vault_search` with the method's core terms to check:
- Does a similar pattern already exist? If yes, report the duplicate and stop.
- Are there related workflow learnings that support this pattern? Note them as evidence.
- **Link harvesting**: From the search results, select 2-3 relevant but non-duplicate notes as related content. Record their filenames for use in the `related:` field and `## Related Notes` body section.

**Domain inference**: From the agent_role and pattern content, infer the primary domain:
- trade, strategy, backtest, IRB, MT5, execution → `novatrade`
- autonomy, heartbeat, decision engine, guardrail → `autonomy`
- memory, fusion, pinecone, neo4j, vault, recall → `memory`
- systemd, circuit breaker, self-heal, deploy, nginx → `infrastructure`
- agent, spawner, orchestrator, multi-agent → `agents`
- risk, gate, filter, drawdown, exposure → `risk`
- Default: `operations`

### Step 3 — Extract the stable pattern

From the evidence, extract:
- **Summary**: What the pattern does in 1-2 sentences
- **When to apply**: Explicit trigger conditions
- **Pattern steps**: The repeatable procedure (numbered)
- **Success indicators**: How to know the pattern worked
- **Failure modes**: Known anti-patterns and misuse boundaries
- **Guidance**: Concrete advice for applying the pattern

Compaction rules:
- Lead with the method, not the history of how it was discovered.
- Write for a future agent that has never seen the original tasks.
- Keep the pattern actionable — every section should inform behavior.
- Omit session-specific details unless they are the lesson itself.

### Step 4 — Compose the note

Build the agent-pattern note with this structure:

```markdown
---
type: agent-pattern
pattern_id: "ap-<descriptive-slug>"
title: "<Concise pattern title>"
agent_role: "<research|coder|critic|verifier|planner|memory>"
confidence: "<high|medium|low>"
task_classes:
  - "<class1>"
  - "<class2>"
date_created: "YYYY-MM-DD"
source: "nova-core-memory"
tags:
  - "#type/pattern"
  - "#agent/<role>"
  - "#confidence/<level>"
  - "#status/active"
  - "#domain/<inferred-domain>"
  - "#project/nova-core"
related:
  - "[[related-note-1]]"
  - "[[related-note-2]]"
---

up:: [[moc-<inferred-domain>]]

## Summary

<1-2 sentence description of what this pattern does>

## When to Apply

- <trigger condition 1>
- <trigger condition 2>

## Pattern Steps

1. **Step name** — description
2. **Step name** — description

## Success Indicators

- <how to know the pattern worked>

## Failure Modes

- **Failure name** — description of anti-pattern

## Guidance

<Concrete advice. The most important section.>

## Related Notes

- [[related-note-1]] — <brief annotation>
- [[related-note-2]] — <brief annotation>
(Populated from vault_search results in Step 2. Omit if no related notes found.)

## Source Evidence

- <evidence 1: session, task, or learning that confirms the pattern>
- <evidence 2>
```

### Step 5 — Validate before writing

Use `vault_validate` to check the composed note against the `agent-pattern` schema. Fix any validation errors before proceeding.

### Step 6 — Write the note

Use `vault_write` to create the note in `20-agent-patterns/`. The filename should follow the pattern: `<descriptive-slug>.md`.

Rules:
- `source` MUST be `"nova-core-memory"`
- Target folder MUST be `20-agent-patterns/`
- Never overwrite an existing note
- If the write is rejected, report the reason and stop

### Step 7 — Verify (optional)

Use `vault_read` to confirm the note was created correctly. Recommended for the first few uses.

## Safe-Source Rules

- **STATE/ and repo outputs are operational truth.** Obsidian patterns are durable guidance, not live execution state.
- **vault_write creates, never overwrites.** Each pattern is a new note.
- **source must be nova-core-memory.** Marks the note as Nova-Core-managed.
- **Human-authored notes are untouchable.** Never modify notes with `source: operator`.
- **When uncertain, prefer no write.** A missing pattern is harmless. A wrong pattern misleads future execution.
- **Degrade gracefully.** If vault unavailable, report and continue.

## Tool Usage Rules

- **vault_search first.** Check for duplicates and related content before writing.
- **vault_validate second.** Always validate before writing.
- **vault_write for creation.** New notes only. Never use `vault_update`.
- **vault_read for verification.** Optional read-back to confirm.
- **Bounded calls.** Maximum 6 tool calls: 1 search + 1 validate + 1 write + 1 read + 2 spare.

## Output Contract

Every invocation of this skill MUST produce:

```
## Agent Pattern Promotion

### Candidate
- **Method**: <1-sentence description>
- **Agent role**: <role>
- **Task classes**: <list>
- **Evidence strength**: <number of confirming tasks/sessions>

### Maturity Assessment
<promoted | deferred — reason>

### Pattern Summary
- **When to apply**: <trigger conditions>
- **Key steps**: <numbered list>
- **Failure modes**: <anti-patterns>
- **Guidance**: <key advice>

### Write Result
- **Status**: <success | skipped | rejected — reason>
- **Note path**: <20-agent-patterns/filename.md or N/A>
- **Pattern ID**: <ap-slug or N/A>

### Promotion Log
| # | Tool | Input | Result |
|---|------|-------|--------|
| 1 | vault_search | "dedup query" | 0 hits |
| 2 | vault_validate | <note content> | valid |
| 3 | vault_write | 20-agent-patterns/note.md | accepted |

### Confidence
<high / medium / low> — <1-sentence justification>
```

## Examples

### Example 1: Promoting a research search pattern

**Candidate**:
- **Method**: Multi-query web research with source quality scoring
- **Agent role**: research
- **Evidence strength**: Confirmed across 4 research tasks (Sessions 22, 28, 30, 32)

**Maturity Assessment**: promoted — stable method with 4 independent confirmations.

**Write Result**:
- **Status**: success
- **Note path**: `20-agent-patterns/research-multi-query-strategy.md`
- **Pattern ID**: `ap-research-multi-query-strategy`

### Example 2: Deferring an immature pattern

**Candidate**:
- **Method**: Parallel agent execution for independent subtasks
- **Agent role**: planner
- **Evidence strength**: Observed once in Session 31

**Maturity Assessment**: deferred — only one datapoint. Recommend tracking as a workflow learning until confirmed across 2+ tasks.
