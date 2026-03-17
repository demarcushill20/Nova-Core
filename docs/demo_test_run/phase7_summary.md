# NovaTrade Demo Test Run — Phase 7 Summary (Monitoring, Reconciliation, Operational Safety)

**Phase:** 7 (Monitoring, Reconciliation, and Operational Safety)
**Date:** 2026-03-17
**Status:** COMPLETE — Operational monitoring layer built with 44 new tests
**Agent:** Monitoring / Reconciliation Layer
**Strategy:** Rob Hoffman IRB v2.0.0 (strategy_spec.yaml v2.0.0)
**Depends on:** Phase 6 (Risk Management Hardening — COMPLETE)

---

## 1. Phase 7 Completion Status

**COMPLETE.** The operational monitoring, reconciliation, and action-execution layer has been built. All Phase 5/6 operational blockers (B-P5-1, B-P5-2, B-P6-1, B-P6-2) are resolved.

**Key constraint:** The OpsMonitor class provides all monitoring capabilities but requires an external caller (webhook server, async loop, or cron) to invoke `run_cycle()`. This is identified as B-P7-1 and MF-P7-1 for Phase 8 resolution.

---

## 2. What Was Done

### 2.1 Operational Boundary Definition

**Phase 7 owns:**
- Health monitoring (adapter connectivity, account access)
- Broker/runtime reconciliation (Trading Agent state vs broker reality)
- Fill detection → `notify_fill()` callback
- Broker close detection → `notify_broker_close()` callback
- Risk-directed action execution (cancel pending orders, flatten positions)
- Daily drawdown reset caller (midnight UTC)
- Operational alerts (CRITICAL/WARNING/INFO)
- Daily summary generation

**Phase 7 does NOT own:**
- Strategy logic (Pine/IRB signal generation)
- Risk policy design (5-layer policy, drawdown limits)
- Trading Agent alert processing pipeline
- Final launch approval
- Dashboard or UI work
- Production observability stack

### 2.2 Health Monitoring (`novatrade/monitor/ops_monitor.py`)

- Uses existing `take_health_snapshot()` from `novatrade/monitor/health.py`
- Checks adapter connectivity, account state, position count
- Classifies health as OK, DEGRADED, or DOWN
- Emits CRITICAL alert on DOWN, WARNING on DEGRADED
- Records all health snapshots to evidence trail
- Reconciliation is skipped when health is not OK (safe degradation)

### 2.3 Reconciliation Worker (`novatrade/monitor/ops_monitor.py`)

Compares Trading Agent internal state against broker reality each cycle:

| Agent State | Broker State | Outcome | Action Taken |
|------------|-------------|---------|-------------|
| PENDING_LONG/SHORT | Position exists | `NOTIFY_FILL` | `agent.notify_fill()` called |
| LONG/SHORT | No position | `NOTIFY_BROKER_CLOSE` | `agent.notify_broker_close()` + `risk.on_trade_close()` called |
| PENDING_LONG/SHORT | No order, no position | `CANCEL_STALE_PENDING` | `agent.force_flat()` called |
| FLAT | Position exists | `ORPHAN_POSITION` | CRITICAL alert, manual review |
| FLAT | Pending order exists | `ORPHAN_PENDING` | WARNING alert, manual review |
| FLAT | Nothing | `NO_ACTION` | State consistent |
| LONG/SHORT | Position exists | `NO_ACTION` | State consistent |
| Any | Adapter error | `DEGRADED` | WARNING alert |

All reconciliation actions are recorded to evidence trail.

### 2.4 B-P5-2 Resolution: Fill/Close Detection Callbacks

**Fill detection:**
- Monitoring cycle checks if agent is in PENDING state and broker has a matching position
- If match found → calls `agent.notify_fill(position_id, fill_price, volume, stop_loss)`
- Agent transitions PENDING_LONG → LONG (or PENDING_SHORT → SHORT)
- Risk engine receives `on_trade_fill()` via the Trading Agent

