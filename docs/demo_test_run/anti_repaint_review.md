# NovaTrade Demo Test Run — Anti-Repaint and Future-Leak Review (Fresh IRB)

**Phase:** 3 (Compile / Lint / Static Validation)
**Date:** 2026-03-17
**Status:** FULLY COMPLIANT (AR1-AR4)
**Agent:** Compiler/Lint Agent
**Pine:** strategy.pine v2.0.0 (Rob Hoffman IRB, 549 lines)
**Replaces:** EMA Crossover anti-repaint review (2026-03-16)

---

## 1. Bar-Close Confirmation Behavior

### `calc_on_every_tick = false` (line 38)

**Verified: COMPLIANT with AR1.**

With this setting, Pine evaluates the strategy exactly once per bar — at bar close. No intra-bar recalculation occurs during bar formation. This is the strongest anti-repaint protection available in Pine.

- During historical replay: each bar is evaluated once, in sequence
- During live execution: the script waits for the bar to close before evaluating
- No tick-by-tick recalculation during bar formation

### `process_orders_on_close = false` (line 37)

**Verified: CONSISTENT with realistic stop-order fill model.**

Stop orders (`strategy.entry(..., stop=ep)`) fill at the stop trigger price when price reaches it during a bar. This is the correct fill model for stop orders:
- Bar N close: IRB signal detected, stop order placed
- Bar N+1 (or later): if price reaches the stop level, order fills at that level
- Bar N+1 close: fill detected, state transitions, `strategy.exit()` placed

This matches the live execution model where the alert fires at bar close, the Trading Agent places a pending stop order on MetaApi, and the order fills when the broker detects the price trigger.

### Signal evaluation timing

All signal logic uses only closed-bar data:
- `high`, `low`, `open`, `close` — current CLOSED bar values (L131-140)
- `ema_h1` — EMA of close (L105)
- `ema_h1[SLOPE_LOOKBACK]` — 20-bar historical EMA value (L148)
- `atr_val` — ATR of current closed bar (L108)
- `adx_val` — ADX of current closed bar (L111)
- `ema_h4` — H4 EMA of closed H4 bar (L114-116)
- `ema_h4[MTF_H1_LOOKBACK]` — historical H4 EMA value (L162)

No intra-bar recomputation. All indicator values are final at bar close. ✓

---

## 2. Future Leak / Lookahead Risk

### Direct checks

| Check | Result | Evidence |
|-------|--------|----------|
| `request.security()` with `lookahead_off`? | **Yes — correct** | Line 114-116: `lookahead = barmerge.lookahead_off` — prevents future H4 data leakage |
| Any `barmerge.lookahead_on` usage? | **No** | Only `barmerge.lookahead_off` |
| `ta.valuewhen()` or lookback-based stateful functions? | **No** | Standard indicators only: `ta.ema()`, `ta.atr()`, `ta.dmi()` |
| Higher-timeframe data referenced? | **Yes — H4 EMA via `request.security()`** | Anti-repaint compliant: `lookahead_off` prevents future data |
| History references beyond current bar? | **Yes — `[1]`, `[20]`** | `strategy.position_size[1]` (L231), `ema_h1[SLOPE_LOOKBACK]` (L148), `ema_h4[MTF_H1_LOOKBACK]` (L162) — all backward-looking |
| Any `timenow`, `last_bar_time`, or forward-counting? | **No** | Only `bar_index` (backward-counting) and `time_close` (current bar timestamp) |
| Any `ta.pivothigh()`, `ta.pivotlow()` (which use future bars)? | **No** | Not used |

**Conclusion: NO future leak detected. NO lookahead behavior.**

---

## 3. Multi-Timeframe Safety

### `request.security()` analysis (lines 114-116)

```pine
ema_h4 = request.security(syminfo.tickerid, "240",
     ta.ema(close, EMA_PERIOD),
     lookahead = barmerge.lookahead_off)
```

**Behavior with `lookahead_off`:**
- Returns the H4 EMA value as of the most recently **closed** H4 bar
- The value is constant across H1 bars within the same H4 bar period
- Updates only when a new H4 bar closes (every 4 H1 bars)
- Cannot access future H4 data

