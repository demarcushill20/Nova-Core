# Month-End Rebalancing-Flow demo executor

Trades the validated **month-end rebalancing-flow** edge on a MetaApi demo account.

## The strategy (validated 2026-06-11)

On the **last business day of each quarter**, currency-hedged international equity
investors rebalance FX hedges into the **16:00 London WM/R fix**. When equities rose
over the quarter they must **sell USD** into the fix, so non-USD currencies drift up
pre-fix. We:

1. ~4h before the fix (12:00 London), read the **quarter-to-date S&P 500 return**
   (no look-ahead — prior trading day's close).
2. Open the non-JPY USD basket (long EUR/GBP/AUD/NZD, short USDCAD if S&P up; flipped
   if down), equal notional per leg.
3. **Flatten the whole basket AT the fix (16:00 London).**

Backtest (2004–2024, 84 quarters): quarterly Sharpe **+0.67** (t=3.07, **DSR 0.984**),
**18/21 positive years**, holdout (2017–2024) stronger than train. It is a *modest,
low-frequency* edge (~4 trades/year) — see `OUTPUT/monthend_pnl.py` for the dollar
profile (≈+3%/yr at ~10× leverage / −10% max drawdown). Strategy logic:
`novatrade/strategies/month_end_flow.py` (pure, tested).

## Safety / deployment

- **Defaults to paper mode** (`MEF_DRY_RUN=true`): logs the exact orders it would
  place, no MetaApi contact. Fully exercises the logic.
- Closes **only the positions it opened** (tracked by id in `monthend_state.json`),
  so it is safe even on a shared account — but use a **dedicated demo account**.
- Independent of NovaTrade's live engine / HardRiskSupervisor.
- Next live event: the **last business day of June 2026** (~2026-06-30).

## Configure

Put secrets in `/etc/novacore/monthend.env`:

```
METAAPI_TOKEN=...
METAAPI_ACCOUNT_ID=<dedicated demo account id>
METAAPI_REGION=london
MEF_DRY_RUN=true        # flip to false to execute on the demo
MEF_LEVERAGE=5.0        # total basket notional = leverage x equity
```

## Run

```bash
# paper, foreground:
MEF_DRY_RUN=true python3 monthend_flow.py

# as a service:
sudo cp monthend-flow.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now monthend-flow
journalctl -u monthend-flow -f
```

State is in `monthend_state.json` (restart-safe: tracks date/phase/signal/position ids).
