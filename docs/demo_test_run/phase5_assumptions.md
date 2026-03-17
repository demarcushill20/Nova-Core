# NovaTrade Demo Test Run — Phase 5 Assumptions

**Phase:** 5 (Trading Agent Runtime)
**Date:** 2026-03-17
**Agent:** Trading Agent
**Strategy:** Rob Hoffman IRB v2.0.0

---

## Assumptions

| ID | Summary | Risk | Testable In |
|----|---------|------|-------------|
| TA-1 | Alert payloads from TradingView webhooks arrive as valid JSON matching `alerts_schema.json` v2.0.0. No partial payloads, no HTML wrappers, no encoding issues. | Medium | Phase 5 demo run (C4 from deployment_recommendation.md) |
| TA-2 | At most one alert per action type per bar is received. The idempotency key format `irb_{action}_{bar_close_time}_{side}` is sufficient to prevent duplicates from webhook retries. | Low | Phase 5 demo run — monitor for duplicate rejections in evidence |
| TA-3 | MetaApi `create_stop_buy_order` / `create_stop_sell_order` correctly places pending stop orders that persist until filled, cancelled, or expired on the broker side. | Medium | Phase 5 demo run — verify first placed order appears in MT5 terminal |
| TA-4 | MetaApi `modify_position` correctly updates the stop-loss on an open position when called with the new SL level from a MODIFY_SL alert. | Medium | Phase 5 demo run — verify SL update after first trailing stop alert |
| TA-5 | The `cancel_order` adapter method (not yet implemented in MetaApiAdapter) can be implemented using MetaApi's `cancelOrder` RPC endpoint. | Low | Implementation — blocked until adapter extension is done |
| TA-6 | The monitoring layer (not built in Phase 5) will call `notify_fill()` when a pending stop order fills and `notify_broker_close()` when broker closes a position via SL/trailing stop. Until this layer exists, these transitions must be triggered manually or via a polling mechanism. | High | Phase 6 or later — monitoring layer is out of Phase 5 scope |
| TA-7 | The RiskEngine is initialized with correct account state before the Trading Agent processes any alerts. The caller is responsible for calling `risk_engine.initialize(account)`. | Low | Phase 5 demo run — verify at startup |
| TA-8 | Symbol resolution via `FtmoProfile.resolve_symbol("EURUSD")` returns the correct broker symbol for the connected FTMO demo account. If the broker uses a different suffix than `.sim`, the `FTMO_SYMBOL_MAP` environment variable must be updated. | Low | Phase 5 demo run — verify on first order placement |
| TA-9 | Pine's `alert.freq_once_per_bar_close` prevents duplicate alerts within the same bar. The Trading Agent's idempotency is a defense-in-depth layer, not the primary deduplication mechanism. | Low | Verified in Phase 3 (compile_report.md) |
| TA-10 | The risk engine's `pre_trade_check` evaluates correctly for STOP orders (price is the trigger level, not current market price). Spread checks use current market price which may differ. | Low | Phase 5 demo run — monitor for spurious risk denials |

---

## Risk Summary

| Risk Level | Count |
|------------|-------|
| High | 1 (TA-6: monitoring layer dependency) |
| Medium | 3 (TA-1, TA-3, TA-4) |
| Low | 6 (TA-2, TA-5, TA-7, TA-8, TA-9, TA-10) |

**Key risk:** TA-6 is the highest-risk assumption. Without the monitoring layer, the Trading Agent cannot autonomously detect pending order fills or broker-side position closes. Phase 5 provides the `notify_fill()` and `notify_broker_close()` methods as the integration surface; the monitoring layer must be built in a subsequent phase.

---

STOPPED AT FRESH IRB PHASE 5 — NO LATER PHASE WORK PERFORMED
