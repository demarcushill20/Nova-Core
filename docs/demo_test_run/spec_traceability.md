# NovaTrade Demo Test Run — Spec-to-Code Traceability Map

**Phase:** 2 (Pine Implementation)
**Date:** 2026-03-17
**Status:** LOCKED
**Spec:** strategy_spec.yaml v2.0.0 (Rob Hoffman IRB)
**Pine:** strategy.pine v2.0.0

---

## Purpose

This artifact maps every meaningful rule in the Phase 1 strategy specification (`strategy_spec.yaml` v2.0.0) to the corresponding section of the Pine implementation (`strategy.pine`). A reviewer can audit each row to confirm 1:1 intent fidelity.

---

## 1. Indicator Rules [Spec §2]

| Spec Ref | Spec Rule | Pine Section | Pine Code | Fidelity |
|----------|-----------|-------------|-----------|----------|
| §2.ema_20_h1 | EMA, period=20, source=close, H1 | Section 3 (L105) | `ema_h1 = ta.ema(close, EMA_PERIOD)` | Exact |
| §2.atr_14 | ATR, period=14, H1 | Section 3 (L108) | `atr_val = ta.atr(ATR_PERIOD)` | Exact |
| §2.adx_14 | ADX, period=14, H1 | Section 3 (L111) | `[dp, dm, adx_val] = ta.dmi(ADX_PERIOD, ADX_PERIOD)` | Exact |
| §2.ema_20_h4 | EMA, period=20, source=close, H4 | Section 3 (L114-116) | `ema_h4 = request.security(..., "240", ta.ema(close, EMA_PERIOD), lookahead=barmerge.lookahead_off)` | Exact [AR4] |

---

## 2. IRB Geometry Detection [Spec §3.1, A1]

| Spec Ref | Spec Rule | Pine Section | Pine Code | Fidelity |
|----------|-----------|-------------|-----------|----------|
| IRB_UP_1 | range = high - low | Section 5 (L131) | `bar_rng = high - low` | Exact |
| IRB_UP_2 | range > 0 | Section 5 (L132) | `valid_rng = bar_rng > 0` | Exact |
| IRB_UP_3 | threshold = high - (0.45 × range) | Section 5 (L135) | `up_thresh = high - IRB_PCT * bar_rng` | Exact |
| IRB_UP_4 | open ≤ threshold AND close ≤ threshold | Section 5 (L136) | `is_up_irb = valid_rng and (open <= up_thresh) and (close <= up_thresh)` | Exact |
| IRB_DN_3 | threshold = low + (0.45 × range) | Section 5 (L139) | `dn_thresh = low + IRB_PCT * bar_rng` | Exact |
| IRB_DN_4 | open ≥ threshold AND close ≥ threshold | Section 5 (L140) | `is_dn_irb = valid_rng and (open >= dn_thresh) and (close >= dn_thresh)` | Exact |

---

## 3. Signal Filter Rules

| Spec Ref | Spec Rule | Pine Section | Pine Code | Fidelity |
|----------|-----------|-------------|-----------|----------|
| §3.2 TF1 | ema_slope = (ema_20_h1[t] - ema_20_h1[t-20]) / atr_14[t] | Section 6 (L147-148) | `ema_slope = nz(atr_val) > 0 ? (ema_h1 - nz(ema_h1[SLOPE_LOOKBACK])) / atr_val : 0.0` | Exact — with div-by-zero guard |
| §3.2 TF2_LONG | ema_slope >= 0.4 | Section 6 (L150) | `trend_up = ema_slope >= SLOPE_THRESH` | Exact |
| §3.2 TF2_SHORT | ema_slope <= -0.4 | Section 6 (L151) | `trend_dn = ema_slope <= -SLOPE_THRESH` | Exact |
| §3.3 MTF1_LONG | ema_20_h4[current] > ema_20_h4[current - 5] | Section 7 (L158) | `h4_up = not na(ema_h4) and not na(ema_h4[MTF_H1_LOOKBACK]) and (ema_h4 > ema_h4[MTF_H1_LOOKBACK])` | Representation — uses H1 20-bar lookback (≈5 H4 bars) with na guards |
| §3.3 MTF1_SHORT | ema_20_h4[current] < ema_20_h4[current - 5] | Section 7 (L159) | `h4_dn = not na(ema_h4) and not na(ema_h4[MTF_H1_LOOKBACK]) and (ema_h4 < ema_h4[MTF_H1_LOOKBACK])` | Representation — same approach |
| §3.4 SW1 | adx_14 >= 20 | Section 8 (L165) | `sw_pass = adx_val >= ADX_THRESH` | Exact |
| §3.5 OE1 | (high-low)/atr_14 <= 2.0 | Section 9 (L171-172) | `oe_ratio = nz(atr_val) > 0 ? bar_rng / atr_val : 0.0; oe_pass = oe_ratio <= OE_THRESH` | Exact — with div-by-zero guard |

