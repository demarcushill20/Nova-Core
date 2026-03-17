# NovaTrade Demo Test Run — Phase 1 Summary (IRB)

**Phase:** 1 (Formal Strategy Specification) — RESTARTED for IRB
**Date:** 2026-03-17
**Status:** COMPLETE — all blockers resolved, all U1-U8 quantified, ready for Phase 2
**Replaces:** EMA Crossover Phase 1 summary (2026-03-16)

---

## What Was Produced

The Rob Hoffman IRB (Inventory Retracement Bar) strategy has been formally specified as a machine-readable contract with atomic, deterministic rules suitable for implementation in Pine and execution by the NovaTrade pipeline. All 10 adopted rules (A1-A10) and all 8 unresolved items (U1-U8) from the source boundary document have been resolved with explicit rationale.

### Strategy Overview

| Parameter | Value | Source Tag | Rationale |
|-----------|-------|------------|-----------|
| **Strategy** | Rob Hoffman IRB | [A1]-[A10] | Competition-proven, publicly documented |
| **IRB Geometry** | O and C >= 45% from extreme | [A1] | Canonical definition from S1/S2 |
| **Trend Filter** | EMA(20) slope >= 0.4 (normalized by ATR) | [A7][U1] | TF-B quantified slope; sweepable 0.2-0.8 |
| **MTF Alignment** | H4 EMA(20) rising/falling over 5 bars | [A8] | Required by source ("critical") |
| **Entry** | Stop order: IRB extreme ± 1 pip | [A2] | Conditional entry — confirms trend resumption |
| **Stop Loss** | Dynamic: IRB opposite side ± 1 pip | [A3] | Thesis stop — invalidates continuation premise |
| **Trailing Stop** | ATR(14) × 1.5 from best close | [A9][U3] | Exit-2 variant — fully deterministic |
| **Time Stop** | 40 H1 bars | [U3] | Safety net against infinite holds |
| **Trigger Window** | 20 bars hard cancel | [U5] | Prevents stale pending orders |
| **IRB Replacement** | New IRB replaces old pending order | [A4] | Source rule — window resets |
| **Sideways Filter** | ADX(14) >= 20 + slope threshold | [A6][U4] | Double guard against ranging markets |
| **ATR Filter** | IRB range / ATR(14) <= 2.0 | [A10][U2] | Rejects overextended candles |
| **Position Sizing** | Risk-based: 1% of equity per trade | [A5][U7] | Dynamic stop requires dynamic sizing |
| **Volume** | Dynamic (0.01-1.0 lots) | [U7] | Clamped per risk gate bounds |
| **Body Filter** | Excluded | [U6] | Community variant, not canonical |
| **Invalidation** | Redundant with stop loss | [U8] | Same price level as A3 |
| **Session** | 24/5, no filter | — | Maximum signal generation |
| **Weekend** | Hold positions and pending orders | — | FTMO permits |

### Key Design Choices

1. **TF-B quantified slope over TF-A simple rising/falling.** TF-A is too permissive — any uptick in EMA counts as a trend. TF-B with ATR normalization provides a dimensionless slope metric that approximates the source's "45-degree" visual description. Threshold s=0.4 is conservative.

2. **ATR trailing over S/R proxy trailing.** Exit-1 (S/R proxy) requires identifying "major S/R levels" — this is semi-discretionary even with deterministic proxies (which pivot? which prior high?). Exit-2 (ATR trail) is fully deterministic and requires no additional data beyond ATR(14).

3. **Risk-based sizing over fixed lots.** The source explicitly states <1% risk per trade. With dynamic stop distances (IRB opposite side), fixed lots would violate this constraint on some trades. Risk-based sizing is faithful to the source.

4. **No opposing-direction reversal.** Unlike EMA crossover, IRB signals while an opposite position is open are ignored. IRB is a continuation setup — reversing contradicts the strategy logic.

5. **Hard trigger window over soft preference.** Source says breakout "ideally" within 20 bars. Hard cancellation prevents stale orders and aligns with the IRB replacement rule.

6. **ADX + slope as dual sideways filter.** The slope threshold alone might miss choppy markets with oscillating EMA. ADX provides an independent directional-strength measure. Both are cheap to compute.

---

## Files Created / Modified

| File | Purpose |
|------|---------|
| `docs/demo_test_run/strategy_spec.yaml` | Full formal IRB strategy contract (11 sections, ~500 lines) |
| `docs/demo_test_run/strategy_test_vectors.yaml` | 25 deterministic test vectors covering all IRB scenarios |
| `docs/demo_test_run/strategy_validation_checklist.md` | 8-section validation with completeness, determinism, traceability, runtime, risk gate, coverage, doctrine, approval checks |
| `docs/demo_test_run/phase1_assumptions.md` | 18 assumptions (5 inherited + 13 new), risk-assessed |
| `docs/demo_test_run/phase1_open_issues.md` | 8 resolved (U1-U8), 8 new issues (3 medium, 5 low), 9 deferred items |
| `docs/demo_test_run/phase1_summary.md` | This file |

