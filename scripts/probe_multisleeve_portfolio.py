#!/usr/bin/env python3
"""Task 1124 — Multi-sleeve FX portfolio + leverage/DD frontier (iteration 4 of the 30% loop).

Blends THREE already-validated in-house sleeves into one monthly-return portfolio, measures
realized correlations + equal-risk (inverse-vol) blended Sharpe, and produces a vol-targeted
leverage frontier (1x..6x) so the operator can pick a risk point for the 30%-CAGR target.

NO new signals are invented here (per task). The three sleeves:

  1. 11:00 EUR short  (live edge, t=-5.2). Daily PnL = short EURUSD 11:00->12:00 UTC from real
     Dukascopy ticks (data/ticks/EURUSD/*.parquet). Entry sells the 11:00 bid, exit buys the
     12:00 ask => the round-trip spread is baked in (honest, net of measured spread).
  2. Crash-gated carry (Sharpe ~0.50). Reuses probe_fx_multifactor_v2.hml_iv with carry_gate on a
     G10-ONLY universe (DKK/KRW/EM dropped per cycle-1 finding that breadth-without-info is drag).
     Native monthly series.
  3. H4 trend-continuation (EMA-pullback + 2*ATR trail, ~+0.038R net). H4 OHLC resampled from
     HistData M1 for EURUSD/GBPUSD/USDJPY; pullback-to-EMA20 entries in the EMA20>EMA50 trend,
     chandelier 2*ATR trailing stop. Daily summed R -> monthly.

METHODOLOGY NOTE (deliberate engineering choice, flagged for the operator):
Because the carry sleeve is fundamentally MONTHLY, mixing it with daily intraday PnL and
annualising a spread-to-daily step function would corrupt the vol estimate (understate carry
daily vol, inflate its daily Sharpe). So the cross-sleeve blend is built at a COMMON MONTHLY
frequency (intraday sleeves 1 & 3 are aggregated up to monthly; carry stays native monthly).
This is the statistically honest common ground. maxDD is therefore month-granular (it slightly
understates intra-month drawdown) — noted in the report.

Equal-risk weights = inverse realized monthly vol, normalised to sum 1 (scale-invariant, so the
nominal per-trade risk of sleeve 3 does not affect the blend). Leverage L is a raw multiplier on
that base portfolio; the frontier reports CAGR / maxDD / worst-month / annual-vol for L=1..6 and
solves for the L that hits 30% CAGR.

Sealed hold-out: weights are frozen on the pre-2024 window and applied UNCHANGED to 2024+ (fit
nothing). The 3-sleeve common window ends 2024-12 (HistData M1 coverage), so the 3-sleeve sealed
read is 2024; the two extendable sleeves (eur_short + carry) are also reported through 2026.

Standalone-runnable: python3 scripts/probe_multisleeve_portfolio.py
"""

from __future__ import annotations

import glob
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
WORK = os.path.join(ROOT, "WORK")
os.makedirs(WORK, exist_ok=True)

from probe_fx_multifactor_v2 import (  # noqa: E402  (sibling reuse, per task)
    build_panel,
    carry_gate,
    excess_returns,
    hml_iv,
    monthly_vol,
)

G10 = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "NOK", "SEK"]
TREND_PAIRS = ["EURUSD", "GBPUSD", "USDJPY"]


