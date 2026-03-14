---
name: firecrawl-site-crawl
description: >-
  Crawl entire websites or documentation sites to index content. Discovers
  pages via sitemap and link following, then scrapes each as markdown.
  Auto-invoked when the user wants to learn a new framework, index docs,
  audit a site, or extract content from multiple pages on the same domain.
  Also use for 'crawl this site', 'index these docs', 'get all pages from'.
argument-hint: "[url] [--limit N] [--paths pattern]"
allowed-tools:
  - mcp__firecrawl__firecrawl_map
  - mcp__firecrawl__firecrawl_crawl
  - mcp__firecrawl__firecrawl_check_crawl_status
  - mcp__firecrawl__firecrawl_scrape
activation:
  keywords:
    - crawl site
    - index docs
    - crawl
    - sitemap
    - get all pages
---

# Firecrawl Site Crawl

Crawl and index entire websites or documentation sites.

## Workflow

1. **Map first** — `firecrawl_map` to discover all URLs on the target site
   - Use `search` parameter to filter by topic (e.g., "API reference")
   - Review the URL list to scope the crawl
   - Default limit: 100 URLs for mapping
2. **Crawl** — `firecrawl_crawl` with appropriate limits
   - Start small: `limit: 10-20` for initial exploration
   - Use `includePaths` / `excludePaths` to scope (e.g., `/docs/` only)
   - Crawl is **async** — returns a job ID immediately
3. **Poll** — `firecrawl_check_crawl_status` with the job ID
   - Check periodically until status is `completed`
   - Partial results are available while crawling
4. **Process** — analyze the crawled content
   - Summarize key findings
   - Store important discoveries in Fusion Memory
   - Write output to OUTPUT/ if substantive

## Credit Budget

- Map: 1 credit
- Crawl: 1 credit per page crawled
- Documentation site (30 pages): ~31 credits
- Keep initial crawls under 50 pages — expand if needed

## Common Use Cases

- **Learn a framework**: crawl docs → summarize API patterns → store in memory
- **Competitive analysis**: crawl competitor pages → extract features/pricing
- **Site audit**: map all URLs → check for broken patterns → report
- **Knowledge base**: crawl internal wiki → index for future reference

## Safety

- Always map before crawling to understand site size
- Never crawl with limit > 100 without explicit user approval
- Respect robots.txt (Firecrawl does this by default)
- Be aware: large crawls consume credits quickly
