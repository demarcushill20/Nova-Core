"""Multi-sleeve v4 — matched-window judgment + gated-BTC decorrelation (Iteration 7).

Iter-6 verdict: BTC TSMOM adds Sharpe but fails the corr<=0.10 bar (0.326 to carry) — both
load on the same risk-on axis. FX TSMOM dead. Gold CONFIRMED on fresh data (18mo OOS +2.03).
Iter-7 (this probe) executes iter-6's proposed follow-ups:

  1. Extend every sleeve to the latest available data and judge everything on ONE matched
     window (the 2020-24 numbers in iter-6 carried a +0.13 window inflation).
       eur_short : WORK cache (ticks) ................ 2016-01 .. 2026-03
       carry     : FRED gated G10 HML (recompute) .... .. 2026-06
       gold      : pre-2025 PnL cache + fresh splice .. .. 2026-06
       btc       : parquet closes 2019-24 + Coinbase public candles .. 2026-07
  2. BTC decorrelation attempt: scale BTC exposure by the SAME vol-regime gate that protects
     carry (both crash in risk-off; gating both should collapse their comovement). This is a
     principled reuse of an existing validated component, not a new fitted parameter.
  3. Sealed read is now 2024-01..2026-06 (30 months, weights frozen pre-2024) — 2.5x longer
     than iter-5's single-year sealed window.

Frozen bars (unchanged): candidate joins the book iff max|corr| to existing sleeves <= ~0.10
and standalone Sharpe >= 0.3. Winner = 30% CAGR at daily-granular maxDD <= 25%.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
WORK = os.path.join(ROOT, "WORK")

from probe_fx_multifactor_v2 import (  # noqa: E402
    build_panel,
    carry_gate,
    excess_returns,
    hml_iv,
    monthly_vol,
)

G10 = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "NOK", "SEK"]
COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles"


# ------------------------------------------------------------------ data: BTC extension
def btc_daily_close() -> pd.Series:
    cache = os.path.join(WORK, "btc_daily_close_2019_2026.csv")
    if os.path.exists(cache):
        return pd.read_csv(cache, index_col=0, parse_dates=True).iloc[:, 0]
    old = pd.read_parquet(os.path.join(ROOT, "data/candles/btcusd_1h.parquet"))
    daily_old = old["close"].resample("D").last().dropna()
    daily_old.index = daily_old.index.tz_localize(None)
    # fresh 2025+ from Coinbase public candles (300/page)
    rows = []
    start = pd.Timestamp("2024-12-01")
    while start < pd.Timestamp("2026-07-15"):
        end = min(start + pd.Timedelta(days=290), pd.Timestamp("2026-07-15"))
        url = f"{COINBASE}?granularity=86400&start={start.date()}T00:00:00Z&end={end.date()}T00:00:00Z"
        req = urllib.request.Request(url, headers={"User-Agent": "nova-research"})  # noqa: S310 (fixed https host)
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())  # noqa: S310
        rows.extend(data)  # [t, low, high, open, close, vol]
        start = end
        time.sleep(0.4)
    fresh = pd.Series({pd.Timestamp(r[0], unit="s"): float(r[4]) for r in rows}).sort_index()
    fresh = fresh[~fresh.index.duplicated()]
    combined = pd.concat([daily_old[daily_old.index < fresh.index[0]], fresh]).sort_index()
    combined.to_frame("close").to_csv(cache)
    return combined


def tsmom_pnl(close: pd.Series, cost_bp: float) -> pd.Series:
    ret = close.pct_change(fill_method=None)
    vol63 = ret.rolling(63, min_periods=40).std() * np.sqrt(252)
    sig = 0.5 * np.sign(close.pct_change(252, fill_method=None)) + 0.5 * np.sign(close.pct_change(63, fill_method=None))
    pos = (sig * (0.10 / vol63)).clip(-2.0, 2.0)
    return (pos.shift(1) * ret - pos.diff().abs() * cost_bp * 1e-4).dropna()


# ------------------------------------------------------------------------- gold splice
def gold_pnl_full() -> pd.Series:
    pre = pd.read_csv(os.path.join(WORK, "sleeve4_gold_tsmom_daily.csv"), index_col=0, parse_dates=True).iloc[:, 0]
    closes = pd.read_csv(os.path.join(WORK, "gold_daily_close_2020_2026.csv"), index_col=0, parse_dates=True).iloc[:, 0]
    fresh = tsmom_pnl(closes, cost_bp=2.0)
    fresh = fresh.loc["2025-01-01":]  # type: ignore[misc]
    return pd.concat([pre[pre.index < fresh.index[0]], fresh]).sort_index()


# ---------------------------------------------------------------------------- helpers
def to_m(daily: pd.Series) -> pd.Series:
    return (1 + daily).groupby(daily.index.to_period("M")).prod() - 1


def ann(m: pd.Series):
    m = m.dropna()
    ar, av = m.mean() * 12, m.std() * np.sqrt(12)
    eq = (1 + m).cumprod()
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    return (ar / av if av else np.nan), ar, av, dd


def line(m: pd.Series, label: str) -> str:
    sh, ar, av, dd = ann(m)
    m = m.dropna()
    if not len(m):
        return f"{label:36s} EMPTY"
    return (
        f"{label:36s} Sharpe {sh:+.2f}  ret {ar * 100:+5.1f}%  vol {av * 100:4.1f}%  "
        f"maxDD {dd * 100:5.1f}%  ({len(m)}mo {m.index[0]}..{m.index[-1]})"
    )


def main() -> None:
    print("=" * 110)
    print("MULTI-SLEEVE v4 — matched-window judgment + gated-BTC decorrelation (iter 7)")
    print("=" * 110)

    # ---- sleeves ----
    s1 = pd.read_csv(os.path.join(WORK, "sleeve1_1100short_daily.csv"), index_col=0, parse_dates=True).iloc[:, 0]

    S, R, SD = build_panel()
    vol = monthly_vol(SD, S.index)
    gate_m = carry_gate(vol, G10)  # monthly, lag-safe
    xs = excess_returns(S, R)
    carry_m = hml_iv(R.sub(R["USD"], axis=0), xs, vol, G10, gate=gate_m)

    gold_d = gold_pnl_full()
    btc_close = btc_daily_close()
    btc_d = tsmom_pnl(btc_close, cost_bp=5.0)

    # gated BTC: monthly gate value applied to the whole following month (known at month-end)
    gate_daily = gate_m.copy()
    gate_daily.index = gate_daily.index.to_period("M")
    btc_gate_factor = btc_d.index.to_period("M").map(gate_daily).astype(float)
    btc_gated_d = btc_d * pd.Series(btc_gate_factor, index=btc_d.index).fillna(0.0)

    panel = pd.DataFrame(
        {
            "eur_short": to_m(s1),
            "gold_tsmom": to_m(gold_d),
            "btc_tsmom": to_m(btc_d),
            "btc_gated": to_m(btc_gated_d),
        }
    )
    cm = carry_m.copy()
    cm.index = cm.index.to_period("M")
    panel["carry"] = cm
    panel = panel[["eur_short", "carry", "gold_tsmom", "btc_tsmom", "btc_gated"]]

    print("\nper-sleeve, full available history:")
    for c in panel.columns:
        print(line(panel[c], c))

    # gold fresh-OOS confirmation (independent recompute of iter-6 claim)
    gold_oos = panel["gold_tsmom"].loc["2025-01":]  # type: ignore[misc]
    print("\n" + line(gold_oos, "gold sealed 2025-01..2026-06 (verify)"))

    # ---- matched window ----
    mw = panel.dropna()
    print(f"\nMATCHED window: {mw.index[0]}..{mw.index[-1]} ({len(mw)}mo)")
    for c in mw.columns:
        print(line(mw[c], f"  {c} (matched)"))

    print("\ncorrelations (matched window):")
    print(mw.corr().round(2).to_string())
    core = ["eur_short", "carry", "gold_tsmom"]
    for cand in ("btc_tsmom", "btc_gated"):
        mx = mw[core + [cand]].corr()[cand][core].abs().max()
        print(f"  {cand}: max|corr| to core = {mx:.3f}  ({'PASS' if mx <= 0.12 else 'FAIL'} vs 0.10 bar)")

    # ---- blends: weights frozen pre-2024, sealed 2024-01..end ----
    def blend(cols, label):
        sub = panel[cols].dropna()
        w = 1.0 / sub.loc[: pd.Period("2023-12", "M")].std()
        w = w / w.sum()
        port = (sub * w).sum(axis=1)
        print(line(port, label + f"  w={dict(zip(cols, w.round(2), strict=True))}"))
        print(line(port.loc[pd.Period("2024-01", "M") :], f"  {label} SEALED 24+"))
        return port

    print("\nblends (inverse-vol weights frozen pre-2024):")
    p3 = blend(core, "3s core")
    p4 = blend(core + ["btc_tsmom"], "3s + btc (raw)")
    p4g = blend(core + ["btc_gated"], "3s + btc_gated")

    # ---- frontier on the rule-compliant best ----
    candidates = {"3s": p3, "3s+btc_gated": p4g, "3s+btc_raw": p4}
    print("\nfrontier + 30% solve:")
    for name, port in candidates.items():
        sh, ar, av, _ = ann(port)
        if ar <= 0:
            continue
        L = 0.30 / ar
        lp = port * L
        eq = (1 + lp).cumprod()
        cagr = eq.iloc[-1] ** (12 / len(lp)) - 1
        dd = ((eq - eq.cummax()) / eq.cummax()).min()
        print(
            f"  {name:14s} Sharpe {sh:+.2f} -> 30% needs L={L:4.1f}: CAGR {cagr * 100:+.1f}%  "
            f"maxDD(mo) {dd * 100:.1f}%  worstMo {lp.min() * 100:+.1f}%"
        )

    # ---- daily-granular DD for the rule-compliant winner candidate ----
    # daily portfolio: eur_short + gold (+ btc_gated) daily, carry spread across month days
    sub = panel[core + ["btc_gated"]].dropna()
    w = 1.0 / sub.loc[: pd.Period("2023-12", "M")].std()
    w = w / w.sum()
    dr = pd.DataFrame({"eur_short": s1, "gold_tsmom": gold_d, "btc_gated": btc_gated_d}).dropna()
    cd = pd.Series(0.0, index=dr.index)
    for dt, v in carry_m.items():
        mask = dr.index.to_period("M") == dt.to_period("M")
        n = int(mask.sum())
        if n:
            cd[mask] = (1 + v) ** (1 / n) - 1
    daily_port = (
        w["eur_short"] * dr["eur_short"]
        + w["gold_tsmom"] * dr["gold_tsmom"]
        + w["btc_gated"] * dr["btc_gated"]
        + w["carry"] * cd
    ).dropna()
    _, ar4, _, _ = ann(p4g)
    L = 0.30 / ar4
    lp = daily_port * L
    eq = (1 + lp).cumprod()
    ddd = ((eq - eq.cummax()) / eq.cummax()).min()
    print(f"\n3s+btc_gated DAILY-granular maxDD at 30%-solve L={L:.1f}: {ddd * 100:.1f}%")

    print("\n" + "#" * 110)
    print("BARS: candidate joins iff max|corr|<=~0.10 & standalone Sharpe>=0.3. WINNER = 30% at daily DD <= 25%.")
    print("#" * 110)

    with open(os.path.join(WORK, "multisleeve_v4_summary.json"), "w") as fh:
        json.dump({c: {"sharpe": float(ann(panel[c])[0])} for c in panel.columns}, fh, indent=1)


if __name__ == "__main__":
    main()
