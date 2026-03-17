# NovaTrade Demo Test Run — Phase 3 Open Issues (Fresh IRB)

**Phase:** 3 (Compile / Lint / Static Validation)
**Date:** 2026-03-17
**Status:** LOCKED
**Agent:** Compiler/Lint Agent
**Replaces:** EMA Crossover Phase 3 open issues (2026-03-16)

---

## Inherited Issues (from Phase 0 / Phase 0.5 / Phase 1 / Phase 2)

| ID | Issue | Status | Phase 3 Relevance |
|----|-------|--------|-------------------|
| P4 | Bar alignment between MetaApi and Pine | **Carries forward** | Phase 4 should verify H1 bar boundaries. Not testable in static review. |
| P-IRB-1 | SL/trail intra-bar priority is engine-determined | **Carries forward** | Confirmed: SL and trail share unified `cur_stop`. No conflict possible. |
| P-IRB-2 | MTF H4 uses H1 20-bar lookback (≈5 H4 bars) | **Carries forward** | Independently verified: temporal windows are equivalent. Conservative `na` guards. |
| P-IRB-3 | `na` guard on H4 EMA may suppress early signals | **Carries forward** | Confirmed: conservative suppression is correct. Warmup (34 bars) covers most cases. |
| P-IRB-4 | Pine cannot implement runtime risk gate (IC10-IC15) | **Carries forward** | Confirmed: correctly deferred. Spec marks these as `enforced_by: "risk_gate"`. |
| P-IRB-5 | TradingView data source may differ from broker data | **Carries forward** | Not testable in static review. Phase 4 should document data feed. |
| P-IRB-6 | Position sizing uses dynamic `strategy.equity` | **Carries forward** | Confirmed: consistent with spec "1% of equity". Equity is inherently dynamic. |
| P-IRB-7 | First MODIFY_SL alert has `old_stop` = 0 (from `nz(na)`) | **Carries forward** | Confirmed: initial SL alert is useful for Trading Agent to verify SL placement. Subsequent bars correctly track changes. |

---

## New Phase 3 Issues

| ID | Issue | Severity | Impact | Resolution Path |
|----|-------|----------|--------|-----------------|
| P3-IRB-1 | **One-bar SL protection gap in backtest** | Warning | After stop-order fill on bar N+1, `strategy.exit()` activates from bar N+2. Bar N+1 has no SL protection in the backtest. Potential for individual trade losses exceeding the intended SL distance. | **Accept.** Inherent Pine backtest limitation. Does NOT affect live execution (Trading Agent places SL immediately on MetaApi). Phase 4 should flag trades with losses > expected SL distance as potential artifacts. |
| P3-IRB-2 | **Format string `"#.#####"` gives variable decimal places** | Warning | `str.tostring(1.10000, "#.#####")` produces `"1.1"` not `"1.10000"`. No data loss — JSON `1.1 == 1.10000`. The `entry_price`, `stop_loss`, and `volume` fields are authoritative regardless of formatting. | **Accept.** If strict 5-digit formatting is desired later, change format to `"0.00000"`. Not required for Phase 4. |
| P3-IRB-3 | **No direct Pine compiler verification in this environment** | Warning | Static analysis covers 45 checks with high confidence, but actual TradingView compilation has not been performed. A compiler failure would be a Phase 4 blocker. | **Accept for Phase 3.** Phase 4 MUST confirm compilation by loading the script in TradingView. Any failure = CA-IRB-1 assumption violated. |
| P3-IRB-4 | **`dp` and `dm` from `ta.dmi()` are unused** | Informational | Pine compiler will emit warnings for unused tuple variables. Not errors. Standard `ta.dmi()` usage — all 3 return values must be destructured. | **Accept.** No fix available. |

---

## Deferred to Later Phases

| ID | Item | Deferred To | Reason |
|----|------|-------------|--------|
| P4 | Bar alignment verification | Phase 4 (Backtesting) | Requires TradingView chart + broker data comparison |
| P-IRB-5 | Data source divergence | Phase 4 (Backtesting) | Requires loading strategy on actual TradingView chart |
| D-IRB-1 to D-IRB-7 | Trading Agent concerns | Phase 4+ (Trading Agent) | Evidence schema, webhook integration, pending order management, alert parsing, trailing stop modification, time stop execution, position reconciliation — all Trading Agent responsibilities |

---

## Issue Summary

| Severity | Count | IDs |
|----------|-------|-----|
| Blocker | 0 | — |
| Must-fix before Phase 4 | 0 | — |
| Warning | 3 | P3-IRB-1, P3-IRB-2, P3-IRB-3 |
| Informational | 1 | P3-IRB-4 |
| Inherited (carries forward) | 8 | P4, P-IRB-1 through P-IRB-7 |

**No blockers. No must-fix items. Phase 4 is not blocked by any Phase 3 finding.**
