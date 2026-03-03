---
name: research-to-action
description: "Workflow skill chaining web-research, http-fetch, and browser-automation with a strict decision tree for tool escalation. Invoke explicitly via /research-to-action."
disable-model-invocation: true
allowed-tools:
  - mcp__brave-search__brave_web_search
  - mcp__brave-search__brave_news_search
  - mcp__tavily__tavily_search
  - mcp__tavily__tavily_research
  - mcp__fetch__fetch
  - mcp__tavily__tavily_extract
  - mcp__playwright__browser_navigate
  - mcp__playwright__browser_snapshot
  - mcp__playwright__browser_click
  - mcp__playwright__browser_type
  - mcp__playwright__browser_evaluate
  - mcp__playwright__browser_wait_for
  - mcp__playwright__browser_take_screenshot
  - mcp__playwright__browser_close
---

# Research-to-Action

Operator workflow that chains search → fetch → browse using a strict decision tree.
Only runs when explicitly invoked.

## When to use
- Complex research tasks that may require multiple tool types
- "Find X, then get the details, then extract the data" workflows
- When you don't know upfront whether fetch or browser will be needed
- Gathering information that spans multiple pages or requires interaction

## Inputs
- **objective**: What information to find and deliver (required)
- **depth**: `shallow` (search only), `moderate` (search + fetch), `full` (search + fetch + browse if needed). Default: `moderate`

## Decision tree

```
START
  │
  ├─ Do I know the exact URL?
  │   ├─ YES → go to FETCH
  │   └─ NO  → go to SEARCH
  │
SEARCH (web-research phase)
  │  Run 2-4 queries via brave_web_search + tavily_search
  │  Collect candidate URLs
  │
  ├─ Found authoritative URL with the answer in snippet?
  │   └─ YES → record finding, go to SYNTHESIZE
  │
  ├─ Found URL that likely has the answer?
  │   └─ YES → go to FETCH
  │
  └─ No useful results?
      └─ Refine queries, retry once. If still nothing → REPORT GAP
  │
FETCH (http-fetch phase)
  │  Fetch URL via mcp__fetch__fetch or tavily_extract
  │
  ├─ Got the content needed?
  │   └─ YES → go to SYNTHESIZE
  │
  ├─ Content is empty, partial, or JavaScript-rendered?
  │   └─ YES → go to BROWSE (only if depth=full)
  │
  └─ Fetch failed (403, timeout, etc.)?
      └─ Try tavily_extract as fallback. If still fails → REPORT CONSTRAINT
  │
BROWSE (browser-automation phase, only when depth=full)
  │  Navigate via playwright, snapshot, interact minimally
  │
  ├─ Got the content needed?
  │   └─ YES → close browser, go to SYNTHESIZE
  │
  ├─ Blocked by login/captcha/paywall?
  │   └─ STOP → close browser, REPORT CONSTRAINT
  │
  └─ Max 10 steps reached?
      └─ STOP → close browser, report partial results
  │
SYNTHESIZE
  │  Merge all findings from search + fetch + browse
  │  Apply source quality rubric
  │  Format final output
  │
END
```

## Escalation rules
1. **Always start with search.** Never jump to fetch or browse without a URL.
2. **Fetch before browse.** Only escalate to Playwright when fetch returns empty/incomplete content AND depth=full.
3. **Browse is the last resort.** Most tasks complete at search or fetch stage.
4. **Close the browser.** Always call `browser_close` after browsing.
5. **Never loop.** Each phase runs once (with one retry for search). No circular escalation.

## Outputs / contract

Every response MUST contain these headings:

```
## Objective
<restated objective>

## Phases executed
| Phase | Tool(s) used | Result |
|-------|-------------|--------|
| search | brave_web_search, tavily_search | found 3 candidate URLs |
| fetch | fetch | extracted pricing data from URL |
| browse | (skipped) | not needed |

## Findings
<synthesized answer with inline citations>

## Sources
- [Title](URL) — how used

## Constraints encountered
<"None" or: login required at X, JS-only page at Y, etc.>

## Confidence
<high / medium / low> — <justification>
```

## Examples

### Example 1: Moderate depth (search + fetch)
**User**: `/research-to-action Find the current rate limits for the GitHub REST API`

```
## Objective
Find current GitHub REST API rate limits.

## Phases executed
| Phase | Tool(s) used | Result |
|-------|-------------|--------|
| search | brave_web_search | found docs.github.com rate-limit page |
| fetch | fetch | extracted rate limit table |
| browse | (skipped) | static content, not needed |

## Findings
- Unauthenticated: 60 requests/hour
- Authenticated (token): 5,000 requests/hour
- GitHub App: 15,000 requests/hour
Source: [GitHub Docs](https://docs.github.com/en/rest/rate-limit)

## Sources
- [GitHub REST API Rate Limits](https://docs.github.com/en/rest/rate-limit) — primary source

## Constraints encountered
None

## Confidence
high — official documentation, single authoritative source
```

### Example 2: Full depth (search + fetch + browse)
**User**: `/research-to-action Extract the full feature comparison table from linear.app/pricing`

```
## Objective
Extract Linear's pricing feature comparison table.

## Phases executed
| Phase | Tool(s) used | Result |
|-------|-------------|--------|
| search | tavily_search | confirmed URL: linear.app/pricing |
| fetch | fetch | partial content — table rendered by JavaScript |
| browse | playwright navigate+snapshot | full table extracted |

## Findings
| Feature | Free | Standard | Plus |
|---------|------|----------|------|
| Issues & projects | Yes | Yes | Yes |
| Cycles | - | Yes | Yes |
| ... | ... | ... | ... |

## Sources
- [Linear Pricing](https://linear.app/pricing) — fetched + browsed

## Constraints encountered
Fetch returned incomplete content (JS-rendered table). Escalated to browser.

## Confidence
high — extracted directly from the live page
```
