# NovaTrade Demo Test Run — Deployment Freeze Note

**Phase:** 0 (Scope Freeze)
**Date:** 2026-03-16
**Status:** FROZEN

---

## Current Execution Stack (Verified 2026-03-16)

| Layer | Component | Version / ID | Status |
|-------|-----------|-------------|--------|
| **Orchestrator** | NovaCore on Linux VPS | Ubuntu, Python 3.10 | Running |
| **Trading Module** | NovaTrade | ~2500 LOC, 17+ modules, 373 tests | Verified |
| **Execution Bridge** | MetaApi Cloud SDK | v29.1.1 (Python) | Connected |
| **MetaApi Account** | Cloud MT5 | `4c121f03-836f-4fb1-8799-736e53699a66` | Deployed (London) |
| **Broker** | OANDA Corporation | Server: OANDA-Demo-1 | Connected |
| **Account Type** | FTMO Free Trial 2-Step | $100,000 demo, 1:100 leverage | Active |
| **Symbol Mapping** | `.sim` suffix | EURUSD→EURUSD.sim | Verified |
| **Risk Gate** | Pre-trade gate | 13 checks | Operational |
| **Evidence Pipeline** | JSONL recorder | Campaign: ftmo-free-trial-march-2026 | Writing |
| **Config** | Env file | `/etc/novacore/novatrade.env` (chmod 600) | Deployed |
| **Preflight** | Full check | 13/13 PASS | Verified |
| **Dry Run** | End-to-end | Denied by dry_run gate (expected) | Verified |

---

## What Is Frozen

The following components and configurations are **locked for the duration of demo run preparation and execution**. No changes permitted without an explicit halt-and-review decision.

1. **Execution bridge**: MetaApi Cloud SDK. No migration to self-hosted infrastructure.
2. **MetaApi account**: Account ID `4c121f03-836f-4fb1-8799-736e53699a66`. No new accounts.
3. **Broker connection**: OANDA-Demo-1 via MetaApi London region. No broker change.
4. **Account type**: FTMO Free Trial demo. No transition to challenge or funded mode.
5. **Symbol mapping**: `.sim` suffix. No symbol map changes.
6. **Risk gate**: Existing 13-check pre-trade gate. No new checks, no relaxed checks.
7. **Evidence format**: JSONL with campaign tags. No schema changes.
8. **NovaTrade module boundaries**: adapter, config, models, risk, validation, execution, monitor. No new modules.
9. **Operating mode**: `NOVATRADE_MODE=DEMO`. No mode escalation.

---

## Explicitly Forbidden Changes During Demo Run Preparation

| Change | Why Forbidden |
|--------|---------------|
| Migrate to KVM/mt5-httpapi | Architecture Decision Report explicitly defers this to "Month 2+" after pipeline is proven. The demo run IS the proof step. |
| Add new broker accounts | "One demo account, one strategy, one monitoring loop" (Fast Deployment Plan §0.2) |
| Switch to cTrader or other platform | MT5 via MetaApi is the verified stack. Platform migration is out of scope. |
| Upgrade MetaApi tier | Current tier is sufficient for one-symbol H1 demo. No infrastructure spend. |
| Add TradingView shadow validation | Path 1 is a validation layer (WP2 §4.2), not required for the demo systems test. |
| Modify risk gate thresholds | Risk parameters are prop-firm-safe defaults. Changing them during prep invalidates the test. |
| Add multi-timeframe **execution** beyond H1+H4 | H4 trend **reading** (EMA(20) direction check) is now in scope — required by IRB strategy (see amended charter). H4 **trade execution** remains forbidden. No additional timeframes beyond H1 (primary) and H4 (MTF confirmation). |
| Refactor NovaTrade module structure | "No uncontrolled scope growth" — structural refactoring is not execution validation. |
| Add new agents beyond the 6 essential | Research, scoring, and advanced agents are Phase 4+ (WP3 §5). |

---

## Why Infrastructure Migration Is Deferred

The Architecture Decision Report (2026-03-15) explicitly defines a two-tier approach:

> **MVP (week 1-2):** MetaApi.cloud — zero infrastructure, Python SDK, works from your existing Linux VPS today.
>
> **Production (month 2+):** mt5-httpapi on a KVM-capable Linux server.

The Fast Deployment Plan reinforces this:

> "The clean principle is: MetaApi is the temporary execution backend for proof, not the final operating system of NovaTrade."

The demo test run IS the proof step. Migrating infrastructure before completing the proof step reverses the prescribed build order. MetaApi's limitations (10-50ms added latency, third-party dependency) are explicitly acceptable for strategies with minute+ holding periods (Architecture Decision Report §8, Appendix: Latency Reference).

Migration to KVM/mt5-httpapi should only begin AFTER:
1. At least one strategy has paper-traded successfully on MetaApi
2. The adapter contract feels stable
3. Third-party dependency becomes a bottleneck

None of these conditions can be evaluated until the demo test run completes.
