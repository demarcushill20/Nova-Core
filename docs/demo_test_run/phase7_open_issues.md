# NovaTrade Demo Test Run — Phase 7 Open Issues

**Phase:** 7 (Monitoring, Reconciliation, and Operational Safety)
**Date:** 2026-03-17
**Agent:** Monitoring / Reconciliation Layer
**Strategy:** Rob Hoffman IRB v2.0.0

---

## Blockers

| ID | Summary | Resolution Path |
|----|---------|----------------|
| B-P7-1 | **No monitoring cycle caller exists.** OpsMonitor.run_cycle() must be called externally by a webhook server, async loop, or cron job. Without a caller, monitoring does not run and fills/closes are not detected. | Build a thin webhook server or async loop in Phase 8 that calls `run_cycle()` every 30-60 seconds during trading hours. Alternatively, use a systemd timer or cron. |

## Inherited Blockers

| ID | Summary | Status |
|----|---------|--------|
| B-IRB-1 | Pine compilation not verified in TradingView | Still open — pre-launch condition |
| B-IRB-2 | No live backtest executed | Still open — pre-launch condition |

## Resolved Blockers

| ID | Summary | Resolution |
|----|---------|-----------|
| B-P5-1 | MetaApiAdapter missing `cancel_order()` | **RESOLVED.** MetaApiAdapter now implements `cancel_order()` via MetaApi SDK `cancel_order()`. |
| B-P5-2 | No monitoring layer to detect fills and broker closes | **RESOLVED.** OpsMonitor reconciliation detects fills (→ `notify_fill()`) and broker closes (→ `notify_broker_close()`). |
| B-P6-1 | No automated daily drawdown reset caller | **RESOLVED.** OpsMonitor `_check_daily_reset()` calls `risk_engine.reset_daily(equity)` at midnight UTC boundary detection. |
| B-P6-2 | FLATTEN/CANCEL actions are advisory only | **RESOLVED.** OpsMonitor `execute_pending_actions()` executes CANCEL_PENDING and FLATTEN_POSITIONS via adapter calls. |

---

## Warnings

| ID | Summary | Mitigation |
|----|---------|------------|
| P7-W-1 | **Broker close P&L is zero.** When reconciliation detects a broker-side close, `pnl_usd=0.0` is passed to `on_trade_close()` because the exact P&L is not available from `get_positions()` (position is gone). | Risk engine equity tracking may drift. Mitigate by periodically syncing equity from `get_account()`. Consider fetching MetaApi deal history for accurate P&L. |
| P7-W-2 | **force_flat() is a reconciliation escape hatch.** Added `TradingAgent.force_flat()` method to handle stale pending orders. This is the only non-alert-driven state transition. | Documented as reconciliation-only. Every call is recorded to evidence trail with reason. |
| P7-W-3 | **get_orders() default is empty list.** If an adapter doesn't implement `get_orders()`, pending order reconciliation degrades — orphan pending orders at the broker may not be detected. | MetaApiAdapter implements `get_orders()`. Other future adapters should too. |
| P7-W-4 | **In-memory daily summary counters reset on process restart.** If the monitoring process restarts mid-day, daily summary counters start from zero. | Evidence trail preserves the durable record. Daily summary is a convenience aggregation. |

---

## Must-Fix Before Fresh IRB Phase 8

| ID | Summary |
|----|---------|
| MF-P7-1 | Build a monitoring cycle caller (webhook server loop, async runner, or cron) that invokes `OpsMonitor.run_cycle()` at regular intervals. Without this, Phase 7 monitoring code exists but never runs. |

---

## Deferred to Later Phase

| ID | Summary |
|----|---------|
| D-P7-1 | **Broker deal history for accurate P&L.** MetaApi provides `get_deals_by_position()` or `get_deal()` which could give exact close price and P&L. Not needed for Phase 7 MVP. |
| D-P7-2 | **Persistent daily summary counters.** Back the in-memory counters with a file or database to survive process restarts. Low priority for demo run. |
| D-P7-3 | **Alert notification channel.** Currently alerts are logged and written to evidence. A Telegram notification or webhook push would make CRITICAL alerts more visible. |
| D-P7-4 | **Reconciliation position ID matching.** Current reconciliation matches by symbol+side (IRB max 1 position). Multi-position strategies would need position ID-based matching. |

---

## Summary

| Severity | Count |
|----------|-------|
| Blocker (new) | 1 (B-P7-1: monitoring cycle caller) |
| Blocker (inherited) | 2 (B-IRB-1, B-IRB-2) |
| Blocker (resolved) | 4 (B-P5-1, B-P5-2, B-P6-1, B-P6-2) |
| Warning | 4 |
| Must-fix before Phase 8 | 1 |
| Deferred | 4 |

**The sole new blocker** (B-P7-1) requires a thin caller (~20-30 lines) to invoke the monitoring cycle. All Phase 5/6 operational blockers are now resolved.

---

STOPPED AT FRESH IRB PHASE 7 — NO LATER PHASE WORK PERFORMED
