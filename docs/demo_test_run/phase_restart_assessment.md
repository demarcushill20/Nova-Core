# NovaTrade Demo Test Run — Phase Restart Assessment

**Date:** 2026-03-17
**Trigger:** Strategy baseline change from EMA Crossover 9/21 to Rob Hoffman IRB
**Purpose:** Explicit restart recommendation for all completed phases

---

## Summary Verdict

| Phase | Status Before Change | Restart Recommendation | Severity |
|-------|---------------------|----------------------|----------|
| **Phase 0** (Scope Freeze) | COMPLETE | **PARTIAL RESTART** — amend 3 documents | Moderate |
| **Phase 1** (Strategy Specification) | COMPLETE | **FULL RESTART** — all deliverables invalidated | Critical |
| **Phase 2** (Pine Implementation) | COMPLETE | **FULL RESTART** — entire script invalidated | Critical |
| **Phase 3** (Compile/Lint/Validation) | COMPLETE | **FULL RESTART** — validates Phase 2 output | Critical |
| **Phase 4** (Backtesting) | COMPLETE | **FULL RESTART** — backtester implements EMA logic | Critical |

---

## Phase 0: PARTIAL RESTART

### What Is Invalidated

The strategy selection and its downstream scope consequences.

| Document | Status | Action Required |
|----------|--------|----------------|
| `test_run_charter.md` | **INVALIDATED** | Amended — strategy reference, entry type, MTF scope updated |
| `chosen_strategy_symbol_timeframe.md` | **INVALIDATED** | Rewritten — IRB selection with full rationale |
| `phase0_summary.md` | **INVALIDATED** | Amended — strategy decision, what's-not-decided, next steps updated |
| `deployment_freeze_note.md` | **VALID** | No change needed — infrastructure scope is strategy-independent |
| `success_criteria.md` | **MOSTLY VALID** | Minor review recommended: E3 (SL on every position) still valid but SL is now dynamic not fixed; E6 (min trade count) may need reassessment for IRB signal frequency |
| `phase0_open_questions.md` | **VALID** | Blockers Q1/Q2 are resolved; new blockers not required at Phase 0 level |

### What Remains Valid

- Mission statement (systems test, not profit test)
- Symbol: EURUSD (EURUSD.sim) — unchanged with additional IRB-specific justification
- Timeframe: H1 — unchanged with MTF (H4) addition
- Account: FTMO Free Trial, $100k demo
- Duration: 10 calendar days
- Infrastructure: MetaApi cloud, frozen stack
- Governance: Risk Governor outranks execution, no learning during trading
- Success criteria structure (operational, execution, risk, observability)

### Why Not a Full Restart

Phase 0's primary function is to freeze scope, infrastructure, and governance. These are strategy-independent. Only the strategy selection subsection needs amendment. The infrastructure verification, account setup, risk gate verification, and preflight results are all still valid.

---

## Phase 1: FULL RESTART

### What Is Invalidated

**Everything.** The Phase 1 strategy specification was built entirely for EMA Crossover 9/21. Every parameter, rule, test vector, and validation check is EMA-specific.

| Document | Status | Why Invalidated |
|----------|--------|----------------|
| `strategy_spec.yaml` | **FULLY INVALIDATED** | Defines EMA crossover signal rules, fixed 50-pip SL, fixed 75-pip TP, market orders, 9/21 EMA parameters |
| `strategy_test_vectors.yaml` | **FULLY INVALIDATED** | All 16 test vectors are for EMA crossover scenarios |
| `strategy_validation_checklist.md` | **FULLY INVALIDATED** | Validates EMA-specific completeness and determinism |
| `phase1_assumptions.md` | **PARTIALLY INVALIDATED** | Inherited assumptions A1-A5 may still apply; EMA-specific assumptions A6-A13 are invalid |
| `phase1_open_issues.md` | **PARTIALLY INVALIDATED** | Some inherited issues carry forward; EMA-specific issues are moot |
| `phase1_summary.md` | **FULLY INVALIDATED** | Summarizes EMA crossover specification |

### What the New Phase 1 Must Produce

