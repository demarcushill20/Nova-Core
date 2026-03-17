# NovaTrade Demo Test Run — Phase 9 Open Issues

**Phase:** Final Demo Launch (Phase 9)
**Date:** 2026-03-17
**Agent:** Launch Gate / Activation Layer
**Strategy:** Rob Hoffman IRB v2.0.0

---

## Blockers

| ID | Summary | Category | Resolution Path |
|----|---------|----------|----------------|
| B-IRB-1 | **TradingView Pine compilation not confirmed.** | External confirmation | Operator loads strategy.pine in TradingView, verifies compilation. Sets `NOVATRADE_CONFIRM_PINE_COMPILED=true`. |
| B-IRB-2 | **TradingView backtest not confirmed.** | External confirmation | Operator runs Strategy Tester on EURUSD H1, verifies IRB signals fire. Sets `NOVATRADE_CONFIRM_TV_BACKTEST=true`. |
| B-P8-1 | **TradingView webhook URL not configured.** | Deployment | Operator deploys server with TLS, configures TradingView alert webhook URL. Sets `NOVATRADE_CONFIRM_WEBHOOK_URL=true`. |
| B-P8-2 | **MetaApi credentials not set.** | Configuration | Operator sets `METAAPI_TOKEN` and `METAAPI_ACCOUNT_ID` in environment. |

**All blockers are operator/deployment tasks — no code blockers.**

---

## Resolved Blockers

| ID | Summary | Resolution |
|----|---------|-----------|
| B-P7-1 | No monitoring cycle caller exists | **RESOLVED (Phase 8).** MonitorLoop calls `OpsMonitor.run_cycle()` at configurable intervals. |
| B-P9-1 | Phase 8 forced DryRunAdapter — no active adapter support | **RESOLVED (Phase 9).** Runner supports MetaApiAdapter via `NOVATRADE_LAUNCH_MODE=active_ready/active_demo`. |
| B-P9-2 | No launch gate / activation procedure | **RESOLVED (Phase 9).** Launch gate evaluates readiness at startup and via `/readiness` endpoint. |
| B-P9-3 | No rollback capability | **RESOLVED (Phase 9).** `/control/rollback` endpoint and `rollback_to_dry_run()` function. |

---

## Warnings

| ID | Summary | Mitigation |
|----|---------|------------|
| P9-W-1 | **No TLS in application layer.** The webhook server listens on plain HTTP. | Deploy behind nginx reverse proxy with TLS. |
| P9-W-2 | **Rollback does not close broker positions.** Open positions persist at the broker after rollback to DryRunAdapter. | Documented in rollback plan. Operator must manage broker-side positions manually. |
| P9-W-3 | **active_ready mode allows order placement.** Orders can be placed in active_ready if TradingView alerts arrive. | Intentional — enables first-live-check procedure. Risk engine governs all orders. |
| P9-W-4 | **Launch gate is stateless.** External confirmations are re-read from env vars on each evaluation, not persisted. | Acceptable — evidence trail records each evaluation. |
| P9-W-5 | **Single-process deployment.** Webhook server and monitor loop share one event loop. | Acceptable for demo volume. Production should use supervisor/systemd. |

---

## Must-Fix Before Active Demo Launch

| ID | Summary | Owner |
|----|---------|-------|
| MF-P9-1 | Deploy webhook server with TLS (nginx reverse proxy) | Operator |
| MF-P9-2 | Set NOVATRADE_WEBHOOK_SECRET | Operator |
| MF-P9-3 | Set MetaApi credentials (METAAPI_TOKEN, METAAPI_ACCOUNT_ID) | Operator |
| MF-P9-4 | Confirm Pine compilation (NOVATRADE_CONFIRM_PINE_COMPILED=true) | Operator |
| MF-P9-5 | Confirm TradingView backtest (NOVATRADE_CONFIRM_TV_BACKTEST=true) | Operator |
| MF-P9-6 | Configure TradingView alerts to POST to webhook URL | Operator |
| MF-P9-7 | Confirm webhook URL (NOVATRADE_CONFIRM_WEBHOOK_URL=true) | Operator |
| MF-P9-8 | Acknowledge active demo mode (NOVATRADE_CONFIRM_ACTIVE_DEMO=true) | Operator |

---

## Deferred to Later Phase

| ID | Summary |
|----|---------|
| D-P9-1 | **Production authentication.** Phase 9 uses shared webhook secret. Production may need API key management, rate limiting, or IP whitelisting. |
| D-P9-2 | **Process supervision.** Phase 9 runs as a single process. Production should use systemd or supervisor. |
| D-P9-3 | **Automatic position management on rollback.** Currently operator must manage broker positions manually after rollback. |
| D-P9-4 | **Continuous launch gate re-evaluation.** Gate is evaluated on demand, not continuously. |
| D-P9-5 | **Multi-symbol support.** Phase 9 handles only EURUSD. Multi-symbol would need routing logic. |
| D-P9-6 | **Metrics and alerting.** Prometheus/Grafana integration for production observability. |

---

## Summary

| Severity | Count |
|----------|-------|
| Blocker (external) | 4 (B-IRB-1, B-IRB-2, B-P8-1, B-P8-2 — all operator tasks) |
| Blocker (resolved) | 4 (B-P7-1, B-P9-1, B-P9-2, B-P9-3) |
| Warning | 5 |
| Must-fix before launch | 8 (all operator tasks) |
| Deferred | 6 |

**No code blockers. All open blockers are deployment/configuration/confirmation tasks requiring operator action.**

---

STOPPED AFTER FINAL DEMO-LAUNCH PHASE — NO FURTHER PHASE WORK PERFORMED
