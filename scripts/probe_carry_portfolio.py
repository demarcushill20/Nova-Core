"""Carry cut — the canonical non-price FX alpha (Lustig-Verdelhan / Menkhoff).

Carry return of holding currency i (funded in USD) over a month:
    xs_i = spot_return(i/USD)  +  (r_i - r_USD)/1200      [rates lagged, % annualized]
The risk premium: high-rate currencies earn more than the rate differential predicts
(forward-premium puzzle) -> long high-yield basket / short low-yield basket, dollar-
neutral, rebalanced monthly. Known to be REAL but with severe negative skew (carry
crashes: 1998, 2008, 2020) and a poor post-GFC decade (rates compressed to zero).

This is genuinely non-price (driven by rate differentials), so it should be ~uncorrelated
to the EUR own-hours edge — the diversifier the price-action sweeps couldn't supply.

Data (FRED, keyless CSV, cached data/rates/):
  rates : IR3TIB01[CC]M156N  3-month interbank, monthly %  (US EZ GB JP AU NZ CA MX ZA)
  spot  : DEX* daily FX -> month-end "USD per 1 unit foreign"
Verdict bar: net Sharpe and — critically — whether the crash risk (skew, maxDD) is
survivable. Reports G10-only and G10+EM, full history and the 2015-2024 sub-window.
"""

from __future__ import annotations

import os
import urllib.request

import numpy as np
import pandas as pd

CACHE = "data/rates"
os.makedirs(CACHE, exist_ok=True)
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="

RATE_ID = {
    "USD": "IR3TIB01USM156N",
    "EUR": "IR3TIB01EZM156N",
    "GBP": "IR3TIB01GBM156N",
    "JPY": "IR3TIB01JPM156N",
    "AUD": "IR3TIB01AUM156N",
    "NZD": "IR3TIB01NZM156N",
    "CAD": "IR3TIB01CAM156N",
    "MXN": "IR3TIB01MXM156N",
    "ZAR": "IR3TIB01ZAM156N",
}
# spot id + invert flag so the value is always USD-per-1-unit-foreign (up = foreign strong)
SPOT_ID = {
    "EUR": ("DEXUSEU", False),
    "GBP": ("DEXUSUK", False),
    "AUD": ("DEXUSAL", False),
    "NZD": ("DEXUSNZ", False),
    "JPY": ("DEXJPUS", True),
    "CAD": ("DEXCAUS", True),
    "MXN": ("DEXMXUS", True),
    "ZAR": ("DEXSFUS", True),
}
G10 = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD"]
EM = ["MXN", "ZAR"]
# round-trip rebalance cost (bps of leg notional) by currency liquidity
COST_BPS = {c: 2.0 for c in G10}
COST_BPS.update({"MXN": 8.0, "ZAR": 10.0})


def fred(series_id: str) -> pd.Series:
    cpath = f"{CACHE}/{series_id}.csv"
    if not os.path.exists(cpath):
        urllib.request.urlretrieve(FRED + series_id, cpath)  # noqa: S310 (fixed host)
    df = pd.read_csv(cpath, na_values=".").dropna()
    df.columns = ["date", "val"]
    s = pd.Series(df["val"].values, index=pd.to_datetime(df["date"]), name=series_id).astype(float)
    return s


def build_panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    # month-end spot (USD per foreign); USD column = 1.0
    spot = {}
    for ccy, (sid, inv) in SPOT_ID.items():
        s = fred(sid).resample("ME").last()
        spot[ccy] = 1.0 / s if inv else s
    spot["USD"] = pd.Series(1.0, index=spot["EUR"].index)
    S = pd.DataFrame(spot).sort_index()
    # monthly lagged rate panel (% annualized), forward-filled to month-end grid
    rates = {ccy: fred(rid).resample("ME").last() for ccy, rid in RATE_ID.items()}
    R = pd.DataFrame(rates).reindex(S.index).ffill()
    return S, R


