# NovaTrade Demo Test Run — Sample Trade Audit (Fresh IRB)

**Phase:** 4 (Backtesting and Validation)
**Date:** 2026-03-17
**Status:** COMPLETE (analytical scenarios — no live trades)
**Agent:** Backtesting Agent
**Strategy:** Rob Hoffman IRB v2.0.0
**Replaces:** EMA Crossover sample trade audit (2026-03-16, SUPERSEDED)

---

## Methodology

No live backtest was executed. The following are **analytical trade scenarios** constructed from the Pine implementation to verify that the strategy logic produces correct behavior across key trade lifecycle paths. Each scenario traces through the Pine code to confirm spec compliance.

These scenarios replace the EMA sample trade audit (7 trades using EMA crossover signals, fixed SL/TP, reversal exits — all irrelevant to IRB).

---

## Scenario 1: LONG IRB — Trailing Stop Win

**Setup:** Strong uptrend on EURUSD H1. EMA(20) slope = 0.6 (above 0.4 threshold). H4 EMA rising. ADX = 28 (above 20). A bullish IRB bar forms: High = 1.10500, Low = 1.10200, Open = 1.10280, Close = 1.10250.

**IRB Geometry Check:**
- Range = 1.10500 - 1.10200 = 0.00300 (30 pips) > 0 ✓
- Threshold = 1.10500 - 0.45 × 0.00300 = 1.10500 - 0.00135 = 1.10365
- Open (1.10280) ≤ 1.10365 ✓
- Close (1.10250) ≤ 1.10365 ✓
- `is_up_irb = true` ✓

**Overextension Check:**
- ATR(14) = 0.00200 (20 pips)
- oe_ratio = 0.00300 / 0.00200 = 1.50 ≤ 2.0 ✓

**Signal:** `sig_long = warmup_ok AND is_up_irb AND trend_up AND h4_up AND sw_pass AND oe_pass = true`

**Pending Order Placement:**
- State: FLAT → PENDING_LONG
- Entry price (ep) = 1.10500 + 0.0001 = **1.10510** (BUY_STOP)
- Stop loss (sp) = 1.10200 - 0.0001 = **1.10190**
- Stop distance = |1.10510 - 1.10190| / 0.0001 = 32.0 pips
- Risk = $100,000 × 0.01 = $1,000
- Lot size = $1,000 / (32.0 × $10) = 0.3125 → rounded to **0.31 lots**
- Units = 0.31 × 100,000 = 31,000
- `strategy.entry("Long", strategy.long, stop = 1.10510, qty = 31000)` ✓
- Alert: PLACE_STOP_ORDER with all 30 fields ✓

**Fill (Bar N+2):** Price reaches 1.10510 → BUY_STOP fills.
- State: PENDING_LONG → LONG
- `cur_stop = 1.10190` (initial SL at irb_lo - PIP_BUF)
- `best_cl = close` of fill bar
- `pos_bars = 0`

**Trailing Stop (Bar N+3, close = 1.10650):**
- `best_cl = max(1.10650, previous) = 1.10650`
- ATR(14) = 0.00200
- trail_level = 1.10650 - 1.5 × 0.00200 = 1.10650 - 0.00300 = 1.10350
- `cur_stop = max(1.10190, 1.10350) = 1.10350` ← **tightened** ✓
- Alert: MODIFY_SL (old_stop=1.10190, new_stop=1.10350) ✓

**Trailing Stop (Bar N+8, close = 1.10900):**
- `best_cl = 1.10900`
- trail_level = 1.10900 - 0.00300 = 1.10600
- `cur_stop = max(1.10350, 1.10600) = 1.10600` ← **tightened** ✓
- Alert: MODIFY_SL (old_stop=1.10350, new_stop=1.10600) ✓

**Exit (Bar N+12):** Price pulls back to 1.10600, trailing stop triggers.
- `strategy.exit("Long Exit", "Long", stop = 1.10600)` fills at 1.10600
- P&L = (1.10600 - 1.10510) / 0.0001 = **+9.0 pips** (0.28R)
- State: LONG → FLAT
- All `var` variables reset ✓

**Spec Compliance:** PASS — IRB geometry correct, filters verified, stop order placed, fill detected, trailing stop tightened 2 times, exit via trailing stop.

---

## Scenario 2: SHORT IRB — Stop-Loss Exit

**Setup:** Strong downtrend on EURUSD H1. EMA(20) slope = -0.55. H4 EMA falling. ADX = 24. A bearish IRB bar forms: High = 1.08800, Low = 1.08400, Open = 1.08750, Close = 1.08780.

