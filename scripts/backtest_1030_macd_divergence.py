#!/usr/bin/env python3
"""Backtest task-1030: MACD bullish/bearish divergence reversal.

Implements the mechanical divergence spec in TASKS/1030:
  - bullish divergence LONG : price lower-low + MACD higher-low, MACD & signal < 0
  - bearish divergence SHORT: price higher-high + MACD lower-high, MACD & signal > 0
  - swing pivots: pivot_left=3 / pivot_right=3, 5..60 bars between, confirmed on the
    pivot_right-th bar AFTER the pivot (no lookahead -- signal bar = confirm bar)
  - strict zero-line rule held across the whole pivot1..entry window

Reuses the validated harness for direct comparability with tasks 1024/1026/1028:
  - generic ``StrategyBacktesterAdapter`` (1-bar-delay fill, no lookahead)
  - same ECN cost model + EURUSD data + ``summarize`` metric set

Run TWICE per dataset: NET (ECN costs, the live verdict) and GROSS (zero cost, to
isolate whether the divergence ENTRY carries any raw edge).
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
from novatrade.strategies.macd_divergence import MacdDivergenceStrategy


def run_once(candles, *, gross: bool, args) -> dict:
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
    strategy = MacdDivergenceStrategy(
        pivot_left=args.pivot_left,
        pivot_right=args.pivot_right,
        min_bars_between=args.min_bars_between,
        max_bars_between=args.max_bars_between,
        tp_rr=args.tp_rr,
        time_stop_bars=args.time_stop_bars,
        require_zero_line=not args.no_zero_line,
    )
    bt = StrategyBacktesterAdapter(strategy, env)
    result = bt.run(candles)
    return summarize(result, candles, args.initial_equity)


def _print(tag: str, m: dict, args) -> None:
    print(f"\n=== TASK-1030  MACD divergence ({tag}) ===")
    print(
        f"params: pivots={args.pivot_left}/{args.pivot_right}  "
        f"gap={args.min_bars_between}..{args.max_bars_between}  tp_rr={args.tp_rr}  "
        f"zero_line={'OFF' if args.no_zero_line else 'ON'}"
    )
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
    print(f"positive months: {m['positive_months']}/{m['n_months']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--timeframe", default="H1")
    ap.add_argument("--spread", type=float, default=0.5)
    ap.add_argument("--commission", type=float, default=3.5)
    ap.add_argument("--slippage", type=float, default=0.2)
    ap.add_argument("--risk", type=float, default=0.0033)
    ap.add_argument("--initial-equity", type=float, default=100_000.0)
    ap.add_argument("--max-rows", type=int, default=0)
    # Divergence params (spec defaults).
    ap.add_argument("--pivot-left", type=int, default=3)
    ap.add_argument("--pivot-right", type=int, default=3)
    ap.add_argument("--min-bars-between", type=int, default=5)
    ap.add_argument("--max-bars-between", type=int, default=60)
    ap.add_argument("--tp-rr", type=float, default=2.0)
    ap.add_argument("--time-stop-bars", type=int, default=0)
    ap.add_argument("--no-zero-line", action="store_true", help="disable the strict MACD zero-line filter")
    args = ap.parse_args()

    candles = load_candles(args.data, args.symbol, args.timeframe)
    if args.max_rows > 0:
        candles = candles[: args.max_rows]
    print(f"Loaded {len(candles):,} candles ({args.symbol} {args.timeframe}) from {args.data}")

    net = run_once(candles, gross=False, args=args)
    gross = run_once(candles, gross=True, args=args)
    _print("NET of ECN cost", net, args)
    _print("GROSS (zero cost)", gross, args)


if __name__ == "__main__":
    main()
