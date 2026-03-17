# NovaTrade Rollback Plan

**Phase:** Final Demo Launch (Phase 9)
**Date:** 2026-03-17
**Strategy:** Rob Hoffman IRB v2.0.0

---

## 1. Trigger Conditions

Rollback to dry-run mode if ANY of the following occur:

| # | Condition | Severity | Detection |
|---|-----------|----------|-----------|
| R-1 | Risk engine triggers HALT | CRITICAL | `/health` shows `risk_halted: true` |
| R-2 | Adapter returns ConnectionError 3+ times | HIGH | Monitor logs: `MONITOR_CYCLE_FAILED` |
| R-3 | Orphan pending order detected | HIGH | Monitor logs: `ORPHAN_PENDING` reconciliation action |
| R-4 | Orphan position detected | HIGH | Monitor logs: `UNEXPECTED` mismatch type |
| R-5 | Webhook returns 5xx errors repeatedly | MEDIUM | Webhook logs or external monitoring |
| R-6 | Risk gate denying all orders unexpectedly | MEDIUM | Evidence trail: continuous `DENY` verdicts |
| R-7 | Agent stuck in unexpected state | MEDIUM | `/status` shows agent in wrong state for >1h |
| R-8 | MetaApi credential expiry | HIGH | Adapter health check returns authentication error |
| R-9 | Unexpected broker behavior | HIGH | Positions or orders appear that don't match agent state |
| R-10 | Operator discretion | ANY | Operator decides to pause for any reason |

---

## 2. Immediate Rollback Steps

### Step 1: API Rollback (fastest — no restart required)

```bash
curl -s -X POST http://localhost:8877/control/rollback | python -m json.tool
```

This immediately:
- Replaces MetaApiAdapter with DryRunAdapter
- Sets launch_mode to dry_run
- Records ROLLBACK_TO_DRY_RUN in evidence trail
- All subsequent orders go to DryRunAdapter (no real broker calls)

### Step 2: Verify Rollback

```bash
curl -s http://localhost:8877/health | python -m json.tool
# Verify: dry_run=true, adapter_type=DryRunAdapter, launch_mode=dry_run

curl -s http://localhost:8877/status | python -m json.tool
# Verify: runtime_mode=dry_run
```

### Step 3: Handle Open Positions (if any)

If the agent has an open position at rollback time:
1. Check FTMO account directly via MetaApi dashboard
2. The position was placed via the real adapter — it still exists at the broker
3. Manually manage the position via FTMO/MetaApi dashboard
4. The DryRunAdapter will NOT see or manage real broker positions

**Important:** Rollback to DryRunAdapter does NOT close positions at the broker. The broker-side position persists independently.

---

## 3. Restart Rollback (full restart)

If the API rollback is insufficient:

```bash
# 1. Stop the server
kill $(pgrep -f "novatrade.runtime.runner")
# Or Ctrl+C if running in foreground

# 2. Restart in dry-run mode
NOVATRADE_LAUNCH_MODE=dry_run python -m novatrade.runtime.runner

# 3. Verify
curl -s http://localhost:8877/health | python -m json.tool
```

---

## 4. Risk Halt Procedure

If the Risk Engine halts (R-1):

```bash
# 1. Check halt reason
curl -s http://localhost:8877/status | python -m json.tool
# Look at: risk_engine.halt_reason

# 2. Rollback to dry-run
curl -s -X POST http://localhost:8877/control/rollback | python -m json.tool

# 3. Investigate
# Check evidence.jsonl for RISK_HALT events
# Review daily drawdown, position limits, or other risk triggers

# 4. To resume after clearing halt:
# Must restart the server — risk halt clear requires re-initialization
```

---

## 5. Adapter Failure Procedure

If MetaApiAdapter fails repeatedly (R-2, R-8):

```bash
# 1. Check health
curl -s http://localhost:8877/health | python -m json.tool
# Look for: status=degraded or adapter errors in logs

# 2. Rollback immediately
curl -s -X POST http://localhost:8877/control/rollback | python -m json.tool

# 3. Check MetaApi dashboard for:
#    - Account deployment status
#    - API token validity
#    - Service outages

# 4. If MetaApi service issue: wait for resolution, then re-launch
# 5. If credential issue: update env vars and restart
```

---

## 6. Orphan State Procedure

If orphan orders/positions detected (R-3, R-4, R-9):

```bash
# 1. Rollback to dry-run
curl -s -X POST http://localhost:8877/control/rollback | python -m json.tool

# 2. Check broker state directly
# Use MetaApi dashboard or FTMO account dashboard
# List all open positions and pending orders

# 3. Compare with agent state
curl -s http://localhost:8877/status | python -m json.tool
# Check trading_agent.state, pending_order_id, position_id

# 4. Manually resolve discrepancies
# Cancel orphan pending orders via broker dashboard
# Close orphan positions via broker dashboard

# 5. Reset agent state by restarting server
```

---

## 7. Evidence Review After Rollback

After any rollback, review the evidence trail:

```bash
# View recent evidence events
tail -20 OUTPUT/novatrade/evidence.jsonl | python -m json.tool

# Search for errors
grep '"error"' OUTPUT/novatrade/evidence.jsonl | python -m json.tool

# Search for rollback events
grep 'ROLLBACK' OUTPUT/novatrade/evidence.jsonl | python -m json.tool

# Generate daily summary
curl -s -X POST http://localhost:8877/control/summary | python -m json.tool
```

---

## 8. Re-Launch After Rollback

To re-launch active mode after a rollback:

1. Identify and fix the root cause
2. Verify fix in dry-run mode first
3. Follow the Launch Procedure in `demo_launch_runbook.md` from Phase A
4. Monitor more closely during the first hour after re-launch

---

## 9. Escalation

If rollback does not resolve the issue:

1. **Stop the server completely**
2. **Check FTMO account via dashboard** — manage any open positions manually
3. **Review full evidence trail** — `OUTPUT/novatrade/evidence.jsonl`
4. **Do NOT restart in active mode** until root cause is identified
5. **Dry-run mode is always safe** — use it for investigation

---

STOPPED AFTER FINAL DEMO-LAUNCH PHASE — NO FURTHER PHASE WORK PERFORMED
