"""EM own-hours / intraday-drift cut — less-efficient-market alpha test.

The majors own-hours test found the effect is EUR-specific (rollover artifacts
elsewhere). EM currencies are less efficient -> potentially larger gross intraday
drift. BUT EM intraday spreads run ~10x majors in bps, so the edge must clear a
much higher cost wall (the strategy_search wall). This tests whether it does.

For each EM USD-pair: the local currency depreciates in its own session => USDxxx
RISES => dir +1. Sessions pre-registered from local market hours (UTC). We also run
an empirical per-hour weakness scan to see where each currency is ACTUALLY weak
(majors lesson: pre-registered window often misses; rollover hours are artifacts).

Costs are conservative round-trip spreads in bps (fraction of price), NOT pips,
since EM handles vary wildly (USDTRY 3->35 over the sample).

Data: HistData M1, fetched 2015-2024. Worked in % returns (cross-pair comparable).
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

CACHE = "data/candles/_em_cache"
os.makedirs(CACHE, exist_ok=True)

# dir=+1 (USDxxx up when EM ccy weak). session start/end UTC from local market hrs.
# cost_bps = conservative round-trip spread as bps of price.
EM = {
    "USDMXN": dict(d=+1, s=13, e=21, cost_bps=3.0, ccy="MXN", local="Mexico/NY 13-21"),
    "USDZAR": dict(d=+1, s=7, e=15, cost_bps=3.0, ccy="ZAR", local="Johannesburg 07-15"),
    "USDTRY": dict(d=+1, s=6, e=14, cost_bps=5.0, ccy="TRY", local="Istanbul 06-14"),
    "USDSGD": dict(d=+1, s=1, e=9, cost_bps=3.0, ccy="SGD", local="Singapore 01-09"),
}


def hourly(pair: str) -> pd.DataFrame | None:
    cpath = f"{CACHE}/{pair}_1h.parquet"
    if os.path.exists(cpath):
        return pd.read_parquet(cpath)
    csv = f"data/candles/histdata/{pair}_M1.csv"
    if not os.path.exists(csv):
        return None
    df = pd.read_csv(csv, usecols=["time", "open", "close"], parse_dates=["time"]).set_index("time").sort_index()
    o = df["open"].resample("1h").first()
    c = df["close"].resample("1h").last()
    h = pd.DataFrame({"open": o, "close": c}).dropna()
    if h.index.tz is None:
        h.index = h.index.tz_localize("UTC")
    h.to_parquet(cpath)
    return h


def hour_scan(pair: str) -> pd.Series:
    """t-stat of LOCAL-CURRENCY-WEAKNESS by UTC hour (positive = EM ccy weak)."""
    h = hourly(pair)
    r = (h["close"] - h["open"]) / h["open"] * 1e4 * EM[pair]["d"]  # weakness-signed bps
    g = r.groupby(h.index.hour)
    return (g.mean() / (g.std() / np.sqrt(g.count()))).round(2)


def session_trades(pair: str) -> pd.Series:
    cfg = EM[pair]
    h = hourly(pair)
    dur = (cfg["e"] - cfg["s"]) % 24
    ent = h[(h.index.hour == cfg["s"]) & (h.index.weekday < 5)]
    rows = {}
    for ts, row in ent.iterrows():
        exit_ts = ts + pd.Timedelta(hours=dur)
        if exit_ts not in h.index:
            continue
        entry, exit_px = row["open"], h.loc[exit_ts, "open"]
        ret = cfg["d"] * (exit_px - entry) / entry
        rows[ts.normalize()] = ret - cfg["cost_bps"] * 1e-4
    return pd.Series(rows, name=pair).sort_index()


def stats(r: pd.Series) -> dict:
    r = r.dropna()
    if len(r) < 30:
        return dict(n=len(r), t=np.nan, sharpe=np.nan, mean_bps=np.nan, win=np.nan)
    return dict(
        n=len(r),
        mean_bps=r.mean() * 1e4,
        t=r.mean() / (r.std() / np.sqrt(len(r))),
        sharpe=r.mean() / r.std() * np.sqrt(252),
        win=(r > 0).mean(),
    )


def main():
    avail = [p for p in EM if hourly(p) is not None]
    print(f"{'=' * 80}\nEM CROSSES — own-hours / intraday-drift cut   (available: {avail})\n{'=' * 80}")
    if not avail:
        print("no EM data yet.")
        return

    print("\nPer-hour LOCAL-CURRENCY-WEAKNESS t-stat (positive = EM ccy weak that UTC hour):")
    for p in avail:
        sc = hour_scan(p)
        h = hourly(p)
        span = f"{h.index.min().date()}..{h.index.max().date()}"
        top = sc.sort_values(ascending=False).head(4)
        s, e = EM[p]["s"], EM[p]["e"]
        insess = [hh for hh in top.index if (s <= hh < e if s < e else (hh >= s or hh < e))]
        print(
            f"  {p} [{span}] session {EM[p]['local']}: peak-weak hours "
            + ", ".join(f"{hh}:{sc[hh]:+.1f}" for hh in top.index)
            + f"  | in-session: {insess}"
        )

    print(
        f"\n{'pair':7s} {'n':>5s} {'mean_bps':>8s} {'t':>7s} {'Sharpe':>7s} {'win%':>6s}  {'IS_t':>6s} {'OOS_t':>6s} {'OOS_Shrp':>8s}  cost"
    )
    print("-" * 88)
    series = {}
    for p in avail:
        r = session_trades(p)
        series[p] = r
        st = stats(r)
        cut = int(len(r.dropna()) * 0.6)
        iss, oos = stats(r.dropna().iloc[:cut]), stats(r.dropna().iloc[cut:])
        print(
            f"{p:7s} {st['n']:5d} {st['mean_bps']:+8.2f} {st['t']:+7.2f} {st['sharpe']:+7.2f} {st['win'] * 100:5.1f}  "
            f"{iss['t']:+6.2f} {oos['t']:+6.2f} {oos['sharpe']:+8.2f}  {EM[p]['cost_bps']:.1f}bps"
        )

    if len(series) > 1:
        M = pd.DataFrame(series).sort_index()
        corr = M.corr()
        print(f"\ncorrelation:\n{corr.round(2).to_string()}")
        n = corr.shape[0]
        w = np.ones(n) / n
        print(f"avg corr {corr.values[np.triu_indices(n, 1)].mean():+.3f}  N_eff {1 / (w @ corr.values @ w):.2f}/{n}")
        vol = M.std()
        wiv = (1 / vol) / (1 / vol).sum()
        port = (M * wiv).sum(axis=1, min_count=1).dropna()
        pst = stats(port)
        cut = int(len(port) * 0.6)
        poos = stats(port.iloc[cut:])
        eq = (1 + port).cumprod()
        dd = ((eq - eq.cummax()) / eq.cummax()).min()
        print(
            f"\nPORTFOLIO: full Sharpe {pst['sharpe']:+.2f} t {pst['t']:+.2f} maxDD {dd * 100:.0f}%  | "
            f"OOS Sharpe {poos['sharpe']:+.2f} t {poos['t']:+.2f}"
        )
        go = poos["sharpe"] >= 0.8 and min(stats(s)["t"] for s in series.values()) > 1.5
        print(f"VERDICT: {'WORTH DEEPER LOOK' if go else 'NO-GO — EM own-hours does not clear cost OOS'}")


if __name__ == "__main__":
    main()