---

## 4. Combined Signal Logic [Spec §3.6]

| Spec Ref | Spec Rule | Pine Section | Pine Code | Fidelity |
|----------|-----------|-------------|-----------|----------|
| §3.6.long_irb | IRB_UP_4 AND TF2_LONG AND MTF1_LONG AND SW1 AND OE1 | Section 10 (L178) | `sig_long = warmup_ok and is_up_irb and trend_up and h4_up and sw_pass and oe_pass` | Exact — includes warmup guard |
| §3.6.short_irb | IRB_DN_4 AND TF2_SHORT AND MTF1_SHORT AND SW1 AND OE1 | Section 10 (L179) | `sig_short = warmup_ok and is_dn_irb and trend_dn and h4_dn and sw_pass and oe_pass` | Exact — includes warmup guard |
| §3.6.no_signal | No valid geometry or filter fails | Section 10 | Both booleans false → no action | Exact (implicit) |

---

## 5. Entry / Execution Rules [Spec §4]

| Spec Ref | Spec Rule | Pine Section | Pine Code | Fidelity |
|----------|-----------|-------------|-----------|----------|
| §4.order_type | STOP (buy-stop / sell-stop) | Section 13 (L296, L309) | `strategy.entry("Long", strategy.long, stop = ep, qty = qv)` | Exact — `stop` parameter makes it a stop order |
| §4.1.long_entry | BUY_STOP at irb_high + 1 pip | Section 13 (L293) | `float ep = high + PIP_BUF` | Exact |
| §4.1.short_entry | SELL_STOP at irb_low - 1 pip | Section 13 (L306) | `float ep = low - PIP_BUF` | Exact |
| §4.1.volume_sizing | risk_based | Section 12 (L206-212) | `f_qty(ep, sp)` — risk-based lot calculation | Exact |
| §4.1.volume_formula | lot_size = (equity × 0.01) / (stop_distance_pips × $10) | Section 12 (L207-211) | `sd = abs(ep-sp)/pip_size; rd = equity*RISK_PCT; raw = rd/(sd*PIP_VAL_LOT); clamp [0.01,1.0]` | Exact |
| §4.2 IRB Replacement | Cancel old, place new at new IRB levels; window resets | Section 14 (L319-340) | Same-direction `strategy.entry()` call with new levels; `pend_bars := 0` | Exact |
| §4.3 Trigger Window | Hard cancel at 20 bars | Section 14 (L271-289) | `if state == S_PLONG and pend_bars >= TRIGGER_WIN → strategy.cancel("Long"); state := S_FLAT` | Exact |

---

## 6. One-Position / One-Active-Order Constraints [Spec §4.4, §5.7, IC8, IC9]

| Spec Ref | Spec Rule | Pine Mechanism | Fidelity |
|----------|-----------|---------------|----------|
| §4.4 max 1 position | Only one position or pending order at a time | `pyramiding = 0` (L36) + state machine | Exact — dual enforcement |
| §4.4 signal suppression while in position | New IRB signals ignored when LONG or SHORT | State machine: LONG/SHORT states have no signal transitions (L344) | Exact |
| §4.4 no reversal | LONG_IRB while SHORT → suppressed | State machine: no cross-direction transitions (L344) | Exact |
| §4.4 opposite while pending → ignore | SHORT signal while PENDING_LONG → suppressed | State machine: only same-direction replacement (L343) | Exact |
| IC8 existing_position | open_position_count > 0 → suppress_signal | State machine S_LONG/S_SHORT + `pyramiding = 0` | Exact |
| IC9 existing_pending_opposite | Pending order in opposite direction → suppress | State machine: only same-direction replacement allowed | Exact |

