---
name: reading-obsidian-memory
description: "Read-only retrieval of prior knowledge from the synced Obsidian vault (Nova-Core open memory). Auto-invoked when tasks benefit from prior architecture decisions, research summaries, workflow patterns, or learned context stored in the vault."
disable-model-invocation: false
allowed-tools:
  - mcp__nova-vault__vault_search
  - mcp__nova-vault__vault_read
  - mcp__nova-vault__vault_list
  - mcp__nova-vault__vault_frontmatter
  - mcp__nova-vault__vault_info
activation:
  keywords:
    - obsidian
    - vault read
    - prior knowledge
    - vault search
---

# Reading Obsidian Memory

## When to use

- The user asks to search prior Nova-Core knowledge, decisions, or research
- Planning or research needs prior architecture decisions, design patterns, workflow learnings, or research summaries
- Relevant open-memory context may improve the quality of an answer
- The user references a topic that was previously researched or documented in the vault
- Debugging or troubleshooting where past solutions may be recorded

## When NOT to use

- Looking up current runtime state (use STATE/ files instead)
- Checking task status or watcher state (use TASKS/, LOGS/)
- Writing or updating vault notes (use a write-oriented skill when available)
- The question is purely about external/public information (use web-research instead)

## Inputs

- **topic**: The subject or question to search for in open memory (required)
- **scope**: `narrow` (1-2 targeted reads), `standard` (search + 2-4 reads), or `broad` (list + search + 5+ reads). Default: `standard`

## Retrieval Workflow

1. **Identify memory target** — restate internally what prior knowledge is needed. Be specific: "past decision on multi-agent architecture" not "stuff about agents".
2. **Search vault with narrow terms** — use `vault_search` with focused keywords. Prefer 2-3 word queries. Run multiple searches with varied phrasing if the first yields few results.
3. **Inspect metadata** — use `vault_frontmatter` on promising results to check tags, dates, and status before reading full content. Skip notes that are clearly irrelevant.
4. **Read only the most relevant notes** — use `vault_read` on the top 2-4 matches. Do NOT read the entire vault. Do NOT read notes speculatively.
5. **Synthesize bounded findings** — extract only the facts, decisions, or patterns relevant to the current question. Discard tangential content.
6. **Cite sources** — reference vault note paths/titles in your response so the user can verify.

## Tool Usage Rules

- **Read-only.** Never attempt to write, update, or delete vault notes through this skill.
- **Bounded retrieval.** Never list or read more than 10 notes in a single invocation.
- **Targeted search first.** Always search before listing. Only use `vault_list` when search yields no results and you need to browse a specific folder.
- **Respect human-authored notes.** Vault notes may be written by the human operator. Treat all content as potentially human-authored — do not dismiss, overwrite, or "correct" it.
- **Do not treat vault as runtime truth.** See [safe-source rules](reference/SAFE_SOURCE_RULES.md).
- **Respect folder ownership.** Some folders are human-managed, others Nova-managed. See [note type guide](reference/NOTE_TYPE_GUIDE.md) for folder roles and retrieval implications.
- **No fabrication.** Only report findings that come from actual vault content. Never invent note titles or content.

## Outputs / Contract

Every response using this skill MUST include:

```
## Vault Findings
<synthesized answer with references to vault notes>

## Sources
- `path/to/note.md` — relevance summary
- ...

## Retrieval Log
| # | Tool | Query/Path | Result |
|---|------|------------|--------|
| 1 | vault_search | "multi-agent architecture" | 3 hits |
| 2 | vault_read | "Architecture/decisions.md" | relevant |

## Confidence
<high / medium / low> — <1-sentence justification>
```

## Examples

### Example 1: Recalling a prior architecture decision
**User**: "What did we decide about agent communication patterns?"

**Retrieval log**:
| # | Tool | Query/Path | Result |
|---|------|------------|--------|
| 1 | vault_search | "agent communication" | 2 hits |
| 2 | vault_frontmatter | "Architecture/multi-agent-plan.md" | tags: architecture, agents |
| 3 | vault_read | "Architecture/multi-agent-plan.md" | relevant — hierarchical orchestrator pattern |

**Vault Findings**: Prior decision was to use a hierarchical orchestrator-worker pattern with blackboard state. Direct agent-to-agent communication was rejected in favor of centralized routing. (Source: `Architecture/multi-agent-plan.md`)

**Confidence**: high — single authoritative note with clear decision record.

### Example 2: Searching for a past research summary
**User**: "Did we research anything about MCP server patterns?"

**Retrieval log**:
| # | Tool | Query/Path | Result |
|---|------|------------|--------|
| 1 | vault_search | "MCP server" | 4 hits |
| 2 | vault_search | "MCP patterns" | 1 hit |
| 3 | vault_read | "Research/mcp-server-design.md" | relevant |
| 4 | vault_read | "Research/mcp-tool-policy.md" | tangential |

**Vault Findings**: Research notes cover MCP server design patterns including tool namespacing, resource exposure, and permission boundaries. Key finding: prefer narrow tool surfaces over broad ones. (Sources: `Research/mcp-server-design.md`, `Research/mcp-tool-policy.md`)

**Confidence**: medium — multiple notes found but may be incomplete.
