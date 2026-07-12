"""FX multi-factor v2 — vol-aware refinement (Iteration 2 of 30%-target loop).

v1 result: carry+mom+value combo REAL (p=0.000 vs permutation) but Sharpe +0.47 full /
+0.17 2015+ -> 30% CAGR needs 15x leverage. Dead standalone. v2 applies the documented
structural lifts, each with a citation-grade rationale, no fitted parameters:

  1. inverse-vol weighting inside each HML book (risk-balanced legs, standard practice)
  2. carry crash-gate: scale carry book by global-FX-vol regime (Menkhoff et al. 2012a:
     carry losses concentrate in high-vol states; gate = trailing vol pctile, no fit)
  3. risk-parity combo across factor streams (inverse trailing 36m vol, standard)
  4. universe breadth: +DKK +KRW (FRED has both rate & spot)

Same gauntlet: net of costs, permutation control, sub-window stats, leverage math.
Decision bar: combo Sharpe >= 0.8 full AND >= 0.5 on 2015+ -> keep refining this family;
else log family as 'real-but-thin' and pivot the loop to the portfolio-blend route.
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
    "DKK": "IR3TIB01DKM156N",
    "KRW": "IR3TIB01KRM156N",
    "MXN": "IR3TIB01MXM156N",
    "ZAR": "IR3TIB01ZAM156N",
}
SPOT_ID = {
    "EUR": ("DEXUSEU", False),
    "GBP": ("DEXUSUK", False),
    "AUD": ("DEXUSAL", False),
    "NZD": ("DEXUSNZ", False),
    "JPY": ("DEXJPUS", True),
    "CAD": ("DEXCAUS", True),
    "CHF": ("DEXSZUS", True),
    "NOK": ("DEXNOUS", True),
    "SEK": ("DEXSDUS", True),
    "DKK": ("DEXDNUS", True),
    "KRW": ("DEXKOUS", True),
    "MXN": ("DEXMXUS", True),
    "ZAR": ("DEXSFUS", True),
}
DM = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "NOK", "SEK", "DKK"]
EMX = ["KRW", "MXN", "ZAR"]
COST_BPS = {c: 2.0 for c in DM}
COST_BPS.update({"KRW": 6.0, "MXN": 8.0, "ZAR": 10.0})
RNG = np.random.default_rng(20260712)


def fred(series_id: str) -> pd.Series | None:
    cpath = f"{CACHE}/{series_id}.csv"
    if not os.path.exists(cpath):
        try:
            urllib.request.urlretrieve(FRED + series_id, cpath)  # noqa: S310
        except Exception as e:
            print(f"  [warn] fetch failed {series_id}: {e}", file=sys.stderr)
            return None
    try:
        df = pd.read_csv(cpath, na_values=".").dropna()
    except Exception:
        return None
    if df.empty or len(df.columns) != 2:
        return None
    df.columns = ["date", "val"]
    return pd.Series(df["val"].values, index=pd.to_datetime(df["date"]), name=series_id).astype(float)


def build_panel():
    spot_d, spot_m, rates = {}, {}, {}
    for ccy, (sid, inv) in SPOT_ID.items():
        s, r = fred(sid), fred(RATE_ID[ccy])
        if s is None or r is None:
            print(f"  [warn] dropping {ccy}", file=sys.stderr)
            continue
        sd = (1.0 / s) if inv else s
        spot_d[ccy] = sd
        spot_m[ccy] = sd.resample("ME").last()
        rates[ccy] = r.resample("ME").last()
    r_usd = fred(RATE_ID["USD"])
    if r_usd is None:
        raise RuntimeError("USD rate series is required")
    rates["USD"] = r_usd.resample("ME").last()
    spot_m["USD"] = pd.Series(1.0, index=spot_m["EUR"].index)
    S = pd.DataFrame(spot_m).sort_index()
    R = pd.DataFrame(rates).reindex(S.index).ffill()
    SD = pd.DataFrame(spot_d).sort_index()  # daily spots for vol estimation
    return S, R, SD


def excess_returns(S, R):
    spot_ret = S.pct_change(fill_method=None)
    rdiff = R.sub(R["USD"], axis=0) / 1200.0
    return spot_ret + rdiff.shift(1)


def monthly_vol(SD: pd.DataFrame, idx) -> pd.DataFrame:
    """Trailing 63d daily-return vol per ccy, sampled at month-end, lag-safe (uses data to t)."""
    dret = SD.pct_change(fill_method=None)
    v = dret.rolling(63, min_periods=40).std() * np.sqrt(252)
    v = v.resample("ME").last().reindex(idx)
    v["USD"] = np.nan
    return v


def hml_iv(sig, xs, vol, ccys, k_frac=0.34, cost=True, rand_ranks=False, gate=None):
    """HML book with inverse-vol-weighted legs. gate: Series in [0,1] scaling exposure."""
    sig = sig[ccys].shift(1)
    x = xs[ccys]
    v = vol[ccys].shift(1)  # vol known at prior month-end
    k = max(1, round(len(ccys) * k_frac))
    rows, prev = {}, {}
    for dt in x.index:
        s_row, x_row = sig.loc[dt].dropna(), x.loc[dt].dropna()
        avail = [c for c in s_row.index if c in x_row.index]
        if len(avail) < 6:
            continue
        order = list(RNG.permutation(avail)) if rand_ranks else list(s_row[avail].sort_values().index)
        longs, shorts = order[-k:], order[:k]
        w = {}
        for side, ccy_list in ((1, longs), (-1, shorts)):
            iv = {}
            for c in ccy_list:
                vc = v.loc[dt].get(c, np.nan)
                iv[c] = 1.0 / vc if (vc and np.isfinite(vc) and vc > 0) else 1.0 / 0.10
            tot = sum(iv.values())
            for c, val in iv.items():
                w[c] = side * val / tot
        g = 1.0 if gate is None else float(gate.get(dt, 1.0))
        ret = sum(w[c] * x_row[c] for c in w) * g
        if cost:
            turn = sum(abs(w.get(c, 0.0) * g - prev.get(c, 0.0)) for c in set(w) | set(prev))
            cbar = np.mean([COST_BPS[c] for c in w]) * 1e-4 if w else 0.0
            ret -= 0.5 * turn * cbar
        rows[dt] = ret
        prev = {c: w[c] * g for c in w}
    return pd.Series(rows).sort_index()


def carry_gate(vol: pd.DataFrame, dm: list[str]) -> pd.Series:
    """Global FX vol regime: cross-sectional mean 63d vol -> expanding pctile (no look-ahead).
    Exposure 1.0 below 70th pctile, linear to 0.0 at 95th. No fitted params — thresholds
    are the Menkhoff-style 'top quintile is toxic' shape."""
    g = vol[[c for c in dm if c != "USD"]].mean(axis=1)
    pct = g.expanding(min_periods=36).apply(lambda a: (a[:-1] <= a[-1]).mean() if len(a) > 1 else 0.5)
    gate = ((0.95 - pct) / 0.25).clip(0.0, 1.0)
    return gate.shift(1)  # regime known at prior month-end


def stats(r, label):
    r = r.dropna()
    if len(r) < 24:
        return f"{label:32s} insufficient ({len(r)} mo)"
    ar, av = r.mean() * 12 * 100, r.std() * np.sqrt(12) * 100
    sh = ar / av if av else float("nan")
    eq = (1 + r).cumprod()
    dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    return (
        f"{label:32s} Sharpe {sh:+.2f}  ret {ar:+5.1f}%  vol {av:4.1f}%  skew {r.skew():+.2f}  "
        f"maxDD {dd:5.0f}%  worstMo {r.min() * 100:+.1f}%  ({len(r)}mo {r.index[0].year}-{r.index[-1].year})"
    )


def sharpe_of(r):
    r = r.dropna()
    return (r.mean() * 12) / (r.std() * np.sqrt(12)) if len(r) and r.std() else float("nan")


def risk_parity_combo(streams: pd.DataFrame) -> pd.Series:
    """Inverse trailing-36m-vol weights, lagged one month."""
    w = (1.0 / streams.rolling(36, min_periods=24).std()).shift(1)
    w = w.div(w.sum(axis=1), axis=0)
    return (streams * w).sum(axis=1, min_count=2)


def main():
    print("=" * 106)
    print("FX MULTI-FACTOR v2 — inverse-vol legs, carry crash-gate, risk-parity combo, 14-ccy universe")
    print("=" * 106)
    S, R, SD = build_panel()
    ccys = [c for c in DM + EMX if c in S.columns]
    print(f"panel: {S.index[0].date()}..{S.index[-1].date()}  universe({len(ccys)}): {ccys}\n")

    xs = excess_returns(S, R)
    vol = monthly_vol(SD, S.index)
    gate = carry_gate(vol, [c for c in DM if c in S.columns])

    sigs = {
        "carry": R.sub(R["USD"], axis=0),
        "mom3": xs.rolling(3).sum(),
        "value": -(S / S.shift(60) - 1.0),
    }

    books = {
        "carry": hml_iv(sigs["carry"], xs, vol, ccys),
        "carry_gated": hml_iv(sigs["carry"], xs, vol, ccys, gate=gate),
        "mom3": hml_iv(sigs["mom3"], xs, vol, ccys),
        "value": hml_iv(sigs["value"], xs, vol, ccys),
    }
    for name, r in books.items():
        print(stats(r, f"{name} (iv-legs)"))
    print()

    streams = pd.DataFrame({k: books[k] for k in ("carry_gated", "mom3", "value")}).dropna(how="all")
    combos = {
        "COMBO eq-wt (gated)": streams.mean(axis=1),
        "COMBO risk-parity (gated)": risk_parity_combo(streams),
        "COMBO eq-wt (ungated)": pd.DataFrame({k: books[k] for k in ("carry", "mom3", "value")}).mean(axis=1),
    }
    for name, r in combos.items():
        print(stats(r, name))
        print(stats(r.loc["2015":], f"  {name} 2015+"))
        print(stats(r.loc["2022":], f"  {name} 2022+"))
    print()

    best_name = max(combos, key=lambda n: sharpe_of(combos[n]))
    best = combos[best_name]
    obs = sharpe_of(best)

    n_sims = 300
    perm = []
    for _ in range(n_sims):
        rc = hml_iv(sigs["carry"], xs, vol, ccys, rand_ranks=True, gate=gate)
        rm = hml_iv(sigs["mom3"], xs, vol, ccys, rand_ranks=True)
        rv = hml_iv(sigs["value"], xs, vol, ccys, rand_ranks=True)
        perm.append(sharpe_of(pd.DataFrame({"a": rc, "b": rm, "c": rv}).mean(axis=1)))
    perm = np.array(perm)
    print(f"permutation control ({n_sims} sims): best = {best_name}")
    print(
        f"  observed Sharpe {obs:+.2f} | perm mean {perm.mean():+.2f} sd {perm.std():.2f} | "
        f"p {float((perm >= obs).mean()):.3f}\n"
    )

    r = best.dropna()
    ar, av = r.mean() * 12, r.std() * np.sqrt(12)
    print("#" * 106)
    if ar > 0:
        lev = 0.30 / ar
        eq = (1 + r * lev).cumprod()
        dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
        cagr = (eq.iloc[-1] ** (12 / len(r)) - 1) * 100
        print(
            f"30% TARGET: {best_name} needs {lev:.1f}x -> vol {lev * av * 100:.0f}%, CAGR {cagr:+.0f}%, maxDD {dd:.0f}%"
        )
        print(f"  (unlevered: Sharpe {obs:+.2f}, ret {ar * 100:+.1f}%, vol {av * 100:.1f}%)")
        r15 = best.loc["2015":].dropna()
        print(f"  2015+ Sharpe {sharpe_of(r15):+.2f}  |  decision bar: >=0.8 full AND >=0.5 2015+")
    else:
        print("30% TARGET: unreachable — combo return non-positive")
    print("#" * 106)


if __name__ == "__main__":
    main()
