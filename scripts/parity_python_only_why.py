"""For each python_only entry (Python took, Pine didn't), inspect what
filter Pine would have used to reject. We don't have Pine's per-bar filter
log, but we can identify likely culprits by the entry's properties:

  - Was the H4 trend (Pine semantic, [20] H1 bars back) actually rising/
    falling at the entry direction? If NOT → Pine's MTF would have rejected
    these too, but Python's MTF (now correctly using 5 H4 bars on this
    config) somehow let them through. Sanity-check.
  - What is Python's pnl_pips and exit_reason distribution for python_only?
    Heavy STOP_LOSS-loss tail = Python's filters too lenient.
  - Are these entries during low-ADX (sideways) regimes? Pine's sideways
    filter may be tighter.
  - Are these entries at extreme overextension? Pine's OE may be tighter.

Goal: surface concrete evidence of which Pine filter is stricter than
Python's. Whichever filter has the most divergent characteristic on the
python_only set is the next leak to fix.
"""

from __future__ import annotations

import csv
import math
import statistics
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from novatrade.backtest.engine import (
    IRBBacktester,
    compute_adx,
    compute_atr,
    compute_ema,
)
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.cli.commands.data import aggregate_h1_to_h4
from novatrade.cli.config_schema import StrategyConfig
from novatrade.models import Candle

REPO = Path("/home/nova/nova-core")
H1_CSV = REPO / "data/candles/EURUSD_H1_10yr.csv"
CFG = REPO / "configs/strategies/irb_v5_m5_pine_aligned.yaml"
PINE_CSV = REPO / "data/irb_novatrade_irb_v5_results_extracted.csv"


def parse_ts(s: str) -> int:
    s = s.strip()
    try:
        return int(float(s))
    except ValueError:
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


def load_pine(path: Path) -> list[tuple[int, str, float]]:
    trades: list[tuple[int, str, float]] = []
    with open(path) as f:
        for r in csv.DictReader(f):
            row_type = r["Type"].strip()
            if not row_type.startswith("Entry"):
                continue
            ts = parse_ts(r["Date and time"])
            side = "Long" if "long" in row_type else "Short"
            pnl = float(r["Net P&L USD"])
            trades.append((ts, side, pnl))
    return trades


