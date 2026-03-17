# NovaTrade Demo Test Run — Kill-Switch & Drawdown Governance

**Phase:** 6 (Risk Management Hardening)
**Date:** 2026-03-17
**Strategy:** Rob Hoffman IRB v2.0.0
**Account:** FTMO Free Trial ($100K, 1:100 leverage)

---

## 1. Purpose

This document defines the kill-switch governance model for the IRB demo run. The kill-switch is the risk engine's ultimate safety mechanism — when triggered, it halts all trading until an operator explicitly resumes.

The kill-switch exists to protect the FTMO Free Trial account from breaching drawdown limits, which would forfeit the account.

---

## 2. FTMO Drawdown Rules

| Rule | Limit | Reference Point | Consequence |
|------|-------|-----------------|-------------|
| Daily drawdown | 5% | Balance at start of trading day (midnight UTC) | Account forfeiture |
| Total drawdown | 10% | Initial account balance ($100,000) | Account forfeiture |

**Important:** FTMO measures drawdown from balance (not equity). However, the risk engine uses equity for real-time tracking since equity reflects open position P&L. This is conservative — equity-based drawdown will trigger before balance-based drawdown.

---

## 3. Drawdown Warning Tiers

The risk engine evaluates drawdown against tiered thresholds on every `pre_trade_check()` call.

### 3.1 Daily Drawdown Tiers

| Tier | Threshold | % of Limit | Action |
|------|-----------|-----------|--------|
| NORMAL | < 3.0% | < 60% | No action |
| ELEVATED | >= 3.0% | >= 60% | Log warning to evidence trail |
| CRITICAL | >= 4.0% | >= 80% | Log critical warning to evidence trail |
| BREACH | >= 5.0% | >= 100% | **HALT all trading** |

### 3.2 Total Drawdown Tiers

| Tier | Threshold | % of Limit | Action |
|------|-----------|-----------|--------|
| NORMAL | < 6.0% | < 60% | No action |
| ELEVATED | >= 6.0% | >= 60% | Log warning to evidence trail |
| CRITICAL | >= 8.0% | >= 80% | Log critical warning to evidence trail |
| BREACH | >= 10.0% | >= 100% | **HALT all trading** |

---

## 4. Kill-Switch Trigger Conditions

The kill-switch activates (halts all trading) when any of these conditions is met:

| ID | Condition | Detection Point |
|----|-----------|----------------|
| KS1 | Daily drawdown >= 5.0% | `pre_trade_check()`, `on_trade_close()` |
| KS2 | Total drawdown >= 10.0% | `pre_trade_check()`, `on_trade_close()` |
| KS3 | NovaCore system kill switch (`mode != "run"`) | `pre_trade_check()` via PreTradeGate |
| KS4 | Operator manual halt | Direct `RiskEngine._halt()` call |

---

## 5. Kill-Switch Behavior

When the kill-switch activates:

1. **`_halted` flag is set to `True`** — this is checked at the top of every `pre_trade_check()` call
2. **`_halt_reason` records the trigger condition** — included in all subsequent HALT verdicts
3. **All future `pre_trade_check()` calls return `HALT` verdict** — no checks are evaluated
4. **The Trading Agent receives HALT** — it records the halt event and returns a rejected result
5. **Evidence is recorded** — the halt trigger is logged to the JSONL evidence trail
6. **Log message at CRITICAL level** — operator-visible in system logs

### What HALT does NOT do:

- Does NOT close open positions (requires monitoring layer — B-P5-2)
- Does NOT cancel pending orders (requires monitoring layer — B-P5-2)
- Does NOT modify any broker state
- Does NOT send notifications (future capability)

These limitations are documented as Phase 6 open issues.

---

## 6. Resume Policy

| Property | Value |
|----------|-------|
| Auto-resume | **No** |
| Daily reset resumes | **No** — midnight UTC daily drawdown reset does NOT auto-resume |
| Resume mechanism | Explicit `RiskEngine.resume()` call by operator |
| Resume evidence | Logged to evidence trail with previous halt reason |

### Resume sequence:

1. Operator identifies and resolves the halt cause
2. Operator calls `risk_engine.resume()`
3. Risk engine clears `_halted` flag and `_halt_reason`
4. Logs resume event at WARNING level
5. Records resume event to evidence trail
6. Next `pre_trade_check()` evaluates normally

---

## 7. Daily Reset Interaction

At midnight UTC each trading day, the daily drawdown counter resets:

- `_daily_start_equity` is updated to current equity
- `_daily_dd` (DrawdownState) is reinitialized
- `_today_trades` list is cleared

**Critical constraint:** Daily reset does NOT clear a halt. If the engine was halted due to daily drawdown breach, the halt persists after midnight. The operator must explicitly resume.

Rationale: A daily drawdown breach indicates a significant adverse event. Automatically resuming at midnight could expose the account to further losses if the root cause (e.g., adverse market conditions) persists.

---

## 8. Evidence Requirements

All kill-switch events are recorded to `OUTPUT/novatrade/evidence.jsonl`:

| Event | Evidence Fields |
|-------|----------------|
| Halt trigger | `trading_agent_event: "RISK_HALT"`, halt_reason, drawdown state |
| Trade denied during halt | `trading_agent_event: "RISK_DENIED"`, `verdict: "HALT"` |
| Drawdown tier transition | `trading_agent_event: "DRAWDOWN_WARNING"`, tier, pct, limit |
| Resume | `trading_agent_event: "RISK_RESUMED"`, previous_halt_reason |

---

## 9. Relationship to Other Safety Layers

| Layer | Scope | Kill-Switch Interaction |
|-------|-------|----------------------|
| NovaCore system kill switch | System-wide | Checked by PreTradeGate (check #1). Fail-open if module unavailable. |
| Risk engine halt | Risk engine | This document. Fail-closed. |
| Trading Agent FSM | Agent | Rejects invalid state transitions. Independent of kill-switch. |
| PreTradeGate 13 checks | Per-trade | Run in Layer 4 after kill-switch check. Not reached during halt. |
| Broker margin check | Broker-side | Independent. Broker may reject even if risk engine allows. |

---

STOPPED AT FRESH IRB PHASE 6 — NO LATER PHASE WORK PERFORMED
