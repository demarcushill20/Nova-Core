# NovaTrade Demo Test Run — Phase 5 Summary (Trading Agent Runtime)

**Phase:** 5 (Trading Agent Runtime Build)
**Date:** 2026-03-17
**Status:** COMPLETE — Trading Agent runtime delivered
**Agent:** Trading Agent
**Strategy:** Rob Hoffman IRB v2.0.0 (strategy_spec.yaml v2.0.0)
**Depends on:** Phase 4 (CONDITIONAL GO)

---

## 1. Phase 5 Completion Status

**COMPLETE.** The Trading Agent runtime layer has been implemented. All required deliverables have been produced.

**Key constraint:** Phase 5 builds the Trading Agent runtime only. It does not build the webhook HTTP server, the fill-detection monitoring layer, or the reconciliation loop. These are identified as open issues (B-P5-1, B-P5-2) to be resolved before or during the demo run.

---

## 2. What Was Done

### 2.1 Trading Agent Runtime (`novatrade/execution/trading_agent.py`)

Implemented the complete IRB Trading Agent with:

- **Alert validation** — validates all 4 alert types against the `alerts_schema.json` v2.0.0 contract (strategy identity, required fields, value ranges)
- **Idempotency** — deterministic key generation (`irb_{action}_{bar_close_time}_{side}`), bounded in-memory deduplication set (max 1000 keys)
- **Order intent creation** — transforms validated alerts into governed `OrderIntent` objects with full traceability metadata
- **5-state FSM** — FLAT, PENDING_LONG, PENDING_SHORT, LONG, SHORT with strict transition validation matching strategy.pine's state model
- **Symbol normalization** — resolves broker symbol via `FtmoProfile.resolve_symbol()`, validates against alert-provided broker symbol
- **Risk engine integration** — routes PLACE/REPLACE intents through `RiskEngine.pre_trade_check()` before execution; calls `on_trade_fill()` and `on_trade_close()` at lifecycle boundaries
- **Execution pipeline** — routes to `adapter.place_order()` (signals), `adapter.modify_order()` (trailing stop), `adapter.cancel_order()` (cancel), `adapter.close_position()` (time stop)
- **External notification API** — `notify_fill()` and `notify_broker_close()` methods for monitoring layer integration
- **Evidence recording** — records every state transition, rejection, and error to append-only JSONL via `EvidenceRecorder`
- **Fail-fast behavior** — no retry logic; adapter errors are recorded and returned immediately

### 2.2 Adapter Extension (`novatrade/adapter/base.py`)

Added `cancel_order(order_id)` as a non-abstract method to the `MT5Adapter` ABC with a default error return. This is a backward-compatible extension for the IRB pending-order lifecycle.

### 2.3 Order Intent Schema (`docs/demo_test_run/order_intent_schema.json`)

JSON Schema defining the `OrderIntent` contract — the governed intermediate representation between alert ingestion and broker execution. Documents:
- 5 intent types mapped from alert actions
- Required and type-specific fields
- Valid FSM transitions per intent type
- Idempotency key format
- Symbol resolution rules

### 2.4 Execution State Machine (`docs/demo_test_run/execution_state_machine.md`)

Complete FSM documentation:
- 5 states with invariants
- 10 alert-driven transitions + 4 broker-driven transitions
- 10 invalid transition rejections
- State diagram
- Risk gate integration flow
- Evidence trail mapping
- Failure behavior matrix

### 2.5 Tests (`tests/test_trading_agent.py`)

53 tests covering:
- Alert validation (14 tests — all 4 types + rejection cases)
- Idempotency key generation (5 tests)
- FSM state transitions (8 valid transitions)
- Invalid transitions (8 rejection cases)
- Idempotency deduplication (3 tests)
- Risk gate integration (2 tests)
- Symbol resolution (2 tests)
- Adapter error handling (4 tests)
- Evidence recording (2 tests)
- External notifications (3 lifecycle tests)
- OrderIntent serialization (4 tests)
- AgentResult properties (3 tests)

---

## 3. Decisions Made

| # | Decision | Rationale |
|---|----------|-----------|
| D-P5-1 | Use `RiskEngine` (not `PreTradeGate` directly) for risk checks | RiskEngine wraps PreTradeGate + adds portfolio-level FTMO checks. The Trading Agent should use the full risk stack. |
| D-P5-2 | Add `cancel_order` as non-abstract method to adapter ABC | Backward compatible — existing adapters work without change. MetaApiAdapter implementation is a separate task (B-P5-1). |
| D-P5-3 | Transition to FLAT on cancel failure (fail-safe) | Staying in PENDING_X after a failed cancel risks getting stuck. The pending order may have already expired/filled on the broker side. |
| D-P5-4 | Risk check only on PLACE/REPLACE, not MODIFY/CANCEL/CLOSE | MODIFY_SL tightens stops (reduces risk). CANCEL/CLOSE reduce exposure. Only new order placement needs risk gate approval. |
| D-P5-5 | Pass `pnl_usd=0.0` to risk engine on time-stop close | Actual P&L not available in Pine's close alert. Reconciliation from broker account state is deferred to monitoring layer. |
| D-P5-6 | Use `EvidenceRecorder._append()` for custom Trading Agent events | Pragmatic — avoids modifying the evidence recorder interface. Single coupling point in `_record_event()`. Documented as P5-W-1. |

