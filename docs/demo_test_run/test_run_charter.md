# NovaTrade Demo Test Run — Charter

**Phase:** 0 (Scope Freeze) — AMENDED
**Date:** 2026-03-16 (original) | **Amended:** 2026-03-17
**Status:** LOCKED (amended — strategy changed from EMA Crossover to Rob Hoffman IRB)

---

## Amendment Notice

This charter was originally locked on 2026-03-16 with EMA Crossover as the selected strategy. The operator has directed a strategy change to the Rob Hoffman Inventory Retracement Bar (IRB). The mission, governance, and infrastructure scope are unchanged. Strategy-dependent scope boundaries have been updated below.

---

## Mission

Prove that NovaTrade can execute one strategy on one symbol through the full
verified pipeline — from strategy contract to MT5 demo order placement — under
continuous observation, risk governance, and evidence logging, on the FTMO Free
Trial demo account.

This is a **systems test**, not a profit test. The demo run exists to validate
that the execution stack, risk gate, monitoring, reconciliation, and evidence
pipeline work correctly under live market conditions for a sustained period.

---

## What Success Means

1. The strategy contract is fully specified before any execution begins.
2. The Trading Agent executes only what the contract permits — no interpretation.
3. The 13-check risk gate blocks every non-compliant order.
4. Every trade action is logged with full evidence and campaign tags.
5. Position reconciliation detects and flags any mismatch.
6. The system runs for the target duration without unrecoverable failure.
7. A go/no-go verdict can be rendered from evidence alone.

## What Success Does NOT Mean

- Profit. This run is not optimized for or evaluated on P&L.
- Strategy quality. The chosen strategy may lose money — that is not a failure.
- Production readiness. Passing this run does not authorize live capital deployment.
- Infrastructure finality. MetaApi remains the MVP bridge; KVM migration is deferred.
- Strategy generalization. One strategy on one symbol proves the pipeline, not the edge.

---

## Exact Scope Boundaries

### In Scope

- One strategy (Rob Hoffman IRB), fully specified in a StrategySpec contract
- One symbol: EURUSD (broker symbol: EURUSD.sim)
- One primary timeframe: H1
- One confirmation timeframe: H4 (for multi-timeframe trend alignment — required by IRB strategy)
- One account: FTMO Free Trial, $100k demo, OANDA-Demo-1
- One execution bridge: MetaApi cloud (account 4c121f03-836f-4fb1-8799-736e53699a66)
- 6 essential agents: Strategy Spec, Pine Implementation, Compiler/Lint, Backtesting, Trading, Risk Management
- Existing NovaTrade risk gate (13 pre-trade checks)
- Existing evidence pipeline with campaign tagging
- Existing position reconciliation and health monitoring
- Duration: 10 calendar days (minimum 5 trading days)
- Stop order management (buy-stop / sell-stop) — required by IRB entry mechanism
- Active trailing stop management — required by IRB exit mechanism

### Out of Scope (Explicit Exclusions)

- Live capital of any kind
- Funded account operation
- Multiple strategies
- Multiple symbols (GBPUSD and USDJPY are configured but not traded in this run)
- Additional timeframes beyond H1 (primary) and H4 (MTF confirmation)
- KVM/mt5-httpapi infrastructure migration
- TradingView shadow validation (Path 1)
- Self-improvement or learning during the run
- Strategy optimization or parameter tuning during the run
- Agents beyond the 6 essential agents listed above
- New risk gate logic (existing 13 checks are frozen)
- MetaApi tier upgrade or infrastructure scaling
- Telegram bot integration for trade signals (alerts-only is acceptable)
- Any modification to execution code during the run period
- Hoffman's proprietary indicators (ITP, Champion Cross, Breakout Forecasters, IRB Trackers)
- Reverse IRBs or advanced IRB variants beyond the core public setup
- Discretionary S/R identification (must use deterministic proxies)

---

## Changes from Original Charter (EMA → IRB)

| Aspect | Original (EMA) | Amended (IRB) |
|--------|---------------|---------------|
| Strategy | EMA Crossover 9/21 | Rob Hoffman IRB |
| Entry type | Market order at bar close | Stop order (buy-stop/sell-stop) beyond IRB extreme |
| Stop loss | Fixed 50 pips | Dynamic — opposite side of IRB candle +/- 1 pip |
| Take profit | Fixed 75 pips | Trailing stop (no fixed TP) |
| MTF scope | Excluded | H4 trend confirmation brought into scope (required by IRB) |
| Order management | Set-and-forget | Active pending order and trailing stop management |
| State complexity | 3 states (FLAT/LONG/SHORT) | 5+ states (FLAT/PENDING_LONG/PENDING_SHORT/LONG/SHORT) |
| Signal detection | EMA crossover | Candle geometry (45% rule) + trend filter + MTF alignment |

---

## Governance

- The Risk Governor outranks execution at all times.
- The Trading Agent must not learn while trading (WP3 non-negotiable rule).
- No silent decisions — every trade action must produce evidence.
- The strategy contract is frozen before the run starts and cannot be modified during the run.
- If the risk gate denies a trade, that denial stands. No override mechanism exists.
- If a blocker emerges during the run, the run halts. It does not self-heal.
