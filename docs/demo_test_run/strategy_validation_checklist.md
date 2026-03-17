# NovaTrade Demo Test Run — Strategy Validation Checklist (IRB)

**Phase:** 1 (Formal Strategy Specification) — RESTARTED for IRB
**Date:** 2026-03-17
**Status:** LOCKED
**Replaces:** EMA Crossover validation checklist (2026-03-16)

---

## Purpose

This checklist validates the IRB strategy specification contract (`strategy_spec.yaml`) against project doctrine, runtime constraints, source traceability, and implementation requirements. Every item must PASS before Phase 2 (Pine Implementation) can begin.

---

## 1. Completeness Checks

| ID | Check | Status | Notes |
|----|-------|--------|-------|
| C1 | Metadata section complete (name, version, symbol, timeframe, account) | PASS | All fields populated; source documents listed |
| C2 | Indicator definitions include type, period, source, timeframe | PASS | EMA(20) H1, EMA(20) H4, ATR(14), ADX(14) |
| C3 | IRB geometry detection rules with 45% threshold formula | PASS | Uptrend and downtrend IRB with explicit formulas [A1] |
| C4 | Signal rules define LONG_IRB, SHORT_IRB, and NO_SIGNAL conditions | PASS | Five-filter signal logic with source tags |
| C5 | Execution rules define order type (STOP), entry levels, and volume sizing | PASS | BUY_STOP/SELL_STOP, risk-based sizing [A2][U7] |
| C6 | Risk rules define SL (dynamic), trailing stop, time stop | PASS | IRB-opposite SL [A3], ATR trail [U3], 40-bar time stop |
| C7 | State rules define all 5 states and valid transitions | PASS | FLAT/PENDING_LONG/PENDING_SHORT/LONG/SHORT |
| C8 | Session policy defined | PASS | 24/5, no session filter, weekend hold |
| C9 | Invalid trade conditions enumerated | PASS | 15 conditions (IC1–IC15) |
| C10 | Telemetry requirements specify all evidence fields | PASS | signal, pending_order, fill, trail_update, close, order_cancel |
| C11 | Contract integrity section defines immutability rules | PASS | Hash placeholder, modification policy |
| C12 | All 8 unresolved items (U1-U8) resolved with rationale | PASS | Section 11 documents all resolutions |
| C13 | IRB replacement rule specified | PASS | [A4] — new IRB replaces old pending order |
| C14 | Trigger window specified | PASS | [U5] — hard cancel at 20 bars |
| C15 | Source document references included | PASS | S1, S2 with locations |

---

## 2. Determinism Checks

| ID | Check | Status | Notes |
|----|-------|--------|-------|
| D1 | IRB geometry detection is boolean (45% threshold comparison) | PASS | Strict <=/>= comparisons on O, C vs threshold |
| D2 | Signal evaluation timing is unambiguous (bar close only) | PASS | on_bar_close, anti-repaint rules AR1–AR4 |
| D3 | Entry price is unambiguous | PASS | IRB high/low ± 1 pip — fixed formula [A2] |
| D4 | Stop-loss is unambiguous | PASS | IRB opposite side ± 1 pip — fixed formula [A3] |
| D5 | Trailing stop is formula-based, not discretionary | PASS | ATR(14) × 1.5 from highest/lowest close [U3] |
| D6 | Position sizing is formula-based | PASS | risk_dollars / (stop_pips × pip_value) [U7] |
| D7 | State machine has no ambiguous transitions | PASS | Every state+trigger pair has exactly one target |
| D8 | Trend filter is quantified (not visual) | PASS | Normalized slope >= 0.4 [U1] |
| D9 | Sideways filter is quantified | PASS | ADX(14) >= 20 [U4] |
| D10 | Overextension filter is quantified | PASS | range/ATR <= 2.0 [U2] |
| D11 | Trigger window is hard (not soft preference) | PASS | 20 bars hard cancel [U5] |
| D12 | MTF alignment check is quantified | PASS | H4 EMA(20) rising/falling over 5 bars [A8] |
| D13 | Edge case: both-direction IRB has defined resolution | PASS | Trend filter determines direction (TV24) |
| D14 | Edge case: zero-range candle excluded | PASS | range > 0 check (TV8) |

---

## 3. Source Traceability Checks

| ID | Check | Status | Notes |
|----|-------|--------|-------|
| ST1 | Every adopted rule carries [A1]-[A10] tag | PASS | All 10 rules tagged in spec |
| ST2 | Every quantification choice carries [U1]-[U8] tag | PASS | All 8 resolutions tagged with rationale |
| ST3 | Excluded items listed with [EXCLUDED] tag | PASS | Charter §Out of Scope; irb_source_boundary.md |
| ST4 | No smuggled assumptions (rules without source tag) | PASS | All rules traceable to A/U tags |
| ST5 | Source documents S1, S2 referenced in metadata | PASS | Both PDFs listed with locations |
| ST6 | Body-size filter explicitly excluded with rationale | PASS | [U6] — community variant, not canonical |

---

## 4. Runtime Alignment Checks

