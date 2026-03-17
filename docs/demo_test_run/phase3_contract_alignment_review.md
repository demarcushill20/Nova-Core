# NovaTrade Demo Test Run — Phase 3 Spec-to-Code Alignment Review (Fresh IRB)

**Phase:** 3 (Compile / Lint / Static Validation)
**Date:** 2026-03-17
**Status:** ALIGNED — no implementation drift detected
**Agent:** Compiler/Lint Agent
**Spec:** strategy_spec.yaml v2.0.0 (Rob Hoffman IRB)
**Pine:** strategy.pine v2.0.0 (549 lines)
**Replaces:** EMA Crossover alignment review (2026-03-16)

---

## 1. Review Method

Every rule in `strategy_spec.yaml` v2.0.0 was checked against `strategy.pine` using the Phase 2 `spec_traceability.md` as a guide. Each rule was independently re-verified (not merely accepting Phase 2's self-assessment). Findings are categorized as exact match, representation choice, runtime-only (deferred), or mismatch.

---

## 2. Indicator Rules [Spec §2]

| Spec Ref | Spec Rule | Pine Code | Line | Verdict |
|----------|-----------|-----------|------|---------|
| §2.ema_20_h1 | EMA(20), source=close, H1 | `ta.ema(close, EMA_PERIOD)` where `EMA_PERIOD=20` | L105 | **Exact** |
| §2.atr_14 | ATR(14), H1 | `ta.atr(ATR_PERIOD)` where `ATR_PERIOD=14` | L108 | **Exact** |
| §2.adx_14 | ADX(14), H1 | `ta.dmi(ADX_PERIOD, ADX_PERIOD)` where `ADX_PERIOD=14` | L111 | **Exact** |
| §2.ema_20_h4 | EMA(20), source=close, H4 | `request.security(..., "240", ta.ema(close, EMA_PERIOD), lookahead=barmerge.lookahead_off)` | L114-116 | **Exact** [AR4] |

---

## 3. IRB Geometry Detection [Spec §3.1, A1]

| Spec Ref | Spec Formula | Pine Code | Line | Verdict |
|----------|-------------|-----------|------|---------|
| IRB_UP_1 | range = high - low | `bar_rng = high - low` | L131 | **Exact** |
| IRB_UP_2 | range > 0 | `valid_rng = bar_rng > 0` | L132 | **Exact** |
| IRB_UP_3 | threshold = high - (0.45 × range) | `up_thresh = high - IRB_PCT * bar_rng` | L135 | **Exact** |
| IRB_UP_4 | open ≤ threshold AND close ≤ threshold | `is_up_irb = valid_rng and (open <= up_thresh) and (close <= up_thresh)` | L136 | **Exact** |
| IRB_DN_3 | threshold = low + (0.45 × range) | `dn_thresh = low + IRB_PCT * bar_rng` | L139 | **Exact** |
| IRB_DN_4 | open ≥ threshold AND close ≥ threshold | `is_dn_irb = valid_rng and (open >= dn_thresh) and (close >= dn_thresh)` | L140 | **Exact** |

---

## 4. Signal Filter Rules [Spec §3.2-§3.5]

| Spec Ref | Spec Rule | Pine Code | Line | Verdict |
|----------|-----------|-----------|------|---------|
| TF1 | ema_slope = (ema[t] - ema[t-20]) / atr[t] | `ema_slope = nz(atr_val) > 0 ? (ema_h1 - nz(ema_h1[SLOPE_LOOKBACK])) / atr_val : 0.0` | L147-148 | **Exact** (+div guard) |
| TF2_LONG | ema_slope ≥ 0.4 | `trend_up = ema_slope >= SLOPE_THRESH` where `SLOPE_THRESH=0.4` | L150 | **Exact** |
| TF2_SHORT | ema_slope ≤ -0.4 | `trend_dn = ema_slope <= -SLOPE_THRESH` | L151 | **Exact** |
| MTF1_LONG | ema_h4[t] > ema_h4[t-5] | `h4_up = not na(ema_h4) and not na(ema_h4[MTF_H1_LOOKBACK]) and (ema_h4 > ema_h4[MTF_H1_LOOKBACK])` | L162 | **Representation** — H1 20-bar ≈ H4 5-bar + na guards |
| MTF1_SHORT | ema_h4[t] < ema_h4[t-5] | `h4_dn = not na(ema_h4) and not na(ema_h4[MTF_H1_LOOKBACK]) and (ema_h4 < ema_h4[MTF_H1_LOOKBACK])` | L163 | **Representation** — same approach |
| SW1 | adx_14 ≥ 20 | `sw_pass = adx_val >= ADX_THRESH` where `ADX_THRESH=20` | L169 | **Exact** |
| OE1 | (high-low)/atr ≤ 2.0 | `oe_ratio = nz(atr_val) > 0 ? bar_rng / atr_val : 0.0; oe_pass = oe_ratio <= OE_THRESH` where `OE_THRESH=2.0` | L175-176 | **Exact** (+div guard) |

---

## 5. Combined Signal Logic [Spec §3.6]

| Spec Ref | Spec Logic | Pine Code | Line | Verdict |
|----------|-----------|-----------|------|---------|
| §3.6 long | IRB_UP_4 AND TF2_LONG AND MTF1_LONG AND SW1 AND OE1 | `sig_long = warmup_ok and is_up_irb and trend_up and h4_up and sw_pass and oe_pass` | L182 | **Exact** (+warmup from IC1) |
| §3.6 short | IRB_DN_4 AND TF2_SHORT AND MTF1_SHORT AND SW1 AND OE1 | `sig_short = warmup_ok and is_dn_irb and trend_dn and h4_dn and sw_pass and oe_pass` | L183 | **Exact** (+warmup from IC1) |

---

## 6. Entry / Execution Rules [Spec §4]

| Spec Ref | Spec Rule | Pine Code | Line | Verdict |
|----------|-----------|-----------|------|---------|
| §4 order_type | STOP | `strategy.entry(..., stop=ep)` | L308, L321 | **Exact** |
| §4.1 long_entry | BUY_STOP at irb_high + 1 pip | `float ep = high + PIP_BUF` where `PIP_BUF=0.0001` | L305 | **Exact** |
| §4.1 short_entry | SELL_STOP at irb_low - 1 pip | `float ep = low - PIP_BUF` | L318 | **Exact** |
| §4.1 volume | risk_based | `f_qty(ep, sp)` with 1% risk formula | L307, L320 | **Exact** |
| §4.2 replacement | Cancel old, place new, reset window | Same-direction `strategy.entry()` + `pend_bars := 0` | L331-352 | **Exact** |
| §4.3 trigger_window | Hard cancel at 20 bars | `pend_bars >= TRIGGER_WIN → strategy.cancel()` | L283-301 | **Exact** |
| §4.4 one_position | Max 1 position or pending order | `pyramiding=0` + state machine gating | L36, L304-354 | **Exact** (dual enforcement) |

---

## 7. Risk / Exit Rules [Spec §5]

| Spec Ref | Spec Rule | Pine Code | Line | Verdict |
|----------|-----------|-----------|------|---------|
| §5.1 SL long | irb_low - 1 pip | `cur_stop := irb_lo - PIP_BUF` | L263 | **Exact** |
| §5.1 SL short | irb_high + 1 pip | `cur_stop := irb_hi + PIP_BUF` | L270 | **Exact** |
| §5.1 modification | only_tighten | `math.max(nz(cur_stop), tl)` (long); `math.min(...)` (short) | L378, L398 | **Exact** |
| §5.2 no_fixed_TP | type: none | No `limit` parameter in `strategy.exit()` | L381, L401 | **Exact** |
| §5.3 trail long | max(highest_close - 1.5×ATR, stop) | `best_cl := math.max(...); tl = best_cl - TRAIL_MULT * nz(atr_val); cur_stop := math.max(...)` | L372-378 | **Exact** |
| §5.3 trail short | min(lowest_close + 1.5×ATR, stop) | `best_cl := math.min(...); tl = best_cl + TRAIL_MULT * nz(atr_val); cur_stop := math.min(...)` | L392-398 | **Exact** |
| §5.4 time_stop | Exit after 40 bars | `pos_bars >= TIME_STOP → strategy.close(...)` where `TIME_STOP=40` | L384-387, L404-407 | **Exact** |
| §5.5 priority | SL > trail > time | `strategy.exit(stop=cur_stop)` (SL+trail unified) + `strategy.close()` (time) | L381, L384 | **Representation** — Pine engine resolves SL vs trail intra-bar |
| §5.6 sizing | 1% risk, clamp [0.01, 1.00] | `f_qty()` implements exact formula | L218-224 | **Exact** |

---

## 8. State Machine [Spec §6]

| Spec Transition | Pine Mechanism | Line(s) | Verdict |
|----------------|---------------|---------|---------|
| FLAT → PENDING_LONG | `state := S_PLONG` + `strategy.entry("Long", ..., stop=ep)` | L304-315 | **Exact** |
| FLAT → PENDING_SHORT | `state := S_PSHORT` + `strategy.entry("Short", ..., stop=ep)` | L317-328 | **Exact** |
| PENDING_LONG → LONG | `fill_long and state == S_PLONG → state := S_LONG` | L259-263 | **Exact** |
| PENDING_SHORT → SHORT | `fill_short and state == S_PSHORT → state := S_SHORT` | L266-270 | **Exact** |
| PENDING → FLAT (timeout) | `pend_bars >= TRIGGER_WIN → strategy.cancel(); state := S_FLAT` | L283-301 | **Exact** |
| PENDING_LONG → PENDING_LONG (replace) | Same-direction `strategy.entry()` with new levels; `pend_bars := 0` | L331-341 | **Exact** |
| PENDING_SHORT → PENDING_SHORT (replace) | Same pattern | L343-353 | **Exact** |
| PENDING → PENDING (opposite signal) | Falls through if/else-if chain — no action | L355-359 | **Exact** |
| LONG → FLAT | `pos_closed → state := S_FLAT` (SL/trail hit), or `strategy.close()` (time stop) | L248-256, L384-387 | **Exact** |
| SHORT → FLAT | Same pattern | L248-256, L404-407 | **Exact** |
| LONG/SHORT → LONG/SHORT (signal while in position) | Not processed — `sig_long`/`sig_short` only checked in FLAT or same-direction PENDING | L304, L317, L331, L343 | **Exact** |
| Initial state | FLAT | `var int state = S_FLAT` | L202 | **Exact** |
| Warmup | 34 bars | `warmup_ok = bar_index >= WARMUP` where `WARMUP=34` | L86, L125 | **Exact** |
| No reversal | Opposite signal while in position → suppressed | State machine gating — only FLAT/same-PENDING transitions available | L304-354 | **Exact** |

---

## 9. Anti-Repaint Rules [Spec §3.7]

| Rule | Spec Requirement | Pine Mechanism | Verdict |
|------|-----------------|---------------|---------|
| AR1 | Confirmed (closed) H1 bars only | `calc_on_every_tick = false` | **Exact** |
| AR2 | Closed bar data only for indicators | `close` source + `calc_on_every_tick = false` | **Exact** |
| AR3 | Detection final once bar closes | No recalculation; `alert.freq_once_per_bar_close` | **Exact** |
| AR4 | H4 EMA with `lookahead=barmerge.lookahead_off` | `request.security(..., lookahead=barmerge.lookahead_off)` | **Exact** |

---

## 10. Alert Payload Alignment

All 4 alert types independently verified against `alerts_schema.json` v2.0.0:

### signal_alert — 30 required fields verified

| Field | Schema Constraint | Pine Value | Match |
|-------|-----------------|-----------|-------|
| strategy_name | const "Rob Hoffman IRB" | `STRAT_NAME` = "Rob Hoffman IRB" | ✓ |
| strategy_version | const "2.0.0" | `STRAT_VER` = "2.0.0" | ✓ |
| action | enum [PLACE_STOP_ORDER, REPLACE_STOP_ORDER] | Conditional on `evt_replace` | ✓ |
| signal_type | enum [LONG_IRB, SHORT_IRB] | Conditional on `evt_side` | ✓ |
| irb_type | enum [UPTREND_IRB, DOWNTREND_IRB] | Conditional on `evt_side` | ✓ |
| symbol | const "EURUSD" | Hardcoded | ✓ |
| broker_symbol | const "EURUSD.sim" | Hardcoded | ✓ |
| timeframe | const "H1" | Hardcoded | ✓ |
| side | enum [BUY, SELL] | From `evt_side` | ✓ |
| order_type | enum [BUY_STOP, SELL_STOP] | Conditional on `evt_side` | ✓ |
| entry_price | number | `irb_hi + PIP_BUF` or `irb_lo - PIP_BUF` | ✓ authoritative |
| stop_loss | number | `irb_lo - PIP_BUF` or `irb_hi + PIP_BUF` | ✓ authoritative |
| stop_distance_pips | number | `math.abs(ep - sp) / pip_size` | ✓ |
| volume | number | Risk-based, clamped [0.01, 1.00] | ✓ |
| risk_dollars | number | `strategy.equity * RISK_PCT` | ✓ |
| bar_close_time | integer | `time_close` | ✓ |
| bar_ohlc_o/h/l/c | number | `open/high/low/close` | ✓ |
| irb_range | number | `bar_rng` | ✓ |
| ema_20_h1 | number | `ema_h1` | ✓ |
| ema_slope | number | `ema_slope` | ✓ |
| ema_20_h4 | number | `ema_h4` | ✓ |
| ema_20_h4_dir | enum [RISING, FALLING, FLAT] | Conditional on `h4_up`/`h4_dn` | ✓ |
| adx_14 | number | `adx_val` | ✓ |
| atr_14 | number | `atr_val` | ✓ |
| overextension_ratio | number | `oe_ratio` | ✓ |
| trigger_window_bars | const 20 | `TRIGGER_WIN` = 20 | ✓ |
| strategy_state | enum [PENDING_LONG, PENDING_SHORT] | Conditional on `state` | ✓ |
| campaign | const "ftmo-free-trial-march-2026" | `CAMPAIGN` | ✓ |

### trail_alert — 11 required fields verified ✓
### cancel_alert — 8 required fields verified ✓
### close_alert — 9 required fields verified ✓

**All fields match schema. All const values match. All enum values are valid members.**

---

## 11. Invalid Trade Conditions [Spec §8]

| ID | Rule | Enforced By | Pine Implementation | Verdict |
|----|------|-------------|-------------------|---------|
| IC1 | bar_count < 34 | Pine | `warmup_ok = bar_index >= WARMUP` | **Exact** |
| IC2 | No uptrend IRB geometry | Pine | `is_up_irb` | **Exact** |
| IC3 | No downtrend IRB geometry | Pine | `is_dn_irb` | **Exact** |
| IC4 | Trend filter fails | Pine | `trend_up` / `trend_dn` | **Exact** |
| IC5 | MTF misaligned | Pine | `h4_up` / `h4_dn` | **Exact** |
| IC6 | Sideways (ADX < 20) | Pine | `sw_pass` | **Exact** |
| IC7 | Overextended IRB | Pine | `oe_pass` | **Exact** |
| IC8 | Existing position | Pine | State machine gating | **Exact** |
| IC9 | Existing pending opposite | Pine | State machine: same-direction only | **Exact** |
| IC10-IC15 | Runtime checks | Risk gate | N/A — correctly deferred | **N/A** |

---

## 12. Representation Choices (3 total)

| # | Spec Intent | Pine Representation | Phase 3 Verdict |
|---|------------|-------------------|-----------------|
| 1 | MTF H4 alignment: 5 H4-bar lookback | H1 20-bar lookback on H4 EMA (≈5 H4 bars) + `na` guards | **Acceptable** — equivalent temporal window. `na` guards are conservative. Documented in PA-IRB-1. |
| 2 | MTF H4 `na` safety | Not in spec | **Acceptable** — conservative addition that suppresses signals when H4 data is insufficient. Cannot cause false signals. |
| 3 | Exit priority: SL > trail > time | SL + trail unified in `strategy.exit(stop=cur_stop)`; time stop via `strategy.close()` | **Acceptable** — SL and trail share the same stop level via `cur_stop`. Pine engine resolves intra-bar. Time stop is separate. Documented in PA-IRB-8. |

**No representation choice introduces semantic drift.** All three are conservative and well-documented.

---

## 13. Mismatches

**NONE FOUND.**

All 134 rules from the Phase 2 traceability map were independently re-verified:
- **125 exact matches** ✓
- **3 representation choices** — all conservative and documented ✓
- **6 runtime-only** — all correctly deferred to Trading Agent / Risk Gate ✓

No cases where Pine implements something the spec doesn't say, or omits something it should implement.

---

## 14. Phase 2 Claim Verification

### Claim 1: "MTF H1 20-bar lookback is equivalent to H4 5-bar lookback"

**VERIFIED.** `request.security()` with `lookahead_off` returns the last closed H4 value, which updates every 4 H1 bars. `ema_h4[20]` on H1 references approximately 5 H4 bars back. The temporal windows are equivalent. The `na` guards add safety. ✓

### Claim 2: "`pyramiding = 0` + state machine jointly enforce one-position/one-order"

**VERIFIED.** `pyramiding = 0` prevents Pine from opening multiple positions. The state machine prevents signal processing when in LONG/SHORT states or when an opposite-direction pending order exists. The if/else-if chain at L304-354 only processes signals in FLAT or same-direction PENDING states. Dual enforcement is correct and complete. ✓

### Claim 3: "Trailing stop only tightens, never widens"

**VERIFIED.** For LONG: `cur_stop := math.max(nz(cur_stop), tl)` — `math.max` ensures the stop can only move upward. For SHORT: `cur_stop := math.min(nz(cur_stop), tl)` — `math.min` ensures the stop can only move downward. Initial SL (`irb_lo - PIP_BUF` for longs) is the starting value; the trail can only tighten from there. ✓

### Claim 4: "Alert payloads cover all 4 event types defined in alerts_schema.json"

**VERIFIED.** Signal (PLACE/REPLACE), trail (MODIFY_SL), cancel (CANCEL_ORDER), and close (CLOSE_POSITION) alerts are all implemented with all required fields. The SL/trailing stop broker-side exits are correctly NOT alerted (broker handles them via stop-loss order on MetaApi). ✓

---

## 15. Explicit Judgment

**No implementation drift exists.** The Pine implementation is a faithful 1:1 translation of the Phase 1 IRB strategy specification. All 134 mapped rules are correctly implemented, acceptably represented, or correctly deferred. The Phase 2 traceability claims are independently verified. The alert contract is fully aligned with the schema.

The Backtesting Agent (Phase 4) can trust the signal generation logic as a correct representation of the approved IRB strategy contract.
