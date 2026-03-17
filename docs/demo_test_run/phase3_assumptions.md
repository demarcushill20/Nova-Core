# NovaTrade Demo Test Run — Phase 3 Assumptions (Fresh IRB)

**Phase:** 3 (Compile / Lint / Static Validation)
**Date:** 2026-03-17
**Status:** LOCKED
**Agent:** Compiler/Lint Agent
**Replaces:** EMA Crossover Phase 3 assumptions (2026-03-16)

---

## Inherited Assumptions

All assumptions from Phase 0 (A1-A5), Phase 0.5 (IRB source boundary), Phase 1 (A1-A10, U1-U8 resolutions), and Phase 2 (PA-IRB-1 to PA-IRB-15) remain in force. Phase 3 does not alter or contradict any of them.

---

## New Phase 3 Assumptions

| ID | Statement | Rationale | Risk Level | Revisit? |
|----|-----------|-----------|------------|----------|
| CA-IRB-1 | **Static analysis of Pine v5 syntax is sufficient to establish compile-readiness with high confidence** | The Pine script uses standard v5 constructs: `strategy()`, `ta.ema()`, `ta.atr()`, `ta.dmi()`, `request.security()`, `strategy.entry()`, `strategy.exit()`, `strategy.close()`, `strategy.cancel()`, `alert()`, `math.*`, `str.tostring()`, `nz()`, `na()`, `var`, one user-defined function. No exotic features, no imports. `request.security()` is the most complex built-in used — well-documented behavior with `lookahead_off`. 45 specific checks pass (see `compile_report.md`). | Low | Phase 4 will confirm by loading the script in TradingView. If it fails to compile, this assumption was wrong. |
| CA-IRB-2 | **The `"#.#####"` format string in `str.tostring()` produces valid JSON numbers for all EURUSD price fields** | EURUSD prices are in the range ~1.00000-1.50000. The format `#.#####` always produces valid numeric strings. ATR values (~0.00150-0.00400) also format correctly. ADX values (0-100) format correctly. No sub-zero or extremely large values possible for these fields. `trigger_window_bars` is formatted via `str.tostring(int)` with no format string. | Low | Verifiable in Phase 4 by checking alert JSON output. |
| CA-IRB-3 | **The one-bar SL protection gap in Pine backtesting does not invalidate backtest results for a systems test** | After stop-order fill, `strategy.exit()` activates from the next bar. The fill bar has no SL protection in the backtest. For EURUSD H1, typical bar ranges are 15-40 pips. With dynamic SL distances (IRB candle height, typically 15-50 pips), the unprotected bar rarely exceeds the SL distance. Even if some trades show worse P&L, this is a known Pine limitation. In live execution, the Trading Agent places SL immediately. The systems test goal is pipeline validation, not exact P&L measurement. | Low | Phase 4 should flag any trades where loss exceeds the expected SL distance as potential one-bar-gap artifacts. |
| CA-IRB-4 | **The alert payload JSON produced by string concatenation is well-formed for all possible runtime values** | All dynamic values are: (a) numbers formatted by `str.tostring()` which produces valid numeric strings, or (b) predefined string constants (`"Rob Hoffman IRB"`, `"2.0.0"`, `"BUY"`, `"SELL"`, etc.) containing no JSON-special characters. The warmup guard ensures no early-bar NaN values. The `nz()` guards on ATR/EMA prevent `na` values from entering calculations. Enum-like strings (`"LONG_IRB"`, `"PENDING_LONG"`, etc.) are hardcoded — no injection possible. | Low | Phase 4 integration testing should verify at least one alert payload parses correctly. |
| CA-IRB-5 | **Pine's `strategy.position_size` reliably reflects fill state at bar close for stop-order entries** | With `process_orders_on_close = false`, stop orders fill intra-bar when the stop price is reached. At bar close, `strategy.position_size` reflects the filled position. The fill detection logic (`fill_long = now_long and was_flat`) correctly identifies transitions. This is standard Pine behavior for stop-order entries, well-tested across the Pine community. | Negligible | Standard Pine behavior. |
| CA-IRB-6 | **The state machine is complete — no unhandled state × event combinations exist** | All 5 states × all relevant events were analyzed. Every combination either produces a valid transition or is explicitly suppressed. The if/else-if chain at L304-354 covers FLAT + signal, same-direction PENDING + signal. All other combinations (opposite PENDING, in-position) fall through with no action — which is the correct spec behavior (suppress). | Negligible | Verified by exhaustive state × event matrix in lint_report.md. |

---

## Risk Assessment Summary

| Risk Level | Count | IDs |
|------------|-------|-----|
| Negligible | 2 | CA-IRB-5, CA-IRB-6 |
| Low | 4 | CA-IRB-1, CA-IRB-2, CA-IRB-3, CA-IRB-4 |

**No medium or high-risk assumptions in Phase 3.** All assumptions are testable in Phase 4.
