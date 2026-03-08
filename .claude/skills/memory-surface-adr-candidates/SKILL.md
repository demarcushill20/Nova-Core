---
skill: memory-surface-adr-candidates
version: "1.0"
trigger: manual
description: >
  Surfaces architectural decisions from Fusion Memory that lack matching
  ADRs in Obsidian. Writes candidate ADR notes to 00-inbox/ for operator
  review. Invoke periodically or at session end to bridge decisions into
  the ADR lifecycle.

allowed-tools:
  # Fusion Memory — find decisions
  - mcp__nova-memory__query_memory
  - mcp__nova-memory__get_recent_events
  # Obsidian Vault — dedup against existing ADRs, write candidates
  - mcp__nova-vault__vault_search
  - mcp__nova-vault__vault_validate
  - mcp__nova-vault__vault_write
  - mcp__nova-vault__vault_list

tool_doctrine:
  decisions_only: >
    Only surface items with memory_type="decision" or category="decision".
    Context, scratch, and research items are not ADR candidates.
  dedup_is_mandatory: >
    Always vault_search for the decision topic in 10-adrs/ before writing.
    If a matching ADR already exists, skip and report "already covered".
    Also check 00-inbox/ to avoid duplicate candidates.
  inbox_not_adrs: >
    Never write to 10-adrs/ — that folder is operator-managed. Always
    write candidates to 00-inbox/ with #action/review tag.
  group_related: >
    If multiple decisions relate to the same topic (e.g., 3 decisions
    about embedding models), group them into a single ADR candidate
    rather than creating 3 separate inbox notes.
  bounded_execution: >
    Max 8 tool calls per invocation. Surface at most 3 ADR candidates
    per invocation to keep context manageable.
  preserve_provenance: >
    Every candidate must include the Fusion Memory IDs, session_ids,
    and event_seq numbers where the decision was recorded.

output_contract:
  required_sections:
    - surfacing_results: >
        List of decisions evaluated and their outcome:
          - decision: title
            status: surfaced | skipped | already_covered | grouped
            vault_path: (if surfaced)
            reason: (if skipped)
    - decisions_evaluated: count
    - candidates_surfaced: count
  format: >
    Brief summary. Candidates are in Obsidian 00-inbox/ for operator review.
---

# Decision-to-ADR Bridge

Surface unmatched architectural decisions from Fusion Memory as ADR
candidates in Obsidian's inbox for operator review.

## Why This Exists

Decisions get stored in Fusion Memory during work sessions with
`category: "decision"`. But the `10-adrs/` folder in Obsidian — the
canonical ADR registry — is operator-managed. Agents cannot write ADRs
directly. This skill bridges the gap by surfacing decision candidates
into `00-inbox/` where the operator can review, refine, and promote
them to proper ADRs.

## What Makes a Decision an ADR Candidate

A Fusion Memory decision is worth surfacing when ALL of these are true:

1. **Architectural scope**: Affects system structure, tool selection,
   data model, or integration approach (not just "chose variable name X")
2. **Durable impact**: The decision constrains future work (not a
   one-time tactical choice)
3. **No existing ADR**: No matching note in `10-adrs/` already covers it
4. **No pending candidate**: No matching note in `00-inbox/` already
   surfaces it
5. **Not trivial**: Contains enough context to justify a formal ADR

### Decision Significance Filter

| Significant (surface) | Not significant (skip) |
|----------------------|----------------------|
| "Chose Pinecone over Weaviate for vectors" | "Used print() for debugging" |
| "MCP transport must use stdio, not HTTP" | "Named the variable `seq`" |
| "Redis sorted sets for timeline, not lists" | "Ran tests with -v flag" |
| "Single Pinecone index with project metadata" | "Used Python 3.10 syntax" |

## Workflow