**Broker close detection:**
- Monitoring cycle checks if agent is in LONG/SHORT state and broker has no matching position
- If position gone → calls `agent.notify_broker_close(position_id, exit_reason)`
- Also calls `risk_engine.on_trade_close()` with placeholder P&L
- Agent transitions LONG/SHORT → FLAT

### 2.5 B-P5-1 Resolution: MetaApiAdapter cancel_order()

Added to `novatrade/adapter/metaapi_provider.py`:
- `cancel_order(order_id)` → delegates to MetaApi SDK `connection.cancel_order()`
- Returns `OrderResult` with success/failure status
- Error handling matches existing adapter pattern (try/except with safe error messages)

Also added:
- `get_orders()` → fetches all pending orders from MetaApi SDK `connection.get_orders()`
- Returns `list[PendingOrder]` (new model type)
- Translation helper `_translate_pending_order()` converts MetaApi order dicts

### 2.6 B-P6-1 Resolution: Daily Drawdown Reset Caller

Implemented in `OpsMonitor._check_daily_reset()`:
- Compares current UTC date against last reset date
- On new day: fetches current equity via `adapter.get_account()`
- Calls `risk_engine.reset_daily(equity)`
- Records reset event to evidence trail
- **Does NOT clear halt** (per kill_switch_policy.md §7)
- Updates last reset date to prevent double reset

### 2.7 B-P6-2 Resolution: Advisory → Operational Action Execution

Implemented in `OpsMonitor.execute_pending_actions()`:

| Risk Action | Execution | Status |
|------------|-----------|--------|
| `CANCEL_PENDING` | Fetches pending orders via `get_orders()`, calls `cancel_order()` for each | **Operational** — now executed automatically |
| `FLATTEN_POSITIONS` | Fetches open positions via `get_positions()`, calls `close_position()` for each, notifies risk engine and trading agent | **Operational** — now executed automatically |
| `HALT_TRADING` | Already enforced by risk engine (fast-reject in Trading Agent) | **Operational** (Phase 6) |
| `SUSPEND_STRATEGY` | Not used in IRB single-strategy setup | Advisory only |

Failure handling:
- Individual cancel/flatten failures are logged but don't halt the batch
- Partial success returns `MANUAL_REVIEW` outcome
- All actions recorded to evidence trail

### 2.8 Trading Agent Minimal Addition

Added `TradingAgent.force_flat(reason)`:
- Reconciliation escape hatch for stale pending orders
- Resets all state (pending_order_id, position_id, sides) to FLAT
- Records `FORCE_FLAT` event to evidence trail with reason
- Only called by monitoring layer, never by alert processing

### 2.9 Model and Adapter Additions

**novatrade/models.py:**
- `EvidenceType.MONITORING` — new evidence type for operational monitoring events
- `PendingOrder` dataclass — represents a pending (unfilled) order at the broker

**novatrade/adapter/base.py:**
- `get_orders()` — non-abstract default returning empty list (backward-compatible)

**novatrade/adapter/metaapi_provider.py:**
- `cancel_order(order_id)` — concrete implementation via MetaApi SDK
- `get_orders()` — concrete implementation via MetaApi SDK
- `_translate_pending_order()` — translation helper for MetaApi order dicts

### 2.10 Alerts and Daily Summary

**Alert system:**
- 3 severity levels: CRITICAL, WARNING, INFO
- 5 categories: HEALTH, RECONCILIATION, RISK, ACTION, OPERATIONAL
- All alerts logged to Python logger and evidence trail
- Daily summary aggregates alert counts by severity

**Daily summary:**
- JSON file written to `OUTPUT/novatrade/daily_summary_YYYY-MM-DD.json`
- Captures: cycles run, health stats, fills, closes, risk halts, actions, reconciliation mismatches, current state
- Generated via `monitor.generate_daily_summary()`
- Written via `monitor.write_daily_summary(summary, output_dir)`

### 2.11 Tests

**44 new Phase 7 tests** (`tests/test_ops_monitor.py`):