---

## 7. Risk / Exit Rules [Spec §5]

| Spec Ref | Spec Rule | Pine Section | Pine Code | Fidelity |
|----------|-----------|-------------|-----------|----------|
| §5.1 SL long | irb_low - 1 pip | Section 13 (L251) | `cur_stop := irb_lo - PIP_BUF` | Exact |
| §5.1 SL short | irb_high + 1 pip | Section 13 (L258) | `cur_stop := irb_hi + PIP_BUF` | Exact |
| §5.1 modification | only_tighten | Section 15 (L359, L379) | `cur_stop := math.max(nz(cur_stop), tl)` (long); `math.min(...)` (short) | Exact |
| §5.2 no fixed TP | type: none | Section 15 | No `limit` parameter in `strategy.exit()` | Exact |
| §5.3 trail long | max(highest_close - 1.5×ATR, current_stop) | Section 15 (L353-359) | `best_cl := math.max(...); tl = best_cl - TRAIL_MULT * nz(atr_val); cur_stop := math.max(nz(cur_stop), tl)` | Exact |
| §5.3 trail short | min(lowest_close + 1.5×ATR, current_stop) | Section 15 (L373-379) | `best_cl := math.min(...); tl = best_cl + TRAIL_MULT * nz(atr_val); cur_stop := math.min(nz(cur_stop), tl)` | Exact |
| §5.4 time stop | Exit after 40 bars | Section 15 (L365-368, L385-388) | `if pos_bars >= TIME_STOP → strategy.close(...)` | Exact |
| §5.5 exit priority | SL > trailing > time | Section 15 | `strategy.exit()` with `stop` handles SL+trail; time stop via separate `strategy.close()` | Representation — Pine engine resolves SL vs trail priority intra-bar |
| §5.6 position sizing | risk_fraction = 0.01 | Section 12 (L206-212) | `f_qty()` function | Exact |
| §5.6 clamp | [0.01, 1.00] lots | Section 12 (L210) | `math.max(MIN_LOTS, math.min(MAX_LOTS, raw))` | Exact |

---

## 8. State Machine [Spec §6]

| Spec Ref | Spec Transition | Pine Mechanism | Fidelity |
|----------|----------------|---------------|----------|
| §6 FLAT→PENDING_LONG | LONG_IRB signal | `strategy.entry("Long",...stop=ep)` + `state := S_PLONG` (L292-303) | Exact |
| §6 FLAT→PENDING_SHORT | SHORT_IRB signal | `strategy.entry("Short",...stop=ep)` + `state := S_PSHORT` (L305-316) | Exact |
| §6 PENDING_LONG→LONG | Buy-stop triggered | `fill_long and state == S_PLONG → state := S_LONG` (L247-251) | Exact |
| §6 PENDING_SHORT→SHORT | Sell-stop triggered | `fill_short and state == S_PSHORT → state := S_SHORT` (L253-258) | Exact |
| §6 PENDING→FLAT | Trigger window expired | `pend_bars >= TRIGGER_WIN → strategy.cancel(); state := S_FLAT` (L271-289) | Exact |
| §6 PENDING_LONG→PENDING_LONG | Replacement | Same-direction `strategy.entry()` with new levels; `pend_bars := 0` (L319-329) | Exact |
| §6 PENDING_SHORT→PENDING_SHORT | Replacement | Same pattern (L331-340) | Exact |
| §6 PENDING→PENDING | Opposite signal | Ignored — no else branch for cross-direction (L343) | Exact |
| §6 LONG→FLAT | SL/trail hit | `strategy.exit("Long Exit", stop=cur_stop)` (L362) + `pos_closed → state := S_FLAT` (L236-244) | Exact |
| §6 LONG→FLAT | Time stop | `pos_bars >= TIME_STOP → strategy.close("Long")` (L365-368) | Exact |
| §6 LONG→LONG | Signal while in position | No signal processing — `sig_long` only checked when `state == S_FLAT` or `state == S_PLONG` (L292, L319) | Exact |
| §6 SHORT→FLAT | SL/trail/time stop | Mirrors LONG (L370-388) | Exact |
| §6 SHORT→SHORT | Signal while in position | No signal processing — same gating (L305, L331) | Exact |
| §6.initial_state | FLAT | `var int state = S_FLAT` (L193) | Exact |
| §6.warmup | 34 bars | `warmup_ok = bar_index >= WARMUP` (L125) where `WARMUP = 34` (L86) | Exact |
| §6.key_difference | No reversal | State machine only allows exits via SL/trail/time; no LONG→SHORT or SHORT→LONG (L344) | Exact |

