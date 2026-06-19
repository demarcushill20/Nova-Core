"""Phase 2 — IRB-RL filter separability probe (GO / NO-GO gate).

Question: can the setup features separate winning from losing IRB setups
*out-of-sample*? If they can't, no take/skip filter can help and we stop here.

This is deliberately a cheap, low-capacity probe — NOT the final filter. It uses
strictly time-ordered folds (train on past, test on future; never shuffle time),
and judges signal the only way that matters for a filter: by the realised net-R
of the *kept* set out-of-fold, not by classification accuracy.

Three guards against fooling ourselves:
  1. Time-ordered CV (sklearn TimeSeriesSplit) — no look-ahead.
  2. The headline metric is OOF kept-set net-R at several keep-fractions, vs the
     +0.000R take-everything base. A filter only earns its place if keeping the
     top-ranked setups lifts net-R clearly positive.
  3. A label-permutation null: shuffle net-R within each train fold and re-run.
     If the "uplift" survives shuffled labels, it was overfitting, not signal.

Usage
-----
    python scripts/probe_irb_rl_separability.py --data data/rl/irb_h1_setups.parquet
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

KEEP_FRACTIONS = (1.0, 0.7, 0.5, 0.3)


def _oof_scores(X: np.ndarray, y_win: np.ndarray, r: np.ndarray, n_splits: int, seed: int, permute: bool):
    """Return OOF (predicted_score, realised_net_r) over time-ordered test folds.

    Two models vote-averaged into one score (rank-normalised): a logistic
    win-classifier and a gradient-boosted net-R regressor. With `permute=True`
    the training targets are shuffled within each fold (null model)."""
    tscv = TimeSeriesSplit(n_splits=n_splits)
    rng = np.random.default_rng(seed)
    oof_score: list[float] = []
    oof_r: list[float] = []
    aucs: list[float] = []

    for tr, te in tscv.split(X):
        Xtr, Xte = X[tr], X[te]
        ytr = y_win[tr].copy()
        rtr = r[tr].copy()
        if permute:
            perm = rng.permutation(len(tr))
            ytr = ytr[perm]
            rtr = rtr[perm]

        clf = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, C=0.5))
        reg = GradientBoostingRegressor(
            n_estimators=120, max_depth=2, learning_rate=0.03, subsample=0.8, random_state=seed
        )

        s_clf = np.full(len(te), 0.5)
        if len(np.unique(ytr)) > 1:
            clf.fit(Xtr, ytr)
            s_clf = clf.predict_proba(Xte)[:, 1]
            if not permute and len(np.unique(y_win[te])) > 1:
                aucs.append(roc_auc_score(y_win[te], s_clf))
        reg.fit(Xtr, rtr)
        s_reg = reg.predict(Xte)

        # combine as average of rank-normalised scores (robust to scale)
        def _rank01(v):
            order = np.argsort(np.argsort(v))
            return order / max(len(v) - 1, 1)

        score = 0.5 * _rank01(s_clf) + 0.5 * _rank01(s_reg)
        oof_score.extend(score.tolist())
        oof_r.extend(r[te].tolist())

    return np.array(oof_score), np.array(oof_r), (float(np.mean(aucs)) if aucs else float("nan"))


def _kept_table(score: np.ndarray, r: np.ndarray) -> pd.DataFrame:
    order = np.argsort(-score)  # best first
    rows = []
    for frac in KEEP_FRACTIONS:
        k = max(1, round(frac * len(score)))
        kept = r[order[:k]]
        rows.append(
            {
                "keep_frac": frac,
                "n_kept": k,
                "mean_net_r": float(kept.mean()),
                "win_rate": float((kept > 0).mean()),
                "total_r": float(kept.sum()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/rl/irb_h1_setups.parquet")
    ap.add_argument("--splits", type=int, default=6)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    df = pd.read_parquet(args.data).sort_values("signal_time").reset_index(drop=True)
    feat = [c for c in df.columns if c.startswith("f_")]
    X = df[feat].to_numpy(dtype=float)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    y_win = df["win"].to_numpy(dtype=int)
    r = df["net_r"].to_numpy(dtype=float)

    print(
        f"[data] {len(df):,} setups | {len(feat)} features | "
        f"base mean net-R {r.mean():+.4f} | base win {y_win.mean():.3f}"
    )
    print(f"[cv]   TimeSeriesSplit n_splits={args.splits} (train=past, test=future)\n")

    score, oof_r, auc = _oof_scores(X, y_win, r, args.splits, args.seed, permute=False)
    null_score, null_r, _ = _oof_scores(X, y_win, r, args.splits, args.seed, permute=True)

    print(f"OOF win-AUC (real labels): {auc:.4f}   (0.50 = no signal)\n")

    real_tbl = _kept_table(score, oof_r)
    null_tbl = _kept_table(null_score, null_r)

    print("=== REAL model: kept-set net-R by keep fraction (out-of-fold) ===")
    print(real_tbl.to_string(index=False, float_format=lambda v: f"{v:+.4f}"))
    print("\n=== NULL model (labels shuffled in train): same table ===")
    print(null_tbl.to_string(index=False, float_format=lambda v: f"{v:+.4f}"))

    base = oof_r.mean()
    top_half = float(real_tbl.loc[real_tbl.keep_frac == 0.5, "mean_net_r"].iloc[0])
    null_half = float(null_tbl.loc[null_tbl.keep_frac == 0.5, "mean_net_r"].iloc[0])
    uplift = top_half - base
    null_uplift = null_half - base

    print("\n=== VERDICT ===")
    print(f"base net-R (take all)      : {base:+.4f}")
    print(f"top-50% net-R (real)       : {top_half:+.4f}  (uplift {uplift:+.4f})")
    print(f"top-50% net-R (null)       : {null_half:+.4f}  (uplift {null_uplift:+.4f})")
    signal = (auc > 0.53) and (top_half > 0.02) and (uplift > 2 * abs(null_uplift) + 0.01)
    verdict = (
        "GO — real OOS separability beyond the null."
        if signal
        else "NO-GO — no OOS separability above the permutation null."
    )
    print(f"\n{verdict}")


if __name__ == "__main__":
    main()