1. A complete StrategySpec contract for the IRB strategy on EURUSD H1, resolving all 8 unresolved items (U1-U8) from `irb_source_boundary.md`
2. New test vectors covering IRB-specific scenarios:
   - IRB geometry detection (uptrend and downtrend)
   - Stop order placement and trigger
   - IRB replacement behavior
   - Trailing stop mechanics
   - MTF alignment check
   - ATR overextension filter
   - Sideways market rejection
   - Trigger window expiration
3. Validation checklist aligned to IRB rules
4. Source traceability tags ([A1]-[A10], [U1]-[U8]) on every rule

### Action on Phase 1 Artifacts

**Do NOT delete the existing Phase 1 files.** They are part of the project's audit trail. The new Phase 1 should:
- Create new files with the same names (overwriting the EMA versions)
- Note in each file that it replaces an earlier EMA Crossover version
- Preserve the same structural conventions (YAML for spec, YAML for test vectors, MD for summaries)

---

## Phase 2: FULL RESTART

### What Is Invalidated

**Everything.** The Pine script (`strategy.pine`) implements EMA Crossover 9/21 logic. The IRB strategy requires fundamentally different:

| Component | EMA (current Pine) | IRB (required) |
|-----------|-------------------|---------------|
| Signal detection | `ta.crossover(ema_fast, ema_slow)` | Candle geometry: O and C vs 45% threshold from extreme |
| Trend filter | None (crossover IS the signal) | EMA(20) slope check + MTF EMA(20) alignment |
| Entry mechanism | `strategy.entry()` market order at bar close | `strategy.entry()` with `stop=` parameter (pending stop order) |
| Stop loss | Fixed 50-pip offset from entry | Dynamic: opposite side of IRB candle +/- 1 pip |
| Take profit | Fixed 75-pip offset from entry | No fixed TP — trailing stop only |
| Position management | Set-and-forget (SL/TP placed at entry) | Active trail: update stop on every bar based on open profit |
| State machine | FLAT → LONG/SHORT (3 states) | FLAT → PENDING → ACTIVE → trailing (5+ states) |
| ATR computation | Not used | Required for overextension filter |
| Higher TF data | Not used | Required for MTF trend alignment |
| Pending order management | Not used | IRB replacement, trigger window, order cancellation |

| Document | Status |
|----------|--------|
| `strategy.pine` | **FULLY INVALIDATED** — must be rewritten from scratch |
| `alerts_schema.json` | **MOSTLY INVALIDATED** — alert structure changes (stop orders, dynamic SL, no fixed TP) |
| `spec_traceability.md` | **FULLY INVALIDATED** — maps EMA rules to EMA code |
| `phase2_assumptions.md` | **PARTIALLY INVALIDATED** — Pine-general assumptions may survive; EMA-specific ones are moot |
| `phase2_open_issues.md` | **PARTIALLY INVALIDATED** — some Pine-general issues carry forward |
| `phase2_summary.md` | **FULLY INVALIDATED** |

### New Pine Implementation Complexity

The IRB Pine script will be significantly more complex than the EMA version:
- ~200 lines (EMA) → estimated ~400-500 lines (IRB)
- Requires `request.security()` for H4 MTF data
- Requires stop-order entry logic with pending order tracking
- Requires bar-by-bar trailing stop updates
- Requires ATR computation for overextension filter
- Requires IRB replacement logic (cancel old pending, place new)
- Requires trigger window countdown

---

## Phase 3: FULL RESTART

### What Is Invalidated

**Everything.** Phase 3 validated the EMA Crossover Pine script against the EMA strategy spec. Both inputs change.

| Document | Status |
|----------|--------|
| `compile_report.md` | **FULLY INVALIDATED** — validates EMA Pine syntax |
| `lint_report.md` | **FULLY INVALIDATED** — reviews EMA code structure |
| `anti_repaint_review.md` | **PARTIALLY INVALIDATED** — anti-repaint principles carry forward, but the review must be redone for IRB Pine (especially with `request.security()` for MTF) |
| `phase3_contract_alignment_review.md` | **FULLY INVALIDATED** — maps EMA rules to EMA code |
| `phase3_assumptions.md` | **PARTIALLY INVALIDATED** |
| `phase3_open_issues.md` | **PARTIALLY INVALIDATED** |
| `phase3_summary.md` | **FULLY INVALIDATED** |