---

## 9. Anti-Repaint Rules [Spec §3.7]

| Spec Ref | Spec Rule | Pine Mechanism | Fidelity |
|----------|-----------|---------------|----------|
| AR1 | IRB geometry on confirmed (closed) H1 bars only | `calc_on_every_tick = false` (L38) | Exact |
| AR2 | EMA/ATR from closed bar data only | `close` source + `calc_on_every_tick = false` | Exact |
| AR3 | Detection final once bar closes | `alert.freq_once_per_bar_close` + no recalculation | Exact |
| AR4 | H4 EMA with `lookahead=barmerge.lookahead_off` | `request.security(..., lookahead=barmerge.lookahead_off)` (L116) | Exact |

---

## 10. Alert Payload Fields [Spec §9 Telemetry]

### signal_alert (PLACE_STOP_ORDER / REPLACE_STOP_ORDER)

| Spec Field | Alert JSON Key | Pine Source | Fidelity |
|------------|---------------|-------------|----------|
| strategy_name | `strategy_name` | `STRAT_NAME` constant | Exact |
| strategy_version | `strategy_version` | `STRAT_VER` constant | Exact |
| action | `action` | `PLACE_STOP_ORDER` or `REPLACE_STOP_ORDER` | Exact |
| signal_type | `signal_type` | `LONG_IRB` / `SHORT_IRB` | Exact |
| irb_type | `irb_type` | `UPTREND_IRB` / `DOWNTREND_IRB` | Exact |
| symbol | `symbol` + `broker_symbol` | `EURUSD` / `EURUSD.sim` | Exact |
| timeframe | `timeframe` | `H1` | Exact |
| side | `side` | `BUY` / `SELL` | Exact |
| order_type | `order_type` | `BUY_STOP` / `SELL_STOP` | Exact |
| entry_price | `entry_price` | `irb_hi + PIP_BUF` or `irb_lo - PIP_BUF` | Exact — authoritative |
| stop_loss | `stop_loss` | `irb_lo - PIP_BUF` or `irb_hi + PIP_BUF` | Exact — authoritative |
| stop_distance_pips | `stop_distance_pips` | `abs(ep - sp) / pip_size` | Exact |
| volume | `volume` | Risk-based lot size, clamped [0.01, 1.00] | Exact |
| risk_dollars | `risk_dollars` | `strategy.equity * RISK_PCT` | Exact |
| bar_close_time | `bar_close_time` | `time_close` | Exact |
| bar_ohlc | `bar_ohlc_o/h/l/c` | `open/high/low/close` | Exact |
| irb_range | `irb_range` | `bar_rng` | Exact |
| ema_20_h1 | `ema_20_h1` | `ema_h1` | Exact |
| ema_slope | `ema_slope` | `ema_slope` | Exact |
| ema_20_h4 | `ema_20_h4` + `ema_20_h4_dir` | `ema_h4` + RISING/FALLING/FLAT | Exact |
| adx_14 | `adx_14` | `adx_val` | Exact |
| atr_14 | `atr_14` | `atr_val` | Exact |
| overextension_ratio | `overextension_ratio` | `oe_ratio` | Exact |
| trigger_window_bars | `trigger_window_bars` | `TRIGGER_WIN` constant (20) | Exact |
| strategy_state | `strategy_state` | `PENDING_LONG` / `PENDING_SHORT` | Exact |
| campaign | `campaign` | `CAMPAIGN` constant | Exact |

