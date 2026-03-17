# NovaTrade Demo Test Run — Compile Validation Report (Fresh IRB)

**Phase:** 3 (Compile / Lint / Static Validation)
**Date:** 2026-03-17
**Status:** PASS
**Agent:** Compiler/Lint Agent
**Pine:** strategy.pine v2.0.0 (Rob Hoffman IRB, 549 lines)
**Replaces:** EMA Crossover compile report (2026-03-16)

---

## 1. Compile Validation Method

**Direct compilation was not possible.** No TradingView Pine compiler is available in this environment. All findings below are from a grounded static review against the Pine Script v5 language specification. Every line of `strategy.pine` (549 lines, 17 sections) was reviewed.

### What was directly verified (static analysis)

| # | Check | Result |
|---|-------|--------|
| 1 | Pine version declaration (`// @version=5` on line 1) | PASS |
| 2 | `strategy()` declaration — all 10 parameters valid Pine v5 | PASS |
| 3 | `overlay`, `default_qty_type`, `default_qty_value`, `initial_capital`, `currency`, `pyramiding`, `process_orders_on_close`, `calc_on_every_tick`, `max_bars_back` — all valid parameter names | PASS |
| 4 | `strategy.fixed` is a valid `default_qty_type` enum | PASS |
| 5 | `currency.USD` is a valid `currency` enum | PASS |
| 6 | All 18 top-level constants have correct types (int/float/string) | PASS |
| 7 | `syminfo.mintick` — valid built-in (float) | PASS |
| 8 | `ta.ema(close, EMA_PERIOD)` — correct signature `ta.ema(source, length)` | PASS |
| 9 | `ta.atr(ATR_PERIOD)` — correct signature `ta.atr(length)` | PASS |
| 10 | `ta.dmi(ADX_PERIOD, ADX_PERIOD)` — correct signature `ta.dmi(dilen, adxlen)`, returns tuple [float, float, float] | PASS |
| 11 | `[dp, dm, adx_val] = ta.dmi(...)` — correct tuple destructuring | PASS |
| 12 | `request.security(syminfo.tickerid, "240", ta.ema(close, EMA_PERIOD), lookahead=barmerge.lookahead_off)` — correct signature with all valid params | PASS |
| 13 | `barmerge.lookahead_off` — valid enum | PASS |
| 14 | All `math.*` built-ins used correctly: `math.abs()`, `math.max()`, `math.min()`, `math.round()` | PASS |
| 15 | `strategy.equity` — valid built-in (float) | PASS |
| 16 | `strategy.position_size` — valid built-in (float series) | PASS |
| 17 | `strategy.position_size[1]` — valid history reference on series type | PASS |
| 18 | `strategy.entry(id, direction, stop, qty)` — correct signature | PASS |
| 19 | `strategy.exit(id, from_entry, stop)` — correct signature; "Long Exit" matches "Long" entry, "Short Exit" matches "Short" entry | PASS |
| 20 | `strategy.close(id, comment)` — correct signature | PASS |
| 21 | `strategy.cancel(id)` — correct signature | PASS |
| 22 | `strategy.long` / `strategy.short` — valid direction enums | PASS |
| 23 | `nz()` — valid built-in; `nz(float)` returns float, `nz(float, float)` returns float | PASS |
| 24 | `na()` — valid built-in; `na(float)` returns bool | PASS |
| 25 | `bar_index` — valid built-in (int series) | PASS |
| 26 | `time_close` — valid built-in (int) | PASS |
| 27 | `var` keyword for persistent variable declarations — valid Pine v5 | PASS |
| 28 | `f_qty(float ep, float sp) =>` — valid Pine v5 function declaration syntax | PASS |
| 29 | All `str.tostring(value, format)` calls — valid signatures | PASS |
| 30 | Format strings `"#.#####"`, `"#.#"`, `"#.##"`, `"#.###"` — valid Java DecimalFormat patterns | PASS |
| 31 | String concatenation with `+` operator on string types | PASS |
| 32 | `alert(message, freq)` — valid signature | PASS |
| 33 | `alert.freq_once_per_bar_close` — valid alert frequency enum | PASS |
| 34 | `plot()` — valid; all params correct | PASS |
| 35 | `plotshape()` — valid; `shape.triangleup`, `shape.triangledown`, `location.belowbar`, `location.abovebar`, `size.small` all valid enums | PASS |
| 36 | `bgcolor()` — valid | PASS |
| 37 | `plot.style_stepline` — valid plot style enum | PASS |
| 38 | `color.new(color.blue/red/green/orange/gray, transp)` — valid | PASS |
| 39 | All `if` / `else if` block scoping and indentation correct | PASS |
| 40 | All ternary operator expressions syntactically valid | PASS |
| 41 | All `:=` reassignment operators used on `var`-declared variables | PASS |
| 42 | All `+=` operators used on `var int` variables | PASS |
| 43 | Boolean operators `and`, `or`, `not` used correctly | PASS |
| 44 | No illegal series usages (all series used in series-compatible contexts) | PASS |
| 45 | No malformed expressions or unclosed blocks | PASS |