```
1. DISCOVER DECISIONS
   - query_memory("architectural decisions", category="decision", top_k_final=15)
   - Also: get_recent_events(n=30, memory_type="decision")
   - Merge and deduplicate by content similarity

2. FILTER FOR SIGNIFICANCE
   - Apply the significance filter (above)
   - Skip trivial, tactical, or ephemeral decisions
   - Group related decisions about the same topic

3. DEDUP AGAINST EXISTING ADRs
   - For each candidate: vault_search(decision_topic) in 10-adrs/
   - If matching ADR exists -> skip (already covered)
   - Also vault_search in 00-inbox/ for pending candidates

4. COMPOSE CANDIDATE NOTE
   - Frontmatter: type=inbox, ADR candidate tags
   - Body: Proposed Decision, Context, Alternatives, Provenance
   - Path: 00-inbox/adr-candidate-<slug>.md

5. VALIDATE
   - vault_validate(frontmatter) before writing
   - Fix and retry once if invalid

6. WRITE TO INBOX
   - vault_write(path, frontmatter, body)
   - Report success with vault path
```

## ADR Candidate Note Template

### Frontmatter

```yaml
---
type: inbox
title: "ADR Candidate: <Decision Title>"
date: "<YYYY-MM-DD>"
source: nova-core-memory
tags:
  - "#type/inbox"
  - "#action/review"
  - "#action/promote-to-adr"
  - "#project/<project>"
---
```

### Body

```markdown
## Proposed Decision

<Clear statement of the decision that was made>

## Context

<Why this decision was needed. What problem it solves.>

## Alternatives Considered

- **<Alternative 1>** — why it was rejected or not chosen
- **<Alternative 2>** — why it was rejected or not chosen
(Include if available from Fusion Memory context. Write "Not recorded"
if the original decision didn't capture alternatives.)

## Consequences

- <Positive consequence>
- <Negative consequence or trade-off>
(Infer from the decision content. Keep brief.)

## Source Evidence

- Fusion Memory ID: <memory_id> (event_seq: N, session: <session_id>)
- First recorded: <session_id> on <date>
- Related decisions: <list of related memory_ids, if any>
- Surfaced from Fusion Memory on <today's date>

## Action Needed

Operator: Review this candidate. If it represents a durable architectural
decision, promote to `10-adrs/` with a proper ADR number. If it's too
tactical or already covered, dismiss by removing from inbox.
```

## Rules

1. **Never write to `10-adrs/`.** That folder is operator-managed.
2. **Always write to `00-inbox/`.** Use `#action/promote-to-adr` tag.
3. **Never create duplicates.** Always dedup against both `10-adrs/`
   and `00-inbox/` before writing.
4. **Always validate before writing.** `vault_validate` is mandatory.
5. **Group related decisions.** 3 decisions about embeddings = 1 candidate.
6. **Always include Source Evidence.** Traceability to Fusion Memory IDs.
7. **Max 3 candidates per invocation.** Keep bounded.
8. **Max 8 tool calls per invocation.** Fail gracefully if limit approached.
9. **Never fabricate alternatives.** If the original decision didn't record
   alternatives, write "Not recorded" — don't invent them.

## Failure Handling

| Situation | Action |
|-----------|--------|
| No decision items found | Report "no decision items in Fusion Memory" |
| All decisions already covered by ADRs | Report "all decisions have matching ADRs" |
| All decisions too tactical | Report "no ADR-worthy decisions found" |
| Candidate already in inbox | Report "already surfaced" with path |
| vault_validate fails | Fix frontmatter, retry once, then skip |
| vault_write fails | Report error, do not retry, continue to next |
| Fusion Memory unavailable | Abort — cannot discover decisions without it |
| Obsidian unavailable | Abort — cannot dedup or write candidates |

## Example

**Fusion Memory decision**:
```
id: core-embedding-selection
content: "Selected text-embedding-3-small as the embedding model for
  Fusion Memory MCP. 5x cheaper than ada-002 ($0.02 vs $0.10 per 1M
  tokens), better benchmark performance, same 1536 dimensions."
metadata:
  category: decision
  memory_type: decision
  session_id: bootstrap-memory
  event_seq: 12
  project: fusion-memory
```

**Dedup check**: vault_search("embedding model") in `10-adrs/` ->
no match found.

**Surfaced as**:
```
00-inbox/adr-candidate-embedding-model-selection.md
```

**With body including**:
- Proposed Decision: Use text-embedding-3-small for all vector embeddings
- Context: Cost and performance comparison with ada-002
- Alternatives: ada-002 (more expensive, lower benchmarks)
- Source: bootstrap-memory session, event_seq 12