def main() -> None:  # noqa: C901
    print(f"[load] Pine: {PINE_CSV.name}")
    pine = load_pine(PINE_CSV)
    print(f"[load] H1: {H1_CSV.name}")
    h1 = load_candles(H1_CSV, "EURUSD", "H1")
    h4 = aggregate_h1_to_h4(h1)
    print(f"        H1: {len(h1):,}, H4: {len(h4):,}")

    print("[run] Python engine")
    cfg = StrategyConfig.from_yaml(CFG)
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
        max_volume=1.0,
        **valid,
    )
    bt = IRBBacktester(env=env)
    res = bt.run(h1, h4)

    # Indicators on H1 (mirror engine.run() pre-compute) for ADX / ATR / EMA
    h1_closes = [c.close for c in h1]
    h4_closes = [c.close for c in h4]

    adx_h1 = compute_adx(h1, env.adx_period) if h1 else []
    atr_h1 = compute_atr(h1, env.atr_period) if h1 else []
    ema_slow = compute_ema(h1_closes, env.ema_slow_period) if h1 else []
    ema_h4 = compute_ema(h4_closes, env.ema_period) if h4 else []

    pine_keys = {(t[0], t[1]) for t in pine}
    py_keys: set[tuple[int, str]] = set()
    py_by_key = {}
    for t in res.trades:
        if not (0 <= t.entry_bar < len(h1)):
            continue
        ts = int(h1[t.entry_bar].timestamp)
        side = "Long" if t.side.value.lower() == "long" else "Short"
        py_keys.add((ts, side))
        py_by_key[(ts, side)] = t

    python_only_keys = py_keys - pine_keys

    # Properties to capture per python_only entry
    pnl_distribution: list[float] = []
    exit_reasons: Counter = Counter()
    h4_pine_aligned: list[bool] = []  # would Pine's MTF (H1-indexed [20]) accept?
    adx_at_entry: list[float] = []  # for sideways/sideways assessment
    h1_oe: list[float] = []  # overextension proxy: |close - ema_slow| / atr at entry

    ts_to_bar: dict[int, int] = {int(c.timestamp): i for i, c in enumerate(h1)}

    for ts, side in python_only_keys:
        if ts not in ts_to_bar:
            continue
        i = ts_to_bar[ts]
        pos = py_by_key[(ts, side)]
        pnl_distribution.append(pos.pnl_pips)
        exit_reasons[pos.exit_reason.value if hasattr(pos.exit_reason, "value") else str(pos.exit_reason)] += 1

        # Pine MTF: ema_h4 at h4_idx vs ema_h4 at h4_idx_at_(i-20)
        if i - 20 >= 0:
            h4_idx_now = i // 4 if 4 > 0 else 0  # mirror h4_map naive lookup
            h4_idx_prev = (i - 20) // 4
            if 0 <= h4_idx_now < len(ema_h4) and 0 <= h4_idx_prev < len(ema_h4):
                e_now = ema_h4[h4_idx_now]
                e_prev = ema_h4[h4_idx_prev]
                if not (math.isnan(e_now) or math.isnan(e_prev)):
                    if side == "Long":
                        h4_pine_aligned.append(e_now > e_prev)
                    else:
                        h4_pine_aligned.append(e_now < e_prev)

        # ADX at entry
        if 0 <= i < len(adx_h1) and not math.isnan(adx_h1[i]):
            adx_at_entry.append(adx_h1[i])

        # Overextension proxy
        if (
            0 <= i < len(ema_slow)
            and not math.isnan(ema_slow[i])
            and 0 <= i < len(atr_h1)
            and not math.isnan(atr_h1[i])
            and atr_h1[i] > 0
        ):
            h1_oe.append(abs(h1[i].close - ema_slow[i]) / atr_h1[i])

    print()
    print("=" * 70)
    print(f"PYTHON_ONLY ENTRIES (Python took, Pine didn't): {len(python_only_keys):,}")
    print("=" * 70)

    if pnl_distribution:
        wins = [p for p in pnl_distribution if p > 0]
        losses = [p for p in pnl_distribution if p < 0]
        print("\n  PnL pips distribution:")
        print(f"    {len(wins)}W / {len(losses)}L  ({len(wins) / len(pnl_distribution) * 100:.1f}% WR)")
        print(f"    median: {statistics.median(pnl_distribution):+.1f}  mean: {statistics.mean(pnl_distribution):+.1f}")
        if losses:
            print(f"    median loss: {statistics.median(losses):.1f}  worst loss: {min(losses):.1f}")
        if wins:
            print(f"    median win:  {statistics.median(wins):.1f}  best win:  {max(wins):.1f}")

    print("\n  Exit reasons:")
    for reason, count in exit_reasons.most_common():
        print(f"    {reason:<20}{count:>5}  ({count / len(python_only_keys) * 100:.1f}%)")

    print("\n  Pine-MTF (H1-indexed [20]) alignment at entry direction:")
    if h4_pine_aligned:
        aligned = sum(1 for b in h4_pine_aligned if b)
        aligned_pct = aligned / len(h4_pine_aligned) * 100
        print(f"    {aligned} / {len(h4_pine_aligned)} ({aligned_pct:.1f}%) WOULD pass Pine's MTF")
        misaligned = len(h4_pine_aligned) - aligned
        misaligned_pct = misaligned / len(h4_pine_aligned) * 100
        print(f"    {misaligned} / {len(h4_pine_aligned)} ({misaligned_pct:.1f}%) WOULD be rejected by Pine's MTF")
        print("       (these are pure false-positives the engine still passes)")

    print("\n  ADX at entry (Pine sideways_filter rejects when ADX < 20):")
    if adx_at_entry:
        below = sum(1 for a in adx_at_entry if a < 20)
        print(f"    median: {statistics.median(adx_at_entry):.1f}  mean: {statistics.mean(adx_at_entry):.1f}")
        below_pct = below / len(adx_at_entry) * 100
        print(f"    {below} / {len(adx_at_entry)} ({below_pct:.1f}%) entries had ADX < 20 (would Pine reject?)")

    print("\n  Overextension proxy |close - ema_slow| / ATR at entry:")
    if h1_oe:
        oe_p90 = sorted(h1_oe)[int(len(h1_oe) * 0.9)]
        print(f"    median: {statistics.median(h1_oe):.2f}  p90: {oe_p90:.2f}  max: {max(h1_oe):.2f}")
        gt2 = sum(1 for x in h1_oe if x > 2.0)
        print(
            f"    {gt2} / {len(h1_oe)} ({gt2 / len(h1_oe) * 100:.1f}%) entries had OE > 2.0 (Pine OE_THRESH may be 2.0)"
        )

    print()
    print("=" * 70)
    print("INTERPRETATION")
    print("=" * 70)
    if h4_pine_aligned:
        misaligned_pct = (len(h4_pine_aligned) - sum(1 for b in h4_pine_aligned if b)) / len(h4_pine_aligned) * 100
        if misaligned_pct > 30:
            print(
                f"  • {misaligned_pct:.0f}% of python_only entries WOULD fail Pine's MTF — Python's "
                "engine MTF check has a gap (likely the off-by-one or boundary case in "
                "h4_map[i - mtf_lookback] mapping). Investigate."
            )
        else:
            print(f"  • {misaligned_pct:.0f}% Pine-MTF-misaligned. MTF parity is mostly correct.")

    if adx_at_entry:
        below_pct = sum(1 for a in adx_at_entry if a < 20) / len(adx_at_entry) * 100
        if below_pct > 25:
            print(f"  • {below_pct:.0f}% had ADX < 20 — Pine's adx_threshold may be tighter (try 22 or 25).")

    if h1_oe:
        gt2_pct = sum(1 for x in h1_oe if x > 2.0) / len(h1_oe) * 100
        if gt2_pct > 20:
            print(f"  • {gt2_pct:.0f}% had OE > 2.0 — Pine's overextension cap may be tighter.")


if __name__ == "__main__":
    main()
