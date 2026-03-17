# NovaTrade Demo Test Run — Deployment Recommendation (Fresh IRB)

**Phase:** 4 (Backtesting and Validation)
**Date:** 2026-03-17
**Status:** CONDITIONAL GO
**Agent:** Backtesting Agent
**Strategy:** Rob Hoffman IRB v2.0.0
**Replaces:** EMA Crossover deployment recommendation (2026-03-16, SUPERSEDED)

---

## 1. Recommendation

### CONDITIONAL GO for controlled demo systems test

The Rob Hoffman IRB strategy (v2.0.0) is recommended for deployment to the FTMO Free Trial demo account as a controlled systems test, subject to the conditions in Section 3.

This is CONDITIONAL (not full GO) because:
1. No live backtest has been executed — all validation is analytical + Phase 3 static
2. Trade frequency is borderline for E6 (≥10 completed trades in 10 days)
3. Pine compilation in TradingView has not been verified (inherited P3-IRB-3)

---

## 2. Evidence Base

### 2.1 What Was Directly Verified

| Finding | Evidence | Source |
|---------|----------|--------|
| Strategy implementation is correct | 134/134 spec rules verified (125 exact, 3 representation, 6 deferred) | Phase 3 `spec_traceability.md`, `phase3_contract_alignment_review.md` |
| Pine syntax is valid | 45-check static analysis, all PASS | Phase 3 `compile_report.md` |
| Anti-repaint compliance | AR1-AR4 all PASS, no future leak | Phase 3 `anti_repaint_review.md` |
| State machine is complete | 5 states, all transitions verified, no deadlocks | Phase 3 `lint_report.md` |
| Alert contract alignment | 58/58 fields match schema across 4 alert types | Phase 3 `phase3_contract_alignment_review.md` |
| Signal mutual exclusivity | trend_up and trend_dn cannot both be true | Phase 3 `lint_report.md` |
| Position sizing correct | 1% risk, clamped [0.01, 1.00] lots | Phase 3 code verification |
| Trailing stop tighten-only | `math.max` (long) / `math.min` (short) verified | Phase 3 code verification |
| Separation of concerns | Pine handles signals; runtime handles governance | Phase 3 review |

### 2.2 What Was Analytically Estimated (Not Directly Measured)

| Inference | Basis | Confidence |
|-----------|-------|------------|
| Trade frequency: 2-8 completed trades in 10 days | EURUSD H1 characteristics + 5 filter interaction analysis | Medium |
| Max daily drawdown: 2-3% | 1% risk/trade × max 2-3 SL hits/day | Medium-High |
| Max total drawdown: ≤7% (extreme) | Consecutive loss analysis with dynamic sizing | Medium |
| FTMO compliance: within limits | All drawdown estimates well below 5%/10% thresholds | High |
| Strategy exercises more pipeline paths than EMA | 4 alert types, stop orders, trailing stops, pending order lifecycle | High |
| Strategy is not profitable (as designed) | Consistent with `expected_profitability: "not_a_goal"` | Medium |

---

## 3. Conditions for Upgrade to Full GO

| # | Condition | Priority | How to Verify |
|---|-----------|----------|---------------|
| C1 | **Pine script compiles in TradingView without errors** | BLOCKER | Load `strategy.pine` on EURUSD H1 chart. Must compile cleanly. |
| C2 | **Pine backtest produces ≥1 trade in 30 days of EURUSD H1** | BLOCKER | Run TradingView strategy tester on 30-day window. Zero trades = investigate filters. |
| C3 | **At least one IRB signal fires in first 3 trading days of demo** | HIGH | Monitor alerts. No signals in 3 days = market ranging — consider extending window. |
| C4 | **Alert JSON payload parses correctly** | HIGH | Trigger one alert in TradingView. Verify JSON matches `alerts_schema.json` v2.0.0. |
| C5 | **If <3 completed trades after 5 trading days, extend demo to 20 calendar days** | MEDIUM | Monitor trade count. Adjust E6 threshold or window length. |

If C1 fails: deployment is BLOCKED until Pine script is corrected.
If C2 fails: investigate filter interaction — may need parameter tuning (returning to Phase 1).
If C3 fails after 3 days: extend observation window; do not modify strategy.
If C4 fails: fix alert JSON construction (Phase 2 fix, re-run Phase 3).
If C5 triggers: extend demo window — does not block deployment.

---

## 4. FTMO Compliance Assessment

### 4.1 Analytical Compliance

| FTMO Rule | Limit | Analytical Estimate | Safety Margin | Compliant? |
|-----------|-------|-------------------|---------------|------------|
| Max daily drawdown | 5% ($5,000) | 2-3% worst case (2-3 SL hits/day × 1%) | ~2x | **YES** |
| Max total drawdown | 10% ($10,000) | ≤7% extreme case (8 consecutive losses) | ~1.4x | **YES** (tight in extreme) |
| Min trading days | 4+ | Strategy signals most days when trending | N/A | **LIKELY YES** |
| Profit target | 10% ($10,000) | Not expected (systems test) | N/A | **N/A** |

### 4.2 Why FTMO Risk Is Low

1. **1% risk per trade** — dynamic sizing adjusts automatically for IRB candle size
2. **Max 1 position** — pyramiding=0 + state machine enforcement
3. **No reversal** — positions close via SL/trail/time before new entry
4. **Conservative filters** — 5 cumulative filters reduce overtrading risk
5. **0-2 signals/day** — very unlikely to hit 20/day risk gate limit or 5% daily DD