**MTF comparison (line 162):**
```pine
h4_up = not na(ema_h4) and not na(ema_h4[MTF_H1_LOOKBACK]) and (ema_h4 > ema_h4[MTF_H1_LOOKBACK])
```

`ema_h4[20]` refers to the H4 EMA value 20 H1 bars ago — a past value. With `lookahead_off`, this is the H4 EMA from a completed H4 bar approximately 5 H4 periods back. No forward reference. ✓

**`na` guards:** `not na(ema_h4)` and `not na(ema_h4[MTF_H1_LOOKBACK])` conservatively suppress signals when H4 data is unavailable (early bars, insufficient history). This is safer than the spec requires. ✓

---

## 4. Indicator Data Source Analysis

| Indicator | Source | Bar Timing | Repaint Risk |
|-----------|--------|-----------|-------------|
| `ema_h1` (L105) | `close` | Current closed bar | None — `calc_on_every_tick = false` |
| `atr_val` (L108) | High/Low/Close (implicit) | Current closed bar | None — ATR uses confirmed OHLC |
| `adx_val` (L111) | High/Low/Close (implicit via DI) | Current closed bar | None — ADX uses confirmed OHLC |
| `ema_h4` (L114-116) | `close` via `request.security()` | Last closed H4 bar | None — `lookahead_off` |

All indicators use confirmed, closed-bar data only. **COMPLIANT with AR2.** ✓

---

## 5. Signal Finality

### Once a bar closes, can the IRB detection change?

**No.** The IRB geometry detection (lines 131-140) depends only on the current bar's `high`, `low`, `open`, `close`. With `calc_on_every_tick = false`, these values are evaluated once at bar close and are final. The detection result (`is_up_irb`, `is_dn_irb`) cannot change retroactively. **COMPLIANT with AR3.** ✓

### Once a signal fires, can it be revoked?

**No.** `sig_long` and `sig_short` are computed from finalized indicator values. The alert fires with `alert.freq_once_per_bar_close` — one alert per bar close per call site. No mechanism exists to revoke a fired alert. ✓

### State machine transitions — are they final?

**Yes.** State transitions are computed once per bar at bar close. The `var` variables persist but are updated deterministically. No retroactive state changes. ✓

---

## 6. Current vs Prior Bar References

| Variable | Bar Reference | Type | Correct? |
|----------|-------------|------|----------|
| `high`, `low`, `open`, `close` | Current closed bar | Series float | ✓ |
| `ema_h1` | Current bar EMA value | Series float | ✓ |
| `ema_h1[SLOPE_LOOKBACK]` | 20 bars ago (confirmed) | Series float | ✓ |
| `atr_val` | Current bar ATR | Series float | ✓ |
| `adx_val` | Current bar ADX | Series float | ✓ |
| `ema_h4` | Latest closed H4 bar | Series float | ✓ |
| `ema_h4[MTF_H1_LOOKBACK]` | 20 H1 bars ago (≈5 H4 bars ago, confirmed) | Series float | ✓ |
| `strategy.position_size` | Current state (after fills) | Series float | ✓ |
| `strategy.position_size[1]` | Previous bar state | Series float | ✓ |
| `strategy.equity` | Current equity | Series float | ✓ |
| `bar_index` | Current bar number (0-based, backward) | Series int | ✓ |
| `time_close` | Current bar close timestamp | Series int | ✓ |

All references are to current or prior confirmed bars. No forward references found.

---

## 7. Intrabar Ambiguity

**None for signal generation.** With `calc_on_every_tick = false`, the entire script evaluates only at bar close. "Intrabar" does not apply to signal computation.

**Stop-order fill timing is intrabar.** With `process_orders_on_close = false`, pending stop orders fill at the stop price when it's hit during a bar. This is the correct behavior for stop orders — the Pine engine simulates realistic intrabar fills. The fill is detected at the next bar close evaluation. No repaint risk from this behavior. ✓

---

## 8. Live vs Historical Behavior Risks

