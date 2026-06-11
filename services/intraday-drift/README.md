# Intraday EUR-Weakness Drift — demo executor

Trades the validated **11:00–12:00 UTC EURUSD-weakness** edge (option A of the
2026-06-11 intraday-edge search): **SHORT EURUSD at 11:00 UTC, flat at 12:00
UTC**, one trade per weekday, sized so a fixed protective stop risks **1% of
equity**. Self-contained and independent of NovaTrade's live engine / risk
supervisor — point it at a **dedicated demo account** (like `irb-bridge` and
`monthend-flow`).

## The edge

EURUSD systematically depreciates during the European afternoon / pre-NY hour
(Breedon–Ranaldo "own-hours depreciation"). The 11:00–12:00 UTC window is the
single most significant intraday signal measured:

- **t = −6.9** on HistData 2004–2015, and an independent **t = −5.2** on
  2016–2026 Dukascopy ticks (different vendor, different era, clean true-UTC clock).
- Out-of-sample on real ticks net of the measured **0.30p** spread, at 1% risk
  with a 15–20p stop: **~8–11% CAGR, ~20% max drawdown, ~52% win rate.**

It is a **modest, real grinder edge** — not a jackpot. Win rate is ~50%; the
profit is a small persistent asymmetry repeated daily, which is exactly why
disciplined 1% sizing and the protective stop matter.

## Sizing & risk

`lots = (risk_frac × equity) / (stop_pips × $10/pip/lot)`. With the defaults
(1% risk, 20p stop) a $100k demo trades 5.0 lots; the stop is attached **at
entry as a broker-side stop**, so the 1% cap holds even if this process is down
at the 12:00 UTC time-exit. Stop choice is a real dial (tighter = higher CAGR
*and* deeper drawdown): 15p ≈ 11% CAGR / −22% DD, 20p ≈ 8% / −19%, 30p ≈ 7% / −10%.

## Run

```bash
# paper mode (default — no MetaApi, just logs the order it would place):
python3 intraday_drift.py

# live on the demo:  set a DEDICATED demo account, then
#   IDF_DRY_RUN=false  in /etc/novacore/intraday-drift.env
```

### systemd

```bash
sudo cp intraday-drift.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now intraday-drift
journalctl -u intraday-drift -f
```

## Env (`/etc/novacore/intraday-drift.env`)

| var | default | meaning |
|-----|---------|---------|
| `METAAPI_TOKEN` | — | MetaApi token (live only) |
| `METAAPI_ACCOUNT_ID` | — | **dedicated** demo account id |
| `METAAPI_REGION` | london | MetaApi region |
| `IDF_DRY_RUN` | true | paper mode; set false to execute |
| `IDF_STOP_PIPS` | 20 | protective stop distance (also sets size) |
| `IDF_RISK_FRAC` | 0.01 | risk per trade (1%) |
| `IDF_ENTRY_HOUR_UTC` | 11 | short at the top of this UTC hour |
| `IDF_EXIT_HOUR_UTC` | 12 | flat at the top of this UTC hour |
| `IDF_POLL_SECONDS` | 60 | scheduler poll cadence |
| `IDF_PAPER_EQUITY` | 100000 | equity used for sizing in paper mode |

## Caveats / future refinements

- **UTC clock, by design.** The 11:00 UTC entry is what was backtested. Whether
  the effect locks to UTC or to London-local time (which shifts ±1h with DST) is
  **not yet validated** — a DST-aware session clock is a candidate refinement,
  not shipped here. Do not change the hour live without re-running the gauntlet.
- **No strategy-param changes without a backtest** (NovaTrade standing rule).
- Backtest is gross of swap/financing; the 11:00–12:00 UTC hold does not cross
  the 17:00-NY rollover, so swap impact is negligible.
- Closes only the position it opened (tracked id + `IDF` magic-comment safety net).
