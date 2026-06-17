#!/usr/bin/env python3
"""Backtest task-1031: FULL MTF-EMA + MACD-divergence union (EUR/USD M5).

Implements the most restrictive reading of the task-1031 written review -- the
*union* of every filter listed there, which no prior runner tested together:

  long_signal = long_bias            (ema_15m_50 > ema_1h_50)
                and bullish_divergence   (price LL + MACD HL)
                and macd_below_zero      (macd & signal < 0, held p1..entry)
                and bullish_hist_gap     (histogram resets >0 between the lows)
                and macd_cross_up        (the entry TRIGGER)
  short_signal = mirror.

Compared with prior MACD-divergence runs:
  * task-1029 had the MTF-EMA bias + MACD cross, but NO zero-line, NO hist gap.
  * task-1030 had the zero-line + pivot-gap divergence, but NO MTF-EMA bias and
    entered on the pivot-confirm bar (no MACD-cross trigger).
  * task-1031 (here) = ALL of them at once.

Reuses the validated harness for direct comparability with 1024/1026/1028/1029/1030:
  * generic ``StrategyBacktesterAdapter`` (1-bar-delay fill, no look-ahead)
  * identical ECN cost model + EURUSD M5 data + ``summarize`` metric set.

Run modes per dataset:
  NET   -- ECN costs deducted (the live verdict).
  GROSS -- zero cost, to isolate whether the ENTRY carries any raw edge.

Example
-------
    python scripts/backtest_1031_macd_divergence_full.py \
        --data data/candles/EURUSD_M5_10yr.csv --symbol EURUSD --timeframe M5 \
        --spread 0.5 --commission 3.5 --slippage 0.2
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
    strategy = MacdDivergenceMtfFullStrategy(
        pivot_left=args.pivot_left,
        pivot_right=args.pivot_right,
        min_bars_between=args.min_bars_between,
        max_bars_between=args.max_bars_between,
        tp_rr=args.tp_rr,
        time_stop_bars=args.time_stop_bars,
        require_zero_line=not args.no_zero_line,
        require_bias=not args.no_bias,
        require_hist_gap=not args.no_hist_gap,
        require_cross=not args.no_cross,
    )
    bt = StrategyBacktesterAdapter(strategy, env)
    result = bt.run(candles)
    return summarize(result, candles, args.initial_equity)


def _print(tag: str, m: dict, args) -> None:
    print(f"\n=== TASK-1031  MTF-EMA + MACD divergence FULL ({tag}) ===")
    print(
        f"filters: bias={'OFF' if args.no_bias else 'ON'}  "
        f"zero_line={'OFF' if args.no_zero_line else 'ON'}  "
        f"hist_gap={'OFF' if args.no_hist_gap else 'ON'}  "
        f"cross={'OFF' if args.no_cross else 'ON'}  tp_rr={args.tp_rr}"
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
    ap.add_argument("--time-stop-bars", type=int, default=0)
    ap.add_argument("--no-zero-line", action="store_true")
    ap.add_argument("--no-bias", action="store_true", help="disable the MTF-EMA bias filter")
    ap.add_argument("--no-hist-gap", action="store_true", help="disable the histogram-gap filter")
    ap.add_argument("--no-cross", action="store_true", help="disable the MACD-cross trigger")
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