| Category | Count | What's Tested |
|----------|-------|--------------|
| Health checks | 5 | OK, DOWN, DEGRADED, exception, alert emission |
| Fill detection | 3 | PENDING_LONG fill, PENDING_SHORT fill, no fill when no position |
| Broker close detection | 3 | LONG close, SHORT close, no close when position exists |
| Stale pending | 1 | Stale pending → force_flat |
| Orphan detection | 2 | Orphan position, orphan pending order |
| Consistent state | 2 | FLAT no broker, LONG with matching position |
| Daily reset | 3 | New day reset, no double reset, reset doesn't clear halt |
| Risk action execution | 5 | Cancel, flatten, no action when not halted, cancel failure, flatten failure |
| Risk alerts | 2 | Halt emits CRITICAL, normal emits nothing |
| Daily summary | 5 | Structure, to_dict, file write, fill counting, reset tracking |
| Evidence recording | 3 | Health recorded, monitoring event recorded, fill event in evidence |
| Data classes | 5 | CycleResult OK, failed recon, to_dict, alert to_dict, summary to_dict |
| Degraded capability | 3 | Recon skipped on health failure, positions failure, orders failure graceful |
| PendingOrder model | 2 | Creation, with SL/TP |

---

## 3. Decisions Made

| # | Decision | Rationale |
|---|----------|-----------|
| D-P7-1 | OpsMonitor is a class, not an event loop | Keeps monitoring testable and caller-agnostic. The calling mechanism (webhook server, cron, async loop) is a Phase 8 concern. |
| D-P7-2 | Reconciliation runs only when health is OK | If the adapter is DOWN, reconciliation would produce false positives. Safe degradation. |
| D-P7-3 | Fill detection matches by presence, not ID | IRB has max 1 position. If agent is PENDING and broker has a position, that's the fill. No need for order-to-position ID matching. |
| D-P7-4 | Broker close P&L is 0.0 placeholder | MetaApi `get_positions()` doesn't include closed positions. Accurate P&L requires deal history API access (deferred). |
| D-P7-5 | `force_flat()` added to Trading Agent | `notify_broker_close()` rejects calls in PENDING state. Stale pending reconciliation needs a different path. Minimal, documented escape hatch. |
| D-P7-6 | `get_orders()` is non-abstract with default | Backward-compatible: existing adapters don't break. Only MetaApiAdapter overrides it. |
| D-P7-7 | Daily summary is file-based JSON | Simple, auditable, no infrastructure dependency. Dashboard is a later-phase concern. |
| D-P7-8 | Alert severity determines log level | CRITICAL → `log.critical()`, WARNING → `log.warning()`, INFO → `log.info()`. Natural integration with existing logging. |

---

## 4. Changes Made

| File | Change | Type |
|------|--------|------|
| `novatrade/models.py` | Added `EvidenceType.MONITORING`, `PendingOrder` dataclass | Extension |
| `novatrade/adapter/base.py` | Added `get_orders()` with default empty implementation | Extension |
| `novatrade/adapter/metaapi_provider.py` | Added `cancel_order()`, `get_orders()`, `_translate_pending_order()` | Completion |
| `novatrade/execution/trading_agent.py` | Added `force_flat(reason)` method | Extension |
| `novatrade/monitor/ops_monitor.py` | **NEW** — Full operational monitoring layer | Core deliverable |
| `tests/test_ops_monitor.py` | **NEW** — 44 tests for monitoring/reconciliation | Tests |
| `docs/demo_test_run/alerts_policy.md` | **NEW** — Alert categories, severities, triggers | Documentation |
| `docs/demo_test_run/daily_summary_report.md` | **NEW** — Daily summary format and example | Documentation |
| `docs/demo_test_run/phase7_assumptions.md` | **NEW** — 10 assumptions (MO-1 to MO-10) | Documentation |
| `docs/demo_test_run/phase7_open_issues.md` | **NEW** — 1 blocker, 4 warnings, 1 must-fix, 4 deferred | Documentation |
| `docs/demo_test_run/phase7_summary.md` | **NEW** — This file | Documentation |

