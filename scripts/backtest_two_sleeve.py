"""Combined two-sleeve backtest (I/O harness). Reconstructs the EUR own-hours monthly
edge from EURUSD M1, loads the carry sleeve from cached FRED data, and reports the
blended book. Carry sleeve (2004+) ~Sharpe 0.41; EUR edge ~uncorrelated to it."""

from __future__ import annotations

import pandas as pd

from novatrade.portfolio.two_sleeve_allocator import AllocatorConfig, combined_backtest, risk_weights
from novatrade.strategies.carry_basket import CarryConfig, carry_backtest
from scripts.probe_carry_portfolio import build_panel


def reconstruct_eur_monthly(csv: str = "data/candles/histdata/EURUSD_M1.csv") -> pd.Series:
    h = pd.read_csv(csv, usecols=["time", "open"], parse_dates=["time"]).set_index("time").sort_index()
    o = h["open"].resample("1h").first().dropna()
    ent = o[(o.index.hour == 11) & (o.index.weekday < 5)]
    rows = {
        ts.normalize(): (px - o[ts + pd.Timedelta(hours=1)]) / px - 0.30e-4
        for ts, px in ent.items()
        if (ts + pd.Timedelta(hours=1)) in o.index
    }
    return pd.Series(rows).resample("ME").sum()


def main() -> None:
    S, R = build_panel()
    carry = carry_backtest(S, R, CarryConfig())
    eur = reconstruct_eur_monthly()
    cfg = AllocatorConfig()
    w_eur, w_carry = risk_weights(cfg.sharpe_eur, cfg.sharpe_carry, cfg.carry_cap)
    out = combined_backtest(eur, carry, w_eur, w_carry)
    print(f"risk weights: EUR {w_eur:.2f} / carry {w_carry:.2f}")
    print(
        f"corr={out['corr']:+.3f}  blended Sharpe={out['blended_sharpe']:+.2f}  "
        f"maxDD={out['maxdd'] * 100:.0f}%  n={out['n']}"
    )


if __name__ == "__main__":
    main()