---

## 4. Changes Made

| File | Change | Type |
|------|--------|------|
| `novatrade/execution/trading_agent.py` | **NEW** — Trading Agent runtime (520 lines) | Implementation |
| `novatrade/adapter/base.py` | Added `cancel_order()` non-abstract method + `OrderStatus` import | Extension |
| `tests/test_trading_agent.py` | **NEW** — 53 tests for Trading Agent | Tests |
| `docs/demo_test_run/order_intent_schema.json` | **NEW** — Order intent contract | Documentation |
| `docs/demo_test_run/execution_state_machine.md` | **NEW** — FSM documentation | Documentation |
| `docs/demo_test_run/phase5_assumptions.md` | **NEW** — 10 assumptions | Documentation |
| `docs/demo_test_run/phase5_open_issues.md` | **NEW** — 2 blockers, 4 warnings, 3 info | Documentation |
| `docs/demo_test_run/phase5_summary.md` | **NEW** — This file | Documentation |

---

## 5. Assumptions

10 new assumptions (TA-1 to TA-10). See `phase5_assumptions.md` for full list.

Key risks:
- **TA-6 (HIGH)**: Monitoring layer dependency — `notify_fill()` and `notify_broker_close()` need a caller
- **TA-1, TA-3, TA-4 (MEDIUM)**: Alert format, stop order placement, SL modification — all testable in demo run

---

## 6. Open Issues

| Severity | Count | Key Items |
|----------|-------|-----------|
| Blocker (new) | 2 | B-P5-1 (MetaApiAdapter cancel_order), B-P5-2 (monitoring layer) |
| Blocker (inherited) | 2 | B-IRB-1 (Pine compilation), B-IRB-2 (no live backtest) |
| Warning | 4 | P5-W-1 to P5-W-4 |
| Informational | 3 | P5-I-1 to P5-I-3 |

Both new blockers are resolvable without changes to the Trading Agent:
- B-P5-1: MetaApiAdapter extension (~20 lines)
- B-P5-2: Monitoring layer (Phase 6+ scope)

---

## 7. Files Created/Updated

| File | Purpose |
|------|---------|
| `novatrade/execution/trading_agent.py` | Trading Agent runtime — alert -> intent -> risk -> execute -> evidence |
| `novatrade/adapter/base.py` | Added cancel_order() for IRB pending-order lifecycle |
| `tests/test_trading_agent.py` | 53 tests covering all Trading Agent functionality |
| `docs/demo_test_run/order_intent_schema.json` | Order intent contract (JSON Schema) |
| `docs/demo_test_run/execution_state_machine.md` | 5-state FSM documentation |
| `docs/demo_test_run/phase5_assumptions.md` | 10 assumptions (1 high, 3 medium, 6 low) |
| `docs/demo_test_run/phase5_open_issues.md` | 2+2 blockers, 4 warnings, 3 info |
| `docs/demo_test_run/phase5_summary.md` | This file |

---

## 8. Recommended Next Steps

### Immediate (before demo run)

1. **Resolve B-P5-1**: Implement `cancel_order()` in MetaApiAdapter using `cancelOrder` RPC
2. **Resolve inherited B-IRB-1**: Load `strategy.pine` in TradingView, confirm compilation
3. **Resolve inherited B-IRB-2**: Run TradingView backtest, verify ≥1 trade

### For demo run

4. **Build thin webhook server**: HTTP endpoint that parses JSON and calls `agent.process_alert(payload)`
5. **Build fill-detection poller** (B-P5-2): Poll `adapter.get_positions()` to detect fills and closes, call `notify_fill()` / `notify_broker_close()`
6. **Wire end-to-end**: TradingView alert -> webhook -> Trading Agent -> MetaApi -> FTMO demo

### Monitoring

7. Monitor evidence trail for: risk denials, adapter errors, invalid transitions, duplicate rejections
8. Verify C3 (signal activity in first 3 days) and C5 (trade count after 5 days)

---

## 9. Final Statement

STOPPED AT FRESH IRB PHASE 5 — NO LATER PHASE WORK PERFORMED

---

**Phase 5 complete — Trading Agent runtime delivered.**