---

## 5. Blockers Resolved

| ID | Summary | How Resolved |
|----|---------|-------------|
| B-P5-1 | MetaApiAdapter missing `cancel_order()` | Implemented in MetaApiAdapter using MetaApi SDK |
| B-P5-2 | No monitoring layer for fill/close detection | OpsMonitor reconciliation with `notify_fill()` / `notify_broker_close()` callbacks |
| B-P6-1 | No automated daily drawdown reset | OpsMonitor `_check_daily_reset()` at midnight UTC boundary |
| B-P6-2 | FLATTEN/CANCEL actions advisory only | OpsMonitor `execute_pending_actions()` executes via adapter |

---

## 6. What Remains Advisory or Manual

| Capability | Status |
|-----------|--------|
| `SUSPEND_STRATEGY` action | Advisory only — not used in IRB single-strategy setup |
| Orphan position resolution | Manual review required — operator must close/claim via FTMO dashboard |
| Orphan pending order resolution | Manual review required — operator decides to cancel or let expire |
| Broker close P&L | Placeholder (0.0) — accurate P&L requires deal history API |
| Risk engine resume after halt | Operator-only — monitoring layer does NOT auto-resume |

---

## 7. How Reconciliation Works

1. Each monitoring cycle fetches broker state (`get_positions()`, `get_orders()`)
2. Compares against Trading Agent internal state (`agent.state`, `agent.pending_order_id`, `agent.position_id`)
3. Detects fill, close, stale pending, orphan, or consistent state
4. Takes corrective action where safe (notify_fill, notify_broker_close, force_flat)
5. Emits alerts for manual-review cases (orphan positions/orders)
6. Records all observations and actions to evidence trail

---

## 8. How Daily Reset Works

1. Each monitoring cycle checks `today_iso()` vs `last_reset_date`
2. On new calendar day (UTC): fetches `adapter.get_account().equity`
3. Calls `risk_engine.reset_daily(equity)` — resets daily drawdown counters
4. Records `DAILY_RESET` event to evidence trail
5. Does NOT clear halt state (per kill_switch_policy.md §7)
6. Updates `last_reset_date` to prevent double reset

---

## 9. Recommended Next Steps for Fresh IRB Phase 8

### Immediate (before demo run)

1. **Resolve B-P7-1**: Build a monitoring cycle caller (webhook server with `run_cycle()` loop, or cron/systemd timer)
2. **Resolve B-IRB-1**: Compile strategy.pine in TradingView
3. **Resolve B-IRB-2**: Run TradingView backtest, verify signal generation
4. **Wire end-to-end**: TradingView webhook → Trading Agent → Risk Engine → MetaApi → FTMO

### For demo run launch

5. **Set IRB config**: Enable `check_forex_session=True`, `irb_max_open_positions=1`
6. **Run controlled dry test**: Send test alerts through the full stack
7. **Verify monitoring**: Confirm fills detected, closes detected, daily reset works
8. **Verify risk governance**: Confirm halt blocks trades, actions execute

### Recommended Phase 8 prompt focus

- Webhook server implementation (FastAPI/Starlette receiving TradingView POST)
- Monitoring loop integration (call `run_cycle()` every 30-60 seconds)
- End-to-end integration test (test alert → intent → risk check → execution → fill detection → close detection)
- Controlled dry-run checklist
- Launch readiness assessment

---

## 10. Final Statement

Fresh IRB Phase 7 complete — ready for Fresh IRB Phase 8.

All Phase 5/6 operational blockers resolved. The IRB demo-run stack is now observable, state-consistent, and capable of enforcing risk-governance outcomes. Phase 8 needs to provide the monitoring cycle caller and wire the end-to-end integration.

---

**Phase 7 complete — Operational monitoring layer built with 44 new tests. 4 blockers resolved.**

STOPPED AT FRESH IRB PHASE 7 — NO LATER PHASE WORK PERFORMED
