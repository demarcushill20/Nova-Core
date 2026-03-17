# NovaTrade Demo Test Run — Phase 6 Summary (Risk Management Hardening)

**Phase:** 6 (Risk Management Hardening)
**Date:** 2026-03-17
**Status:** COMPLETE — Risk engine hardened with 5-layer policy model
**Agent:** Risk Management Agent
**Strategy:** Rob Hoffman IRB v2.0.0 (strategy_spec.yaml v2.0.0)
**Depends on:** Phase 5 (Trading Agent Runtime — COMPLETE)

---

## 1. Phase 6 Completion Status

**COMPLETE.** The risk engine has been hardened into the governing authority for the IRB demo run. All required deliverables have been produced.

**Key constraint:** Phase 6 hardens the risk engine only. It does not build the daily-reset caller, the action execution handlers, or the monitoring layer. These are identified as open issues (B-P6-1, B-P6-2) to be resolved before or during the demo run.

---

## 2. What Was Done

### 2.1 Risk Policy Document (`docs/demo_test_run/risk_policy.yaml`)

Comprehensive risk policy defining:
- Risk boundary (what the engine governs vs what it doesn't)
- 5-layer policy evaluation model with short-circuit semantics
- Session policy (24/5 forex hours, weekend handling)
- IRB-specific exposure controls (max 1 position, max 1 pending)
- Drawdown governance with tiered warnings (60%, 80%, 100%)
- Kill-switch trigger conditions and resume policy
- Risk decision model (ALLOW, DENY, HALT verdicts + 5 action types)
- Evidence requirements

### 2.2 Risk Decision Schema (`docs/demo_test_run/risk_decision_schema.json`)

JSON Schema v1.0.0 defining the expanded `RiskDecision` contract:
- 3 verdict types (ALLOW, DENY, HALT)
- Policy layer identification (0-5)
- Portfolio-level action directives
- Drawdown state snapshot
- Check result structure

### 2.3 Kill-Switch Policy (`docs/demo_test_run/kill_switch_policy.md`)

Complete kill-switch governance documentation:
- FTMO drawdown rules (daily 5%, total 10%)
- Drawdown warning tiers (NORMAL, ELEVATED, CRITICAL, BREACH)
- 4 kill-switch trigger conditions
- Halt behavior (what it does and does NOT do)
- Resume policy (operator-only, no auto-resume)
- Daily reset interaction (reset does NOT clear halt)
- Evidence requirements for kill-switch events
- Relationship to other safety layers

### 2.4 Model Expansion (`novatrade/models.py`)

- **`RiskVerdict.HALT`** — new verdict for drawdown breach and kill-switch scenarios
- **`RiskAction`** — new enum: NONE, HALT_TRADING, SUSPEND_STRATEGY, FLATTEN_POSITIONS, CANCEL_PENDING
- **`RiskDecision.policy_layer`** — which layer produced the verdict (-1 = all passed)
- **`RiskDecision.actions`** — portfolio-level action directives
- **`RiskDecision.halted`** — property to distinguish HALT from DENY
- **`RiskDecision.denied`** — updated to return True for both DENY and HALT
- **`EvidenceType.RISK_HALT`** — new evidence type for halt events
- **`EvidenceType.RISK_WARNING`** — new evidence type for drawdown warnings

### 2.5 Risk Engine Hardening (`novatrade/risk/risk_engine.py`)

Complete restructuring of `pre_trade_check()` into 5 policy layers:

- **Layer 0: System kill switch** — checks `_halted` flag, returns HALT verdict
- **Layer 1: Session policy** — forex market hours (24/5), denies during weekend close (opt-in via `check_forex_session` config)
- **Layer 2: Drawdown governance** — FTMO daily (5%) and total (10%) limits with tiered warnings at 60%/80%/100%, triggers HALT on breach
- **Layer 3: Exposure control** — IRB max 1 open position (opt-in via `irb_max_open_positions` config), defense-in-depth
- **Layer 4: Pre-trade gate** — existing 13-check gate preserved as-is

New methods and helpers:
- `_check_session()` — forex market hours check with injectable clock
- `_check_drawdown_governance()` — tiered drawdown evaluation
- `_check_irb_exposure()` — IRB position limit enforcement
- `get_required_actions()` — returns portfolio-level actions (HALT, FLATTEN, CANCEL)
- `DrawdownTier` enum (NORMAL, ELEVATED, CRITICAL, BREACH)
- `_drawdown_tier()` helper — classifies drawdown percentage into tier
- `_is_forex_weekend()` helper — checks if UTC datetime is in forex weekend close

### 2.6 Config Extension (`novatrade/config.py`)

- `RiskConfig.check_forex_session: bool = False` — Phase 6 session check (opt-in)
- `RiskConfig.irb_max_open_positions: int = 0` — Phase 6 IRB exposure limit (0 = disabled)

Backward-compatible: both default to disabled, so existing code is unaffected.

### 2.7 Trading Agent Integration (`novatrade/execution/trading_agent.py`)

- **Fast-reject on halt** — `_process_inner()` checks `risk_engine.halted` before validation
- **HALT evidence** — distinguishes RISK_HALT from RISK_DENIED in evidence recording
- **Policy layer in evidence** — records `policy_layer` from the risk decision
- **Verdict in rejected_reason** — format changed from `risk_denied:` to `risk_{verdict}:` for clarity

### 2.8 Tests

**58 new Phase 6 tests** (`tests/test_risk_engine_phase6.py`) covering:

- Drawdown tier classification (9 tests)
- Forex weekend detection (9 tests)
- Session policy (7 tests — weekday/weekend/Friday close/Sunday open)
- Drawdown governance (6 tests — normal/elevated/critical/breach/actions)
- IRB exposure control (4 tests — enabled/disabled/1 position/2 positions)
- Policy layer order (5 tests — layer priority verification)
- HALT verdict semantics (3 tests — HALT/DENY/ALLOW distinction)
- Kill-switch governance (5 tests — daily/total halt, reset, resume, blocking)
- Required actions (4 tests — normal/halt/flatten/no-flatten)
- Check completeness (3 tests — all checks present in ALLOW)
- Edge cases (3 tests — uninitialized/zero equity/negative drawdown)

**3 existing test updates** (`tests/test_risk_engine.py`):
- `test_deny_when_halted` → `test_halt_when_halted` (HALT verdict, not DENY)
- `test_deny_daily_drawdown_breach` → `test_halt_daily_drawdown_breach` (HALT verdict)
- `test_halt_blocks_trades` — added HALT verdict assertion

**1 existing test update** (`tests/test_trading_agent.py`):
- `test_risk_deny_rejects_signal` — updated expected `rejected_reason` from "risk_denied" to "risk_halt"

**Total test count: 4608** (all passing)

---

## 3. Decisions Made

| # | Decision | Rationale |
|---|----------|-----------|
| D-P6-1 | Use policy layers (0-4) with short-circuit evaluation | Provides clear precedence: halt > session > drawdown > exposure > gate. Early layers prevent unnecessary evaluation of later layers. All checks within a layer still run (no short-circuit within). |
| D-P6-2 | Session check and IRB exposure check are opt-in via config | Backward-compatible: existing tests and code work without modification. Phase 6 demo config explicitly enables them. |
| D-P6-3 | Drawdown breach returns HALT (not DENY) | HALT is semantically stronger than DENY: it blocks the current trade AND all future trades. This matches the kill-switch behavior specified in strategy_spec §5.8. |
| D-P6-4 | Injectable clock via `_clock` attribute | Enables deterministic testing of session checks without mocking the system clock. Tests set `engine._clock = lambda: fixed_datetime`. |
| D-P6-5 | Daily reset does NOT clear halt | kill_switch_policy.md §7: A drawdown breach indicates a significant adverse event. Auto-resume at midnight could mask real problems. Operator must explicitly resume. |
| D-P6-6 | FLATTEN_POSITIONS and CANCEL_PENDING are advisory | The risk engine has no adapter access. It can signal what should happen; the caller (monitoring layer) must execute. This preserves the engine's provider-neutral design. |
| D-P6-7 | Trading Agent fast-rejects when halted | Avoids unnecessary validation, idempotency, and intent creation when the engine is halted. The halt check runs before any other processing. |

---

## 4. Changes Made

| File | Change | Type |
|------|--------|------|
| `novatrade/models.py` | Added `RiskVerdict.HALT`, `RiskAction` enum, `RiskDecision.policy_layer`, `RiskDecision.actions`, `RiskDecision.halted`, `EvidenceType.RISK_HALT`, `EvidenceType.RISK_WARNING` | Extension |
| `novatrade/config.py` | Added `RiskConfig.check_forex_session`, `RiskConfig.irb_max_open_positions` | Extension |
| `novatrade/risk/risk_engine.py` | Restructured `pre_trade_check()` into 5 policy layers; added session, drawdown governance, exposure checks; added `DrawdownTier`, `_drawdown_tier()`, `_is_forex_weekend()`, `get_required_actions()` | Hardening |
| `novatrade/execution/trading_agent.py` | Fast-reject on halt; HALT evidence recording; policy_layer in evidence | Integration |
| `tests/test_risk_engine.py` | Updated 3 tests for HALT verdict behavior | Test fix |
| `tests/test_trading_agent.py` | Updated 1 test for risk_halt rejection format | Test fix |
| `tests/test_risk_engine_phase6.py` | **NEW** — 58 tests for Phase 6 risk hardening | Tests |
| `docs/demo_test_run/risk_policy.yaml` | **NEW** — Active IRB demo-run risk policy | Documentation |
| `docs/demo_test_run/risk_decision_schema.json` | **NEW** — Risk decision contract (JSON Schema) | Documentation |
| `docs/demo_test_run/kill_switch_policy.md` | **NEW** — Kill-switch & drawdown governance | Documentation |
| `docs/demo_test_run/phase6_assumptions.md` | **NEW** — 10 assumptions | Documentation |
| `docs/demo_test_run/phase6_open_issues.md` | **NEW** — 2+4 blockers, 4 warnings, 5 info | Documentation |
| `docs/demo_test_run/phase6_summary.md` | **NEW** — This file | Documentation |

---

## 5. Assumptions

10 new assumptions (RE-1 to RE-10). See `phase6_assumptions.md` for full list.

Key risks:
- **RE-7 (HIGH)**: Daily reset dependency — `reset_daily()` needs a caller
- **RE-1, RE-2, RE-5 (MEDIUM)**: Initial equity, FTMO calculation differences, PnL accuracy

---

## 6. Open Issues

| Severity | Count | Key Items |
|----------|-------|-----------|
| Blocker (new) | 2 | B-P6-1 (daily reset caller), B-P6-2 (action execution) |
| Blocker (inherited) | 4 | B-P5-1 (cancel_order), B-P5-2 (monitoring layer), B-IRB-1 (Pine), B-IRB-2 (backtest) |
| Warning | 4 | P6-W-1 to P6-W-4 |
| Informational | 5 | P6-I-1 to P6-I-5 |

Both new blockers are resolvable without changes to the risk engine:
- B-P6-1: Daily reset caller (~10 lines in webhook server or monitoring loop)
- B-P6-2: Action execution handlers in monitoring layer (Phase 7+ scope)

---

## 7. Files Created/Updated

| File | Purpose |
|------|---------|
| `novatrade/models.py` | Expanded risk verdicts, actions, and decision model |
| `novatrade/config.py` | Phase 6 config flags for session and exposure checks |
| `novatrade/risk/risk_engine.py` | 5-layer policy model, drawdown tiers, session/exposure checks |
| `novatrade/execution/trading_agent.py` | HALT fast-reject and evidence recording |
| `tests/test_risk_engine.py` | Updated existing tests for HALT verdict |
| `tests/test_trading_agent.py` | Updated risk rejection test |
| `tests/test_risk_engine_phase6.py` | 58 new Phase 6 tests |
| `docs/demo_test_run/risk_policy.yaml` | Active risk policy document |
| `docs/demo_test_run/risk_decision_schema.json` | Risk decision contract |
| `docs/demo_test_run/kill_switch_policy.md` | Kill-switch governance |
| `docs/demo_test_run/phase6_assumptions.md` | 10 assumptions |
| `docs/demo_test_run/phase6_open_issues.md` | Blockers, warnings, info |
| `docs/demo_test_run/phase6_summary.md` | This file |

---

## 8. Recommended Next Steps

### Immediate (before demo run)

1. **Resolve B-P6-1**: Add daily-reset call at midnight UTC in webhook server or monitoring loop
2. **Resolve B-P5-1**: Implement `cancel_order()` in MetaApiAdapter
3. **Resolve B-IRB-1**: Compile strategy.pine in TradingView
4. **Resolve B-IRB-2**: Run TradingView backtest

### For demo run

5. **Set IRB config**: Enable `check_forex_session=True` and `irb_max_open_positions=1` in demo config
6. **Build monitoring layer** (B-P5-2): Fill detection + daily reset + action execution
7. **Wire end-to-end**: TradingView → webhook → Trading Agent → RiskEngine → MetaApi → FTMO

### Monitoring

8. Monitor evidence trail for: drawdown tier transitions, session denials, halt events, exposure denials
9. Verify daily drawdown resets correctly at midnight UTC
10. Compare risk engine drawdown tracking vs FTMO dashboard

---

## 9. Final Statement

STOPPED AT FRESH IRB PHASE 6 — NO LATER PHASE WORK PERFORMED

---

**Phase 6 complete — Risk engine hardened with 5-layer policy model, 58 new tests, 4608 total passing.**
