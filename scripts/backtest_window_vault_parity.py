#!/usr/bin/env python3
"""Vault-native backtest for a date window, for live-vs-backtest parity.

WHY THIS EXISTS (and why it is NOT scripts/backtest_window_for_parity.py):
    The live runtime runs NOVATRADE_STRATEGY_LOGIC=vault_native — i.e.
    `VaultLiveEngine` driving vault Python's `IRBStrategyState`
    (validated +$45,310 / 11-of-11 yrs). The original parity backtest script
    runs the in-house `IRBBacktester`, which carries the documented
    sign-flipping engine bug (-$102k / 0-of-11 yrs). Diffing live vault trades
    against the nova engine would flag false "strategy drift" on every trade.

    This script runs the SAME vault core the live engine runs
    (`run_vault_strategy_batch` is proven by tests/test_vault_engine_streaming_parity.py
    to match `VaultLiveEngine.on_bar()` streaming), over MetaApi-fetched M5 bars
    for the window, and emits a trades CSV in the exact schema
    scripts/parity_report.py consumes.

Config is built the same way novatrade.runtime.runner.build_live_stack builds it:
    from_champion_config(YAML) -> risk_fraction override -> env_to_irb_config().
    (Sizing params do not affect which trades fire or their win/loss outcome.)

Usage:
    python3 scripts/backtest_window_vault_parity.py \
        --start 2026-05-25 --end 2026-05-31 --warmup-days 3 \
        --env-file OUTPUT/novatrade.env.updated
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.config import NovaTradeCfg
from novatrade.data.historical_fetcher import HistoricalFetcher
from novatrade.strategies.vault_irb_state import IRBStrategyState, prepare_dataframe
from novatrade.strategy.vault_engine import env_to_irb_config


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


async def _fetch_m5(cfg, symbol: str, start: datetime, end: datetime) -> list:
    fetcher = HistoricalFetcher(
        token=cfg.metaapi.token,
        account_id=cfg.metaapi.account_id,
        region=cfg.metaapi.region,
    )
    await fetcher.connect()
    try:
        print(f"[vbt] fetching M5 {symbol} {start.date()} -> {end.date()}")
        m5 = await fetcher.fetch_range(symbol, "M5", start, end)
        print(f"[vbt] fetched {len(m5)} M5 bars")
    finally:
        await fetcher.disconnect()
    return m5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD inclusive UTC (window start)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD exclusive UTC (window end)")
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--warmup-days", type=int, default=3, help="Lookback days before start for EMA/HTF warmup")
    ap.add_argument("--config", default="configs/strategies/irb_v5_m5_champion.yaml")
    ap.add_argument("--risk-fraction", type=float, default=0.0033, help="Match live NOVATRADE_RISK_FRACTION_OVERRIDE")
    ap.add_argument("--env-file", default="OUTPUT/novatrade.env.updated")
    ap.add_argument("--out-dir", default="OUTPUT/parity")
    args = ap.parse_args()

    _load_env_file(Path(args.env_file))
    cfg = NovaTradeCfg.load(env_file=Path(args.env_file))
    print(f"[vbt] metaapi account={cfg.metaapi.account_id} region={cfg.metaapi.region}")

    win_start = _parse_date(args.start)
    win_end = _parse_date(args.end)
    fetch_start = win_start - timedelta(days=args.warmup_days)

    m5 = asyncio.run(_fetch_m5(cfg, args.symbol, fetch_start, win_end))
    if not m5:
        print("[vbt] ERROR: empty bar data")
        return 1

    # --- Build config exactly as the live runner does (vault_native path) ---
    env = BacktestEnvironment.from_champion_config(args.config)
    env = replace(env, risk_fraction=args.risk_fraction)
    irb_cfg = env_to_irb_config(env)
    print(
        f"[vbt] cfg: htf={irb_cfg.htf_timeframe} irb%={irb_cfg.irb_percent} ema20={irb_cfg.ema20_len} "
        f"ema50={irb_cfg.ema50_len} expiry={irb_cfg.signal_expiry_bars} take_partial={irb_cfg.take_partial} "
        f"max_bars={irb_cfg.max_bars_in_trade} risk%={irb_cfg.risk_pct}"
    )

    # --- Prepare dataframe and drive the vault state machine bar-by-bar ---
    raw_df = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp(c.timestamp, unit="s", tz="UTC").tz_localize(None),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
            }
            for c in m5
        ]
    )
    df = prepare_dataframe(raw_df, irb_cfg)
    state = IRBStrategyState(irb_cfg)

    trades: list[dict] = []
    open_trade: dict | None = None
    prev_len = 0
    for i, row in df.iterrows():
        state.process_bar(i, row, df)
        new_events = state.trades[prev_len:]
        prev_len = len(state.trades)
        for ev in new_events:
            if ev.type == "entry":
                # state.position is set on the fill bar — read its stop + bar index.
                pos = state.position or {}
                open_trade = {
                    "entry_bar": pos.get("entry_bar", i),
                    "side": ev.side,
                    "entry_time": ev.time,
                    "entry_price": ev.price,
                    "stop_loss": pos.get("initial_stop", 0.0),
                    "legs": [],
                }
                trades.append(open_trade)
            elif ev.type == "exit":
                if open_trade is not None:
                    open_trade["legs"].append(ev)
                    if state.position is None:
                        open_trade = None

    # --- Filter to entries inside the window ---
    def _ts(t) -> datetime:
        # df 'time' is naive UTC; attach tz
        py = pd.Timestamp(t).to_pydatetime()
        return py.replace(tzinfo=timezone.utc) if py.tzinfo is None else py.astimezone(timezone.utc)

    in_window = [t for t in trades if win_start <= _ts(t["entry_time"]) < win_end]
    print(f"[vbt] total vault trades: {len(trades)}  in window [{args.start}..{args.end}): {len(in_window)}")

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
        "hold_bars",
        "outcome",
    ]
    rows_written = 0
    with trades_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for ti, t in enumerate(in_window):
            et = _ts(t["entry_time"])
            entry_iso = et.isoformat()
            side = t["side"]
            ep = t["entry_price"]
            sl = t["stop_loss"]
            if not t["legs"]:
                # Open trade at window end — emit entry-only row (outcome FLAT).
                w.writerow(
                    {
                        "trade_id": ti,
                        "side": side,
                        "entry_bar": t["entry_bar"],
                        "exit_bar": "",
                        "entry_timestamp": et.timestamp(),
                        "entry_iso": entry_iso,
                        "entry_price": ep,
                        "stop_loss": sl,
                        "exit_timestamp": "",
                        "exit_iso": "",
                        "exit_price": "",
                        "exit_reason": "OPEN",
                        "pnl_pips": 0.0,
                        "pnl_usd": 0.0,
                        "hold_bars": "",
                        "outcome": "FLAT",
                    }
                )
                rows_written += 1
                continue
            for ev in t["legs"]:
                xt = _ts(ev.time)
                if side == "long":
                    pnl_pips = (ev.price - ep) * 10000.0
                else:
                    pnl_pips = (ep - ev.price) * 10000.0
                w.writerow(
                    {
                        "trade_id": ti,
                        "side": side,
                        "entry_bar": t["entry_bar"],
                        "exit_bar": "",
                        "entry_timestamp": et.timestamp(),
                        "entry_iso": entry_iso,
                        "entry_price": ep,
                        "stop_loss": sl,
                        "exit_timestamp": xt.timestamp(),
                        "exit_iso": xt.isoformat(),
                        "exit_price": ev.price,
                        "exit_reason": ev.reason or "",
                        "pnl_pips": round(pnl_pips, 2),
                        "pnl_usd": round(ev.pnl or 0.0, 2),
                        "hold_bars": "",
                        "outcome": "WIN" if pnl_pips > 0 else ("LOSS" if pnl_pips < 0 else "FLAT"),
                    }
                )
                rows_written += 1
    print(f"[vbt] wrote {trades_csv} ({rows_written} leg rows)")

    summary = {
        "engine": "vault_native (run_vault_strategy_batch)",
        "symbol": args.symbol,
        "window_start": args.start,
        "window_end": args.end,
        "warmup_days": args.warmup_days,
        "config": args.config,
        "risk_fraction": args.risk_fraction,
        "m5_bars": len(m5),
        "total_trades": len(trades),
        "trades_in_window": len(in_window),
        "out": str(trades_csv),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