### trail_alert (MODIFY_SL)

| Spec Field | Alert JSON Key | Pine Source | Fidelity |
|------------|---------------|-------------|----------|
| action | `action` | `MODIFY_SL` | Exact |
| side | `side` | `BUY` / `SELL` | Exact |
| old_stop | `old_stop` | `nz(prev_stop)` | Exact |
| new_stop | `new_stop` | `cur_stop` | Exact — authoritative |
| best_close | `best_close` | `best_cl` | Exact |
| atr_14 | `atr_14` | `atr_val` | Exact |
| bars_since_entry | `bars_since_entry` | `pos_bars` | Exact |

### cancel_alert (CANCEL_ORDER)

| Spec Field | Alert JSON Key | Pine Source | Fidelity |
|------------|---------------|-------------|----------|
| action | `action` | `CANCEL_ORDER` | Exact |
| side | `side` | `BUY` / `SELL` | Exact |
| cancel_reason | `cancel_reason` | `TRIGGER_WINDOW_EXPIRED` | Exact |
| bars_elapsed | `bars_elapsed` | `TRIGGER_WIN` | Exact |

### close_alert (CLOSE_POSITION)

| Spec Field | Alert JSON Key | Pine Source | Fidelity |
|------------|---------------|-------------|----------|
| action | `action` | `CLOSE_POSITION` | Exact |
| side | `side` | `BUY` / `SELL` | Exact |
| close_reason | `close_reason` | `TIME_STOP` | Exact |
| bars_held | `bars_held` | `pos_bars` | Exact |
| close_price | `close_price` | `close` | Exact — reference only |

### Runtime-only fields (not in Pine)

| Spec Field | Populated By | Phase |
|------------|-------------|-------|
| risk_decision, risk_checks | Trading Agent (after risk gate) | Phase 4+ |
| fill_price, slippage_pips, position_id | Trading Agent (from broker) | Phase 4+ |
| pnl_pips, pnl_usd, hold_duration_seconds | Trading Agent (computed on close) | Phase 4+ |
| max_favorable/adverse_excursion_pips | Trading Agent (computed on close) | Phase 4+ |

---

## 11. Risk-Aware Payload Fields for Downstream Systems

The signal_alert payload includes all fields required by the runtime risk gate and Trading Agent for safe order submission:

| Field | Purpose | Downstream Consumer |
|-------|---------|-------------------|
| `entry_price` | Stop order trigger price — authoritative | Trading Agent → MetaApi `placePendingOrder()` |
| `stop_loss` | SL price — authoritative | Trading Agent → MetaApi `placePendingOrder(stopLoss=)` |
| `stop_distance_pips` | Pre-computed SL distance | Risk gate validation |
| `volume` | Risk-based lot size | Trading Agent → MetaApi `volume` param; risk gate validation |
| `risk_dollars` | Dollar risk amount | Risk gate `max_risk_per_trade` check |
| `side` | BUY / SELL | Trading Agent order direction |
| `order_type` | BUY_STOP / SELL_STOP | Trading Agent order type |
| `symbol` / `broker_symbol` | EURUSD / EURUSD.sim | Trading Agent symbol resolution |
| `atr_14` | Current ATR — context for risk gate | Risk gate, evidence logging |
| `campaign` | Campaign tag | Evidence lineage, FTMO tracking |

---

## 12. Session Rules [Spec §7]

| Spec Ref | Spec Rule | Pine Code | Fidelity |
|----------|-----------|-----------|----------|
| §7.trading_hours | 24/5 | No session filter applied | Exact |
| §7.session_filter | null | No `time()` or session guards | Exact |
| §7.weekend_handling | hold | No Friday-close logic | Exact |
| §7.friday_close | false | No forced close on Friday | Exact |
| §7.pending_order_weekend | hold | Pending orders persist over weekend | Exact |

