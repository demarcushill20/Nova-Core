# NovaTrade Demo Launch Runbook

**Phase:** Final Demo Launch (Phase 9)
**Date:** 2026-03-17
**Strategy:** Rob Hoffman IRB v2.0.0
**Stack:** Trading Agent + Risk Engine + OpsMonitor + MetaApiAdapter

---

## 1. Prerequisites

Before starting, confirm all of the following:

| # | Prerequisite | How to verify |
|---|-------------|---------------|
| 1 | Pine script compiles in TradingView | Load `strategy.pine` in TradingView chart editor, verify no compilation errors |
| 2 | TradingView backtest shows IRB signals | Run Strategy Tester on EURUSD H1, verify signals fire |
| 3 | FTMO Free Trial account active | Log into FTMO dashboard, verify demo account is active |
| 4 | MetaApi account deployed | Check MetaApi dashboard, verify account status is DEPLOYED |
| 5 | VPS has TLS proxy configured | `curl -s https://<your-domain>/health` returns 200 |
| 6 | All environment variables set | See Section 2 below |

---

## 2. Required Environment Variables

```bash
# --- Core ---
NOVATRADE_LAUNCH_MODE=active_ready     # Start in active_ready, upgrade to active_demo after verification
NOVATRADE_DRY_RUN=false                # Required for active modes
NOVATRADE_WEBHOOK_SECRET=<strong-random-secret>  # Must match TradingView webhook header

# --- MetaApi ---
METAAPI_TOKEN=<your-metaapi-token>
METAAPI_ACCOUNT_ID=<your-metaapi-account-id>
METAAPI_REGION=london                  # Or new-york, depending on account

# --- FTMO Profile ---
FTMO_ENABLED=true
FTMO_CHALLENGE_TYPE=free_trial
FTMO_CAMPAIGN_LABEL=ftmo-free-trial-march-2026
FTMO_ACCOUNT_SIZE=100000

# --- External Confirmations (set true after verifying each) ---
NOVATRADE_CONFIRM_PINE_COMPILED=true
NOVATRADE_CONFIRM_TV_BACKTEST=true
NOVATRADE_CONFIRM_WEBHOOK_URL=true
NOVATRADE_CONFIRM_ACTIVE_DEMO=false    # Set true only at final activation

# --- Optional ---
NOVATRADE_PORT=8877
NOVATRADE_HOST=0.0.0.0
NOVATRADE_MONITOR_INTERVAL=60
```

---

## 3. Launch Procedure

### Phase A: Start in active_ready Mode

```bash
# 1. Set environment variables (see Section 2)
source /etc/novacore/novatrade.env

# 2. Start the server
NOVATRADE_LAUNCH_MODE=active_ready python -m novatrade.runtime.runner

# 3. Check startup readiness report (printed to log)
# Look for: LAUNCH READINESS ASSESSMENT
# Expected: CONDITIONALLY_READY (pending NOVATRADE_CONFIRM_ACTIVE_DEMO)

# 4. Verify health endpoint
curl -s http://localhost:8877/health | python -m json.tool
# Expected: status=ok, launch_mode=active_ready, adapter_type=MetaApiAdapter

# 5. Verify status endpoint
curl -s http://localhost:8877/status | python -m json.tool
# Expected: runtime_mode=active_ready, trading_agent.state=FLAT

# 6. Check readiness gate
curl -s http://localhost:8877/readiness | python -m json.tool
# Review all checks — identify any remaining failures
```

### Phase B: First-Live-Check Procedure

```bash
# 7. Configure TradingView alert to POST to:
#    https://<your-domain>/webhook/alert
#    Header: X-Webhook-Secret: <your-secret>
#    Body: {{strategy.order.alert_message}}

# 8. Wait for first TradingView alert
#    Monitor server logs for: WEBHOOK_RECEIVED
#    Expected: alert is received, validated, and processed

# 9. Verify alert was processed correctly
curl -s http://localhost:8877/status | python -m json.tool
# Check: webhook.alerts_received >= 1

# 10. Run a monitor cycle manually (if needed)
#     The MonitorLoop runs automatically every 60s
#     Check logs for: MONITOR_CYCLE_COMPLETE

# 11. Trigger a daily summary to verify the pipeline
curl -s -X POST http://localhost:8877/control/summary | python -m json.tool
```

### Phase C: Activate Demo Mode

```bash
# 12. Once satisfied with Phase B observations:
export NOVATRADE_CONFIRM_ACTIVE_DEMO=true

# 13. Restart with active_demo mode
NOVATRADE_LAUNCH_MODE=active_demo python -m novatrade.runtime.runner

# 14. Verify launch gate passes
curl -s http://localhost:8877/readiness | python -m json.tool
# Expected: verdict=READY_FOR_ACTIVE_DEMO

# 15. Monitor first 24 hours
#     Check: /health every 15 minutes initially
#     Check: /status for alert counts and agent state
#     Review: evidence.jsonl for complete audit trail
#     Review: daily summary output
```

---

## 4. First-24-Hour Monitoring

| Time | Action |
|------|--------|
| T+0 | Verify /health returns ok, launch_mode=active_demo |
| T+15m | Check /status — verify first monitor cycles completed |
| T+1h | Verify at least one TradingView alert received (if market is open) |
| T+4h | Check evidence.jsonl for any RISK_HALT or ADAPTER_ERROR events |
| T+8h | Trigger /control/summary — review daily progress |
| T+12h | Check for any reconciliation mismatches in monitor logs |
| T+24h | Full review: daily summary, evidence trail, risk status |

**Success criteria for first 24 hours:**
- No ADAPTER_ERROR events
- No unexpected RISK_HALT events
- Monitor cycles completing without failures
- If IRB signals fire: proper state transitions observed
- Daily summary generates cleanly
- No orphan orders or reconciliation mismatches

---

## 5. Rollback / Return to Dry-Run

### Immediate Rollback (runtime endpoint)

```bash
# Emergency rollback via API
curl -s -X POST http://localhost:8877/control/rollback | python -m json.tool
# This immediately switches to DryRunAdapter
# No real broker operations will occur after this
```

### Restart in Dry-Run Mode

```bash
# Stop the server (Ctrl+C or kill)

# Restart in dry-run
NOVATRADE_LAUNCH_MODE=dry_run python -m novatrade.runtime.runner
```

### When to Rollback

Rollback immediately if any of these occur:
1. Risk engine triggers HALT
2. Adapter returns repeated errors (check /health for degraded status)
3. Orphan orders detected in reconciliation
4. Unexpected state transitions in Trading Agent
5. Any unrecognized behavior in evidence.jsonl

See `rollback_plan.md` for detailed rollback procedures.

---

## 6. Post-Launch Review

After the first 24 hours, review:

1. **Evidence trail** (`OUTPUT/novatrade/evidence.jsonl`)
   - All events recorded?
   - Any errors or warnings?

2. **Daily summary** (`OUTPUT/novatrade/daily_summary_*.json`)
   - Cycles run vs cycles failed
   - Alerts processed vs rejected
   - Risk decisions

3. **Launch decision:**
   - If first 24h is clean: continue active_demo operation
   - If issues found: revert to dry_run, investigate, re-launch

---

STOPPED AFTER FINAL DEMO-LAUNCH PHASE — NO FURTHER PHASE WORK PERFORMED
