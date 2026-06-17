#!/usr/bin/env python3
"""Backtest task-1040: Markov Regime — full-spec walk-forward build.

This is the spec-complete successor to ``backtest_1035_markov_regime.py``. It
keeps the rigorous no-lookahead walk-forward core (the transition matrix at day
``t`` is built only from labels observed up to ``t``) and adds the behaviours
the 1040 spec calls for that 1035 did not have:

  * **Label self-check** — the rolling-return means of the Bear / Sideways /
    Bull buckets must be strictly monotone (mean_bear < mean_sideways <
    mean_bull). If they are not, the labelling is incoherent and the run stays
    flat for the whole backtest (do not trade).
  * **Corrected stride sampling** — default ``stride = lookback`` (counts only
    non-overlapping windows, killing the manufactured autocorrelation). A
    ``stride = 1`` legacy matrix is also built, for diagnostics ONLY — it never
    drives trades.
  * **Minimum-sample gate** — require ``min_total_transitions`` observed
    transitions overall AND ``min_current_state_transitions`` out of the current
    state's row before trusting the row; otherwise stay flat that bar.
  * **Deadband with asymmetric exposure caps** — ``signal = p_bull - p_bear``;
    above ``+deadband`` target = ``min(signal, max_long_exposure)``, below
    ``-deadband`` target = ``max(signal, max_short_exposure)``, else flat.
  * **Three modes** — ``standalone`` (trade target_exposure directly),
    ``long_only`` (long when signal > deadband else flat, never short), and
    ``filter`` (place no trades; emit a per-bar allow_longs / allow_shorts /
    regime_signal / state / probability frame for an external strategy).
  * **Full report set** — total return, CAGR, Sharpe, Sortino, max drawdown,
    Calmar, win rate, profit factor, turnover.

EXECUTION (spec §"Execution"): the signal is computed at the daily close of day
``t`` and the position is rebalanced at the NEXT daily open (open[t+1]); it is
held until the next rebalance, so it earns the open[t+1] -> open[t+2] move.
Reversals are allowed by default (re-deriving the target each bar flips the
position automatically). Commission/spread/slippage enter as a round-trip cost
in bps charged on the traded exposure delta at each rebalance. When the data has
no ``open`` column the model falls back to a close->close fill and says so.

Primary asset = BTCUSD (the model's recommended crypto use case). Any liquid-FX
daily series works as the control, consistent with prior NovaCore findings.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BULL, SIDE, BEAR = 2, 1, 0
STATE_NAME = {BEAR: "Bear", SIDE: "Sideways", BULL: "Bull"}


# --------------------------------------------------------------------------- #
# Data + labelling
# --------------------------------------------------------------------------- #
def load_daily(path: Path) -> pd.DataFrame:
    """Load OHLC and resample to daily (UTC). Carries ``open`` when available so
    the spec's open-based execution (signal at close → fill next open) can run;
    ``close`` is always present."""
    if path.suffix == ".parquet":
        df = pd.read_parquet(path)
        if "time" in df.columns:
            df = df.set_index("time")
        elif "timestamp" in df.columns:
            df = df.set_index("timestamp")
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


def rolling_return(close: pd.Series, lookback: int) -> np.ndarray:
    """Trailing ``lookback``-bar simple return for each bar (NaN during warmup)."""
    return (close / close.shift(lookback) - 1.0).to_numpy()


def label_regimes(ret: np.ndarray, bull_thr: float, bear_thr: float) -> np.ndarray:
    """Per-bar regime label from the trailing-return array.

    ret >= +bull_thr  -> Bull
    ret <= -bear_thr  -> Bear
    else (defined)    -> Sideways
    NaN               -> -1 (warmup / undefined)
    """
    lab = np.full(ret.shape, -1, dtype=int)
    lab[ret >= bull_thr] = BULL
    lab[ret <= -bear_thr] = BEAR
    defined = ~np.isnan(ret)
    lab[defined & (ret > -bear_thr) & (ret < bull_thr)] = SIDE
    return lab


def label_self_check(ret: np.ndarray, labels: np.ndarray) -> dict:
    """Mean rolling return per label must be strictly monotone Bear<Side<Bull.

    Returns a dict with the three means and a boolean ``ok``. NaN means appear
    when a bucket is empty; an empty bucket fails the check (cannot order it).
    """
    means = {}
    for st in (BEAR, SIDE, BULL):
        m = ret[(labels == st) & ~np.isnan(ret)]
        means[st] = float(m.mean()) if m.size else float("nan")
    ok = not any(np.isnan(v) for v in means.values()) and means[BEAR] < means[SIDE] < means[BULL]
    return {
        "mean_bear": means[BEAR],
        "mean_sideways": means[SIDE],
        "mean_bull": means[BULL],
        "ok": bool(ok),
    }


# --------------------------------------------------------------------------- #
# Transition matrix
# --------------------------------------------------------------------------- #
def transition_matrix(
    labels: np.ndarray, stride: int, stickiness: float, t_limit: int
) -> tuple[np.ndarray, np.ndarray]:
    """3x3 row-stochastic matrix from transitions ``state[i] -> state[i+stride]``.

    Only transitions whose *destination* index ``i + stride <= t_limit`` are
    counted — this is the no-lookahead guarantee (spec: "Only include
    transitions where i + stride <= t"). Diagonal Laplace ``stickiness``
    smoothing is applied to the probability matrix only.

    Returns ``(P, raw_counts)``; ``raw_counts`` are the OBSERVED transitions with
    no prior, so the minimum-sample gate reads real evidence, not the smoothing.
    """
    raw = np.zeros((3, 3))
    # Walk the labelled series in original index space so the i+stride<=t_limit
    # rule is exact. Skip pairs where either endpoint is warmup (-1).
    last_src = min(t_limit - stride, len(labels) - 1 - stride)
    for i in range(0, last_src + 1, stride):
        j = i + stride
        si, sj = labels[i], labels[j]
        if si < 0 or sj < 0:
            continue
        raw[si, sj] += 1.0
    counts = raw.copy()
    counts[np.diag_indices(3)] += stickiness
    row_sums = counts.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1.0
    return counts / row_sums, raw


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #
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
    mode: str = "standalone",
    max_long_exposure: float = 1.0,
    max_short_exposure: float = -1.0,
    min_total_transitions: int = 30,
    min_current_state_transitions: int = 10,
    exec_mode: str = "open",
) -> dict:
    close = daily["close"]
    ret = rolling_return(close, lookback)
    labels = label_regimes(ret, bull_thr, bear_thr)

    # Whole-series label self-check (descriptive monotonicity gate).
    selfcheck = label_self_check(ret, labels)

    # Execution alignment (spec "Execution"). open: position decided at close[t]
    # is filled at open[t+1] and earns open[t+1]->open[t+2]. close: fall back to
    # close[t]->close[t+1] when no open column exists.
    if exec_mode == "open" and "open" in daily.columns:
        op = daily["open"]
        fwd_ret = (op.shift(-2) / op.shift(-1) - 1.0).to_numpy()
    else:
        exec_mode = "close"
        fwd_ret = (close.shift(-1) / close - 1.0).to_numpy()

    n = len(close)
    positions = np.zeros(n)
    signals = np.full(n, np.nan)
    states = np.full(n, -1, dtype=int)
    p_bull = np.full(n, np.nan)
    p_bear = np.full(n, np.nan)
    p_side = np.full(n, np.nan)
    allow_longs = np.zeros(n, dtype=bool)
    allow_shorts = np.zeros(n, dtype=bool)

    start = max(warmup, lookback + 2)
    insufficient = 0

    # Hard self-check gate: incoherent labels -> never trade.
    trade_enabled = selfcheck["ok"]

    for t in range(start, n - 1):
        cur = labels[t]
        if cur < 0:
            continue
        states[t] = cur
        # Walk-forward matrix from transitions with destination index <= t.
        P, raw = transition_matrix(labels, stride, stickiness, t_limit=t)
        p_bull[t] = P[cur, BULL]
        p_bear[t] = P[cur, BEAR]
        p_side[t] = P[cur, SIDE]

        # Minimum-sample gate on OBSERVED transitions.
        if raw.sum() < min_total_transitions or raw[cur].sum() < min_current_state_transitions:
            insufficient += 1
            continue

        signal = P[cur, BULL] - P[cur, BEAR]
        signals[t] = signal
        allow_longs[t] = signal > deadband
        allow_shorts[t] = signal < -deadband

        if not trade_enabled:
            continue  # self-check failed -> stay flat (still records diagnostics)

        # Deadband + asymmetric exposure caps.
        if signal > deadband:
            target = min(signal, max_long_exposure)
        elif signal < -deadband:
            target = max(signal, max_short_exposure)
        else:
            target = 0.0

        if mode == "filter":
            target = 0.0  # filter mode places no trades
        elif mode == "long_only":
            target = target if signal > deadband else 0.0  # never short

        positions[t] = target

    # ----- PnL & cost (round-trip bps on traded exposure delta at rebalance) --
    cost = cost_bps / 1e4
    turns = np.abs(np.diff(np.concatenate([[0.0], positions])))
    gross_daily = np.nan_to_num(positions * fwd_ret)
    net_daily = gross_daily - turns * cost

    active = positions != 0
    n_active = int(active.sum())
    n_trades = int((turns > 1e-12).sum())
    wins = int(((gross_daily > 0) & active).sum())
    turnover = float(turns.sum())

    equity = np.cumprod(1.0 + net_daily)
    final_eq = float(equity[-1]) if equity.size else 1.0
    total_ret = final_eq - 1.0
    years = max(n / 365.25, 1e-9)
    cagr = final_eq ** (1.0 / years) - 1.0 if final_eq > 0 else -1.0

    exposed = net_daily[active]
    ann = np.sqrt(365.0)
    if exposed.size > 1 and exposed.std(ddof=1) > 0:
        sharpe = exposed.mean() / exposed.std(ddof=1) * ann
    else:
        sharpe = 0.0
    # Sortino: downside deviation of the exposed returns (target 0).
    if exposed.size > 1:
        downside = exposed[exposed < 0]
        dd_std = np.sqrt(np.mean(np.square(downside))) if downside.size else 0.0
        sortino = (exposed.mean() / dd_std * ann) if dd_std > 0 else 0.0
    else:
        sortino = 0.0

    peak = np.maximum.accumulate(equity) if equity.size else np.array([1.0])
    max_dd = float(((equity - peak) / peak).min()) if equity.size else 0.0
    calmar = (cagr / abs(max_dd)) if max_dd < 0 else 0.0

    gross_wins = float(gross_daily[gross_daily > 0].sum())
    gross_losses = float(-gross_daily[gross_daily < 0].sum())
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else float("inf")

    result = {
        "mode": mode,
        "exec_mode": exec_mode,
        "trade_enabled": trade_enabled,
        "self_check": selfcheck,
        "bars": n,
        "exposed_days": n_active,
        "exposure_pct": 100.0 * n_active / n if n else 0.0,
        "insufficient_sample_days": insufficient,
        "n_trades": n_trades,
        "turnover": turnover,
        "win_rate_pct": (100.0 * wins / n_active) if n_active else 0.0,
        "profit_factor": profit_factor,
        "total_return_pct": 100.0 * total_ret,
        "cagr_pct": 100.0 * cagr,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_dd_pct": 100.0 * max_dd,
        "calmar": calmar,
        "final_equity": final_eq,
        "mean_signal": float(np.nanmean(signals)) if np.isfinite(signals).any() else float("nan"),
    }

    # Filter-mode per-bar output frame (no trades; for an external strategy).
    if mode == "filter":
        idx = daily.index
        result["filter_frame"] = pd.DataFrame(
            {
                "current_state": [STATE_NAME.get(int(s), "n/a") if s >= 0 else "n/a" for s in states],
                "regime_signal": signals,
                "p_bull": p_bull,
                "p_bear": p_bear,
                "p_sideways": p_side,
                "allow_longs": allow_longs,
                "allow_shorts": allow_shorts,
            },
            index=idx,
        )
    return result


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def fmt(tag: str, m: dict) -> str:
    sc = m["self_check"]
    sc_line = (
        f"  label self-check  : Bear={sc['mean_bear']:+.4f} < Side={sc['mean_sideways']:+.4f}"
        f" < Bull={sc['mean_bull']:+.4f}  -> {'PASS' if sc['ok'] else 'FAIL (flat)'}\n"
    )
    pf = m["profit_factor"]
    pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
    return (
        f"\n=== TASK-1040  Markov Regime ({tag}) ===\n"
        f"  mode / exec       : {m['mode']} / {m['exec_mode']}\n"
        f"{sc_line}"
        f"  trade enabled     : {m['trade_enabled']}\n"
        f"  daily bars        : {m['bars']}\n"
        f"  exposed days      : {m['exposed_days']} ({m['exposure_pct']:.1f}% of bars)\n"
        f"  flat (low sample) : {m['insufficient_sample_days']} bars below min-transition gate\n"
        f"  trades (rebal.)   : {m['n_trades']}\n"
        f"  turnover          : {m['turnover']:.2f}\n"
        f"  win rate          : {m['win_rate_pct']:.1f}%\n"
        f"  profit factor     : {pf_s}\n"
        f"  total return      : {m['total_return_pct']:+.1f}%\n"
        f"  CAGR              : {m['cagr_pct']:+.2f}%\n"
        f"  Sharpe (ann.)     : {m['sharpe']:+.2f}\n"
        f"  Sortino (ann.)    : {m['sortino']:+.2f}\n"
        f"  max drawdown      : {m['max_dd_pct']:.1f}%\n"
        f"  Calmar            : {m['calmar']:+.2f}\n"
        f"  final equity (x)  : {m['final_equity']:.3f}\n"
        f"  mean signal       : {m['mean_signal']:+.3f}\n"
    )


def diagnostic_strides(daily: pd.DataFrame, lookback: int, bull_thr: float, bear_thr: float, stickiness: float) -> str:
    """Build the corrected (stride=lookback) and legacy (stride=1) full-series
    matrices for diagnostics only — neither drives trades."""
    ret = rolling_return(daily["close"], lookback)
    labels = label_regimes(ret, bull_thr, bear_thr)
    t_limit = len(labels) - 1
    out = ["\n--- transition-matrix diagnostics (full series, NOT walk-forward) ---"]
    for tag, stride in (("corrected stride=lookback", lookback), ("legacy stride=1", 1)):
        _, raw = transition_matrix(labels, stride, stickiness, t_limit)
        diag = (raw[np.diag_indices(3)].sum() / raw.sum()) if raw.sum() else float("nan")
        out.append(f"  {tag:<26} total={int(raw.sum()):>5}  mean-diagonal stickiness={diag * 100:.1f}%")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--symbol", default="ASSET")
    ap.add_argument("--lookback", type=int, default=20)
    ap.add_argument("--bull-thr", type=float, default=0.05)
    ap.add_argument("--bear-thr", type=float, default=0.05)
    ap.add_argument("--stride", type=int, default=0, help="0 => stride = lookback (corrected default)")
    ap.add_argument("--stickiness", type=float, default=2.0)
    ap.add_argument("--deadband", type=float, default=0.10)
    ap.add_argument("--warmup", type=int, default=120)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--mode", choices=["standalone", "long_only", "filter"], default="standalone")
    ap.add_argument("--max-long-exposure", type=float, default=1.0)
    ap.add_argument("--max-short-exposure", type=float, default=-1.0)
    ap.add_argument("--min-total-transitions", type=int, default=30)
    ap.add_argument("--min-current-state-transitions", type=int, default=10)
    ap.add_argument("--exec", dest="exec_mode", choices=["open", "close"], default="open")
    ap.add_argument("--filter-out", default="", help="CSV path for filter-mode per-bar frame")
    args = ap.parse_args()

    stride = args.stride or args.lookback
    daily = load_daily(Path(args.data))
    print(
        f"Loaded {len(daily)} daily bars ({args.symbol}) "
        f"{daily.index[0].date()} -> {daily.index[-1].date()}  "
        f"[mode={args.mode}, exec={args.exec_mode}, stride={stride}, open_col={'open' in daily.columns}]"
    )

    common = dict(
        lookback=args.lookback,
        bull_thr=args.bull_thr,
        bear_thr=args.bear_thr,
        stride=stride,
        stickiness=args.stickiness,
        deadband=args.deadband,
        warmup=args.warmup,
        mode=args.mode,
        max_long_exposure=args.max_long_exposure,
        max_short_exposure=args.max_short_exposure,
        min_total_transitions=args.min_total_transitions,
        min_current_state_transitions=args.min_current_state_transitions,
        exec_mode=args.exec_mode,
    )

    print(diagnostic_strides(daily, args.lookback, args.bull_thr, args.bear_thr, args.stickiness))

    if args.mode == "filter":
        res = run_backtest(daily, cost_bps=0.0, **common)
        frame = res.pop("filter_frame")
        print(fmt("FILTER", res))
        live = frame.dropna(subset=["regime_signal"])
        print(f"  filter rows w/ signal : {len(live)}")
        print(f"  allow_longs true      : {int(live['allow_longs'].sum())}")
        print(f"  allow_shorts true     : {int(live['allow_shorts'].sum())}")
        if not live.empty:
            last = live.iloc[-1]
            print(
                f"  latest                : {live.index[-1].date()}  state={last['current_state']}"
                f"  signal={last['regime_signal']:+.3f}"
                f"  pB={last['p_bull']:.2f}/pS={last['p_sideways']:.2f}/pBr={last['p_bear']:.2f}"
            )
        if args.filter_out:
            frame.to_csv(args.filter_out)
            print(f"  wrote filter frame    : {args.filter_out}")
    else:
        gross = run_backtest(daily, cost_bps=0.0, **common)
        net = run_backtest(daily, cost_bps=args.cost_bps, **common)
        print(fmt("GROSS", gross))
        print(fmt(f"NET @ {args.cost_bps:.0f}bps/rebal", net))

    bh = daily["close"].iloc[-1] / daily["close"].iloc[0] - 1.0
    print(f"  [benchmark] buy & hold total return: {100 * bh:+.1f}%\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
