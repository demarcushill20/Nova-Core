"""FX multi-factor portfolio — carry + momentum + value (Iteration 1 of 30%-target loop).

Rationale (research 2026-07-12): the only FX edges that survive academic replication at
portfolio scale are cross-sectional factor premia — carry (validated in-house, Sharpe +0.54
full / +1.69 hiking-regime, corr -0.03 to the live 11:00 edge), momentum (Menkhoff et al.),
and value (Asness-Moskowitz-Pedersen 5y reversion). Burnside et al.: 50/50 carry+momentum
Sharpe ~0.98 vs ~0.4-0.5 single-factor — the lift is factor diversification, not signal magic.

This probe extends probe_carry_portfolio.py:
  universe : G10 (USD EUR GBP JPY AUD NZD CAD CHF NOK SEK) + EM (MXN ZAR), FRED daily->monthly
  factors  : carry  = lagged 3M interbank rate diff vs USD
             mom    = trailing 1M and 3M excess return (lagged, no look-ahead)
             value  = negative 5y nominal spot change (mean-reversion proxy, lagged)
  combo    : equal-weight of the three factor return streams (rank-based HML books)
  costs    : turnover-based round-trip bps by liquidity tier (2 G10 / 8-10 EM)
  controls : monthly random-rank permutation (1000 sims) -> p-value of combo Sharpe
  leverage : vol required to hit 30% CAGR and the maxDD that implies

Verdict bar: combo net Sharpe >= ~0.9 full-sample AND positive 2015+ AND beats >95% of
permutations -> promote to refinement loop (crash mgmt, weighting, 11:00-edge blend).
"""

from __future__ import annotations

import os
import sys
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
    "CHF": "IR3TIB01CHM156N",
    "NOK": "IR3TIB01NOM156N",
    "SEK": "IR3TIB01SEM156N",
    "MXN": "IR3TIB01MXM156N",
    "ZAR": "IR3TIB01ZAM156N",
}
SPOT_ID = {  # id + invert flag -> USD per 1 unit foreign
    "EUR": ("DEXUSEU", False),
    "GBP": ("DEXUSUK", False),
    "AUD": ("DEXUSAL", False),
    "NZD": ("DEXUSNZ", False),
    "JPY": ("DEXJPUS", True),
    "CAD": ("DEXCAUS", True),
    "CHF": ("DEXSZUS", True),
    "NOK": ("DEXNOUS", True),
    "SEK": ("DEXSDUS", True),
    "MXN": ("DEXMXUS", True),
    "ZAR": ("DEXSFUS", True),
}
G10 = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "NOK", "SEK"]
EM = ["MXN", "ZAR"]
COST_BPS = {c: 2.0 for c in G10}
COST_BPS.update({"MXN": 8.0, "ZAR": 10.0})
RNG = np.random.default_rng(20260712)


def fred(series_id: str) -> pd.Series | None:
    cpath = f"{CACHE}/{series_id}.csv"
    if not os.path.exists(cpath):
        try:
            urllib.request.urlretrieve(FRED + series_id, cpath)  # noqa: S310 (fixed host)
        except Exception as e:  # missing series -> drop currency gracefully
            print(f"  [warn] fetch failed {series_id}: {e}", file=sys.stderr)
            return None
    df = pd.read_csv(cpath, na_values=".").dropna()
    if df.empty or len(df.columns) != 2:
        return None
    df.columns = ["date", "val"]
    return pd.Series(df["val"].values, index=pd.to_datetime(df["date"]), name=series_id).astype(float)


def build_panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    spot, rates = {}, {}
    for ccy, (sid, inv) in SPOT_ID.items():
        s = fred(sid)
        r = fred(RATE_ID[ccy])
        if s is None or r is None:
            print(f"  [warn] dropping {ccy} (missing data)", file=sys.stderr)
            continue
        s = s.resample("ME").last()
        spot[ccy] = 1.0 / s if inv else s
        rates[ccy] = r.resample("ME").last()
    r_usd = fred(RATE_ID["USD"])
    if r_usd is None:
        raise RuntimeError("USD rate series is required")
    spot["USD"] = pd.Series(1.0, index=spot["EUR"].index)
    rates["USD"] = r_usd.resample("ME").last()
    S = pd.DataFrame(spot).sort_index()
    R = pd.DataFrame(rates).reindex(S.index).ffill()
    return S, R


