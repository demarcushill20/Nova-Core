"""Phase 2 (exit-manager) — robustness teardown of the soft GO.

The base probe gave a GO, but two effects could be faking it:
  (1) the OOF window bleeds under the fixed policy, so *any* early exit helps
      mechanically (the permutation null already shows this);
  (2) the label `close_now > hold` is a function of current unrealised-R, so a
      model fed s_unreal_r gets a high AUC for free — "lock in profit" is a fixed
      rule, not an RL edge.

This script decides whether there is *learnable market-context* exit signal by
comparing four OOF policies on identical time-ordered folds (threshold tuned on
TRAIN only for each):

  A. fixed base (ride IRB's exits)
  B. simple rule: close when s_unreal_r > k   (k tuned on train) — no ML
  C. GBM on PnL-state + market features (the full model)
  D. GBM on MARKET-CONTEXT features ONLY (PnL-state removed)

If D ~ base+null (collapses), there is no market-readable exit edge — the result
is just PnL-locking, which is a fixed-rule re-tune (gated by no-change-without-
backtest), not RL. If C ~ B, ML adds nothing over a one-line rule.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import TimeSeriesSplit

from scripts.probe_irb_rl_exit import _simulate_policy, build_snapshots

PROB_GRID = (0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75)
K_GRID = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0)


def _best_thresh(train_df, scores, grid):
    best_t, best_r = grid[0], -1e9
    for t in grid:
        r = _simulate_policy(train_df, scores, t).mean()
        if r > best_r:
            best_r, best_t = r, t
    return best_t


def cv_policy(snap_df, trades, scorer, grid, n_splits, permute, seed):
    """scorer(Xtr,ytr,Xte,seed,tr_df,te_df) -> (train_scores, test_scores)."""
    tscv = TimeSeriesSplit(n_splits=n_splits)
    rng = np.random.default_rng(seed)
    pol_r, base_r = [], []
    eb = snap_df["entry_bar"]
    for tr_t, te_t in tscv.split(trades):
        tr_trades, te_trades = set(trades[tr_t]), set(trades[te_t])
        tr_mask = eb.isin(tr_trades).to_numpy()
        te_mask = eb.isin(te_trades).to_numpy()
        tr_df, te_df = snap_df[tr_mask], snap_df[te_mask]
        s_tr, s_te = scorer(tr_df, te_df, rng if permute else None, seed)
        thr = _best_thresh(tr_df, s_tr, grid)
        pol_r.append(_simulate_policy(te_df, s_te, thr).mean())
        base_r.append(te_df.groupby("entry_bar")["r_hold"].first().to_numpy().mean())
    return float(np.mean(pol_r)), float(np.mean(base_r))


def make_gbm_scorer(feat_cols):
    def scorer(tr_df, te_df, perm_rng, seed):
        Xtr = np.nan_to_num(tr_df[feat_cols].to_numpy(float), nan=0.0, posinf=0.0, neginf=0.0)
        Xte = np.nan_to_num(te_df[feat_cols].to_numpy(float), nan=0.0, posinf=0.0, neginf=0.0)
        ytr = tr_df["label_close"].to_numpy(int)
        if perm_rng is not None:
            ytr = ytr[perm_rng.permutation(len(ytr))]
        if len(np.unique(ytr)) < 2:
            return np.full(len(tr_df), 0.5), np.full(len(te_df), 0.5)
        clf = GradientBoostingClassifier(
            n_estimators=120, max_depth=2, learning_rate=0.03, subsample=0.8, random_state=seed
        )
        clf.fit(Xtr, ytr)
        return clf.predict_proba(Xtr)[:, 1], clf.predict_proba(Xte)[:, 1]

    return scorer


def simple_scorer(tr_df, te_df, perm_rng, seed):
    # no ML: score = current unrealised R (permutation N/A — it's not trained)
    return tr_df["s_unreal_r"].to_numpy(float), te_df["s_unreal_r"].to_numpy(float)


def _report(name, snap_df, trades, scorer, grid, n_splits, n_perm, trainable):
    pol, base = cv_policy(snap_df, trades, scorer, grid, n_splits, permute=False, seed=0)
    line = f"{name:<34} net-R {pol:+.4f}  uplift {pol - base:+.4f}"
    if trainable and n_perm:
        null = np.array([cv_policy(snap_df, trades, scorer, grid, n_splits, True, 1000 + s)[0] for s in range(n_perm)])
        p = float(((null - base) >= (pol - base)).mean())
        line += f"  | null uplift mean {null.mean() - base:+.4f} p95 {np.percentile(null, 95) - base:+.4f}  p={p:.3f}"
    print(line)
    return pol, base


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--m1", default="data/candles/EURUSD_M1.csv")
    ap.add_argument("--splits", type=int, default=6)
    ap.add_argument("--n-perm", type=int, default=20)
    args = ap.parse_args()

    snap_df, feat_cols = build_snapshots(Path(args.m1), "60min", 0.5)
    snap_df = snap_df.sort_values(["entry_bar", "bar"]).reset_index(drop=True)
    trades = snap_df["entry_bar"].drop_duplicates().to_numpy()

    pnl_state = ["s_unreal_r", "s_fav_r", "s_adv_r"]
    market_cols = [c for c in feat_cols if c not in pnl_state]  # market context + bars + side

    base = snap_df.groupby("entry_bar")["r_hold"].first().mean()
    print(f"\n[snapshots] {len(snap_df):,} bars over {len(trades):,} trades | full-sample base net-R {base:+.4f}")
    print(f"[features] full={len(feat_cols)}  market-only={len(market_cols)} (dropped {pnl_state})\n")
    print("=== OOF policy comparison (threshold tuned on train) ===")

    _report("A. fixed base", snap_df, trades, simple_scorer, (1e9,), args.splits, 0, False)
    _report("B. simple rule (unreal_r > k)", snap_df, trades, simple_scorer, K_GRID, args.splits, 0, False)
    _report(
        "C. GBM full (PnL+market)",
        snap_df,
        trades,
        make_gbm_scorer(feat_cols),
        PROB_GRID,
        args.splits,
        args.n_perm,
        True,
    )
    _report(
        "D. GBM market-context ONLY",
        snap_df,
        trades,
        make_gbm_scorer(market_cols),
        PROB_GRID,
        args.splits,
        args.n_perm,
        True,
    )

    print("\nRead: if D's uplift sits inside its null (p>0.05), there is no learnable")
    print("market-context exit edge — only PnL-locking, which is a fixed-rule re-tune.")


if __name__ == "__main__":
    main()
