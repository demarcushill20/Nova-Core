"""Phase 2 (exit-manager) — probe whether an RL exit-manager can beat IRB's
fixed exits on EURUSD H1. Cheap GO/NO-GO before any training.

The exit-manager's job: while an IRB trade is open, decide hold vs close-now
(the simplest meaningful action). We test it in two gates:

PART A — Headroom. Compare the fixed policy's realised net-R to an oracle that
closes each trade at its best in-trade bar (perfect-hindsight exit). The gap is
the *maximum* R an exit-manager could ever recover. Small gap -> nothing to win.

PART B — Separability + simulated policy. At each in-trade bar we capture market
context + position state (unrealised R, favourable/adverse excursion, bars held)
and label whether closing now beats holding to the fixed exit. Then:
  * OOF AUC of that label (time-ordered CV) — is "close now" predictable?
  * A *causal* simulated early-exit policy: walk each test trade's bars in order,
    close at the first bar the model flags; else ride the fixed exit. Compare its
    OOF mean net-R to the fixed base AND to a label-permutation null.

A GO needs real headroom AND a simulated policy that beats both base and null.

Usage
-----
    python -m scripts.probe_irb_rl_exit
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import TimeSeriesSplit

from novatrade.backtest.fidelity import PIP_DEFAULT, _resample_with_subs
from novatrade.strategies.vault_irb_state import IRBConfig, ohlc_path, prepare_dataframe
from scripts.build_irb_rl_dataset import _add_features, _RecorderState


def build_snapshots(m1_path: Path, primary_tf: str, cost_pips: float):
    """Run the fixed IRB policy; emit one snapshot per in-trade bar + per-trade
    realised net-R. Returns (snapshots_df, feature_cols)."""
    cfg = IRBConfig()
    pip = PIP_DEFAULT

    fine = pd.read_csv(m1_path)
    fine.columns = [c.lower().strip() for c in fine.columns]
    if "time" not in fine.columns and "timestamp" in fine.columns:
        fine = fine.rename(columns={"timestamp": "time"})
    fine["time"] = pd.to_datetime(fine["time"])
    fine = fine[["time", "open", "high", "low", "close"]].sort_values("time").reset_index(drop=True)
    print(f"[load] {len(fine):,} M1 bars  {fine['time'].iloc[0]} -> {fine['time'].iloc[-1]}")

    primary, subs = _resample_with_subs(fine, primary_tf)
    pdf = prepare_dataframe(primary, cfg)
    sub_paths: dict[int, list[float]] = {}
    for i, r in pdf.iterrows():
        sub = subs.get(r["time"])
        if sub:
            path: list[float] = []
            for o, hi, lo, c in sub:
                path += ohlc_path(o, hi, lo, c)
            sub_paths[i] = path
    market_cols = _add_features(pdf, cfg)

    st = _RecorderState(cfg, sub_paths=sub_paths, cost_pips=cost_pips, pip=pip)
    track: dict[int, dict] = {}
    snaps: list[dict] = []
    for i, row in pdf.iterrows():
        st.process_bar(i, row, pdf)
        pos = st.position
        if pos is None:
            continue
        eb = pos["entry_bar"]
        entry = pos["entry_price"]
        stop_dist = abs(entry - pos["initial_stop"])
        if stop_dist <= 0:
            continue
        t = track.setdefault(eb, {"max_high": row["high"], "min_low": row["low"]})
        t["max_high"] = max(t["max_high"], row["high"])
        t["min_low"] = min(t["min_low"], row["low"])
        side = pos["side"]
        close = row["close"]
        stop_pips = stop_dist / pip
        cost_r = cost_pips / stop_pips
        if side == "long":
            unreal = (close - entry) / stop_dist
            fav = (t["max_high"] - entry) / stop_dist
            adv = (t["min_low"] - entry) / stop_dist
        else:
            unreal = (entry - close) / stop_dist
            fav = (entry - t["min_low"]) / stop_dist
            adv = (entry - t["max_high"]) / stop_dist
        snap = {c: float(pdf.iloc[i][c]) for c in market_cols}
        snap.update(
            {
                "entry_bar": int(eb),
                "bar": int(i),
                "entry_time": pos["entry_time"],
                "s_side_long": 1.0 if side == "long" else 0.0,
                "s_unreal_r": float(unreal),
                "s_fav_r": float(fav),
                "s_adv_r": float(adv),
                "s_bars_in_trade": float(i - eb),
                "r_close_now_net": float(unreal - cost_r),
            }
        )
        snaps.append(snap)

    # per-trade realised net-R from the correct-by-construction ledger
    realized: dict[int, float] = {}
    for p in st._positions:
        risk = p["stop_pips"] * pip * p["qty"]
        if risk > 0 and p["stop_pips"] > 0:
            realized[p["entry_bar"]] = float(p["net"] / risk)

    snap_df = pd.DataFrame(snaps)
    snap_df["r_hold"] = snap_df["entry_bar"].map(realized)
    snap_df = snap_df.dropna(subset=["r_hold"]).reset_index(drop=True)
    snap_df["label_close"] = (snap_df["r_close_now_net"] > snap_df["r_hold"]).astype(int)

    feat_cols = market_cols + ["s_side_long", "s_unreal_r", "s_fav_r", "s_adv_r", "s_bars_in_trade"]
    return snap_df, feat_cols


def part_a_headroom(snap_df: pd.DataFrame) -> None:
    g = snap_df.groupby("entry_bar")
    r_hold = g["r_hold"].first()
    oracle = g["r_close_now_net"].max()  # best in-trade close (incl. final-ish bars)
    oracle = np.maximum(oracle, r_hold)  # holding to fixed exit is always an option
    print("\n=== PART A — exit headroom (per trade) ===")
    print(f"trades                    : {len(r_hold):,}")
    print(f"fixed-policy mean net-R   : {r_hold.mean():+.4f}")
    print(f"perfect-exit ceiling net-R: {oracle.mean():+.4f}")
    print(f"max recoverable headroom  : {oracle.mean() - r_hold.mean():+.4f} R/trade")


def _simulate_policy(test_df: pd.DataFrame, score: np.ndarray, thresh: float) -> np.ndarray:
    """Causal early-exit: per trade, close at first bar where score>thresh (realise
    r_close_now_net there); else ride to fixed r_hold. Returns per-trade net-R."""
    tmp = test_df[["entry_bar", "bar", "r_close_now_net", "r_hold"]].copy()
    tmp["score"] = score
    out = []
    for _, grp in tmp.groupby("entry_bar"):
        grp = grp.sort_values("bar")
        fired = grp[grp["score"] > thresh]
        out.append(float(fired["r_close_now_net"].iloc[0]) if len(fired) else float(grp["r_hold"].iloc[0]))
    return np.array(out)


def part_b_separability(snap_df: pd.DataFrame, feat_cols: list[str], n_splits: int, n_perm: int, thresh: float) -> None:
    snap_df = snap_df.sort_values(["entry_bar", "bar"]).reset_index(drop=True)
    trades = snap_df["entry_bar"].drop_duplicates().to_numpy()  # entry_bar increases with time
    X_all = np.nan_to_num(snap_df[feat_cols].to_numpy(float), nan=0.0, posinf=0.0, neginf=0.0)
    y_all = snap_df["label_close"].to_numpy(int)

    tscv = TimeSeriesSplit(n_splits=n_splits)

    def run(permute: bool, seed: int):
        rng = np.random.default_rng(seed)
        aucs, pol_r, base_r = [], [], []
        for tr_t, te_t in tscv.split(trades):
            tr_trades, te_trades = set(trades[tr_t]), set(trades[te_t])
            tr_mask = snap_df["entry_bar"].isin(tr_trades).to_numpy()
            te_mask = snap_df["entry_bar"].isin(te_trades).to_numpy()
            ytr = y_all[tr_mask].copy()
            if permute:
                ytr = ytr[rng.permutation(len(ytr))]
            if len(np.unique(ytr)) < 2:
                continue
            clf = GradientBoostingClassifier(
                n_estimators=120, max_depth=2, learning_rate=0.03, subsample=0.8, random_state=seed
            )
            clf.fit(X_all[tr_mask], ytr)
            s_te = clf.predict_proba(X_all[te_mask])[:, 1]
            if not permute and len(np.unique(y_all[te_mask])) > 1:
                aucs.append(roc_auc_score(y_all[te_mask], s_te))
            te_df = snap_df[te_mask]
            pol = _simulate_policy(te_df, s_te, thresh)
            base = te_df.groupby("entry_bar")["r_hold"].first().to_numpy()
            pol_r.append(pol.mean())
            base_r.append(base.mean())
        return (float(np.mean(aucs)) if aucs else float("nan"), float(np.mean(pol_r)), float(np.mean(base_r)))

    auc, pol, base = run(permute=False, seed=0)
    null_pol = np.array([run(permute=True, seed=1000 + s)[1] for s in range(n_perm)])
    uplift = pol - base
    p_val = float((null_pol - base >= uplift).mean())

    print("\n=== PART B — separability + simulated early-exit policy (OOF) ===")
    print(f"snapshots (in-trade bars) : {len(snap_df):,} over {len(trades):,} trades")
    print(f"OOF AUC of 'close>hold'   : {auc:.4f}  (0.50 = no signal)")
    print(f"fixed base mean net-R     : {base:+.4f}")
    print(f"early-exit policy net-R   : {pol:+.4f}  (uplift {uplift:+.4f}, thresh={thresh})")
    null_mean = null_pol.mean() - base
    null_p95 = np.percentile(null_pol, 95) - base
    print(f"null policy uplift (n={n_perm}): mean {null_mean:+.4f}  p95 {null_p95:+.4f}")
    print(f"p-value (null uplift >= real): {p_val:.3f}")
    go = (auc > 0.53) and (uplift > 0.02) and (p_val < 0.05)
    verdict = (
        "GO — exit-manager beats fixed exits beyond the null."
        if go
        else "NO-GO — no learnable exit edge above the fixed policy + null."
    )
    print(f"\n{verdict}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--m1", default="data/candles/EURUSD_M1.csv")
    ap.add_argument("--primary-tf", default="60min")
    ap.add_argument("--cost-pips", type=float, default=0.5)
    ap.add_argument("--splits", type=int, default=6)
    ap.add_argument("--n-perm", type=int, default=20)
    ap.add_argument("--thresh", type=float, default=0.6)
    args = ap.parse_args()

    snap_df, feat_cols = build_snapshots(Path(args.m1), args.primary_tf, args.cost_pips)
    part_a_headroom(snap_df)
    part_b_separability(snap_df, feat_cols, args.splits, args.n_perm, args.thresh)


if __name__ == "__main__":
    main()
