# NovaTrade Demo Test Run — Phase 6 Assumptions

**Phase:** 6 (Risk Management Hardening)
**Date:** 2026-03-17
**Agent:** Risk Management Agent
**Strategy:** Rob Hoffman IRB v2.0.0

---

## Assumptions

| ID | Summary | Risk | Testable In |
|----|---------|------|-------------|
| RE-1 | The risk engine is initialized with correct account equity before any alerts are processed. `RiskEngine.initialize(account_state)` must be called at startup. If initial equity is wrong, all drawdown calculations are incorrect. | Medium | Phase 6 demo run — verify at startup |
| RE-2 | FTMO measures daily drawdown from the start-of-day balance, not from peak equity within the day. The risk engine uses `daily_start_equity` set by `initialize()` or `reset_daily()`. FTMO's exact calculation methodology may differ slightly. | Medium | Verify against FTMO dashboard during demo run |
| RE-3 | The forex market close window (Friday 22:00 UTC to Sunday 22:00 UTC) is correct for the broker connected to the FTMO demo account. Different brokers may use different server times. | Low | Verify first weekend of demo run — check if session denial fires correctly |
| RE-4 | The IRB exposure limit of max 1 open position is sufficient defense-in-depth. The Trading Agent FSM already prevents multiple concurrent positions, so the risk engine exposure check is a secondary guard. | Low | Covered by Phase 5 FSM tests + Phase 6 exposure tests |
| RE-5 | The `on_trade_close()` PnL values provided by the caller are accurate. The risk engine trusts the caller's `pnl_usd` and `pnl_pips` for drawdown tracking. Inaccurate values would cause incorrect drawdown calculations. | Medium | Phase 6 demo run — reconcile risk engine equity vs broker account equity |
| RE-6 | The injectable clock (`_clock`) is only overridden in tests. In production, the default `_default_utc_now()` returns correct UTC time. The VPS system clock must be synchronized (NTP). | Low | System-level — verify NTP is configured on VPS |
| RE-7 | Daily drawdown reset is called at midnight UTC each trading day. The caller (monitoring layer or webhook server) is responsible for calling `reset_daily(equity)` at the correct time. Without this call, daily drawdown accumulates across multiple days. | High | Phase 6+ monitoring layer — not yet built (B-P5-2) |
| RE-8 | The drawdown warning tiers (60%, 80%, 100%) are logged but do not trigger automated notifications. The operator must monitor evidence logs or system logs for ELEVATED/CRITICAL warnings. | Low | Phase 6 — check evidence trail for tier transition events |
| RE-9 | `get_required_actions()` returns advisory actions. FLATTEN_POSITIONS and CANCEL_PENDING cannot be executed by the risk engine itself — they require the monitoring layer (B-P5-2) or direct operator intervention. | Medium | Documented in phase6_open_issues.md |
| RE-10 | The `RiskAction` enum values (HALT_TRADING, FLATTEN_POSITIONS, CANCEL_PENDING, SUSPEND_STRATEGY) are informational in Phase 6. Only HALT_TRADING is actively enforced. The others are defined for future phases. | Low | Phase 6 — verify HALT_TRADING enforcement, note others are advisory |

---

## Risk Summary

| Risk Level | Count |
|------------|-------|
| High | 1 (RE-7: daily reset dependency on monitoring layer) |
| Medium | 3 (RE-1, RE-2, RE-5, RE-9) |
| Low | 5 (RE-3, RE-4, RE-6, RE-8, RE-10) |

**Key risk:** RE-7 is the highest-risk assumption. Without an automated daily reset mechanism, the daily drawdown counter accumulates across days, making the daily drawdown check overly conservative (it would never reset). The monitoring layer (B-P5-2) or a thin startup script must call `reset_daily()` at midnight UTC.

---

STOPPED AT FRESH IRB PHASE 6 — NO LATER PHASE WORK PERFORMED
