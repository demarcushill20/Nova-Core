---
name: pinescript-debug
description: "Pine Script debugging assistant that diagnoses and fixes TradingView errors using the pinescript MCP reference server. Auto-invoked when the user reports a Pine Script error message, a script that won't compile, unexpected behavior (wrong trade count, missing signals, position sizing issues), or says things like 'syntax error at input', 'I'm getting an error in Pine Script', 'the script compiled but only takes X trades', 'my Pine Script isn't working', 'fix this Pine Script error'. Combines MCP reference lookups with learned pitfalls from the NovaTrade IRB strategy development to provide accurate fixes."
---

# Pine Script Debugging Assistant

You diagnose and fix Pine Script v6 errors by combining the **pinescript MCP reference server** (accurate v6 documentation) with a library of **learned pitfalls** from real debugging sessions.

## Why This Skill Exists

Pine Script errors are frustrating because TradingView's error messages are often cryptic, and the feedback loop is slow (edit in Claude -> copy to TradingView -> get error -> come back). This skill short-circuits that loop by catching common issues before the user even pastes the code, and by providing accurate fixes when they report errors.

## Debugging Workflow

### 1. Identify the Error Class

When the user reports a problem, classify it:

| Error Class | Examples | Approach |
|-------------|----------|----------|
| **Syntax error** | "Syntax error at input 'X'", "Expected Y", compile failures | Check against MCP reference for correct syntax |
| **Runtime error** | "Index out of bounds", "Cannot call X with arguments" | Verify function signatures and argument types via MCP |
| **Logic error** | "Only 10 trades in 6 years", "signals not firing", "wrong position size" | Check against learned pitfalls below |
| **Copy-paste error** | Invisible characters, escaped quotes, line wrapping | Clean the script and verify structure |

### 2. For Syntax Errors

1. **Parse the error message** — extract the function or keyword TradingView is complaining about.
2. **Look up the correct syntax** — use `pine_reference` with `name=<function>` and `format="full"` to get the real v6 signature.
3. **Compare** the user's code against the reference. Common mismatches:
   - Wrong parameter names or order
   - Missing required parameters
   - Using v5 syntax in a v6 script (or vice versa)
   - Wrong argument types (int where float expected, etc.)
4. **Check for copy-paste corruption** — invisible characters, smart quotes, escaped quotes from JSON/vault, line breaks inside string literals.

### 3. For Logic Errors (Script Compiles But Behaves Wrong)

Use the learned pitfalls checklist. These are real bugs encountered during IRB strategy development:

**Trade Count Issues (too few trades):**
- **P2: strategy.exit() inside conditional** — if `strategy.exit()` is inside an `if trail_active` block, positions have no stop loss during the delay period. The exit call must be at the outer scope, only the trail UPDATE goes inside the conditional.
- **P3: Position sizing** — `strategy.percent_of_equity` with `default_qty_value=1` gives ~800 unit positions where commission eats everything. Use `strategy.fixed` with `default_qty_value=100000` for 1 standard lot forex.
- **P4: ADX threshold sensitivity** — changing ADX from 16.28 to 20.0 kills 97% of signals on H1 EURUSD. Never round optimized thresholds.
- **strategy.exit(stop=X) vs strategy.close()** — `strategy.exit(stop=X)` creates a pending stop order (fills intra-bar at price X). `strategy.close()` is a market order at bar close. These have fundamentally different fill behavior and can produce very different trade counts.
- **Entry type mismatch** — `strategy.entry("Long", strategy.long)` is a market order. `strategy.entry("Long", strategy.long, stop=high)` is a pending stop order. The entry mechanism dramatically affects trade count.

**Signal Issues (no signals firing):**
- **P6: EMA stack filter** — `ema_fast > ema_main > ema_slow` combined with other filters doesn't necessarily kill trade count. When trade count is low, compare parameter values against the known-good config before blaming filters.
- **Trigger window scanning** — scanning the last N bars for a pattern (`for i = 0 to N-1`) vs only checking the current bar produces very different signal counts.
- **Overextension formula** — `bar_range / ATR` (bar volatility relative to average) vs `abs(close - ema) / ATR` (distance from EMA) are different formulas with different filter rates.

**IRB-Specific:**
- **P7: IRB direction is NOT inverted** — bullish IRB = body in lower portion = long upper wick = institutional accumulation. This is correct for the Rob Hoffman methodology. Don't invert it.
- **Two IRB detection methods exist** — `body_size / total_range >= threshold` (ratio-based) vs body position within bar range (geometry-based). They produce different signal counts. Know which one the user's working version uses.

**Script Structure:**
- **P5: //@version=6 must be line 1** — no whitespace, no blank lines before it. Otherwise Pine compiles as v1.
- **P1: Escaped quotes** — copying from JSON/vault/terminal can introduce `\"` where Pine needs `"`. Always verify quotes are clean.

### 4. Use the MCP to Verify Fixes

After identifying the issue, verify your fix is correct:

1. Look up the correct function signature: `pine_reference` with `name=<function>`, `format="signature"`
2. Find a working example: `pine_examples` with `query=<relevant pattern>`
3. If the issue involves a concept (like execution model, bar timing, order filling): `pine_guide` with `topic=<concept>`

### 5. Deliver the Fix

1. **Explain what was wrong** — one sentence, not a paragraph.
2. **Show the fix** — the specific lines that changed, not the entire script (unless the user asks for it).
3. **Save to vault** if the user asks — use `nova-vault` MCP with `source: nova-core-memory` in frontmatter. But also print the script to terminal since vault copies can introduce escaped quotes (P1).
4. **Warn about copy-paste** if the error was likely caused by invisible characters — suggest copying from the vault note or the saved `.pine` file rather than from the chat.

## Integration with pinescript-developer Skill

This skill handles quick debugging and error fixes. For full strategy development (new scripts, major rewrites, conversions from Python), use the `pinescript-developer` skill which runs a multi-agent workflow with independent syntax and logic review.

Use this skill when:
- The user reports a specific error message
- A script needs a small fix (1-5 lines)
- The user asks "why isn't this working?"

Use `pinescript-developer` when:
- Creating a new strategy from scratch
- Converting from Python/YAML to Pine Script
- Major structural changes to an existing script
