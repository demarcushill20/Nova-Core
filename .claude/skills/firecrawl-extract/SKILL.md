---
name: firecrawl-extract
description: >-
  Extract structured JSON data from web pages using LLM-powered extraction
  with schemas and prompts. Auto-invoked when the user needs specific data
  points from a page (pricing, specs, contacts, API parameters), wants to
  compare structured data across sites, or says 'extract', 'pull data from',
  'get the pricing from', 'parse this page'.
argument-hint: "[url] [what to extract]"
allowed-tools:
  - mcp__firecrawl__firecrawl_scrape
  - mcp__firecrawl__firecrawl_extract
activation:
  keywords:
    - extract
    - pull data
    - parse page
    - structured data
    - get the pricing
---

# Firecrawl Structured Extraction

Extract specific data points from web pages as structured JSON.

## When to Use

- Need **specific fields** from a page (prices, features, specs, contacts)
- Comparing **structured data** across multiple pages
- Building datasets from web sources
- NOT for reading full articles (use firecrawl-deep-research instead)

## Workflow

### Single Page — Use `firecrawl_scrape` with JSON format

```
firecrawl_scrape:
  url: "https://example.com/pricing"
  formats: ["json"]
  jsonOptions:
    prompt: "Extract all pricing tiers with name, price, and features"
    schema:
      type: object
      properties:
        tiers:
          type: array
          items:
            type: object
            properties:
              name: { type: string }
              price: { type: string }
              features: { type: array, items: { type: string } }
```

### Multiple Pages — Use `firecrawl_extract`

```
firecrawl_extract:
  urls: ["https://site1.com/pricing", "https://site2.com/pricing"]
  prompt: "Extract pricing tiers"
  schema: { ... }
```

- Supports wildcards: `https://example.com/products/*`
- Max 10 URLs per batch
- Async for large jobs — poll with check status

## Schema Design Tips

- Be specific in field names and types
- Include `description` on ambiguous fields
- Use `enum` for known categories
- Keep schemas focused — extract one concept per call
- Test on 1 URL before scaling to batch

## Credit Budget

- Scrape with JSON: 1 credit per URL
- Extract: 1 credit per URL in batch
- Keep batches under 10 URLs to avoid rate limits
