"""Entry-filter probe for the intraday EUR-weakness drift edge.

Question (pre-registered, falsifiable): can a model decide *which* days' 11:00 UTC
EURUSD shorts to take, using only information available BEFORE 11:00, and beat
taking every trade — out of sample, walk-forward?

This is the same test that KILLED the IRB+RL entry filter (walk-forward AUC 0.52,
p=0.125 — a coin flip; see MEMORY project_irb_rl_filter). We run it here cheaply
(logistic + HistGBM, no RL) before committing any RL compute. A thin statistical
drift (52% win rate, edge from aggregating ~2,500 tiny pushes) is the worst
possible signal-to-noise for per-instance conditioning, so the prior is "no".

Decision rule:
  * GO signal  = pooled OOS AUC >= 0.55 AND filtered net-pips/trade and Sharpe
                 beat take-all on BOTH datasets independently.
  * NO-GO      = AUC ~0.50-0.53 or no robust improvement (expected).

Trade model: SHORT at 11:00 UTC open, flat at 12:00 UTC open, round-trip cost
0.30p. PnL in pips (sizing-agnostic; fixed stop => R proportional to pips).

Datasets (independent vendors/eras — replication is the point):
  * OUTPUT/eurusd_15m_ticks_ohlc.parquet  Dukascopy ticks->15m, 2016-2026, UTC-aware
  * OUTPUT/eurusd_15m.parquet              HistData 15m, 2004-2024, naive (treat UTC)
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

PIP = 1e-4
SPREAD_PIPS = 0.30  # measured in-hour round-trip cost
ENTRY_HOUR = 11
EXIT_HOUR = 12


# ----------------------------------------------------------------------------- data
def load(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path)[["open", "high", "low", "close"]].copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.sort_index()


def bar_at(df: pd.DataFrame, day: pd.Timestamp, hour: int, minute: int = 0):
    ts = pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=hour, minute=minute, tz="UTC")
    return df.loc[ts] if ts in df.index else None


def build_trades(df: pd.DataFrame) -> pd.DataFrame:
    """One row per trading day: features (pre-11:00) + net-pip outcome of the short."""
    h = df.index.hour
    entry_idx = df.index[(h == ENTRY_HOUR) & (df.index.minute == 0)]
    rows = []
    # daily close series for prior-day returns (last bar of each day)
    daily_close = df["close"].groupby(df.index.normalize()).last()
    for ts in entry_idx:
        day = ts.normalize()
        if ts.weekday() > 4:
            continue
        entry = df.loc[ts, "open"]
        exit_ts = pd.Timestamp(year=ts.year, month=ts.month, day=ts.day, hour=EXIT_HOUR, tz="UTC")
        if exit_ts not in df.index:
            continue
        exit_px = df.loc[exit_ts, "open"]
        net_pips = (entry - exit_px) / PIP - SPREAD_PIPS  # SHORT: profit when price falls

        # ---- features, all strictly before 11:00 ----
        pre = df.loc[(df.index >= day) & (df.index < ts)]  # today, midnight..11:00
        morn = df.loc[(df.index >= day + pd.Timedelta(hours=8)) & (df.index < ts)]  # 08:00..11:00
        if len(pre) < 4:
            continue
        # vol regime: realized range over the pre-session, in pips
        pre_range_pips = (pre["high"].max() - pre["low"].min()) / PIP
        pre_ret_abs = pre["close"].pct_change().abs().mean() * 1e4  # avg |bar ret| in pips-ish
        # directional morning move into 11:00 (does morning EUR weakness continue?)
        morn_ret = ((pre["close"].iloc[-1] - pre["open"].iloc[0]) / pre["open"].iloc[0]) * 1e4
        morn8_ret = ((morn["close"].iloc[-1] - morn["open"].iloc[0]) / morn["open"].iloc[0]) * 1e4 if len(morn) else 0.0
        # prior-day returns
        prior_days = daily_close[daily_close.index < day]
        r1 = (
            np.log(daily_close[daily_close.index < day].iloc[-1] / prior_days.iloc[-2]) * 1e4
            if len(prior_days) >= 2
            else 0.0
        )
        r2 = np.log(prior_days.iloc[-2] / prior_days.iloc[-3]) * 1e4 if len(prior_days) >= 3 else 0.0
        # calendar
        dow = ts.weekday()
        # month-end: within last 3 business days of month
        month_end = int((ts + pd.offsets.BMonthEnd(0) - ts).days <= 4)
        # NFP: first Friday of month
        nfp = int(dow == 4 and ts.day <= 7)

        rows.append(
            dict(
                ts=ts,
                net_pips=net_pips,
                win=int(net_pips > 0),
                pre_range_pips=pre_range_pips,
                pre_ret_abs=pre_ret_abs,
                morn_ret=morn_ret,
                morn8_ret=morn8_ret,
                r1=r1,
                r2=r2,
                dow=dow,
                month_end=month_end,
                nfp=nfp,
            )
        )
    out = pd.DataFrame(rows).set_index("ts")
    # trailing edge momentum (past trades only — shift to avoid using current outcome)
    out["edge_mom20"] = out["net_pips"].rolling(20, min_periods=5).mean().shift(1).fillna(0.0)
    return out


FEATURES = [
    "pre_range_pips",
    "pre_ret_abs",
    "morn_ret",
    "morn8_ret",
    "r1",
    "r2",
    "dow",
    "month_end",
    "nfp",
    "edge_mom20",
]


# ------------------------------------------------------------------- walk-forward
def walk_forward(trades: pd.DataFrame, n_folds: int = 6):
    """Rolling-origin: train on a window, predict the next block. Pool OOS preds."""
    X = trades[FEATURES].values
    y = trades["win"].values
    pnl = trades["net_pips"].values
    n = len(trades)
    fold = n // (n_folds + 1)  # +1 so the first block is training-only
    models = {
        "logistic": lambda: make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, C=0.5)),
        "histgbm": lambda: HistGradientBoostingClassifier(
            max_depth=3, learning_rate=0.05, max_iter=200, l2_regularization=1.0, min_samples_leaf=40, random_state=0
        ),
    }
    results = {}
    for name, mk in models.items():
        oof_p = np.full(n, np.nan)
        per_fold = []
        for k in range(1, n_folds + 1):
            tr_end = fold * k
            te_end = min(fold * (k + 1), n)
            if te_end <= tr_end:
                break
            Xtr, ytr = X[:tr_end], y[:tr_end]
            Xte, yte = X[tr_end:te_end], y[tr_end:te_end]
            if len(np.unique(ytr)) < 2 or len(yte) < 20:
                continue
            m = mk()
            m.fit(Xtr, ytr)
            p = m.predict_proba(Xte)[:, 1]
            oof_p[tr_end:te_end] = p
            try:
                per_fold.append(roc_auc_score(yte, p))
            except ValueError:
                pass
        mask = ~np.isnan(oof_p)
        pooled_auc = roc_auc_score(y[mask], oof_p[mask]) if mask.sum() > 50 else float("nan")
        results[name] = dict(pooled_auc=pooled_auc, per_fold=per_fold, oof_p=oof_p, mask=mask, pnl=pnl, y=y)
    return results


def filter_vs_takeall(res: dict, q: float = 0.40):
    """Take only trades whose predicted P(win) is in the top (1-q) — i.e. skip the
    bottom q-quantile of conviction. Compare net-pips/trade and Sharpe to take-all,
    OOS only."""
    oof_p, mask, pnl = res["oof_p"], res["mask"], res["pnl"]
    p, pl = oof_p[mask], pnl[mask]
    thr = np.quantile(p, q)
    take = p >= thr

    def stats(x):
        return dict(
            n=len(x),
            mean=float(np.mean(x)),
            sharpe=float(np.mean(x) / (np.std(x) + 1e-9) * np.sqrt(252)),
            total=float(np.sum(x)),
        )

    return dict(
        threshold_q=q, take_all=stats(pl), filtered=stats(pl[take]), skipped=stats(pl[~take]) if (~take).any() else None
    )


def run(path: str, label: str):
    print(f"\n{'=' * 72}\n{label}\n  {path}\n{'=' * 72}")
    df = load(path)
    trades = build_trades(df)
    base = trades["net_pips"]
    t_stat = base.mean() / (base.std() / np.sqrt(len(base)))
    print(
        f"trades={len(trades)}  win_rate={trades['win'].mean():.3f}  "
        f"mean_net={base.mean():+.3f}p  edge t={t_stat:+.2f}  "
        f"period {trades.index.min().date()}..{trades.index.max().date()}"
    )
    res = walk_forward(trades)
    for name, r in res.items():
        pf = ", ".join(f"{a:.3f}" for a in r["per_fold"])
        print(f"  [{name:8s}] pooled OOS AUC = {r['pooled_auc']:.4f}   per-fold = [{pf}]")
    # filter test using the stronger model
    best = max(res, key=lambda k: res[k]["pooled_auc"])
    f = filter_vs_takeall(res[best])
    ta, fl = f["take_all"], f["filtered"]
    print(f"  filter test (model={best}, skip bottom {int(f['threshold_q'] * 100)}% conviction, OOS):")
    print(
        f"    take-all : n={ta['n']:4d}  mean={ta['mean']:+.3f}p  Sharpe={ta['sharpe']:+.2f}  total={ta['total']:+.0f}p"
    )
    print(
        f"    filtered : n={fl['n']:4d}  mean={fl['mean']:+.3f}p  Sharpe={fl['sharpe']:+.2f}  total={fl['total']:+.0f}p"
    )
    improved = (res[best]["pooled_auc"] >= 0.55) and (fl["mean"] > ta["mean"]) and (fl["sharpe"] > ta["sharpe"])
    print(f"    -> {'IMPROVES' if improved else 'NO ROBUST IMPROVEMENT'} on this dataset")
    return res[best]["pooled_auc"], improved


if __name__ == "__main__":
    verdicts = []
    verdicts.append(run("OUTPUT/eurusd_15m_ticks_ohlc.parquet", "Dukascopy ticks 2016-2026 (OOS-validated set)"))
    verdicts.append(run("OUTPUT/eurusd_15m.parquet", "HistData 2004-2024 (independent replication)"))

    aucs = [v[0] for v in verdicts]
    both_improve = all(v[1] for v in verdicts)
    print(f"\n{'#' * 72}\nVERDICT")
    print(f"  pooled OOS AUC: {aucs[0]:.4f} (Dukascopy), {aucs[1]:.4f} (HistData)")
    go = (min(aucs) >= 0.55) and both_improve
    print("  GO = AUC>=0.55 on both AND filter beats take-all on both")
    print(
        f"  -> {'GO: build the RL/GBM filter' if go else 'NO-GO: features do not separate winners OOS (same as IRB+RL)'}"
    )
    print("#" * 72)
    sys.exit(0)