| ID | Check | Expected (config.py) | Spec Value | Status |
|----|-------|---------------------|------------|--------|
| R1 | Stop-loss required | `require_stop_loss=True` | Yes, dynamic IRB-opposite [A3] | PASS |
| R2 | Max positions | `max_positions=5` | Strategy uses 1, gate allows 5 | PASS |
| R3 | Min volume | `min_volume_per_trade=0.01` | Dynamic, min clamped to 0.01 | PASS |
| R4 | Max volume | `max_volume_per_trade=1.0` | Dynamic, max clamped to 1.0 | PASS |
| R5 | Spread ceiling | `spread_ceiling_points=30.0` | Acknowledged in IC10 | PASS |
| R6 | Max daily drawdown | `max_daily_drawdown_pct=5.0` | 5.0% aligned | PASS |
| R7 | Max total drawdown | `max_total_drawdown_pct=10.0` | 10.0% aligned | PASS |
| R8 | Cooldown | `cooldown_seconds=60` | 60s aligned, acknowledged in IC15 | PASS |
| R9 | Max trades per day | `max_trades_per_day=20` | 20 aligned, expected 0-2 | PASS |
| R10 | Symbol allowlist | `symbols=EURUSD` → `EURUSD.sim` | EURUSD.sim | PASS |
| R11 | Dry run gate | `dry_run=True` default | Acknowledged in IC13 | PASS |
| R12 | Account mode | `DEMO` only in MVP | DEMO specified | PASS |
| R13 | Kill switch integration | kill_switch check #1 | Acknowledged in IC12 | PASS |
| R14 | Stop order support | MetaApi supports BUY_STOP/SELL_STOP | Required by [A2] | PASS |

---

## 5. Risk Gate Compatibility

| Gate Check | Strategy Compatible? | Notes |
|------------|---------------------|-------|
| 1. kill_switch | Yes | Strategy halts if kill switch active [IC12] |
| 2. dry_run | Yes | Must be disabled before live trading [IC13] |
| 3. account_mode | Yes | DEMO only |
| 4. health | Yes | Strategy does not trade when adapter DOWN [IC14] |
| 5. symbol_allowed | Yes | EURUSD.sim resolved from EURUSD |
| 6. volume_bounds | Yes | Dynamic lot clamped to [0.01, 1.0] |
| 7. stop_loss | Yes | Dynamic SL on every order [A3] |
| 8. max_positions | Yes | Strategy holds max 1, gate allows 5 |
| 9. daily_trade_count | Yes | Expected 0-2/day, limit 20 |
| 10. cooldown | Yes | H1 bars are 3600s apart >> 60s cooldown |
| 11. duplicate_position | Yes | Strategy prevents duplicates via state machine |
| 12. drawdown | Yes | FTMO limits acknowledged |
| 13. spread | Yes | IC10 documents spread denial |

---

## 6. Test Vector Coverage

| Scenario | Vector ID |
|----------|-----------|
| Valid uptrend IRB — LONG signal | TV1 |
| Valid downtrend IRB — SHORT signal | TV2 |
| IRB valid but trend filter fails | TV3 |
| IRB valid but H4 MTF misaligned | TV4 |
| IRB valid but ADX too low (sideways) | TV5 |
| IRB overextended (ATR filter rejects) | TV6 |
| Normal candle — not an IRB | TV7 |
| Doji candle — zero range | TV8 |
| Buy-stop triggered — enter LONG | TV9 |
| Sell-stop NOT triggered — pending | TV10 |
| Trigger window expired — cancel | TV11 |
| IRB replacement — new IRB replaces | TV12 |
| Trailing stop tightens (LONG) | TV13 |
| Trailing stop hit — exit LONG | TV14 |
| Stop-loss hit — exit SHORT | TV15 |
| Time stop — exit after 40 bars | TV16 |
| LONG signal while SHORT — ignored | TV17 |
| SHORT signal while PENDING_LONG — ignored | TV18 |
| Warmup insufficient | TV19 |
| Risk gate — spread denial | TV20 |
| Risk gate — drawdown denial | TV21 |
| SL/sizing verification — LONG | TV22 |
| SL/sizing verification — SHORT | TV23 |
| Edge case — both-direction IRB | TV24 |
| Trailing stop tightens (SHORT) | TV25 |

**Coverage:** 25 test vectors covering IRB geometry, all 5 filters, 5 state machine states, stop order lifecycle, trailing stop mechanics, IRB replacement, time stop, edge cases, and risk gate interactions.

---

## 7. Doctrine Compliance

| ID | Doctrine Rule | Compliance | Source |
|----|--------------|------------|--------|
| DC1 | "Spec before code" | PASS — spec complete before Pine implementation | WP1 §2 |
| DC2 | "The trading agent must not learn while trading" | PASS — no adaptive parameters, fixed rules | WP3 non-negotiable |
| DC3 | "Strategies come from credible sources with public specificity" | PASS — Rob Hoffman, 35+ competition wins, publicly documented | WP1 §2 |
| DC4 | "Risk Governor outranks execution at all times" | PASS — all 13 gate checks documented, strategy defers to gate | Charter governance |
| DC5 | "No silent decisions — every action produces evidence" | PASS — telemetry covers full lifecycle including trail updates | Charter governance |
| DC6 | "Contract frozen before run, unmodifiable during run" | PASS — contract_integrity section, failure condition F6 | Charter governance |
| DC7 | "Systems test, not profit test" | PASS — metadata.purpose = systems_test | Phase 0 charter |
| DC8 | "One strategy, one symbol, primary + MTF timeframe" | PASS — EURUSD H1 with H4 MTF confirmation (in scope) | Phase 0 charter (amended) |

---

## 8. Approval Gates

| Gate | Requirement | Status |
|------|------------|--------|
| G1 | Operator approved IRB as strategy type | **PASS** — approved 2026-03-17 |
| G2 | All completeness checks PASS | PASS (15/15) |
| G3 | All determinism checks PASS | PASS (14/14) |
| G4 | All source traceability checks PASS | PASS (6/6) |
| G5 | All runtime alignment checks PASS | PASS (14/14) |
| G6 | All risk gate compatibility checks PASS | PASS (13/13) |
| G7 | Test vector coverage sufficient | PASS (25 vectors) |
| G8 | Doctrine compliance verified | PASS (8/8) |
| G9 | All U1-U8 resolved with rationale | PASS |

**Phase 1 Status: COMPLETE — all gates PASS**
