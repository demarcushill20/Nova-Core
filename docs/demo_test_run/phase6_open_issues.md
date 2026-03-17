# NovaTrade Demo Test Run — Phase 6 Open Issues

**Phase:** 6 (Risk Management Hardening)
**Date:** 2026-03-17
**Agent:** Risk Management Agent
**Strategy:** Rob Hoffman IRB v2.0.0

---

## Blockers

| ID | Summary | Resolution Path |
|----|---------|----------------|
| B-P6-1 | **No automated daily drawdown reset.** The risk engine requires `reset_daily(equity)` to be called at midnight UTC each trading day. Without this, the daily drawdown counter accumulates across days, making the check overly conservative. No component currently calls this method. | Build a thin daily-reset caller in the monitoring layer (B-P5-2) or webhook server startup logic. Alternatively, call `reset_daily()` manually at the start of each demo day. |
| B-P6-2 | **FLATTEN_POSITIONS and CANCEL_PENDING actions are advisory only.** The risk engine can signal these actions via `get_required_actions()`, but it cannot execute them — it has no adapter access. These actions require the monitoring layer (B-P5-2). | Implement action handlers in the monitoring layer or webhook server that check `get_required_actions()` and execute the appropriate adapter calls. For initial demo, the operator can flatten/cancel manually via the FTMO dashboard when the risk engine halts. |

## Inherited Blockers

| ID | Summary | Status |
|----|---------|--------|
| B-P5-1 | MetaApiAdapter does not implement `cancel_order()` | Still open |
| B-P5-2 | No monitoring layer to detect fills and broker closes | Still open — also needed for B-P6-1 and B-P6-2 |
| B-IRB-1 | Pine compilation not verified in TradingView | Still open |
| B-IRB-2 | No live backtest executed | Still open |

---

## Warnings

| ID | Summary | Mitigation |
|----|---------|------------|
| P6-W-1 | **Session check uses system clock.** The forex session check relies on the VPS system clock being correctly set to UTC. If the system clock is wrong, the session check may deny trades during market hours or allow them during the weekend. | Ensure NTP is configured on the VPS. The `_clock` is injectable for testing but the VPS clock is authoritative in production. |
| P6-W-2 | **Drawdown reference may differ from FTMO's calculation.** The risk engine uses equity-based drawdown from the initial equity at `initialize()`. FTMO may use balance-based drawdown from the account's initial deposit. Small differences in calculation methodology could cause the risk engine to halt slightly before or after FTMO's actual limit. | Conservative approach: the risk engine's equity-based calculation is typically more sensitive than balance-based (equity reflects unrealized P&L). Monitor FTMO dashboard to verify alignment. |
| P6-W-3 | **IRB exposure check is defense-in-depth, not primary.** The Trading Agent FSM (Phase 5) is the primary enforcement of the 1-position limit. The risk engine's `irb_max_positions` check at Layer 3 is a secondary guard. If the FSM has a bug, the risk engine provides backup. | Acceptable — both layers test identically. The risk engine check was added specifically for defense-in-depth. |
| P6-W-4 | **Policy layer short-circuit means later layers don't run on early denial.** If Layer 1 (session) denies, Layers 2-4 don't run. This means the decision's `checks` list won't include drawdown or gate checks. This is by design (performance) but means the operator sees fewer diagnostics on session denials. | Acceptable — session denials are expected (weekend) and don't require drawdown diagnostics. All layers run in the common (ALLOW) case. |

---

## Informational

| ID | Summary |
|----|---------|
| P6-I-1 | The `RiskVerdict.HALT` enum value is new in Phase 6. Code that previously checked `verdict == RiskVerdict.DENY` for halt cases now receives `RiskVerdict.HALT` instead. The `RiskDecision.denied` property returns `True` for both DENY and HALT, maintaining backward compatibility for code that uses `decision.denied`. |
| P6-I-2 | `RiskAction` enum (NONE, HALT_TRADING, SUSPEND_STRATEGY, FLATTEN_POSITIONS, CANCEL_PENDING) is defined for completeness but only HALT_TRADING is actively triggered in Phase 6. The others are available for the monitoring layer to consume. |
| P6-I-3 | The `RiskDecision.policy_layer` field indicates which layer produced the verdict: 0=halt, 1=session, 2=drawdown, 3=exposure, 4=gate, -1=all passed. This aids diagnostics in the evidence trail. |
| P6-I-4 | `RiskConfig` gains two new fields: `check_forex_session` (default False) and `irb_max_open_positions` (default 0). Both default to disabled for backward compatibility. Phase 6 demo config must set them explicitly. |
| P6-I-5 | The Trading Agent's `_process_inner()` now fast-rejects alerts when `risk_engine.halted` is True, before validation or idempotency checks. This avoids unnecessary processing during halt. |

---

## Summary

| Severity | Count |
|----------|-------|
| Blocker (new) | 2 |
| Blocker (inherited) | 4 |
| Warning | 4 |
| Informational | 5 |

**Both new blockers** are addressable without changes to the risk engine:
- B-P6-1 requires a daily-reset caller (thin wrapper, ~10 lines)
- B-P6-2 requires action execution handlers in the monitoring layer

---

STOPPED AT FRESH IRB PHASE 6 — NO LATER PHASE WORK PERFORMED
