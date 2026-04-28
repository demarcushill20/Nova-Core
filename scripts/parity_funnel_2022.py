"""Diagnostic: run the Python engine on 2022 only (where Pine fired 108 trades,
PF 2.06) and print the full filter-rejection funnel + counts to localize where
the engine is over-firing vs Pine v5.
"""

import csv
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.cli.config_schema import StrategyConfig
from novatrade.models import Candle

REPO = Path("/home/nova/nova-core")
M5_CSV = REPO / "data/candles/EURUSD_M5_10yr.csv"
H1_CSV = REPO / "data/candles/EURUSD_H1_10yr.csv"
CONFIG_PATH = REPO / "configs/strategies/irb_v5_m5_champion.yaml"


def parse_ts(s: str) -> float:
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def load(path, sym, tf, start_ts, end_ts):
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            ts = parse_ts(r["timestamp"]) if not r["timestamp"].replace(".", "").isdigit() else float(r["timestamp"])
            if ts < start_ts or ts > end_ts:
                continue
            out.append(
                Candle(
                    timestamp=ts,
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r.get("volume") or 0),
                    symbol=sym,
                    timeframe=tf,
                )
            )
    return out


def main():
    # 2022 with 30-day warmup
    start = datetime(2021, 12, 1, tzinfo=timezone.utc).timestamp()
    end = datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp()

    print("Loading 2022 window (with 30d warmup) ...")
    m5 = load(M5_CSV, "EURUSD", "M5", start, end)
    h1 = load(H1_CSV, "EURUSD", "H1", start, end)
    print(f"  M5 bars: {len(m5):,}")
    print(f"  H1 bars: {len(h1):,}")

    cfg = StrategyConfig.from_yaml(CONFIG_PATH)
    env = replace(BacktestEnvironment.for_v5_m5(), **cfg.to_environment_kwargs())

    bt = IRBBacktester(env=env)
    res = bt.run(m5, h1)

    # In-2022 trades only (filter on entry_bar timestamp)
    jan22 = datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp()
    trades_2022 = []
    for t in res.trades:
        idx = t.entry_bar
        if 0 <= idx < len(m5) and m5[idx].timestamp >= jan22 and m5[idx].timestamp < end:
            trades_2022.append(t)

    print()
    print("=== Signal funnel (full window, including warmup) ===")
    print(f"  Total M5 bars:          {res.total_bars:,}")
    print(f"  Signals (passed all):   {len(res.signals):,}")
    rj = res.filter_rejections
    print("  Rejections by stage:")
    print(f"    irb_geometry:         {rj.irb_geometry:,}")
    print(f"    trend_filter:         {rj.trend_filter:,}")
    print(f"    mtf_alignment:        {rj.mtf_alignment:,}")
    print(f"    sideways_filter:      {rj.sideways_filter:,}")
    print(f"    overextension_filter: {rj.overextension_filter:,}")
    print(f"    existing_position:    {rj.existing_position:,}")
    print(f"    existing_pending:     {rj.existing_pending:,}")
    print(f"    warmup:               {rj.warmup:,}")
    print(f"    session_filter:       {rj.session_filter:,}")
    print(f"    volatility_filter:    {rj.volatility_filter:,}")
    print(f"    circuit_breaker:      {rj.circuit_breaker:,}")
    print()
    print("=== Trades in 2022 ===")
    print(f"  Python:   {len(trades_2022):,}")
    print("  Pine:     108")
    print(f"  Ratio:    {len(trades_2022) / 108:.1f}x")
    print()

    # Trades by month within 2022
    from collections import Counter

    by_month = Counter()
    pnl_by_month = {}
    for t in trades_2022:
        idx = t.entry_bar
        m = datetime.utcfromtimestamp(m5[idx].timestamp).month
        by_month[m] += 1
        pnl_by_month.setdefault(m, []).append(t.pnl_usd)
    print("  Python 2022 monthly:")
    print(f"  {'Mo':>3} {'Trades':>7} {'Net P&L':>12}")
    for m in sorted(by_month):
        n = by_month[m]
        net = sum(pnl_by_month[m])
        print(f"  {m:>3} {n:>7} {net:>+12,.0f}")

    # Sample first 10 entry signals to inspect
    print()
    print("First 5 signals (Python, 2022):")
    sigs_2022 = [
        s
        for s in res.signals
        if 0 <= s.bar_index < len(m5) and m5[s.bar_index].timestamp >= jan22 and m5[s.bar_index].timestamp < end
    ]
    print(f"  Total 2022 signals: {len(sigs_2022)}")
    for s in sigs_2022[:5]:
        b = m5[s.bar_index]
        dt = datetime.utcfromtimestamp(b.timestamp).strftime("%Y-%m-%d %H:%M")
        print(
            f"  {dt}  side={s.side.name}  irb_range_pips={s.irb_range / env.pip_value:.1f}  "
            f"adx={s.adx_value:.1f}  overext={s.overextension_ratio:.2f}"
        )


if __name__ == "__main__":
    main()
