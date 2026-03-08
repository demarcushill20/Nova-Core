---
name: capturing-workflow-learnings
description: "Safely capture completed workflow outcomes into compact, reusable workflow-learning notes in the canonical synced Obsidian vault. Invoked when a workflow or implementation step completes successfully and contains durable lessons worth preserving."
disable-model-invocation: false
allowed-tools:
  - mcp__nova-vault__vault_validate
  - mcp__nova-vault__vault_write
  - mcp__nova-vault__vault_read
  - mcp__nova-vault__vault_search
  - mcp__nova-vault__vault_frontmatter
---

# Capturing Workflow Learnings

## When to use

- A workflow, implementation step, or multi-step task has completed successfully
- The completed work contains a reusable lesson, failure mode, caveat, or next-time guidance worth preserving
- Nova-Core should compact execution results into durable open memory for future retrieval
- A debugging session produced insights that would prevent the same failure mode next time
- A task exposed an anti-pattern or gotcha that future task execution should know about

## When NOT to use

- The workflow is still in progress or has not been verified
- The output is too thin, noisy, or speculative to be a durable learning — skip the write
- You want to create a broad research note — use a different note type / skill
- You want to edit or correct an existing human-authored note — not allowed
- You need to store live runtime state — use STATE/ files instead
- You want to create an agent-pattern, debugging-guide, or inbox note — those are different note types
- You are unsure whether the learning is durable — prefer no write over a weak write

## Inputs

- **workflow_result**: The completed task output, execution summary, or session outcome that contains learnings. Required.
- **task_class**: The type of work that was performed (one of: `research`, `code_impl`, `code_review`, `system`, `simple`, `unknown`). Required.
- **workflow_id**: An identifier for the workflow or session (e.g., `session_32`, `task_0042`). Required.

## Capture Workflow

### Step 1 — Inspect the completed result

Review the workflow result to understand what happened. Identify:
- What was the task?
- What approach was taken?
- What worked?
- What failed or was abandoned?
- What caveats or edge cases surfaced?

### Step 2 — Decide whether to capture

Apply these filters before proceeding:
- **Is there a durable lesson?** If the task completed trivially with no novel insight, stop.
- **Is the learning reusable?** If it only applies to this exact one-time scenario, stop.
- **Is the learning non-obvious?** If any experienced developer would know this, stop.
- **Is the source verified?** Only capture from completed, verified workflows. Never from speculative or in-progress work.

If all filters pass, continue. If any filter fails, report "No durable learning identified" and stop.

### Step 3 — Extract compact reusable insights

From the workflow result, extract:
- **Key decisions**: What choices were made and why
- **What worked**: Reusable patterns and approaches
- **What failed**: Anti-patterns and abandoned approaches with reasons
- **Reusable guidance**: Concrete next-time advice (not platitudes)
- **Metrics**: Quantitative outcomes if available (test counts, performance numbers)

Compaction rules:
- Summarize, do not transcript-dump. A 500-line execution log becomes 5-10 bullet points.
- Prefer actionable guidance over narrative description.
- Include enough context to understand the learning without the original task file.
- Omit session-specific details (timestamps, exact file paths) unless they are the lesson itself.

### Step 4 — Compose the note

Build the workflow-learning note with this structure:

```markdown
---
type: workflow-learning
learning_id: "wl-YYYY-MM-<slug>"
title: "<Concise descriptive title>"
workflow_id: "<workflow or session ID>"
task_class: "<research|code_impl|code_review|system|simple|unknown>"
verification_outcome: "<approved|rejected|partial|not_verified>"
confidence: "<high|medium|low>"
roles_involved:
  - "<role1>"
  - "<role2>"
date: "YYYY-MM-DD"
source: "nova-core-memory"
tags:
  - "#type/learning"
  - "#confidence/<level>"
  - "#status/active"
related:
  - "[[related-note-1]]"
---

## Task Summary

<1-2 sentence description of what was done>

## Key Decisions

- <decision 1>
- <decision 2>

## What Worked

- <pattern 1>
- <pattern 2>

## What Failed

- <anti-pattern or abandoned approach — and why>

## Reusable Guidance

<Concrete next-time advice. The most important section.>

## Metrics

- <quantitative outcomes if available>

## Trace

- **Workflow ID**: <ID>
- **Source artifact**: <path to OUTPUT/ or WORK/ artifact if applicable>
- **Task file**: <TASKS/ path if applicable>
```

