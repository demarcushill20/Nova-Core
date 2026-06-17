#!/usr/bin/env python3
"""Backtest the 3 EMA + Stochastic RSI + ATR strategy on OHLCV / tick-derived data.

Loads candle data (parquet or CSV), runs it through the generic
``StrategyBacktesterAdapter`` (1-bar fill delay, no lookahead), and reports
sizing-aware PnL plus the sizing-independent R-multiple edge.

Examples
--------
    # 15m EURUSD parquet (built from M1/tick data)
    python scripts/backtest_three_ema_stoch_rsi.py \
        --data data/candles/eurusd_15m.parquet --symbol EURUSD --timeframe M15

    # CSV with a timestamp/open/high/low/close header
    python scripts/backtest_three_ema_stoch_rsi.py \
        --data data/candles/EURUSD_H1_10yr.csv --symbol EURUSD --timeframe H1 \
        --spread 0.5 --commission 3.5
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

from novatrade.backtest.engine import StrategyBacktesterAdapter
from novatrade.backtest.environment import BacktestEnvironment, SpreadAssumptions
from novatrade.models import Candle
from novatrade.strategies.three_ema_stoch_rsi import ThreeEmaStochRsiStrategy

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _parse_ts(raw: str) -> float:
    """Parse an ISO-ish timestamp or epoch string to unix seconds."""
    raw = raw.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    import datetime as _dt

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(raw, fmt).replace(tzinfo=_dt.timezone.utc).timestamp()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized timestamp: {raw!r}")


def load_candles(path: Path, symbol: str, timeframe: str) -> list[Candle]:
    """Load candles from a parquet (DatetimeIndex + OHLC[V]) or CSV file."""
    if path.suffix == ".parquet":
        import pandas as pd

        df = pd.read_parquet(path)
        if "time" in df.columns:
            df = df.set_index("time")
        idx = pd.to_datetime(df.index)
        candles = [
            Candle(
                timestamp=ts.timestamp(),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(getattr(row, "volume", 0.0) or 0.0),
                symbol=symbol,
                timeframe=timeframe,
            )
            for ts, row in zip(idx, df.itertuples(index=False), strict=False)
        ]
    else:
        candles = []
        with path.open() as f:
            reader = csv.DictReader(f)
            for r in reader:
                candles.append(
                    Candle(
                        timestamp=_parse_ts(r["timestamp"] if "timestamp" in r else r["time"]),
                        open=float(r["open"]),
                        high=float(r["high"]),
                        low=float(r["low"]),
                        close=float(r["close"]),
                        volume=float(r.get("volume") or 0.0),
                        symbol=symbol,
                        timeframe=timeframe,
                    )
                )
    return sorted(candles, key=lambda c: c.timestamp)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _year(ts: float) -> int:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).year


def summarize(result, candles: list[Candle], initial_equity: float) -> dict:
    """Compute sizing-aware and sizing-independent performance metrics."""
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

    # Equity curve / max drawdown
    equity = initial_equity
    peak = initial_equity
    max_dd = 0.0
    for t in trades:
        equity += t.pnl_usd
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak > 0 else 0.0)

    # Per-year R breakdown
    per_year: dict[int, list[float]] = defaultdict(list)
    for t in trades:
        per_year[_year(candles[t.entry_bar].timestamp)].append(t.risk_r)

    return {
        "n_trades": n,
        "win_rate": len(wins) / n,
        "total_pnl_usd": total_pnl,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        "mean_r": sum(r_values) / n,
        "expectancy_r": sum(r_values) / n,
        "max_drawdown_pct": max_dd * 100.0,
        "final_equity": initial_equity + total_pnl,
        "return_pct": total_pnl / initial_equity * 100.0,
        "per_year": {y: {"n": len(rs), "mean_r": sum(rs) / len(rs)} for y, rs in sorted(per_year.items())},
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--timeframe", default="M15")
    ap.add_argument("--spread", type=float, default=0.5, help="avg spread in pips")
    ap.add_argument("--commission", type=float, default=3.5, help="commission per lot (USD round-turn)")
    ap.add_argument("--risk", type=float, default=0.0033, help="risk fraction per trade")
    ap.add_argument("--initial-equity", type=float, default=100_000.0)
    ap.add_argument("--max-rows", type=int, default=0, help="limit candles (0 = all)")
    # strategy params
    ap.add_argument("--sl-atr-mult", type=float, default=1.5)
    ap.add_argument("--tp-rr", type=float, default=1.5)
    ap.add_argument("--time-stop-bars", type=int, default=0)
    ap.add_argument("--stoch-overbought", type=float, default=80.0)
    ap.add_argument("--stoch-oversold", type=float, default=20.0)
    args = ap.parse_args()

    candles = load_candles(args.data, args.symbol, args.timeframe)
    if args.max_rows > 0:
        candles = candles[: args.max_rows]
    print(f"Loaded {len(candles):,} candles ({args.symbol} {args.timeframe}) from {args.data}")

    env = BacktestEnvironment(
        initial_equity=args.initial_equity,
        risk_fraction=args.risk,
        spread=SpreadAssumptions(avg_spread_pips=args.spread, commission_per_lot_usd=args.commission),
    )
    strategy = ThreeEmaStochRsiStrategy(
        sl_atr_mult=args.sl_atr_mult,
        tp_rr=args.tp_rr,
        time_stop_bars=args.time_stop_bars,
        stoch_overbought=args.stoch_overbought,
        stoch_oversold=args.stoch_oversold,
    )
    bt = StrategyBacktesterAdapter(strategy, env)
    result = bt.run(candles)

    m = summarize(result, candles, args.initial_equity)
    print("\n=== 3 EMA + Stochastic RSI + ATR backtest ===")
    print(f"params: SL={args.sl_atr_mult}xATR  TP={args.tp_rr}R  spread={args.spread}p  comm={args.commission}/lot")
    if m["n_trades"] == 0:
        print("No trades generated.")
        return
    print(f"trades         : {m['n_trades']:,}")
    print(f"win rate       : {m['win_rate'] * 100:.1f}%")
    print(f"profit factor  : {m['profit_factor']:.3f}")
    print(f"mean R / trade : {m['mean_r']:+.4f}")
    print(f"total PnL      : ${m['total_pnl_usd']:,.0f}  ({m['return_pct']:+.1f}%)")
    print(f"final equity   : ${m['final_equity']:,.0f}")
    print(f"max drawdown   : {m['max_drawdown_pct']:.1f}%")
    print("\nper-year mean R (sizing-independent edge):")
    pos = 0
    for y, ym in m["per_year"].items():
        flag = "+" if ym["mean_r"] > 0 else " "
        pos += 1 if ym["mean_r"] > 0 else 0
        print(f"  {y}: n={ym['n']:>4}  mean_R={ym['mean_r']:+.4f} {flag}")
    print(f"\npositive years : {pos}/{len(m['per_year'])}")


if __name__ == "__main__":
    main()
