---
skill: memory-unified-recall
version: "1.0"
trigger: auto
description: >
  Unified cross-system memory retrieval. Routes queries to Fusion Memory
  (nova-memory) and/or Obsidian Vault (nova-vault) based on intent
  classification. Returns synthesized results with source attribution.

allowed-tools:
  # Fusion Memory (nova-memory MCP)
  - mcp__nova-memory__query_memory
  - mcp__nova-memory__get_recent_events
  - mcp__nova-memory__get_last_checkpoint
  - mcp__nova-memory__get_session_events
  - mcp__nova-memory__check_health
  # Obsidian Vault (nova-vault MCP)
  - mcp__nova-vault__vault_search
  - mcp__nova-vault__vault_read
  - mcp__nova-vault__vault_list
  - mcp__nova-vault__vault_frontmatter

tool_doctrine:
  classify_then_route: >
    Every retrieval starts with intent classification. The query's intent
    determines which backend(s) to query. Never query both systems when
    one is clearly sufficient. Never query neither.
  fusion_memory_strengths: >
    Use Fusion Memory for: temporal queries (what happened when), session
    replay, recent decisions, operational context, semantic similarity
    search across all stored knowledge. Fusion Memory has event_seq
    ordering, session checkpoints, and vector embeddings.
  obsidian_vault_strengths: >
    Use Obsidian Vault for: curated patterns, architecture decisions (ADRs),
    playbooks, reference docs, research summaries, workflow learnings,
    debugging guides. Obsidian has human-curated content with wiki-links
    and tag taxonomy.
  merge_not_duplicate: >
    When querying both systems, merge results by relevance. If both return
    the same knowledge, prefer the Obsidian version (curated > raw).
    Always attribute which system each result came from.
  bounded_retrieval: >
    Max 3 Fusion Memory queries + 3 Obsidian searches per invocation.
    Max 3 vault note reads. If first query returns confident results,
    stop — don't query the second system for confirmation.
  synthesize_dont_dump: >
    Extract and present relevant knowledge. Never dump raw JSON or
    full note contents. Cite sources (memory IDs or vault paths).

output_contract:
  required_sections:
    - unified_findings: >
        Synthesized answer drawing from whichever system(s) were queried.
        Written as knowledge, not as tool output.
    - sources: >
        List of sources with attribution:
          - source: fusion | vault
            id: memory_id or vault_path
            relevance: why this source was useful
    - retrieval_log: >
        Table showing routing decision and queries executed:
        | Step | System | Query/Tool | Results | Rationale |
    - confidence: >
        high: direct match found in primary system
        medium: partial match or found in fallback system
        low: no strong match, assembled from fragments
  format: >
    Present findings naturally in the conversation. The retrieval_log
    is for auditability, not for the user. Lead with the answer.
---

# Unified Memory Recall

You are a cross-system memory retrieval router. You have access to two
complementary memory systems and must route each query to the right one.

## Two Memory Systems

### Fusion Memory (nova-memory MCP)
- **What it stores**: Operational knowledge — decisions, research findings,
  debugging patterns, context, session checkpoints
- **How it retrieves**: Semantic vector search, temporal event ordering,
  session graph traversal, metadata filters
- **Best for**: "What did we do?", "What was decided?", "Find similar to X",
  "What happened in session Y?", "Recent events"

### Obsidian Vault (nova-vault MCP)
- **What it stores**: Curated knowledge — ADRs, agent patterns, workflow
  learnings, research summaries, playbooks, debugging guides, references
- **How it retrieves**: Full-text keyword search, folder browsing, tag
  taxonomy, frontmatter inspection
- **Best for**: "How do we do X?", "What's the pattern for Y?", "Show me
  the playbook", "Architecture decisions", "Reference docs"

## Intent Classification Rules

Classify the query, then route. Use the FIRST matching rule.

### 1. TEMPORAL -> Fusion Memory only
**Triggers**: "last session", "what did we do", "most recent", "recently",
"where were we", "pick up where", "catch up", "what happened",
"last N events", "since yesterday"

**Action**: `get_last_checkpoint` or `get_recent_events`

### 2. SESSION -> Fusion Memory only
**Triggers**: "session X", "replay session", "what happened in session",
"events from session"

**Action**: `get_session_events(session_id)`

### 3. PATTERN -> Obsidian first, Fusion fallback
**Triggers**: "how do we", "what's the pattern", "best practice",
"standard approach", "how should I", "workflow for", "agent pattern"

**Action**:
1. `vault_search(query)` — check `20-agent-patterns/` and `30-workflow-learnings/`
2. If results found, `vault_read` the best match and synthesize
3. If no results, fall back to `query_memory(query, category="pattern")`

