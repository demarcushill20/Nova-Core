# NovaTrade Demo Test Run — Phase 4 Open Issues (Fresh IRB)

**Phase:** 4 (Backtesting and Validation)
**Date:** 2026-03-17
**Status:** LOCKED
**Agent:** Backtesting Agent
**Replaces:** EMA Crossover Phase 4 open issues (2026-03-16, SUPERSEDED)

---

## Blockers

| ID | Issue | Severity | Impact | Resolution Path |
|----|-------|----------|--------|-----------------|
| B-IRB-1 | **Pine compilation not verified in TradingView** | BLOCKER | `strategy.pine` v2.0.0 has been validated by Phase 3 static analysis (45 checks, all PASS) but has never been loaded in TradingView. If it fails to compile, the demo run cannot proceed. Inherited from Phase 3 (P3-IRB-3). | **Must be resolved before demo run.** Load on EURUSD H1 chart in TradingView. If it compiles: B-IRB-1 is resolved. If it fails: fix syntax, re-validate Phase 3. |
| B-IRB-2 | **No live backtest has been executed** | BLOCKER (for full GO) | All performance estimates are analytical. Trade frequency, win rate, drawdown, and actual signal behavior on EURUSD H1 are unknown. This is the primary reason for CONDITIONAL (not full) GO. | **Resolve via C2:** Run TradingView strategy tester on 30-day EURUSD H1 window. Verify ≥1 trade exists. Measure actual signal frequency. If zero trades: investigate filter interaction. |

---

## New Phase 4 Issues

| ID | Issue | Severity | Impact | Resolution Path |
|----|-------|----------|--------|-----------------|
| P4-IRB-1 | **Trade frequency is borderline for E6 success criterion** | Warning | Analytical estimate: 2-8 completed trades in 10 calendar days. E6 requires ≥10 completed trades. With 0-2 signals/day and ~50% stop-order fill rate, the strategy may not produce enough completed trades for a conclusive systems test in 10 days. | **Monitor via C3/C5.** If <3 completed trades after 5 trading days, extend demo to 20 calendar days. Count pipeline events (signals + cancellations + trail updates) as additional validation. |
| P4-IRB-2 | **Actual IRB frequency on EURUSD H1 is unknown** | Warning | The 5-filter combination has not been tested on real data. Analytical estimates (1.5% of bars qualify) may be too optimistic or pessimistic. The trend filter (s=0.4) and ADX filter (≥20) may interact to suppress more signals than expected. | **Resolve via C2.** TradingView backtest on 30-day window will reveal actual signal count. If zero signals: lower trend threshold from 0.4 to 0.3 (requires Phase 1 restart). |
| P4-IRB-3 | **No measured drawdown data exists** | Warning | All FTMO compliance estimates are analytical upper bounds. Actual equity drawdown (including floating P&L) has not been measured. The FTMO safety margin estimates (2x daily, 1.4x total) are computed from worst-case consecutive-loss scenarios, not from measured data. | **Phase 5 must measure equity drawdown** directly from MetaApi account snapshots. Inherited concern from EMA IF2 (drawdown methodology). |
| P4-IRB-4 | **Time stop (40 bars) may never trigger in a short demo** | Informational | The time stop requires a position held for 40+ bars (~40 hours = ~2 trading days). In a 10-day demo with few trades, a time stop event may never occur, leaving that pipeline path untested. | **Accept for demo.** Time stop is a safety mechanism. If it doesn't trigger, that's a positive signal (trades are resolving via SL or trail). |
| P4-IRB-5 | **No sensitivity analysis on filter parameters** | Informational | The spec defines sweepable ranges for key parameters (trend s: 0.2-0.8, overextension k: 1.5-3.0, trigger window: 10-40, trail multiplier: 1.0-3.0). No parameter sweep has been conducted to understand sensitivity. This is acceptable for a first demo but limits understanding of strategy behavior under different parameterizations. | **Defer to post-demo analysis.** Parameter sensitivity testing requires TradingView or a Python IRB backtester. Not needed for the systems test GO/NO-GO. |

---

## Inherited Issues (from Phases 0-3)

