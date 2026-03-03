# Parsing Patterns

Practical patterns for handling different content types returned by HTTP fetch tools.

## HTML (most common)

`mcp__fetch__fetch` auto-converts HTML to markdown. Post-processing:

```
1. Scan for the main content area (usually after nav, before footer).
2. Strip boilerplate: cookie banners, sidebars, ads, related links.
3. Preserve: headings, code blocks, tables, lists, links.
4. If the page is too long, extract the section most relevant to the query.
```

**Common issues**:
- Truncation at `max_length` — increase limit or fetch with `start_index` for pagination.
- JavaScript-rendered content missing — escalate to `browser-automation`.
- Encoding issues — the tool handles UTF-8; flag if garbled output appears.

## JSON

Fetch returns raw JSON. Processing:

```
1. Parse the JSON structure mentally (identify top-level keys).
2. Extract only the fields relevant to the user's question.
3. Format extracted fields as a readable summary.
4. Preserve nested structures when they carry meaning (e.g., API responses).
```

**Common shapes**:
- Single object: `{"key": "value"}` — extract directly.
- Array of objects: `[{...}, {...}]` — summarize count, show first 2-3 items.
- Paginated: look for `next`, `offset`, `total` fields — report pagination state.
- Error responses: look for `error`, `message`, `status` fields — report the error.

## Plain text

```
1. Read first 20 lines to determine structure (log file, config, prose, CSV).
2. For configs: identify format (INI, TOML, YAML, env) and extract key-value pairs.
3. For logs: find the most recent or relevant entries.
4. For CSV: describe columns, row count, show first 3 rows.
```

## Handling failures

| Symptom | Cause | Action |
|---------|-------|--------|
| Empty response | Blocked, rate-limited, or JS-only | Try `tavily_extract` or escalate to browser |
| 403/401 | Auth required | Report constraint, do NOT retry with credentials |
| 404 | URL invalid or content moved | Verify URL, try web-research to find current URL |
| Timeout | Slow server or large payload | Retry once with smaller `max_length` |
| Garbled text | Encoding issue | Note in output, try `raw=true` on fetch |
| Redirect loop | Misconfigured URL | Report the redirect chain, stop |