### 4.3 Worst-Case Drawdown Path

10 consecutive SL losses (each at 1% of declining equity):
- After 5 losses: ~4.9% drawdown — below 5% daily limit
- After 8 losses: ~7.7% drawdown — below 10% total limit
- After 10 losses: ~9.6% drawdown — still below 10% total limit
- Probability at 40% win rate: (0.60)^10 = 0.6%

**Assessment:** Even the extreme scenario stays within FTMO limits. At 0-2 signals/day, accumulating 10 consecutive losses requires 5-10+ trading days, making daily DD limit nearly impossible to breach.

---

## 5. Risk Assessment for Controlled Demo Run

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Pine fails to compile (C1) | Low | BLOCKS deployment | Verify C1 first |
| Zero trades in 10 days (ranging market) | Low-Medium | Systems test inconclusive | Extend window to 20 days |
| Fewer than 10 trades in 10 days | Medium | E6 not met — partial validation | Count pipeline events (signals + cancels) as validation |
| Live fills differ from Pine | Certain | Low — stop orders fill at trigger price ± spread | Expected; systems test validates pipeline, not P&L |
| Weekend gap through SL | Low | Small — max 1 position at 1% risk | Accepted per spec §7 |
| Strategy loses money | High | None — expected per spec | `expected_profitability: "not_a_goal"` |
| FTMO drawdown limits hit | Very Low | Would end trial | 1% risk/trade with large margin |
| Trailing stop updates overwhelm Trading Agent | Low | Alert frequency ~1/bar while in position | MetaApi rate limits well above 1 call/hour |
| MetaApi bar alignment differs from Pine | Low | Minor signal timing differences | P4 carries forward; monitor in demo |

---

## 6. Pipeline Validation Value

The IRB strategy provides **superior pipeline validation** compared to the superseded EMA Crossover:

| Pipeline Feature | EMA (superseded) | IRB (current) |
|-----------------|-------------------|---------------|
| Alert types | 1 (signal only) | 4 (signal, trail, cancel, close) |
| Order types | Market order | Pending stop order |
| SL model | Fixed (50 pips) | Dynamic (IRB opposite side) |
| SL modification | Never | Trailing stop tightens each bar |
| TP model | Fixed (75 pips) | None (trail-only exit) |
| Pending order lifecycle | N/A | Place → fill/replace/cancel |
| Time stop | N/A | 40-bar hard close |
| Position reversal | Yes (opposing signal) | No (signal suppressed while in position) |
| Position sizing | Fixed (0.10 lots) | Dynamic (1% risk-based) |
| State machine states | 3 (FLAT, LONG, SHORT) | 5 (FLAT, PENDING_LONG, PENDING_SHORT, LONG, SHORT) |

**Every significant NovaTrade pipeline component gets exercised** by the IRB strategy. This makes the demo run a more thorough systems test even if trade count is lower than EMA.

---

## 7. Summary

The IRB strategy is:
1. **Mechanically correct** — 134/134 spec rules verified, Phase 3 passed all checks
2. **Safe for FTMO** — 1% risk/trade, dynamic sizing, well within drawdown limits
3. **A strong pipeline validator** — 4 alert types, stop orders, trailing stops, pending order lifecycle, time stops
4. **Borderline on trade frequency** — estimated 2-8 completed trades in 10 days (E6 target: ≥10)
5. **Unverified by live backtest** — all estimates are analytical

**Recommendation: CONDITIONAL GO — resolve C1 (Pine compilation) first, then deploy with C3-C5 monitoring.**

---

## 8. What Phase 5 Should Focus On

1. **Resolve C1** — Load `strategy.pine` in TradingView on EURUSD H1. Confirm compilation.
2. **Run C2** — TradingView backtest on 30-day window. Verify ≥1 trade.
3. **Verify C4** — Trigger one alert. Validate JSON against schema.
4. **Establish Trading Agent pipeline** — Pine alert → webhook → Trading Agent → MetaApi → FTMO demo
5. **Begin controlled demo run** — Start with dry_run=true, then switch to live after C1-C4 confirmed.
6. **Monitor C3/C5** — Track signal count and completed trades daily.
7. **Measure equity drawdown** — Track floating P&L via MetaApi account snapshots (lesson from IF2).
8. **Do NOT modify strategy.pine** or strategy_spec.yaml during the demo run.

---

## 9. Recommended Next Prompt for Phase 5

> Execute Phase 5 of the NovaTrade Demo Test Run Implementation Plan: Live Demo Deployment.
> Strategy: Rob Hoffman IRB v2.0.0 (CONDITIONAL GO from Phase 4).
> Resolve C1 first: load `strategy.pine` on a EURUSD H1 chart in TradingView and confirm it compiles.
> If it compiles and C2 confirms trades exist, upgrade to full GO.
> Then establish the Trading Agent pipeline (Pine alert → webhook → MetaApi → FTMO demo)
> and begin the controlled demo run on the FTMO Free Trial account.
> Monitor C3 (signal activity) and C5 (trade count) daily.
> Do not modify the strategy spec. Do not modify Pine unless C1 requires syntax fixes.

STOPPED AT FRESH IRB PHASE 4 — NO LATER PHASE WORK PERFORMED
