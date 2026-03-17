# NovaTrade Demo Test Run — Phase 4 Code Review

**Phase:** 4 (Backtesting and Validation) — Code Review Step
**Date:** 2026-03-17
**Reviewer:** Backtesting Agent (code review role)
**Scope:** Quality, security, and correctness of all Phase 4 artifacts
**Strategy:** Rob Hoffman IRB v2.0.0

---

## 1. Files Reviewed

| File | Lines | Purpose | Verdict |
|------|-------|---------|---------|
| `strategy.pine` | 549 | IRB Pine implementation | **PASS** |
| `phase4_summary.md` | 164 | Phase 4 summary | **PASS** |
| `phase4_assumptions.md` | 57 | IRB-specific assumptions | **PASS** |
| `phase4_open_issues.md` | 91 | Open issues tracker | **PASS** |
| `backtest_report.md` | 446 | Analytical backtest validation | **PASS** |
| `deployment_recommendation.md` | 178 | CONDITIONAL GO recommendation | **PASS** |
| `sample_trade_audit.md` | 263 | 7 analytical trade scenarios | **PASS** |
| `strategy_validation_checklist.md` | 177 | Phase 1 validation checklist | **PASS** |
| `spec_traceability.md` | 80+ | Spec-to-code mapping | **PASS** |
| `phase3_contract_alignment_review.md` | 80+ | Phase 3 alignment review | **PASS** |
| `strategy_spec.yaml` | 896 | Strategy specification contract | **PASS** |
| `phase4_backtester.py` | 850 | Python backtester | **FAIL — EMA artifact** |
| `phase4_backtest_data.json` | ~57KB | Backtest output data | **FAIL — EMA artifact** |

---

## 2. Findings

### FINDING-1: CRITICAL — `phase4_backtester.py` implements EMA, not IRB

**Severity:** CRITICAL (quality/correctness)
**File:** `docs/demo_test_run/phase4_backtester.py`
**Evidence:**
- Line 2: `"""NovaTrade Phase 4 — EMA 9/21 Crossover Backtester."""`
- Lines 38-39: `EMA_FAST_PERIOD = 9`, `EMA_SLOW_PERIOD = 21`
- Lines 41-44: `SL_PIPS = 50`, `TP_PIPS = 75` (fixed SL/TP, not IRB dynamic SL)
- Lines 400-401: Uses `ema_fast[i] > ema_slow[i]` crossover logic
- Contains zero IRB geometry detection, zero 5-filter chain, zero stop-order logic, zero trailing stop logic

**Impact:** This file is a stale artifact from the superseded EMA Crossover strategy. If executed, it would produce EMA-based results that are **irrelevant to the current IRB strategy**. Anyone running this backtester would get misleading results.

**Recommendation:** Either:
1. Delete the file entirely (preferred — it serves no purpose for IRB)
2. Rename to `phase4_backtester_EMA_SUPERSEDED.py` with a prominent header noting it is not the active strategy
3. Replace with an IRB backtester (deferred — TradingView is the authoritative backtest engine)

### FINDING-2: WARNING — `phase4_backtest_data.json` contains EMA data

**Severity:** WARNING (data integrity)
**File:** `docs/demo_test_run/phase4_backtest_data.json`
**Evidence:**
- Line 4: `"date": "2026-03-16"` (pre-IRB date)
- Line 19-20: Test vectors reference "EMA multiplier constants" and `k_fast=0.2000, k_slow=0.090909` (EMA 9/21 multipliers)
- Lines 8-9: SL/TP placement vectors use fixed 50/75 pip values

**Impact:** This JSON was produced by the EMA backtester and contains no IRB-relevant data. Could mislead any automated pipeline that reads this file.

**Recommendation:** Delete or rename with `_EMA_SUPERSEDED` suffix.

### FINDING-3: PASS — `strategy.pine` is correct and well-structured

**Assessment:** No quality, security, or correctness issues found.

Verified:
- All 6 constants sections match `strategy_spec.yaml` v2.0.0 exactly
- IRB geometry detection (L131-140): correct 45% threshold formula, both directions
- Trend filter (L147-151): ATR-normalized slope with div-by-zero guard
- MTF alignment (L162-163): H4 EMA via `request.security` with `lookahead=off` (AR4 compliant)
- Sideways filter (L169): ADX >= 20
- Overextension filter (L175-176): bar_rng/atr <= 2.0 with div-by-zero guard
- Combined signal (L182-183): AND chain of all 6 conditions including warmup
- State machine (L194-256): 5 states, all transitions verified, no deadlocks
- Position sizing (L218-224): risk-based, clamped [0.01, 1.00], rounded to 0.01
- Pending order management (L283-354): trigger window, replacement, suppression all correct
- Exit management (L370-407): trailing stop tighten-only, time stop at 40 bars
- Alert payloads (L433-527): 4 alert types, all fields present, valid JSON construction
- Anti-repaint: `calc_on_every_tick=false`, `process_orders_on_close=false`, `lookahead=off`

