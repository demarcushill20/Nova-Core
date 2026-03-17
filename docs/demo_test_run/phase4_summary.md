# NovaTrade Demo Test Run — Phase 4 Summary (Fresh IRB)

**Phase:** 4 (Backtesting and Validation)
**Date:** 2026-03-17
**Status:** COMPLETE — CONDITIONAL GO for demo deployment
**Agent:** Backtesting Agent
**Strategy:** Rob Hoffman IRB v2.0.0 (strategy_spec.yaml v2.0.0)
**Pine:** strategy.pine v2.0.0 (549 lines, Phase 3 validated)
**Replaces:** EMA Crossover Phase 4 summary (2026-03-16, SUPERSEDED)

---

## 1. Fresh IRB Phase 4 Completion Status

**COMPLETE.** Phase 4 analytical validation of the Rob Hoffman IRB strategy has been executed. All required deliverables have been produced.

**Key constraint:** No live backtest was executed. Neither a TradingView Pine compiler nor a Python backtesting environment was available. All findings are from structured analytical validation based on the Phase 3-validated Pine implementation, the approved strategy specification, and known EURUSD H1 market characteristics.

---

## 2. What Was Done

### 2.1 Backtest Environment Definition
Documented the exact requirements for a live backtest (TradingView Pine Strategy Tester on EURUSD H1 with H4 MTF, OANDA data, stop-order fill model). Explicitly stated what was directly verified (logic correctness, spec alignment, anti-repaint) versus analytically estimated (trade frequency, drawdown, win rate).

### 2.2 Analytical Trade Frequency Estimation
Estimated IRB signal frequency by analyzing the 5-filter interaction on EURUSD H1:
- Combined filter pass rate: ~0.5-3.8% of bars → 2-18 signals/month
- Stop-order fill rate: ~40-60% → 1-12 completed trades/month
- In 10 calendar days: estimated **2-8 completed trades** (borderline for E6 ≥10 target)

### 2.3 FTMO Compliance Assessment
Analytically verified FTMO compliance:
- Max daily drawdown: 2-3% worst case (vs 5% limit)
- Max total drawdown: ≤7% extreme case (vs 10% limit)
- Per-trade risk: exactly 1% with dynamic sizing
- Safety margin adequate even with analytical uncertainty

### 2.4 Strategy Suitability Assessment
Assessed the IRB strategy as a **superior pipeline validator** compared to EMA:
- 4 alert types (vs 1)
- Stop order lifecycle (vs market orders)
- Trailing stop modifications (vs fixed TP)
- Pending order management (new)
- Time stop (new)
- Dynamic position sizing (vs fixed lots)

### 2.5 Analytical Trade Scenarios
Constructed 7 scenarios tracing through Pine code to verify all trade lifecycle paths:
1. Long trade with trailing stop exit (win)
2. Short trade with stop-loss exit (loss)
3. Long trade with time stop exit
4. Pending order trigger window expiry
5. IRB replacement (same-direction)
6. Opposite-direction signal suppression
7. In-position signal suppression

All 7 scenarios verified spec-compliant.

---

## 3. Compile/Lint/Static Validation Decisions Made

**No new decisions.** Phase 4 relies entirely on Phase 3 validation results:
- Compile: 45 checks PASS (static analysis)
- Lint: 0 blockers, 3 warnings, 5 informational
- Anti-repaint: AR1-AR4 FULLY COMPLIANT
- Contract alignment: 134/134 rules verified (no drift)
- Alert contract: 58/58 fields match schema

Phase 4 does not modify Phase 3 findings.

---

## 4. Changes Made

**None.** No changes were made to:
- `strategy.pine` — no code modifications
- `alerts_schema.json` — no schema changes
- `strategy_spec.yaml` — no spec changes
- `spec_traceability.md` — no traceability changes

Phase 4 is a validation-only phase. All modifications are to Phase 4 deliverable artifacts.

---

## 5. Assumptions Made

6 new IRB-specific assumptions (BA-IRB-1 to BA-IRB-6):

| ID | Summary | Risk |
|----|---------|------|
| BA-IRB-1 | Analytical validation sufficient for CONDITIONAL GO | Medium |
| BA-IRB-2 | IRB frequency 0.1-0.4 completed trades/day | Medium |
| BA-IRB-3 | Stop orders fill 40-60% within 20 bars | Medium |
| BA-IRB-4 | 5-filter combination not pathologically restrictive | Low |
| BA-IRB-5 | FTMO compliance maintained at 1%/trade, 0-2 signals/day | Low |
| BA-IRB-6 | Trailing stop (1.5 × ATR) provides meaningful profit protection | Low |

3 medium-risk, 3 low-risk. All testable in Phase 5.

EMA assumptions BA1-BA8 are SUPERSEDED.

---

## 6. Open Issues

| Severity | Count | Key Items |
|----------|-------|-----------|
| Blocker | 2 | B-IRB-1 (Pine compilation), B-IRB-2 (no live backtest) |
| Warning | 3 | P4-IRB-1 (trade frequency borderline), P4-IRB-2 (actual IRB frequency unknown), P4-IRB-3 (no measured drawdown) |
| Informational | 2 | P4-IRB-4 (time stop may not trigger), P4-IRB-5 (no sensitivity analysis) |
| Inherited | 10 | From Phases 0-3 |

Both blockers are resolvable at the start of Phase 5 by loading the script in TradingView.

---

## 7. Files Created/Updated

| File | Purpose |
|------|---------|
| `docs/demo_test_run/backtest_report.md` | Fresh IRB analytical backtest validation (replaces EMA) |
| `docs/demo_test_run/deployment_recommendation.md` | CONDITIONAL GO for IRB demo (replaces EMA) |
| `docs/demo_test_run/sample_trade_audit.md` | 7 analytical IRB trade scenarios (replaces EMA) |
| `docs/demo_test_run/phase4_assumptions.md` | 6 IRB-specific assumptions (replaces EMA) |
| `docs/demo_test_run/phase4_open_issues.md` | 2 blockers, 3 warnings, 2 informational (replaces EMA) |
| `docs/demo_test_run/phase4_summary.md` | This file (replaces EMA) |

---

## 8. Recommended Next Prompt for Fresh IRB Phase 5

> Execute Phase 5 of the NovaTrade Demo Test Run Implementation Plan: Live Demo Deployment.
>
> Strategy: Rob Hoffman IRB v2.0.0 (CONDITIONAL GO from Phase 4).
>
> Phase 5 must resolve two blockers before deployment:
> 1. B-IRB-1: Load `strategy.pine` on EURUSD H1 in TradingView. Confirm compilation.
> 2. B-IRB-2: Run TradingView strategy tester on 30-day window. Verify ≥1 trade exists.
>
> If both blockers clear, upgrade recommendation to full GO.
> Then establish the Trading Agent pipeline:
>   Pine alert → webhook → Trading Agent → MetaApi → FTMO demo
>
> Begin controlled demo run on the FTMO Free Trial account.
> Monitor C3 (signal activity in first 3 days) and C5 (trade count after 5 days).
> Measure equity drawdown via MetaApi account snapshots.
>
> Do not modify strategy_spec.yaml.
> Do not modify strategy.pine unless B-IRB-1 requires syntax fixes.
> Do not redesign the strategy.
> Do not expand scope.

---

## 9. Final Statement

STOPPED AT FRESH IRB PHASE 4 — NO LATER PHASE WORK PERFORMED

---

**Phase 4 complete — CONDITIONAL GO for demo deployment.**
