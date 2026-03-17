# NovaTrade Demo Test Run — Execution State Machine

**Phase:** 5 (Trading Agent Runtime)
**Date:** 2026-03-17
**Agent:** Trading Agent
**Strategy:** Rob Hoffman IRB v2.0.0

---

## 1. State Model

The Trading Agent maintains a 5-state FSM that mirrors the strategy.pine state model exactly. The agent state and the Pine state must remain synchronized — any divergence is a reconciliation issue to be detected by the monitoring layer.

### 1.1 States

| State | Description | Tracked IDs |
|-------|-------------|-------------|
| **FLAT** | No pending orders, no open positions | None |
| **PENDING_LONG** | Buy stop order placed, awaiting fill | `pending_order_id` |
| **PENDING_SHORT** | Sell stop order placed, awaiting fill | `pending_order_id` |
| **LONG** | Long position open | `position_id` |
| **SHORT** | Short position open | `position_id` |

### 1.2 Invariants

1. **At most one pending order** at any time (pyramiding=0 per spec §4.3)
2. **At most one open position** at any time (pyramiding=0 per spec §4.3)
3. **Never both** a pending order and an open position simultaneously
4. `pending_order_id` is non-null only in PENDING_LONG/PENDING_SHORT
5. `position_id` is non-null only in LONG/SHORT

---

## 2. Transitions

### 2.1 Alert-Driven Transitions

| From State | Alert Action | To State | Condition |
|------------|-------------|----------|-----------|
| FLAT | PLACE_STOP_ORDER (BUY) | PENDING_LONG | Risk gate ALLOW + adapter success |
| FLAT | PLACE_STOP_ORDER (SELL) | PENDING_SHORT | Risk gate ALLOW + adapter success |
| PENDING_LONG | REPLACE_STOP_ORDER (BUY) | PENDING_LONG | Same-direction IRB replacement |
| PENDING_SHORT | REPLACE_STOP_ORDER (SELL) | PENDING_SHORT | Same-direction IRB replacement |
| PENDING_LONG | CANCEL_ORDER | FLAT | Trigger window expired (20 bars) |
| PENDING_SHORT | CANCEL_ORDER | FLAT | Trigger window expired (20 bars) |
| LONG | MODIFY_SL | LONG | Trailing stop tightened (no state change) |
| SHORT | MODIFY_SL | SHORT | Trailing stop tightened (no state change) |
| LONG | CLOSE_POSITION | FLAT | Time stop (40 bars) |
| SHORT | CLOSE_POSITION | FLAT | Time stop (40 bars) |

### 2.2 Broker-Driven Transitions (External Notifications)

| From State | Event | To State | Source |
|------------|-------|----------|--------|
| PENDING_LONG | Order filled | LONG | Monitoring layer detects fill |
| PENDING_SHORT | Order filled | SHORT | Monitoring layer detects fill |
| LONG | Position closed | FLAT | Broker SL/trailing stop hit |
| SHORT | Position closed | FLAT | Broker SL/trailing stop hit |

### 2.3 Invalid Transitions (Rejected)

| Current State | Alert Action | Rejection Reason |
|--------------|-------------|------------------|
| PENDING_LONG/SHORT | PLACE_STOP_ORDER | Must be FLAT to place new order |
| LONG/SHORT | PLACE_STOP_ORDER | Signal suppression while in position (spec §4.3.3) |
| FLAT | MODIFY_SL | No open position to modify |
| PENDING_LONG/SHORT | MODIFY_SL | Position not yet open |
| FLAT | CANCEL_ORDER | No pending order to cancel |
| LONG/SHORT | CANCEL_ORDER | No pending order while in position |
| FLAT | CLOSE_POSITION | No position to close |
| PENDING_LONG/SHORT | CLOSE_POSITION | Position not yet open |

---

## 3. State Diagram

```
                    PLACE_STOP_ORDER (BUY)
              ┌──────────────────────────────┐
              │         risk ALLOW            │
              │                               ▼
              │                        ┌──────────────┐
              │      REPLACE (BUY)     │              │
              │    ┌──────────────────►│ PENDING_LONG │──┐
              │    │  (cancel + place) │              │  │
              │    └───────────────────┤              │  │
              │                        └──────┬───────┘  │
              │                               │          │
              │                   CANCEL_ORDER│    fill  │
              │                               │          │
    ┌─────────┴──────┐                        │          │
    │                │◄───────────────────────┘          │
    │      FLAT      │                                   │
    │                │◄──────────────────────────────────┤
    │                │◄──── broker SL/trail ────┐        │
    └─────────┬──────┘                          │        │
              │                        ┌────────┴───┐    │
              │                   ┌───►│            │    │
              │    CLOSE (time    │    │    LONG    │◄───┘
              │     stop)         │    │            │
              │                   │    └────────────┘
              │                   │    MODIFY_SL (no state change)
              │
              │    PLACE_STOP_ORDER (SELL)
              │         risk ALLOW
              │                        ┌───────────────┐
              │      REPLACE (SELL)    │               │
              └──────────────────────►│ PENDING_SHORT  │──┐
                   ┌──────────────────►│               │  │
                   │  (cancel + place) │               │  │
                   └───────────────────┤               │  │
                                       └──────┬────────┘  │
                                              │           │
                                  CANCEL_ORDER│     fill  │
                                              │           │
                      ┌───────────────────────┘           │
                      │                                   │
                      ▼                                   │
                    FLAT ◄── broker SL/trail ──┐          │
                                               │          │
                                       ┌───────┴────┐    │
                                  ┌───►│            │    │
                   CLOSE (time    │    │   SHORT    │◄───┘
                    stop)         │    │            │
                                  │    └────────────┘
                                  │    MODIFY_SL (no state change)
```

