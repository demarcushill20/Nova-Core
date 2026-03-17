# NovaTrade Demo Test Run — Phase 8 Open Issues

**Phase:** 8 (Controlled Dry Runs and Launch Readiness)
**Date:** 2026-03-17
**Agent:** Runtime Caller / Dry-Run Orchestration Layer
**Strategy:** Rob Hoffman IRB v2.0.0

---

## Blockers

| ID | Summary | Resolution Path |
|----|---------|----------------|
| B-P8-1 | **TradingView webhook URL not configured.** The server exists but TradingView alerts are not yet pointed at it. | Operator deploys server with TLS, configures TradingView alert webhook URL. |
| B-P8-2 | **MetaApi credentials not set.** Real adapter requires METAAPI_TOKEN and METAAPI_ACCOUNT_ID. | Operator sets credentials in environment before switching to MetaApiAdapter. |

## Inherited Blockers

| ID | Summary | Status |
|----|---------|--------|
| B-IRB-1 | Pine compilation not verified in TradingView | Still open — pre-launch condition |
| B-IRB-2 | No live backtest executed | Still open — pre-launch condition |

## Resolved Blockers

| ID | Summary | Resolution |
|----|---------|-----------|
| B-P7-1 | No monitoring cycle caller exists | **RESOLVED.** MonitorLoop calls `OpsMonitor.run_cycle()` at configurable intervals (default 60s). |

---

## Warnings

| ID | Summary | Mitigation |
|----|---------|------------|
| P8-W-1 | **No TLS in Phase 8.** The webhook server listens on plain HTTP. | Deploy behind nginx reverse proxy with TLS before production. |
| P8-W-2 | **Webhook secret is optional.** If not set, any client can POST alerts. | Must set NOVATRADE_WEBHOOK_SECRET before production deployment. |
| P8-W-3 | **Phase 8 runner always forces DryRunAdapter.** Cannot use real adapter without modifying runner.py. | Intentional safety — final launch phase must update runner to support MetaApiAdapter. |
| P8-W-4 | **Evidence writes are synchronous.** High-frequency production use may cause I/O latency. | Acceptable for demo run volumes. Consider async writes for production scaling. |

---

## Must-Fix Before Final Demo Launch Phase

| ID | Summary |
|----|---------|
| MF-P8-1 | Update `runner.py` to support MetaApiAdapter selection (not just DryRunAdapter). |
| MF-P8-2 | Deploy with TLS (nginx reverse proxy or equivalent). |
| MF-P8-3 | Set NOVATRADE_WEBHOOK_SECRET in production environment. |
| MF-P8-4 | Set MetaApi credentials (METAAPI_TOKEN, METAAPI_ACCOUNT_ID) in environment. |

---

## Deferred to Later Phase

| ID | Summary |
|----|---------|
| D-P8-1 | **Production authentication.** Phase 8 uses shared secret. Production may need API key management, rate limiting, or IP whitelisting. |
| D-P8-2 | **Process supervision.** Phase 8 runs as a single process. Production should use systemd or supervisor for restart resilience. |
| D-P8-3 | **Async evidence writes.** Batched or async evidence recording for high-frequency production use. |
| D-P8-4 | **Metrics and alerting.** Prometheus/Grafana integration for production observability. |
| D-P8-5 | **Multi-symbol support.** Phase 8 handles only EURUSD. Multi-symbol would need routing logic. |

---

## Summary

| Severity | Count |
|----------|-------|
| Blocker (new) | 2 (B-P8-1, B-P8-2 — deployment/credential, not code) |
| Blocker (inherited) | 2 (B-IRB-1, B-IRB-2) |
| Blocker (resolved) | 1 (B-P7-1: monitoring cycle caller) |
| Warning | 4 |
| Must-fix before launch | 4 |
| Deferred | 5 |

**No code blockers.** All open blockers are deployment/configuration tasks requiring operator action.

---

STOPPED AT FRESH IRB PHASE 8 — NO LATER PHASE WORK PERFORMED
