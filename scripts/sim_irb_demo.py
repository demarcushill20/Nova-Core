"""Quick demo-account simulation of the IRB-H1 strategy from the labelled-setup
dataset (net-R per trade, already net of 0.5p cost). Compounds equity at a few
risk-per-trade levels and reports the metrics that matter for a demo/prop run.

NOTE: this is the OPTIMISTIC bound — backtest fills are idealised (fixed 0.5p
cost, no variable spread / slippage / weekend gaps). A real demo with broker
fills would track at or below these numbers.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

df = pd.read_parquet("data/rl/irb_h1_setups.parquet").sort_values("signal_time").reset_index(drop=True)
r = df["net_r"].to_numpy(float)
t = pd.to_datetime(df["signal_time"])
years = (t.iloc[-1] - t.iloc[0]).days / 365.25
n = len(r)

print(f"trades: {n:,} over {years:.1f}y ({n / years:.0f}/yr) | per-trade net-R mean {r.mean():+.4f} std {r.std():.3f}")
print(f"gross win rate: {(r > 0).mean():.3f}\n")

for rf in (0.0025, 0.005, 0.01):
    eq = 10_000.0
    curve = [eq]
    for x in r:
        eq *= 1 + rf * x
        curve.append(eq)
    curve = np.array(curve)
    peak = np.maximum.accumulate(curve)
    max_dd = float(((curve - peak) / peak).min())
    cagr = (curve[-1] / curve[0]) ** (1 / years) - 1
    per_trade_ret = rf * r
    sharpe = (per_trade_ret.mean() / per_trade_ret.std()) * np.sqrt(n / years)
    yr = df.assign(ret=rf * r, year=t.dt.year).groupby("year")["ret"].sum()
    print(
        f"risk {rf * 100:>4.2f}%/trade | final ${curve[-1]:>9,.0f} | CAGR {cagr * 100:+5.1f}% "
        f"| maxDD {max_dd * 100:5.1f}% | Sharpe {sharpe:+.2f} | yrs+ {(yr > 0).sum()}/{len(yr)} "
        f"| worst yr {yr.min() * 100:+.0f}% best yr {yr.max() * 100:+.0f}%"
    )
