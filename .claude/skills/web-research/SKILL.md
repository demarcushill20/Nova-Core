---
name: web-research
description: "Fast, reliable web research using Brave Search and Tavily with citations, source quality scoring, and reproducible query logging. Auto-invoked when tasks require finding current information, comparing sources, or answering factual questions."
disable-model-invocation: false
allowed-tools:
  - mcp__brave-search__brave_web_search
  - mcp__brave-search__brave_news_search
  - mcp__tavily__tavily_search
  - mcp__tavily__tavily_research
---

# Web Research

## When to use
- Answering factual questions that need current or verified data
- Comparing information across multiple sources
- Finding official documentation, release notes, changelogs
- Investigating errors, CVEs, or library compatibility
- News and current events lookup

## Inputs
- **query**: The research question or topic (required)
- **scope**: `quick` (1-2 queries), `standard` (3-4 queries), or `deep` (5-6 queries). Default: `standard`
- **source_preference**: `official`, `community`, or `any`. Default: `official`

## Workflow

1. **Clarify internally** — restate the question. Do NOT ask the user unless genuinely ambiguous.
2. **Generate queries** — produce 3-6 targeted search strings. Vary phrasing and specificity.
3. **Execute searches** — run queries across Brave and Tavily in parallel where possible.
   - Use `brave_web_search` for broad coverage and recency.
   - Use `tavily_search` for relevance-ranked results with content snippets.
   - Use `brave_news_search` when the topic is time-sensitive.
   - Use `tavily_research` only for deep multi-source synthesis (scope=deep).
4. **Score sources** — apply the [source quality rubric](reference/SOURCES_RUBRIC.md).
5. **Synthesize** — merge findings, resolve conflicts, note gaps.
6. **Format output** — use the contract headings below.

## Tool usage rules
- Always run at least 2 queries with different phrasing.
- Prefer Brave for breadth, Tavily for depth. Use both when scope >= standard.
- Never fabricate URLs. Only cite URLs returned by search tools.
- Include the search query text in the query log so results are reproducible.

## Outputs / contract

Every response MUST contain these headings:

```
## Findings
<synthesized answer with inline citations as [Source Title](URL)>

## Sources
- [Title](URL) — relevance / quality note
- ...

## Query log
| # | Engine | Query | Why |
|---|--------|-------|-----|
| 1 | brave  | "..." | ... |

## Confidence
<high / medium / low> — <1-sentence justification>

## Next actions
- <suggested follow-ups if confidence < high, or "None">
```

## Examples

### Example 1: Quick factual lookup
**User**: "What Python version does the latest Django require?"

**Query log**:
| # | Engine | Query | Why |
|---|--------|-------|-----|
| 1 | brave | `Django latest version Python requirement 2026` | official requirement |
| 2 | tavily | `Django 6 minimum Python version` | confirm with community |

**Findings**: Django 6.0 requires Python 3.12+. ([Django docs](https://docs.djangoproject.com/en/6.0/faq/install/))

**Confidence**: high — confirmed by official docs and multiple community sources.

### Example 2: Investigating an error
**User**: "Why does `libatk-1.0.so.0` fail to load on headless Ubuntu?"

**Query log**:
| # | Engine | Query | Why |
|---|--------|-------|-----|
| 1 | brave | `libatk-1.0.so.0 cannot open shared object headless Ubuntu` | exact error |
| 2 | tavily | `playwright chromium missing system dependencies Ubuntu 22.04` | root cause |
| 3 | brave | `install libatk without sudo user local lib` | workaround |

**Findings**: Chromium requires GTK/ATK system libraries. On headless servers without sudo, extract .deb packages to a local path and set `LD_LIBRARY_PATH`.

**Confidence**: high — verified by Playwright docs and direct testing.
