# TV Webhook → Demo Pipeline (parallel to vault engine port)

**Status:** Approved 2026-04-30
**Author:** Nova (Claude Code, Opus 4.7)
**Operator:** Demarcus
**Spec ID:** 2026-04-30-tv-webhook-demo-pipeline

## Goal

Stand up a second NovaTrade runner instance on the **already-built** TradingView webhook pipeline (Phase 8/9 code), connected to a fresh non-expiring MetaApi broker demo, running in parallel with the existing live_loop runner. Trust Pine v5 IRB Champion alerts as the validated signal source — bypass the engine-drift bug entirely. HardRiskSupervisor watches.

## Non-goals

- No change to the currently running `novacore-novatrade.service` (vault-port validation track keeps running)
- No fix to `novatrade/backtest/engine.py` engine-drift bug (orthogonal — handled by the vault engine port workstream)
- No change to live_loop / local-Python strategy logic
- No new Pine strategy logic — fork is constants-only
- No automation of TradingView alert creation (manual UI step on operator's side)

## Surprising context

The webhook pipeline **already exists, end-to-end** — built during Phase 8/9 commits (`a507660`, `97596a4`). What's missing is *running it as the active path*:

- `novatrade/runtime/webhook_server.py` — FastAPI receiver (`/webhook/alert`, `/health`, `/status`, `/control/resume`, `/readiness`)
- `novatrade/execution/trading_agent.py` (1630 lines) — full alert pipeline: validate → idempotency → risk → supervisor → MetaApi adapter, for `signal_alert / trail_alert / cancel_alert / close_alert`
- `docs/demo_test_run/alerts_schema.json` — JSON-schema contract (v5.0.0)
- `configs/pinescript/irb_v5_m5_champion.pine` — already emits the three alert types (signal, cancel, time-stop) via `alert(p, alert.freq_once_per_bar_close)`
- nginx at `https://nova-link.duckdns.org/webhook/alert` already proxies to `127.0.0.1:8877` with Let's Encrypt TLS

Currently `/etc/novacore/novatrade.env` sets `NOVATRADE_PIPELINE=live` (local-Python live_loop, dry-run, vault-validation work), so the webhook code path is dormant. `/status` confirms `"pipeline":"live"`. The existing runner stays untouched; this design adds a second instance.

## Architecture

```
TradingView (Pine v5 webhook fork)
    │
    │ HTTPS POST {alert JSON}
    ▼
nginx (nova-webhook.duckdns.org)         ← new subdomain + Let's Encrypt cert
    │
    │ proxy_pass 127.0.0.1:8878
    ▼
novacore-novatrade-webhook.service       ← new systemd unit, separate env
    │   NOVATRADE_PIPELINE=webhook
    │   NOVATRADE_PORT=8878
    │   METAAPI_ACCOUNT_ID=<new IC Markets demo>
    │   NOVATRADE_DRY_RUN=false
    │   NOVATRADE_CAMPAIGN_LABEL=ic-markets-demo-2026-q2
    ▼
TradingAgent.process_alert()
    │ schema validate → idempotency → RiskEngine → HardRiskSupervisor
    ▼
MetaApiAdapter ──► IC Markets cTrader Demo (non-expiring)
```

The existing `novacore-novatrade.service` (port 8877, FTMO trial demo, live_loop) **keeps running unchanged** — it is the vault-port validation track. The two services share zero state.

## Resolved decisions

### D1. Run a second runner instance, don't switch the existing one
**Reasoning:** existing runner is doing useful vault-engine validation work in dry-run; killing it loses that. Operator explicitly asked for a "separate workstream."
**Trade-offs:** two systemd units, two ports, two MetaApi accounts. Worth it for clean isolation.

### D2. Provision a fresh non-expiring broker demo via MetaApi cloud — IC Markets cTrader Demo
**Reasoning:** current FTMO trial demo expires every 14 days (per memory `project_metaapi_account_rotation.md`). Webhook validation needs a sustained runway. IC Markets cTrader demo accounts work cleanly through MetaApi cloud and do not expire.
**Trade-offs:** spread/fill behavior differs from FTMO. Acceptable — this pipeline validates the *signal path*, not P&L proof. Documented as such.
**Considered:** stay on OANDA and rotate every 14d (too much friction); use the existing FTMO trial (conflicts with vault-port runner); Pepperstone demo (also viable; IC Markets chosen for tighter EURUSD spread).

### D3. Fork Pine champion → `irb_v5_m5_webhook.pine` and add missing management alerts
**Reasoning:** champion's alert payload hard-codes `broker_symbol: "EURUSD.sim"`, `campaign: "ftmo-free-trial-march-2026"`, and `WH_SECRET` — these need to differ for the new demo. **Additionally, the champion currently has ATR-trail, breakeven-at-+1R, and partial-exit logic that runs inside Pine's `strategy.exit/strategy.close` calls but emits NO webhook alerts.** Without those alerts, the broker-side SL would stay at its initial level for the trade's full life — materially different from the validated Pine baseline.
**Scope of fork:**
1. Constants block: `WH_SECRET`, `broker_symbol`, `CAMPAIGN`
2. **NEW: emit `MODIFY_SL` alert when `cur_stop` changes** (covers both ATR trail and breakeven move — both already update `cur_stop`)
3. Partial exits (PARTIAL_TP at +1R) **deferred to v2** of this pipeline (see D9)
**Trade-offs:** two Pine files; ~30 lines of new alert-emit logic. Strategy decision logic stays identical — only the alert surface and constants diverge.

### D9. v1 defers partial exits; documents the expected divergence
**Reasoning:** Pine champion fires partial exits at +1R for PARTIAL_PCT of position size. Broker-side replication requires a new `partial_alert` schema action and a partial-close handler in TradingAgent (not currently wired). That's a non-trivial scope expansion. v1 ships without partials, accepting that webhook-runner P&L will diverge from pure-Pine by the partial-exit smoothing component (~5–10% drawdown-variance difference is the rough order; expectancy should be close since partial-R = BE-R for this strategy).
**Trade-offs:** documented imperfect parity; faster to v1; partial-alert support is the natural v2 add. Reconciliation script (D8) will explicitly flag partial-exit divergence as expected/known, not as a real-mismatch.
**Considered:** ship full partial support in v1 (adds schema action, TradingAgent partial-close handler, ~80 LoC + tests, doubles scope); skip trail/BE too (rejected — that breaks the strategy's core management logic).

### D4. Bump alert schema to v5.1 — relax `broker_symbol` and `campaign` from `const` to `string` / `enum`
**Reasoning:** schema-level lock-in to FTMO-specific constants blocks reuse across broker demos. Strict validation stays on the load-bearing fields (action, side, entry/SL prices, idempotency-relevant fields).
**Trade-offs:** schema migration + minor TradingAgent patch to read campaign from env (`NOVATRADE_CAMPAIGN_LABEL`). ~30 LoC.

### D5. Keep full HardRiskSupervisor + FTMO-grade guards on the demo runner
**Reasoning:** point of this pipeline is to *prove* Pine alerts produce FTMO-passing trades when the supervisor watches. Relaxing guards defeats that. The `rejection_telegram` autouse-fixture leak is fixed (memory: `project_validate_guards_bug.md`, task 0790).
**Trade-offs:** demo trades feel real with real halts/limits. Intended.

### D6. New subdomain `nova-webhook.duckdns.org` for the second instance, not a path prefix on the existing host
**Reasoning:** cleanest routing. Avoids tangling two webhook receivers behind one host. ~5 min of DNS + certbot.
**Trade-offs:** one more DNS record + cert renewal to monitor.

### D7. Trust Pine alerts for signal/trail/BE/cancel/time-stop; rely on broker SL for hard stops
**Reasoning:** Webhook fork emits 4 alert types (signal, MODIFY_SL covering trail+BE, cancel, time-stop). Hard SL hits are managed by broker — no alert needed. IRB v5 has no take-profit. Expected alert volume ~15–25/day (1–2 trades × ~10 trail/BE updates per trade × bar-close cadence on EURUSD M5), well inside TV Pro plan quota.
**Trade-offs:** if TV is down or our endpoint blips during a MODIFY_SL alert, that bar's tighten is missed but the next bar's trail update will re-emit. Net miss is bounded to one bar of trail. Mitigated further by D8.

### D8. Add `scripts/diff_pine_alerts_vs_metaapi.py` daily reconciliation
**Reasoning:** TV doesn't retry alerts. Need an out-of-band check that every alert produced a matching MetaApi action and that MetaApi positions match expected state. Mirrors the existing `diff_vault_vs_nova_ledger.py` pattern.
**Trade-offs:** one new script (~150 LoC). Cheap insurance.

## Components

| Unit | Purpose | New/Existing |
|------|---------|--------------|
| `configs/pinescript/irb_v5_m5_webhook.pine` | Forked Pine with broker-symbol/campaign/secret + new MODIFY_SL emit for trail/BE | new (fork of `irb_v5_m5_champion.pine`) |
| `docs/demo_test_run/alerts_schema_v5_1.json` | Schema with relaxed `broker_symbol` and `campaign` | new (bumped from v5.0.0) |
| `novatrade/execution/trading_agent.py` | Read campaign from env; accept v5.1 schema | patch (~30 LoC) |
| `configs/novatrade.webhook.env` | Env file for second instance (committed template; secret values via `/etc/novacore/novatrade-webhook.env`) | new |
| `/etc/novacore/novatrade-webhook.env` | Production env file (operator-installed, mode 600, holds METAAPI_TOKEN, METAAPI_ACCOUNT_ID, NOVATRADE_WEBHOOK_SECRET) | new (operator) |
| `/etc/systemd/system/novacore-novatrade-webhook.service` | Second systemd unit, EnvironmentFile points at new env | new (operator-installed) |
| `/etc/nginx/sites-enabled/nova-webhook` | nginx config + Let's Encrypt cert for new subdomain | new (operator-installed) |
| MetaApi cloud account (IC Markets cTrader Demo) | Broker connection | new (operator-provisioned via metaapi.cloud UI) |
| `scripts/diff_pine_alerts_vs_metaapi.py` | Daily reconciliation: evidence-trail alerts vs MetaApi deals | new |
| `tests/test_webhook_demo_pipeline.py` | E2E: fake POST → agent → mock adapter → asserted intents | new |
| `tests/test_alerts_schema_v5_1.py` | Schema migration tests | new |

## Data flow / contract

Pine champion currently emits 3 payload shapes; the webhook fork adds a 4th:

1. `PLACE_STOP_ORDER` / `REPLACE_STOP_ORDER` — entry stop order *(already in champion)*
2. `CANCEL_ORDER` — trigger window expired *(already in champion)*
3. `CLOSE_POSITION` — time stop *(already in champion)*
4. `MODIFY_SL` — ATR-trail tightening + breakeven move *(NEW in webhook fork)*

Partial-exit (`PARTIAL_TP`) emission is deferred to v2 (see D9). Hard SL hits are managed by the broker (no alert). IRB v5 has no take-profit.

Schema v5.1 keeps all required fields strict; relaxes only `broker_symbol` (any string matching the configured symbol map) and `campaign` (enum of known campaigns). TradingAgent reads `NOVATRADE_CAMPAIGN_LABEL` from env and validates payload `campaign` matches.

Idempotency key is already deterministic from `(action, bar_close_time, side)` — replays are no-ops.

## Error handling

- **Bad JSON / missing secret / schema fail** → 400/403, logged to evidence trail (already wired in `webhook_server.py`)
- **TradingAgent rejects (risk halt, supervisor veto)** → 200 with `ok:false, rejected_reason:…` (already wired)
- **MetaApi failure** → captured in `AgentResult.error`, evidence-recorded, surfaces on `/status`
- **TV alert delivery failure / endpoint down** → daily reconciliation script (D8) catches divergence next morning
- **Supervisor halt persists across restart** → existing `/control/resume` endpoint clears it (already wired)
- **Idempotency collision** → existing dedupe in TradingAgent suppresses duplicate; logged

## Testing

### Unit
- TradingAgent already has `tests/test_trading_agent.py`. Add cases for v5.1 schema acceptance + env-driven campaign validation.
- New `tests/test_alerts_schema_v5_1.py` — verify v5.1 accepts non-FTMO broker symbols and rejects unknown campaigns.

### Integration
- New `tests/test_webhook_demo_pipeline.py` — POST forged alert payloads to FastAPI test client → mock MetaApiAdapter → assert order intent state machine, idempotency, supervisor wiring.

### Live smoke (manual, on demo only)
- Operator triggers a Pine alert manually from TradingView; verify webhook receives, agent processes, MetaApi sees the order on the IC Markets demo account.
- **Per `CLAUDE.md` "NovaTrade Live System Safety": never invoke supervisor with synthetic data on a live system.** Live smoke uses real Pine bar-close events, not synthesized payloads.

### Reconciliation
- `diff_pine_alerts_vs_metaapi.py` runs daily as a cron, compares evidence-trail alerts (in `OUTPUT/novatrade/`) to MetaApi deals fetched via the existing MetaApi adapter helpers. Reports divergences (alert without deal, deal without alert, parameter mismatch).

## Operational runbook (post-implementation)

1. Operator provisions IC Markets cTrader Demo via metaapi.cloud, captures account ID + token.
2. Operator installs `/etc/novacore/novatrade-webhook.env` (mode 600) with secrets.
3. Operator installs `/etc/systemd/system/novacore-novatrade-webhook.service` and enables it.
4. Operator adds `nova-webhook.duckdns.org` DNS A record pointing at the VPS, runs `certbot --nginx`, installs `/etc/nginx/sites-enabled/nova-webhook`.
5. Operator opens TradingView, loads `irb_v5_m5_webhook.pine` on EURUSD M5, creates an "Any alert() function call" alert pointed at `https://nova-webhook.duckdns.org/webhook/alert`.
6. Verify `/health` and `/status` on the new endpoint return `ok` and `pipeline:webhook`.
7. Trigger a manual smoke (live bar close).
8. Cron `diff_pine_alerts_vs_metaapi.py` daily.

## Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| TV alert quota exceeded | Pro plan supports >100 alerts/day; expected ~10/day. Monitor `/status.alerts_received` counter. |
| TV outage drops an alert | Daily reconciliation (D8) catches; supervisor halts on unexpected state. |
| Endpoint downtime | systemd `Restart=on-failure` + nginx healthcheck; alerts routed by webhook secret only — replay-safe via idempotency. |
| Schema v5.1 breaks existing v5.0.0 callers | Backward-compatible: v5.0.0 payloads still pass v5.1 (relaxation only). Existing runner is on `live` pipeline anyway, doesn't ingest schema. |
| IC Markets demo data quality differs from FTMO | This pipeline is for *signal-path* validation, not performance proof. Documented in spec. |
| v1 lacks partial-exit alerts → P&L diverges from pure-Pine baseline | Documented as known divergence (D9). Reconciliation script flags it as expected, not real-mismatch. v2 adds partial-alert support. |
| Two MetaApi accounts hit rate limits | Each account has its own MetaApi quota. Existing rate-limiting integration handles per-account throttling. |

## Coexistence with vault engine port

The webhook pipeline does not depend on the buggy `novatrade/backtest/engine.py` — alerts come from Pine and execute via MetaApi. The vault engine port workstream (`novatrade/strategy/vault_engine.py`, `tests/test_vault_engine_streaming_parity.py`, etc.) continues independently. The webhook instance gives us a real-money parity check (Pine vs MetaApi deals) that doesn't go through the buggy nova engine at all. No conflict.
