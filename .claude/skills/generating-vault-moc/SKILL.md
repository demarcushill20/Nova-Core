---
name: generating-vault-moc
description: >
  Generate a Map of Content (MOC) note for a given knowledge domain.
  MOCs serve as hub notes that link to all related notes in a domain,
  turning the vault from a flat filing cabinet into a navigable
  knowledge graph. Invoke when a domain has 5+ notes and no existing MOC.
activation:
  keywords:
    - generate moc
    - create moc
    - map of content
    - domain index
    - build moc
allowed-tools:
  - mcp__nova-vault__vault_search
  - mcp__nova-vault__vault_read
  - mcp__nova-vault__vault_frontmatter
  - mcp__nova-vault__vault_validate
  - mcp__nova-vault__vault_write

tool_doctrine:
  minimum_density: >
    Do not create a MOC for a domain with fewer than 5 related notes.
    A sparse MOC adds noise without navigation value. Report "domain
    too sparse" and stop.
  no_duplicate_mocs: >
    Always vault_search for an existing MOC (moc-<domain>) before
    creating. If one exists, report "use updating-vault-moc instead"
    and stop.
  annotated_links: >
    Every wikilink in the MOC must include a brief annotation explaining
    why that note is in this MOC. Bare links without context are not
    useful for navigation.
  read_before_annotate: >
    Read 3-5 of the most important notes to write accurate annotations.
    Do not guess what a note contains from its title alone.
  bounded_execution: >
    Max 10 tool calls per invocation. If approaching the limit, write
    the MOC with whatever notes have been gathered so far.

output_contract:
  required_sections:
    - moc_result: >
        - domain: <domain name>
          status: created | skipped
          vault_path: (if created)
          notes_linked: count
          reason: (if skipped)
    - domain: The domain this MOC covers
    - notes_linked: Number of notes linked in the MOC
  format: >
    Brief summary. The MOC itself is in Obsidian.
---

# Generating a Vault MOC

Create a Map of Content (MOC) hub note for a knowledge domain, linking
to all relevant notes with annotated wikilinks.

## When to Use

- A knowledge domain (novatrade, autonomy, memory, etc.) has 5+ notes in the vault
- No MOC exists yet for that domain
- You want to improve vault navigability by creating a hub note
- The graph hygiene skill identified a domain cluster without a MOC

## When NOT to Use

- The domain has fewer than 5 notes — too sparse for a useful MOC
- A MOC already exists — use `updating-vault-moc` instead
- You want to edit an existing MOC — use `updating-vault-moc` instead
- You want to create a note that isn't a MOC — use the appropriate note skill

## Inputs

- **domain**: The knowledge domain to create a MOC for. Required.
  Valid domains: novatrade, infrastructure, memory, autonomy, research,
  debugging, agents, risk, trading-strategies, operations
- **focus** (optional): Specific sub-topic focus within the domain

## Workflow

```
1. VALIDATE DOMAIN
   - Confirm domain is in the valid domain list
   - If invalid, report error and stop

2. CHECK FOR EXISTING MOC
   - vault_search("moc-<domain>")
   - If found, report "MOC already exists — use updating-vault-moc" and stop

3. DISCOVER DOMAIN NOTES
   - vault_search with domain keywords (2-3 searches if needed)
   - Collect all matching notes across writable folders
   - If fewer than 5 results, report "domain too sparse for MOC" and stop

4. READ KEY NOTES
   - vault_read 3-5 of the most important/central notes
   - Extract key themes, relationships, and note purposes
   - Use this to write accurate annotations

5. COMPOSE MOC NOTE
   - Build frontmatter with moc type, domain, tags
   - Organize notes into logical sections (Key Notes, Patterns, etc.)
   - Write annotated wikilinks for each note
   - Add Open Questions section for future work

6. VALIDATE
   - vault_validate the composed note
   - Fix any validation errors

7. WRITE
   - vault_write to 00-inbox/moc-<domain>.md
   - Report success with vault path and note count
```

## MOC Note Template

### Frontmatter
```yaml
---
type: moc
moc_id: "moc-<domain>"
title: "MOC: <Domain Display Name>"
domain: "<domain>"
date_created: "<YYYY-MM-DD>"
source: "nova-core-memory"
tags:
  - "#type/moc"
  - "#domain/<domain>"
  - "#status/active"
  - "#project/nova-core"
related:
  - "[[related-moc]]"
---
```

### Body
```markdown
up:: [[novacore-memory-index]]

## Overview

<1-2 sentences describing this domain's scope and why it matters>

## Key Notes

- [[note-1]] — <annotation: what this note covers and why it's important>
- [[note-2]] — <annotation>
- [[note-3]] — <annotation>

## Patterns

- [[ap-relevant-pattern]] — <annotation: what pattern this codifies>
(Include agent-pattern notes related to this domain)

## Learnings

- [[wl-relevant-learning]] — <annotation: key insight from this learning>
(Include workflow-learning notes related to this domain)

## Research

- [[rs-relevant-research]] — <annotation: what was researched>
(Include research-summary notes related to this domain)

## Open Questions

- <question for future investigation>
- <unresolved design decision>

## Sub-Topics

- [[moc-sub-topic]] — <annotation> (if applicable, link to sub-domain MOCs)
```

## Rules

1. **Minimum 5 notes.** Do not create a MOC for sparse domains.
2. **No duplicates.** Always check for existing MOC first.
3. **Annotated links.** Every `[[wikilink]]` must have a `—` annotation.
4. **Read before annotate.** Read key notes; don't guess from titles.
5. **Validate before write.** vault_validate is mandatory.
6. **Write to 00-inbox/.** MOCs go to inbox for operator review.
7. **Max 10 tool calls.** Fail gracefully if limit approached.
8. **Include up:: field.** All MOCs point up to the top-level index.

## Domain Keyword Map

| Domain | Search Keywords |
|--------|----------------|
| novatrade | trade, strategy, backtest, IRB, MT5, execution, FTMO |
| autonomy | autonomy, heartbeat, decision engine, guardrail, scoring |
| memory | memory, fusion, pinecone, neo4j, vault, retrieval |
| infrastructure | systemd, circuit breaker, self-healing, deploy, nginx |
| agents | agent, spawner, orchestrator, multi-agent, runtime |
| risk | risk, gate, filter, drawdown, exposure, position |
| research | research, search, evaluation, benchmark, method |
| debugging | debug, troubleshoot, error, fix, diagnosis |
| trading-strategies | strategy, indicator, signal, entry, exit, backtest |
| operations | operations, weekly, digest, session, diary |

## Failure Handling

| Situation | Action |
|-----------|--------|
| Domain not in valid list | Report error with valid domains |
| MOC already exists | Report "use updating-vault-moc" with path |
| Fewer than 5 notes | Report "domain too sparse for MOC" |
| vault_validate fails | Fix frontmatter, retry once, then skip |
| vault_write fails | Report error, do not retry |
| Vault unavailable | Abort with health warning |
