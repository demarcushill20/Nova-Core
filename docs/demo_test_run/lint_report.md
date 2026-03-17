# NovaTrade Demo Test Run — Lint and Structural Review Report (Fresh IRB)

**Phase:** 3 (Compile / Lint / Static Validation)
**Date:** 2026-03-17
**Status:** PASS (no blockers, no must-fix items)
**Agent:** Compiler/Lint Agent
**Pine:** strategy.pine v2.0.0 (Rob Hoffman IRB, 549 lines)
**Replaces:** EMA Crossover lint report (2026-03-16)

---

## 1. Categorized Findings

### Blockers

**None.**

### Must-Fix Before Phase 4

**None.**

### Warnings

| ID | Finding | Lines | Impact | Recommendation |
|----|---------|-------|--------|----------------|
| W1 | **Format string `"#.#####"` drops trailing zeros** | 456-474 | `str.tostring(1.10000, "#.#####")` produces `"1.1"` not `"1.10000"`. No data loss — JSON numbers are equivalent (`1.1 == 1.10000`). The `entry_price`, `stop_loss`, and `volume` fields are authoritative numbers regardless of formatting. | Acceptable for Phase 4. If exact 5-digit formatting is desired, change format to `"0.00000"`. Not required — no downstream correctness impact. |
| W2 | **One-bar SL protection gap in backtest** | 370-408 | With `process_orders_on_close = false`, stop order fills intra-bar on bar N+1. `strategy.exit()` is called at bar N+1 close → exit order activates from bar N+2. Bar N+1 (the fill bar) has no SL protection in the backtest. | Known Pine backtest limitation. Does NOT affect live execution — Trading Agent places SL immediately on MetaApi after fill confirmation. Phase 4 backtest results should note this. Not a code defect. |
| W3 | **`dp` and `dm` from `ta.dmi()` are unused** | 111 | Pine compiler will emit "unused variable" warnings. These are the DI+/DI- components from `ta.dmi()` — only `adx_val` is needed. All three must be destructured. | Accepted. Standard Pine `ta.dmi()` usage. No alternative syntax available. |

### Informational

| ID | Finding | Lines | Notes |
|----|---------|-------|-------|
| I1 | **Volume computation is duplicated** | 218-224, 441-443 | `f_qty()` computes volume in units for `strategy.entry(qty=)`. The alert payload recomputes volume in lots for the Trading Agent. Both use the same formula but differ in final units. The duplication is intentional — Pine needs units, MetaApi needs lots. |
| I2 | **`prev_stop` update is unconditional** | 498 | `prev_stop := cur_stop` executes every bar, even when flat (setting `prev_stop` to `na`). This correctly resets trail tracking between positions. On the first bar after fill, `cur_stop != nz(prev_stop)` triggers an initial MODIFY_SL alert — correct behavior per P-IRB-7. |
| I3 | **Cancel + Signal alerts can fire on the same bar** | 283-316 | If a trigger window expires and a new qualifying IRB forms on the same bar, both CANCEL_ORDER and PLACE_STOP_ORDER alerts fire. This is correct per the state machine: PENDING → FLAT (cancel) → PENDING (new signal). The Trading Agent should process both sequentially. |
| I4 | **`pip_size` derivation assumes 5-digit broker** | 97 | `pip_size = syminfo.mintick * 10`. For 5-digit EURUSD, `mintick = 0.00001` → `pip_size = 0.0001` ✓. On a 4-digit chart, this would be wrong. The spec pins the strategy to 5-digit pricing (§1.symbol.digits: 5). |
| I5 | **Signal exclusivity is enforced by trend filter, not by IRB geometry** | 136, 140, 182-183 | Both `is_up_irb` and `is_dn_irb` can be true simultaneously (when O and C are in the middle 10% of the range). However, `sig_long` and `sig_short` cannot both be true because `trend_up` and `trend_dn` are mutually exclusive (`ema_slope` cannot be both ≥ 0.4 and ≤ -0.4). No correctness issue. |

---

## 2. Structural Quality Review

