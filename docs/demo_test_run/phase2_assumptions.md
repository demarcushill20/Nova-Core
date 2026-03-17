# NovaTrade Demo Test Run — Phase 2 Assumptions

**Phase:** 2 (Pine Implementation)
**Date:** 2026-03-17
**Status:** LOCKED
**Spec:** strategy_spec.yaml v2.0.0 (Rob Hoffman IRB)

---

## Inherited Assumptions (from Phase 0, Phase 0.5, and Phase 1)

All assumptions from prior phases remain in force, including the 10 ADOPTED rules (A1–A10) and 8 UNRESOLVED item resolutions (U1–U8) documented in `irb_source_boundary.md` and `strategy_spec.yaml`. Phase 2 does not alter or contradict any of them.

---

## New Phase 2 Assumptions (Pine-Specific)

| ID | Assumption | Rationale | Risk Level |
|----|-----------|-----------|------------|
| PA-IRB-1 | **`request.security()` with `lookahead=barmerge.lookahead_off` returns the H4 EMA value as of the most recently closed H4 bar, and this value is constant across H1 bars within the same H4 bar** | This is standard Pine v5 behavior. With `lookahead_off`, the function returns the value from the last completed H4 bar, preventing future data leakage. The H4 EMA value updates only when a new H4 bar closes (every 4 H1 bars). The MTF comparison `ema_h4 > ema_h4[MTF_H1_LOOKBACK]` uses a 20-H1-bar lookback (≈5 H4 bars), which provides a valid temporal window for direction detection. | Low |
| PA-IRB-2 | **The ATR-normalized slope formula `(ema[t] - ema[t-20]) / atr[t]` with a `nz(atr_val) > 0` guard handles the edge case where ATR is zero** | If ATR is exactly zero (all bars have zero range — impossible on live forex but possible on synthetic data), the slope defaults to 0.0, which fails the ≥0.4 threshold and suppresses the signal. This is the conservative choice. | Negligible |
| PA-IRB-3 | **Pine's `strategy.entry()` with `stop=price` parameter correctly models a pending stop order (BUY_STOP / SELL_STOP)** | In Pine backtesting, `strategy.entry(..., stop=price)` places a pending stop order that fills when price reaches the trigger level. In live execution, the Pine alert fires and the Trading Agent places the actual pending order on MetaApi. The Pine mechanism models the spec's stop-order entry correctly for backtesting purposes. | Low |
| PA-IRB-4 | **`pyramiding = 0` combined with the explicit state machine provides correct one-position/one-active-order enforcement** | `pyramiding = 0` prevents Pine from opening multiple positions in the same direction. The explicit state machine (5 states) adds a second layer of enforcement: signals are only processed when `state == S_FLAT` or during same-direction replacement when `state == S_PLONG/S_PSHORT`. These dual constraints jointly satisfy spec §4.4 (duplicate_prevention). | Negligible |
| PA-IRB-5 | **Position fill is detected reliably via `strategy.position_size` change between bars** | Pine's `strategy.position_size` is 0 when flat, positive when long, negative when short. The fill detection logic (`fill_long = now_long and was_flat`) correctly identifies when a pending stop order has been triggered. This transitions the state machine from PENDING to LONG/SHORT. In live execution, fill detection is the Trading Agent's responsibility via MetaApi position monitoring. | Low |
| PA-IRB-6 | **Risk-based position sizing via `f_qty()` produces lot sizes consistent with the spec's 1% risk formula** | The function computes `lot_size = (equity × 0.01) / (stop_distance_pips × $10)` then clamps to [0.01, 1.00] lots. The result is multiplied by `units_per_lot` (100,000) to convert to Pine's unit-based quantity. This matches spec §5.6 exactly. For the alert payload, the lot-based volume is computed separately (without the units conversion) and included as the authoritative `volume` field. | Low |
| PA-IRB-7 | **`calc_on_every_tick = false` and `process_orders_on_close = false` together provide the correct execution model** | `calc_on_every_tick = false`: strategy evaluates once per bar at bar close (anti-repaint). `process_orders_on_close = false`: pending stop orders fill at the market price when the stop level is breached, not at bar close. For stop orders, this is the correct Pine behavior — the order fills when the stop price is hit within the bar, using the stop price as the fill price. | Negligible |
| PA-IRB-8 | **The trailing stop update and SL/trail exit share a single `strategy.exit()` call per bar** | Each bar while in position, `strategy.exit("Long Exit", "Long", stop=cur_stop)` is called with the updated `cur_stop` value. This single call serves both the initial SL (when `cur_stop` equals the IRB opposite side) and the trailing stop (when `cur_stop` has been tightened by the trail logic). Pine's engine evaluates the stop level on each bar. The time stop uses a separate `strategy.close()` call which takes priority when `pos_bars >= TIME_STOP`. | Negligible |
| PA-IRB-9 | **Pine's `var` keyword provides correct persistence for state machine variables across bars** | `var int state = S_FLAT` initializes on the first bar and persists across all subsequent bars. The same applies to `irb_hi`, `irb_lo`, `pend_bars`, `pos_bars`, `best_cl`, `cur_stop`, and `pend_qty`. These variables correctly maintain state across bars as required by the 5-state machine. | Negligible |
| PA-IRB-10 | **`alert.freq_once_per_bar_close` prevents duplicate alerts within a single bar** | This Pine alert frequency ensures at most one alert fires per `alert()` call site per bar close. Combined with `calc_on_every_tick = false`, exactly one alert fires per event per bar. Multiple different alerts CAN fire on the same bar (e.g., CANCEL + SIGNAL on the same bar when a window expires and a new signal fires). This is correct behavior per spec §4.3. | Negligible |
| PA-IRB-11 | **The trail update alert fires only when `cur_stop` changes, not on every bar** | The condition `cur_stop != nz(prev_stop)` ensures MODIFY_SL alerts fire only when the trailing stop actually tightens. Bars where the trail level would be lower than the current stop (i.e., no tightening) produce no alert. This avoids spamming the Trading Agent with redundant SL modification requests. | Negligible |
| PA-IRB-12 | **The alert payload JSON is parseable without loss of precision for 5-digit EURUSD** | Pine's `str.tostring(value, "#.#####")` provides 5 decimal places for price fields. The JSON is constructed via string concatenation. The Trading Agent must parse this JSON and use the `entry_price`, `stop_loss`, and `volume` fields as authoritative values for order placement. | Low |

