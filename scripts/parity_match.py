"""Per-trade matching: Pine v5 baseline vs Python IRBBacktester.

Joins the Pine CSV trade log with the Python engine's run on the same H1
EURUSD data, then partitions trades into:
  - matched      : same (entry_ts, side) in both; report pip divergence
  - pine_only    : Pine took it, Python did not (entry-filter divergence)
  - python_only  : Python took it, Pine did not (false positive in Python)

This is the diagnostic that points to where the remaining parity gap lives,
post engine-stop-divergence fix (cb82340 on main):
  PF 0.945 vs Pine 1.078; trade count 758 vs Pine 1,204.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.cli.commands.data import aggregate_h1_to_h4
from novatrade.cli.config_schema import StrategyConfig
from novatrade.models import Candle

REPO = Path("/home/nova/nova-core")
H1_CSV = REPO / "data/candles/EURUSD_H1_10yr.csv"
CFG = REPO / "configs/strategies/irb_v5_m5_pine_aligned.yaml"
PINE_CSV = REPO / "data/irb_novatrade_irb_v5_results_extracted.csv"


@dataclass
class PineTrade:
    n: int
    entry_ts: int
    side: str  # "Long" or "Short"
    exit_ts: int
    entry_price: float
    exit_price: float
    pnl_usd: float


def parse_ts(s: str) -> int:
    s = s.strip()
    try:
        return int(float(s))
    except ValueError:
        # Pine CSV uses "YYYY-MM-DD HH:MM" (no seconds)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                continue
        raise


def load_candles(path: Path, sym: str, tf: str) -> list[Candle]:
    out: list[Candle] = []
    with open(path) as f:
        for r in csv.DictReader(f):
            out.append(
                Candle(
                    timestamp=float(parse_ts(r["timestamp"])),
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


def load_pine(path: Path) -> list[PineTrade]:
    """Pair Entry/Exit rows from the Pine CSV into PineTrade records."""
    by_n: dict[int, dict] = defaultdict(dict)
    with open(path) as f:
        for r in csv.DictReader(f):
            n = int(r["Trade #"])
            row_type = r["Type"].strip()  # "Entry long" / "Exit long" / etc.
            ts = parse_ts(r["Date and time"])
            price = float(r["Price USD"])
            pnl = float(r["Net P&L USD"])
            if row_type.startswith("Entry"):
                by_n[n]["entry_ts"] = ts
                by_n[n]["side"] = "Long" if "long" in row_type else "Short"
                by_n[n]["entry_price"] = price
                by_n[n]["pnl_usd"] = pnl  # mirrored on both rows
            elif row_type.startswith("Exit"):
                by_n[n]["exit_ts"] = ts
                by_n[n]["exit_price"] = price
    trades: list[PineTrade] = []
    for n, d in sorted(by_n.items()):
        if "entry_ts" not in d or "exit_ts" not in d:
            continue
        trades.append(
            PineTrade(
                n=n,
                entry_ts=d["entry_ts"],
                side=d["side"],
                exit_ts=d["exit_ts"],
                entry_price=d["entry_price"],
                exit_price=d["exit_price"],
                pnl_usd=d["pnl_usd"],
            )
        )
    return trades


def run_python(h1: list[Candle], h4: list[Candle], cfg: StrategyConfig) -> list:
    env_kwargs = cfg.to_environment_kwargs()
    env_kwargs["use_ema_stack_filter"] = True
    base = BacktestEnvironment.for_v5_m5()
    valid = {k: v for k, v in env_kwargs.items() if k in base.__dataclass_fields__}
    env = replace(
        base,
        primary_timeframe="H1",
        higher_timeframe="H4",
        h1_to_h4_ratio=4,
        h1_bars_per_day=24,
        h4_bars_per_day=6,
        min_volume=1.0,
        max_volume=1.0,  # fixed-1-lot to match Pine sizing
        **valid,
    )
    bt = IRBBacktester(env=env)
    res = bt.run(h1, h4)
    return res.trades


def main() -> None:
    print(f"[load] Pine baseline: {PINE_CSV.name}")
    pine = load_pine(PINE_CSV)
    print(
        f"        {len(pine):,} trades, "
        f"{datetime.fromtimestamp(pine[0].entry_ts, tz=timezone.utc).date()} → "
        f"{datetime.fromtimestamp(pine[-1].entry_ts, tz=timezone.utc).date()}"
    )

    print(f"[load] H1 candles: {H1_CSV.name}")
    h1 = load_candles(H1_CSV, "EURUSD", "H1")
    print(f"        {len(h1):,} bars")
    h4 = aggregate_h1_to_h4(h1)
    print(f"[derive] H4: {len(h4):,} bars")

    print("[run] Python engine (Pine-aligned config, fixed 1-lot)")
    cfg = StrategyConfig.from_yaml(CFG)
    py_trades = run_python(h1, h4, cfg)
    print(f"        {len(py_trades):,} Python trades")

    # Index Python trades by (entry_ts_seconds, side)
    py_by_key: dict[tuple[int, str], Any] = {}
    for t in py_trades:
        if not (0 <= t.entry_bar < len(h1)):
            continue
        ts = int(h1[t.entry_bar].timestamp)
        side = "Long" if t.side.value.lower() == "long" else "Short"
        key = (ts, side)
        py_by_key[key] = t

    pine_keys = {(t.entry_ts, t.side) for t in pine}
    py_keys = set(py_by_key.keys())

    matched = pine_keys & py_keys
    pine_only = pine_keys - py_keys
    python_only = py_keys - pine_keys

    print()
    print("=" * 70)
    print("PARTITION")
    print("=" * 70)
    print(f"  matched         : {len(matched):4d}  (Pine ∩ Python by entry_ts+side)")
    print(f"  pine_only       : {len(pine_only):4d}  (Pine took, Python missed)")
    print(f"  python_only     : {len(python_only):4d}  (Python took, Pine didn't)")
    print(f"  Pine total      : {len(pine):4d}")
    print(f"  Python total    : {len(py_trades):4d}")
    print()

    # Pine-only sample (the highest-leverage diagnostic — what filter is rejecting?)
    pine_by_key = {(t.entry_ts, t.side): t for t in pine}
    print("=" * 70)
    print("SAMPLE: pine_only (first 20) — Pine took, Python missed")
    print("=" * 70)
    print(f"  {'#':<6}{'entry_ts (UTC)':<22}{'side':<8}{'entry':<10}{'pnl_usd':>10}")
    for key in sorted(pine_only)[:20]:
        t = pine_by_key[key]
        ts_str = datetime.fromtimestamp(t.entry_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"  {t.n:<6}{ts_str:<22}{t.side:<8}{t.entry_price:<10.5f}{t.pnl_usd:>+10.2f}")

    # Pine-only PnL breakdown — would these have been wins or losses?
    pine_only_trades = [pine_by_key[k] for k in pine_only]
    if pine_only_trades:
        wins = [t for t in pine_only_trades if t.pnl_usd > 0]
        losses = [t for t in pine_only_trades if t.pnl_usd < 0]
        print()
        print(
            f"  pine_only PnL : {len(wins)}W / {len(losses)}L = "
            f"{len(wins) / len(pine_only_trades) * 100:.1f}% WR  "
            f"(total ${sum(t.pnl_usd for t in pine_only_trades):+,.0f})"
        )

    print()

    # Python-only sample
    print("=" * 70)
    print("SAMPLE: python_only (first 10) — Python took, Pine didn't")
    print("=" * 70)
    print(f"  {'entry_ts (UTC)':<22}{'side':<8}{'pnl_pips':>10}{'pnl_usd':>12}{'exit_reason':<18}")
    for key in sorted(python_only)[:10]:
        t = py_by_key[key]
        ts_str = datetime.fromtimestamp(key[0], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        side = key[1]
        reason = t.exit_reason.value if hasattr(t.exit_reason, "value") else str(t.exit_reason)
        print(f"  {ts_str:<22}{side:<8}{t.pnl_pips:>+10.2f}{t.pnl_usd:>+12,.2f}  {reason}")

    # Matched trades — pip divergence
    print()
    print("=" * 70)
    print("MATCHED TRADES — pnl_usd divergence")
    print("=" * 70)
    diffs = []
    for key in matched:
        pine_t = pine_by_key[key]
        py_t = py_by_key[key]
        diff = py_t.pnl_usd - pine_t.pnl_usd
        diffs.append((key, pine_t, py_t, diff))
    diffs.sort(key=lambda x: -abs(x[3]))
    print(f"  {'entry_ts (UTC)':<22}{'side':<8}{'pine $':>10}{'py $':>10}{'diff':>10}")
    for key, pt, yt, diff in diffs[:10]:
        ts_str = datetime.fromtimestamp(key[0], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"  {ts_str:<22}{key[1]:<8}{pt.pnl_usd:>+10.2f}{yt.pnl_usd:>+10.2f}{diff:>+10.2f}")

    if diffs:
        avg_diff = sum(d[3] for d in diffs) / len(diffs)
        max_abs = max(abs(d[3]) for d in diffs)
        print()
        print(
            f"  matched avg_diff={avg_diff:+,.2f}  max_abs={max_abs:,.2f}  "
            f"(positive = Python more profitable than Pine)"
        )

    print()
    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    if len(pine_only) > len(python_only):
        print(
            f"  Primary leak: {len(pine_only)} pine_only trades → entry-side filter "
            "rejecting setups Pine accepts. Inspect first 20 above for patterns."
        )
    else:
        print(
            f"  Primary leak: {len(python_only)} python_only trades → engine taking "
            "trades Pine filters out. False-positive entries."
        )
    print(f"  Coverage: {len(matched) / len(pine) * 100:.1f}% of Pine trades matched.")


if __name__ == "__main__":
    main()