| ID | Issue | Status | Phase 4 Resolution |
|----|-------|--------|--------------------|
| P4 | Bar alignment between MetaApi and Pine | **Carries forward** | Cannot verify without live broker data. Phase 5 concern. |
| P-IRB-1 | SL/trail intra-bar priority is engine-determined | **Carries forward** | Phase 3 confirmed: SL and trail share unified `cur_stop`. No conflict. |
| P-IRB-2 | MTF H4 uses H1 20-bar lookback (≈5 H4 bars) | **Carries forward** | Phase 3 confirmed: temporal windows equivalent. |
| P-IRB-3 | `na` guard on H4 EMA may suppress early signals | **Carries forward** | Conservative. Warmup covers most cases. |
| P-IRB-4 | Pine cannot implement runtime risk gate (IC10-IC15) | **Carries forward** | Correctly deferred to Trading Agent. |
| P-IRB-5 | TradingView data source may differ from broker data | **Carries forward** | Cannot verify without TradingView access. |
| P-IRB-6 | Position sizing uses dynamic `strategy.equity` | **Carries forward** | Consistent with spec. |
| P-IRB-7 | First MODIFY_SL alert has `old_stop` = 0 | **Carries forward** | Documented. Trading Agent should handle. |
| P3-IRB-1 | One-bar SL protection gap in backtest | **Carries forward** | Cannot quantify without backtest. Inherent Pine limitation. |
| P3-IRB-2 | Format string `"#.#####"` variable precision | **Carries forward** | No data loss. Accepted. |
| P3-IRB-3 | No direct Pine compiler verification | **Elevated to B-IRB-1** | Blocker for demo deployment. |

---

## Moot Issues (EMA-Specific, No Longer Applicable)

| ID | Prior Issue | Why Moot |
|----|-----------|----------|
| B1 | EMA Pine compilation blocker | Superseded by B-IRB-1 (IRB Pine) |
| IF1 | EMA backtester SL/TP-bar signal suppression | EMA backtester not applicable to IRB |
| IF2 | Drawdown methodology (closed-trade vs equity) | Carries forward as lesson, not as specific issue |
| IF3 | 30d label mislabel in EMA backtester | EMA backtester artifact |
| P3 | Reversal atomicity (TV16) | IRB does not reverse |
| P8 | Same-bar SL+TP priority | IRB has no fixed TP |
| P17 | 30d data_timeframe label bug | EMA backtester artifact |
| P18 | Missing computed metrics | EMA metrics; new metrics needed for IRB |

---

## Deferred to Later Phases

| ID | Item | Deferred To | Reason |
|----|------|-------------|--------|
| B-IRB-1 | Pine compilation in TradingView | Phase 5 (pre-deployment) | Requires TradingView access |
| B-IRB-2 | Live backtest execution | Phase 5 (pre-deployment) | Requires TradingView strategy tester |
| P4-IRB-5 | Parameter sensitivity analysis | Post-demo | Requires backtesting infrastructure |
| D-IRB-1 to D-IRB-7 | Trading Agent concerns | Phase 5+ | Evidence schema, webhook integration, pending order management, alert parsing, trailing stop modification, time stop execution, position reconciliation |

---

## Issue Summary

| Severity | Count | IDs |
|----------|-------|-----|
| Blocker | 2 | B-IRB-1, B-IRB-2 |
| Warning | 3 | P4-IRB-1, P4-IRB-2, P4-IRB-3 |
| Informational | 2 | P4-IRB-4, P4-IRB-5 |
| Inherited (carries forward) | 10 | P4, P-IRB-1 to P-IRB-7, P3-IRB-1, P3-IRB-2 |
| Moot (EMA-specific) | 8 | B1, IF1, IF2, IF3, P3, P8, P17, P18 |

**Two blockers prevent full GO:**
1. B-IRB-1: Pine must compile in TradingView
2. B-IRB-2: Live backtest must confirm strategy produces trades

**Both blockers are resolvable at the start of Phase 5** by loading the script in TradingView. The CONDITIONAL GO allows Phase 5 to begin with these as first-order tasks.
