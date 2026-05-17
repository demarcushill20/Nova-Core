# TIMBOT Backtest Baseline — XAUUSD 15m

- **Instrument / timeframe:** XAUUSD (Gold) / 15m
- **History window:** ____ → ____  (target: 6–12 months — 15m bars are denser, TradingView caps bar count per plan)
- **Strategy version:** timbot_official_universal.pine (v2.0)
- **Run date:** ____

## Auto-detected engine config (for this TF — verify in Debug table)

| Item                  | Expected value      |
|-----------------------|---------------------|
| FROTE tier            | 2 — DAILY→WEEKLY    |
| Sweep TF              | 60 (1h)             |
| Runner target tier    | Weekly 78.6%        |

## Strategy settings used (locked 2026-05-17)

| Setting          | Value          | Notes                                          |
|------------------|----------------|------------------------------------------------|
| Initial capital  | 100,000        | Client demo account size                       |
| Order size       | 5 % of equity  | Was 100 % — caused 56 margin calls; see FINDINGS |
| Commission       | broker XAUUSD  | Set to demo broker's real gold commission      |
| Slippage (ticks) | 1–3            | Gold spreads widen; never use 0                |
| Pyramiding       | 0              |                                                |
| Recalculate      | bar close      |                                                |

> First run (Order size = 100) is void — see `FINDINGS_XAUUSD_15m_2026-05-17.md`.
> This baseline is the corrected re-run.

## Inputs (non-default only)

| Input | Value | Reason |
|-------|-------|--------|
|       |       |        |

## Performance summary

| Metric              | Value |
|---------------------|-------|
| Net profit          |       |
| Net profit %        |       |
| Total closed trades |       |
| Win rate %          |       |
| Profit factor       |       |
| Max drawdown %      |       |
| Avg trade           |       |
| Avg win / avg loss  |       |
| Largest losing trade|       |
| Sharpe              |       |
| Sortino             |       |

## Notes / observations

- Default mode is `signalOnFlipOnly = true` — entries fire on 4H bias flips only.
  15m gives more bars but flips are still driven by the 4H bias, so the trade
  count rises mainly from finer entry timing, not more flip events. If still
  thin, extend history.

## Artifacts

- Trade list CSV: `backtest_XAUUSD_15m_<date>.csv`
- Performance screenshot: `backtest_XAUUSD_15m_<date>.png`
