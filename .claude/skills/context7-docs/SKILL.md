---
name: context7-docs
description: "Retrieve version-specific library documentation via Context7 MCP to eliminate hallucinated API references. Auto-invoked when tasks require library docs, code generation with specific APIs, or setup/configuration steps."
disable-model-invocation: false
allowed-tools:
  - mcp__context7__resolve-library-id
  - mcp__context7__get-library-docs
activation:
  keywords: [docs, documentation, API, library, package, how to use, import, example, setup, configure, SDK]
  when:
    - User asks how to use a specific library or framework
    - Code generation requires accurate API signatures
    - Debugging involves suspected API misuse
    - Setup or configuration of a library is needed
    - User says "use context7" or "check the docs"
tool_doctrine:
  documentation:
    workflow:
      - resolve_library_id_first
      - query_with_specific_intent
      - prefer_official_docs_over_guessing
      - never_fabricate_api_signatures
      - cite_library_version
output_contract:
  required:
    - summary
    - library
    - version_context
    - verification
    - confidence
---

# Context7 Documentation Lookup

## When to use

- Writing code that imports a library and you need the correct API
- User asks "how do I use X?" for a library or framework
- Debugging an error that might be caused by API changes between versions
- Generating setup/configuration for a library (install, config files, env vars)
- Any time you would otherwise guess at an API signature

## When NOT to use

- General knowledge questions unrelated to library APIs
- Code that uses only Python builtins or standard library
- Questions about nova-core's own codebase (use file reads instead)
- When the user has already provided the exact API they want

## Inputs

- **library**: The library or framework name (required). E.g., "fastapi", "pydantic", "react"
- **query**: What you need to know about it (required). E.g., "how to create a WebSocket endpoint"
- **topic**: Optional narrowing. E.g., "middleware", "authentication", "CLI"

## Workflow

### Step 1 -- Resolve the Library ID

Before querying docs, resolve the library name to a Context7 library ID:

```
Tool: mcp__context7__resolve-library-id
Args: {
  "libraryName": "fastapi",
  "query": "WebSocket endpoint creation"
}
```

This returns a list of matching libraries with their Context7 IDs (e.g., `/tiangolo/fastapi`).
Pick the best match by relevance.

### Step 2 -- Query Documentation

Use the resolved library ID to fetch relevant documentation:

```
Tool: mcp__context7__get-library-docs
Args: {
  "libraryId": "/tiangolo/fastapi",
  "query": "how to create a WebSocket endpoint"
}
```

The response contains version-specific documentation snippets directly from the library's official docs.

### Step 3 -- Apply to Task

Use the retrieved documentation to:
- Write code with correct API signatures
- Provide accurate setup instructions
- Debug version-specific issues
- Answer the user's question with citations

## Tool Usage Rules

- **Always resolve first.** Never guess a library ID -- call `resolve-library-id` even for well-known libraries.
- **Be specific in queries.** "WebSocket endpoint with authentication" retrieves better docs than "WebSocket".
- **Cite what you find.** When the docs confirm an API, state the library version context.
- **Fall back gracefully.** If Context7 has no docs for a library, say so and use your training knowledge with a confidence downgrade.
- **One library per invocation.** If the user needs docs for multiple libraries, resolve and query each separately.

## Failure Handling

- If `resolve-library-id` returns no matches: try alternate names (e.g., "python-telegram-bot" vs "telegram"), check for typos, then fall back to training knowledge with `confidence: low`.
- If `get-library-docs` returns empty: broaden the query terms, try a different topic angle, then fall back with a note.
- If the MCP server is unreachable: proceed with training knowledge, note the fallback in the contract.

## Outputs / Contract

```
## Context7 Docs Contract
summary: <what was looked up and the key finding>
library: <library name and Context7 ID>
version_context: <version info from the docs, or "unversioned">
docs_retrieved: <yes | no — fell back to training knowledge>
verification: <API confirmed by Context7 docs | fell back to training knowledge>
confidence: <high | medium | low>
```

## Examples

### Example 1: Looking up an API

**User**: "How do I create a background task in FastAPI?"

**Step 1**: resolve-library-id("fastapi", "background task")
**Step 2**: get-library-docs("/tiangolo/fastapi", "background task BackgroundTasks")

**Answer**: Use `BackgroundTasks` from `fastapi`:
```python
from fastapi import BackgroundTasks

@app.post("/items/")
async def create_item(background_tasks: BackgroundTasks):
    background_tasks.add_task(send_notification, item.id)
    return {"status": "created"}
```

**Contract**:
```
summary: Retrieved FastAPI BackgroundTasks API from Context7
library: fastapi (/tiangolo/fastapi)
version_context: FastAPI 0.115+
docs_retrieved: yes
verification: API confirmed by Context7 docs
confidence: high
```

### Example 2: Library not found

**User**: "How do I use obscure-lib's parse method?"

**Step 1**: resolve-library-id("obscure-lib", "parse method") -> no results

**Answer**: Context7 doesn't have documentation for obscure-lib. Based on training knowledge [with caveat]...

**Contract**:
```
summary: obscure-lib not found in Context7, fell back to training knowledge
library: obscure-lib (not in Context7)
version_context: unknown
docs_retrieved: no
verification: fell back to training knowledge
confidence: low
```
