---
name: pinescript-reference
description: "Pine Script v6 function and variable reference lookup via the pinescript MCP server. Auto-invoked when writing, reviewing, or modifying Pine Script code to verify correct function signatures, parameter types, return values, and usage patterns. Use whenever you need to check a Pine Script function (ta.ema, strategy.entry, request.security, etc.), browse available functions in a namespace, find code examples, or read Pine Script conceptual guides. Also use when the user asks 'how does X work in Pine Script', 'what are the parameters for Y', or 'show me examples of Z'. This skill prevents hallucinated function signatures — always prefer it over guessing Pine Script API details."
---

# Pine Script v6 Reference Lookup

You have access to the **pinescript MCP server** which contains the complete TradingView Pine Script v6 reference (457 functions, 427 variables, full user guide). Use it to give accurate, verified answers about Pine Script syntax and API.

## Why This Matters

Pine Script v6 has subtle API differences from v5 and earlier. Hallucinating function signatures leads to syntax errors that waste the user's time (they have to paste into TradingView, get an error, come back, and ask for fixes). By looking up the real reference, you give correct code the first time.

## Available MCP Tools

Use the `pinescript` MCP server tools. The tool names follow the pattern used by the server:

| Tool | When to Use | Key Parameters |
|------|-------------|----------------|
| `pine_reference` | Look up a specific function or variable by exact name | `name` (e.g., "ta.ema", "strategy.entry"), `format` ("full", "signature", or "examples") |
| `pine_search` | Search across all docs when you're not sure of the exact name | `query` (search terms), `source` ("manual", "docs", or "all"), `limit` (default 5) |
| `pine_categories` | Browse what's available in a namespace or category | `category` (e.g., "ta", "strategy", "input") — omit to list all 48 categories |
| `pine_guide` | Read conceptual documentation (how things work, not just API) | `topic` (e.g., "arrays", "matrices", "strategy"), `listTopics` (true to see all topics) |
| `pine_examples` | Find code examples for a specific pattern or technique | `query` (what you're looking for), `limit` (default 5) |

## Workflow

1. **User asks about a Pine Script function or concept** — determine whether they need a specific function lookup or a broader search.

2. **For specific functions** (user says "how does ta.ema work?" or you need to verify a signature while writing code):
   - Use `pine_reference` with `name` = the function name and `format` = "full" for complete docs, or "signature" for just the call signature.

3. **For exploratory questions** (user says "what moving average functions are available?" or "how do I do trailing stops?"):
   - Use `pine_search` with relevant keywords, or `pine_categories` to browse a namespace.
   - Follow up with `pine_reference` on specific functions that look relevant.

4. **For conceptual questions** (user asks "how do Pine Script strategies work?" or "what's the execution model?"):
   - Use `pine_guide` to get the conceptual documentation.

5. **For code patterns** (user says "show me an example of ATR trailing stop" or you need to verify how a function is used in practice):
   - Use `pine_examples` to find real code examples.

6. **Present results cleanly** — extract the key information (signature, parameters, return type, important notes) and present it in a format the user can immediately use. Don't dump raw reference text — summarize what matters for their question.

## When Writing Pine Script Code

Whenever you're writing or modifying a `.pine` file, proactively look up functions you're about to use — especially:
- `strategy.*` functions (entry, exit, close, order) — parameter names and types vary
- `ta.*` functions — some return tuples (like `ta.dmi` returning `[diplus, diminus, adx]`)
- `request.security()` — the timeframe and expression parameters are tricky
- `input.*` functions — each type has different parameter sets

This takes seconds and prevents minutes of debugging syntax errors on TradingView.

## Example

User: "What parameters does strategy.exit take?"

1. Call `pine_reference` with `name="strategy.exit"`, `format="full"`
2. Present the signature, required vs optional parameters, and a brief usage note
3. If helpful, call `pine_examples` with `query="strategy.exit trailing stop"` to show practical usage