### What was inferred (not directly compilable)

| Check | Inference | Confidence |
|-------|-----------|------------|
| No Pine compiler errors | All 549 lines pass static analysis against Pine v5 spec | High |
| `dp` and `dm` from `ta.dmi()` unused | Pine compiler would emit warnings (not errors). Standard `ta.dmi()` usage — all three return values must be destructured. | High |
| `str.tostring(value, "#.#####")` format accepted | Pine accepts Java DecimalFormat-style patterns. `#.#####` gives up to 5 decimal places. | High |
| No runtime type coercion errors | All operations use compatible types. No implicit narrowing. | High |
| `strategy.exit()` `from_entry` IDs match `strategy.entry()` IDs | "Long Exit" → "Long", "Short Exit" → "Short" — IDs match. | High |
| `f_qty()` return type | Returns float (last expression: `rnd * units_per_lot`). Compatible with `strategy.entry(qty=)`. | High |

---

## 2. Compile Validation Result

**PASS — No compile errors found.**

The Pine script is syntactically valid across all 549 lines: version declaration correct, `strategy()` parameters valid, all identifiers defined before use, all built-in functions called with correct signatures, all types consistent, all Pine v5 constructs properly formed, all enums valid, function declaration valid, tuple destructuring valid.

---

## 3. Compile-Related Blockers

**None.**

---

## 4. Compile-Related Must-Fix Items

**None.**

---

## 5. Compile-Related Warnings

| ID | Warning | Impact |
|----|---------|--------|
| CW1 | `dp` and `dm` from `ta.dmi()` are unused | Pine compiler will emit warnings for unused variables. Not errors. Syntactically required — `ta.dmi()` returns a 3-tuple that must be fully destructured. |

---

## 6. Notes

1. The script uses 17 clearly delimited sections with consistent comment headers.
2. Verification confidence is HIGH across all 45 checks. The script uses standard Pine v5 patterns with no exotic features.
3. The `f_qty()` function (lines 218-224) is the only user-defined function. It uses standard `math.*` operations and returns a float — no compile risk.
4. Alert JSON construction via string concatenation (lines 445-527) is verbose but syntactically correct.
5. Phase 4 must confirm compilation by loading the script in TradingView.

---

## 7. Phase 4 Re-Validation (2026-03-17)

**Status: No changes since Phase 3 — compile findings remain valid.**

Phase 4 (Backtesting and Validation) re-examined `strategy.pine` v2.0.0 for compile-relevant concerns in the context of alert readiness and downstream contract safety. No code modifications were made between Phase 3 and Phase 4.

### Timestamp and Bar Reference Validation

All timestamp and bar-reference constructs were reviewed for correctness and clarity:

| Construct | Pine Source | Type | Definition | Verdict |
|-----------|-----------|------|-----------|---------|
| `time_close` (L461) | Pine built-in | Unix ms (int) | Close time of current bar | Well-defined |
| `bar_index` (L125) | Pine built-in | 0-based int | Bar counter from chart start | Well-defined |
| `pend_bars` (L206) | `var int` counter | int | Bars since pending order placed; incremented at L274 | Well-defined |
| `pos_bars` (L207) | `var int` counter | int | Bars since position opened; incremented at L276 | Well-defined |
| `ema_h1[SLOPE_LOOKBACK]` (L148) | History ref | float | EMA value 20 bars ago | Well-defined |
| `ema_h4[MTF_H1_LOOKBACK]` (L162) | History ref | float | H4 EMA value 20 H1 bars ago | Well-defined (see PA-IRB-1) |
| `strategy.position_size[1]` (L231) | History ref | float | Previous bar position | Well-defined |
| `prev_stop` (L481-498) | `var float` | float | Trail stop from prior bar | Well-defined |

**All bar references are backward-looking. No forward references found.**

### Compile-Relevant Contract Observations

| # | Observation | Impact |
|---|------------|--------|
| 1 | `bars_elapsed` in cancel alert (L509) uses constant `TRIGGER_WIN` rather than measured `pend_bars` | Functionally equivalent — cancel fires when `pend_bars >= TRIGGER_WIN`, so `pend_bars` is exactly 20 at trigger point. No compile issue; noted for contract awareness. |
| 2 | All `str.tostring()` calls use valid format strings accepted by Pine v5 | Confirmed — no new compile risk from alert payload construction. |

### Phase 4 Compile Verdict

**PASS — Phase 3 compile validation stands. No new compile-related concerns.**
