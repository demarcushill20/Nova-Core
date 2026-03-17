# NovaTrade Demo Test Run — Phase 9 Assumptions

**Phase:** Final Demo Launch (Phase 9)
**Date:** 2026-03-17
**Agent:** Launch Gate / Activation Layer
**Strategy:** Rob Hoffman IRB v2.0.0

---

## Assumptions

| ID | Statement | Rationale | Risk | Revisit |
|----|-----------|-----------|------|---------|
| LG-1 | **Environment variables are sufficient for launch-mode selection.** `NOVATRADE_LAUNCH_MODE` controls the runtime mode. No CLI flags or config files are needed for mode selection. | Env vars are the established pattern for NovaTrade config (MetaApi, FTMO, risk). Consistent with deployment via systemd EnvironmentFile. | LOW. | No. |
| LG-2 | **External confirmations are tracked via env vars.** Operator sets `NOVATRADE_CONFIRM_*` env vars to `true` after verifying each external dependency. The system does not independently verify TradingView state. | NovaTrade cannot programmatically access TradingView's compilation or backtest results. Env vars provide a simple, auditable confirmation mechanism. | LOW — operator responsibility. | Yes — could add webhook-delivery verification in a later phase. |
| LG-3 | **MetaApiAdapter is production-ready for demo use.** The adapter was built and tested in Phase 2-3. Phase 9 relies on it for active modes without modification. | MetaApiAdapter passes its unit tests. Real-world behavior is verified during first-live-check procedure. | MEDIUM — real broker behavior may differ from mocks. | Yes — monitor closely during first 24h. |
| LG-4 | **DryRunAdapter is always safe to roll back to.** Emergency rollback replaces the active adapter with DryRunAdapter at runtime. This is safe because DryRunAdapter intercepts all mutating operations. | Code review confirms DryRunAdapter never touches a real broker. Rollback is a single-step operation. | LOW. | No. |
| LG-5 | **Rollback does not close broker-side positions.** When rolling back to DryRunAdapter, any open positions at the broker persist. The operator must manage them via the broker dashboard. | This is by design — automatic position closing during an emergency could cause worse outcomes than leaving positions open. | MEDIUM — operator must be aware. | No — documented in rollback plan. |
| LG-6 | **The launch gate is evaluated at startup only.** The gate runs once during `build_stack()` and is available via `/readiness` on demand. It does not continuously re-evaluate. | Continuous re-evaluation would add complexity without benefit — the external confirmations don't change at runtime. | LOW. | Yes — could add periodic re-evaluation if needed. |
| LG-7 | **`active_ready` mode is a safe observation mode.** In active_ready, the MetaApiAdapter is connected but the operator has not yet confirmed full activation. Orders can still be placed if alerts arrive — this is intentional for the first-live-check procedure. | active_ready exists to verify the adapter works before committing to active_demo. The risk engine still governs all orders. | LOW — risk engine is the safety net. | Yes — could add an explicit order-blocking flag for active_ready if needed. |
| LG-8 | **cfg.dry_run=False for all modes.** Phase 9 sets cfg.dry_run=False for all three launch modes so the pre-trade risk gate allows orders through. The adapter layer (DryRunAdapter vs MetaApiAdapter) provides the actual safety boundary. | Established in Phase 8: DryRunAdapter IS the safety net, not the config flag. This is consistent across all modes. | LOW — by design since Phase 8. | No. |
| LG-9 | **Single-process deployment is sufficient for demo activation.** The webhook server and monitor loop run in the same async event loop. | Demo traffic is low-volume (0-2 IRB signals per day on H1). Single-process avoids coordination complexity. | LOW for demo. | Yes — production may need supervisor/systemd. |
| LG-10 | **The launch gate does not persist state.** Gate evaluation is stateless — it reads env vars and component state at evaluation time. There is no gate state file or database. | Stateless evaluation is simpler and more predictable. The evidence trail records each evaluation for audit purposes. | LOW. | No. |

---

## Summary

| Risk Level | Count |
|-----------|-------|
| HIGH | 0 |
| MEDIUM | 2 (LG-3: real adapter behavior, LG-5: rollback leaves positions open) |
| LOW | 8 |

No high-risk assumptions. The two medium-risk items are mitigated by the first-live-check procedure and rollback documentation respectively.

---

STOPPED AFTER FINAL DEMO-LAUNCH PHASE — NO FURTHER PHASE WORK PERFORMED
