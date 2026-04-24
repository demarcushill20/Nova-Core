#!/usr/bin/env python3
"""Run IRB v5 backtest for a specific date window, with trade-level export.

Fetches M5 (primary) + H1 (higher) bars from MetaApi for the window, runs the
validated IRB v5 champion engine, and writes a trades CSV suitable for
pairing against live FTMO deals in the parity report.

Usage:
    python3 scripts/backtest_window_for_parity.py \
        --start 2026-04-21 \
        --end 2026-04-24 \
        --warmup-days 2 \
        --env-file OUTPUT/novatrade.env.updated
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.data.historical_fetcher import HistoricalFetcher


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


async def _fetch_bars(symbol: str, start: datetime, end: datetime) -> tuple[list, list]:
    fetcher = HistoricalFetcher(
        token=os.environ["METAAPI_TOKEN"],
        account_id=os.environ["METAAPI_ACCOUNT_ID"],
        region=os.environ.get("METAAPI_REGION", "new-york"),
    )
    await fetcher.connect()
    try:
        print(f"[bt] fetching M5 {symbol} {start.date()} -> {end.date()}")
        m5 = await fetcher.fetch_range(symbol, "M5", start, end)
        print(f"[bt] fetched {len(m5)} M5 bars")
        print(f"[bt] fetching H1 {symbol} {start.date()} -> {end.date()}")
        h1 = await fetcher.fetch_range(symbol, "H1", start, end)
        print(f"[bt] fetched {len(h1)} H1 bars")
    finally:
        await fetcher.disconnect()
    return m5, h1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive, UTC)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (exclusive, UTC)")
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--warmup-days", type=int, default=2, help="Additional lookback days for indicator warmup")
    ap.add_argument("--config", default="configs/strategies/irb_v5_m5_champion.yaml")
    ap.add_argument("--env-file", default="OUTPUT/novatrade.env.updated")
    ap.add_argument("--out-dir", default="OUTPUT/parity")
    args = ap.parse_args()

    _load_env_file(Path(args.env_file))

    live_start = _parse_date(args.start)
    live_end = _parse_date(args.end)
    fetch_start = live_start - timedelta(days=args.warmup_days)

    # Fetch bars
    m5, h1 = asyncio.run(_fetch_bars(args.symbol, fetch_start, live_end))
    if not m5 or not h1:
        print("[bt] ERROR: empty bar data")
        return 1

    # Load v5 champion environment
    env = BacktestEnvironment.from_champion_config(args.config)
    print(
        f"[bt] env primary_tf={env.primary_timeframe} higher_tf={env.higher_timeframe} "
        f"irb_thr={env.irb_threshold} trigger_window={env.trigger_window_bars}"
    )

    # Run backtest (engine param names are legacy h1/h4 but accept any primary/higher pair)
    bt = IRBBacktester(env=env)
    result = bt.run(h1_candles=m5, h4_candles=h1)
    print(f"[bt] total bars processed: {result.total_bars}  trades: {len(result.trades)}")

    # Engine does not populate entry/exit_timestamp — map from bar index.
    bar_ts = [c.timestamp for c in m5]

    def _ts(idx: int) -> float:
        if 0 <= idx < len(bar_ts):
            return bar_ts[idx]
        return 0.0

    for t in result.trades:
        if not t.entry_timestamp:
            t.entry_timestamp = _ts(t.entry_bar)
        if not t.exit_timestamp:
            t.exit_timestamp = _ts(t.exit_bar)

    # Filter to trades whose entry falls in the live window
    live_start_ts = live_start.timestamp()
    live_end_ts = live_end.timestamp()
    in_window = [t for t in result.trades if live_start_ts <= t.entry_timestamp < live_end_ts]
    print(f"[bt] trades in live window [{args.start}..{args.end}): {len(in_window)}")
    if result.trades:
        first = result.trades[0]
        last = result.trades[-1]
        print(
            f"[bt] first trade entry_ts={first.entry_timestamp} "
            f"({datetime.fromtimestamp(first.entry_timestamp, tz=timezone.utc).isoformat()})"
        )
        print(
            f"[bt] last  trade entry_ts={last.entry_timestamp} "
            f"({datetime.fromtimestamp(last.entry_timestamp, tz=timezone.utc).isoformat()})"
        )
    # If no trades in window but we have trades, export all so we can diagnose.
    if not in_window and result.trades:
        print(f"[bt] WARN: zero trades in window — exporting ALL {len(result.trades)} for diagnostics")
        in_window = list(result.trades)

    # Export CSV
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    trades_csv = out_dir / f"backtest_trades_{stamp}.csv"
    fields = [
        "trade_id",
        "side",
        "entry_bar",
        "exit_bar",
        "entry_timestamp",
        "entry_iso",
        "entry_price",
        "stop_loss",
        "exit_timestamp",
        "exit_iso",
        "exit_price",
        "exit_reason",
        "pnl_pips",
        "pnl_usd",
        "risk_r",
        "hold_bars",
        "outcome",
    ]
    with trades_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in in_window:
            w.writerow(
                {
                    "trade_id": t.trade_id,
                    "side": t.side.value if hasattr(t.side, "value") else str(t.side),
                    "entry_bar": t.entry_bar,
                    "exit_bar": t.exit_bar,
                    "entry_timestamp": t.entry_timestamp,
                    "entry_iso": datetime.fromtimestamp(t.entry_timestamp, tz=timezone.utc).isoformat(),
                    "entry_price": t.entry_price,
                    "stop_loss": t.stop_loss,
                    "exit_timestamp": t.exit_timestamp,
                    "exit_iso": datetime.fromtimestamp(t.exit_timestamp, tz=timezone.utc).isoformat()
                    if t.exit_timestamp
                    else "",
                    "exit_price": t.exit_price,
                    "exit_reason": t.exit_reason.value if hasattr(t.exit_reason, "value") else str(t.exit_reason),
                    "pnl_pips": round(t.pnl_pips, 2),
                    "pnl_usd": round(t.pnl_usd, 2),
                    "risk_r": round(t.risk_r, 3),
                    "hold_bars": t.hold_bars,
                    "outcome": "WIN" if t.pnl_pips > 0 else ("LOSS" if t.pnl_pips < 0 else "FLAT"),
                }
            )
    print(f"[bt] wrote {trades_csv}")

    summary = {
        "symbol": args.symbol,
        "window_start": args.start,
        "window_end": args.end,
        "warmup_days": args.warmup_days,
        "config": args.config,
        "m5_bars": len(m5),
        "h1_bars": len(h1),
        "total_trades_in_run": len(result.trades),
        "trades_in_window": len(in_window),
        "out": str(trades_csv),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