### Signal generation

| Aspect | Historical | Live | Risk |
|--------|-----------|------|------|
| Signal evaluation timing | Bar close | Bar close | **Identical** — `calc_on_every_tick = false` |
| IRB geometry detection | From confirmed OHLC | From confirmed OHLC | **Identical** given same data |
| Indicator values | From historical close | From live close | **Identical** given same data |
| Filter results | Deterministic | Deterministic | **Identical** |
| Alert firing | N/A (backtest) | Once per bar close | **Consistent** |

### Execution

| Aspect | Historical | Live | Risk |
|--------|-----------|------|------|
| Stop order fill price | At stop level when touched | Broker fill (may include spread/slippage) | **Differs** — expected |
| Stop order fill timing | Intrabar (Pine simulated) | Continuous (broker-side) | **Differs** — expected |
| SL placement | From `cur_stop` via `strategy.exit()` | Trading Agent places via MetaApi from alert | **Differs** — alert provides authoritative `stop_loss` |
| Trailing stop update | `strategy.exit()` each bar | Trading Agent `modifyPosition()` from MODIFY_SL alert | **Differs** — alert provides authoritative `new_stop` |
| Time stop | `strategy.close()` at 40 bars | Trading Agent `closePosition()` from CLOSE_POSITION alert | **Differs** — Trading Agent executes the close |

### Assessment

**Signal divergence risk: NEGLIGIBLE.** Given the same data source and bar boundaries, IRB signals are identical between historical and live.

**Execution divergence risk: LOW and expected.** Fill price differences are inherent to any backtest-to-live transition. The alert payload provides authoritative `entry_price`, `stop_loss`, and `volume` values. The Trading Agent executes from these values, and actual fill prices are recorded separately.

---

## 9. Warmup Logic Review

### Implementation (line 86, 125)

```pine
WARMUP = 34
warmup_ok = bar_index >= WARMUP
```

### Analysis

- `bar_index` is 0-based. `bar_index >= 34` means the current bar is the 35th bar (index 34).
- At this point:
  - EMA(20) has 35 bars of data — well-converged ✓
  - ATR(14) has 35 bars of data — well-converged ✓
  - ADX(14) has 35 bars of data — well-converged ✓
  - EMA slope lookback (20 bars) references bar index 14 — EMA at that point has 15 bars of data — reasonable ✓
  - H4 EMA lookback (20 H1 bars) — sufficient H4 data ✓

**Spec says:** 34 bars minimum (§6.warmup.bars_required).
**Pine implements:** `bar_index >= 34` — exactly 34 bars minimum.
**Verdict: EXACT match, sufficient for all indicator convergence.** ✓

---

## 10. Stop Order and Pending Order Behavior

### Pending stop order model

Pine's `strategy.entry("Long", strategy.long, stop = ep, qty = qv)` creates a pending stop order. Key behaviors:

1. Order activates on the bar AFTER it's placed (bar N+1)
2. If price on bar N+1 (or later) touches `ep`, the order fills at `ep`
3. Calling `strategy.entry("Long", ...)` again replaces the previous pending order — correct for IRB replacement (A4)
4. `strategy.cancel("Long")` cancels the pending order — correct for trigger window expiry (U5)
5. `pyramiding = 0` prevents a second position from opening — correct for one-position constraint (§4.4)

All behaviors align with the spec. ✓

---

## 11. Trailing Stop Anti-Repaint Analysis

The trailing stop logic (lines 370-408) executes only when in position (state == S_LONG or S_SHORT):

1. `best_cl` tracks the highest/lowest close since entry — updates each bar
2. `tl` computes the trail level from `best_cl` and current ATR
3. `cur_stop` only tightens (moves in favorable direction) — never widens
4. `strategy.exit()` updates the exit stop level each bar

This is evaluated at bar close only. The trailing stop level is deterministic given the bar's close and ATR values. No repainting possible. ✓

---

## 12. Explicit Conclusions

### Signals are generated only on confirmed closed bars

