---
name: vault-graph-hygiene
description: >
  Audit the Obsidian vault's knowledge graph health: detect orphan notes,
  stale MOCs, missing domain tags, and domain clusters without MOCs.
  Produces an actionable health report written to 00-inbox/. Invoke
  weekly via heartbeat or manually to maintain vault connectivity.
activation:
  keywords:
    - vault hygiene
    - graph health
    - orphan notes
    - vault audit
    - graph hygiene
    - stale moc
allowed-tools:
  - mcp__nova-vault__vault_list
  - mcp__nova-vault__vault_read
  - mcp__nova-vault__vault_search
  - mcp__nova-vault__vault_frontmatter
  - mcp__nova-vault__vault_validate
  - mcp__nova-vault__vault_write

tool_doctrine:
  sample_dont_exhaust: >
    Do not read every note in the vault. Use vault_search and vault_list
    to sample and estimate. The goal is a useful health report, not a
    complete census.
  actionable_output: >
    Every issue in the report must include a suggested action. "Note X
    is orphaned" is not enough — suggest which MOC to link it to or
    which domain tag to add.
  bounded_execution: >
    Max 10 tool calls per invocation. Produce the best report possible
    within this budget.
  write_report: >
    Always write the health report to 00-inbox/ as a vault note. This
    creates a historical record of vault health over time.

output_contract:
  required_sections:
    - hygiene_result: >
        - orphan_count: <number of notes with no wikilinks>
          tag_coverage: <percentage with #domain/* tags>
          stale_mocs: <count of MOCs not updated in 30+ days>
          missing_mocs: <domains with 5+ notes but no MOC>
          report_path: <vault path of written report>
  format: >
    Summary stats plus the full report is in Obsidian.
---

# Vault Graph Hygiene

Audit vault connectivity and produce an actionable health report.

## When to Use

- Weekly maintenance (triggered by heartbeat or manual)
- After a batch of new notes have been written
- When you suspect vault connectivity has degraded
- After generating new MOCs to verify they improved the graph

## When NOT to Use

- For one-off note creation — use the appropriate writing skill
- For reading vault content — use reading-obsidian-memory
- For fixing individual notes — use vault_update directly

## Workflow

```
1. INVENTORY WRITABLE FOLDERS
   - vault_list for each writable folder: 20-agent-patterns,
     30-workflow-learnings, 40-research, 70-debugging, 00-inbox
   - Count total notes per folder

2. SAMPLE RECENT NOTES
   - vault_search for recent notes (last 7-14 days)
   - Check each result for:
     a. Presence of wikilinks in body (any [[...]] pattern)
     b. Presence of related: field in frontmatter
     c. Presence of up:: field in body
     d. Presence of #domain/* tag
     e. Presence of #project/* tag

3. DETECT ORPHANS
   - From the sample, identify notes with:
     - No wikilinks in body AND
     - No related: entries in frontmatter AND
     - No up:: field
   - These are "orphan" notes with no graph connections

4. CHECK MOC HEALTH
   - vault_search for "moc-" to find all MOCs
   - For each MOC found:
     - vault_frontmatter to get date_created and domain
     - Estimate staleness (compare date to today)
   - Identify MOCs not updated in 30+ days

5. IDENTIFY MISSING MOCs
   - For each valid domain (novatrade, autonomy, memory,
     infrastructure, agents, risk, operations, research,
     debugging, trading-strategies):
     - Check if a moc-<domain> exists
     - vault_search to estimate note count for that domain
     - If 5+ notes exist but no MOC → flag as "missing MOC"

6. COMPILE HEALTH REPORT
   - Calculate metrics:
     - Orphan count and percentage
     - Tag coverage (% with #domain/*)
     - MOC coverage (domains with MOCs vs without)
     - Stale MOC count
   - Generate suggested actions for each issue

7. WRITE REPORT
   - vault_validate the report note
   - vault_write to 00-inbox/vault-health-<YYYY-MM-DD>.md
```

## Health Report Template

### Frontmatter
```yaml
---
type: inbox
title: "Vault Health Report: <YYYY-MM-DD>"
date: "<YYYY-MM-DD>"
source: nova-core-memory
tags:
  - "#type/inbox"
  - "#action/review"
  - "#domain/operations"
  - "#project/nova-core"
---
```

### Body
```markdown
up:: [[moc-operations]]

## Vault Health Summary

| Metric | Value | Status |
|--------|-------|--------|
| Total notes (writable) | <count> | — |
| Orphan notes | <count> (<pct>%) | <ok/warn/critical> |
| Notes with #domain/* tag | <count> (<pct>%) | <ok/warn> |
| Notes with up:: field | <count> (<pct>%) | <ok/warn> |
| Active MOCs | <count>/<total domains> | <ok/warn> |
| Stale MOCs (30+ days) | <count> | <ok/warn> |

## Orphan Notes

Notes with no wikilinks, no related: field, and no up:: field:

- [[orphan-note-1]] — Suggested: link to [[moc-<domain>]]
- [[orphan-note-2]] — Suggested: add #domain/<domain> tag

## Missing MOCs

Domains with 5+ notes but no MOC:

| Domain | Note Count | Suggested Action |
|--------|-----------|------------------|
| <domain> | <count> | Generate moc-<domain> |

## Stale MOCs

MOCs not updated in 30+ days:

- [[moc-<domain>]] — Last updated: <date>. Action: run updating-vault-moc

## Tag Coverage Gaps

Notes missing #domain/* tags:

- [[note-without-domain-tag]] — Suggested: #domain/<inferred>

## Suggested Actions

1. Generate MOCs for: <list of missing MOC domains>
2. Update stale MOCs: <list>
3. Backfill links on orphan notes: <count> notes
4. Add domain tags to: <count> notes

## Related Notes

- [[previous-vault-health-report]] — prior health report for comparison
```

## Thresholds

| Metric | OK | Warn | Critical |
|--------|----|------|----------|
| Orphan % | <20% | 20-50% | >50% |
| #domain/* coverage | >80% | 50-80% | <50% |
| MOC coverage | >70% domains | 40-70% | <40% |
| Stale MOCs | 0 | 1-2 | 3+ |

## Rules

1. **Sample, don't census.** Use search results, not exhaustive reads.
2. **Actionable suggestions.** Every issue includes a fix recommendation.
3. **Write the report.** Always persist to 00-inbox/ for history.
4. **Max 10 tool calls.** Work within the budget.
5. **Compare to prior reports.** If a previous health report exists, note trends.
6. **Don't fix — report.** This skill audits; it doesn't modify notes.

## Failure Handling

| Situation | Action |
|-----------|--------|
| Vault unavailable | Abort with health warning |
| Too few notes to sample | Write minimal report noting sparse vault |
| No MOCs exist yet | Note all domains as "missing MOC" |
| vault_write fails | Report error, output results to console |
| Rate limit hit | Write partial report with available data |
