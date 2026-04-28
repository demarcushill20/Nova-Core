"""Disambiguate hypotheses A vs B for the 332 'irb_geometry' first-hit
rejections in the pine_only set.

Hypothesis A: data divergence — our H1 OHLC differs from TradingView's,
so the IRB Pine detected literally doesn't exist in our data.

Hypothesis B: state-machine bug — our H1 data DOES contain the IRB Pine
detected, but Python's engine isn't holding the pending order long enough
or is cancelling it.

For each pine_only entry rejected on irb_geometry, scan backward up to
TRIGGER_WIN=20 bars from entry_ts. For each candidate bar B:
  - LONG : evaluate is_up_irb on bar B, then check high(entry_ts) >= high(B) + PIP_BUF
  - SHORT: evaluate is_dn_irb on bar B, then check low(entry_ts)  <= low(B)  - PIP_BUF

Bucket each rejection into:
  - has_valid_irb_in_window  (Bucket B candidate)
  - no_valid_irb_in_window   (Bucket A candidate)
  - inconclusive             (e.g., entry_ts not in our H1 timeline)
"""

from __future__ import annotations

import csv
from collections import defaultdict
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

    print("[run] Python engine (recording rejections)")
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

    py_keys: set[tuple[int, str]] = set()
    for t in res.trades:
        if not (0 <= t.entry_bar < len(h1)):
            continue
        ts = int(h1[t.entry_bar].timestamp)
        side = "Long" if t.side.value.lower() == "long" else "Short"
        py_keys.add((ts, side))

    pine_only = [(ts, s, p) for (ts, s, p) in pine if (ts, s) not in py_keys]

    # Filter to those rejected on irb_geometry first-hit (the 332 bucket)
    irb_rejected: list[tuple[int, str, float]] = []
    for entry_ts, side, pnl in pine_only:
        if entry_ts not in ts_to_bar:
            continue
        bar_i = ts_to_bar[entry_ts]
        window: list[str] = []
        for b in (bar_i, bar_i - 1, bar_i - 2):
            if b < 0:
                continue
            window.extend(rejections_by_bar.get(b, []))
        if window and window[0] == "irb_geometry":
            irb_rejected.append((entry_ts, side, pnl))

    print(f"[scope] {len(irb_rejected)} pine_only entries rejected on irb_geometry first-hit")
    print()

    # For each, scan backward up to TRIGGER_WIN looking for a valid IRB+break combo
    bucket_b = 0  # has valid IRB candidate in window (state-machine bug)
    bucket_a = 0  # no valid IRB candidate in window (data divergence)
    inconclusive = 0
    bucket_b_examples: list[dict] = []
    bucket_a_examples: list[tuple[int, str]] = []

    for entry_ts, side, _pnl in irb_rejected:
        entry_bar = ts_to_bar[entry_ts]
        entry_candle = h1[entry_bar]
        found = False
        found_bar = -1
        found_high = 0.0
        found_low = 0.0
        # Scan B = entry_bar - 1 down to entry_bar - TRIGGER_WIN
        for offset in range(1, TRIGGER_WIN + 1):
            B = entry_bar - offset
            if B < 0:
                break
            cb = h1[B]
            if side == "Long":
                if is_up_irb(cb) and entry_candle.high >= cb.high + PIP_BUF:
                    found = True
                    found_bar = B
                    found_high = cb.high
                    found_low = cb.low
                    break
            else:
                if is_dn_irb(cb) and entry_candle.low <= cb.low - PIP_BUF:
                    found = True
                    found_bar = B
                    found_high = cb.high
                    found_low = cb.low
                    break
        if found:
            bucket_b += 1
            if len(bucket_b_examples) < 5:
                bucket_b_examples.append(
                    {
                        "entry_ts": entry_ts,
                        "side": side,
                        "irb_bar": found_bar,
                        "irb_offset": entry_bar - found_bar,
                        "irb_high": found_high,
                        "irb_low": found_low,
                        "entry_high": entry_candle.high,
                        "entry_low": entry_candle.low,
                    }
                )
        else:
            bucket_a += 1
            if len(bucket_a_examples) < 5:
                bucket_a_examples.append((entry_ts, side))

    if not irb_rejected:
        print("No irb_geometry first-hit rejections found in pine_only set.")
        return

    print("=" * 70)
    print("BUCKET PARTITION")
    print("=" * 70)
    total = len(irb_rejected)
    print(
        f"  Bucket B (state-machine): {bucket_b:4d}  ({bucket_b / total * 100:.1f}%)  "
        "valid IRB+break found in our H1 data"
    )
    print(
        f"  Bucket A (data divergence): {bucket_a:4d}  ({bucket_a / total * 100:.1f}%)  "
        "no valid IRB+break in 20-bar window"
    )
    print(f"  Inconclusive            : {inconclusive:4d}")
    print(f"  Total                   : {total:4d}")
    print()

    print("=" * 70)
    print("BUCKET B SAMPLES — IRB exists in our data, Python missed it")
    print("=" * 70)
    print(f"  {'entry_ts':<22}{'side':<8}{'IRB_bar_offset':>14}{'IRB high/low':>22}{'entry high/low':>22}")
    for ex in bucket_b_examples:
        ts_str = datetime.fromtimestamp(ex["entry_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        irb_str = f"{ex['irb_high']:.5f}/{ex['irb_low']:.5f}"
        ent_str = f"{ex['entry_high']:.5f}/{ex['entry_low']:.5f}"
        print(f"  {ts_str:<22}{ex['side']:<8}{ex['irb_offset']:>14}{irb_str:>22}{ent_str:>22}")

    print()
    print("=" * 70)
    print("BUCKET A SAMPLES — no valid IRB in our 20-bar window")
    print("=" * 70)
    print(f"  {'entry_ts':<22}{'side':<8}")
    for entry_ts, side in bucket_a_examples:
        ts_str = datetime.fromtimestamp(entry_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"  {ts_str:<22}{side:<8}")

    print()
    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    if bucket_b > bucket_a * 2:
        print(f"  Hypothesis B dominates ({bucket_b / total * 100:.0f}% Bucket B). The IRB Pine detected")
        print("  EXISTS in our H1 data — Python's state machine isn't holding/firing it.")
        print("  Next step: investigate pending-order persistence in engine.py state machine.")
    elif bucket_a > bucket_b * 2:
        print(f"  Hypothesis A dominates ({bucket_a / total * 100:.0f}% Bucket A). The IRB Pine detected")
        print("  does NOT exist in our H1 data — broker feed differs from TradingView.")
        print("  Next step: source-feed reconciliation; engine fix can't close this.")
    else:
        print(f"  Mixed: A={bucket_a / total * 100:.0f}%, B={bucket_b / total * 100:.0f}%. Both are real.")
        print("  Tackle B (state-machine) first; A requires data work.")


if __name__ == "__main__":
    main()
