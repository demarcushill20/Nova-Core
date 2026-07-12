"""Multi-sleeve v3 (task 1125) — push blended Sharpe 1.24 -> >=1.5 via orthogonal sleeves.

Iter-5 book = 11:00 EUR short (perp) + crash-gated G10 carry + gold TSMOM = Sharpe +1.24.
The ONLY valid lever to improve 30%-CAGR/DD is higher *blended* Sharpe, which requires new
sleeves with corr <~0.1 to all three existing. This script tests three candidates against
the frozen 3-sleeve book:

  A. BTC TSMOM   — btcusd_1h.parquet -> daily. 12m+3m sign, vol-target 10%, cap 2x, 5bp cost.
  B. Silver TSMOM — SKIPPED: HistData has no XAG feed (verified: no XAG*/silver* under data/).
  C. FX TSMOM    — 11-pair HistData M1 daily basket. Per-pair sign(3m)/sign(12m), vol-target
                   10%, cap 2x; inverse-vol equal-risk basket. Academic FX TSMOM is weak
                   post-2010; included only to confirm/deny the corr+Sharpe bar.

Gold sleeve is refreshed to 2026-06 (fresh HistData 2025 + 2026H1 M1) by splicing the fresh
2025+2026 compute onto the preserved pre-2025 daily-PnL cache; a standalone 2025..2026-06 OOS
read on gold is printed. NOTE: the base sleeves (eur_short, carry) still end 2024-12, so the
*blend* common window and sealed read remain capped at 2024-12 — flagged in the verdict.

Frozen specs only. No parameter fitting. Net of cost. Research only.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK = os.path.join(ROOT, "WORK")
PANEL_CSV = os.path.join(WORK, "multisleeve_monthly_panel.csv")
GOLD_OLD = os.path.join(WORK, "sleeve4_gold_tsmom_daily.csv")  # pre-2025 gold sleeve PnL (2009-2024)
GOLD_NEW_PX = os.path.join(WORK, "gold_daily_close_2020_2026.csv")  # fresh gold daily close 2020-2026H1
BTC_PARQUET = os.path.join(ROOT, "data/candles/btcusd_1h.parquet")
FX_PAIRS = [
    "AUDNZD",
    "AUDUSD",
    "EURUSD",
    "GBPUSD",
    "NZDUSD",
    "USDCAD",
    "USDJPY",
    "USDMXN",
    "USDSGD",
    "USDTRY",
    "USDZAR",
]


# --------------------------------------------------------------- generic TSMOM on a daily px
def tsmom_daily(
    px: pd.Series,
    long_win: int,
    short_win: int,
    ann: float,
    vol_win: int,
    cost_per_turn: float,
    target: float = 0.10,
    cap: float = 2.0,
) -> pd.Series:
    """Parameter-light TSMOM: pos = clip(0.5 sign(Lm) + 0.5 sign(Sm)) * target/vol, capped."""
    px = px.sort_index()
    ret = px.pct_change(fill_method=None)
    vol = ret.rolling(vol_win, min_periods=max(20, vol_win // 2)).std() * np.sqrt(ann)
    sig = 0.5 * np.sign(px.pct_change(long_win, fill_method=None)) + 0.5 * np.sign(
        px.pct_change(short_win, fill_method=None)
    )
    pos = (sig * (target / vol)).clip(-cap, cap)
    pnl = pos.shift(1) * ret - pos.diff().abs() * cost_per_turn
    return pnl.dropna()


# ----------------------------------------------------------------- sleeve: gold (extended)
def gold_sleeve_extended() -> pd.Series:
    """Preserve pre-2025 cached gold PnL; splice fresh 2025..2026-06 compute (warmed by 2020-24)."""
    old = pd.read_csv(GOLD_OLD, index_col=0, parse_dates=True).iloc[:, 0]
    px = pd.read_csv(GOLD_NEW_PX, index_col=0, parse_dates=True)["close"]
    fresh = tsmom_daily(px, 252, 63, 252.0, 63, 2e-4)  # gold spec: 2bp/turn, business-day windows
    new_part = fresh.loc["2025-01-01":]  # type: ignore[misc]
    old_part = old.loc[:"2024-12-31"]  # type: ignore[misc]
    return pd.concat([old_part, new_part]).sort_index()


# ------------------------------------------------------------------ sleeve: BTC TSMOM
def btc_sleeve() -> pd.Series:
    df = pd.read_parquet(BTC_PARQUET)
    daily = df["close"].resample("D").last().dropna()  # 24/7 -> calendar daily
    # 12m/3m on a 7-day asset = 365/90 calendar days, sqrt(365) annualization; 5bp/turn.
    return tsmom_daily(daily, 365, 90, 365.0, 90, 5e-4)


# ------------------------------------------------------------------ sleeve: FX TSMOM basket
def fx_tsmom_sleeve() -> pd.Series:
    legs = {}
    for p in FX_PAIRS:
        f = os.path.join(WORK, f"fxtsmom_daily_{p}.csv")
        if not os.path.exists(f):
            continue
        px = pd.read_csv(f, index_col=0, parse_dates=True)["close"]
        cost = 8e-4 if p in ("USDMXN", "USDZAR", "USDTRY") else 2e-4  # EM turnover costs more
        legs[p] = tsmom_daily(px, 252, 63, 252.0, 63, cost)
    L = pd.DataFrame(legs)
    # equal-risk basket: each leg already vol-targeted to ~10%, so equal-weight the available legs
    return L.mean(axis=1).dropna()


# ---------------------------------------------------------------------------- helpers
def to_monthly(daily: pd.Series) -> pd.Series:
    daily = daily.dropna()
    return (1 + daily).groupby(daily.index.to_period("M")).prod() - 1


def ann_stats(m: pd.Series):
    m = m.dropna()
    ar, av = m.mean() * 12, m.std() * np.sqrt(12)
    eq = (1 + m).cumprod()
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    return (ar / av if av else np.nan), ar, av, dd


def line(m: pd.Series, label: str) -> str:
    m = m.dropna()
    if len(m) < 6:
        return f"{label:40s} insufficient ({len(m)}mo)"
    sh, ar, av, dd = ann_stats(m)
    return (
        f"{label:40s} Sharpe {sh:+.2f}  ret {ar * 100:+5.1f}%  vol {av * 100:4.1f}%  "
        f"maxDD {dd * 100:6.1f}%  ({len(m)}mo {m.index[0]}..{m.index[-1]})"
    )


def main() -> None:
    out: dict = {}
    print("=" * 112)
    print("MULTI-SLEEVE v3 (task 1125) — BTC/FX TSMOM candidates vs the Sharpe-1.24 3-sleeve book")
    print("=" * 112)

    base = pd.read_csv(PANEL_CSV, index_col=0, parse_dates=True)[["eur_short", "carry"]]
    base.index = base.index.to_period("M")

    gold_d = gold_sleeve_extended()
    gold_m = to_monthly(gold_d)
    btc_m = to_monthly(btc_sleeve())
    fx_m = to_monthly(fx_tsmom_sleeve())

    # --- standalone gold OOS on the fresh 2025..2026-06 data ---
    gold_oos = gold_m.loc[pd.Period("2025-01") :]  # type: ignore[misc]
    print("\n[fresh-data check] gold TSMOM standalone, sealed 2025-01..2026-06:")
    print("  " + line(gold_oos, "gold_tsmom 2025-2026H1"))

    panel = base.copy()
    panel["gold_tsmom"] = gold_m
    panel["btc_tsmom"] = btc_m
    panel["fx_tsmom"] = fx_m

    print("\nper-sleeve (full available history):")
    per = {}
    for c in panel.columns:
        print("  " + line(panel[c], c))
        sh, ar, av, dd = ann_stats(panel[c])
        s = panel[c].dropna()
        per[c] = {
            "sharpe": sh,
            "ret": ar,
            "vol": av,
            "maxDD": dd,
            "n": len(s),
            "start": str(s.index[0]),
            "end": str(s.index[-1]),
        }
    out["per_sleeve"] = per

    # blend on the common (base-capped) window
    common = panel.dropna()
    print(f"\ncommon window (all 5 sleeves): {common.index[0]}..{common.index[-1]}  ({len(common)}mo)")
    print("\ncorrelation matrix (monthly, common window):")
    corr = common.corr()
    print("  " + corr.round(3).to_string().replace("\n", "\n  "))
    out["common_window"] = f"{common.index[0]}..{common.index[-1]}"
    out["n_common"] = len(common)
    out["corr"] = corr.round(4).to_dict()

    # orthogonality of candidates to the existing three
    existing = ["eur_short", "carry", "gold_tsmom"]
    print("\ncandidate orthogonality (max |corr| to existing 3 sleeves; bar <=0.10):")
    for cand in ("btc_tsmom", "fx_tsmom"):
        mc = max(abs(corr.loc[cand, e]) for e in existing)
        verdict = "PASS" if mc <= 0.10 else "FAIL"
        print(f"  {cand:12s} max|corr| {mc:.3f}  -> {verdict}")
        per[cand]["max_abs_corr_to_existing"] = round(float(mc), 4)
        per[cand]["orth_pass"] = bool(mc <= 0.10)

    def blend(cols, label, fit_end="2023-12"):
        sub = panel[cols].dropna()
        w = 1.0 / sub.loc[:fit_end].std()
        w = w / w.sum()
        port = (sub * w).sum(axis=1)
        print("  " + line(port, f"{label} w={dict(zip(cols, w.round(2), strict=True))}"))
        sealed = port.loc[pd.Period("2024-01") :]  # type: ignore[misc]
        if len(sealed) >= 6:
            print("  " + line(sealed, f"    {label} SEALED 2024+"))
        return port, w

    print("\nblends (inverse-vol weights frozen on pre-2024 vols):")
    blends = {}
    weights = {}
    for cols, lbl in [
        (["eur_short", "carry", "gold_tsmom"], "3s book (iter-5 baseline)"),
        (["eur_short", "carry", "gold_tsmom", "btc_tsmom"], "4s +BTC"),
        (["eur_short", "carry", "gold_tsmom", "fx_tsmom"], "4s +FX"),
        (["eur_short", "carry", "gold_tsmom", "btc_tsmom", "fx_tsmom"], "5s +BTC +FX"),
    ]:
        p, w = blend(cols, lbl)
        blends[lbl] = p
        weights[lbl] = {c: round(float(x), 4) for c, x in zip(cols, w, strict=True)}

    out["blends"] = {}
    for lbl, p in blends.items():
        sh, ar, av, dd = ann_stats(p)
        sealed = p.loc[pd.Period("2024-01") :]  # type: ignore[misc]
        ssh = ann_stats(sealed)[0] if len(sealed) >= 6 else None
        out["blends"][lbl] = {
            "sharpe": sh,
            "ret": ar,
            "vol": av,
            "maxDD": dd,
            "sealed_2024_sharpe": ssh,
            "weights": weights[lbl],
        }

    # frontier on the highest-Sharpe blend
    best_lbl = max(blends, key=lambda k: ann_stats(blends[k])[0])
    best = blends[best_lbl]
    sh, ar, av, _ = ann_stats(best)
    print(f"\nbest blend: '{best_lbl}'  Sharpe {sh:+.2f}")
    print(f"{'L':>6} {'annVol':>8} {'CAGR':>8} {'maxDD(mo)':>10} {'worstMo':>8}")
    frontier = []
    for L in (1, 2, 4, 6, 8, 10):
        lp = best * L
        eq = (1 + lp).cumprod()
        cagr = eq.iloc[-1] ** (12 / len(lp)) - 1
        dd = ((eq - eq.cummax()) / eq.cummax()).min()
        print(f"{L:>6} {av * L * 100:>7.1f}% {cagr * 100:>+7.1f}% {dd * 100:>9.1f}% {lp.min() * 100:>+7.1f}%")
        frontier.append({"L": L, "vol": av * L, "cagr": cagr, "maxDD_mo": dd, "worstMo": lp.min()})
    solve = None
    if ar > 0:
        Ls = 0.30 / ar
        lp = best * Ls
        eq = (1 + lp).cumprod()
        cagr = eq.iloc[-1] ** (12 / len(lp)) - 1
        dd = ((eq - eq.cummax()) / eq.cummax()).min()
        print(
            f"{Ls:>6.1f} {av * Ls * 100:>7.1f}% {cagr * 100:>+7.1f}% "
            f"{dd * 100:>9.1f}% {lp.min() * 100:>+7.1f}%  <- 30% solve"
        )
        solve = {"L": Ls, "vol": av * Ls, "cagr": cagr, "maxDD_mo": dd, "worstMo": lp.min()}
    out["best_blend"] = best_lbl
    out["best_sharpe"] = sh
    out["frontier"] = frontier
    out["solve_30pct"] = solve

    print("\n" + "#" * 112)
    win = sh >= 1.5
    print(
        f"VERDICT: best blended Sharpe {sh:+.2f} (iter-5 book was +1.24).  Target >= 1.50 -> "
        f"{'REACHED' if win else 'NOT reached'}."
    )
    print("  Blend common window capped at 2024-12 by eur_short/carry (not yet refreshed to 2026);")
    print("  gold standalone 2025-2026H1 shown above is the only fresh-data OOS this cycle.")
    print("#" * 112)
    out["target_1p5_reached"] = bool(win)

    with open(os.path.join(WORK, "multisleeve_v3_results.json"), "w") as fh:
        json.dump(out, fh, indent=2, default=float)
    print("\n[wrote WORK/multisleeve_v3_results.json]")


if __name__ == "__main__":
    main()
