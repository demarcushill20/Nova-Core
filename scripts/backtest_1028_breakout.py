#!/usr/bin/env python3
"""Backtest task-1028: "test another strategy" -> Donchian Channel breakout.

Context: the 3 EMA + Stoch RSI + ATR strategy (tasks 1024/1026/1027) was REJECTED
on EURUSD H1/M15 (net-negative, PF < 1) -- its entry signal carries no edge. The
standing portfolio-search verdict (MEMORY/project_strategy_search) is that simple
mechanical TA on liquid FX is not viable, with trend-CONTINUATION the only "first
real candidate" found so far.

A Donchian breakout is a different alpha family (momentum/trend continuation, not
the 3 EMA mean-pullback shape), so it is a clean "another strategy" to put through
the SAME validated harness for direct comparability:

  - reuses the registered ``BreakoutStrategy`` (Donchian entry + ATR trailing exit)
  - reuses the generic ``StrategyBacktesterAdapter`` (1-bar-delay fill, no lookahead)
  - reuses the same ECN cost model + EURUSD_H1_10yr data used for task 1024

We run TWICE per dataset:
  1. NET  -- ECN costs (spread + commission + slippage) deducted, the live verdict
  2. GROSS -- zero cost, to isolate whether the breakout ENTRY has any raw edge

Reports: trades, win rate, profit factor, expectancy/avg/total R, total PnL,
max drawdown, positive months.
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
from novatrade.strategies.breakout import BreakoutStrategy


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
    strategy = BreakoutStrategy(
        channel_period=args.channel_period,
        atr_period=args.atr_period,
        trail_atr_mult=args.trail_atr_mult,
        time_stop_bars=args.time_stop_bars,
    )
    bt = StrategyBacktesterAdapter(strategy, env)
    result = bt.run(candles)
    return summarize(result, candles, args.initial_equity)


def _print(tag: str, m: dict, args) -> None:
    print(f"\n=== TASK-1028  Donchian breakout ({tag}) ===")
    print(
        f"params: channel={args.channel_period}  atr={args.atr_period}  "
        f"trail={args.trail_atr_mult}xATR  time_stop={args.time_stop_bars} bars"
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
    # Breakout params (Donchian defaults).
    ap.add_argument("--channel-period", type=int, default=20)
    ap.add_argument("--atr-period", type=int, default=14)
    ap.add_argument("--trail-atr-mult", type=float, default=2.0)
    ap.add_argument("--time-stop-bars", type=int, default=50)
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
