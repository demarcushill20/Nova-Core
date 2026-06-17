#!/usr/bin/env python3
"""Backtest task-1033: 15m EMA-50 vs 1h EMA-50 MTF bias + MACD divergence (EUR/USD M5).

Task 1033 spec headline: "15-minute EMA 50 < 1-hour EMA 50" -- i.e. the trade
bias is gated by the relationship between the 50-period EMA on the 15-minute
chart and the 50-period EMA on the 1-hour chart:

    long  bias  <=>  ema_15m_50 > ema_1h_50   (15m above 1h  -> up-trend)
    short bias  <=>  ema_15m_50 < ema_1h_50   (15m below 1h  -> down-trend)

On top of that bias the spec is the strict, mechanical MACD-divergence reversal
already implemented as ``MacdDivergenceMtfFullStrategy`` (task-1031 union):

  * confirmed swing pivots (3/3), price LL + MACD HL (bull) / price HH + MACD LH (bear)
  * MACD & signal held on the correct side of zero across the setup (zero-line rule)
  * MACD histogram gap/reset between the two divergent pivots
  * entry TRIGGER = MACD line crosses signal (up for long / down for short)
  * confirm on close, fill next-bar open (1-bar delay, no look-ahead)
  * stop = divergence pivot +/- 2 pips, take-profit = entry +/- 2 x risk (2R)
  * one trade at a time, no pyramiding/averaging/trailing/break-even/partials
  * SL-before-TP tie-break inside the same candle (engine default)

This script answers the ONE question that distinguishes 1033 from the prior
MACD-divergence rejects (1029/1030/1031): does the 15m/1h EMA-50 bias filter add
real edge? It runs the SAME strategy with bias ON and bias OFF, each NET (ECN
cost) and GROSS (zero cost), on the identical validated harness + EURUSD M5 feed.

Run:
    PYTHONPATH=. .venv/bin/python scripts/backtest_1033_macd_divergence_mtf.py \
        --data data/candles/EURUSD_M5_10yr.csv --symbol EURUSD --timeframe M5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_1024_three_ema_atr import summarize
from backtest_three_ema_stoch_rsi import load_candles

from novatrade.backtest.engine import StrategyBacktesterAdapter
from novatrade.backtest.environment import BacktestEnvironment, SpreadAssumptions
from novatrade.strategies.macd_divergence_mtf_full import (
    MacdDivergenceMtfFullStrategy,
)


def run_once(candles, *, gross: bool, require_bias: bool, args) -> dict:
    spread = SpreadAssumptions(
        avg_spread_pips=0.0 if gross else args.spread,
        commission_per_lot_usd=0.0 if gross else args.commission,
        slippage_pips=0.0 if gross else args.slippage,
    )
    env = BacktestEnvironment(
        initial_equity=args.initial_equity,
        risk_fraction=args.risk,
        spread=spread,
    )
    strategy = MacdDivergenceMtfFullStrategy(
        pivot_left=args.pivot_left,
        pivot_right=args.pivot_right,
        min_bars_between=args.min_bars_between,
        max_bars_between=args.max_bars_between,
        tp_rr=args.tp_rr,
        require_zero_line=True,
        require_bias=require_bias,
        require_hist_gap=True,
        require_cross=True,
    )
    bt = StrategyBacktesterAdapter(strategy, env)
    return summarize(bt.run(candles), candles, args.initial_equity)


def _print(tag: str, m: dict) -> None:
    print(f"\n=== TASK-1033  15m/1h EMA-50 bias + MACD divergence ({tag}) ===")
    if m.get("n_trades", 0) == 0:
        print("No trades generated.")
        return
    print(f"trades         : {m['n_trades']:,}")
    print(f"win rate       : {m['win_rate'] * 100:.1f}%")
    print(f"profit factor  : {m['profit_factor']:.3f}")
    print(f"expectancy R   : {m['expectancy_r']:+.4f}")
    print(f"total R        : {m['total_r']:+.2f}")
    print(f"total PnL      : ${m['total_pnl_usd']:,.0f}  ({m['return_pct']:+.1f}%)")
    print(f"final equity   : ${m['final_equity']:,.0f}")
    print(f"max drawdown   : {m['max_drawdown_pct']:.1f}%")
    print(f"max consec loss: {m.get('max_consecutive_losses', 'n/a')}")
    print(f"positive months: {m['positive_months']}/{m['n_months']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--spread", type=float, default=0.5)
    ap.add_argument("--commission", type=float, default=3.5)
    ap.add_argument("--slippage", type=float, default=0.2)
    ap.add_argument("--risk", type=float, default=0.0033)
    ap.add_argument("--initial-equity", type=float, default=100_000.0)
    ap.add_argument("--max-rows", type=int, default=0)
    ap.add_argument("--pivot-left", type=int, default=3)
    ap.add_argument("--pivot-right", type=int, default=3)
    ap.add_argument("--min-bars-between", type=int, default=5)
    ap.add_argument("--max-bars-between", type=int, default=60)
    ap.add_argument("--tp-rr", type=float, default=2.0)
    args = ap.parse_args()

    candles = load_candles(args.data, args.symbol, args.timeframe)
    if args.max_rows > 0:
        candles = candles[: args.max_rows]
    print(f"Loaded {len(candles):,} candles ({args.symbol} {args.timeframe}) from {args.data}")

    _print("BIAS ON  -- NET of ECN cost", run_once(candles, gross=False, require_bias=True, args=args))
    _print("BIAS ON  -- GROSS (zero cost)", run_once(candles, gross=True, require_bias=True, args=args))
    _print("BIAS OFF -- NET of ECN cost", run_once(candles, gross=False, require_bias=False, args=args))
    _print("BIAS OFF -- GROSS (zero cost)", run_once(candles, gross=True, require_bias=False, args=args))


if __name__ == "__main__":
    main()
