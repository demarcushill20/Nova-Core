---
name: http-fetch
description: "Deterministic HTTP retrieval of specific URLs for documentation pages, raw text, JSON endpoints, and structured data extraction. Auto-invoked when the task requires fetching a known URL."
disable-model-invocation: false
allowed-tools:
  - mcp__fetch__fetch
  - mcp__tavily__tavily_extract
---

# HTTP Fetch

## When to use
- Retrieving content from a known, specific URL
- Pulling documentation pages, API responses, or raw files
- Extracting structured data from a web page
- Reading JSON/text endpoints programmatically
- When full browser automation is NOT needed (no JavaScript rendering, no interaction)

## When NOT to use
- URL is unknown — use `web-research` first to find it
- Page requires JavaScript rendering or interaction — use `browser-automation`
- Page is behind login, captcha, or paywall — use `browser-automation` or report constraint

## Inputs
- **url**: The target URL (required). Must be a complete, valid URL.
- **intent**: What to extract — `full`, `summary`, `specific_fields`, or `raw`. Default: `summary`
- **expected_type**: `html`, `json`, `text`, or `auto`. Default: `auto`

## Workflow

1. **Sanity-check URL** — verify it looks valid (scheme, domain, path). Reject obviously malformed URLs.
2. **Choose tool**:
   - `mcp__fetch__fetch` — general purpose, returns markdown-converted content. Best for docs, articles, text.
   - `mcp__tavily__tavily_extract` — better for structured extraction with relevance ranking. Use when extracting specific information from complex pages.
3. **Fetch** — execute the retrieval.
4. **Parse** — apply the appropriate pattern from [PARSING_PATTERNS.md](reference/PARSING_PATTERNS.md).
5. **Extract** — pull out the requested information.
6. **Format output** — use the contract headings below.

## Tool usage rules
- Always use `mcp__fetch__fetch` as the default. It handles HTML→markdown conversion.
- Use `mcp__tavily__tavily_extract` when you need content chunked by relevance to a query.
- Set `max_length` on fetch to avoid pulling enormous pages (default 5000 chars, increase if needed).
- For JSON endpoints, fetch raw and parse the structure.
- Never guess or fabricate URL content — always fetch first.

## Outputs / contract

Every response MUST contain these headings:

```
## Retrieved content
<extracted/summarized content>

## Source
- URL: <fetched URL>
- Content type: <html | json | text | other>
- Length: <character count of raw response>

## Extraction notes
<what was extracted, any truncation, parsing decisions>

## Errors
<"None" or description of failures, timeouts, blocked requests>
```

## Examples

### Example 1: Fetch documentation page
**User**: "Get the httpbin.org API docs"

**Tool call**: `mcp__fetch__fetch(url="https://httpbin.org", max_length=5000)`

```
## Retrieved content
httpbin.org — A simple HTTP Request & Response Service.
Endpoints: /get, /post, /put, /delete, /status/:codes, /headers, ...

## Source
- URL: https://httpbin.org
- Content type: html
- Length: 3842

## Extraction notes
Converted HTML to markdown. Navigation and footer stripped by converter.

## Errors
None
```

### Example 2: Fetch JSON endpoint
**User**: "What's my public IP according to httpbin?"

**Tool call**: `mcp__fetch__fetch(url="https://httpbin.org/ip", max_length=500)`

```
## Retrieved content
{"origin": "144.202.55.19"}
Public IP: 144.202.55.19

## Source
- URL: https://httpbin.org/ip
- Content type: json
- Length: 32

## Extraction notes
Raw JSON response. Single field extracted.

## Errors
None
```