---

## 4. Risk Gate Integration

Only **PLACE_ORDER** and **REPLACE_ORDER** intents pass through the risk gate. All other intents (MODIFY_SL, CANCEL_ORDER, CLOSE_POSITION) bypass risk checks because they reduce or close exposure.

### 4.1 Risk Gate Flow for Signal Alerts

```
signal_alert -> validate -> idempotency check -> create OrderIntent
    -> create OrderRequest -> RiskEngine.pre_trade_check()
        -> if ALLOW: adapter.place_order() -> state transition
        -> if DENY:  reject intent, record evidence, no state change
```

### 4.2 Risk Engine Lifecycle Calls

| Event | RiskEngine Method | When |
|-------|------------------|------|
| Order placed | (no call — pending orders don't affect risk) | After PLACE/REPLACE success |
| Order filled | `on_trade_fill(position_id, symbol, side, volume, fill_price, stop_loss)` | `notify_fill()` |
| SL modified | (no call — trailing stop is informational to risk engine) | After MODIFY_SL success |
| Position closed (time stop) | `on_trade_close(position_id, symbol, side, volume, pnl_*, exit_reason)` | After CLOSE success |
| Position closed (broker SL) | `on_trade_close(...)` | `notify_broker_close()` (caller responsibility) |

---

## 5. Idempotency

### 5.1 Key Generation

```
idempotency_key = irb_{action}_{bar_close_time}_{side}
```

Examples:
- `irb_PLACE_STOP_ORDER_1710000000000_BUY`
- `irb_MODIFY_SL_1710003600000_BUY`
- `irb_CANCEL_ORDER_20_SELL` (uses bars_elapsed when bar_close_time absent)

### 5.2 Duplicate Detection

- Keys are stored in a bounded in-memory set (max 1000).
- Duplicate keys are rejected before any state or execution logic.
- The set is pruned by removing the oldest half when capacity is exceeded.

---

## 6. Evidence Trail

Every state transition, rejection, and error is recorded to the append-only JSONL evidence file via the existing `EvidenceRecorder`. Events include:

| Event | Recorded When |
|-------|---------------|
| `ORDER_PLACED` | Successful PLACE/REPLACE execution |
| `ORDER_CANCELLED` | Successful CANCEL execution |
| `SL_MODIFIED` | Successful MODIFY_SL execution |
| `POSITION_CLOSED` | Successful CLOSE execution |
| `ORDER_FILLED` | `notify_fill()` called |
| `BROKER_CLOSE` | `notify_broker_close()` called |
| `RISK_DENIED` | Risk engine rejected the intent |
| `ADAPTER_ERROR` | Broker adapter returned failure |
| `MODIFY_SL_ERROR` | SL modification failed |
| `CANCEL_ERROR` | Order cancellation failed |
| `CLOSE_ERROR` | Position close failed |

Each evidence record includes the full `OrderIntent.to_dict()` for traceability.

---

## 7. Failure Behavior

| Failure Type | Behavior | State Impact |
|-------------|----------|--------------|
| Alert validation failure | Reject, record error, no state change | None |
| Duplicate alert | Reject silently, no state change | None |
| Invalid FSM transition | Reject, record reason | None |
| Risk gate DENY | Reject, record decision | None |
| Adapter error (place) | Fail, record error, no state change | None |
| Adapter error (modify SL) | Fail, record error, no state change | None |
| Adapter error (cancel) | Log error, transition to FLAT anyway | PENDING -> FLAT |
| Adapter error (close) | Fail, record error, no state change | None |
| Unhandled exception | Catch, record error, no state change | None |

**Cancel failure note:** Transitioning to FLAT on cancel failure is intentional (fail-safe). The pending order may have already expired or filled on the broker side. Staying in PENDING_X risks getting stuck in a dead state.

---

STOPPED AT FRESH IRB PHASE 5 — NO LATER PHASE WORK PERFORMED
