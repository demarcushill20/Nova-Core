"""Parity hypothesis test: Pine v5 entries are all at HH:00 timestamps,
implying the certified Pine chart was H1 (not M5). Re-run the Python engine
with primary=H1, higher=H4 (derived from H1), full Pine-aligned config plus
EMA-stack filter on, and stagnation guard on.

If this gets us close to Pine's 1,204 trades / +$8,887 / PF 1.08, the residual
6× signal-rate gap was driven by chart timeframe.
"""

import csv
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.cli.commands.data import aggregate_h1_to_h4
from novatrade.cli.config_schema import StrategyConfig
from novatrade.models import Candle

REPO = Path("/home/nova/nova-core")
H1_CSV = REPO / "data/candles/EURUSD_H1_10yr.csv"
CFG = REPO / "configs/strategies/irb_v5_m5_pine_aligned.yaml"


def parse_ts(s):
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def load(path, sym, tf):
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            out.append(
                Candle(
                    timestamp=parse_ts(r["timestamp"]),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r.get("volume") or 0),
                    symbol=sym,
                    timeframe=tf,
                )
            )
    return sorted(out, key=lambda c: c.timestamp)


def main():
    print(f"[load] H1: {H1_CSV.name}")
    h1 = load(H1_CSV, "EURUSD", "H1")
    print(f"        {len(h1):,} bars")

    print("[derive] H4 ← H1 aggregation")
    h4 = aggregate_h1_to_h4(h1)
    print(f"        {len(h4):,} bars")

    print(f"[config] {CFG.name}")
    cfg = StrategyConfig.from_yaml(CFG)
    env_kwargs = cfg.to_environment_kwargs()
    # Pine v5 source has the EMA-stack filter ALWAYS ON (line 399).
    # Override the YAML/champion default which leaves it off.
    env_kwargs["use_ema_stack_filter"] = True

    # Use H1/H4 primary/higher (matches Pine's HH:00 entry timestamps)
    base = BacktestEnvironment.for_v5_m5()  # gives v5 defaults
    valid = {k: v for k, v in env_kwargs.items() if k in base.__dataclass_fields__}
    env = replace(
        base,
        primary_timeframe="H1",
        higher_timeframe="H4",
        h1_to_h4_ratio=4,
        h1_bars_per_day=24,
        h4_bars_per_day=6,
        **valid,
    )

    print(
        f"[env] primary={env.primary_timeframe}  higher={env.higher_timeframe}  "
        f"adx≥{env.adx_threshold} mtf_lookback={env.mtf_lookback} "
        f"trigger_window={env.trigger_window_bars} time_stop={env.time_stop_bars}"
    )

    print("[run] IRBBacktester (H1 chart)...")
    bt = IRBBacktester(env=env)
    res = bt.run(h1, h4)

    trades = res.trades
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd < 0]
    gross_w = sum(t.pnl_usd for t in wins)
    gross_l = -sum(t.pnl_usd for t in losses)
    pf = gross_w / gross_l if gross_l > 0 else float("inf")
    net = sum(t.pnl_usd for t in trades)

    print()
    print("Pine target: 1,204 trades · WR 19.4% · PF 1.078 · Net +$8,887")
    print(
        f"Python (H1): {len(trades):,} trades · WR {len(wins) / max(len(trades), 1) * 100:.1f}% · "
        f"PF {pf:.3f} · Net ${net:,.0f}"
    )
    print(f"Trade-count ratio vs Pine: {len(trades) / 1204:.2f}x")
    print()
    rj = res.filter_rejections
    print(
        f"Rejections: irb={rj.irb_geometry:,}  trend={rj.trend_filter:,}  "
        f"mtf={rj.mtf_alignment:,}  sideways={rj.sideways_filter:,}  "
        f"overext={rj.overextension_filter:,}  existing_pos={rj.existing_position:,}"
    )

    # Sample first few entries to see if they land at HH:00 like Pine
    print()
    print("First 10 entry timestamps (by entry_bar index):")
    for t in trades[:10]:
        eb = t.entry_bar
        if 0 <= eb < len(h1):
            ts = h1[eb].timestamp
            dt = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            print(f"  trade_id={t.trade_id:4d}  side={t.side.name:5s}  entry_bar_ts={dt}  pnl=${t.pnl_usd:+.0f}")

    # Exit reason mix
    from collections import Counter

    er = Counter(t.exit_reason.name for t in trades)
    print()
    print("Exit reason mix:")
    for r, n in er.most_common():
        print(f"  {r:<14} {n:>5}  ({n / len(trades) * 100:5.1f}%)")


if __name__ == "__main__":
    main()
