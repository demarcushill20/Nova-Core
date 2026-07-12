"""Multi-sleeve v2 — add orthogonal Sharpe: gold TSMOM + gated EM carry (Iteration 5).

Iter-4 verdict: 2-sleeve book (11:00 EUR short ⊥ gated G10 carry) = Sharpe +1.07, but 30%
CAGR at that Sharpe costs −44% maxDD. The ONLY lever that improves the CAGR/DD trade is
higher blended Sharpe (for fixed Sharpe, CAGR and DD scale together regardless of whether
vol is native or levered). Target: blended Sharpe >= ~1.5 -> 30% at ~20-25% maxDD.

New sleeves (both from in-house data, parameter-light, documented premia):
  4. GOLD TSMOM  — XAUUSD daily (from HistData M1, 2009+). 50/50 sign(12m ret) + sign(3m ret),
     vol-targeted to 10% (63d realized, cap 2x), cost 2bp per unit turnover.
  5. EM CARRY (directional, gated) — long 0.5*MXN + 0.5*ZAR vs USD monthly excess return
     (FRED spot + 3M rates, lagged), scaled by the v2 crash-gate, cost 9bp on turnover.
     NOTE: shares the carry premium with sleeve 2 (G10 HML) — corr matrix decides if both stay.

Blend: inverse-vol monthly weights across sleeves, common window, sealed 2024+ read with
weights frozen on pre-2024. Frontier + leverage-for-30% solve. Honest bar: report blended
Sharpe and the DD that 30% costs; promote only if Sharpe materially > 1.07.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
WORK = os.path.join(ROOT, "WORK")

from probe_fx_multifactor_v2 import (  # noqa: E402
    build_panel,
    carry_gate,
    monthly_vol,
)

GOLD_M1 = os.path.join(ROOT, "data/candles/histdata/XAUUSD_M1.csv")
PANEL_CSV = os.path.join(WORK, "multisleeve_monthly_panel.csv")


# ---------------------------------------------------------------- sleeve 4: gold TSMOM
def sleeve4_gold_tsmom() -> pd.Series:
    cache = os.path.join(WORK, "sleeve4_gold_tsmom_daily.csv")
    if os.path.exists(cache):
        s = pd.read_csv(cache, index_col=0, parse_dates=True).iloc[:, 0]
        return s
    px = pd.read_csv(GOLD_M1, usecols=["time", "close"], parse_dates=["time"])
    daily = px.set_index("time")["close"].resample("D").last().dropna()
    ret = daily.pct_change(fill_method=None)
    vol63 = ret.rolling(63, min_periods=40).std() * np.sqrt(252)
    sig = 0.5 * np.sign(daily.pct_change(252, fill_method=None)) + 0.5 * np.sign(daily.pct_change(63, fill_method=None))
    pos = (sig * (0.10 / vol63)).clip(-2.0, 2.0)
    pnl = pos.shift(1) * ret - (pos.diff().abs() * 2e-4)  # 2bp per unit turnover
    pnl = pnl.dropna()
    pnl.to_frame("ret").to_csv(cache)
    return pnl


# ------------------------------------------------------- sleeve 5: gated EM carry basket
def sleeve5_em_carry() -> pd.Series:
    S, R, SD = build_panel()
    vol = monthly_vol(SD, S.index)
    gate = carry_gate(vol, ["EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "NOK", "SEK"])
    em = [c for c in ("MXN", "ZAR") if c in S.columns]
    spot_ret = S[em].pct_change(fill_method=None)
    rdiff = R[em].sub(R["USD"], axis=0) / 1200.0
    xs = (spot_ret + rdiff.shift(1)).mean(axis=1)  # equal-weight basket vs USD
    g = gate.reindex(xs.index).fillna(0.0)
    ret = xs * g
    ret -= (g.diff().abs().fillna(0.0)) * 9e-4  # 9bp cost on gate-driven turnover
    return ret.dropna()


# ---------------------------------------------------------------------------- helpers
def to_monthly(daily: pd.Series) -> pd.Series:
    return (1 + daily).groupby(daily.index.to_period("M")).prod() - 1


def ann(m: pd.Series) -> tuple[float, float, float, float]:
    m = m.dropna()
    ar, av = m.mean() * 12, m.std() * np.sqrt(12)
    eq = (1 + m).cumprod()
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    return ar / av if av else np.nan, ar, av, dd


def line(m: pd.Series, label: str) -> str:
    sh, ar, av, dd = ann(m)
    m = m.dropna()
    return (
        f"{label:34s} Sharpe {sh:+.2f}  ret {ar * 100:+5.1f}%  vol {av * 100:4.1f}%  "
        f"maxDD {dd * 100:5.1f}%  ({len(m)}mo {m.index[0]}..{m.index[-1]})"
    )


def main() -> None:
    print("=" * 108)
    print("MULTI-SLEEVE v2 — adding gold TSMOM + gated EM carry to the Sharpe-1.07 book (target: Sharpe >= 1.5)")
    print("=" * 108)

    base = pd.read_csv(PANEL_CSV, index_col=0, parse_dates=True)[["eur_short", "carry"]]
    base.index = base.index.to_period("M")

    g = to_monthly(sleeve4_gold_tsmom())
    e = sleeve5_em_carry()
    e.index = e.index.to_period("M")

    panel = base.copy()
    panel["gold_tsmom"] = g
    panel["em_carry"] = e

    for c in panel.columns:
        print(line(panel[c], c))
    print()

    common = panel.dropna()
    print(f"common window: {common.index[0]}..{common.index[-1]}  ({len(common)}mo)")
    print("\ncorrelations (monthly, common window):")
    print(common.corr().round(2).to_string(), "\n")

    def blend(cols: list[str], label: str, fit_end: str = "2023-12") -> pd.Series:
        sub = panel[cols].dropna()
        w = 1.0 / sub.loc[:fit_end].std()  # type: ignore[misc]  # weights frozen pre-2024
        w = w / w.sum()
        port = (sub * w).sum(axis=1)
        print(line(port, label + f"  w={dict(zip(cols, w.round(2), strict=True))}"))
        sealed = port.loc["2024-01":]  # type: ignore[misc]
        if len(sealed) >= 6:
            print(line(sealed, f"  {label} SEALED 2024+"))
        return port

    print("blends (inverse-vol weights frozen on pre-2024):")
    p2 = blend(["eur_short", "carry"], "2-sleeve (iter-4 baseline)")
    p3 = blend(["eur_short", "carry", "gold_tsmom"], "3-sleeve (+gold)")
    p4 = blend(["eur_short", "carry", "gold_tsmom", "em_carry"], "4-sleeve (+gold +EM carry)")
    print()

    # frontier on the best blend
    blends = {"2s": p2, "3s": p3, "4s": p4}
    best_label = max(blends, key=lambda k: ann(blends[k])[0])
    best = blends[best_label]
    sh, ar, av, _ = ann(best)
    print(f"best blend: {best_label}  Sharpe {sh:+.2f}")
    print(f"{'L':>6} {'annVol':>8} {'CAGR':>8} {'maxDD':>8} {'worstMo':>8}")
    for L in (1, 2, 4, 6, 8, 10):
        lp = best * L
        eq = (1 + lp).cumprod()
        cagr = eq.iloc[-1] ** (12 / len(lp)) - 1
        dd = ((eq - eq.cummax()) / eq.cummax()).min()
        print(f"{L:>6} {av * L * 100:>7.1f}% {cagr * 100:>+7.1f}% {dd * 100:>7.1f}% {lp.min() * 100:>+7.1f}%")
    if ar > 0:
        Ls = 0.30 / ar
        lp = best * Ls
        eq = (1 + lp).cumprod()
        cagr = eq.iloc[-1] ** (12 / len(lp)) - 1
        dd = ((eq - eq.cummax()) / eq.cummax()).min()
        row = f"{Ls:>6.1f} {av * Ls * 100:>7.1f}% {cagr * 100:>+7.1f}% {dd * 100:>7.1f}% {lp.min() * 100:>+7.1f}%"
        print(row + "  <- 30% solve")
    print()
    print("#" * 108)
    print(f"VERDICT: blended Sharpe {sh:+.2f} (was +1.07 with 2 sleeves). 30% CAGR at Sharpe S costs maxDD as")
    print("  shown above; bar for 'winner' = 30% at maxDD <= ~25%, i.e. Sharpe >= ~1.5.")
    print("#" * 108)


if __name__ == "__main__":
    main()
