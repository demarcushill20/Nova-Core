# Pattern Retrieval Guide

## What is a task-class pattern?

A **task-class pattern** is a reusable approach, structure, or lesson learned from executing a specific type of work. Examples:

| Task Class | Pattern Example |
|-----------|----------------|
| skill creation | SKILL.md + reference/ structure, output contract, activation/non-activation criteria |
| MCP server | tool namespacing, resource vs tool decisions, bounded tool surfaces |
| multi-agent rollout | staged deployment, verifier gates, risk assessment before each stage |
| research task | source quality scoring, citation format, progressive depth |
| bug fix | reproduce first, minimal change, regression test |
| task execution | lifecycle states, output before done-rename, crash recovery |

## Where patterns live in the vault

Patterns are most likely found in these folders (in priority order):

1. **`30-workflow-learnings/`** — direct task execution lessons. Highest signal.
2. **`20-agent-patterns/`** — agent behavior conventions and templates.
3. **`70-debugging/`** — debugging insights, failure modes, solutions.
4. **`10-adrs/`** — architectural decisions. High authority but less task-specific.
5. **`90-diary/`** — session logs may contain inline learnings. Lower signal, higher noise.

## Search strategy

### Good search queries
- `"skill creation checklist"` — specific task-class + artifact type
- `"MCP server lesson"` — specific domain + learning signal
- `"task lifecycle failure"` — specific workflow + failure mode

### Bad search queries
- `"patterns"` — too broad, matches everything
- `"how to do things"` — too vague
- `"Nova-Core"` — matches nearly all notes

### Query refinement
If the first search yields too many results, narrow by:
- Adding the specific workflow step (e.g., "skill activation criteria")
- Adding a failure/success qualifier (e.g., "skill anti-pattern")
- Targeting a specific folder with `vault_list` on `30-workflow-learnings/`

If the first search yields no results:
- Broaden the task class (e.g., "automation pattern" instead of "cron job pattern")
- Try synonyms (e.g., "lesson" vs "learning" vs "insight")
- Browse `30-workflow-learnings/` with `vault_list` to discover what's available

## Interpreting pattern notes

### Frontmatter signals
- `tags: pattern, <task-class>` — high relevance
- `status: validated` — pattern has been confirmed across multiple uses
- `status: draft` — pattern is provisional, lower confidence
- `date` — older patterns may be outdated; check against current repo state

### Content signals
- Explicit "Pattern:" / "Anti-pattern:" sections — directly extractable
- "Lesson learned" / "Next time" phrasing — captures distilled experience
- "Caveat" / "Gotcha" / "Watch out" — edge cases and conditions

## Boundary with reading-obsidian-memory

| Concern | retrieving-task-patterns | reading-obsidian-memory |
|---------|------------------------|------------------------|
| Goal | Find reusable task-class patterns | General vault knowledge retrieval |
| Scope | Narrow: specific task type | Broad: any topic |
| Primary folders | 30-workflow-learnings, 20-agent-patterns, 70-debugging | All folders |
| Output shape | Patterns + anti-patterns + caveats | Synthesized findings + sources |
| When to use | Planning similar work | Any knowledge lookup |
