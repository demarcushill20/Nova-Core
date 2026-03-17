# NovaTrade Demo Test Run — Success Criteria

**Phase:** 0 (Scope Freeze) — AMENDED
**Date:** 2026-03-16 (original) | **Amended:** 2026-03-17
**Status:** LOCKED (amended — IRB-specific annotations added to E3, E6)

---

## 1. Operational Success Criteria

These criteria validate that the execution stack runs reliably for the test duration.

| ID | Criterion | Threshold | Measurement |
|----|-----------|-----------|-------------|
| O1 | **MetaApi uptime** | >= 95% of trading hours | Health polling log — count of successful vs failed health checks |
| O2 | **Connection recovery** | Auto-reconnect within 5 minutes of any disconnect | Adapter health log timestamps |
| O3 | **No unrecoverable crashes** | Zero crashes requiring manual intervention during trading hours | System logs, evidence pipeline gaps |
| O4 | **Position reconciliation accuracy** | Zero unresolved mismatches at end of run | Reconciliation evidence records |
| O5 | **Config stability** | No config changes required during the run | Git log of novatrade/ directory |
| O6 | **Run duration achieved** | >= 5 trading days of active monitoring | Calendar count of active trading days |

---

## 2. Execution Success Criteria

These criteria validate that the pipeline correctly translates strategy intent into broker actions.

| ID | Criterion | Threshold | Measurement |
|----|-----------|-----------|-------------|
| E1 | **Strategy contract fidelity** | Every executed trade matches a valid signal from the strategy contract | Evidence log cross-referenced against strategy spec |
| E2 | **No unauthorized trades** | Zero trades placed outside the strategy contract rules | Evidence log audit |
| E3 | **Stop-loss on every position** | 100% of opened positions have SL set at time of entry | Broker position records via MetaApi | *(IRB note: SL is dynamic — opposite side of IRB candle +/- 1 pip — but still set at entry time. Criterion remains valid.)* |
| E4 | **Order fill confirmation** | Every submitted order has a recorded fill or rejection | Adapter execution log |
| E5 | **Symbol correctness** | All trades placed on EURUSD.sim only | Evidence log symbol field |
| E6 | **Minimum trade count** | >= 10 completed trades over the run period | Evidence log count | *(IRB note: IRB signals on H1 EURUSD are less frequent than EMA crossover signals. If Phase 4 backtest suggests <10 trades in 10 days, this threshold should be revisited in Phase 1.)* |
| E7 | **Dry-run gate disabled cleanly** | System transitions from dry_run=true to dry_run=false via explicit config change only | Config change record |

---

## 3. Risk / Governance Success Criteria

These criteria validate that the risk governor operates correctly and outranks execution.

| ID | Criterion | Threshold | Measurement |
|----|-----------|-----------|-------------|
| R1 | **Risk gate enforcement** | Every order attempt passes through the 13-check pre-trade gate | Evidence log — every execution has a risk_decision record |
| R2 | **Risk gate denials logged** | Every denied trade is logged with reason | Evidence log DENIED records |
| R3 | **Daily drawdown respected** | Account never exceeds 5% daily drawdown | Account balance/equity snapshots |
| R4 | **Total drawdown respected** | Account never exceeds 10% total drawdown | Account balance history |
| R5 | **Max positions respected** | Never more than 5 simultaneous open positions | Position snapshot log |
| R6 | **No strategy modification during run** | Strategy contract hash unchanged from start to end | File checksum comparison |
| R7 | **Kill switch functional** | If triggered, all new orders blocked immediately | Test or evidence of kill switch activation (if it fires) |

---

## 4. Observability Success Criteria

These criteria validate that the run produces enough evidence for a meaningful verdict.

| ID | Criterion | Threshold | Measurement |
|----|-----------|-----------|-------------|
| V1 | **Evidence completeness** | Every trade cycle (signal → order → fill → close) has full evidence chain | Evidence JSONL record count and completeness |
| V2 | **Campaign tagging** | 100% of evidence records tagged with `ftmo-free-trial-march-2026` | Evidence record audit |
| V3 | **Health snapshots** | Health recorded at least every 5 minutes during trading hours | Health evidence record timestamps |
| V4 | **Latency tracking** | Order submission latency recorded for every trade | Adapter execution log |
| V5 | **Verdict renderable** | The `verdict` subcommand produces a meaningful go/no-go from collected evidence | `novatrade_ftmo.py verdict` output |
| V6 | **Human-readable report** | A formatted validation report can be generated at any point during the run | `novatrade_ftmo.py health` output |

---

## 5. Failure Conditions That Block Later Phases

If ANY of these occur, the demo run is considered failed and later phases are blocked until resolution:

| ID | Failure Condition | Consequence |
|----|-------------------|-------------|
| F1 | MetaApi connection cannot be maintained for >= 4 hours continuously | Infrastructure re-evaluation required before retry |
| F2 | Risk gate fails to block an invalid order | Risk gate code must be fixed and re-verified |
| F3 | Unauthorized trade placed (not matching strategy contract) | Pipeline integrity failure — full audit required |
| F4 | Position reconciliation shows phantom or missing positions | Adapter reliability failure — MetaApi evaluation required |
| F5 | Evidence pipeline loses records (gaps in JSONL) | Evidence system must be hardened before any future run |
| F6 | Strategy contract requires modification during the run to avoid crashes | Strategy spec was incomplete — return to Phase 1 |
| F7 | Account breaches FTMO daily or total drawdown limits | Risk gate calibration failure — risk parameters must be tightened |
| F8 | Fewer than 10 trades completed in the run period | Strategy or timeframe selection was wrong — re-evaluate |
