# NovaTrade Demo Test Run — Phase 2 Open Issues

**Phase:** 2 (Pine Implementation)
**Date:** 2026-03-17
**Status:** LOCKED
**Spec:** strategy_spec.yaml v2.0.0 (Rob Hoffman IRB)

---

## Inherited Issues (from Phase 0 / Phase 0.5 / Phase 1)

| ID | Issue | Status | Relevance to Phase 2 |
|----|-------|--------|---------------------|
| Q1 | Strategy approval | **RESOLVED** | N/A — IRB strategy approved |
| Q2 | FTMO trial expiration | **RESOLVED** | N/A — expires 2026-03-28 |
| P4 | Bar-alignment between MetaApi and Pine | **Carries forward** | Phase 3 backtesting should verify H1 bar boundaries match between TradingView chart data and broker data. |
| P6 | Evidence schema not formally defined | **Partially resolved** | The alert payload schema (`alerts_schema.json` v2.0.0) defines the Pine→Trading Agent contract for all 4 alert types. The full evidence JSONL schema (fill, close, risk_decision records) remains a Trading Agent concern. |

---

## New Phase 2 Issues

| ID | Issue | Severity | Impact | Resolution Path |
|----|-------|----------|--------|-----------------|
| P-IRB-1 | **SL/trailing stop intra-bar priority is determined by Pine engine, not by code** | Non-blocking | When the stop level is hit during a bar, Pine's engine determines the fill price and timing. The spec defines priority SL > trailing > time, but since SL and trailing stop share the same `strategy.exit(stop=cur_stop)` call, they are effectively one mechanism. The time stop uses a separate `strategy.close()`. In live execution, the broker handles SL fills. | Accept. SL and trail are unified in Pine via `cur_stop`. No conflict possible. |
| P-IRB-2 | **MTF H4 alignment uses H1 20-bar lookback instead of direct H4 5-bar lookback** | Non-blocking | The spec says "ema_20_h4[current] > ema_20_h4[current - 5]" (5 H4 bars). Pine code uses `ema_h4 > ema_h4[MTF_H1_LOOKBACK]` where `MTF_H1_LOOKBACK = 20` (20 H1 bars ≈ 5 H4 bars). Since `ema_h4` from `request.security()` is sampled at H4 granularity, `ema_h4[20]` on H1 refers to the H4 value 20 H1 bars ago. Due to `request.security()` behavior, this is the H4 EMA value from approximately 5 H4 bars back. The temporal windows are equivalent. | Accept. Standard approach for cross-timeframe lookback in Pine. Phase 3 should verify H4 direction detection against manual inspection. |
| P-IRB-3 | **`na` guard on `ema_h4` may suppress early signals even after warmup** | Non-blocking | The MTF alignment check includes `not na(ema_h4) and not na(ema_h4[MTF_H1_LOOKBACK])`. If the chart has fewer than 20 H1 bars of H4 EMA data available, MTF signals are suppressed. The warmup guard (34 bars) should cover this in most cases, but on charts with limited history, early signals may be missed. | Accept. Conservative suppression is the correct behavior — better to miss a signal than to generate one with insufficient data. |
| P-IRB-4 | **Pine cannot implement runtime risk gate checks (IC10-IC15)** | Non-blocking | Six of the fifteen invalid trade conditions (spread, drawdown, kill switch, dry run, adapter health, cooldown) are runtime concerns that Pine has no access to. Pine enforces IC1-IC9 (warmup, geometry, filters, position constraints). The remaining checks are the Risk Management Agent's responsibility. | By design. The spec explicitly marks these as `enforced_by: "risk_gate"`. The separation is correct and documented in spec_traceability.md. |
| P-IRB-5 | **TradingView chart data source may differ from broker data source** | Non-blocking | Pine backtests use TradingView's EURUSD data feed. The live demo run uses OANDA-Demo-1 via MetaApi. Differences in data source can cause: (a) different bar close prices, (b) different H1 bar boundaries, (c) different indicator values. This means backtest signals may not match live signals exactly. | Phase 3 should document which data feed is used for backtesting. Phase 4 should expect minor signal divergence and use reconciliation to detect it. |
| P-IRB-6 | **Position sizing via `f_qty()` uses `strategy.equity` which varies over the backtest** | Non-blocking | `strategy.equity` changes as P&L accrues during backtesting. This means lot sizes vary not just by IRB stop distance but also by accumulated equity. In live execution, the Trading Agent should use current account equity from MetaApi. The alert payload's `risk_dollars` field is computed from `strategy.equity` at signal time, providing the Trading Agent with the Pine-side expectation. | Accept. The spec says "1% of equity" — equity is inherently dynamic. |
| P-IRB-7 | **`prev_stop` tracking for trail alerts may miss the first bar's SL** | Non-blocking | The `var float prev_stop = na` initialization means on the first bar after fill, `cur_stop` (initial SL) will differ from `na`, triggering a MODIFY_SL alert. This is actually correct behavior — the Trading Agent should receive the initial SL level as a MODIFY_SL event after fill detection. On subsequent bars, only actual tightening triggers alerts. | Accept. The initial SL alert is a useful signal for the Trading Agent to confirm SL placement. |

---

## Deferred to Later Phases

| ID | Item | Deferred To | Reason |
|----|------|-------------|--------|
| D-IRB-1 | Full evidence JSONL schema definition | Phase 4 (Trading Agent) | The alert payload schema covers Pine→Agent contract. The complete evidence schema (including fill, close, risk_decision records) must be defined by the Trading Agent. |
| D-IRB-2 | Pine-to-TradingView webhook integration | Phase 4 (Trading Agent) | Pine generates alerts; TradingView delivers them via webhook. The delivery mechanism, authentication, and retry logic are Phase 4 concerns. |
| D-IRB-3 | Pending order management on MetaApi | Phase 4 (Trading Agent) | The Trading Agent must translate PLACE_STOP_ORDER, REPLACE_STOP_ORDER, and CANCEL_ORDER alerts into MetaApi `placePendingOrder()`, `modifyPosition()`, and `cancelOrder()` calls. |
| D-IRB-4 | Alert payload parsing and validation | Phase 4 (Trading Agent) | The Trading Agent must parse JSON payloads, validate against `alerts_schema.json`, and reject malformed alerts. |
| D-IRB-5 | Trailing stop SL modification on MetaApi | Phase 4 (Trading Agent) | MODIFY_SL alerts require the Trading Agent to call `modifyPosition()` with the new stop level. The Trading Agent must verify the position still exists before modifying. |
| D-IRB-6 | Time stop market close on MetaApi | Phase 4 (Trading Agent) | CLOSE_POSITION alerts require the Trading Agent to close the position at market. This is a market order, not a stop order — different from the initial entry mechanism. |
| D-IRB-7 | Position reconciliation between Pine state and broker state | Phase 4 (Trading Agent) | The Trading Agent must detect and handle state divergence (e.g., Pine thinks PENDING but broker has no pending order; Pine thinks LONG but broker is flat due to manual intervention). |

---

## Resolution Priority

1. **P4 (bar alignment)** — Low severity, should be checked in Phase 3 backtesting.
2. **P-IRB-2 (MTF lookback)** — Non-blocking, should be verified in Phase 3.
3. **P-IRB-5 (data source divergence)** — Non-blocking but important context for Phase 3 interpretation.
4. All other Phase 2 issues (P-IRB-1, P-IRB-3, P-IRB-4, P-IRB-6, P-IRB-7) are non-blocking and well-documented.

**No blockers introduced in Phase 2.**