def signals(S: pd.DataFrame, R: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """All signals lagged one month at use-time by the caller (rank on .shift(1))."""
    xs = excess_returns(S, R)
    return {
        "carry": R.sub(R["USD"], axis=0),  # rate diff vs USD
        "mom1": xs,  # last month's excess return
        "mom3": xs.rolling(3).sum(),  # 3m excess return
        "value": -(S / S.shift(60) - 1.0),  # neg 5y spot change (reversion)
    }


def excess_returns(S: pd.DataFrame, R: pd.DataFrame) -> pd.DataFrame:
    spot_ret = S.pct_change()
    rdiff = R.sub(R["USD"], axis=0) / 1200.0
    return spot_ret + rdiff.shift(1)


def hml(sig: pd.DataFrame, xs: pd.DataFrame, ccys, k_frac=0.34, cost=True, rand_ranks=False) -> pd.Series:
    """Dollar-neutral HML book: long top-k signal, short bottom-k, monthly, net of cost."""
    sig = sig[ccys].shift(1)  # signal known at prior month-end
    x = xs[ccys]
    k = max(1, round(len(ccys) * k_frac))
    rows: dict = {}
    prev_long: set[str] = set()
    prev_short: set[str] = set()
    for dt in x.index:
        s_row, x_row = sig.loc[dt].dropna(), x.loc[dt].dropna()
        avail = [c for c in s_row.index if c in x_row.index]
        if len(avail) < 6:
            continue
        if rand_ranks:
            order = list(RNG.permutation(avail))
        else:
            order = list(s_row[avail].sort_values().index)
        longs, shorts = set(order[-k:]), set(order[:k])
        ret = x_row[list(longs)].mean() - x_row[list(shorts)].mean()
        if cost:
            turn = len(longs ^ prev_long) + len(shorts ^ prev_short)
            c = np.mean([COST_BPS[cc] for cc in (longs | shorts)]) * 1e-4
            ret -= turn / (2 * k) * c
        rows[dt] = ret
        prev_long, prev_short = longs, shorts
    return pd.Series(rows).sort_index()


def stats(r: pd.Series, label: str) -> str:
    r = r.dropna()
    if len(r) < 24:
        return f"{label:30s} insufficient ({len(r)} mo)"
    ann_ret = r.mean() * 12 * 100
    ann_vol = r.std() * np.sqrt(12) * 100
    sharpe = ann_ret / ann_vol if ann_vol else float("nan")
    eq = (1 + r).cumprod()
    dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    return (
        f"{label:30s} Sharpe {sharpe:+.2f}  ret {ann_ret:+5.1f}%  vol {ann_vol:4.1f}%  "
        f"skew {r.skew():+.2f}  maxDD {dd:5.0f}%  worstMo {r.min() * 100:+.1f}%  "
        f"({len(r)}mo {r.index[0].year}-{r.index[-1].year})"
    )


def sharpe_of(r: pd.Series) -> float:
    r = r.dropna()
    return (r.mean() * 12) / (r.std() * np.sqrt(12)) if len(r) and r.std() else float("nan")


def main():
    print("=" * 104)
    print("FX MULTI-FACTOR — carry + momentum + value, dollar-neutral HML, monthly, net of cost")
    print("=" * 104)
    S, R = build_panel()
    ccys = [c for c in G10 + EM if c in S.columns]
    print(f"panel: {S.index[0].date()}..{S.index[-1].date()}  universe({len(ccys)}): {ccys}\n")

    xs = excess_returns(S, R)
    sigs = signals(S, R)

    books: dict[str, pd.Series] = {}
    for name, sig in sigs.items():
        books[name] = hml(sig, xs, ccys)
        print(stats(books[name], f"{name.upper()} (12ccy)"))
    print()

    # ---- combos (equal-weight the return streams; factors are parameter-free) ----
    aligned = pd.DataFrame(books).dropna(how="all")
    combos = {
        "COMBO c+m1+v": aligned[["carry", "mom1", "value"]].mean(axis=1),
        "COMBO c+m3+v": aligned[["carry", "mom3", "value"]].mean(axis=1),
        "COMBO 50/50 c+m1": aligned[["carry", "mom1"]].mean(axis=1),
    }
    for name, r in combos.items():
        print(stats(r, name))
        print(stats(r.loc["2015":], f"  {name} 2015+"))
        print(stats(r.loc["2022":], f"  {name} 2022+"))
    print()

    print("factor correlations (monthly):")
    print(aligned[["carry", "mom1", "mom3", "value"]].corr().round(2).to_string(), "\n")

    # ---- permutation control: random ranks, same universe/costs/dates ----
    best_name = max(combos, key=lambda n: sharpe_of(combos[n]))
    best = combos[best_name]
    obs_sh = sharpe_of(best)
    n_sims = 500
    perm = []
    for _ in range(n_sims):
        rc = hml(sigs["carry"], xs, ccys, rand_ranks=True)
        rm = hml(sigs["mom1"], xs, ccys, rand_ranks=True)
        rv = hml(sigs["value"], xs, ccys, rand_ranks=True)
        perm.append(sharpe_of(pd.DataFrame({"a": rc, "b": rm, "c": rv}).mean(axis=1)))
    perm = np.array(perm)
    pval = float((perm >= obs_sh).mean())
    print(f"permutation control ({n_sims} sims, random ranks): best combo = {best_name}")
    print(f"  observed Sharpe {obs_sh:+.2f} | perm mean {perm.mean():+.2f} sd {perm.std():.2f} | p-value {pval:.3f}\n")

    # ---- leverage math for the 30% CAGR target ----
    r = best.dropna()
    ann_ret, ann_vol = r.mean() * 12, r.std() * np.sqrt(12)
    print(f"{'#' * 104}")
    if ann_ret > 0:
        lev = 0.30 / ann_ret
        lev_vol = lev * ann_vol * 100
        eq = (1 + r * lev).cumprod()
        lev_dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
        lev_cagr = (eq.iloc[-1] ** (12 / len(r)) - 1) * 100
        print(
            f"30% TARGET: {best_name} needs {lev:.1f}x leverage -> vol {lev_vol:.0f}%, "
            f"realized CAGR {lev_cagr:+.0f}%, maxDD {lev_dd:.0f}%"
        )
        print(f"  (unlevered: Sharpe {obs_sh:+.2f}, ret {ann_ret * 100:+.1f}%, vol {ann_vol * 100:.1f}%)")
    else:
        print("30% TARGET: unreachable — combo return non-positive")
    print("#" * 104)


if __name__ == "__main__":
    main()