| PA-IRB-13 | **Signal identifiers (LONG_IRB, SHORT_IRB, UPTREND_IRB, DOWNTREND_IRB) are constructed from event state at alert time, not from Pine-native identifiers** | The alert JSON builds `signal_type`, `irb_type`, `action`, and other enum-like fields via conditional string assignment (e.g., `evt_side == "BUY" ? "LONG_IRB" : "SHORT_IRB"`). These are string literals embedded in the JSON payload, not Pine variables used elsewhere. The Trading Agent must parse these strings and route them accordingly. There is no Pine-level validation that these strings match the `alerts_schema.json` schema — that validation is the Trading Agent's responsibility. | Negligible |
| PA-IRB-14 | **Chart-side strategy simulation and broker-side execution reality differ in fill mechanics, spread, and timing** | Pine's `strategy.entry(..., stop=ep)` simulates a stop-order fill at the stop price when price crosses it intra-bar. In broker-side reality: (1) the stop order is a real pending order on the MetaApi server, (2) fill price includes spread and potential slippage, (3) the order may be re-quoted or rejected, (4) fill timing is continuous (not bar-aligned). These differences are inherent to all Pine backtesting and cannot be eliminated. The alert payload provides the authoritative `entry_price` and `stop_loss` computed from bar-close data; the Trading Agent uses these as order parameters, and actual fill prices are logged separately as evidence. Pine's backtest P&L is indicative, not authoritative. | Low |
| PA-IRB-15 | **Trailing stop values in Pine represent the current desired SL level, not a direct MetaApi `modifyPosition()` call** | Each bar, `strategy.exit("Long Exit", "Long", stop=cur_stop)` updates Pine's internal exit order. In broker-side reality, the Trading Agent receives `MODIFY_SL` alerts with `new_stop` and must call `modifyPosition(stopLoss=new_stop)` on MetaApi. Pine has no knowledge of whether the modification succeeds. If the Trading Agent fails to modify, the broker-side SL may lag behind Pine's trailing level. This is mitigated by the MODIFY_SL alert containing the authoritative `new_stop` value on every tightening event. | Low |

---

## Risk Assessment Summary

| Risk Level | Count | IDs |
|------------|-------|-----|
| Negligible | 8 | PA-IRB-2, PA-IRB-4, PA-IRB-7, PA-IRB-8, PA-IRB-9, PA-IRB-10, PA-IRB-11, PA-IRB-13 |
| Low | 7 | PA-IRB-1, PA-IRB-3, PA-IRB-5, PA-IRB-6, PA-IRB-12, PA-IRB-14, PA-IRB-15 |

**No medium or high-risk assumptions in Phase 2.** All Pine-specific interpretations follow standard PineScript v5 patterns with well-understood behavior.
