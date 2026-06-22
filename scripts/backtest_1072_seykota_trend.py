#!/usr/bin/env python3
"""Backtest task-1072: the reverse-engineered Ed Seykota trend system.

Context: a YouTube transcript ("I Tested the Strategy That Turned $5,000 Into
$15 Million") reconstructs Seykota's mechanical rules and claims an edge across
stocks/gold/oil/crypto. The five public rules are:

  1. EMA-100 trend filter (long above / short below)
  2. 20-bar Donchian breakout entry
  3. pyramid up to 3 units
  4. ATR trailing stop
  5. fixed-% sizing

This puts the 1-unit CORE (rules 1, 2, 4 + fixed-% sizing) through the same
validated, no-lookahead harness used for the Donchian breakout (task-1028) so
the EMA filter's contribution is directly comparable. Rule 3 (pyramiding) is a
position-scaling overlay the single-position engine does not model -- it
amplifies an existing edge, it does not create one, so the honest first test is
whether the core expectancy is positive net of cost.

We run TWICE per dataset:
  1. NET   -- transaction costs deducted (the live verdict)
  2. GROSS -- zero cost, isolating whether the EMA-filtered entry has raw edge

It also prints the plain Donchian breakout (no EMA filter) as a control, so the
filter's marginal effect is visible.

Examples
--------
    # Gold 15m (parquet)
    python scripts/backtest_1072_seykota_trend.py \
        --data data/candles/xauusd_15m.parquet --symbol XAUUSD --timeframe M15 \
        --spread 2.0 --commission 0 --slippage 0.5 --pip-buffer 0.01

    # Crypto 1h (parquet)
    python scripts/backtest_1072_seykota_trend.py \
        --data data/candles/btcusd_1h.parquet --symbol BTCUSD --timeframe H1 \
        --spread 5.0 --commission 0 --slippage 2.0 --pip-buffer 1.0

    # EURUSD H1 10yr (CSV) -- ECN cost
    python scripts/backtest_1072_seykota_trend.py \
        --data data/candles/EURUSD_H1_10yr.csv --symbol EURUSD --timeframe H1 \
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
from novatrade.strategies.breakout import BreakoutStrategy
from novatrade.strategies.seykota_trend import SeykotaTrendStrategy


def _build_env(args, *, gross: bool) -> BacktestEnvironment:
    spread = SpreadAssumptions(
        avg_spread_pips=0.0 if gross else args.spread,
        commission_per_lot_usd=0.0 if gross else args.commission,
        slippage_pips=0.0 if gross else args.slippage,
    )
    return BacktestEnvironment(
        initial_equity=args.initial_equity,
        risk_fraction=args.risk,
        spread=spread,
    )


def run_once(candles, *, strategy, gross: bool, args) -> dict:
    env = _build_env(args, gross=gross)
    bt = StrategyBacktesterAdapter(strategy, env)
    result = bt.run(candles)
    return summarize(result, candles, args.initial_equity)


def _make_seykota(args) -> SeykotaTrendStrategy:
    return SeykotaTrendStrategy(
        channel_period=args.channel_period,
        atr_period=args.atr_period,
        trail_atr_mult=args.trail_atr_mult,
        time_stop_bars=args.time_stop_bars,
        ema_period=args.ema_period,
        pip_buffer=args.pip_buffer,
    )


def _make_breakout(args) -> BreakoutStrategy:
    return BreakoutStrategy(
        channel_period=args.channel_period,
        atr_period=args.atr_period,
        trail_atr_mult=args.trail_atr_mult,
        time_stop_bars=args.time_stop_bars,
        pip_buffer=args.pip_buffer,
    )


def _print(tag: str, m: dict, args) -> None:
    print(f"\n=== TASK-1072  Seykota trend ({tag}) ===")
    print(
        f"params: ema={args.ema_period}  channel={args.channel_period}  "
        f"atr={args.atr_period}  trail={args.trail_atr_mult}xATR  "
        f"time_stop={args.time_stop_bars} bars"
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
    # Seykota / breakout params.
    ap.add_argument("--ema-period", type=int, default=100)
    ap.add_argument("--channel-period", type=int, default=20)
    ap.add_argument("--atr-period", type=int, default=14)
    ap.add_argument("--trail-atr-mult", type=float, default=2.0)
    ap.add_argument("--time-stop-bars", type=int, default=200)
    ap.add_argument("--pip-buffer", type=float, default=0.0001)
    ap.add_argument(
        "--no-control",
        action="store_true",
        help="Skip the plain-Donchian (no EMA filter) control run.",
    )
    args = ap.parse_args()

    candles = load_candles(args.data, args.symbol, args.timeframe)
    if args.max_rows > 0:
        candles = candles[: args.max_rows]
    print(f"Loaded {len(candles):,} candles ({args.symbol} {args.timeframe}) from {args.data}")
    print("NOTE: pyramiding (rule 3) is NOT modelled -- single-unit core only.")

    seykota = _make_seykota(args)
    net = run_once(candles, strategy=seykota, gross=False, args=args)
    gross = run_once(candles, strategy=_make_seykota(args), gross=True, args=args)
    _print("NET of cost", net, args)
    _print("GROSS (zero cost)", gross, args)

    if not args.no_control:
        ctrl_net = run_once(candles, strategy=_make_breakout(args), gross=False, args=args)
        _print("CONTROL: plain Donchian, no EMA filter (NET)", ctrl_net, args)


if __name__ == "__main__":
    main()
