# NovaTrade Demo Test Run — Phase 8 Failure Injection Report

**Phase:** 8 (Controlled Dry Runs and Launch Readiness)
**Date:** 2026-03-17
**Strategy:** Rob Hoffman IRB v2.0.0

---

## Purpose

Verify that failures at every layer of the stack are explicit, safe, and
do not cause silent corruption or uncontrolled behavior.

---

## Negative-Path Scenarios Exercised

### 1. Input Validation Failures

| # | Scenario | Expected | Observed | Safe? |
|---|----------|----------|----------|-------|
| FI-1 | Empty HTTP body | 400 error, no state change | 400 "empty request body" | YES |
| FI-2 | Malformed JSON | 400 error, no state change | 400 "malformed JSON" | YES |
| FI-3 | Non-object payload (`[1,2,3]`) | 400 error, no state change | 400 "payload must be a JSON object" | YES |
| FI-4 | Missing required fields | Agent rejects, ok=false | ok=false, validation error | YES |
| FI-5 | Wrong strategy_name | Agent rejects, ok=false | ok=false, validation error | YES |
| FI-6 | Wrong strategy_version | Agent rejects, ok=false | ok=false, validation error | YES |

### 2. State Machine Violations

| # | Scenario | Expected | Observed | Safe? |
|---|----------|----------|----------|-------|
| FI-7 | CANCEL_ORDER when FLAT | Rejected, no state change | ok=false, agent stays FLAT | YES |
| FI-8 | CLOSE_POSITION when FLAT | Rejected, no state change | ok=false, agent stays FLAT | YES |
| FI-9 | MODIFY_SL when FLAT | Rejected, no state change | ok=false, agent stays FLAT | YES |
| FI-10 | PLACE_STOP_ORDER when PENDING | Rejected, stays PENDING | ok=false, agent stays PENDING_LONG | YES |

### 3. Risk Engine Failures

| # | Scenario | Expected | Observed | Safe? |
|---|----------|----------|----------|-------|
| FI-11 | Risk engine halted | Order denied, FLAT | ok=false, agent stays FLAT, risk decision recorded | YES |
| FI-12 | Halt visible in status | Status shows halted=true | `/status` shows `risk_engine.halted=true` | YES |

### 4. Idempotency

| # | Scenario | Expected | Observed | Safe? |
|---|----------|----------|----------|-------|
| FI-13 | Duplicate alert | Second suppressed | Second returns ok=false, no side effects | YES |

### 5. Authentication

| # | Scenario | Expected | Observed | Safe? |
|---|----------|----------|----------|-------|
| FI-14 | Missing webhook secret | 403 Forbidden | 403 "invalid webhook secret" | YES |
| FI-15 | Wrong webhook secret | 403 Forbidden | 403 "invalid webhook secret" | YES |

### 6. Adapter/Infrastructure Failures

| # | Scenario | Expected | Observed | Safe? |
|---|----------|----------|----------|-------|
| FI-16 | health_check() raises ConnectionError | Monitor cycle degrades, loop continues | Health degraded, cycle recorded, loop continues | YES |
| FI-17 | Monitor cycle exception (simulated) | Cycle fails, loop continues | cycles_failed=1, loop continues to next cycle | YES |

### 7. Reconciliation Edge Cases

| # | Scenario | Expected | Observed | Safe? |
|---|----------|----------|----------|-------|
| FI-18 | Orphan pending order (agent FLAT, adapter has order) | ORPHAN_PENDING detected | Reconciliation action with ORPHAN_PENDING outcome | YES |

### 8. Uninitialized Component

| # | Scenario | Expected | Observed | Safe? |
|---|----------|----------|----------|-------|
| FI-19 | POST alert with no Trading Agent | 503 error | 503 "trading agent not initialized" | YES |

---

## Summary

| Category | Scenarios | All Safe? |
|----------|-----------|-----------|
| Input validation | 6 | YES |
| State machine violations | 4 | YES |
| Risk engine | 2 | YES |
| Idempotency | 1 | YES |
| Authentication | 2 | YES |
| Infrastructure failures | 2 | YES |
| Reconciliation edge cases | 1 | YES |
| Uninitialized components | 1 | YES |
| **Total** | **19** | **YES** |

All failures are:
- **Explicit**: clear error messages, no silent corruption
- **Safe**: no state mutation on failure
- **Logged**: evidence trail records the failure
- **Non-cascading**: individual failures don't kill the runtime

---

STOPPED AT FRESH IRB PHASE 8 — NO LATER PHASE WORK PERFORMED
