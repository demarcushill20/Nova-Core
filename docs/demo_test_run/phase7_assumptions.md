# NovaTrade Demo Test Run — Phase 7 Assumptions

**Phase:** 7 (Monitoring, Reconciliation, and Operational Safety)
**Date:** 2026-03-17
**Agent:** Monitoring / Reconciliation Layer
**Strategy:** Rob Hoffman IRB v2.0.0

---

## Assumptions

| ID | Statement | Rationale | Risk | Revisit |
|----|-----------|-----------|------|---------|
| MO-1 | **Monitoring cycle is called externally.** The OpsMonitor does not run its own event loop. A webhook server, cron job, or manual invocation must call `run_cycle()` at regular intervals. | Keeps the monitor stateless and testable. Avoids building a scheduler framework. | MEDIUM — if no caller exists, monitoring does not run. | Yes — Phase 8 must provide the caller (webhook server or cron). |
| MO-2 | **IRB max 1 position at a time.** Reconciliation assumes at most 1 open position and 1 pending order. Multi-position scenarios are treated as orphan/mismatch. | Strategy spec §4.4 mandates max 1 simultaneous position. Risk engine Layer 3 enforces this. | LOW — enforced at multiple levels. | No, unless strategy changes. |
| MO-3 | **Broker position disappearance means SL/trailing stop hit.** When the monitoring layer detects a position closed at the broker, it attributes the exit to "BROKER_CLOSE". | Pine does not emit alerts for SL exits. The broker closes the position autonomously when SL is hit. | LOW — this is the expected behavior per execution_state_machine.md §2.2. | No. |
| MO-4 | **P&L from broker close is not available at detection time.** When reconciliation detects a broker close, it records pnl_usd=0.0 as placeholder. Real P&L must be derived from account equity delta. | The monitoring layer sees that the position is gone but doesn't know the exact close price or P&L. MetaApi `get_positions()` only returns currently open positions. | MEDIUM — risk engine equity tracking may drift from reality if P&L is consistently zero. | Yes — consider fetching deal history from MetaApi for accurate P&L. |
| MO-5 | **Daily reset timing is midnight UTC.** The daily drawdown counter resets when the monitoring cycle detects a new calendar day (UTC). | FTMO defines the trading day boundary at midnight UTC. | LOW — standard industry convention. | No. |
| MO-6 | **Daily reset does NOT clear halt.** Per kill_switch_policy.md §7, a drawdown breach halt persists until explicit operator resume. | Prevents automated recovery from masking real problems. | LOW — by design. | No. |
| MO-7 | **get_orders() may not be available on all adapters.** The base adapter returns an empty list by default. If the concrete adapter doesn't override it, pending order reconciliation degrades gracefully. | Backward-compatible design. MetaApiAdapter now implements it, but other adapters may not. | LOW — graceful degradation. | No. |
| MO-8 | **cancel_order() may fail for already-expired orders.** If a pending order has already expired at the broker, the cancel call may return an error. This is logged but not treated as a critical failure. | MetaApi may reject cancel attempts for non-existent orders. | LOW — expected edge case. | No. |
| MO-9 | **Flatten action uses market close.** When the risk engine directs FLATTEN_POSITIONS, the monitoring layer calls `adapter.close_position()` which executes at market price. Slippage is accepted. | Emergency flatten prioritizes risk reduction over execution quality. | LOW — acceptable for risk management. | No. |
| MO-10 | **Evidence trail is the single source of operational truth.** All monitoring events, alerts, actions, and state changes are recorded to `evidence.jsonl`. The daily summary aggregates from in-memory counters, not from re-reading the evidence file. | In-memory counters are faster and simpler. Evidence file is the durable record for post-hoc analysis. | LOW — counters and evidence may diverge if the process crashes mid-cycle. | Yes — consider reconciling counters with evidence on restart. |

---

## Summary

| Risk Level | Count |
|-----------|-------|
| HIGH | 0 |
| MEDIUM | 2 (MO-1, MO-4) |
| LOW | 8 |

Key risks to address before demo run:
- **MO-1**: A monitoring cycle caller must exist (webhook server loop or cron)
- **MO-4**: Broker close P&L accuracy depends on deal history access

---

STOPPED AT FRESH IRB PHASE 7 — NO LATER PHASE WORK PERFORMED
