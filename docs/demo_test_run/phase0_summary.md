# NovaTrade Demo Test Run — Phase 0 Summary

**Phase:** 0 (Scope Freeze) — AMENDED
**Date:** 2026-03-16 (original) | **Amended:** 2026-03-17
**Status:** COMPLETE (amended — strategy changed from EMA Crossover to Rob Hoffman IRB)

---

## Amendment Notice

Phase 0 was originally completed on 2026-03-16 with EMA Crossover as the selected strategy. The operator directed a strategy change to the Rob Hoffman IRB on 2026-03-17. This summary reflects the amended state. The original Phase 0 decisions about infrastructure, account, duration, and governance remain valid.

---

## What Was Decided

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Symbol** | EURUSD (broker: EURUSD.sim) | Most liquid major pair, verified in preflight 13/13 PASS, tightest spreads, project default recommendation. IRB source materials include a worked EUR/USD H1 example. |
| **Timeframe** | H1 (primary) + H4 (MTF confirmation) | H1 balances signal frequency with operational simplicity; H4 added because IRB requires MTF trend alignment. Source materials include a worked H1 example. |
| **Strategy type** | Rob Hoffman IRB (approved) | Competition-proven, publicly documented, credible source, mechanically clear core rules, stronger institutional logic than generic EMA crossover |
| **Demo duration** | 10 calendar days (minimum 5 trading days) | Per project guidance; long enough for meaningful trade count, short enough for FTMO 14-day trial |
| **Account mode** | FTMO Free Trial / demo only | Frozen — no live capital, no funded account, no challenge mode |
| **Deployment mode** | Current verified stack frozen | MetaApi cloud, NovaTrade orchestration, FTMO demo — no infrastructure migration |
| **Essential agents** | 6 total: Strategy Spec, Pine Implementation, Compiler/Lint, Backtesting, Trading, Risk Management | Per project scope — no additional agents |
| **Success standard** | Systems correctness, not profitability | The demo run proves the pipeline works, not that the strategy makes money |

---

## What Was NOT Decided

| Item | Why Not | Who Decides | When |
|------|---------|-------------|------|
| Trend filter quantification (U1) | "45-degree slope" is visual, not mathematical — needs deterministic formula | Phase 1 | Strategy Spec Agent |
| ATR overextension threshold (U2) | Source gives direction but no hard k value | Phase 1 | Strategy Spec Agent |
| Trailing stop mechanics (U3) | Multiple variants possible; must choose one deterministic baseline | Phase 1 | Strategy Spec Agent |
| Sideways market detection (U4) | Source says "avoid sideways" but no detection method specified | Phase 1 | Strategy Spec Agent |
| Trigger window enforcement (U5) | "20 bars" stated as preference, not hard rule | Phase 1 | Strategy Spec Agent |
| Body-size filter inclusion (U6) | Community variant, not canonical — include or exclude? | Phase 1 | Strategy Spec Agent |
| Position sizing method (U7) | Risk-based vs fixed lots | Phase 1 | Strategy Spec Agent |
| Trade invalidation vs stop loss (U8) | Potentially redundant with stop loss rule — needs clarification | Phase 1 | Strategy Spec Agent |
| Pine implementation details | Requires strategy contract first | Phase 2 | Pine Implementation Agent |
| Backtest parameters and windows | Requires implemented strategy | Phase 3/4 | Backtesting Agent |
| Run start date | Depends on FTMO trial clock | Operator | Before trial expires (~2026-03-28) |

---

## Assumptions Made

| ID | Assumption | Risk Level |
|----|-----------|------------|
| A1 | EURUSD.sim spread acceptable for H1 and for stop-order entry | Low (verified in preflight) |
| A2 | MetaApi G1 tick rate (1/2.5s) sufficient for H1 bar-close signals and stop-order management | Low |
| A3 | Agents are pipeline stages in Python, not autonomous Claude sub-agents | Medium — needs operator confirmation |
| A4 | MetaApi supports stop orders (buy-stop, sell-stop) on the connected account | Low — standard MT5 order types |
| A5 | Weekend position holding acceptable | Low |
| A6 | MetaApi supports trailing stop modification (update SL on open position) | Low — standard position modification |
| A7 | H4 candle data accessible from the same MetaApi connection (for MTF check) | Low — standard MT5 timeframe |

---

## Blockers

| ID | Blocker | Impact |
|----|---------|--------|
| Q1 | ~~Operator must approve strategy type~~ | **RESOLVED** — IRB approved 2026-03-17 |
| Q2 | ~~FTMO trial expiration date~~ | **RESOLVED** — expires 2026-03-28, run must start by 2026-03-18 |

---

## Files Created / Modified

| File | Purpose | Status |
|------|---------|--------|
| `docs/demo_test_run/test_run_charter.md` | Mission, success definition, scope boundaries, exclusions | **AMENDED** for IRB |
| `docs/demo_test_run/chosen_strategy_symbol_timeframe.md` | Symbol, timeframe, strategy selection with rationale | **REWRITTEN** for IRB |
| `docs/demo_test_run/deployment_freeze_note.md` | Current stack, frozen components, forbidden changes | Unchanged |
| `docs/demo_test_run/success_criteria.md` | Operational, execution, risk, observability criteria + failure conditions | Unchanged (minor review recommended) |
| `docs/demo_test_run/phase0_open_questions.md` | Blockers, assumptions, deferred items | Unchanged |
| `docs/demo_test_run/phase0_summary.md` | This file | **AMENDED** for IRB |
| `docs/demo_test_run/irb_source_boundary.md` | IRB source boundary: adopted rules, unresolved items, exclusions | **NEW** |
| `docs/demo_test_run/phase_restart_assessment.md` | Phase restart recommendations after strategy change | **NEW** |

---

## Phase Restart Impact

The strategy change from EMA Crossover to IRB invalidates **all completed phases after Phase 0**:

- **Phase 1:** FULL RESTART — entire StrategySpec must be rebuilt for IRB
- **Phase 2:** FULL RESTART — entire Pine script must be rewritten for IRB
- **Phase 3:** FULL RESTART — validation must be redone on new Pine script
- **Phase 4:** FULL RESTART — backtester must be rebuilt for IRB logic

See `phase_restart_assessment.md` for detailed per-phase analysis.

---

## What Should Happen Next

1. **Phase 1 restart begins** — Strategy Spec Agent produces a complete StrategySpec contract for the approved IRB strategy on EURUSD H1 + H4 MTF, resolving all 8 unresolved items (U1-U8) from `irb_source_boundary.md`.
2. Phase 1 deliverable: a frozen, versioned, checksummed StrategySpec with source traceability tags on every rule.

---

## Recommended Next Prompt for Phase 1

> Execute Phase 1 of the NovaTrade Demo Test Run Implementation Plan: Strategy Specification.
> The operator has approved Rob Hoffman IRB as the strategy type, replacing EMA Crossover.
> Using the Phase 0 charter (amended), `irb_source_boundary.md`, and the two IRB source PDFs
> as ground truth, produce a complete StrategySpec contract for the Rob Hoffman IRB strategy
> on EURUSD (EURUSD.sim) H1 with H4 MTF confirmation.
> Resolve all 8 unresolved items (U1-U8) with explicit rationale.
> Carry source tags ([A1]-[A10], [U1]-[U8]) on every rule.
> Do not write Pine code. Do not implement agents. Stop at the completed StrategySpec.

---

**Phase 0 complete (amended) — all blockers resolved. Phases 1-4 require full restart. Ready for Phase 1 (IRB Strategy Specification).**
