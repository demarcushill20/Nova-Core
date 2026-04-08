---
name: memory-promote-pattern
description: >
  Promotes stable patterns from Fusion Memory to Obsidian agent-pattern
  notes. A pattern is "stable" when it appears across 2+ sessions and
  has not been contradicted. Runs the full safety audit before writing.
  Invoke at session end or periodically to bridge operational memory
  into curated knowledge.
activation:
  keywords:
    - promote pattern
    - stable pattern
    - promote to vault
    - bridge pattern
allowed-tools:
  # Fusion Memory — find and verify patterns
  - mcp__nova-memory__query_memory
  - mcp__nova-memory__get_session_events
  - mcp__nova-memory__get_recent_events
  - mcp__nova-memory__upsert_memory
  # Obsidian Vault — dedup, validate, write
  - mcp__nova-vault__vault_search
  - mcp__nova-vault__vault_validate
  - mcp__nova-vault__vault_write
  - mcp__nova-vault__vault_read

tool_doctrine:
  stability_over_speed: >
    Never promote a pattern from a single session. Stability requires
    evidence from 2+ distinct session_ids. A pattern seen once is a
    candidate; seen twice is promotable; seen three times is high
    confidence.
  dedup_is_mandatory: >
    Always vault_search for the pattern title and pattern_id before
    writing. If a similar pattern already exists in 20-agent-patterns/,
    do NOT create a duplicate — report "already exists" instead.
  audit_before_write: >
    Every promotion must pass the safety audit checklist before
    vault_write. Validate frontmatter with vault_validate. Check for
    secrets, runtime state, and provenance violations.
  mark_promoted: >
    After successful vault_write, upsert the Fusion Memory item with
    metadata.promoted_to_vault=true and metadata.vault_path set to
    the Obsidian note path. This prevents re-promotion.
  preserve_provenance: >
    The Obsidian note must include Source Evidence linking back to
    the original Fusion Memory IDs and session_ids where the pattern
    was observed.
  bounded_execution: >
    Max 10 tool calls per invocation. Promote at most 3 patterns per
    invocation to keep context manageable.

output_contract:
  required_sections:
    - promotion_results: >
        List of patterns evaluated and their outcome:
          - pattern: title
            status: promoted | skipped | already_exists | insufficient_evidence
            vault_path: (if promoted)
            reason: (if skipped)
    - patterns_evaluated: count
    - patterns_promoted: count
  format: >
    Brief summary. Details in Obsidian and Fusion Memory.
---

# Pattern Promotion Pipeline

Promote stable patterns from Fusion Memory into curated Obsidian
agent-pattern notes.

## What Makes a Pattern "Stable"

A pattern is promotable when ALL of these are true:

1. **Multi-session**: Appears in 2+ distinct session_ids in Fusion Memory
2. **Categorized**: Has `category: "pattern"` or `memory_type: "pattern"` in metadata
3. **Not contradicted**: No later event explicitly overrides or reverses it
4. **Not already promoted**: Does not have `promoted_to_vault: true` in metadata
5. **Actionable**: Contains concrete, repeatable guidance (not just an observation)

### Confidence Mapping

| Sessions observed | Confidence |
|-------------------|------------|
| 2 sessions | medium |
| 3+ sessions | high |

## Workflow

```
1. DISCOVER CANDIDATES
   - query_memory("patterns", category="pattern", top_k_final=15)
   - Also check: get_recent_events(n=30, memory_type="pattern")
   - Filter out items with promoted_to_vault=true in metadata

2. VERIFY STABILITY (for each candidate)
   - Check session_id in the candidate's metadata
   - Search for similar content across other sessions:
     query_memory(candidate_content, top_k_final=5)
   - Count distinct session_ids in results
   - If < 2 distinct sessions -> skip (insufficient evidence)
   - If contradicted by a later event -> skip (superseded)

3. DEDUP CHECK AND LINK HARVESTING
   - vault_search(pattern_title) in 20-agent-patterns/
   - If similar pattern exists -> skip (already exists)
   - Harvest 2-3 related (but non-duplicate) notes from results
   - Record filenames for related: field and ## Related Notes section

4. CLASSIFY AND COMPOSE
   - Determine agent_role from pattern content:
     - Code/implementation patterns -> "coder"
     - Research/search patterns -> "research"
     - Review/quality patterns -> "critic"
     - Architecture/planning patterns -> "planner"
     - Memory/retrieval patterns -> "memory"
     - Verification/testing patterns -> "verifier"
   - Determine task_classes from pattern context:
     - "research", "code_impl", "code_review", "system", "simple"
   - Generate pattern_id: "ap-<descriptive-slug>"
   - Compose frontmatter and body following the schema below

5. VALIDATE
   - vault_validate(frontmatter)
   - If invalid, fix and retry once

6. WRITE TO OBSIDIAN
   - vault_write("20-agent-patterns/<slug>.md", frontmatter, body)

7. MARK AS PROMOTED IN FUSION MEMORY
   - upsert_memory(content, id=original_id, metadata={
       ...original_metadata,
       promoted_to_vault: true,
       vault_path: "20-agent-patterns/<slug>.md"
     })
```

