"""Carry crash-management backtest — the make-or-break test for tradeable carry.

Raw carry is Sharpe ~0.5 but with -0.9 skew and -33% crash drawdowns (1998/2008/2020).
The whole question: can standard overlays tame the TAIL without killing the Sharpe?

Overlays (all use only info available at the rebalance — vol/DD/mom are lagged):
  1. vol-target : scale by target_vol / trailing-6m-realized-vol, leverage capped 1.5x
                  (Barroso-Santa-Clara: de-risk into rising vol, which precedes crashes)
  2. +DD-brake  : on top of (1), halve exposure while in a >10% drawdown
  3. +carry-mom : on top of (2), cut to half when trailing-12m carry return < 0
                  (crashes cluster; carry momentum sidesteps the worst clusters)

Sharpe is scale-invariant, so any Sharpe change is from TIMING (de-risking before bad
months), not leverage. maxDD/skew/worst-month show whether the tail actually shrank.
Then: does crash-managed carry blend better with the EUR own-hours edge?

Honest checks: per-crash P&L (2008/2015-CHF/2020), and the leverage profile (a defense
that just quietly de-levers to ~0 isn't a strategy).
"""

from __future__ import annotations

import importlib.util

import numpy as np
import pandas as pd

spec = importlib.util.spec_from_file_location("c", "scripts/probe_carry_portfolio.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)

TARGET_VOL = 0.08  # 8% annualized target
LEV_CAP = 1.5
VOL_WIN = 6  # months trailing realized vol


def overlays(raw: pd.Series):
    r = raw.dropna().copy()
    rv = r.rolling(VOL_WIN).std() * np.sqrt(12)  # trailing realized vol (ann)
    lev = (TARGET_VOL / rv.shift(1)).clip(upper=LEV_CAP).fillna(1.0)  # lagged, capped
    vt = lev * r

    # DD brake: halve while trailing equity is >10% below its running peak (lagged)
    eq_vt = (1 + vt).cumprod()
    dd = (eq_vt / eq_vt.cummax() - 1).shift(1).fillna(0.0)
    brake = np.where(dd < -0.10, 0.5, 1.0)
    vt_dd = pd.Series(brake, index=vt.index) * vt

    # carry-momentum: half size when trailing 12m raw carry return < 0 (lagged)
    mom = r.rolling(12).sum().shift(1)
    mfac = np.where(mom < 0, 0.5, 1.0)
    vt_dd_mom = pd.Series(brake, index=vt.index) * pd.Series(mfac, index=vt.index) * vt

    return {"raw": r, "vol-target": vt, "+DD-brake": vt_dd, "+carry-mom": vt_dd_mom}, lev


def stats(r: pd.Series) -> dict:
    r = r.dropna()
    ann = r.mean() * 12 * 100
    vol = r.std() * np.sqrt(12) * 100
    sh = ann / vol if vol else np.nan
    eq = (1 + r).cumprod()
    dd = (eq / eq.cummax() - 1).min() * 100
    calmar = ann / abs(dd) if dd else np.nan
    w12 = r.rolling(12).sum().min() * 100
    return dict(
        sharpe=sh,
        ann=ann,
        vol=vol,
        skew=r.skew(),
        maxdd=dd,
        calmar=calmar,
        worst1=r.min() * 100,
        worst12=w12,
        pos=(r > 0).mean() * 100,
    )


CRASHES = {
    "GFC 2008": ("2008-06", "2009-03"),
    "CHF 2015": ("2014-11", "2015-03"),
    "COVID 2020": ("2020-01", "2020-06"),
    "2022 turmoil": ("2022-01", "2022-12"),
}


def main():
    print(
        f"{'=' * 104}\nCARRY CRASH-MANAGEMENT BACKTEST  (G10+EM, monthly, target vol {TARGET_VOL:.0%}, lev cap {LEV_CAP})\n{'=' * 104}"
    )
    S, R = c.build_panel()
    raw = c.carry_portfolio(c.G10 + c.EM, S, R)
    variants, lev = overlays(raw)

    print(
        f"\n{'variant':12s} {'Sharpe':>7s} {'ann%':>6s} {'vol%':>5s} {'skew':>6s} {'maxDD%':>7s} "
        f"{'Calmar':>7s} {'worst1m':>8s} {'worst12m':>9s} {'%+':>5s}"
    )
    print("-" * 92)
    for name, s in variants.items():
        st = stats(s)
        print(
            f"{name:12s} {st['sharpe']:+7.2f} {st['ann']:+6.1f} {st['vol']:5.1f} {st['skew']:+6.2f} "
            f"{st['maxdd']:+7.0f} {st['calmar']:+7.2f} {st['worst1']:+8.1f} {st['worst12']:+9.1f} {st['pos']:5.0f}"
        )

    print(f"\nPer-crash cumulative P&L (%):  {'  '.join(f'{k:>12s}' for k in CRASHES)}")
    for name, s in variants.items():
        cells = []
        for k, (a, b) in CRASHES.items():
            seg = s.loc[a:b]
            cells.append(f"{((1 + seg).prod() - 1) * 100:+12.1f}" if len(seg) else f"{'n/a':>12s}")
        print(f"  {name:12s} {'  '.join(cells)}")

    print(
        f"\nleverage profile: mean {lev.mean():.2f}x  min {lev.min():.2f}x  max {lev.max():.2f}x  "
        f"(% months at cap {(lev >= LEV_CAP - 1e-9).mean() * 100:.0f}%, % de-risked <0.75x {(lev < 0.75).mean() * 100:.0f}%)"
    )

    # blend with EUR own-hours edge — does managed carry diversify better?
    h = (
        pd.read_csv("data/candles/histdata/EURUSD_M1.csv", usecols=["time", "open"], parse_dates=["time"])
        .set_index("time")
        .sort_index()
    )
    o = h["open"].resample("1h").first().dropna()
    ent = o[(o.index.hour == 11) & (o.index.weekday < 5)]
    drows = {
        ts.normalize(): (px - o[ts + pd.Timedelta(hours=1)]) / px - 0.30e-4
        for ts, px in ent.items()
        if (ts + pd.Timedelta(hours=1)) in o.index
    }
    eur = pd.Series(drows).resample("ME").sum()

    print(f"\n{'blend (50/50 risk-parity) with EUR own-hours edge':52s} {'corr':>6s} {'carrySh':>8s} {'blendSh':>8s}")
    for name in ["raw", "vol-target", "+DD-brake", "+carry-mom"]:
        df = pd.concat([variants[name].rename("c"), eur.rename("e")], axis=1).dropna()
        rho = df["c"].corr(df["e"])
        shc = df["c"].mean() / df["c"].std() * np.sqrt(12)
        she = df["e"].mean() / df["e"].std() * np.sqrt(12)
        comb = 0.5 * df["c"] / df["c"].std() + 0.5 * df["e"] / df["e"].std()
        shb = comb.mean() / comb.std() * np.sqrt(12)
        print(f"  {name:50s} {rho:+6.2f} {shc:+8.2f} {shb:+8.2f}   (EUR standalone {she:+.2f})")

    print("#" * 104)


if __name__ == "__main__":
    main()
