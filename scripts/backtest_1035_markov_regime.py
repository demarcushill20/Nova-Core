#!/usr/bin/env python3
"""Backtest task-1035: Markov Regime 2.0 -- Bull / Bear / Sideways.

NOT a normal indicator-entry strategy. Instead of EMA/RSI/MACD, this model:
  1. Labels each daily bar's regime from its rolling N-day return:
        return >= +bull_thr  -> Bull (2)
        return <= -bear_thr  -> Bear (0)
        else                 -> Sideways (1)
  2. Builds a 3x3 regime transition matrix P[i][j] = P(next=j | now=i),
     with Laplace ("stickiness") smoothing on the diagonal.
  3. Directional signal from the *current* state's row:
        edge = P(next=Bull) - P(next=Bear)
     Long if edge > +deadband, Short if edge < -deadband, else flat.

NO-LOOKAHEAD WALK-FORWARD: at day t the transition matrix is rebuilt from
labels observed strictly up to t, the signal sizes the position held for day
t+1, and PnL is the t->t+1 close-to-close move. This is the rigorous standard
the NovaCore research log enforces (expanding window, out-of-sample by design).

Run NET (with cost on every position change) and GROSS (zero cost) to isolate
whether the regime edge survives realistic friction.

Designed for the DAILY timeframe / swing-allocation horizon, per the strategy
spec. Primary asset = BTCUSD (the model's recommended crypto use case);
EURUSD daily included as the liquid-FX control consistent with prior findings.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BULL, SIDE, BEAR = 2, 1, 0


def load_daily(path: Path) -> pd.DataFrame:
    """Load OHLC and resample to daily (UTC). Carries ``open`` when available so
    the open-based execution model (spec §18: signal at close → fill next open)
    can be tested; ``close`` is always present."""
    if path.suffix == ".parquet":
        df = pd.read_parquet(path)
        if "time" in df.columns:
            df = df.set_index("time")
        df.index = pd.to_datetime(df.index, utc=True)
    else:
        df = pd.read_csv(path)
        ts = "timestamp" if "timestamp" in df.columns else "time"
        df[ts] = pd.to_datetime(df[ts], utc=True)
        df = df.set_index(ts)
    cols = {"close": df["close"].resample("1D").last()}
    if "open" in df.columns:
        cols["open"] = df["open"].resample("1D").first()
    return pd.DataFrame(cols).dropna()


def label_regimes(close: pd.Series, lookback: int, bull_thr: float, bear_thr: float) -> np.ndarray:
    """Per-bar regime label from the trailing ``lookback``-bar return."""
    ret = close / close.shift(lookback) - 1.0
    lab = np.full(len(close), -1, dtype=int)  # -1 = warmup / undefined
    arr = ret.to_numpy()
    lab[arr >= bull_thr] = BULL
    lab[arr <= -bear_thr] = BEAR
    defined = ~np.isnan(arr)
    lab[defined & (arr > -bear_thr) & (arr < bull_thr)] = SIDE
    return lab


def transition_matrix(labels: np.ndarray, stride: int, stickiness: float) -> tuple[np.ndarray, np.ndarray]:
    """3x3 row-stochastic matrix with diagonal Laplace smoothing.

    ``stride`` sub-samples the (state_t -> state_{t+1}) pairs to dampen the
    overlapping-window autocorrelation that rolling-return labels induce.

    Returns ``(P, raw_counts)`` where ``raw_counts`` holds the *observed*
    transition counts only (no smoothing prior). The caller uses ``raw_counts``
    to enforce the spec §23/§24 minimum-sample-size checks — gating on real
    evidence, never on the Laplace prior that would otherwise mask a thin row.
    """
    raw = np.zeros((3, 3))
    valid = labels[labels >= 0]
    for k in range(0, len(valid) - 1, stride):
        i, j = valid[k], valid[k + 1]
        raw[i, j] += 1.0
    counts = raw.copy()
    counts[np.diag_indices(3)] += stickiness  # prior favouring regime persistence
    row_sums = counts.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1.0
    return counts / row_sums, raw


def run_backtest(
    daily: pd.DataFrame,
    *,
    lookback: int,
    bull_thr: float,
    bear_thr: float,
    stride: int,
    stickiness: float,
    deadband: float,
    warmup: int,
    cost_bps: float,
    sizing: str = "binary",
    max_edge: float = 1.0,
    exec_mode: str = "close",
    min_total_transitions: int = 30,
    min_current_state_transitions: int = 10,
) -> dict:
    close = daily["close"]
    labels = label_regimes(close, lookback, bull_thr, bear_thr)

    # Execution model (spec §18). close: position decided at close[t] earns the
    # close[t]->close[t+1] move (the original, reproducible default). open: the
    # signal at close[t] is filled at open[t+1] and earns open[t+1]->open[t+2],
    # the spec's "signal at close, trade at next open, hold to next rebalance".
    if exec_mode == "open" and "open" in daily.columns:
        op = daily["open"]
        fwd_ret = (op.shift(-2) / op.shift(-1) - 1.0).to_numpy()
    else:
        exec_mode = "close"  # fall back if no open column
        fwd_ret = (close.shift(-1) / close - 1.0).to_numpy()  # t -> t+1 return

    n = len(close)
    positions = np.zeros(n)
    edges = np.full(n, np.nan)
    start = max(warmup, lookback + 2)
    insufficient = 0  # bars forced flat by the §23/§24 sample-size gate

    for t in range(start, n - 1):
        if labels[t] < 0:
            continue
        cur = labels[t]
        # Walk-forward matrix from labels strictly <= t (no lookahead, spec §24).
        P, raw = transition_matrix(labels[: t + 1], stride, stickiness)
        # §23/§24 minimum-sample-size checks: require enough *observed*
        # transitions overall AND out of the current state's row before the
        # probability row is trusted. Too-thin evidence -> stay flat, never
        # trade on the smoothing prior alone.
        if raw.sum() < min_total_transitions or raw[cur].sum() < min_current_state_transitions:
            insufficient += 1
            continue
        # Find the current state's probability row, then the directional edge.
        edge = P[cur, BULL] - P[cur, BEAR]
        edges[t] = edge
        # §19 exit logic: inside the deadband -> flat (exit). Outside ->
        # directional. Re-deriving the target each bar means a sign-flip closes
        # and reverses automatically, and a return inside the deadband exits.
        if abs(edge) > deadband:
            if sizing == "linear":
                # Spec §18 fractional target exposure: edge=+0.45 -> 45% long,
                # clipped to [-1, 1]. Maintains exposure proportional to edge.
                positions[t] = float(np.clip(edge / max_edge, -1.0, 1.0))
            else:  # binary: full ±1 (original behaviour, reproducible)
                positions[t] = 1.0 if edge > 0 else -1.0
        # else flat (0)

    # PnL: position held on day t earns the t->t+1 move; cost on position change.
    cost = cost_bps / 1e4
    pos = positions
    turns = np.abs(np.diff(np.concatenate([[0.0], pos])))
    gross_daily = np.nan_to_num(pos * fwd_ret)
    net_daily = gross_daily - turns * cost

    active = pos != 0
    n_days = int(active.sum())
    n_trades = int((turns > 0).sum())
    wins = int(((gross_daily > 0) & active).sum())

    equity = np.cumprod(1.0 + net_daily)
    total_ret = equity[-1] - 1.0
    years = max((n) / 365.25, 1e-9)
    cagr = (equity[-1]) ** (1.0 / years) - 1.0 if equity[-1] > 0 else -1.0

    # annualised Sharpe on the days we are exposed (daily -> *sqrt(365))
    exposed = net_daily[active]
    if exposed.size > 1 and exposed.std(ddof=1) > 0:
        sharpe = exposed.mean() / exposed.std(ddof=1) * np.sqrt(365.0)
    else:
        sharpe = 0.0

    peak = np.maximum.accumulate(equity)
    max_dd = float(((equity - peak) / peak).min()) if equity.size else 0.0

    return {
        "bars": n,
        "exposed_days": n_days,
        "exposure_pct": 100.0 * n_days / n,
        "insufficient_sample_days": insufficient,
        "n_trades": n_trades,
        "hit_rate_pct": (100.0 * wins / n_days) if n_days else 0.0,
        "total_return_pct": 100.0 * total_ret,
        "cagr_pct": 100.0 * cagr,
        "sharpe": sharpe,
        "max_dd_pct": 100.0 * max_dd,
        "final_equity": float(equity[-1]) if equity.size else 1.0,
        "mean_edge": float(np.nanmean(edges)),
        "sizing": sizing,
        "exec_mode": exec_mode,
        "mean_abs_pos": float(np.abs(pos[pos != 0]).mean()) if (pos != 0).any() else 0.0,
    }


def fmt(tag: str, m: dict) -> str:
    return (
        f"\n=== TASK-1035  Markov Regime 2.0 ({tag}) ===\n"
        f"  sizing / exec     : {m['sizing']} / {m['exec_mode']} (mean |pos|={m['mean_abs_pos']:.3f})\n"
        f"  daily bars        : {m['bars']}\n"
        f"  exposed days      : {m['exposed_days']} ({m['exposure_pct']:.1f}% of bars)\n"
        f"  flat (low sample) : {m['insufficient_sample_days']} bars below min-transition gate\n"
        f"  trades (flips)    : {m['n_trades']}\n"
        f"  hit rate          : {m['hit_rate_pct']:.1f}%\n"
        f"  total return      : {m['total_return_pct']:+.1f}%\n"
        f"  CAGR              : {m['cagr_pct']:+.2f}%\n"
        f"  Sharpe (ann.)     : {m['sharpe']:+.2f}\n"
        f"  max drawdown      : {m['max_dd_pct']:.1f}%\n"
        f"  final equity (x)  : {m['final_equity']:.3f}\n"
        f"  mean signal edge  : {m['mean_edge']:+.3f}\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--symbol", default="ASSET")
    ap.add_argument("--lookback", type=int, default=20)
    ap.add_argument("--bull-thr", type=float, default=0.05)
    ap.add_argument("--bear-thr", type=float, default=0.05)
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--stickiness", type=float, default=2.0)
    ap.add_argument("--deadband", type=float, default=0.10)
    ap.add_argument("--warmup", type=int, default=120)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument(
        "--sizing",
        choices=["binary", "linear"],
        default="binary",
        help="binary=full +/-1 (original); linear=target exposure proportional to edge (spec §18)",
    )
    ap.add_argument("--max-edge", type=float, default=1.0, help="edge that maps to 100%% exposure under linear sizing")
    ap.add_argument(
        "--min-total-transitions",
        type=int,
        default=30,
        help="spec §23: min observed transitions in the whole matrix before trusting it (else flat)",
    )
    ap.add_argument(
        "--min-current-state-transitions",
        type=int,
        default=10,
        help="spec §23: min observed transitions out of the current state's row before trading it (else flat)",
    )
    ap.add_argument(
        "--exec",
        dest="exec_mode",
        choices=["close", "open"],
        default="close",
        help="close=close->close (original); open=signal at close, fill next open (spec §18)",
    )
    args = ap.parse_args()

    daily = load_daily(Path(args.data))
    print(
        f"Loaded {len(daily)} daily bars ({args.symbol}) "
        f"{daily.index[0].date()} -> {daily.index[-1].date()}  "
        f"[sizing={args.sizing}, exec={args.exec_mode}, open_col={'open' in daily.columns}]"
    )

    common = dict(
        lookback=args.lookback,
        bull_thr=args.bull_thr,
        bear_thr=args.bear_thr,
        stride=args.stride,
        stickiness=args.stickiness,
        deadband=args.deadband,
        warmup=args.warmup,
        sizing=args.sizing,
        max_edge=args.max_edge,
        exec_mode=args.exec_mode,
        min_total_transitions=args.min_total_transitions,
        min_current_state_transitions=args.min_current_state_transitions,
    )
    gross = run_backtest(daily, cost_bps=0.0, **common)
    net = run_backtest(daily, cost_bps=args.cost_bps, **common)
    print(fmt("GROSS", gross))
    print(fmt(f"NET @ {args.cost_bps:.0f}bps/flip", net))

    # buy-and-hold benchmark
    bh = daily["close"].iloc[-1] / daily["close"].iloc[0] - 1.0
    print(f"  [benchmark] buy & hold total return: {100 * bh:+.1f}%\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
