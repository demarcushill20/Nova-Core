# NovaTrade Demo Test Run — Phase 2 Summary

**Phase:** 2 (Pine Implementation)
**Date:** 2026-03-17
**Status:** COMPLETE — ready for Phase 3
**Spec:** strategy_spec.yaml v2.0.0 (Rob Hoffman IRB)
**Pine:** strategy.pine v2.0.0

---

## What Was Implemented

The approved Phase 1 strategy specification (`strategy_spec.yaml` v2.0.0 — Rob Hoffman IRB) was translated into a deterministic PineScript v5 strategy (548 lines) with full traceability and no logic drift.

### Implementation Decisions

| Decision | Choice | Spec Basis | Rationale |
|----------|--------|------------|-----------|
| Pine version | v5 | N/A (Pine-specific) | Latest stable PineScript; required for `request.security()`, `ta.dmi()`, `alert()` |
| Script type | `strategy()` | Needed for Phase 3 backtesting | Supports `strategy.entry()`, `strategy.exit()`, position tracking, stop orders |
| `calc_on_every_tick` | `false` | AR1, AR2 | Bar-close-only evaluation; strongest anti-repaint protection |
| `process_orders_on_close` | `false` | §4.execution_timing | Stop orders fill at trigger price during the bar (realistic for stop entries) |
| `pyramiding` | `0` | §4.4, §5.7 | Max 1 position; duplicate/overlap prevention |
| Entry type | `strategy.entry(..., stop=)` | §4.order_type: STOP | Pending stop orders (BUY_STOP / SELL_STOP) |
| Position sizing | `f_qty(ep, sp)` risk-based | §5.6, A5, U7 | `lot_size = (equity × 0.01) / (sd × $10)`, clamped [0.01, 1.00] |
| SL placement | IRB opposite side ± 1 pip | §5.1, A3 | Dynamic per-trade; no fixed pip value |
| Exit mechanism | Trailing stop + time stop | §5.3, §5.4, A9, U3 | ATR(1.5×) trail; 40-bar time stop; no fixed TP |
| Warmup | `bar_index >= 34` | §6.warmup, IC1 | 34 bars ensures EMA(20), ATR(14), slope lookback all converged |
| MTF alignment | H1 20-bar lookback on H4 EMA | §3.3, A8 | 20 H1 bars ≈ 5 H4 bars — equivalent temporal window |
| State machine | 5 explicit states with `var` persistence | §6 | FLAT, PENDING_LONG, PENDING_SHORT, LONG, SHORT |
| Alert encoding | JSON string via concatenation | §9 telemetry | 5-decimal precision; `alert.freq_once_per_bar_close`; 4 alert types |
| No reversal | Opposite-direction signals suppressed | §6.key_difference_from_ema | State machine only allows exits via SL/trail/time stop |
| IRB replacement | Same-direction replacement resets window | §4.2, A4 | New `strategy.entry()` call overwrites previous pending order |

---

## One-Position / One-Active-Order Constraints

The spec requires at most one position OR one pending order active at any time (§4.4). This is enforced at two levels:

1. **Pine `pyramiding = 0`** — prevents multiple simultaneous positions at the engine level
2. **Explicit state machine** — signals are only processed when:
   - `state == S_FLAT` → new signal allowed (transitions to PENDING)
   - `state == S_PLONG` and `sig_long` → same-direction replacement allowed
   - `state == S_PSHORT` and `sig_short` → same-direction replacement allowed
   - All other combinations → signal suppressed (no action taken)

This dual enforcement ensures no race conditions or edge cases can create multiple positions or multiple pending orders.

---

## Risk-Aware Payload Fields

The signal_alert JSON payload includes all fields required by downstream systems (risk gate, Trading Agent, MetaApi) for safe order execution:

| Field | Type | Purpose |
|-------|------|---------|
| `entry_price` | number | Authoritative stop order trigger price |
| `stop_loss` | number | Authoritative SL price for order placement |
| `stop_distance_pips` | number | Pre-computed SL distance for risk gate validation |
| `volume` | number | Risk-based lot size [0.01, 1.00] for order placement |
| `risk_dollars` | number | Dollar risk amount (equity × 0.01) for risk gate check |
| `side` | string | BUY / SELL — order direction |
| `order_type` | string | BUY_STOP / SELL_STOP — order type for MetaApi |
| `symbol` / `broker_symbol` | string | Symbol resolution for Trading Agent |

The trail_alert (`MODIFY_SL`) includes `new_stop` as the authoritative SL level for position modification.

---

## What Was Intentionally NOT Implemented (Belongs to Later Phases)

| Feature | Spec Ref | Responsible Agent | Phase |
|---------|----------|-------------------|-------|
| Risk gate checks (spread, drawdown, kill switch, etc.) | IC10–IC15 | Risk Management Agent | Phase 4+ |
| Cooldown enforcement (60 seconds) | IC15, §5.9 | Risk Management Agent | Phase 4+ |
| Evidence JSONL logging | §9 | Trading Agent | Phase 4+ |
| Position reconciliation | Success criteria | Trading Agent | Phase 4+ |
| Pending order management on MetaApi | §4.1 | Trading Agent | Phase 4+ |
| Trailing stop SL modification on MetaApi | §5.3 | Trading Agent | Phase 4+ |
| Time stop market close on MetaApi | §5.4 | Trading Agent | Phase 4+ |
| Strategy contract hash verification at run start | §10 | Trading Agent | Phase 4+ |

---

## Where Pine Exactly Matches the Spec

