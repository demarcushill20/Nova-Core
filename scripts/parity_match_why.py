"""Per-bar rejection-reason tagger for the pine_only set.

Runs the Python IRBBacktester with a recording wrapper around
FilterRejection that captures (bar_index, reason) for every rejection.
Then for each pine_only trade (Pine accepted, Python rejected at the same
H1 timestamp), looks up which rejection reasons fired on that bar — the
one closest in time to Pine's entry timestamp.

Output: histogram of rejection reasons across all pine_only entries.
That tells us which filter is the primary leak.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.backtest.metrics import FilterRejection
from novatrade.cli.commands.data import aggregate_h1_to_h4
from novatrade.cli.config_schema import StrategyConfig
from novatrade.models import Candle

REPO = Path("/home/nova/nova-core")
H1_CSV = REPO / "data/candles/EURUSD_H1_10yr.csv"
CFG = REPO / "configs/strategies/irb_v5_m5_pine_aligned.yaml"
PINE_CSV = REPO / "data/irb_novatrade_irb_v5_results_extracted.csv"


class RecordingFilterRejection(FilterRejection):
    """Drop-in for FilterRejection that records per-bar rejection events.

    Engine increments via `self._rejections.<reason> += 1`, which desugars
    to attribute set on the dataclass. We override __setattr__ to log the
    (current_bar, reason) tuple before delegating to the parent.
    """

    def __init__(self, engine_ref):
        super().__init__()
        # Use object.__setattr__ to bypass our own override during init
        object.__setattr__(self, "_engine_ref", engine_ref)
        object.__setattr__(self, "_log", [])

    def __setattr__(self, name, value):
        # Only record actual integer field counter increments — guard against
        # writes to internal fields like _engine_ref or _log itself.
        if name in self.__dataclass_fields__ and isinstance(value, int):
            try:
                bar = self._engine_ref._cur_bar
                # Detect the increment direction (always +1 by engine convention).
                # Record as (bar, reason).
                self._log.append((bar, name))
            except AttributeError:
                pass  # _cur_bar not yet stashed (pre-warmup)
        object.__setattr__(self, name, value)


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
    """Return (entry_ts, side, pnl_usd) tuples — entry rows only."""
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


def main() -> None:
    print(f"[load] Pine baseline: {PINE_CSV.name}")
    pine = load_pine(PINE_CSV)
    print(f"        {len(pine):,} entries")

    print(f"[load] H1 candles: {H1_CSV.name}")
    h1 = load_candles(H1_CSV, "EURUSD", "H1")
    h4 = aggregate_h1_to_h4(h1)
    print(f"        {len(h1):,} H1, {len(h4):,} H4 (derived)")

    # ts -> bar index
    ts_to_bar: dict[int, int] = {int(c.timestamp): i for i, c in enumerate(h1)}

    print("[run] Python engine (recording rejections per bar)")
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
    # Swap in recording wrapper before run() so all increments are captured
    recorder = RecordingFilterRejection(bt)
    bt._rejections = recorder
    res = bt.run(h1, h4)
    log: list[tuple[int, str]] = recorder._log  # type: ignore[attr-defined]
    print(f"        {len(res.trades):,} Python trades, {len(log):,} rejection events")

    # Index rejection events by bar
    rejections_by_bar: dict[int, list[str]] = defaultdict(list)
    for bar, reason in log:
        rejections_by_bar[bar].append(reason)

    # Build the pine_only set: Pine entries whose (ts, side) is not in Python's run
    py_keys: set[tuple[int, str]] = set()
    for t in res.trades:
        if not (0 <= t.entry_bar < len(h1)):
            continue
        ts = int(h1[t.entry_bar].timestamp)
        side = "Long" if t.side.value.lower() == "long" else "Short"
        py_keys.add((ts, side))

    pine_only: list[tuple[int, str, float]] = [(ts, s, p) for (ts, s, p) in pine if (ts, s) not in py_keys]
    print(f"        {len(pine_only):,} pine_only entries to attribute")

    # For each pine_only entry, find rejection reasons at that bar.
    # Pine reports the entry timestamp at the bar AFTER the IRB-detection bar
    # (Pine fills next-bar at the IRB-extreme break). So the most relevant
    # diagnostic bar is the one ~1 bar BEFORE Pine's entry timestamp — but we
    # don't know exactly. Search a small window: bar(ts), bar(ts)-1, bar(ts)-2.
    reasons_first_hit: Counter = Counter()
    reasons_any_hit: Counter = Counter()
    no_rejection_at_bar = 0
    not_in_h1 = 0

    for entry_ts, _side, _pnl in pine_only:
        if entry_ts not in ts_to_bar:
            not_in_h1 += 1
            continue
        bar_i = ts_to_bar[entry_ts]
        # Search current bar + 2 prior (Pine's IRB+fill window)
        all_reasons_in_window: list[str] = []
        for b in (bar_i, bar_i - 1, bar_i - 2):
            if b < 0:
                continue
            all_reasons_in_window.extend(rejections_by_bar.get(b, []))

        if not all_reasons_in_window:
            no_rejection_at_bar += 1
            continue

        # Count first hit (the earliest filter to reject in the chain — usually
        # the most diagnostic), and ALL hits in the window.
        reasons_first_hit[all_reasons_in_window[0]] += 1
        for r in set(all_reasons_in_window):
            reasons_any_hit[r] += 1

    print()
    print("=" * 70)
    print("REJECTION REASONS — pine_only entries (first hit per bar window)")
    print("=" * 70)
    total_attributed = sum(reasons_first_hit.values())
    print(
        f"  attributed: {total_attributed:,} / {len(pine_only):,} pine_only "
        f"({total_attributed / max(len(pine_only), 1) * 100:.1f}%)"
    )
    print(f"  not in h1 timeline: {not_in_h1}")
    print(f"  bar had no recorded rejection: {no_rejection_at_bar}")
    print()
    print(f"  {'reason':<25}{'first-hit count':>18}{'% of attributed':>18}")
    for reason, count in reasons_first_hit.most_common():
        pct = count / max(total_attributed, 1) * 100
        print(f"  {reason:<25}{count:>18,}{pct:>17.1f}%")

    print()
    print("=" * 70)
    print("REJECTION REASONS — pine_only entries (any hit in 3-bar window)")
    print("=" * 70)
    print(f"  {'reason':<25}{'any-hit count':>18}")
    for reason, count in reasons_any_hit.most_common():
        print(f"  {reason:<25}{count:>18,}")

    # Also show aggregate engine rejections for comparison
    print()
    print("=" * 70)
    print("ENGINE-WIDE REJECTION TOTALS (full 10yr run)")
    print("=" * 70)
    total_log = Counter(r for _, r in log)
    for reason, count in total_log.most_common():
        print(f"  {reason:<25}{count:>10,}")


if __name__ == "__main__":
    main()