---

## Validation Results

| Category | Checks | Result |
|----------|--------|--------|
| Completeness | 15/15 | PASS |
| Determinism | 14/14 | PASS |
| Source Traceability | 6/6 | PASS |
| Runtime alignment | 14/14 | PASS |
| Risk gate compatibility | 13/13 | PASS |
| Test vector coverage | 25 vectors | PASS |
| Doctrine compliance | 8/8 | PASS |
| Approval gates | 9/9 | PASS (all gates cleared) |

---

## U1-U8 Resolution Summary

| Item | Source Ambiguity | Resolution | Rationale |
|------|-----------------|------------|-----------|
| **U1** | "45° slope" is visual | TF-B: `(EMA20[t]-EMA20[t-20])/ATR14 >= 0.4` | ATR-normalized slope; sweepable 0.2-0.8 |
| **U2** | "Abnormally large range" — no threshold | `(H-L)/ATR(14) <= 2.0` | Midpoint of 1.5-3.0 range per S2 |
| **U3** | Trailing concept clear, mechanics vary | Exit-2: `max(best_close - 1.5*ATR14, stop)` + 40-bar time stop | Fully deterministic; avoids discretionary S/R |
| **U4** | "Don't trade sideways" — no detection method | ADX(14) >= 20 + TF-B slope threshold | Double guard; both cheap to compute |
| **U5** | "Within 20 bars" — preference or hard rule? | Hard cancel at 20 bars | Per S2 recommendation; prevents stale orders |
| **U6** | Body-size filter — community variant? | Excluded | Not canonical per S2; test as variant later |
| **U7** | <1% risk — fixed lots or risk-based? | Risk-based, r=0.01 | Dynamic stop requires dynamic sizing |
| **U8** | Invalidation separate from SL? | Redundant — same price level | No separate exit needed |

---

## Blockers

| ID | Blocker | Impact |
|----|---------|--------|
| Q1 | ~~Operator must approve strategy~~ | **RESOLVED** — IRB approved 2026-03-17 |
| Q2 | ~~FTMO trial expiration~~ | **RESOLVED** — expires 2026-03-28 |
| U1-U8 | ~~All unresolved items~~ | **RESOLVED** — all quantified with rationale |

**No remaining blockers.**

---

## What Should Happen Next

1. **Compute strategy spec SHA-256 hash** — Record the file hash in strategy_spec.yaml §10.
2. **Phase 2 begins** — Pine Implementation Agent translates `strategy_spec.yaml` into a PineScript v5 strategy that implements IRB detection, stop order entry, dynamic SL, ATR trailing, and the 5-state machine.

---

## Recommended Next Prompt for Phase 2

> Execute Phase 2 of the NovaTrade Demo Test Run Implementation Plan: Pine Implementation.
> The operator has approved the Rob Hoffman IRB strategy specification.
> Using `strategy_spec.yaml` (IRB v2.0.0) as the immutable source of truth, implement a
> PineScript v5 strategy that:
> 1. Detects IRB candles using the 45% geometry rule [A1]
> 2. Validates trend with EMA(20) normalized slope >= 0.4 [A7][U1]
> 3. Checks H4 MTF alignment via `request.security()` with `barmerge.lookahead_off` [A8]
> 4. Applies ADX(14) >= 20 sideways filter [A6][U4]
> 5. Applies ATR(14) overextension filter (range/ATR <= 2.0) [A10][U2]
> 6. Places BUY_STOP/SELL_STOP orders at IRB extreme ± 1 pip [A2]
> 7. Sets dynamic SL at IRB opposite side ± 1 pip [A3]
> 8. Implements ATR trailing stop (1.5 × ATR from best close) [A9][U3]
> 9. Implements 40-bar time stop [U3]
> 10. Implements IRB replacement logic [A4]
> 11. Implements 20-bar trigger window with hard cancel [U5]
> 12. Implements risk-based position sizing (1% of equity) [A5][U7]
> 13. Carries source tags in code comments
> 14. Passes all 25 test vectors from `strategy_test_vectors.yaml`
>
> Do not modify the strategy spec. Do not modify execution code. Stop at the completed Pine script.

---

**Phase 1 complete — all gates PASS, all U1-U8 resolved. Ready for Phase 2 (Pine Implementation).**
