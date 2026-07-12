# multisleeve-paper — 4-sleeve paper book at 6x (measurement-only)

Paper-trades the validated multi-sleeve book (FX alpha loop winner candidate, commit
`2197f90`; operator-approved at **6x**, 2026-07-12):

| sleeve | weight | source of truth |
|---|---:|---|
| eur_short (11:00→12:00 UTC EURUSD short) | 0.58 | Dukascopy 11:00/12:00 real quotes (spread paid) |
| carry (crash-gated G10 HML, monthly book) | 0.22 | FRED 3M rates + daily spots |
| gold_tsmom (12m+3m sign, 10% vol-target) | 0.12 | Dukascopy XAUUSD close hour |
| btc_gated (same TSMOM × vol-regime gate) | 0.09 | Coinbase public daily candles |

Backtest reference: Sharpe +1.88 full / +1.96 sealed (27mo). At 6x expect ~+22%/yr,
daily vol ~0.75%, maxDD ~−13-15%. **This service places NO orders** — it is a
deterministic daily reconciler that rebuilds the whole paper history from price
caches every run (missed runs and publication lags self-heal). Execution realism for
the 11:00 sleeve is covered separately by the `intraday-drift` demo executor.

## Files
- `multisleeve_paper.py` — reconciler (strategy logic in `novatrade/strategies/multisleeve.py`, tested)
- state: `data/paper_multisleeve/` (`ledger.csv`, `state.json`, price caches)
- weekly report: `OUTPUT/multisleeve_paper_weekly_<date>.md` (Fridays)

## Install (user units)
```sh
cp services/multisleeve-paper/multisleeve-paper.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now multisleeve-paper.timer
```

## Manual run / backfill
```sh
python3 services/multisleeve-paper/multisleeve_paper.py --inception 2026-07-01
```

## Review gate (operator)
After 8-12 weeks compare `state.json` stats vs the backtest reference band. Promote to
funded only if realized Sharpe/DD track; if realized Sharpe < ~0.8 after 12 weeks,
kill and return to the research loop.
