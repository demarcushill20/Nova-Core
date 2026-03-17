# NovaTrade Demo Test Run — Phase 1 Open Issues (IRB)

**Phase:** 1 (Formal Strategy Specification) — RESTARTED for IRB
**Date:** 2026-03-17
**Status:** LOCKED
**Replaces:** EMA Crossover Phase 1 open issues (2026-03-16)

---

## Inherited Blockers (from Phase 0)

| ID | Issue | Status | Impact |
|----|-------|--------|--------|
| Q1 | ~~Operator must approve strategy~~ | **RESOLVED** | Originally EMA (2026-03-16), changed to IRB (2026-03-17). Approved. |
| Q2 | ~~FTMO trial expiration date~~ | **RESOLVED** | Trial expires 2026-03-28. Run must start by 2026-03-18. |

---

## Resolved in This Phase

| ID | Issue | Status | Resolution |
|----|-------|--------|------------|
| U1 | Trend filter quantification | **RESOLVED** | TF-B with s=0.4. See strategy_spec.yaml §3.2 |
| U2 | ATR overextension threshold | **RESOLVED** | k=2.0. See strategy_spec.yaml §3.5 |
| U3 | Trailing stop mechanics | **RESOLVED** | Exit-2 ATR trail (1.5×ATR) + 40-bar time stop. See strategy_spec.yaml §5.3-5.4 |
| U4 | Sideways market detection | **RESOLVED** | ADX(14) >= 20 + TF-B slope threshold. See strategy_spec.yaml §3.4 |
| U5 | Trigger window enforcement | **RESOLVED** | Hard cancel at N=20 bars. See strategy_spec.yaml §4.3 |
| U6 | Body-size filter | **RESOLVED** | Excluded — community variant, not canonical. See strategy_spec.yaml §11 |
| U7 | Position sizing method | **RESOLVED** | Risk-based, r=0.01. See strategy_spec.yaml §5.6 |
| U8 | Trade invalidation vs SL | **RESOLVED** | Redundant — same as SL. See strategy_spec.yaml §5.1 |

---

## New Phase 1 Issues (Non-Blocking)

| ID | Issue | Severity | Impact | Resolution Path |
|----|-------|----------|--------|-----------------|
| P1 | **Stop order support in MetaApi adapter needs verification** | Medium | The NovaTrade adapter currently uses `strategy.entry()` with market orders. BUY_STOP/SELL_STOP require different adapter methods. If MetaApi's `createOrder()` doesn't support pending stop orders, the entire entry mechanism fails. | Phase 2 must verify MetaApi stop order support with a test order before Pine implementation. |
| P2 | **Position SL modification for trailing stop needs verification** | Medium | The trailing stop requires modifying the stop-loss on an open position after every H1 bar close. The adapter must support `modifyPosition()` to change the SL level. If not supported, trailing stop cannot be implemented in-band. | Phase 2 must verify MetaApi SL modification API. Fallback: close and reopen with new SL (ugly but functional). |
| P3 | **IRB signal frequency on EURUSD H1 may be lower than expected** | Medium | With all 5 filters active (geometry + slope + MTF + ADX + ATR), IRB signals may be infrequent. If fewer than 10 trades complete in 10 days, failure condition F8 applies. Unlike EMA crossover which generates 1-3 signals/day, IRB may generate 0-2 signals/day, and only a fraction trigger (stop orders may expire). | Phase 4 backtest on historical EURUSD H1 data will quantify signal frequency. If insufficient, consider: (a) lower slope threshold to 0.3, (b) widen trigger window to 30 bars, (c) reduce ADX threshold to 18. |
| P4 | **H4 data availability in Pine requires `request.security()`** | Low | Accessing H4 EMA(20) from an H1 chart in Pine requires `request.security()` with correct lookahead settings. This introduces potential repainting risk if misconfigured. | Phase 2 Pine implementation must use `barmerge.lookahead_off` (AR4). Phase 3 anti-repaint review must specifically validate MTF data access. |
| P5 | **Trailing stop evaluation frequency: bar-close vs intra-bar** | Low | The spec defines trailing stop updates on H1 bar close. In live execution, the protective stop is a server-side MT5 stop — it triggers intra-bar if price touches it. The trail update only moves the stop at bar close. Between bar closes, the stop is wherever it was last set. This is correct behavior but means intra-bar spikes can stop out a position even if the close would have been favorable. | Accepted. This is standard behavior for bar-close strategy evaluation. The stop is a protective mechanism, not a trailing recalculation on every tick. |
| P6 | **Volume clamping may reduce risk consistency** | Low | For small IRB candles (tight stops), the computed lot size may exceed the 1.0 lot max, requiring clamping. When clamped, the actual risk is less than 1% of equity. For large IRB candles (wide stops), lot size is naturally smaller. | Accepted. Clamping only reduces risk below 1%, never increases it. For a systems test, this is safe. TV22/TV23 demonstrate the clamping behavior. |
| P7 | **Strategy spec SHA-256 hash is a placeholder** | Low | The hash must be computed after the spec is finalized and operator-approved. It must be recorded before the demo run starts. | Compute hash after this Phase 1 review. Record in strategy_spec.yaml §10. |
| P8 | **No opposing-direction reversal may reduce position utilization** | Low | Unlike EMA crossover which reverses on opposing signals, the IRB strategy ignores opposite-direction signals while a position is open. This means the strategy is FLAT or PENDING after each trade resolves, potentially missing signals during the cooldown. | Accepted. This is by design — IRB is a continuation setup, not a switching system. The state machine correctly prevents reversal. |

---

## Deferred to Later Phases

| ID | Item | Deferred To | Reason |
|----|------|-------------|--------|
| D8 | Exit-1 S/R proxy trailing (pivot R1/S1, prior day high/low) | Post-demo-run | Adds complexity; Exit-2 ATR trail is sufficient for systems test |
| D9 | Walk-forward / parameter stability testing | Phase 4 (Backtesting) | Requires backtest infrastructure |
| D10 | Parameter sweep (slope s, overextension k, trail multiplier) | Phase 4 (Backtesting) | Requires backtest infrastructure |
| D11 | Body-size filter (body < 45% of range) | Post-demo-run | Community variant, not canonical; test as robustness variant |
| D12 | Multiple simultaneous IRB positions | Post-demo-run | One position at a time for first demo |
| D13 | Partial position closing | Post-demo-run | All-or-nothing exits are simpler |
| D14 | Session-specific filters (London/NY) | Post-demo-run | More signals = more evidence for systems test |
| D15 | News event handling | Post-demo-run | No news calendar feed implemented |
| D16 | Reverse IRBs | Post-demo-run | Advanced variant, out of scope per charter |

---

## Resolution Priority

1. ~~**U1-U8 (all unresolved items)**~~ — ALL RESOLVED in this phase.
2. **P1 (stop order support)** — Medium severity, verify in Phase 2 before Pine implementation.
3. **P2 (SL modification for trailing)** — Medium severity, verify in Phase 2.
4. **P3 (signal frequency)** — Medium severity, quantify in Phase 4 backtest.
5. ~~**P7 (hash computation)**~~ — Compute after operator review.
6. All other issues (P4-P6, P8) are low severity and do not block forward progress.