### New Phase 3 Concerns for IRB

- `request.security()` introduces potential repainting risk if not handled correctly (lookahead settings)
- Stop order logic in Pine has specific behavioral characteristics that must be validated
- Trailing stop implementation must be checked for intra-bar vs bar-close evaluation
- IRB replacement logic must be verified for state consistency

---

## Phase 4: FULL RESTART

### What Is Invalidated

**Everything.** The Python backtester (`phase4_backtester.py`) implements EMA Crossover logic.

| Document | Status |
|----------|--------|
| `phase4_backtester.py` | **FULLY INVALIDATED** — implements EMA crossover, not IRB |
| `phase4_backtest_data.json` | **FULLY INVALIDATED** — EMA backtest results |
| `backtest_report.md` | **FULLY INVALIDATED** — EMA backtest analysis |
| `deployment_recommendation.md` | **FULLY INVALIDATED** — based on EMA backtest |
| `sample_trade_audit.md` | **FULLY INVALIDATED** — audits EMA trades |
| `phase4_assumptions.md` | **PARTIALLY INVALIDATED** — data source assumptions may carry forward |
| `phase4_open_issues.md` | **PARTIALLY INVALIDATED** — some issues are structural |
| `phase4_summary.md` | **FULLY INVALIDATED** |

### New Phase 4 Backtester Requirements

The IRB backtester will need:
- IRB candle geometry detection (45% rule)
- EMA(20) trend filter with quantified slope (from U1)
- H4 MTF trend alignment check
- Stop-order simulation (entry only if price breaks IRB extreme within N bars)
- Dynamic stop loss (opposite side of IRB)
- Trailing stop simulation (chosen variant from U3)
- ATR overextension filter
- Sideways market filter
- IRB replacement logic
- Position sizing (risk-based if U7 chooses that path)

This is significantly more complex than the EMA backtester but follows the same structural pattern.

---

## Recommended Execution Order

1. **Phase 0 amendment** — DONE (this task)
2. **Phase 1 restart** — Fresh IRB StrategySpec, resolving U1-U8
3. **Phase 2 restart** — Fresh IRB Pine implementation
4. **Phase 3 restart** — Fresh validation of IRB Pine
5. **Phase 4 restart** — Fresh IRB backtest

Each phase depends on the previous one. No phase can be skipped or parallelized.

---

## Recommended Next Prompt (for Phase 1 restart)

> Execute Phase 1 of the NovaTrade Demo Test Run Implementation Plan: Strategy Specification.
> The operator has approved Rob Hoffman IRB as the strategy type, replacing EMA Crossover.
> Using the Phase 0 charter (amended), `irb_source_boundary.md`, and the two IRB source PDFs
> (`OUTPUT/rob_hoffman_irb_strategy.pdf`, `OUTPUT/rob_hoffman_irb_forex_full.pdf`) as ground truth,
> produce a complete StrategySpec contract for the Rob Hoffman IRB strategy on EURUSD (EURUSD.sim) H1
> with H4 MTF confirmation.
>
> The spec must:
> 1. Resolve all 8 unresolved items (U1-U8) from `irb_source_boundary.md` with explicit rationale
> 2. Implement all 10 adopted rules (A1-A10) with exact source traceability
> 3. Carry source tags ([A1]-[A10], [U1]-[U8]) on every rule
> 4. Include IRB-specific test vectors covering: geometry detection, stop order placement,
>    IRB replacement, trailing stop, MTF alignment, ATR filter, sideways rejection, trigger window
> 5. Satisfy WP2 §5.1 (Strategy Contract) with: metadata, signal rules, risk rules,
>    execution rules, state rules, telemetry requirements
>
> Do not write Pine code. Do not implement agents. Do not modify execution logic.
> Stop at the completed StrategySpec.
