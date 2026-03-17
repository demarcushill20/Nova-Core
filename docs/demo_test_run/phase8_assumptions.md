# NovaTrade Demo Test Run — Phase 8 Assumptions

**Phase:** 8 (Controlled Dry Runs and Launch Readiness)
**Date:** 2026-03-17
**Agent:** Runtime Caller / Dry-Run Orchestration Layer
**Strategy:** Rob Hoffman IRB v2.0.0

---

## Assumptions

| ID | Statement | Rationale | Risk | Revisit |
|----|-----------|-----------|------|---------|
| RT-1 | **DryRunAdapter is sufficient safety for Phase 8.** All mutating broker operations are intercepted locally. No real orders can be placed, modified, or cancelled through the DryRunAdapter. | DryRunAdapter tracks orders/positions in memory and returns deterministic results. Code review confirms no path to real broker calls. | LOW — by design. | No. |
| RT-2 | **cfg.dry_run=False is safe when DryRunAdapter is used.** The pre_trade_gate's dry_run check prevents orders when cfg.dry_run=True. Setting it to False allows orders through to the DryRunAdapter, which is the actual safety net. | The DryRunAdapter intercepts `place_order()`, `cancel_order()`, etc. regardless of config flags. The config flag controls the risk gate, not the adapter. | LOW — separation of concerns is correct. | Yes — when switching to real adapter, cfg.dry_run must match intent. |
| RT-3 | **FastAPI is available and suitable for the webhook ingress.** The project already has FastAPI 0.135.1 and uvicorn 0.41.0 installed. | Verified at build time. FastAPI is lightweight, async-native, and well-tested. | LOW. | No. |
| RT-4 | **TradingView sends JSON POST payloads.** The webhook ingress expects `Content-Type: application/json` POST requests. TradingView webhook alerts are documented to send POST requests with the alert message as the body. | TradingView docs confirm POST delivery. The Pine script constructs JSON payloads via string concatenation. | LOW — standard TradingView behavior. | Yes — verify actual payload format on first real alert. |
| RT-5 | **Monitor loop interval of 60s is acceptable for dry-run.** Reconciliation every 60 seconds means fills/closes may be detected up to 60s after they occur. | For dry-run testing, timing is not critical. Production can use shorter intervals (30s or less). | LOW for dry-run. | Yes — production interval should be tuned based on observed fill latency. |
| RT-6 | **Single-process deployment is sufficient for Phase 8.** The webhook server and monitor loop run in the same async event loop. | Phase 8 is a controlled dry-run, not a production deployment. Single-process simplifies testing and debugging. | LOW for demo run. | Yes — production may need supervisor/systemd for restart resilience. |
| RT-7 | **Webhook secret is optional for dry-run.** Phase 8 dry-run does not need authentication because it runs on localhost or a protected network. | Dry-run testing doesn't need production security. | LOW for dry-run. MEDIUM for production. | Yes — must set NOVATRADE_WEBHOOK_SECRET before production deployment. |
| RT-8 | **Evidence recording is synchronous and acceptable.** The EvidenceRecorder writes to JSONL synchronously on each event. For dry-run volumes this is fine. | Dry-run generates low event volumes. File I/O is fast for append-only JSONL. | LOW for dry-run. | Yes — high-frequency production use may need async or batched writes. |
| RT-9 | **MonitorLoop cycle errors are non-fatal.** If a single cycle throws an exception, the loop logs the error and continues to the next cycle. | Transient errors (network blips, adapter reconnection) should not kill the monitoring process. | LOW — matches standard daemon behavior. | No. |
| RT-10 | **Phase 8 runner always uses DryRunAdapter.** Even if cfg.dry_run=False is passed, the runner enforces DryRunAdapter. Switching to a real adapter is a final-launch-phase concern. | Prevents accidental live trading during dry-run testing. Explicit code guard in runner.py. | LOW — by design. | Yes — final launch phase must modify runner to use MetaApiAdapter. |

---

## Summary

| Risk Level | Count |
|-----------|-------|
| HIGH | 0 |
| MEDIUM | 0 (2 noted as MEDIUM for production, LOW for dry-run) |
| LOW | 10 |

No high-risk assumptions. All are appropriate for a controlled dry-run phase.

---

STOPPED AT FRESH IRB PHASE 8 — NO LATER PHASE WORK PERFORMED
