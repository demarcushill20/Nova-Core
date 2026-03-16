# FTMO Free Trial — NovaTrade Quick Start

## 1. Environment Setup

Create `/etc/novacore/novatrade.env` (or pass `--env-file <path>`):

```bash
# MetaApi credentials (from metaapi.cloud dashboard)
METAAPI_TOKEN=your_metaapi_token
METAAPI_ACCOUNT_ID=your_metaapi_account_id

# NovaTrade core
NOVATRADE_MODE=DEMO
NOVATRADE_DRY_RUN=true
NOVATRADE_SYMBOLS=EURUSD,GBPUSD,USDJPY

# FTMO profile
FTMO_ENABLED=true
FTMO_CHALLENGE_TYPE=free_trial
FTMO_CAMPAIGN_LABEL=ftmo-free-trial-march-2026

# Optional: if FTMO uses symbol suffixes (check your MT5 terminal)
# FTMO_SYMBOL_SUFFIX=.ftmo

# Optional: explicit symbol mapping overrides
# FTMO_SYMBOL_MAP=EURUSD:EURUSDm,GBPUSD:GBPUSDm

# Optional: nominal account size for reference
# FTMO_ACCOUNT_SIZE=10000
```

## 2. Preflight Check

Verify config + connectivity before any validation work:

```bash
python3 scripts/novatrade_ftmo.py preflight
python3 scripts/novatrade_ftmo.py preflight --env-file /path/to/env
python3 scripts/novatrade_ftmo.py preflight -j   # JSON output
```

Expected: all checks PASS or WARN (no FAIL).

## 3. Dry-Run Execution

Test the full execution pipeline without placing real orders:

```bash
python3 scripts/novatrade_ftmo.py dry-run \
    --symbol EURUSD --side BUY --volume 0.01 --sl 1.0900
```

Expected: `outcome: DENIED` with `dry_run` gate check. This confirms
the entire pipeline works end-to-end.

## 4. Health & Reconciliation Check

Read-only health snapshot + position reconciliation:

```bash
python3 scripts/novatrade_ftmo.py health
python3 scripts/novatrade_ftmo.py health -j   # JSON output
```

## 5. Verdict & Readiness Review

Full evidence review with go/no-go verdict and remediation guidance:

```bash
python3 scripts/novatrade_ftmo.py verdict
python3 scripts/novatrade_ftmo.py verdict -j   # JSON output
```

## 6. Live Demo Execution (Optional)

To actually place orders on the FTMO demo account:

1. Set `NOVATRADE_DRY_RUN=false` in your env file
2. Pass `--live` to the dry-run command:

```bash
python3 scripts/novatrade_ftmo.py dry-run \
    --symbol EURUSD --side BUY --volume 0.01 --sl 1.0900 --live
```

**Safety:** Even with `--live`, the risk gate still enforces all 13
pre-trade checks (DEMO mode only, stop loss required, drawdown limits,
position limits, etc.).

## Notes

- All commands are read-only by default — no trades unless `--live` + `dry_run=false`
- Evidence is written to `OUTPUT/novatrade/evidence.jsonl` with campaign tags
- The FTMO Free Trial lasts 14 days (one active trial at a time)
- Symbol names may vary — check your FTMO MT5 terminal and configure
  `FTMO_SYMBOL_SUFFIX` or `FTMO_SYMBOL_MAP` accordingly
- Risk defaults (5% daily drawdown, SL required) align with FTMO rules