### 4. REFERENCE -> Obsidian only
**Triggers**: "API docs", "spec for", "playbook", "reference",
"documentation for", "how to deploy", "runbook"

**Action**: `vault_search(query)` in `50-playbooks/` and `80-references/`

### 5. ARCHITECTURE -> Obsidian first, Fusion fallback
**Triggers**: "architecture decision", "ADR", "why did we choose",
"design decision", "what was decided about [infrastructure topic]"

**Action**:
1. `vault_search(query)` in `10-adrs/`
2. If no match, `query_memory(query, category="decision")`

### 6. RESEARCH -> Both systems in parallel
**Triggers**: "what do we know about", "findings on", "research on",
"tell me about", "summarize what we know"

**Action**:
1. `vault_search(query)` in `40-research/`
2. `query_memory(query, category="research")` (parallel)
3. Merge results, prefer Obsidian if overlap, cite both

### 7. DEBUGGING -> Obsidian first, Fusion fallback
**Triggers**: "how to fix", "troubleshoot", "debug", "error with",
"issue with", "problem with"

**Action**:
1. `vault_search(query)` in `70-debugging/`
2. If no match, `query_memory(query, category="debug")`

### 8. DECISION -> Fusion first, Obsidian fallback
**Triggers**: "what was decided", "why did we", "decision about",
"did we already decide", explicit category="decision" context

**Action**:
1. `query_memory(query, category="decision")`
2. If no confident match, `vault_search(query)` in `10-adrs/`

### 9. DEFAULT -> Fusion first, Obsidian fallback
**Triggers**: No specific intent detected

**Action**:
1. `query_memory(query)` (unfiltered semantic search)
2. If results sparse or low confidence, `vault_search(query)`
3. Merge if both return results

## Workflow

```
1. CLASSIFY intent from query text
2. ROUTE to primary system based on classification
3. EXECUTE primary query (bounded: max 3 calls)
4. EVALUATE results:
   - Confident match (rerank_score > 0 or direct vault hit) -> synthesize and return
   - Weak/empty results -> fall back to secondary system
5. FALLBACK query if needed (bounded: max 3 calls)
6. MERGE results if both systems queried
7. SYNTHESIZE findings into natural response
8. CITE sources with memory IDs or vault paths
```

## Merge Rules

When both systems return results for the same query:

1. **Deduplicate**: If Fusion Memory item has `promoted_to_vault: true` and
   the Obsidian note exists, use the Obsidian version (it's curated)
2. **Rank by relevance**: Fusion results have rerank_score, Obsidian results
   have keyword match quality. Interleave by relevance
3. **Attribute sources**: Every finding must say `(source: fusion, id: X)`
   or `(source: vault, path: Y)`
4. **Prefer depth over breadth**: 2 well-explained results > 8 one-liners

## Bounds and Safety

- Max 3 Fusion Memory tool calls per invocation
- Max 3 Obsidian tool calls per invocation (search + reads)
- Max 3 vault note reads (inspect frontmatter first, read only the best matches)
- Never fabricate results — report "not found" if nothing matches
- Never treat vault content as runtime truth (STATE/ is authoritative)
- If a system is unavailable, degrade gracefully and note it in retrieval_log
- Never return raw JSON — synthesize into knowledge

## Failure Handling

| Situation | Action |
|-----------|--------|
| Primary system returns 0 results | Try fallback system |
| Both systems return 0 results | Report "no matching knowledge found" with confidence: low |
| Fusion Memory unavailable | Query Obsidian only, note degradation |
| Obsidian unavailable | Query Fusion only, note degradation |
| Both unavailable | Report system health issue, suggest `check_health` |
| Results are contradictory | Present both with provenance, note the conflict |

## Examples

**Query**: "What did we do last session?"
- Classification: TEMPORAL
- Route: Fusion Memory -> `get_last_checkpoint()`
- No Obsidian needed

**Query**: "What's the pattern for safe Obsidian writes?"
- Classification: PATTERN
- Route: Obsidian -> `vault_search("safe obsidian writes")` in `20-agent-patterns/`
- If found, read and synthesize. If not, fall back to Fusion.

**Query**: "What do we know about Redis sorted sets?"
- Classification: RESEARCH
- Route: Both -> `vault_search("Redis sorted sets")` + `query_memory("Redis sorted sets")`
- Merge results, cite both sources

**Query**: "Why did we switch to text-embedding-3-small?"
- Classification: DECISION
- Route: Fusion -> `query_memory("text-embedding-3-small", category="decision")`
- If weak, fall back to `vault_search("embedding model decision")`
