# NovaTrade Demo Test Run — Phase 5 Open Issues

**Phase:** 5 (Trading Agent Runtime)
**Date:** 2026-03-17
**Agent:** Trading Agent
**Strategy:** Rob Hoffman IRB v2.0.0

---

## Blockers

| ID | Summary | Resolution Path |
|----|---------|----------------|
| B-P5-1 | **MetaApiAdapter does not implement `cancel_order()`**. The adapter ABC now declares a non-abstract `cancel_order` method with a default error return, but the MetaApiAdapter does not override it. Cancel alerts will log an error and transition to FLAT (fail-safe), but the pending order will not actually be cancelled on the broker side. | Implement `cancel_order` in MetaApiAdapter using MetaApi's `cancelOrder` RPC endpoint. Required before demo run processes cancel alerts. |
| B-P5-2 | **No monitoring layer to detect fills and broker closes**. The Trading Agent exposes `notify_fill()` and `notify_broker_close()` as the integration surface, but no monitoring component calls these methods. Without this, PENDING -> LONG/SHORT transitions and broker-side SL exits are not detected. | Build a fill-detection polling loop or webhook listener. This is Phase 6+ scope. For initial demo, manual invocation or a thin polling adapter is acceptable. |

## Inherited Blockers (from Phase 4)

| ID | Summary | Status |
|----|---------|--------|
| B-IRB-1 | Pine compilation not verified in TradingView | Still open — resolve at start of demo run |
| B-IRB-2 | No live backtest executed | Still open — resolve at start of demo run |

---

## Warnings

| ID | Summary | Mitigation |
|----|---------|------------|
| P5-W-1 | **Evidence recorder coupling**: Trading Agent uses `EvidenceRecorder._append()` (private method) for custom events. This is a minor encapsulation violation within the same package. | Acceptable for Phase 5. If evidence recording is refactored later, the Trading Agent's `_record_event` method is the single point to update. |
| P5-W-2 | **Idempotency set is in-memory only**. If the Trading Agent process restarts, all seen keys are lost. Duplicate alerts received after restart will be processed again. | Acceptable for demo. For production, persist seen keys to disk or use the evidence trail to reconstruct. |
| P5-W-3 | **Risk engine PnL values are zero on close**. When the Trading Agent processes a CLOSE_POSITION (time stop), it calls `risk_engine.on_trade_close()` with `pnl_usd=0.0` and `pnl_pips=0.0` because the actual P&L is not available in the close alert. | Acceptable for demo. Actual P&L should be reconciled from broker account state after close. The monitoring layer (B-P5-2) would handle this. |
| P5-W-4 | **Replace order cancel may fail silently**. When processing REPLACE_STOP_ORDER, the Trading Agent attempts to cancel the existing pending order before placing the new one. If the cancel fails (order already filled/expired), the new order is placed anyway. This could briefly result in two pending orders. | Acceptable per spec §4.3.1 (IRB replacement). The broker's pyramiding=0 constraint provides a safety net. Monitor for duplicate order evidence events. |

---

## Informational

| ID | Summary |
|----|---------|
| P5-I-1 | The Trading Agent does not implement a webhook HTTP server. Alert delivery mechanism (webhook -> JSON parse -> `process_alert()`) is out of Phase 5 scope. A thin HTTP wrapper will be needed for the demo run. |
| P5-I-2 | `notify_broker_close()` does not call `risk_engine.on_trade_close()` — this is the caller's responsibility, since the caller has access to actual P&L data from the broker. |
| P5-I-3 | The `cancel_order` method was added to the adapter ABC as a non-abstract method with a default error return. This is a backward-compatible extension — existing adapter implementations continue to work without modification. |

---

## Summary

| Severity | Count |
|----------|-------|
| Blocker (new) | 2 |
| Blocker (inherited) | 2 |
| Warning | 4 |
| Informational | 3 |

**Both new blockers are resolvable** without changes to the Trading Agent itself:
- B-P5-1 requires a MetaApiAdapter extension (~20 lines)
- B-P5-2 requires a monitoring/polling layer (Phase 6+ scope, but a thin wrapper is feasible for demo)

---

STOPPED AT FRESH IRB PHASE 5 — NO LATER PHASE WORK PERFORMED
