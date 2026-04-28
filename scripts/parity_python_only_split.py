"""Partition the python_only set into:
  - Bucket E (exit-timing artifact): Pine was IN a position when Python
    took this entry. Some Pine trade has entry_ts ≤ python_only_ts < exit_ts.
    These can't be closed by an entry-filter fix — they reflect Python
    exiting earlier than Pine on a different trade, freeing Python to
    take this entry while Pine is still busy.
  - Bucket F (real filter divergence): Pine was FLAT at this timestamp.
    Pine evaluated the bar and rejected on some filter. These are the
    candidates for an actual entry-filter fix.

For Bucket F entries, replay Pine's filter conditions (trend_up slope,
ema_stack, ema10_confirm, sw_pass, oe_pass, h4_up) on our H1 data and
report which Pine condition fails for each.
"""

from __future__ import annotations

import csv
import math
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

# Pine constants (configs/pinescript/irb_v5_stag.pine)
IRB_PCT = 0.45
SLOPE_LOOKBACK = 20
SLOPE_THRESH = 0.4
MTF_H1_LOOKBACK = 20
ADX_THRESH = 20.0
OE_THRESH = 2.0
EMA_CONFIRM_BARS = 3


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


def load_pine_trades(path: Path) -> list[dict]:
    """Pair Entry/Exit Pine rows; return list of {entry_ts, exit_ts, side}."""
    by_n: dict[int, dict] = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            n = int(r["Trade #"])
            row_type = r["Type"].strip()
            ts = parse_ts(r["Date and time"])
            if n not in by_n:
                by_n[n] = {}
            if row_type.startswith("Entry"):
                by_n[n]["entry_ts"] = ts
                by_n[n]["side"] = "Long" if "long" in row_type else "Short"
            elif row_type.startswith("Exit"):
                by_n[n]["exit_ts"] = ts
    return [d for d in by_n.values() if "entry_ts" in d and "exit_ts" in d]