### Step 5 — Validate before writing

Use `vault_validate` to check the composed note against the `workflow-learning` schema before writing. Fix any validation errors before proceeding.

### Step 6 — Write the note

Use `vault_write` to create the note in `30-workflow-learnings/`. The filename should follow the pattern: `YYYY-MM-<descriptive-slug>.md`.

Rules:
- `source` MUST be `"nova-core-memory"` — this is enforced by the write path
- Target folder MUST be `30-workflow-learnings/`
- Never overwrite an existing note — the write path rejects duplicates
- If the write is rejected, report the reason and stop

### Step 7 — Verify (optional)

If write succeeds, optionally use `vault_read` to confirm the note was created correctly. This is recommended for the first few uses but can be skipped once confidence is established.

## Safe-Source Rules

- **STATE/ and repo outputs are operational truth.** Obsidian workflow learnings are durable summaries, not live execution state.
- **vault_write creates, never overwrites.** Each learning is a new note. No mutation of existing notes.
- **source must be nova-core-memory.** This marks the note as Nova-Core-managed and protects human-authored notes from accidental updates.
- **Human-authored notes are untouchable.** Never use `vault_update` on notes with `source: operator` or unknown ownership.
- **When uncertain, prefer no write.** A missing learning is harmless. A noisy or incorrect learning pollutes the vault.
- **Degrade gracefully.** If the vault is unavailable or write fails, report the issue and continue. Never block on vault access.

## Tool Usage Rules

- **vault_validate first.** Always validate the note content before writing. Fix errors before proceeding.
- **vault_write for creation.** Use only `vault_write` to create new notes. Never use `vault_update` for initial creation.
- **vault_read for verification.** Optionally read back the created note to confirm correctness.
- **vault_search for dedup check.** Before writing, search for similar existing learnings. If a closely matching note exists, skip the write and report the duplicate.
- **No vault_update.** This skill creates new learning notes. It does not modify existing ones.
- **Bounded tool calls.** Maximum 6 tool calls per invocation: 1 search (dedup) + 1 validate + 1 write + 1 read (verify) + 2 spare.

## Output Contract

Every invocation of this skill MUST produce:

```
## Workflow Learning Capture

### Learning Candidate
- **Task class**: <task class>
- **Workflow ID**: <workflow or session ID>
- **Summary**: <1-sentence summary of the learning>

### Decision
<captured | skipped — reason>

### Extracted Lessons
- **What worked**: <bullet points>
- **What failed**: <bullet points or "nothing notable">
- **Reusable guidance**: <key takeaway>
- **Caveats**: <gotchas or edge cases, or "none">

### Write Result
- **Status**: <success | skipped | rejected — reason>
- **Note path**: <30-workflow-learnings/filename.md or N/A>
- **Note size**: <bytes or N/A>

### Capture Log
| # | Tool | Input | Result |
|---|------|-------|--------|
| 1 | vault_search | "dedup query" | 0 hits |
| 2 | vault_validate | <note content> | valid |
| 3 | vault_write | 30-workflow-learnings/note.md | accepted |

### Confidence
<high / medium / low> — <1-sentence justification>
```

## Examples

### Example 1: Capturing a successful skill creation workflow

**Workflow result**: Created the `reading-obsidian-memory` skill with SKILL.md, reference docs, and output contract. 148 tests pass.

**Learning Candidate**:
- **Task class**: code_impl
- **Workflow ID**: session_30
- **Summary**: Obsidian read-only skill creation follows a consistent SKILL.md + reference/ structure with explicit activation, tool doctrine, and output contract.

**Decision**: captured — contains reusable skill creation pattern.

**Write Result**:
- **Status**: success
- **Note path**: `30-workflow-learnings/2026-03-obsidian-skill-creation-pattern.md`
- **Note size**: 892 bytes

### Example 2: Skipping a trivial workflow

**Workflow result**: Fixed a typo in CLAUDE.md.

**Learning Candidate**:
- **Task class**: simple
- **Workflow ID**: session_33
- **Summary**: Fixed a typo.

**Decision**: skipped — no durable learning. Trivial fix with no reusable insight.
