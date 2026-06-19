"""Phase 2b — permutation test to harden the separability verdict.

The single-seed null in the probe is noisy. Here we build the null *distribution*
by shuffling training labels across many seeds, then ask: where does the real
model's out-of-fold edge fall within that null? p = fraction of null runs that
match or beat the real model. A real edge should sit in the right tail (p small).
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from scripts.probe_irb_rl_separability import _kept_table, _oof_scores


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/rl/irb_h1_setups.parquet")
    ap.add_argument("--splits", type=int, default=6)
    ap.add_argument("--n-perm", type=int, default=40)
    args = ap.parse_args()

    df = pd.read_parquet(args.data).sort_values("signal_time").reset_index(drop=True)
    feat = [c for c in df.columns if c.startswith("f_")]
    X = np.nan_to_num(df[feat].to_numpy(float), nan=0.0, posinf=0.0, neginf=0.0)
    y = df["win"].to_numpy(int)
    r = df["net_r"].to_numpy(float)

    # Real model (average over a few seeds; deterministic CV so variance is tiny)
    real_half, real_auc = [], []
    for s in range(5):
        score, oof_r, auc = _oof_scores(X, y, r, args.splits, s, permute=False)
        real_half.append(float(_kept_table(score, oof_r).query("keep_frac == 0.5").mean_net_r.iloc[0]))
        real_auc.append(auc)
    real_half_m = float(np.mean(real_half))
    real_auc_m = float(np.mean(real_auc))

    # Null distribution (top-50% net-R is the economic metric; null AUC unneeded)
    null_vals: list[float] = []
    for s in range(args.n_perm):
        score, oof_r, _ = _oof_scores(X, y, r, args.splits, 1000 + s, permute=True)
        null_vals.append(float(_kept_table(score, oof_r).query("keep_frac == 0.5").mean_net_r.iloc[0]))
    null_half = np.array(null_vals)

    p_half = float((null_half >= real_half_m).mean())

    print(f"[data] {len(df):,} setups | base net-R {r.mean():+.4f}")
    print("\nReal model (mean of 5 seeds):")
    print(f"  OOF win-AUC        : {real_auc_m:.4f}")
    print(f"  top-50% net-R      : {real_half_m:+.4f}")
    print(f"\nNull distribution (labels shuffled, n={args.n_perm}):")
    print(f"  top-50% net-R mean : {null_half.mean():+.4f}")
    print(f"  top-50% net-R std  : {null_half.std():+.4f}")
    print(f"  top-50% net-R p95  : {np.percentile(null_half, 95):+.4f}")
    print(f"\np-value (null top-50% >= real top-50%): {p_half:.3f}")
    verdict = (
        "GO — real edge beats the null at p<0.05."
        if p_half < 0.05 and real_auc_m > 0.53
        else "NO-GO — real model is inside the null distribution. No tradeable separability."
    )
    print(f"\n{verdict}")


if __name__ == "__main__":
    main()
