# NovaTrade Demo Test Run — Phase 1 Assumptions (IRB)

**Phase:** 1 (Formal Strategy Specification) — RESTARTED for IRB
**Date:** 2026-03-17
**Status:** LOCKED
**Replaces:** EMA Crossover Phase 1 assumptions (2026-03-16)

---

## Inherited Assumptions (from Phase 0)

These assumptions carry forward from Phase 0 and remain applicable to the IRB strategy specification.

| ID | Assumption | Risk Level | Phase 0 Ref |
|----|-----------|------------|-------------|
| A1 | EURUSD.sim spread acceptable for H1 strategy | Low | Phase 0 A1 |
| A2 | MetaApi G1 tick rate sufficient for H1 bar-close signals | Negligible | Phase 0 A2 |
| A3 | Agents are pipeline stages in Python, not autonomous Claude sub-agents | Medium | Phase 0 A3 |
| A4 | ~~Market orders only~~ **SUPERSEDED** — IRB uses stop orders (BUY_STOP/SELL_STOP) | N/A | Phase 0 A4 (superseded) |
| A5 | Weekend position holding acceptable | Low | Phase 0 A5 |

---

## New Phase 1 Assumptions (IRB-specific)

| ID | Assumption | Risk Level | Rationale |
|----|-----------|------------|-----------|
| A6 | **MetaApi supports BUY_STOP and SELL_STOP order types on FTMO demo** | Low | MT5 natively supports stop orders. MetaApi wraps the MT5 API. FTMO demo accounts use standard MT5 order types. If wrong: stop orders cannot be placed, blocking the entire IRB strategy. Mitigation: verify in Phase 2 with a test order before go-live. |
| A7 | **MetaApi supports stop-loss modification for trailing stop updates** | Medium | The IRB trailing stop requires modifying the SL on an open position on every bar close. MetaApi must support `modifyPosition()` or equivalent. If wrong: trailing stop cannot be implemented via the adapter — would require closing and reopening positions (ugly but functional). Should be verified in Phase 2/3. |
| A8 | **IRB on EURUSD H1 will generate >= 10 trade signals in 10 calendar days with all filters active** | Medium | IRB signals on H1 are less frequent than EMA crossover. With trend filter (s=0.4), MTF alignment, ADX >= 20, and ATR filter, many potential IRBs will be filtered out. Over 240 H1 bars, estimated 1-3 qualifying IRBs per trading day = 5-15 signals. Not all will trigger (stop orders may expire). If wrong: failure condition F8 applies. Mitigation: lower trend threshold (s=0.3) or widen trigger window in next iteration. |
| A9 | **Normalized EMA slope (TF-B) with s=0.4 is appropriate for EURUSD H1** | Medium | The threshold s=0.4 was chosen as mid-low in the 0.2-0.8 range. On EURUSD H1, typical ATR(14) is 15-40 pips and EMA(20) displacement over 20 bars in a trend is 20-80 pips, giving slope values of 0.5-2.0 in trending markets and <0.3 in ranging markets. Threshold 0.4 should admit most genuine trends. If wrong: too restrictive (few signals) or too permissive (noisy signals). Sweepable in 0.2-0.8 for robustness testing. |
| A10 | **ATR overextension threshold k=2.0 is appropriate for EURUSD H1** | Low | EURUSD H1 candles rarely exceed 2× ATR unless major news event. k=2.0 filters only extreme candles. If wrong: too many signals filtered (increase k) or too few (decrease k). Low-risk because k is sweepable 1.5-3.0. |
| A11 | **ATR trailing stop with multiplier 1.5 provides adequate profit protection** | Medium | The 1.5× ATR trail gives room for 1 ATR of pullback while protecting profits beyond that. On EURUSD H1 with ATR ~30 pips, the trail distance is ~45 pips from the best close. This is looser than Hoffman's "50% of open profit" concept but is fully deterministic. If wrong: too tight (stopped out of winners) or too loose (give back too much profit). Sweepable in 1.0-3.0. |
| A12 | **40-bar time stop prevents infinite holds without materially affecting strategy** | Low | 40 H1 bars ≈ 2 trading days. Most IRB moves resolve within a few bars (the thesis is about quick trend resumption). A 40-bar hold suggests the thesis has expired. If wrong: premature exit of slow-developing trades — acceptable for a systems test. |
| A13 | **ADX(14) >= 20 adequately separates trending from ranging markets** | Low | ADX is a standard trend strength indicator. Values below 20 are widely considered "no trend" in academic and practitioner literature. If wrong: too restrictive or too permissive — easily adjustable. Low-risk because the trend slope filter (U1) already provides primary sideways protection. |
| A14 | **Risk-based position sizing with r=0.01 is compatible with the risk gate volume bounds** | Low | For typical IRB stop distances (20-60 pips on H1 EURUSD), lot size = $1,000 / (20-60 × $10) = 1.67-0.17 lots. The upper end (small stops) may exceed the 1.0 lot max — clamped by the risk gate. The lower end is well above the 0.01 lot min. If wrong: clamping reduces risk consistency — acceptable for systems test. |
| A15 | **H4 EMA(20) rising/falling over 5 bars adequately represents H4 trend direction** | Low | 5 H4 bars = 20 H1 bars = same temporal window as the H1 slope lookback. Simple rising/falling check (current > 5 bars ago) is a minimal requirement — less restrictive than applying a slope threshold to H4. If wrong: H4 check is too permissive — could add slope threshold to H4 in future iteration. |
| A16 | **One position at a time (no reversal) is acceptable for IRB strategy** | Low | Unlike EMA crossover which reverses on opposing signals, IRB is a continuation setup. Opening a reverse position while the current thesis is still active contradicts the strategy logic. One-position-at-a-time simplifies the state machine and aligns with "one strategy, one symbol." |
| A17 | **Pending stop orders carry over weekends** | Low | FTMO/MT5 keeps pending orders active over weekends. A buy-stop placed Friday afternoon may trigger on Monday's gap. This is acceptable — the stop price and SL are already set, and weekend gap risk is acknowledged in Phase 0 A5. |
| A18 | **H1 bar close time from MetaApi aligns with TradingView for cross-validation** | Low | Both should use UTC bar boundaries for H1. If wrong: IRB detection timing may differ between Pine backtest and live execution. This should be verified in Phase 3. |

---

## Risk Assessment Summary

| Risk Level | Count | Details |
|------------|-------|---------|
| Negligible | 1 | A2 (MetaApi tick rate) |
| Low | 10 | A1, A5, A6, A10, A12, A13, A14, A15, A16, A17, A18 |
| Medium | 5 | A3 (agent model), A7 (SL modification), A8 (signal frequency), A9 (slope threshold), A11 (trail multiplier) |
| N/A | 1 | A4 (superseded) |

**No high-risk assumptions.** The five medium-risk assumptions should be monitored:
- A7 (SL modification) — verify MetaApi supports position SL modification in Phase 2
- A8 (signal frequency) — if backtest shows <10 trades, revisit threshold parameters
- A9 (slope threshold) — sweepable, verify in Phase 4 backtest
- A11 (trail multiplier) — sweepable, verify in Phase 4 backtest
- A3 (agent model) — inherited from Phase 0, still applicable