**Minor observation:** Position sizing formula is duplicated between `f_qty()` (L218-224) and the alert payload section (L440-443). Both compute volume identically, but this creates a maintenance risk if one is updated without the other. Acceptable for frozen demo but should be refactored post-demo.

### FINDING-4: PASS — Analytical validation documents are internally consistent

All Phase 4 analytical documents correctly:
- Reference IRB strategy (not EMA) throughout
- Mark EMA artifacts as "SUPERSEDED" or "MOOT"
- Inherit Phase 3 validation results without modification
- Identify appropriate blockers (B-IRB-1, B-IRB-2) and conditions (C1-C5)
- Apply correct CONDITIONAL (not full) GO based on analytical-only evidence
- Maintain traceability to spec sections with [A] and [U] tags

### FINDING-5: PASS — Security review clean

| Check | Result |
|-------|--------|
| No secrets or API keys in code | PASS — MetaApi account ID is demo-only |
| No command injection vectors | PASS — Pine script is declarative |
| No SQL injection | PASS — N/A |
| No XSS vectors | PASS — N/A |
| No unsafe network calls | PASS — EMA backtester uses yfinance but is superseded |
| Alert JSON construction | PASS — values are all computed from price/indicator data |
| File path safety | PASS — output path is hardcoded, no user input |

### FINDING-6: PASS — Doctrine compliance verified

| Doctrine Rule | Status |
|---------------|--------|
| One strategy only (IRB) | PASS — all active artifacts reference IRB only |
| Spec before code | PASS — spec v2.0.0 preceded Pine v2.0.0 |
| No silent decisions | PASS — all assumptions documented with rationale |
| Governance outranks execution | PASS — CONDITIONAL GO with explicit blockers |
| Demo run is a systems test, not profit test | PASS — `expected_profitability: "not_a_goal"` |
| Contract frozen during run | PASS — no modifications to spec or Pine |
| Trading agent must not learn | PASS — all parameters are fixed constants |

### FINDING-7: PASS — Sample trade scenarios are correctly constructed

All 7 analytical scenarios in `sample_trade_audit.md`:
- Use correct IRB geometry formulas
- Apply all 5 filters correctly
- Trace through actual Pine code line numbers
- Produce expected state transitions
- Generate correct alert payloads
- Cover all 4 alert types and all 5 state machine states
- Coverage matrix accounts for all major lifecycle paths

---

## 3. Review Summary

| Category | Finding Count | Severity |
|----------|--------------|----------|
| CRITICAL | 1 | FINDING-1: EMA backtester still present |
| WARNING | 1 | FINDING-2: EMA backtest data still present |
| PASS | 5 | FINDING-3 through FINDING-7 |

### Overall Verdict: **PASS with 2 cleanup items**

The IRB Phase 4 analytical validation is **correct, complete, and internally consistent**. The strategy implementation (`strategy.pine`) is verified correct against the spec. The deployment recommendation (CONDITIONAL GO) is appropriately conservative.

The two stale EMA artifacts (`phase4_backtester.py` and `phase4_backtest_data.json`) should be cleaned up to prevent confusion, but they do not affect the validity of the IRB Phase 4 conclusions.

---

## 4. Recommended Actions

| Priority | Action | Rationale |
|----------|--------|-----------|
| HIGH | Rename `phase4_backtester.py` → `phase4_backtester_EMA_SUPERSEDED.py` | Prevent confusion with active IRB strategy |
| HIGH | Rename `phase4_backtest_data.json` → `phase4_backtest_data_EMA_SUPERSEDED.json` | Prevent stale EMA data from being used |
| LOW | Post-demo: refactor duplicated volume calculation in `strategy.pine` (L218-224 vs L440-443) | Reduce maintenance risk |
| NONE | No changes to `strategy.pine`, `strategy_spec.yaml`, or Phase 4 analytical artifacts | All are correct and frozen |

---

**Reviewed by: Backtesting Agent (code review role)**
**Date: 2026-03-17**
**Files reviewed: 13**
**Lines reviewed: ~3,500+**
