"""For each Bucket-B pine_only entry, look at Python's rejection log at the
EXACT IRB bar (entry_ts - offset), not the 3-bar window around entry_ts.

Goal: distinguish H1 (pre-IRB filter ate the IRB bar) from H2 (Python
detected the IRB but lost the pending) for the 325 Bucket-B cases.

Categorization at exact IRB bar:
  - existing_position rejection : Python in non-FLAT state at IRB bar
  - volatility_filter rejection : volatility filter too aggressive
  - session_filter rejection    : session filter blocking the bar
  - trend_filter rejection      : EMA stack / ADX rejecting at IRB bar
  - mtf_alignment rejection     : H4 trend mismatch at IRB bar
  - irb_geometry rejection      : Python's IRB equation said NO (math divergence)
  - sideways/overextension      : remaining filter buckets
  - circuit_breaker             : disabled by recent loss streak
  - warmup                      : bar inside warmup
  - NO rejection at IRB bar     : Python was silent — either in PENDING/LONG/SHORT
                                  (silent paths) or successfully detected the IRB
                                  and entered S_PLONG (also silent — increment is
                                  the only thing that fires, and detection doesn't
                                  increment any counter)

The "NO rejection" bucket is ambiguous: Python may have detected the IRB
and set pending, then lost it (H2). To disambiguate, also check whether
Python entered ANY position whose entry_bar is in [IRB_bar+1, entry_ts]
window — if yes, Python detected a different (or the same) IRB but
attached to a different entry; if no, Python set pending and lost it.
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

IRB_PCT = 0.45
PIP_BUF = 0.0001
TRIGGER_WIN = 20


class RecordingFilterRejection(FilterRejection):
    def __init__(self, engine_ref):
        super().__init__()
        object.__setattr__(self, "_engine_ref", engine_ref)
        object.__setattr__(self, "_log", [])

    def __setattr__(self, name, value):
        if name in self.__dataclass_fields__ and isinstance(value, int):
            try:
                bar = self._engine_ref._cur_bar
                self._log.append((bar, name))
            except AttributeError:
                pass
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


def is_up_irb(c: Candle) -> bool:
    rng = c.high - c.low
    if rng <= 0:
        return False
    thr = c.high - IRB_PCT * rng
    return c.open <= thr and c.close <= thr


def is_dn_irb(c: Candle) -> bool:
    rng = c.high - c.low
    if rng <= 0:
        return False
    thr = c.low + IRB_PCT * rng
    return c.open >= thr and c.close >= thr


def main() -> None:  # noqa: C901
    print(f"[load] Pine: {PINE_CSV.name}")
    pine = load_pine(PINE_CSV)
    print(f"[load] H1: {H1_CSV.name}")
    h1 = load_candles(H1_CSV, "EURUSD", "H1")
    h4 = aggregate_h1_to_h4(h1)
    ts_to_bar: dict[int, int] = {int(c.timestamp): i for i, c in enumerate(h1)}

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
    recorder = RecordingFilterRejection(bt)
    bt._rejections = recorder
    res = bt.run(h1, h4)
    log: list[tuple[int, str]] = recorder._log  # type: ignore[attr-defined]

    rejections_by_bar: dict[int, list[str]] = defaultdict(list)
    for bar, reason in log:
        rejections_by_bar[bar].append(reason)

    # Map: bar_index -> CompletedTrade entered on that bar (if any)
    entries_by_bar: dict[int, object] = {}
    for t in res.trades:
        if 0 <= t.entry_bar < len(h1):
            entries_by_bar[t.entry_bar] = t

    # pine_only set keyed by (ts, side)
    py_keys: set[tuple[int, str]] = set()
    for t in res.trades:
        if not (0 <= t.entry_bar < len(h1)):
            continue
        ts = int(h1[t.entry_bar].timestamp)
        side = "Long" if t.side.value.lower() == "long" else "Short"
        py_keys.add((ts, side))
    pine_only = [(ts, s, p) for (ts, s, p) in pine if (ts, s) not in py_keys]

    # Restrict to Bucket B: those with valid IRB+break in our H1 within TRIGGER_WIN
    bucket_b: list[tuple[int, str, int]] = []  # (entry_ts, side, irb_offset)
    for entry_ts, side, _pnl in pine_only:
        if entry_ts not in ts_to_bar:
            continue
        entry_bar = ts_to_bar[entry_ts]
        entry_candle = h1[entry_bar]
        for offset in range(1, TRIGGER_WIN + 1):
            B = entry_bar - offset
            if B < 0:
                break
            cb = h1[B]
            if side == "Long":
                if is_up_irb(cb) and entry_candle.high >= cb.high + PIP_BUF:
                    bucket_b.append((entry_ts, side, offset))
                    break
            else:
                if is_dn_irb(cb) and entry_candle.low <= cb.low - PIP_BUF:
                    bucket_b.append((entry_ts, side, offset))
                    break

    print(f"[scope] {len(bucket_b)} Bucket-B pine_only entries to inspect at exact IRB bar")
    print()

    # Categorize each Bucket-B by what (if anything) Python rejected at the IRB bar
    at_irb_bar_first: Counter = Counter()  # first rejection at exact IRB bar
    at_irb_bar_any: Counter = Counter()
    no_rejection_at_irb_bar = 0
    python_entered_in_window = (
        0  # for the "no rejection" bucket: did Python enter a position between IRB_bar+1 and entry_ts?
    )

    samples_by_category: dict[str, list[tuple[int, str, int, int]]] = defaultdict(list)

    for entry_ts, side, offset in bucket_b:
        entry_bar = ts_to_bar[entry_ts]
        irb_bar = entry_bar - offset
        rejs_at_irb = rejections_by_bar.get(irb_bar, [])
        if rejs_at_irb:
            at_irb_bar_first[rejs_at_irb[0]] += 1
            for r in set(rejs_at_irb):
                at_irb_bar_any[r] += 1
            cat = rejs_at_irb[0]
        else:
            no_rejection_at_irb_bar += 1
            # Did Python enter any position with entry_bar in (irb_bar, entry_bar]?
            entered = any(b in entries_by_bar for b in range(irb_bar + 1, entry_bar + 1))
            if entered:
                python_entered_in_window += 1
                cat = "no_rej_python_entered"
            else:
                cat = "no_rej_python_silent"

        if len(samples_by_category[cat]) < 4:
            samples_by_category[cat].append((entry_ts, side, offset, irb_bar))

    print("=" * 70)
    print("WHAT WAS PYTHON DOING AT THE EXACT IRB BAR?")
    print("=" * 70)
    print(f"  {'category':<30}{'count':>10}{'% of B':>10}")
    total = len(bucket_b)
    for cat, count in at_irb_bar_first.most_common():
        pct = count / max(total, 1) * 100
        print(f"  {cat:<30}{count:>10,}{pct:>9.1f}%")
    if no_rejection_at_irb_bar:
        pct = no_rejection_at_irb_bar / max(total, 1) * 100
        print(f"  {'(no rejection)':<30}{no_rejection_at_irb_bar:>10,}{pct:>9.1f}%")
        print(f"     ↳ Python entered a position in (IRB_bar, entry_ts]: {python_entered_in_window}")
        print(
            f"     ↳ Python silent (no entry, no rejection)         : "
            f"{no_rejection_at_irb_bar - python_entered_in_window}"
        )

    print()
    print("=" * 70)
    print("ANY-HIT AT IRB BAR (cumulative across all filters)")
    print("=" * 70)
    print(f"  {'category':<30}{'any-hit':>10}")
    for cat, count in at_irb_bar_any.most_common():
        print(f"  {cat:<30}{count:>10,}")

    print()
    print("=" * 70)
    print("SAMPLES (first 4 per category)")
    print("=" * 70)
    for cat in sorted(samples_by_category.keys()):
        samples = samples_by_category[cat]
        print(f"\n  [{cat}]")
        for entry_ts, side, offset, irb_bar in samples:
            entry_str = datetime.fromtimestamp(entry_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            irb_ts = int(h1[irb_bar].timestamp)
            irb_str = datetime.fromtimestamp(irb_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            rejs = rejections_by_bar.get(irb_bar, [])
            rej_str = ",".join(rejs) if rejs else "(none)"
            print(f"    entry {entry_str} {side}  ←  IRB {irb_str} (offset {offset})  rejs@IRB: {rej_str}")

    print()
    print("=" * 70)
    print("ROOT CAUSE INTERPRETATION")
    print("=" * 70)
    if at_irb_bar_first:
        top = at_irb_bar_first.most_common(1)[0]
        if top[0] == "irb_geometry":
            print(f"  Top: irb_geometry at IRB bar ({top[1]}) — UNEXPECTED. Equations agree on identical")
            print("  OHLC, so this would imply a real math divergence. Check rng > 0 edge cases.")
        elif top[0] in (
            "volatility_filter",
            "session_filter",
            "trend_filter",
            "mtf_alignment",
            "sideways_filter",
            "overextension_filter",
        ):
            print(f"  Top: {top[0]} at IRB bar ({top[1]}) — H1' confirmed.")
            print(f"  Python's {top[0]} is rejecting bars Pine accepts. Action: compare Python's")
            print("  filter logic + Pine source for this filter, identify the divergence, fix.")
        elif top[0] == "existing_position":
            print(f"  Top: existing_position at IRB bar ({top[1]}) — H3 confirmed at bar level.")
            print("  Python is in non-FLAT state at the IRB bar. Action: investigate exit-timing")
            print("  divergence (probably exit-side fix needed before this leak closes).")
        elif top[0] == "circuit_breaker":
            print(f"  Top: circuit_breaker at IRB bar ({top[1]}) — Python's loss-streak guard rejecting.")
            print("  Pine has no equivalent. Action: configure off in pine_aligned.yaml or relax.")
        else:
            print(f"  Top: {top[0]} at IRB bar ({top[1]}) — investigate further.")
    if no_rejection_at_irb_bar:
        if python_entered_in_window > no_rejection_at_irb_bar / 2:
            print()
            print(f"  Plus {python_entered_in_window} cases where Python entered a different position in")
            print("  (IRB_bar, entry_ts] — Python detected something but on a different bar/direction.")
            print("  Action: trace pending-replacement logic, possibly H2.")
        elif no_rejection_at_irb_bar - python_entered_in_window > 0:
            print()
            print(f"  Plus {no_rejection_at_irb_bar - python_entered_in_window} cases where Python was silent")
            print("  (no rejection AND no entry) at IRB bar. Python was likely in PENDING/LONG/SHORT")
            print("  state silently. Need state-log instrumentation to confirm.")


if __name__ == "__main__":
    main()