**IRB Geometry Check:**
- Range = 0.00400 (40 pips) > 0 ✓
- Threshold = 1.08400 + 0.45 × 0.00400 = 1.08400 + 0.00180 = 1.08580
- Open (1.08750) ≥ 1.08580 ✓
- Close (1.08780) ≥ 1.08580 ✓
- `is_dn_irb = true` ✓

**Overextension:** 0.00400 / 0.00250 (ATR) = 1.60 ≤ 2.0 ✓

**Signal:** `sig_short = true`

**Pending Order:**
- Entry (ep) = 1.08400 - 0.0001 = **1.08390** (SELL_STOP)
- Stop (sp) = 1.08800 + 0.0001 = **1.08810**
- Stop distance = 42.0 pips
- Lot size = $1,000 / (42.0 × $10) = 0.2381 → **0.24 lots**
- Alert: PLACE_STOP_ORDER ✓

**Fill:** Price drops to 1.08390 → SELL_STOP fills.
- State: PENDING_SHORT → SHORT
- `cur_stop = 1.08810` (irb_hi + PIP_BUF)
- `best_cl = close` of fill bar

**Price Reverses:** Market reverses upward. Price reaches 1.08810 on bar N+5.
- `strategy.exit("Short Exit", "Short", stop = 1.08810)` fills at 1.08810
- P&L = (1.08390 - 1.08810) / 0.0001 = **-42.0 pips** (-1.0R)
- State: SHORT → FLAT

**Spec Compliance:** PASS — SL placed at IRB opposite side + 1 pip. Loss exactly equals stop distance. Thesis invalidation correctly handled by SL.

---

## Scenario 3: LONG IRB — Time Stop Exit

**Setup:** Moderate uptrend. IRB detected, stop order fills. Price consolidates sideways — neither SL nor trailing stop triggers.

**Post-Fill:** Position entered at 1.12100, SL at 1.11850 (25-pip SL).
- Trailing stop: best_cl barely moves. Trail level stays near initial SL.
- Price oscillates between 1.12050-1.12200 for 40 bars.

**Bar 40 (pos_bars >= TIME_STOP):**
- `strategy.close("Long", comment = "TIME_STOP")` ✓
- Close price = 1.12150
- P&L = (1.12150 - 1.12100) / 0.0001 = **+5.0 pips** (small win)
- Alert: CLOSE_POSITION with close_reason="TIME_STOP", bars_held=40 ✓
- State: LONG → FLAT

**Spec Compliance:** PASS — Time stop at exactly 40 bars per spec §5.4. Position closed at market. All fields in close_alert match schema.

---

## Scenario 4: Pending Order — Trigger Window Expiry

**Setup:** IRB detected, pending stop order placed. Price never reaches the stop order trigger level.

**Signal fires:** BUY_STOP placed at 1.15300 (irb_high + 1 pip).
- State: FLAT → PENDING_LONG
- `pend_bars = 0`

**Bars 1-19:** Price stays below 1.15300. `pend_bars` increments each bar.

**Bar 20 (pend_bars >= TRIGGER_WIN):**
- `strategy.cancel("Long")` ✓
- State: PENDING_LONG → FLAT
- All pending state variables reset ✓
- Alert: CANCEL_ORDER with cancel_reason="TRIGGER_WINDOW_EXPIRED", bars_elapsed=20 ✓

**Spec Compliance:** PASS — Trigger window correctly enforced at 20 bars per spec §4.3. No trade was opened. No P&L.

---

## Scenario 5: IRB Replacement — Same-Direction

**Setup:** LONG IRB signal places pending order. While still pending (unfilled), a new LONG IRB forms on a different bar.

**First IRB (Bar N):** BUY_STOP at 1.14200.
- State: FLAT → PENDING_LONG
- `irb_hi = 1.14190`, `irb_lo = 1.13900`

**Second IRB (Bar N+8):** New qualifying LONG IRB with High = 1.14150, Low = 1.13850.
- State remains PENDING_LONG but levels update:
- New entry: 1.14150 + 0.0001 = **1.14160**
- New SL: 1.13850 - 0.0001 = **1.13840**
- `pend_bars := 0` ← **window resets** ✓
- `strategy.entry("Long", strategy.long, stop = 1.14160, qty = new_qty)` ← **replaces old order** ✓
- Alert: REPLACE_STOP_ORDER ✓

**Spec Compliance:** PASS — Same-direction replacement per spec §4.2. Window resets per spec. Old pending order implicitly cancelled by new `strategy.entry()` with same ID.

---