### Domain Inference

Infer the primary domain from pattern content keywords:
- trade, strategy, backtest, IRB, MT5, execution → `novatrade`
- autonomy, heartbeat, decision engine, guardrail → `autonomy`
- memory, fusion, pinecone, neo4j, vault, recall → `memory`
- systemd, circuit breaker, self-heal, deploy, nginx → `infrastructure`
- agent, spawner, orchestrator, multi-agent → `agents`
- risk, gate, filter, drawdown, exposure → `risk`
- Default: `operations`

## Agent-Pattern Note Schema

### Frontmatter (all required)

```yaml
---
type: agent-pattern
pattern_id: "ap-<descriptive-slug>"
title: "<Pattern Title>"            # max 100 chars
agent_role: "<role>"                 # research|coder|critic|verifier|planner|memory
confidence: "<level>"               # high|medium|low (based on session count)
task_classes:                        # 1+ items
  - "<class>"                        # research|code_impl|code_review|system|simple
date_created: "<YYYY-MM-DD>"
source: "nova-core-memory"           # MUST be this value for tool writes
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
```

### Body (all sections required)

```markdown
up:: [[moc-<inferred-domain>]]

## Summary

1-2 sentence description of the pattern. Written for a future agent
that has never seen the original work.

## When to Apply

- Trigger condition 1
- Trigger condition 2

## Pattern Steps

1. **Step name** — description
2. **Step name** — description
3. **Step name** — description

## Success Indicators

- Observable outcome that confirms the pattern worked
- Another success indicator

## Failure Modes

- **Failure name** — how this pattern can be misapplied
- **Failure name** — boundary condition to watch for

## Guidance

Concrete, actionable advice. This is the most important section.
Write for an agent that will follow these instructions literally.

## Related Notes

- [[related-note-1]] — <brief annotation>
- [[related-note-2]] — <brief annotation>
(Populated from vault_search dedup results. Omit if none found.)

## Source Evidence

- Fusion Memory ID: <memory_id> (event_seq: N, session: <session_id>)
- First observed: <session_id_1> on <date>
- Confirmed in: <session_id_2> on <date>
- Promoted from Fusion Memory on <today's date>
```

## Rules

1. **Never promote from a single session.** Minimum 2 distinct session_ids.
2. **Never create duplicates.** Always dedup check before writing.
3. **Never skip the safety audit.** vault_validate is mandatory.
4. **Never use `source: "operator"`.** Tool writes MUST use `"nova-core-memory"`.
5. **Always mark promoted items.** upsert back to Fusion with promoted_to_vault=true.
6. **Always include Source Evidence.** Traceability back to Fusion Memory IDs.
7. **Max 3 promotions per invocation.** Keep cognitive load bounded.
8. **Max 10 tool calls per invocation.** Fail gracefully if limit approached.

## Failure Handling

| Situation | Action |
|-----------|--------|
| No pattern candidates found | Report "no promotable patterns" |
| All candidates from single session | Report "insufficient cross-session evidence" |
| Pattern already in Obsidian | Report "already exists" with vault path |
| vault_validate fails | Fix frontmatter, retry once, then skip |
| vault_write fails | Report error, do not retry, continue to next |
| Fusion Memory unavailable | Abort — cannot verify stability without it |
| Obsidian unavailable | Abort — cannot dedup or write |

## Example Promotion

**Fusion Memory item**:
```
id: core-mcp-stdout-lesson
content: "MCP stdio transport critical lesson: logging.basicConfig()
  must be called BEFORE importing libraries that register their own
  loggers. Root logger must be set to WARNING. All logging must go
  to stderr — any stdout pollution breaks JSON-RPC protocol."
metadata:
  category: pattern
  memory_type: pattern
  session_id: bootstrap-memory
  event_seq: 25
```

**Stability check**: Found in session `bootstrap-memory` (event_seq 25)
and referenced in session `test-session-alpha` (event_seq 8).
2 sessions -> confidence: medium.

**Promoted to Obsidian as**:
```
20-agent-patterns/mcp-stdio-logging-order.md
```

**Fusion Memory updated with**:
```
promoted_to_vault: true
vault_path: "20-agent-patterns/mcp-stdio-logging-order.md"
```