**YES.** `calc_on_every_tick = false` + all indicators use `close` + all filters use confirmed data = bar-close-only signal generation. **COMPLIANT with AR1, AR2, AR3.** ✓

### Multi-timeframe data is future-safe

**YES.** `request.security()` with `lookahead = barmerge.lookahead_off` prevents any future H4 data from leaking into the current H1 bar. `na` guards provide additional safety. **COMPLIANT with AR4.** ✓

### Could signals differ between historical and live?

**SIGNAL: No.** Given the same data, the same signals fire deterministically.
**EXECUTION: Yes, slightly.** Stop-order fill prices may differ (backtest: exact stop price; live: broker fill with spread/slippage). This is expected and correctly handled by the alert payload's authoritative fields.

### Could exits differ materially between historical and live?

**YES, in two known ways:**
1. **One-bar SL gap** (backtest only — first bar after fill is unprotected by `strategy.exit()`). Does not exist in live execution (Trading Agent places SL immediately).
2. **SL monitoring granularity** (backtest: bar OHLC; live: tick-by-tick). Both are legitimate fill models.

Neither creates spec non-compliance. Both are documented.

### State machine behavior could create unexpected live behavior?

**NO.** The state machine is purely internal to Pine for backtest tracking. In live execution, the state machine determines which alert to fire — the Trading Agent manages the actual broker state.

---

## 13. Final Anti-Repaint Verdict

**FULLY COMPLIANT.**

All four spec anti-repaint rules (AR1-AR4) are correctly implemented in the IRB Pine strategy. No future leak, no lookahead, no repainting. The single `request.security()` call uses `lookahead_off` with conservative `na` guards. Signal generation is deterministic and final at bar close. Known backtest-vs-live differences are documented, expected, and do not compromise signal correctness.

| Rule | Status | Implementation |
|------|--------|---------------|
| AR1 — Bar-close-only evaluation | ✅ PASS | `calc_on_every_tick = false` (L38) |
| AR2 — Closed bar data only | ✅ PASS | All indicators use `close` source (L105, 108, 111, 114) |
| AR3 — Detection final once bar closes | ✅ PASS | No recalculation mechanism; `alert.freq_once_per_bar_close` |
| AR4 — H4 EMA with lookahead off | ✅ PASS | `lookahead = barmerge.lookahead_off` (L116) |

---

## 14. Phase 4 Re-Validation (2026-03-17)

**Status: Anti-repaint compliance confirmed — no changes since Phase 3.**

Phase 4 re-examined all bar references and temporal constructs for anti-repaint safety in the context of alert readiness.

### Bar Reference Anti-Repaint Summary

| Reference | Direction | Anti-Repaint Safe? |
|-----------|-----------|-------------------|
| `high`, `low`, `open`, `close` (current bar) | Current closed bar | Yes — `calc_on_every_tick = false` |
| `ema_h1[SLOPE_LOOKBACK]` (20 bars back) | Backward | Yes |
| `ema_h4[MTF_H1_LOOKBACK]` (20 H1 bars back) | Backward | Yes |
| `strategy.position_size[1]` (1 bar back) | Backward | Yes |
| `prev_stop` (prior bar's stop level) | Backward (via `var`) | Yes |
| `time_close` (current bar close timestamp) | Current | Yes — finalized at bar close |
| `bar_index` (chart bar counter) | Current | Yes — monotonically increasing |

No forward-looking references exist. All history operators (`[N]`) access confirmed past data. All `var`-declared variables update deterministically at bar close.

### Alert Timing Anti-Repaint Safety

All four alert types use `alert.freq_once_per_bar_close`:
- Signal alert (L478): fires once when bar closes and signal conditions are met
- Trail alert (L496): fires once when stop tightens at bar close
- Cancel alert (L512): fires once when trigger window expires at bar close
- Close alert (L527): fires once when time stop triggers at bar close

No alert can fire mid-bar. No alert can fire twice for the same event. No alert can be retroactively revoked.

### Phase 4 Anti-Repaint Verdict

**FULLY COMPLIANT — AR1-AR4 hold. All bar references are backward-looking or current-bar-at-close. No repaint risk identified.**