def main() -> None:  # noqa: C901
    print(f"[load] Pine: {PINE_CSV.name}")
    pine_trades = load_pine_trades(PINE_CSV)
    pine_entry_keys = {(t["entry_ts"], t["side"]) for t in pine_trades}

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
    res = bt.run(h1, h4)

    py_keys: set[tuple[int, str]] = set()
    py_by_key: dict = {}
    for t in res.trades:
        if not (0 <= t.entry_bar < len(h1)):
            continue
        ts = int(h1[t.entry_bar].timestamp)
        side = "Long" if t.side.value.lower() == "long" else "Short"
        py_keys.add((ts, side))
        py_by_key[(ts, side)] = t

    python_only = py_keys - pine_entry_keys
    print(f"[scope] {len(python_only)} python_only entries")

    # Pre-compute indicators (mirrors engine internal) for Pine-replay
    h1_closes = [c.close for c in h1]
    h4_closes = [c.close for c in h4]
    ema_10 = compute_ema(h1_closes, 10) if h1 else []
    ema_20 = compute_ema(h1_closes, 20) if h1 else []
    ema_50 = compute_ema(h1_closes, 50) if h1 else []
    ema_h4 = compute_ema(h4_closes, 20) if h4 else []
    atr_h1 = compute_atr(h1, 14) if h1 else []
    adx_h1 = compute_adx(h1, 14) if h1 else []

    # Bucket E: Pine was in a position at the python_only timestamp
    bucket_e = 0
    bucket_f: list[tuple[int, str]] = []
    for ts, side in python_only:
        in_pine_position = False
        for pt in pine_trades:
            if pt["entry_ts"] <= ts < pt["exit_ts"]:
                in_pine_position = True
                break
        if in_pine_position:
            bucket_e += 1
        else:
            bucket_f.append((ts, side))

    print()
    print("=" * 70)
    print("BUCKET PARTITION")
    print("=" * 70)
    print(
        f"  Bucket E (Pine in position when Python entered): {bucket_e:4d}  ({bucket_e / len(python_only) * 100:.1f}%)"
    )
    f_pct = len(bucket_f) / len(python_only) * 100
    print(f"  Bucket F (Pine was FLAT — real filter divergence): {len(bucket_f):4d}  ({f_pct:.1f}%)")
    print(f"  Total                                            : {len(python_only):4d}")

    if not bucket_f:
        print()
        print("=" * 70)
        print("VERDICT")
        print("=" * 70)
        print("  All python_only entries are exit-timing artifacts (Bucket E).")
        print("  No entry-filter fix can close them — they reflect Python's exit")
        print("  semantics being more aggressive than Pine's. The right next move")
        print("  is to investigate exit-side parity (existing_position cascade).")
        return

    # For Bucket F, replay Pine's filter conditions on each entry's IRB bar
    # (Pine fills next-bar after IRB detection, so the IRB bar is at entry_ts - 1
    # typically. We'll scan offsets 1-3 to find the most likely IRB candidate.)
    print()
    print("=" * 70)
    print("BUCKET F — REPLAY PINE FILTERS ON IRB BAR")
    print("=" * 70)

    def is_up_irb_bar(c: Candle) -> bool:
        rng = c.high - c.low
        if rng <= 0:
            return False
        thr = c.high - IRB_PCT * rng
        return c.open <= thr and c.close <= thr

    def is_dn_irb_bar(c: Candle) -> bool:
        rng = c.high - c.low
        if rng <= 0:
            return False
        thr = c.low + IRB_PCT * rng
        return c.open >= thr and c.close >= thr

    failure_counts: Counter = Counter()
    no_irb_found = 0

    for entry_ts, side in bucket_f:
        if entry_ts not in ts_to_bar:
            continue
        entry_bar = ts_to_bar[entry_ts]
        # Find most recent IRB candidate within 5 bars
        irb_bar = -1
        for offset in range(1, 6):
            B = entry_bar - offset
            if B < 0:
                break
            if (side == "Long" and is_up_irb_bar(h1[B])) or (side == "Short" and is_dn_irb_bar(h1[B])):
                irb_bar = B
                break

        if irb_bar < 0:
            no_irb_found += 1
            continue

        # Now replay Pine filters at irb_bar
        c = h1[irb_bar]
        atr = atr_h1[irb_bar] if irb_bar < len(atr_h1) and not math.isnan(atr_h1[irb_bar]) else 0.0
        if atr <= 0:
            failure_counts["no_atr"] += 1
            continue

        # 1. trend_up / trend_dn (slope >= 0.4 or <= -0.4)
        if (
            irb_bar >= SLOPE_LOOKBACK
            and not math.isnan(ema_20[irb_bar])
            and not math.isnan(ema_20[irb_bar - SLOPE_LOOKBACK])
        ):
            slope = (ema_20[irb_bar] - ema_20[irb_bar - SLOPE_LOOKBACK]) / atr
            if side == "Long" and slope < SLOPE_THRESH:
                failure_counts["trend_up_slope"] += 1
                continue
            if side == "Short" and slope > -SLOPE_THRESH:
                failure_counts["trend_dn_slope"] += 1
                continue

        # 2. h4_up / h4_dn (Pine semantic: ema_h4[i] vs ema_h4[i - MTF_H1_LOOKBACK])
        h4_idx_now = irb_bar // 4
        h4_idx_prev = (irb_bar - MTF_H1_LOOKBACK) // 4 if irb_bar >= MTF_H1_LOOKBACK else 0
        if 0 <= h4_idx_now < len(ema_h4) and 0 <= h4_idx_prev < len(ema_h4):
            e_now = ema_h4[h4_idx_now]
            e_prev = ema_h4[h4_idx_prev]
            if not (math.isnan(e_now) or math.isnan(e_prev)):
                if side == "Long" and e_now <= e_prev:
                    failure_counts["h4_up"] += 1
                    continue
                if side == "Short" and e_now >= e_prev:
                    failure_counts["h4_dn"] += 1
                    continue

        # 3. sw_pass (ADX >= 20)
        adx = adx_h1[irb_bar] if irb_bar < len(adx_h1) and not math.isnan(adx_h1[irb_bar]) else 0.0
        if adx < ADX_THRESH:
            failure_counts["sw_pass_adx"] += 1
            continue

        # 4. oe_pass (bar_rng / atr <= 2.0)
        oe = (c.high - c.low) / atr
        if oe > OE_THRESH:
            failure_counts["oe_pass"] += 1
            continue

        # 5. ema_stack_bull / ema_stack_bear
        if (
            irb_bar < len(ema_10)
            and irb_bar < len(ema_20)
            and irb_bar < len(ema_50)
            and not math.isnan(ema_10[irb_bar])
            and not math.isnan(ema_20[irb_bar])
            and not math.isnan(ema_50[irb_bar])
        ):
            e10, e20, e50 = ema_10[irb_bar], ema_20[irb_bar], ema_50[irb_bar]
            if side == "Long" and not (e10 > e20 > e50):
                failure_counts["ema_stack_bull"] += 1
                continue
            if side == "Short" and not (e10 < e20 < e50):
                failure_counts["ema_stack_bear"] += 1
                continue

        # 6. ema10_confirm_long/short (last 3 bars closing above/below ema_10)
        confirm_ok = True
        for j in range(EMA_CONFIRM_BARS):
            idx = irb_bar - j
            if idx < 0 or idx >= len(ema_10) or math.isnan(ema_10[idx]):
                confirm_ok = False
                break
            if side == "Long" and h1[idx].close <= ema_10[idx]:
                confirm_ok = False
                break
            if side == "Short" and h1[idx].close >= ema_10[idx]:
                confirm_ok = False
                break
        if not confirm_ok:
            failure_counts["ema10_confirm"] += 1
            continue

        # If all Pine filters pass, check whether Pine was in a position AT
        # THE IRB BAR (not just at entry_ts). Pine's state machine requires
        # state == S_FLAT at the IRB bar to fire a new signal. If Pine had
        # an open trade spanning the IRB bar, it couldn't fire the new signal
        # even though every filter technically passes.
        irb_bar_ts = int(h1[irb_bar].timestamp)
        pine_busy_at_irb = any(pt["entry_ts"] <= irb_bar_ts < pt["exit_ts"] for pt in pine_trades)
        if pine_busy_at_irb:
            failure_counts["pine_busy_at_irb_bar"] += 1
        else:
            # Pine was FLAT at IRB bar, all filters pass, yet didn't enter.
            # This is the residual mystery — likely Pine pending-order cancel
            # /replace dynamics, trigger window, or a Pine filter we missed.
            failure_counts["all_pine_filters_pass"] += 1

    total_replayed = sum(failure_counts.values())
    print(f"  Replayed Pine filters on {total_replayed:,} of {len(bucket_f):,} Bucket-F entries")
    print(f"  ({no_irb_found} had no IRB bar within 5 offsets)")
    print()
    print(f"  {'Pine filter that rejects':<28}{'count':>8}{'% of F':>10}")
    for reason, count in failure_counts.most_common():
        pct = count / max(total_replayed, 1) * 100
        print(f"  {reason:<28}{count:>8,}{pct:>9.1f}%")

    print()
    print("=" * 70)
    print("INTERPRETATION")
    print("=" * 70)
    if bucket_e > len(bucket_f):
        e_pct = bucket_e / len(python_only) * 100
        print(f"  Majority ({e_pct:.0f}%) of python_only entries are EXIT-TIMING ARTIFACTS.")
        print("  No entry-filter fix can close them. Next: investigate Python's exit")
        print("  semantics — the existing_position cascade we noted earlier is the")
        print("  same root cause, viewed from the other side.")
    if failure_counts:
        top = failure_counts.most_common(1)[0]
        print(f"  Bucket F primary leak: '{top[0]}' rejecting {top[1]} entries.")
        if top[0] == "all_pine_filters_pass":
            print("    All replayed Pine filters PASS — divergence is in state machine,")
            print("    cooldown, or warmup, not a per-bar filter.")
        else:
            print(f"    Investigate Python's '{top[0]}' check vs Pine's.")


if __name__ == "__main__":
    main()