### Code Organization

The Pine script is organized into 17 clearly labeled sections:

| Section | Lines | Purpose | Quality |
|---------|-------|---------|---------|
| Header/comments | 1-27 | Version, purpose, anti-repaint design, scope boundary | Excellent |
| Strategy declaration | 29-39 | `strategy()` with all parameters | Clean |
| 1. Constants | 42-91 | All spec-sourced constants (18 values) | Excellent — each traced to spec |
| 2. Derived constants | 93-98 | `pip_size`, `units_per_lot` | Clean |
| 3. Indicators | 100-119 | EMA H1, ATR, ADX, EMA H4 via request.security() | Clean |
| 4. Warmup guard | 121-125 | `bar_index >= 34` | Clean |
| 5. IRB Geometry | 128-140 | Uptrend/downtrend IRB detection (45% rule) | Clean — 1:1 spec match |
| 6. Trend Filter | 143-151 | ATR-normalized EMA slope with threshold | Clean |
| 7. MTF Alignment | 154-163 | H4 EMA direction with na guards | Clean |
| 8. Sideways Filter | 166-169 | ADX threshold | Clean |
| 9. Overextension Filter | 172-176 | Range/ATR ratio | Clean |
| 10. Combined Signal | 179-183 | 6-condition AND chain | Clean |
| 11. State Machine | 186-209 | 5 states + 9 persistent variables | Clean |
| 12. Position Sizing | 212-224 | `f_qty()` risk-based function | Clean |
| 13. State Transitions | 227-354 | Fill detection, signal handling, replacement | Clean |
| 14. Exit Management | 361-408 | Trailing stop + time stop | Clean |
| 15. Alert Payloads | 410-527 | 4 alert types (signal, trail, cancel, close) | Acceptable — verbose but necessary |
| 16. Visual Annotations | 530-544 | plotshape, plot, bgcolor | Clean |

**Overall structure: Excellent.** Linear top-to-bottom flow. No circular dependencies. Each section depends only on prior sections.

### Naming Conventions

| Convention | Usage | Consistent? |
|-----------|-------|-------------|
| UPPER_SNAKE_CASE | Constants (`IRB_PCT`, `EMA_PERIOD`, `SLOPE_THRESH`, etc.) | Yes |
| lower_snake_case | Variables (`ema_h1`, `atr_val`, `sig_long`, `cur_stop`, etc.) | Yes |
| `S_` prefix | State constants (`S_FLAT`, `S_PLONG`, `S_PSHORT`, `S_LONG`, `S_SHORT`) | Yes |
| `is_` prefix | Boolean detections (`is_up_irb`, `is_dn_irb`) | Yes |
| `sig_` prefix | Combined signals (`sig_long`, `sig_short`) | Yes |
| `evt_` prefix | Event flags (`evt_signal`, `evt_replace`, `evt_cancel`, `evt_tstop`, `evt_side`) | Yes |
| Spec references | `[Spec §X]`, `[A1]`-`[A10]`, `[U1]`-`[U8]`, `[AR1]`-`[AR4]`, `[PA-IRB-X]` | Consistent throughout |

No shadowed or confusing names found.

### Control Flow Analysis

1. **Signal path:** Indicators → 5 filters → combined signal → state machine → order placement → alert
2. **Exit path:** State detection → trailing stop computation → `strategy.exit()` / `strategy.close()`
3. **Fill path:** `strategy.position_size` change → state transition → initial SL setup
4. **Cancel path:** `pend_bars` counter → `strategy.cancel()` → state reset → alert

All paths are linear. No recursion, no loops, no callbacks.

### State Machine Integrity

5 states, all transitions verified. No deadlock paths. No orphan states. No unhandled transitions.

| State | Valid Exits | Via |
|-------|------------|-----|
| S_FLAT | S_PLONG, S_PSHORT | sig_long, sig_short |
| S_PLONG | S_LONG, S_FLAT, S_PLONG (replacement) | fill_long, window expiry, sig_long |
| S_PSHORT | S_SHORT, S_FLAT, S_PSHORT (replacement) | fill_short, window expiry, sig_short |
| S_LONG | S_FLAT | pos_closed (SL/trail/time stop) |
| S_SHORT | S_FLAT | pos_closed (SL/trail/time stop) |

