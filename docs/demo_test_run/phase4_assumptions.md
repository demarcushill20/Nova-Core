# NovaTrade Demo Test Run — Phase 4 Assumptions (Fresh IRB)

**Phase:** 4 (Backtesting and Validation)
**Date:** 2026-03-17
**Status:** LOCKED
**Agent:** Backtesting Agent
**Replaces:** EMA Crossover Phase 4 assumptions (2026-03-16, SUPERSEDED)

---

## Inherited Assumptions

All assumptions from Phase 0 (A1-A5), Phase 0.5 (IRB source boundary), Phase 1 (A1-A10, U1-U8 resolutions), Phase 2 (PA-IRB-1 to PA-IRB-15), and Phase 3 (CA-IRB-1 to CA-IRB-6) remain in force. Phase 4 does not alter or contradict any of them.

**EMA-specific assumptions (BA1-BA8) are SUPERSEDED.** They applied to the EMA Crossover backtester and are irrelevant to IRB.

---

## New Phase 4 Assumptions (IRB-Specific)

| ID | Statement | Rationale | Risk Level | Revisit? |
|----|-----------|-----------|------------|----------|
| BA-IRB-1 | **Analytical validation is sufficient for a CONDITIONAL GO recommendation when no live backtest is available** | Phase 3 validates correctness (134/134 spec rules, 45-check compile, AR1-AR4 anti-repaint). Phase 4 analytical validation estimates trade frequency, drawdown, and FTMO compliance from strategy structure and known EURUSD H1 characteristics. The CONDITIONAL status requires live verification (C1-C5) before upgrading to full GO. | Medium | Phase 5 must confirm C1-C5. If C2 fails (zero trades in 30 days), this assumption was insufficient. |
| BA-IRB-2 | **IRB trade frequency on EURUSD H1 will be 0.1-0.4 completed trades per day** | Based on analytical filter interaction: IRB geometry (~15% of bars) × trend filter (~40%) × MTF (~60%) × ADX (~50%) × overextension (~85%) ≈ 1.5% of bars qualify as signals. With state machine blocking and ~50% stop-order fill rate, completed trades are ~0.1-0.4/day. Spec expects 0-2 signals/day (§5.9). | Medium | C2 and C3 will confirm. If actual frequency is <0.05/day or >1.0/day, this estimate was wrong. |
| BA-IRB-3 | **Stop orders on EURUSD H1 fill approximately 40-60% of the time within 20 bars** | IRB stop orders are placed at the candle extreme + 1 pip. In a trending market, price continuing past the IRB extreme is the trend-continuation thesis. Estimated fill rate based on: IRBs form during retracements, and the resumption breakout is the expected outcome in trending conditions. 40-60% accounts for IRBs that form near temporary reversals. | Medium | Can only be measured with a live backtest or demo data. |
| BA-IRB-4 | **The 5-filter combination does not create pathological signal suppression on EURUSD H1** | Each filter was independently assessed for restrictiveness. The trend filter (s=0.4) and ADX filter (≥20) are the most restrictive, both targeting trending conditions. During sustained trends, both filters pass simultaneously, allowing IRB signals. During ranging markets, both filters correctly suppress signals. No identified scenario where the filters contradict (e.g., one passes but the other blocks in a way that systematically prevents all trading). | Low | C2 will confirm — zero trades in 30 days would indicate pathological suppression. |
| BA-IRB-5 | **FTMO compliance is maintained with 1% risk per trade and 0-2 signals per day** | Maximum daily exposure: 2 signals/day × 1% risk = 2% max daily loss from new trades. Adding unrealized floating loss on existing positions: worst case ~3% daily equity drawdown (vs 5% limit). Total drawdown over extended losing streak: 8 consecutive losses ≈ 7.7% (vs 10% limit). Both scenarios maintain FTMO compliance with margin. | Low | Phase 5 must measure actual equity drawdown via MetaApi snapshots. |
| BA-IRB-6 | **The trailing stop mechanism (1.5 × ATR) provides meaningful profit protection on EURUSD H1** | ATR(14) on EURUSD H1 is typically 15-40 pips. Trail distance = 1.5 × ATR = 22.5-60 pips from best close. This allows normal pullbacks (~1 ATR) while locking in profits beyond the trail level. In strong trends where price moves 2-3 ATR from entry, the trail captures 0.5-1.5 ATR of profit. Not aggressive enough for maximum profit extraction, but provides a defined exit mechanism. | Low | Demo run will reveal actual trail behavior. If most trail exits are at break-even or small loss, multiplier may need adjustment. |

---

## Prior EMA Assumptions — Status After IRB Transition

| ID | EMA Assumption | IRB Status |
|----|---------------|------------|
| BA1 | Yahoo data representative | **SUPERSEDED** — no Python backtester for IRB |
| BA2 | Python EMA matches Pine EMA | **SUPERSEDED** — no Python backtester |
| BA3 | One-bar SL/TP gap matches Pine | **SUPERSEDED** — IRB uses stop orders, not market orders |
| BA4 | Daily bar data gives structural insights | **SUPERSEDED** — no D1 backtest for IRB |
| BA5 | Spread impact negligible | **PARTIALLY APPLICABLE** — concept carries forward to IRB |
| BA6 | Weekend gap behavior representative | **APPLICABLE** — gap risk is strategy-independent |
| BA7 | Closed-trade DD proxy for equity DD | **APPLICABLE** — methodology concern for any backtester |
| BA8 | SL/TP-bar signal suppression negligible | **SUPERSEDED** — EMA backtester only |

---

## Risk Assessment Summary

| Risk Level | Count | IDs |
|------------|-------|-----|
| Low | 3 | BA-IRB-4, BA-IRB-5, BA-IRB-6 |
| Medium | 3 | BA-IRB-1, BA-IRB-2, BA-IRB-3 |

**Three medium-risk assumptions (BA-IRB-1, BA-IRB-2, BA-IRB-3).** All relate to the absence of a live backtest. These are testable: C1-C5 conditions in the deployment recommendation will validate or invalidate them.

**No high-risk assumptions in Phase 4.** The analytical validation approach is conservative — CONDITIONAL GO explicitly requires live verification before deployment.
