# NovaTrade Demo Test Run — Alerts Policy

**Phase:** 7 (Monitoring, Reconciliation, and Operational Safety)
**Date:** 2026-03-17
**Agent:** Monitoring / Reconciliation Layer

---

## 1. Alert Categories

| Category | Description |
|----------|-------------|
| **HEALTH** | Adapter connectivity, system liveness, account access |
| **RECONCILIATION** | State mismatches between Trading Agent and broker |
| **RISK** | Kill-switch activation, drawdown warnings |
| **ACTION** | Risk-directed cancel/flatten execution outcomes |
| **OPERATIONAL** | Daily reset, summary generation, system lifecycle |

---

## 2. Severity Levels

| Severity | Definition | Operator Action |
|----------|-----------|-----------------|
| **CRITICAL** | Immediate operator attention required. System safety may be compromised. | Operator must investigate within minutes. |
| **WARNING** | Important condition that may require intervention. Not immediately dangerous. | Operator should review within the hour. |
| **INFO** | Normal operational event. Logged for audit trail only. | No action required. Review in daily summary. |

---

## 3. Alert Trigger Conditions

### CRITICAL Alerts (Operator-Visible, Immediate)

| Trigger | Category | Condition |
|---------|----------|-----------|
| Adapter DOWN | HEALTH | `adapter.health_check()` returns `HealthState.DOWN` |
| Risk engine HALTED | RISK | `risk_engine.halted == True` (drawdown breach) |
| Orphan position at broker | RECONCILIATION | Agent is FLAT but broker has open position(s) |
| Flatten action failure | ACTION | `adapter.close_position()` returns `ok=False` |
| Health check exception | HEALTH | Unhandled exception during health snapshot |

### WARNING Alerts (Operator-Visible, Non-Immediate)

| Trigger | Category | Condition |
|---------|----------|-----------|
| Adapter degraded | HEALTH | `adapter.health_check()` returns `HealthState.DEGRADED` |
| Broker close detected | RECONCILIATION | Position disappeared from broker (SL/trailing stop hit) |
| Stale pending order | RECONCILIATION | Agent in PENDING state but no matching order/position at broker |
| Orphan pending order | RECONCILIATION | Agent is FLAT but broker has pending order(s) |
| Cancel action failure | ACTION | `adapter.cancel_order()` returns `ok=False` |
| Daily reset skipped | OPERATIONAL | Cannot fetch account equity for reset |
| Reconciliation exception | RECONCILIATION | Unhandled exception during reconciliation |

### INFO Alerts (Logged Only)

| Trigger | Category | Condition |
|---------|----------|-----------|
| Fill detected | RECONCILIATION | Pending order filled at broker → `notify_fill()` |
| Daily reset executed | OPERATIONAL | Daily drawdown counters reset at midnight UTC |
| Cycle complete | OPERATIONAL | One monitoring cycle finished (health + recon + actions) |

---

## 4. What Is Operator-Visible vs Logged-Only

### Operator-Visible
- All CRITICAL alerts
- All WARNING alerts
- Daily summary report

### Logged Only (Evidence Trail)
- INFO alerts
- Cycle completion records
- Health snapshots
- Reconciliation no-action results

---

## 5. Alert Evidence Format

All alerts are recorded to `evidence.jsonl` with event type `MONITORING`:

```json
{
  "event_type": "MONITORING",
  "data": {
    "monitoring_event": "ALERT",
    "severity": "CRITICAL",
    "category": "RISK",
    "message": "Risk engine HALTED: FTMO daily drawdown limit reached",
    "halt_reason": "FTMO daily drawdown limit reached"
  },
  "error": "",
  "timestamp": 1742212800.0
}
```

---

## 6. Alert Frequency

Alerts are generated per monitoring cycle. In a typical demo-run configuration:

- Health check: every cycle
- Reconciliation: every cycle (when health is OK)
- Risk check: every cycle
- Daily reset: once per calendar day (midnight UTC)

The monitoring cycle frequency is determined by the caller (webhook server loop, cron, or manual invocation). Recommended: every 30-60 seconds during active trading hours.

---

STOPPED AT FRESH IRB PHASE 7 — NO LATER PHASE WORK PERFORMED