134 spec rules were mapped in `spec_traceability.md`. Of these:

- **125 exact matches** — Pine code implements the spec rule with 1:1 fidelity
- **3 representation choices** — Pine forces a specific encoding (documented and conservative):
  1. MTF H4 alignment uses H1 20-bar lookback (≈5 H4 bars) rather than direct H4 bar reference
  2. MTF H4 alignment includes `na` guards (conservative addition not in spec)
  3. SL/trail intra-bar priority determined by Pine engine (spec says SL > trail > time)
- **6 runtime-only** — correctly deferred to Trading Agent / Risk Gate

---

## Where Pine Required Explicit Representation Choices

| Choice | Spec Intent | Pine Representation | Risk |
|--------|------------|-------------------|------|
| MTF lookback | H4 5-bar lookback | H1 20-bar lookback on H4 EMA | Low — equivalent temporal window |
| MTF na guard | Not specified | `not na(ema_h4)` checks | Negligible — conservative |
| SL/trail priority | SL > trail > time | Unified `strategy.exit(stop=cur_stop)` + separate `strategy.close()` for time | Negligible — SL and trail are merged into one stop level |

---

## Pine-Specific Interpretations and Boundary Decisions

The following aspects of the strategy spec cannot be perfectly expressed in Pine and are documented for reviewer awareness:

| Boundary | Spec Intent | Pine Representation | Broker-Side Reality | Assumptions |
|----------|------------|-------------------|-------------------|-------------|
| Pending-order state | Stop order lives on broker server | `strategy.entry(..., stop=ep)` creates Pine-internal pending order; 5-state machine tracks state via `var int state` | Trading Agent places real MetaApi pending order from alert payload | PA-IRB-3, PA-IRB-9 |
| H4 EMA safety | Read H4 data without future leakage | `request.security(..., "240", ..., lookahead=barmerge.lookahead_off)` with `na` guards | N/A (Pine-only concern) | PA-IRB-1 |
| Signal identifiers | Enum-like action/type fields | String literals constructed in alert JSON (e.g., `"LONG_IRB"`, `"BUY_STOP"`) | Trading Agent parses JSON and routes on these strings | PA-IRB-13 |
| Trailing stop values | Desired SL level, tighten-only | `cur_stop` updated via `math.max`/`math.min`; emitted as `new_stop` in MODIFY_SL alert | Trading Agent calls `modifyPosition(stopLoss=new_stop)` on MetaApi | PA-IRB-15 |
| Fill mechanics | Stop order fills at trigger price | Pine fills at stop price intra-bar | Broker fill includes spread, slippage, possible re-quote | PA-IRB-14 |
| Fields deferred to runtime | Risk gate checks, fill details, P&L | Not computed in Pine; documented as "Runtime-only" in traceability map | Trading Agent computes at fill/close time | See spec_traceability.md §13 |

---

## What Phase 3 Should Verify

1. **Signal correctness** — Verify IRB geometry detection produces valid uptrend/downtrend signals on historical data
2. **Filter interaction** — Verify all 5 filters (geometry, trend, MTF, sideways, overextension) independently suppress signals when conditions fail
3. **State machine** — Verify FLAT→PENDING→POSITION→FLAT transitions in backtest trade list
4. **One-position constraint** — Verify no overlapping positions or multiple pending orders in backtest
5. **IRB replacement** — Verify same-direction replacement resets window and updates levels
6. **Trigger window** — Verify pending orders cancel after 20 bars of non-trigger
7. **Trailing stop** — Verify stop only tightens (never widens) during position hold
8. **Time stop** — Verify positions close at 40 bars if not previously exited
9. **Warmup** — Verify no trades in the first 34 bars
10. **Anti-repaint** — Confirm no signal changes on real-time bars
11. **Trade count** — Document signal frequency on EURUSD H1
12. **Data feed** — Document which TradingView data feed is used
13. **Alert payload** — Verify alert JSON is well-formed and matches `alerts_schema.json`

---

## Files

| File | Purpose | Location |
|------|---------|----------|
| `strategy.pine` | PineScript v5 IRB strategy (17 sections, 548 lines) | `docs/demo_test_run/strategy.pine` |
| `alerts_schema.json` | Alert payload JSON schema (4 alert types) | `docs/demo_test_run/alerts_schema.json` |
| `spec_traceability.md` | 134-rule spec-to-code mapping across 14 categories | `docs/demo_test_run/spec_traceability.md` |
| `phase2_assumptions.md` | 15 Pine-specific assumptions (PA-IRB-1 to PA-IRB-15), all low/negligible risk | `docs/demo_test_run/phase2_assumptions.md` |
| `phase2_open_issues.md` | 4 inherited issues, 7 new non-blocking issues, 7 deferred items | `docs/demo_test_run/phase2_open_issues.md` |
| `phase2_summary.md` | This file | `docs/demo_test_run/phase2_summary.md` |

---

## Assumptions

15 Pine-specific assumptions (PA-IRB-1 to PA-IRB-15) documented in `phase2_assumptions.md`. 8 rated negligible risk, 7 rated low risk. No medium or high-risk assumptions introduced.

---

## Open Issues

No blockers. 7 new non-blocking issues (P-IRB-1 to P-IRB-7) documented in `phase2_open_issues.md`. 7 items deferred to Phase 4+ (D-IRB-1 to D-IRB-7).

---

**Phase 2 complete — ready for Phase 3.**

STOPPED AT PHASE 2 — NO LATER PHASE WORK PERFORMED.
