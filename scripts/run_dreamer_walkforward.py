#!/usr/bin/env python3
"""Run the corrected DreamerV3 gold-bot through the walk-forward orchestrator.

This is the end-to-end entrypoint tasks 1063/1065 flagged as missing: it builds
the H1-decision / M1-execution ``DecisionSeries`` from the raw XAUUSD M1 feed,
wires the torch ``train_fn``/``evaluate_fn`` adapters into
``orchestrator.run_orchestration``, runs the fixed-size sliding walk-forward
with the cross-fold consistency gate, and (only if the gate passes) reads the
held-out test window exactly once.

Bounded by design: defaults train a small number of folds for a few thousand
steps each on CPU — enough to get the FIRST real read through the corrected
pipeline, not a converged 5M-step model. Use --start / fold sizes / steps to
scale. Honest-negative outcome (gate fails -> no test read) is expected and fine.

Usage:
    PYTHONPATH=. python3 scripts/run_dreamer_walkforward.py \
        --start 2021-01-01 --steps-per-fold 2500 --out runs/dreamer_wf
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from novatrade.research.dreamer import config, data, features
from novatrade.research.dreamer.execution import Bar
from novatrade.research.dreamer.orchestrator import DecisionSeries, run_orchestration
from novatrade.research.dreamer.torch_adapter import TrainFnConfig, make_train_fn

H1_DUR = pd.Timedelta(hours=1)


def build_decision_series(
    m1_path: str,
    start: pd.Timestamp | None,
    end: pd.Timestamp | None,
    log=print,
) -> DecisionSeries:
    """Build the H1 decision grid + M1 execution paths from the M1 feed.

    No-lookahead throughout: H1 decision features are the latest M5 152-feature
    row whose *close* is <= the H1 close (backward merge_asof on close-time keys);
    ATR is Wilder ATR on closed H1 bars; ``m1_paths[i]`` holds the M1 bars of the
    NEXT hour (the window an entry at H1 bar i's close runs through).
    """
    log(f"[io] loading M1 {m1_path} ...")
    m1 = data.load_m1(m1_path)
    if start is not None:
        m1 = m1.loc[m1.index >= start]
    if end is not None:
        m1 = m1.loc[m1.index < end]
    log(f"[io] M1 rows={len(m1)} ({m1.index[0]} -> {m1.index[-1]})")

    # 152-feature matrix on the M5 base grid (committed, leakage-tested builder).
    matrix = features.build_features(m1)  # index = M5 open labels
    # Re-key by M5 close time WITHOUT copying the (large) feature blocks.
    feat_by_close = matrix.set_axis(matrix.index + config.TF_DURATION[config.BASE_TF], axis=0).sort_index()

    # H1 decision bars (open-labelled), Wilder ATR on H1 close.
    h1 = data.resample_ohlc(m1, "H1")
    atr = features._atr(h1["high"], h1["low"], h1["close"], config.DEFAULT.atr_period)
    h1_close_ts = h1.index + H1_DUR  # the moment each H1 bar's OHLC is knowable

    decision = pd.DataFrame(
        {"decision_close": h1["close"].to_numpy(), "atr": atr.to_numpy()},
        index=h1_close_ts,
    ).sort_index()

    # Align 152 features onto H1 close timestamps with NO lookahead.
    aligned = pd.merge_asof(decision, feat_by_close, left_index=True, right_index=True, direction="backward")
    aligned = aligned.dropna(how="any")
    feature_cols = list(matrix.columns)
    feat_df = aligned[feature_cols]
    decision_close = aligned["decision_close"].to_numpy(dtype="float64")
    atr_arr = aligned["atr"].to_numpy(dtype="float64")
    kept_ts = aligned.index

    # M1 execution paths: bar at t belongs to the window after H1 close
    # (t - 1min).floor('1h')  -> covers (tc, tc+1h].
    log("[io] bucketing M1 execution paths ...")
    keys = (m1.index - pd.Timedelta(minutes=1)).floor("1h")
    highs, lows, closes = m1["high"].to_numpy(), m1["low"].to_numpy(), m1["close"].to_numpy()
    paths: dict[pd.Timestamp, list[Bar]] = {}
    for k, hi, lo, cl in zip(keys, highs, lows, closes, strict=True):
        paths.setdefault(k, []).append(Bar(high=float(hi), low=float(lo), close=float(cl)))

    m1_paths = [paths.get(ts, []) for ts in kept_ts]
    del m1, matrix, feat_by_close, aligned, paths, highs, lows, closes, keys  # free before training
    series = DecisionSeries(
        features=feat_df,
        decision_close=decision_close,
        atr=atr_arr,
        m1_paths=m1_paths,
    )
    log(
        f"[io] decision bars={len(series)} ({kept_ts[0]} -> {kept_ts[-1]}); "
        f"empty paths={sum(1 for p in m1_paths if not p)}"
    )
    return series


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--m1", default=str(data.DEFAULT_M1_PATH))
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--train-size", type=int, default=6000)
    ap.add_argument("--val-size", type=int, default=2000)
    ap.add_argument("--test-size", type=int, default=2000)
    ap.add_argument("--step", type=int, default=4000)
    ap.add_argument("--steps-per-fold", type=int, default=2500)
    ap.add_argument("--seq-len", type=int, default=32)
    ap.add_argument("--n-checkpoints", type=int, default=3)
    ap.add_argument("--max-folds", type=int, default=0, help="0 = all generated folds")
    ap.add_argument("--out", default="runs/dreamer_wf")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp(args.start) if args.start else None
    endt = pd.Timestamp(args.end) if args.end else None
    cfg = config.DEFAULT
    t0 = time.time()

    series = build_decision_series(args.m1, start, endt)

    # Optionally cap the series length so only --max-folds folds are generated.
    if args.max_folds > 0:
        capped = args.train_size + args.val_size + args.test_size + (args.max_folds - 1) * args.step
        if capped < len(series):
            series = series.slice(0, capped)
            print(f"[wf] capped series to {len(series)} bars for {args.max_folds} folds")

    tcfg = TrainFnConfig(
        steps_per_fold=args.steps_per_fold,
        seq_len=args.seq_len,
        n_checkpoints=args.n_checkpoints,
    )
    fold_no = {"i": 0}

    def logged_print(*a):
        print(*a, flush=True)

    train_fn, evaluate_fn = make_train_fn(cfg, tcfg, log=logged_print)

    def wrapped_train_fn(fold_data):
        fold_no["i"] += 1
        print(
            f"\n[fold {fold_no['i']}] train={len(fold_data.train)} val={len(fold_data.val)} "
            f"test={len(fold_data.test)} | {args.steps_per_fold} steps",
            flush=True,
        )
        ft0 = time.time()
        res = train_fn(fold_data)
        print(f"[fold {fold_no['i']}] done val_R={res.validation_profit:+.3f} ({time.time() - ft0:.0f}s)", flush=True)
        return res

    print(
        f"\n[wf] running walk-forward: train={args.train_size} val={args.val_size} "
        f"test={args.test_size} step={args.step}",
        flush=True,
    )
    result = run_orchestration(
        series,
        train_fn=wrapped_train_fn,
        evaluate_fn=evaluate_fn,
        train_size=args.train_size,
        val_size=args.val_size,
        test_size=args.test_size,
        step=args.step,
        config=cfg,
    )

    c = result.consistency
    print("\n=== WALK-FORWARD RESULT ===", flush=True)
    print(
        f"  folds: {c.total_folds} | profitable: {c.profitable_folds} "
        f"({c.profitable_ratio:.0%}) | required: {c.required_ratio:.0%} -> "
        f"gate {'PASSED' if c.passed else 'FAILED'}",
        flush=True,
    )
    print(f"  fold validation R: {[round(p, 3) for p in result.walk_forward.fold_profits]}", flush=True)

    test_summary = None
    if result.test_report is not None:
        tr = result.test_report
        n_trades = len(result.test_trades or [])
        print(
            f"\n  TEST READ (gate passed): trades={n_trades} "
            f"total_R={tr.total_return:+.3f} maxDD={tr.max_drawdown:.3f}",
            flush=True,
        )
        print(f"  exit reasons: {tr.exit_reason_breakdown}", flush=True)
        print(f"  champion outside good region: {result.champion_outside_good_region}", flush=True)
        test_summary = {"n_trades": n_trades, **asdict(tr)}
    else:
        print("\n  NO TEST READ — consistency gate failed; edge not proven. Test data was never touched.", flush=True)

    report = {
        "params": vars(args),
        "wall_seconds": round(time.time() - t0, 1),
        "consistency": asdict(c),
        "fold_validation_R": [float(p) for p in result.walk_forward.fold_profits],
        "has_champion": result.has_champion,
        "champion_outside_good_region": result.champion_outside_good_region,
        "test_report": test_summary,
        "note": config.TEST_DATA_AVAILABLE_NOTE,
    }
    (out / "wf_report.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"\n[done] wrote {out / 'wf_report.json'} ({report['wall_seconds']}s)", flush=True)


if __name__ == "__main__":
    main()
