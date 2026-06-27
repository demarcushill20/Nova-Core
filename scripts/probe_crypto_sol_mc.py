#!/usr/bin/env python3
"""task-1093 glue probe — crypto acquire -> 60/40 split -> engine -> Monte Carlo.

Wires the two new bounded pieces (``crypto_fetcher`` + ``split_by_ratio``) into
the exact workflow the source transcript describes:

    1. ACQUIRE  — free public exchange API (Binance) -> SOL/USDT 1h candles
    2. SPLIT    — reserve 60% train, 40% validation (chronological block)
    3. ENGINE   — a tiny vectorized SMA-cross baseline (stand-in for the
                  parity-validated vbt adapter; see note below)
    4. MONTE    — block-bootstrap the validation returns N times; a run that
       CARLO     stays net-positive counts as a PASS. Report the pass rate.

This is a *demonstration* probe, kept self-contained so it runs without the
heavy internal engine and degrades gracefully offline (synthetic geometric
random walk if the exchange is unreachable). For a production verdict, route
the fetched candles through the validated stack instead:

    novatrade/backtest/cross_validation/vectorbt_adapter.py   (engine, parity-checked)
    novatrade/evaluation/stage_c.py::monte_carlo_gate          (trade-order MC + DD gate)
    novatrade/research/autoresearch/gauntlet.py                (DSR + sealed hold-out)

NOTE on MC method: the source resamples the *price series* (what this script
does, via block bootstrap). ``monte_carlo_gate`` shuffles the *trade P&L
sequence*. They test different robustness properties — use both.

Usage:
    python3 scripts/probe_crypto_sol_mc.py --symbol SOL/USDT --tf 1h --bars 2000
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np

# Make the repo root importable when run as ``python3 scripts/probe_...py``
# (matches the convention used by the other scripts in this directory).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.data.splits import split_by_ratio

log = logging.getLogger("probe_crypto_sol_mc")


# ----------------------------------------------------------------- data acquire
def acquire(symbol: str, tf: str, bars: int) -> list:
    """Fetch real candles; fall back to a synthetic walk if offline."""
    try:
        from novatrade.data.crypto_fetcher import fetch_ohlcv

        candles = fetch_ohlcv(symbol=symbol, timeframe=tf, limit=bars)
        if candles:
            log.info("acquired %d real %s %s candles", len(candles), symbol, tf)
            return candles
    except Exception as exc:
        log.warning("live fetch unavailable (%s); using synthetic walk", exc)

    # Synthetic geometric random walk so the pipeline is always demonstrable.
    rng = np.random.default_rng(7)
    rets = rng.normal(0.0002, 0.02, size=bars)
    closes = 100.0 * np.exp(np.cumsum(rets))
    from novatrade.models import Candle

    return [
        Candle(timestamp=float(i), open=c, high=c, low=c, close=c, volume=0.0, symbol=symbol, timeframe=tf)
        for i, c in enumerate(closes)
    ]


# ---------------------------------------------------------------------- engine
def sma_cross_returns(closes: np.ndarray, fast: int = 10, slow: int = 30) -> np.ndarray:
    """Long-only SMA-cross: per-bar strategy returns (next-bar execution)."""
    closes = np.asarray(closes, dtype=float)
    if len(closes) <= slow:
        return np.zeros(0)

    def sma(a, w):
        out = np.full_like(a, np.nan)
        c = np.cumsum(np.insert(a, 0, 0.0))
        out[w - 1 :] = (c[w:] - c[:-w]) / w
        return out

    f, s = sma(closes, fast), sma(closes, slow)
    signal = (f > s).astype(float)  # 1 = long, 0 = flat
    signal = np.nan_to_num(signal)
    pos = np.roll(signal, 1)  # act next bar — no look-ahead
    pos[0] = 0.0
    bar_ret = np.diff(closes) / closes[:-1]
    return pos[1:] * bar_ret


# ------------------------------------------------------------------ monte carlo
def block_bootstrap_passrate(rets: np.ndarray, n_sims: int = 1000, block: int = 24, seed: int = 13) -> dict:
    """Resample the return series in blocks; PASS = net-positive total return."""
    rets = np.asarray(rets, dtype=float)
    if rets.size == 0:
        return {"n_sims": 0, "pass_rate": 0.0, "median_total": 0.0}

    rng = np.random.default_rng(seed)
    n = rets.size
    n_blocks = int(np.ceil(n / block))
    totals = np.empty(n_sims)
    for i in range(n_sims):
        starts = rng.integers(0, n, size=n_blocks)
        sample = np.concatenate([rets[st : st + block] for st in starts])[:n]
        totals[i] = float(np.sum(sample))
    return {
        "n_sims": n_sims,
        "pass_rate": float(np.mean(totals > 0.0)),
        "median_total": float(np.median(totals)),
        "p05_total": float(np.percentile(totals, 5)),
        "p95_total": float(np.percentile(totals, 95)),
    }


# --------------------------------------------------------------------- driver
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="SOL/USDT")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--bars", type=int, default=2000)
    ap.add_argument("--train-frac", type=float, default=0.60)
    ap.add_argument("--sims", type=int, default=1000)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    candles = acquire(args.symbol, args.tf, args.bars)
    train, val = split_by_ratio(candles, train_frac=args.train_frac, embargo=30)
    log.info("split: %d train / %d validation (embargo=30)", len(train), len(val))

    train_rets = sma_cross_returns(np.array([c.close for c in train]))
    val_rets = sma_cross_returns(np.array([c.close for c in val]))
    log.info("in-sample total return:     %+.4f", float(np.sum(train_rets)))
    log.info("out-of-sample total return: %+.4f", float(np.sum(val_rets)))

    mc = block_bootstrap_passrate(val_rets, n_sims=args.sims)
    log.info("--- Monte Carlo (block bootstrap, validation) ---")
    log.info("sims=%d  PASS rate (net-positive)=%.1f%%", mc["n_sims"], 100 * mc["pass_rate"])
    log.info(
        "median total=%+.4f  p05=%+.4f  p95=%+.4f",
        mc["median_total"],
        mc.get("p05_total", 0.0),
        mc.get("p95_total", 0.0),
    )

    verdict = "PASS" if mc["pass_rate"] >= 0.95 else "FAIL"
    log.info("VERDICT (>=95%% sims positive): %s", verdict)
    log.info(
        "NOTE: gross of fees/slippage — not a tradeable number. "
        "Route through the validated gauntlet for a real verdict."
    )


if __name__ == "__main__":
    main()
