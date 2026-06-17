#!/usr/bin/env python3
"""Backtest task-1024 mechanical spec: 3 EMA + Stoch RSI entry, ATR(3x/2x) exits.

This is the "cleanest mechanical version" of the strategy tied to the referenced
YouTube write-up. It reuses the validated ``ThreeEmaStochRsiStrategy`` (entry +
exit logic) and the generic ``StrategyBacktesterAdapter`` (1-bar-delay fill, no
lookahead), and pins the exit/management rules from TASKS/1024:

Exit (set at entry, never moved):
    LONG :  stop  = entry - 3 * ATR    take_profit = entry + 2 * ATR
    SHORT:  stop  = entry + 3 * ATR    take_profit = entry - 2 * ATR

  -> sl_atr_mult = 3.0 ; tp_rr = (2*ATR)/(3*ATR) = 2/3 (TP distance over risk).

Trade management (enforced by the engine, see novatrade/backtest/engine.py):
    - one trade at a time (entry only checked while flat)         line ~1624
    - no pyramiding / averaging / partial exits / trailing / BE   (none coded)
    - new signals ignored while in a trade                        (flat gate)
    - exit only at stop loss or take profit                       (check_exit)

Backtest safety:
    - no lookahead; signals confirmed on the closed bar, filled next bar
    - same-candle stop+target -> stop assumed first (check_exit checks stop first)
    - configurable spread, commission, slippage (cost = spread + 2*slippage pips)

Reports: win rate, profit factor, expectancy (R), average R, total R,
max drawdown, and monthly returns.

Example
-------
    python scripts/backtest_1024_three_ema_atr.py \
        --data data/candles/EURUSD_H1_10yr.csv --symbol EURUSD --timeframe H1 \
        --spread 0.5 --commission 3.5 --slippage 0.2
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from collections import defaultdict
from pathlib import Path

# Reuse the candle loader from the sibling runner.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_three_ema_stoch_rsi import load_candles

from novatrade.backtest.engine import StrategyBacktesterAdapter
from novatrade.backtest.environment import BacktestEnvironment, SpreadAssumptions
from novatrade.strategies.three_ema_stoch_rsi import ThreeEmaStochRsiStrategy


def _month_key(ts: float) -> str:
    d = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    return f"{d.year:04d}-{d.month:02d}"


def summarize(result, candles, initial_equity: float) -> dict:
    """Full task-1024 metric set, including total R and monthly returns."""
    trades = result.trades
    n = len(trades)
    if n == 0:
        return {"n_trades": 0}

    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd < 0]
    gross_profit = sum(t.pnl_usd for t in wins)
    gross_loss = -sum(t.pnl_usd for t in losses)
    total_pnl = sum(t.pnl_usd for t in trades)
    r_values = [t.risk_r for t in trades]
    total_r = sum(r_values)

    # Equity curve / max drawdown (trade-sequential).
    equity = initial_equity
    peak = initial_equity
    max_dd = 0.0
    for t in trades:
        equity += t.pnl_usd
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak > 0 else 0.0)

    # Monthly returns (PnL $ and R), keyed by entry month.
    monthly_pnl: dict[str, float] = defaultdict(float)
    monthly_r: dict[str, float] = defaultdict(float)
    monthly_n: dict[str, int] = defaultdict(int)
    for t in trades:
        mk = _month_key(candles[t.entry_bar].timestamp)
        monthly_pnl[mk] += t.pnl_usd
        monthly_r[mk] += t.risk_r
        monthly_n[mk] += 1

    pos_months = sum(1 for v in monthly_r.values() if v > 0)

    return {
        "n_trades": n,
        "win_rate": len(wins) / n,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        "expectancy_r": total_r / n,
        "average_r": total_r / n,
        "total_r": total_r,
        "total_pnl_usd": total_pnl,
        "max_drawdown_pct": max_dd * 100.0,
        "final_equity": initial_equity + total_pnl,
        "return_pct": total_pnl / initial_equity * 100.0,
        "monthly": {
            mk: {"n": monthly_n[mk], "pnl_usd": monthly_pnl[mk], "r": monthly_r[mk]} for mk in sorted(monthly_pnl)
        },
        "positive_months": pos_months,
        "n_months": len(monthly_pnl),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--timeframe", default="H1")
    ap.add_argument("--spread", type=float, default=0.5, help="avg spread in pips")
    ap.add_argument("--commission", type=float, default=3.5, help="commission per lot (USD round-turn)")
    ap.add_argument("--slippage", type=float, default=0.2, help="slippage in pips (applied per side)")
    ap.add_argument("--risk", type=float, default=0.0033, help="risk fraction per trade")
    ap.add_argument("--initial-equity", type=float, default=100_000.0)
    ap.add_argument("--max-rows", type=int, default=0, help="limit candles (0 = all)")
    # 1024 spec exit params (overridable, but defaulted to the spec).
    ap.add_argument("--sl-atr-mult", type=float, default=3.0)
    ap.add_argument("--tp-rr", type=float, default=2.0 / 3.0, help="TP distance / risk; 2*ATR over 3*ATR = 0.667")
    ap.add_argument("--show-monthly", action="store_true", help="print the full monthly table")
    args = ap.parse_args()

    candles = load_candles(args.data, args.symbol, args.timeframe)
    if args.max_rows > 0:
        candles = candles[: args.max_rows]
    print(f"Loaded {len(candles):,} candles ({args.symbol} {args.timeframe}) from {args.data}")

    env = BacktestEnvironment(
        initial_equity=args.initial_equity,
        risk_fraction=args.risk,
        spread=SpreadAssumptions(
            avg_spread_pips=args.spread,
            commission_per_lot_usd=args.commission,
            slippage_pips=args.slippage,
        ),
    )
    strategy = ThreeEmaStochRsiStrategy(sl_atr_mult=args.sl_atr_mult, tp_rr=args.tp_rr)
    bt = StrategyBacktesterAdapter(strategy, env)
    result = bt.run(candles)

    m = summarize(result, candles, args.initial_equity)
    print("\n=== TASK-1024  3 EMA + Stoch RSI + ATR (3x SL / 2x TP) ===")
    print(
        f"params: SL={args.sl_atr_mult}xATR  TP={args.tp_rr * args.sl_atr_mult:.0f}xATR "
        f"(tp_rr={args.tp_rr:.3f})  spread={args.spread}p  slip={args.slippage}p  comm={args.commission}/lot"
    )
    if m["n_trades"] == 0:
        print("No trades generated.")
        return
    print(f"trades         : {m['n_trades']:,}")
    print(f"win rate       : {m['win_rate'] * 100:.1f}%")
    print(f"profit factor  : {m['profit_factor']:.3f}")
    print(f"expectancy R   : {m['expectancy_r']:+.4f}")
    print(f"average R      : {m['average_r']:+.4f}")
    print(f"total R        : {m['total_r']:+.2f}")
    print(f"total PnL      : ${m['total_pnl_usd']:,.0f}  ({m['return_pct']:+.1f}%)")
    print(f"final equity   : ${m['final_equity']:,.0f}")
    print(f"max drawdown   : {m['max_drawdown_pct']:.1f}%")
    print(f"positive months: {m['positive_months']}/{m['n_months']}")

    if args.show_monthly:
        print("\nmonthly returns (entry month):")
        print(f"  {'month':<9} {'n':>4} {'pnl_usd':>12} {'R':>9}")
        for mk, mm in m["monthly"].items():
            print(f"  {mk:<9} {mm['n']:>4} {mm['pnl_usd']:>12,.0f} {mm['r']:>+9.2f}")


if __name__ == "__main__":
    main()