## Scenario 6: Signal Suppression — Opposite Direction While Pending

**Setup:** LONG IRB places pending BUY_STOP. While pending, a SHORT IRB signal fires.

**State:** PENDING_LONG (BUY_STOP waiting to fill)
**Event:** `sig_short = true`

**Pine behavior:** The if/else-if chain at L304-354 only processes:
- `state == S_FLAT and sig_long` → place long
- `state == S_FLAT and sig_short` → place short
- `state == S_PLONG and sig_long` → replace long
- `state == S_PSHORT and sig_short` → replace short

`state == S_PLONG and sig_short` matches NONE of these conditions. Falls through with no action.

**Result:** SHORT signal is silently ignored. Pending BUY_STOP remains active. No alert fired.

**Spec Compliance:** PASS — per spec §6 PENDING_LONG transition: "SHORT_IRB_SIGNAL while pending long → Ignore — only same-direction replacement allowed."

---

## Scenario 7: Signal Suppression — While in Position

**Setup:** LONG position open. A new LONG IRB signal fires.

**State:** S_LONG
**Event:** `sig_long = true`

**Pine behavior:** Signal processing (L304-354) only fires for `state == S_FLAT` or same-direction PENDING. `state == S_LONG` is not matched. Signal falls through.

**Result:** Signal ignored. Position continues with existing trailing stop management.

**Spec Compliance:** PASS — per spec §6 LONG transition: "LONG_IRB_SIGNAL (while LONG) → Ignore — already in position."

---

## Coverage Matrix

| Scenario | Direction | Lifecycle Stage | Exit Type | Spec Section |
|----------|-----------|----------------|-----------|-------------|
| 1 | LONG | Full trade | Trailing stop | §3.1, §4.1, §5.3 |
| 2 | SHORT | Full trade | Stop-loss | §3.1, §4.1, §5.1 |
| 3 | LONG | Full trade | Time stop | §5.4 |
| 4 | LONG | Pending only | Trigger window expiry | §4.3 |
| 5 | LONG | Pending → replaced | IRB replacement | §4.2 |
| 6 | LONG (pending) | Signal rejection | Opposite suppression | §6 (IC9) |
| 7 | LONG (in position) | Signal rejection | Position suppression | §6 (IC8) |

### Features Verified

| Feature | Scenarios |
|---------|-----------|
| IRB geometry detection (45% rule) | 1, 2 |
| 5-filter signal chain | 1, 2 |
| Stop order entry (BUY_STOP/SELL_STOP) | 1, 2, 4, 5 |
| Dynamic SL (IRB opposite ± 1 pip) | 1, 2 |
| Risk-based position sizing | 1, 2 |
| Trailing stop (tighten-only) | 1 |
| Stop-loss exit | 2 |
| Time stop (40 bars) | 3 |
| Trigger window expiry (20 bars) | 4 |
| IRB replacement (same-direction) | 5 |
| Opposite-direction signal suppression | 6 |
| In-position signal suppression | 7 |
| 4 alert types (signal, trail, cancel, close) | 1, 2, 3, 4, 5 |

### Key Differences from EMA Audit

| Dimension | EMA Audit (superseded) | IRB Audit (current) |
|-----------|----------------------|---------------------|
| Entry type | Market order at next bar open | Stop order at IRB extreme + 1 pip |
| SL type | Fixed 50 pips | Dynamic (IRB range + 2 pips) |
| TP type | Fixed 75 pips | None — trailing stop only |
| Reversal | LONG→SHORT on opposing signal | NO reversal — signal suppressed |
| Pending orders | N/A | Full lifecycle tested (place, replace, cancel) |
| Time stop | N/A | 40-bar exit tested |
| Alert types | 1 | 4 |

---

## Limitations

1. **No actual trade data.** All scenarios are analytical traces through Pine code. Actual fills, slippage, and market conditions are not captured.
2. **No P14-equivalent gap analysis.** The one-bar SL gap (P3-IRB-1) cannot be assessed without actual backtest data.
3. **No edge-case discovery.** Analytical scenarios test expected paths. Unexpected interactions (e.g., cancel + signal on same bar, trailing stop vs time stop on same bar) would only be discovered by running the strategy on real data.
4. **Scenario 1 exit P&L is modest.** In practice, trailing stop exits in strong trends could yield much larger profits (2-5R). The scenario uses a moderate example.

---

*Strategy source of truth: `strategy_spec.yaml` v2.0.0 (Rob Hoffman IRB)*
*Pine implementation: `strategy.pine` v2.0.0 (549 lines, Phase 3 PASS)*