# --------------------------------------------------------------------------- Sleeve 1
def sleeve1_eur_short() -> pd.Series:
    """Daily fractional return of a short EURUSD 11:00->12:00 UTC position, spread-inclusive."""
    cache = os.path.join(WORK, "sleeve1_1100short_daily.csv")
    if os.path.exists(cache):
        s = pd.read_csv(cache, parse_dates=["date"]).set_index("date")["ret"]
        return s
    files = sorted(glob.glob(os.path.join(ROOT, "data/ticks/EURUSD/*.parquet")))
    rows: dict = {}
    for f in files:
        df = pd.read_parquet(f, columns=["time", "bid", "ask"]).sort_values("time")
        days = pd.DatetimeIndex(pd.to_datetime(df["time"].dt.date.unique())).tz_localize("UTC")
        t11 = pd.DataFrame({"time": days + pd.Timedelta(hours=11)})
        t12 = pd.DataFrame({"time": days + pd.Timedelta(hours=12)})
        m11 = pd.merge_asof(t11, df, on="time", direction="backward", tolerance=pd.Timedelta("30min"))
        m12 = pd.merge_asof(t12, df, on="time", direction="backward", tolerance=pd.Timedelta("30min"))
        for i in range(len(days)):
            b11, a11 = m11["bid"].iloc[i], m11["ask"].iloc[i]
            b12, a12 = m12["bid"].iloc[i], m12["ask"].iloc[i]
            if not np.all(np.isfinite([b11, a11, b12, a12])):
                continue
            entry_mid = 0.5 * (b11 + a11)
            rows[days[i].tz_localize(None).normalize()] = (b11 - a12) / entry_mid  # short: sell bid, buy ask
    s = pd.Series(rows).sort_index()
    s.name = "ret"
    s.to_csv(cache, index_label="date")
    return s


# --------------------------------------------------------------------------- Sleeve 2
def sleeve2_carry() -> pd.Series:
    """Crash-gated G10 carry HML, native monthly (reuses probe_fx_multifactor_v2)."""
    S, R, SD = build_panel()
    g10 = [c for c in G10 if c in S.columns]
    xs = excess_returns(S, R)
    vol = monthly_vol(SD, S.index)
    gate = carry_gate(vol, [c for c in g10 if c != "USD"])
    carry_sig = R.sub(R["USD"], axis=0)
    book = hml_iv(carry_sig, xs, vol, g10, gate=gate).dropna()
    book.name = "ret"
    return book


# --------------------------------------------------------------------------- Sleeve 3
def load_h4(pair: str) -> pd.DataFrame:
    cache = os.path.join(WORK, f"{pair}_H4_2016.parquet")
    if os.path.exists(cache):
        return pd.read_parquet(cache)
    csv = os.path.join(ROOT, f"data/candles/histdata/{pair}_M1.csv")
    df = pd.read_csv(csv, usecols=["time", "open", "high", "low", "close"], dtype={"time": str})
    df = df[df["time"] >= "2016-01-01"].copy()
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time").sort_index()
    h4 = pd.DataFrame(
        {
            "open": df["open"].resample("4h").first(),
            "high": df["high"].resample("4h").max(),
            "low": df["low"].resample("4h").min(),
            "close": df["close"].resample("4h").last(),
        }
    ).dropna()
    h4.to_parquet(cache)
    return h4