### Division-by-Zero Guards

| Location | Guard | Fallback |
|----------|-------|----------|
| L147-148 (ema_slope) | `nz(atr_val) > 0` | `0.0` → signal suppressed |
| L175 (oe_ratio) | `nz(atr_val) > 0` | `0.0` → overextension filter passes (harmless — trend filter already blocks) |
| L221 (f_qty) | `sd > 0` | `MIN_LOTS` (0.01 lots) |

### Semantic Correctness

- `sig_long` and `sig_short` mutually exclusive: `trend_up` and `trend_dn` cannot both be true ✓
- `fill_long` and `pos_closed` mutually exclusive: `was_flat` vs `not was_flat` ✓
- Trailing stop direction correct: `math.max()` for long (tightens up), `math.min()` for short (tightens down) ✓
- Counter increment fires after fill/state detection, preserving correct timing ✓
- All `var` variables properly reset on `pos_closed` ✓

---

## 3. Security Review

| Check | Result |
|-------|--------|
| No external data sources beyond `request.security()` | PASS |
| No `input()` fields that could inject user data | PASS |
| No `str.format()` with user-controlled strings | PASS |
| Alert JSON via concatenation, not template injection | PASS |
| No `import` statements | PASS |
| No embedded URLs, API keys, or secrets | PASS |

---

## 4. Complexity Assessment

**Rating: Proportionate to strategy complexity.**

- ~350 lines of active code (excluding comments)
- 1 user-defined function (`f_qty()`, 7 lines)
- 4 indicators, 5 filters, 1 state machine (5 states)
- 4 alert payload blocks (verbose but linear)
- No exotic Pine features

The IRB strategy is inherently more complex than EMA Crossover (5-state vs 3-state, stop orders vs market orders, trailing stop vs fixed TP, 5 filters vs 0). The 549-line implementation is proportionate.

---

## 5. Phase 4 Extended Review — Timestamps, Parsing, and Separation of Concerns (2026-03-17)

### 5.1 Timestamp and Bar Reference Clarity

All temporal constructs in `strategy.pine` v2.0.0 were reviewed for definitional clarity and downstream interpretability.

| Reference | Used In | Definition | Downstream Consumer | Risk |
|-----------|---------|-----------|-------------------|------|
| `time_close` | signal_alert `bar_close_time` (L461) | Unix ms of current bar close | Trading Agent: order timestamp | None — Pine built-in, well-defined |
| `bar_index` | Warmup guard (L125) | 0-based bar count from chart start | Internal only | None — not exposed in alerts |
| `pend_bars` | Trigger window (L283-301), cancel alert `bars_elapsed` | Bars since pending order placed | Trading Agent: cancel context | See note below |
| `pos_bars` | Time stop (L384, L404), trail alert `bars_since_entry`, close alert `bars_held` | Bars since position opened | Trading Agent: position age | None — direct measurement |
| `prev_stop` | Trail alert `old_stop` (L489) | Stop level from prior bar | Trading Agent: trail tracking | None — captures correct prior value |

**Note on `bars_elapsed` (cancel alert, L509):** The alert emits `TRIGGER_WIN` (constant 20) instead of the measured `pend_bars`. At the trigger point, `pend_bars >= 20` is guaranteed, and since the counter increments by 1 per bar, `pend_bars` will be exactly 20 the first time the condition is met. Functionally identical. Not a defect, but the Trading Agent should treat this as "the configured window length" rather than "the actual measured count."

**Verdict: All timestamp/bar references are clearly defined and deterministic.**

### 5.2 Pine-Side Downstream Parsing Fragility

The alert JSON is constructed via string concatenation (no JSON library in Pine). Reviewed for risks that would make Trading Agent parsing fragile.

