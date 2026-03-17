# NovaTrade Demo Test Run — Phase 0 Open Questions

**Phase:** 0 (Scope Freeze)
**Date:** 2026-03-16
**Status:** LOCKED

---

## Blocker

Items that MUST be resolved before Phase 1 can begin.

### Q1: Operator approval of test strategy

**Status:** RESOLVED (2026-03-16, amended 2026-03-17)
**Resolution:** Operator initially approved EMA Crossover (2026-03-16). Operator subsequently directed strategy change to Rob Hoffman IRB (2026-03-17). IRB is now the approved baseline. Phase 1 requires full restart with IRB strategy specification.

### Q2: FTMO Free Trial expiration timeline

**Status:** RESOLVED (2026-03-16)
**Resolution:** Operator confirmed trial expires 2026-03-28. Run must START by 2026-03-18 to complete 10 calendar days within trial window.

---

## Non-Blocking Assumptions

Items where a reasonable assumption has been made. Can be corrected without halting.

### A1: EURUSD.sim spread is acceptable for H1 strategy

**Assumption:** EURUSD.sim spread on FTMO demo (~0.3 pips observed in preflight) is tight enough that spread does not dominate trade outcomes on H1 timeframe.
**Basis:** EURUSD is the tightest-spread major pair. H1 bar ranges typically 10-50 pips. Spread is <1% of bar range.
**Risk if wrong:** Low. Even 3-pip spread would not invalidate a systems test.

### A2: MetaApi quote streaming is sufficient for H1 bar-close signals

**Assumption:** MetaApi's G1 tier quote streaming (1 tick per 2.5 seconds) is adequate for a strategy that only acts on H1 bar close.
**Basis:** Architecture Decision Report §D confirms "G1 tier quote streaming limited to 1 tick per 2.5 seconds." For H1 signals, the bar-close price is what matters — 2.5-second granularity is more than sufficient.
**Risk if wrong:** Negligible for H1. Would only matter for sub-minute strategies.

### A3: The 6 essential agents will be implemented as NovaTrade pipeline stages, not as separate autonomous processes

**Assumption:** For this demo run, "agents" means pipeline stages within NovaTrade's Python codebase — not separate Claude Code sub-agents or autonomous processes. The Trading Agent is the execution loop; the Risk Management Agent is the pre-trade gate; the Strategy Spec Agent is the specification step; etc.
**Basis:** The existing NovaTrade codebase already implements several of these as modules (risk gate, executor, validator). The demo run should use and extend existing code, not build a new multi-agent orchestration layer.
**Risk if wrong:** Medium. If the operator intends fully autonomous Claude-powered agents, that is a significantly larger scope and would require re-evaluation.

### A4: ~~Market orders only for the first demo run~~ SUPERSEDED

**Status:** SUPERSEDED (2026-03-17)
**Original assumption:** EMA crossover strategy would use market orders at bar close.
**Superseded by:** IRB strategy requires stop orders (buy-stop / sell-stop) as the core entry mechanism. Stop order support is now IN SCOPE. See amended charter and `chosen_strategy_symbol_timeframe.md` §3 for details.
**MetaApi impact:** MetaApi supports stop orders via standard MT5 order types — no infrastructure change required.

### A5: Weekend positions are acceptable

**Assumption:** The strategy may hold positions over the weekend if the signal has not reversed by Friday close.
**Basis:** FTMO allows overnight and weekend holding. The risk gate checks position count and drawdown, not hold duration.
**Risk if wrong:** Low. A weekend gap could cause a larger-than-expected loss, but the risk gate's drawdown limits would still apply.

---

## Deferred to Later Phase

Items that are explicitly out of scope for Phase 0 and the demo test run.

### D1: TradingView shadow validation (Path 1)

**Deferred to:** Post-demo-run evaluation
**Reason:** WP2 §4.2 defines Path 1 as a validation layer, not the primary execution path. Adding TradingView signal comparison doubles the implementation scope. The demo run proves Path 2 first.

### D2: KVM/mt5-httpapi migration

**Deferred to:** After demo run completes successfully
**Reason:** Architecture Decision Report explicitly prescribes MetaApi for MVP, self-hosted for Month 2+.

### D3: Multi-symbol trading

**Deferred to:** Second demo run (if first succeeds)
**Reason:** One symbol isolates execution variables. Multi-symbol adds correlation risk, symbol mapping complexity, and position management overhead.

### D4: Strategy optimization and parameter tuning

**Deferred to:** Post-demo-run, offline only
**Reason:** WP3 non-negotiable rule: "The trading agent must not learn while trading." Optimization occurs offline with evidence from the demo run.

### D5: Monte Carlo / walk-forward / overfit detection

**Deferred to:** Phase 3 (Backtesting Agent implementation)
**Reason:** These are validation agents defined in WP2 §2.3. They require the backtest infrastructure to be built first.

### D6: Telegram bot trade signal integration

**Deferred to:** Post-demo-run
**Reason:** Telegram alerts for errors and health are acceptable. Telegram as a signal delivery or command channel is out of scope.

### D7: News event handling

**Deferred to:** Post-demo-run strategy refinement
**Reason:** FTMO restricts news trading on funded accounts but permits it on free trial. The risk gate has a `news_blackout_minutes` config (default 15 min) but no news calendar feed is implemented. For the demo, news impact is accepted as market noise.