def trend_trades(h4: pd.DataFrame) -> list:
    """EMA20/50 trend, pullback-to-EMA20 entry, chandelier 2*ATR trail. Returns [(exit_time, R)]."""
    c, hi, lo = h4["close"], h4["high"], h4["low"]
    e20 = c.ewm(span=20, adjust=False).mean().values
    e50 = c.ewm(span=50, adjust=False).mean().values
    prevc = c.shift(1)
    tr = pd.concat([hi - lo, (hi - prevc).abs(), (lo - prevc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean().values
    idx = h4.index
    C, H, L = c.values, hi.values, lo.values
    state = 0
    entry = stop = risk = ext = 0.0
    recs = []
    for i in range(51, len(idx)):
        if state == 0:
            if e20[i] > e50[i] and L[i] <= e20[i] and C[i] > e20[i] and atr[i] > 0:
                state, entry, risk, stop, ext = 1, C[i], 2 * atr[i], C[i] - 2 * atr[i], H[i]
            elif e20[i] < e50[i] and H[i] >= e20[i] and C[i] < e20[i] and atr[i] > 0:
                state, entry, risk, stop, ext = -1, C[i], 2 * atr[i], C[i] + 2 * atr[i], L[i]
        elif state == 1:
            ext = max(ext, H[i])
            stop = max(stop, ext - risk)
            if L[i] <= stop:
                recs.append((idx[i], (stop - entry) / risk))
                state = 0
        else:  # state == -1
            ext = min(ext, L[i])
            stop = min(stop, ext + risk)
            if H[i] >= stop:
                recs.append((idx[i], (entry - stop) / risk))
                state = 0
    return recs


def sleeve3_trend() -> pd.Series:
    """Daily summed trade R across the 3 pairs."""
    recs = []
    for p in TREND_PAIRS:
        recs += trend_trades(load_h4(p))
    df = pd.DataFrame(recs, columns=["time", "R"])
    df["date"] = pd.to_datetime(df["time"]).dt.tz_localize(None).dt.normalize()
    return df.groupby("date")["R"].sum().sort_index()


# --------------------------------------------------------------------------- Aggregation / blend
def daily_ret_to_monthly(s: pd.Series) -> pd.Series:
    s = s.copy()
    s.index = pd.DatetimeIndex(s.index).tz_localize(None)
    m = (1 + s).resample("ME").prod() - 1
    m.index = m.index.to_period("M")
    return m


def daily_R_to_monthly(s: pd.Series, risk_frac: float = 0.005) -> pd.Series:
    s = s.copy()
    s.index = pd.DatetimeIndex(s.index).tz_localize(None)
    m = risk_frac * s.resample("ME").sum()
    m.index = m.index.to_period("M")
    return m


def monthly_native(s: pd.Series) -> pd.Series:
    s = s.copy()
    s.index = pd.DatetimeIndex(s.index).to_period("M")
    return s


def ann_stats(m: pd.Series) -> dict:
    m = m.dropna()
    mu, sd = m.mean() * 12, m.std() * np.sqrt(12)
    eq = (1 + m).cumprod()
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    return {
        "sharpe": mu / sd if sd else float("nan"),
        "ret": mu,
        "vol": sd,
        "maxDD": dd,
        "worstMo": m.min(),
        "n": len(m),
    }


def lev_stats(m: pd.Series, L: float) -> dict:
    r = L * m.dropna()
    ruin = bool((r <= -1).any())
    eq = (1 + r.clip(lower=-0.999999)).cumprod()
    cagr = eq.iloc[-1] ** (12 / len(r)) - 1
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    return {"L": L, "cagr": cagr, "maxDD": dd, "worstMo": r.min(), "annVol": r.std() * np.sqrt(12), "ruin": ruin}


def solve_leverage_for_cagr(m: pd.Series, target: float = 0.30) -> dict | None:
    grid = np.arange(0.25, 20.01, 0.05)
    for L in grid:
        st = lev_stats(m, float(L))
        if st["ruin"]:
            return {**st, "note": "ruin (a month <= -100%) before hitting target"}
        if st["cagr"] >= target:
            return st
    return None


# --------------------------------------------------------------------------- Main
def main() -> None:
    print("=" * 100)
    print("TASK 1124 — MULTI-SLEEVE FX PORTFOLIO + LEVERAGE/DD FRONTIER")
    print("=" * 100)

    print("\n[1/3] building sleeve 1: 11:00 EUR short (from ticks) ...")
    s1 = sleeve1_eur_short()
    print(
        f"      {len(s1)} trading days, {s1.index.min().date()}..{s1.index.max().date()}, "
        f"mean {s1.mean() * 1e4:+.2f}bps/day"
    )

    print("[2/3] building sleeve 2: crash-gated G10 carry (FRED) ...")
    s2 = sleeve2_carry()
    print(f"      {len(s2)} months, {s2.index.min().date()}..{s2.index.max().date()}")

    print("[3/3] building sleeve 3: H4 EMA-pullback trend (HistData M1) ...")
    s3 = sleeve3_trend()
    n_trades = int((s3 != 0).sum())
    print(
        f"      {n_trades} active days of {len(s3)}, {s3.index.min().date()}..{s3.index.max().date()}, "
        f"mean {s3.mean():+.3f}R/active-day"
    )

    m1 = daily_ret_to_monthly(s1)
    m2 = monthly_native(s2)
    m3 = daily_R_to_monthly(s3)
    panel = pd.DataFrame({"eur_short": m1, "carry": m2, "trend": m3}).dropna()
    win = f"{panel.index.min()}..{panel.index.max()}"
    print(f"\nCOMMON 3-SLEEVE MONTHLY WINDOW: {win}  ({len(panel)} months)\n")

    print("-" * 100)
    print("PER-SLEEVE (annualised, monthly freq, unlevered on own capital):")
    per = {}
    for col in panel.columns:
        st = ann_stats(panel[col])
        per[col] = st
        print(
            f"  {col:10s} Sharpe {st['sharpe']:+.2f}  ret {st['ret'] * 100:+6.1f}%  "
            f"vol {st['vol'] * 100:5.1f}%  maxDD {st['maxDD'] * 100:6.1f}%  worstMo {st['worstMo'] * 100:+6.1f}%"
        )

    print("\nREALIZED CORRELATION MATRIX (monthly):")
    corr = panel.corr()
    print(corr.round(3).to_string())

    # equal-risk (inverse-vol) weights, full sample
    w = 1.0 / panel.std()
    w /= w.sum()
    print("\nEQUAL-RISK (inverse-vol) WEIGHTS:", {k: round(v, 3) for k, v in w.items()})
    blend = panel.mul(w, axis=1).sum(axis=1)
    b = ann_stats(blend)
    print(
        f"\nBLENDED PORTFOLIO (1x): Sharpe {b['sharpe']:+.2f}  ret {b['ret'] * 100:+.1f}%  "
        f"vol {b['vol'] * 100:.1f}%  maxDD {b['maxDD'] * 100:.1f}%  worstMo {b['worstMo'] * 100:+.1f}%"
    )

    print("\n" + "-" * 100)
    print("LEVERAGE FRONTIER (multiplier on the equal-risk base portfolio):")
    print(f"  {'L':>4} {'annVol':>8} {'CAGR':>8} {'maxDD':>8} {'worstMo':>9} {'ruin':>6}")
    frontier = []
    for L in range(1, 7):
        st = lev_stats(blend, float(L))
        frontier.append(st)
        print(
            f"  {L:>4} {st['annVol'] * 100:>7.1f}% {st['cagr'] * 100:>7.1f}% "
            f"{st['maxDD'] * 100:>7.1f}% {st['worstMo'] * 100:>+8.1f}% {st['ruin']!s:>6}"
        )

    tgt = solve_leverage_for_cagr(blend, 0.30)
    print("\n30% CAGR TARGET:")
    if tgt is None:
        print("  unreachable within L<=20x without ruin.")
    elif tgt.get("note"):
        print(f"  {tgt['note']} (at L={tgt['L']:.2f})")
    else:
        print(
            f"  needs L={tgt['L']:.2f}x  ->  annVol {tgt['annVol'] * 100:.0f}%, "
            f"CAGR {tgt['cagr'] * 100:.0f}%, maxDD {tgt['maxDD'] * 100:.0f}%, worstMo {tgt['worstMo'] * 100:+.0f}%"
        )

    # ---------- sealed hold-out ----------
    print("\n" + "-" * 100)
    print("SEALED HOLD-OUT (weights FROZEN on pre-2024, applied unchanged):")
    train = panel[panel.index < pd.Period("2024-01", "M")]
    hold = panel[panel.index >= pd.Period("2024-01", "M")]
    wf = 1.0 / train.std()
    wf /= wf.sum()
    print(
        f"  frozen weights (pre-2024): {{'eur_short': {wf['eur_short']:.3f}, "
        f"'carry': {wf['carry']:.3f}, 'trend': {wf['trend']:.3f}}}"
    )
    bt = ann_stats(train.mul(wf, axis=1).sum(axis=1))
    bh = ann_stats(hold.mul(wf, axis=1).sum(axis=1))
    hdr = f"  TRAIN 2016-2023 ({bt['n']}mo): Sharpe {bt['sharpe']:+.2f}"
    print(hdr + f"  ret {bt['ret'] * 100:+.1f}%  vol {bt['vol'] * 100:.1f}%")
    print(
        f"  SEALED {hold.index.min()}..{hold.index.max()} ({bh['n']}mo): "
        f"Sharpe {bh['sharpe']:+.2f}  ret {bh['ret'] * 100:+.1f}%  vol {bh['vol'] * 100:.1f}%"
    )

    # 2-sleeve view that extends past 2024 (eur_short + carry only)
    p2 = pd.DataFrame({"eur_short": m1, "carry": m2}).dropna()
    w2 = 1.0 / p2.std()
    w2 /= w2.sum()
    b2 = ann_stats(p2.mul(w2, axis=1).sum(axis=1))
    p2h = p2[p2.index >= pd.Period("2024-01", "M")]
    b2h = ann_stats(p2h.mul(w2, axis=1).sum(axis=1))
    print(
        f"\n  [2-sleeve eur_short+carry, extends to {p2.index.max()}] "
        f"full Sharpe {b2['sharpe']:+.2f} ({b2['n']}mo); sealed 2024+ Sharpe {b2h['sharpe']:+.2f} ({b2h['n']}mo)"
    )

    # ---------- verdict ----------
    print("\n" + "=" * 100)
    verdict_thresh = 0.6
    if b["sharpe"] < verdict_thresh:
        print(f"VERDICT: blended Sharpe {b['sharpe']:+.2f} < {verdict_thresh} -> per task, 30% CAGR is NOT")
        print("         achievable at survivable drawdown. See frontier: the 30% leverage point carries a")
        print("         drawdown far beyond prop-firm caps. OPERATOR DECISION REQUIRED on acceptable risk,")
        print("         not further leverage.")
    else:
        print(
            f"VERDICT: blended Sharpe {b['sharpe']:+.2f} >= {verdict_thresh} -> stacking is viable; "
            "pick a leverage point from the frontier."
        )
    print("=" * 100)

    # ---------- persist ----------
    out = {
        "window": win,
        "n_months": len(panel),
        "per_sleeve": {k: {kk: float(vv) for kk, vv in v.items()} for k, v in per.items()},
        "corr": corr.round(4).to_dict(),
        "weights": {k: float(v) for k, v in w.items()},
        "blend_1x": {k: float(v) for k, v in b.items()},
        "frontier": [{k: (float(v) if not isinstance(v, bool) else v) for k, v in st.items()} for st in frontier],
        "target_30pct": (
            None if tgt is None else {k: (float(v) if isinstance(v, (int, float)) else v) for k, v in tgt.items()}
        ),
        "sealed": {
            "train_sharpe": float(bt["sharpe"]),
            "hold_sharpe": float(bh["sharpe"]),
            "hold_window": f"{hold.index.min()}..{hold.index.max()}",
            "hold_n": bh["n"],
            "two_sleeve_full_sharpe": float(b2["sharpe"]),
            "two_sleeve_sealed_sharpe": float(b2h["sharpe"]),
            "two_sleeve_sealed_n": b2h["n"],
        },
        "verdict_blended_sharpe": float(b["sharpe"]),
    }
    with open(os.path.join(WORK, "multisleeve_results.json"), "w") as fh:
        json.dump(out, fh, indent=2)
    panel.to_csv(os.path.join(WORK, "multisleeve_monthly_panel.csv"))
    print("\nartifacts -> WORK/multisleeve_results.json, WORK/multisleeve_monthly_panel.csv")


if __name__ == "__main__":
    main()