| Risk | Assessment | Evidence | Severity |
|------|-----------|----------|----------|
| **NaN values in JSON** | **Low** — signal alerts are safe; trail alerts have theoretical risk | Signal alert: all numeric fields are guaranteed non-na because `sig_long`/`sig_short` require all 5 filters to pass, which requires non-na indicator values. Trail alert: `atr_val` could theoretically be `na` during an in-position bar (would produce `"NaN"` in JSON). Warmup guard (34 bars) makes this extremely unlikely. | Informational |
| **Malformed JSON from special characters** | **None** — all values are numeric, constants, or controlled enums | No user input, no free-text fields. All string values are hardcoded constants (`"EURUSD"`, `"BUY"`, etc.) or derived from fixed enums. | None |
| **Trailing zero dropping** | **None** — JSON numeric equivalence | `str.tostring(1.10000, "#.#####")` → `"1.1"`. Valid JSON. Any conformant JSON parser produces the same float. Already documented as W1. | None |
| **Multiple alerts per bar** | **Low** — documented, correct behavior | CANCEL + SIGNAL can fire on the same bar (I3). Two separate `alert()` calls produce two webhook events. Trading Agent must handle sequential processing. | Informational |
| **Integer vs float type ambiguity** | **None** — schema types are explicit | `bar_close_time` is emitted as `str.tostring(time_close)` (no format string = integer representation). `trigger_window_bars` is `str.tostring(TRIGGER_WIN)` (integer). Schema types (`"type": "integer"`) disambiguate. | None |

**Verdict: No parsing fragility blockers. One informational note (NaN in trail alert under extreme edge case). Trading Agent should validate JSON on receipt as standard practice.**

### 5.3 Separation of Concerns

Validated the Pine implementation boundary against the 7 concern domains.

| Domain | Pine Responsibility | Evidence | Boundary Correct? |
|--------|-------------------|----------|-------------------|
| **Strategy setup / detection** | Indicator computation, warmup guard | Sections 1-4 (L42-125) | Yes — clean, no runtime dependencies |
| **Trend/context qualification** | 5 filters, combined signal | Sections 5-10 (L128-183) | Yes — pure bar-close data, deterministic |
| **Pending-order lifecycle** | State machine, fill detection, replacement, cancellation | Sections 11, 13, 14 (L186-354) | Yes — models pending order semantics correctly |
| **Strategy-level exits** | Trailing stop, time stop | Section 15 (L361-408) | Yes — exit logic contained, well-scoped |
| **Runtime execution** | NOT in Pine | Deferred to Trading Agent via alerts | Yes — header (L22-26) documents boundary |
| **Risk governance** | NOT in Pine | IC10-IC15 deferred to risk gate | Yes — traceability confirms (§12-§13) |
| **Monitoring/reconciliation** | NOT in Pine | Deferred to Trading Agent + evidence pipeline | Yes — `_implementation_notes.not_in_scope` in schema |

**Places where Pine takes on runtime responsibilities: NONE.**
- `strategy.entry()`/`strategy.exit()`/`strategy.close()` are Pine backtest engine calls — required for TradingView strategy testing but NOT used for live execution. The alert payloads are the live execution interface.
- `strategy.equity` in position sizing (L220, L441) is used for both backtest accuracy AND alert payload `risk_dollars`. In live execution, the Trading Agent should use its own equity snapshot (may differ slightly from Pine's internal equity). This is a known live/backtest divergence, not a boundary violation.

**Places where Pine fails to represent a signal-generation requirement: NONE blocker-level.**
- Spec §9 telemetry lists `irb_threshold` and `filter_results` as signal telemetry fields. Neither appears in the alert payload. However: `irb_threshold` is derivable from `bar_ohlc_h`, `bar_ohlc_l`, and the known 0.45 ratio. `filter_results` is implicit — the signal only fires when ALL filters pass. These are enrichment fields, not authoritative order fields. Not a blocker.

**Verdict: Separation of concerns is correct. Pine stays within its signal-generation boundary. All runtime, risk, and monitoring concerns are properly deferred.**
