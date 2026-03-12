---
name: firecrawl-deep-research
description: >-
  Deep web research using Firecrawl search + full-page scraping. Unlike
  web-research (Brave/Tavily snippets), this retrieves complete page content
  from search results. Auto-invoked when research needs full article text,
  comprehensive analysis across sources, or when snippets are insufficient.
  Also use when the user says 'deep research', 'thorough search', or needs
  content from multiple full pages.
argument-hint: "[topic or question]"
tools:
  - mcp__firecrawl__firecrawl_search
  - mcp__firecrawl__firecrawl_scrape
---

# Firecrawl Deep Research

Search the web and scrape full page content for comprehensive research.

## When to Use This vs Other Tools

| Need | Tool |
|------|------|
| Quick factual answer | web-research (Brave/Tavily) |
| Full article content from search results | **this skill** |
| Known URL, just fetch it | http-fetch or firecrawl_scrape |
| Entire documentation site | firecrawl-site-crawl |
| Structured data from pages | firecrawl-extract |

## Workflow

1. **Search** — `firecrawl_search` with a focused query
   - Start WITHOUT `scrapeOptions` to get results fast
   - Use `limit: 5-10` to conserve credits
   - Use search operators (`site:`, `intitle:`, `""`) to narrow results
2. **Evaluate** — review titles/descriptions, pick the 2-3 most relevant URLs
3. **Scrape** — `firecrawl_scrape` each selected URL
   - Use `formats: ["markdown"]` + `onlyMainContent: true` for articles
   - Use `formats: ["json"]` + `jsonOptions` when extracting specific data points
   - Add `waitFor: 5000` for JS-heavy pages (SPAs, dashboards)
4. **Synthesize** — combine findings with source attribution

## Credit Budget

- Search: 1 credit per query
- Scrape: 1 credit per URL
- Typical deep research: 3-8 credits (1 search + 2-7 scrapes)
- Stay under 10 credits per research task unless user approves more

## Known Pitfalls

- Rate limit (429): back off and retry, or use batch scrape for many URLs
- JS-heavy pages: add `waitFor: 3000-5000` before giving up
- Paywalled content: scrape may return partial content — note this in output
- Don't scrape search result pages themselves — scrape the actual target URLs