---

## 13. Invalid Trade Conditions [Spec §8]

| Spec Ref | Spec Rule | Implemented In | Pine Code | Fidelity |
|----------|-----------|---------------|-----------|----------|
| IC1 | bar_count < 34 → suppress | Pine (L125) | `warmup_ok = bar_index >= WARMUP` | Exact |
| IC2 | No uptrend IRB geometry → suppress | Pine (L136) | `is_up_irb` boolean | Exact |
| IC3 | No downtrend IRB geometry → suppress | Pine (L140) | `is_dn_irb` boolean | Exact |
| IC4 | Trend filter fails → suppress | Pine (L150-151) | `trend_up` / `trend_dn` | Exact |
| IC5 | MTF misaligned → suppress | Pine (L158-159) | `h4_up` / `h4_dn` | Exact |
| IC6 | Sideways market (ADX < 20) → suppress | Pine (L165) | `sw_pass` | Exact |
| IC7 | Overextended IRB → suppress | Pine (L172) | `oe_pass` | Exact |
| IC8 | Existing position → suppress | Pine state machine | Signals only processed in S_FLAT/S_PENDING states | Exact |
| IC9 | Existing pending opposite → suppress | Pine state machine | Only same-direction replacement allowed | Exact |
| IC10 | Spread > 30 points → deny | **Runtime risk gate** | N/A — runtime only | N/A |
| IC11 | Drawdown breach → deny | **Runtime risk gate** | N/A — runtime only | N/A |
| IC12 | Kill switch active → deny | **Runtime risk gate** | N/A — runtime only | N/A |
| IC13 | Dry run enabled → deny | **Runtime risk gate** | N/A — runtime only | N/A |
| IC14 | Adapter health down → deny | **Runtime risk gate** | N/A — runtime only | N/A |
| IC15 | Cooldown active → deny | **Runtime risk gate** | N/A — runtime only | N/A |

---

## 14. Contract Integrity [Spec §10]

| Spec Ref | Rule | Pine Implementation |
|----------|------|-------------------|
| §10.immutable_during_run | true | Pine comment header includes spec SHA-256 hash (L6) |
| §10.hash_algorithm | SHA-256 | Recorded in Pine header |
| §10.version_control | Git-tracked | strategy.pine under version control |

---

## Summary

| Category | Total Rules | Exact Match | Representation | Runtime Only (N/A) |
|----------|-------------|-------------|---------------|-------------------|
| Indicators | 4 | 4 | 0 | 0 |
| IRB Geometry | 6 | 6 | 0 | 0 |
| Signal Filters | 7 | 5 | 2 | 0 |
| Combined Signal | 3 | 3 | 0 | 0 |
| Entry/Execution | 7 | 7 | 0 | 0 |
| One-Position Constraints | 6 | 6 | 0 | 0 |
| Risk/Exit | 10 | 9 | 1 | 0 |
| State Machine | 16 | 16 | 0 | 0 |
| Anti-Repaint | 4 | 4 | 0 | 0 |
| Alert Payloads | 38 | 38 | 0 | 0 |
| Risk-Aware Fields | 10 | 10 | 0 | 0 |
| Session | 5 | 5 | 0 | 0 |
| Invalid Conditions | 15 | 9 | 0 | 6 |
| Contract | 3 | 3 | 0 | 0 |
| **Total** | **134** | **125** | **3** | **6** |

- **125 exact matches** — Pine code maps 1:1 to spec intent
- **3 representation choices** — Pine forces a specific encoding; documented and conservative:
  1. MTF H4 alignment uses H1 20-bar lookback (≈5 H4 bars) rather than direct H4 bar reference
  2. MTF H4 alignment includes `na` guards not specified in spec (conservative addition)
  3. SL/trail intra-bar priority determined by Pine engine (spec says SL > trail > time)
- **6 runtime-only** — correctly deferred to Trading Agent / Risk Gate; not Pine's responsibility