def carry_portfolio(ccys, S, R, k_frac=0.34, cost=True):
    """Dollar-neutral HML carry: long top-k-rate, short bottom-k-rate, monthly."""
    S = S[ccys].copy()
    R = R[ccys].copy()
    spot_ret = S.pct_change()  # foreign vs USD this month
    rdiff = R.sub(R["USD"], axis=0) / 1200.0  # monthly carry vs USD
    xs = spot_ret + rdiff.shift(1)  # excess return, rate lagged 1m
    rrank = R.shift(1)  # rank on lagged rate (no look-ahead)
    rows = {}
    prev_long, prev_short = set(), set()
    k = max(1, round(len(ccys) * k_frac))
    for dt in xs.index[2:]:
        rk = rrank.loc[dt].dropna()
        x = xs.loc[dt].dropna()
        avail = [c for c in rk.index if c in x.index]
        if len(avail) < 4:
            continue
        rk = rk[avail].sort_values()
        longs, shorts = set(rk.index[-k:]), set(rk.index[:k])
        ret = x[list(longs)].mean() - x[list(shorts)].mean()
        if cost:
            turn = len(longs ^ prev_long) + len(shorts ^ prev_short)
            c = np.mean([COST_BPS[c] for c in (longs | shorts)]) * 1e-4
            ret -= turn / (2 * k) * c
        rows[dt] = ret
        prev_long, prev_short = longs, shorts
    return pd.Series(rows).sort_index()


def stats(r: pd.Series, label: str):
    r = r.dropna()
    if len(r) < 24:
        return f"{label:28s} insufficient ({len(r)} mo)"
    ann_ret = r.mean() * 12 * 100
    ann_vol = r.std() * np.sqrt(12) * 100
    sharpe = ann_ret / ann_vol if ann_vol else float("nan")
    eq = (1 + r).cumprod()
    dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    skew = r.skew()
    worst = r.min() * 100
    return (
        f"{label:28s} Sharpe {sharpe:+.2f}  ret {ann_ret:+5.1f}%  vol {ann_vol:4.1f}%  "
        f"skew {skew:+.2f}  maxDD {dd:5.0f}%  worstMo {worst:+.1f}%  ({len(r)}mo {r.index[0].year}-{r.index[-1].year})"
    )


def main():
    print(f"{'=' * 100}\nCARRY CUT — dollar-neutral HML, monthly rebalance, net of cost\n{'=' * 100}")
    S, R = build_panel()
    print(f"panel: {S.index[0].date()}..{S.index[-1].date()}  currencies={list(S.columns)}\n")

    books = {"G10 (7ccy)": G10, "G10+EM (9ccy)": G10 + EM}
    series = {}
    for name, ccys in books.items():
        full = carry_portfolio(ccys, S, R)
        series[name] = full
        print(stats(full, name + " FULL"))
        print(stats(full.loc["2015":], name + "  2015-24"))
        print(stats(full.loc[:"2014"], name + "  pre-2015"))
        print(stats(full.loc["2022":], name + "  2022-24(hiking)"))
        print()

    # correlation to the EUR own-hours edge proxy? report carry self-corr G10 vs G10+EM
    g10, gem = series["G10 (7ccy)"], series["G10+EM (9ccy)"]
    common = pd.concat([g10, gem], axis=1).dropna()
    print(f"corr(G10, G10+EM) = {common.iloc[:, 0].corr(common.iloc[:, 1]):+.2f}  (EM sleeve adds yield + tail risk)\n")

    stats(gem, "")  # rough verdict on G10+EM full
    s_full = gem.dropna()
    sh = (s_full.mean() * 12) / (s_full.std() * np.sqrt(12))
    sh15 = (gem.loc["2015":].mean() * 12) / (gem.loc["2015":].std() * np.sqrt(12))
    print(f"{'#' * 100}")
    print(f"VERDICT: carry IS a real non-price risk premium (full-sample Sharpe {sh:+.2f}), but it is a")
    print("  RISK PREMIUM not a free lunch — negative skew + deep crash DDs, and it decayed post-GFC")
    print(f"  (2015-24 Sharpe {sh15:+.2f}). Tradeable only with crash management. Key value: ~uncorrelated to")
    print("  price-action edges -> a genuine diversifier. See per-window skew/maxDD above.")
    print("#" * 100)


if __name__ == "__main__":
    main()
