"""Research: What differentiates TIME_STOP entries from regular exit entries?

If we can find ANY signal that separates them, we can filter losers
without killing TIME_STOP winners.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.tv_robustness_report import parse_tv_csv

V2_PATH = "data/irb_v2_tv_1222trades_10yr.csv"


def hours_held(t):
    return (t.exit_time - t.entry_time).total_seconds() / 3600


def main():  # noqa: C901
    trades = parse_tv_csv(V2_PATH)

    ts = [t for t in trades if t.exit_signal == "TIME_STOP"]
    reg = [t for t in trades if t.exit_signal in ("Long Exit", "Short Exit")]

    print("=" * 80)
    print("  v6 RESEARCH — TIME_STOP vs Regular Exit Entry Characteristics")
    print("=" * 80)
    print(f"  TIME_STOP: {len(ts)} trades | Regular: {len(reg)} trades")

    # ── 1. DIRECTION ──
    print("\n\n── 1. DIRECTION ─────────────────────────────────────────────")
    for label, group in [("TIME_STOP", ts), ("Regular", reg)]:
        longs = sum(1 for t in group if t.direction == "long")
        shorts = len(group) - longs
        long_pnl = sum(t.pnl_usd for t in group if t.direction == "long")
        short_pnl = sum(t.pnl_usd for t in group if t.direction == "short")
        long_pct = 100 * longs / len(group)
        short_pct = 100 * shorts / len(group)
        print(
            f"  {label:>10}: LONG {longs} ({long_pct:.0f}%)"
            f" ${long_pnl:>+,.0f} | SHORT {shorts}"
            f" ({short_pct:.0f}%) ${short_pnl:>+,.0f}"
        )

    # What if we only traded LONGS?
    long_ts = [t for t in ts if t.direction == "long"]
    long_reg = [t for t in reg if t.direction == "long"]
    short_ts = [t for t in ts if t.direction == "short"]
    short_reg = [t for t in reg if t.direction == "short"]
    print("\n  LONG-ONLY scenario:")
    print(f"    TIME_STOP: {len(long_ts)} trades, ${sum(t.pnl_usd for t in long_ts):>+,.0f}")
    print(f"    Regular exits: {len(long_reg)} trades, ${sum(t.pnl_usd for t in long_reg):>+,.0f}")
    print(f"    Net: ${sum(t.pnl_usd for t in long_ts) + sum(t.pnl_usd for t in long_reg):>+,.0f}")
    print("  SHORT-ONLY scenario:")
    print(f"    TIME_STOP: {len(short_ts)} trades, ${sum(t.pnl_usd for t in short_ts):>+,.0f}")
    print(f"    Regular exits: {len(short_reg)} trades, ${sum(t.pnl_usd for t in short_reg):>+,.0f}")
    print(f"    Net: ${sum(t.pnl_usd for t in short_ts) + sum(t.pnl_usd for t in short_reg):>+,.0f}")

    # ── 2. DAY OF WEEK ──
    print("\n\n── 2. DAY OF WEEK (Entry Day) ───────────────────────────────")
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    print(
        f"  {'Day':>5} | {'TS Count':>8} {'TS %':>6}"
        f" | {'Reg Count':>9} {'Reg %':>6} | {'TS Rate':>7}"
        f" | {'Reg P&L':>9} | {'TS P&L':>9}"
    )
    print("  " + "-" * 80)
    for d in range(7):
        ts_d = [t for t in ts if t.entry_time.weekday() == d]
        reg_d = [t for t in reg if t.entry_time.weekday() == d]
        total_d = len(ts_d) + len(reg_d)
        ts_rate = 100 * len(ts_d) / total_d if total_d else 0
        ts_pnl = sum(t.pnl_usd for t in ts_d)
        reg_pnl = sum(t.pnl_usd for t in reg_d)
        ts_pct = 100 * len(ts_d) / len(ts) if ts else 0
        reg_pct = 100 * len(reg_d) / len(reg) if reg else 0
        print(
            f"  {days[d]:>5} | {len(ts_d):>8} {ts_pct:>5.1f}%"
            f" | {len(reg_d):>9} {reg_pct:>5.1f}%"
            f" | {ts_rate:>5.1f}%"
            f" | ${reg_pnl:>+8,.0f} | ${ts_pnl:>+8,.0f}"
        )

    # ── 3. HOUR OF ENTRY ──
    print("\n\n── 3. HOUR OF ENTRY ─────────────────────────────────────────")
    print(
        f"  {'Hour':>5} | {'TS':>4} {'TS%':>5}"
        f" | {'Reg':>5} {'Reg%':>5} | {'TS Rate':>7}"
        f" | {'Reg P&L':>9} | {'All P&L':>9}"
    )
    print("  " + "-" * 70)
    for h in range(24):
        ts_h = [t for t in ts if t.entry_time.hour == h]
        reg_h = [t for t in reg if t.entry_time.hour == h]
        total_h = len(ts_h) + len(reg_h)
        if total_h == 0:
            continue
        ts_rate = 100 * len(ts_h) / total_h
        reg_pnl = sum(t.pnl_usd for t in reg_h)
        ts_pnl = sum(t.pnl_usd for t in ts_h)
        all_pnl = reg_pnl + ts_pnl
        marker = " ◀" if ts_rate > 12 else " ✗" if ts_rate < 5 else ""
        ts_pct = 100 * len(ts_h) / max(len(ts), 1)
        reg_pct = 100 * len(reg_h) / max(len(reg), 1)
        print(
            f"  {h:>5} | {len(ts_h):>4} {ts_pct:>4.1f}%"
            f" | {len(reg_h):>5} {reg_pct:>4.1f}%"
            f" | {ts_rate:>5.1f}%"
            f" | ${reg_pnl:>+8,.0f}"
            f" | ${all_pnl:>+8,.0f}{marker}"
        )

    # ── 4. HOUR BLOCKS ──
    print("\n\n── 4. SESSION BLOCKS ────────────────────────────────────────")
    sessions = [
        ("Asia (0-7)", range(0, 7)),
        ("London (7-12)", range(7, 12)),
        ("NY Overlap (12-16)", range(12, 16)),
        ("NY PM (16-20)", range(16, 20)),
        ("Evening (20-24)", range(20, 24)),
    ]
    for label, hours in sessions:
        ts_s = [t for t in ts if t.entry_time.hour in hours]
        reg_s = [t for t in reg if t.entry_time.hour in hours]
        total_s = len(ts_s) + len(reg_s)
        if total_s == 0:
            continue
        ts_rate = 100 * len(ts_s) / total_s
        reg_pnl = sum(t.pnl_usd for t in reg_s)
        ts_pnl = sum(t.pnl_usd for t in ts_s)
        all_pnl = reg_pnl + ts_pnl
        print(
            f"  {label:>20} | TS: {len(ts_s):>3}"
            f" Reg: {len(reg_s):>4}"
            f" | TS Rate: {ts_rate:.1f}%"
            f" | All P&L: ${all_pnl:>+9,.0f}"
        )

    # ── 5. MONTH OF YEAR ──
    print("\n\n── 5. MONTH OF YEAR (Seasonality) ───────────────────────────")
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    print(f"  {'Month':>5} | {'TS':>4} | {'Reg':>5} | {'TS Rate':>7} | {'Reg P&L':>9} | {'TS P&L':>9} | {'Net':>9}")
    print("  " + "-" * 70)
    for m in range(1, 13):
        ts_m = [t for t in ts if t.entry_time.month == m]
        reg_m = [t for t in reg if t.entry_time.month == m]
        total_m = len(ts_m) + len(reg_m)
        if total_m == 0:
            continue
        ts_rate = 100 * len(ts_m) / total_m
        reg_pnl = sum(t.pnl_usd for t in reg_m)
        ts_pnl = sum(t.pnl_usd for t in ts_m)
        marker = " ◀" if ts_rate > 12 else " ✗" if ts_rate < 6 else ""
        net_pnl = reg_pnl + ts_pnl
        print(
            f"  {month_names[m - 1]:>5} | {len(ts_m):>4}"
            f" | {len(reg_m):>5} | {ts_rate:>5.1f}%"
            f" | ${reg_pnl:>+8,.0f} | ${ts_pnl:>+8,.0f}"
            f" | ${net_pnl:>+8,.0f}{marker}"
        )

    # ── 6. FAVORABLE EXCURSION (entry quality proxy) ──
    print("\n\n── 6. FAVORABLE EXCURSION (whole trade) ─────────────────────")
    fav_buckets = [
        ("$0-50", 0, 50),
        ("$50-100", 50, 100),
        ("$100-200", 100, 200),
        ("$200-400", 200, 400),
        ("$400-700", 400, 700),
        ("$700-1000", 700, 1000),
        ("$1000+", 1000, 99999),
    ]
    print(f"  {'Fav Exc':>10} | {'TS':>4} | {'Reg':>5} | {'TS Rate':>7} | {'Reg Avg P&L':>11} | {'TS Avg P&L':>11}")
    print("  " + "-" * 65)
    for label, lo, hi in fav_buckets:
        ts_f = [t for t in ts if lo <= t.favorable_usd < hi]
        reg_f = [t for t in reg if lo <= t.favorable_usd < hi]
        total_f = len(ts_f) + len(reg_f)
        if total_f == 0:
            continue
        ts_rate = 100 * len(ts_f) / total_f
        reg_avg = sum(t.pnl_usd for t in reg_f) / len(reg_f) if reg_f else 0
        ts_avg = sum(t.pnl_usd for t in ts_f) / len(ts_f) if ts_f else 0
        print(
            f"  {label:>10} | {len(ts_f):>4}"
            f" | {len(reg_f):>5} | {ts_rate:>5.1f}%"
            f" | ${reg_avg:>+10,.0f}"
            f" | ${ts_avg:>+10,.0f}"
        )

    # ── 7. YEARLY TS RATE ──
    print("\n\n── 7. YEARLY TIME_STOP RATE ─────────────────────────────────")
    for y in sorted(set(t.entry_time.year for t in trades)):
        ts_y = [t for t in ts if t.entry_time.year == y]
        reg_y = [t for t in reg if t.entry_time.year == y]
        all_y = [t for t in trades if t.entry_time.year == y]
        total_y = len(ts_y) + len(reg_y)
        if total_y == 0:
            continue
        ts_rate = 100 * len(ts_y) / len(all_y)
        ts_pnl = sum(t.pnl_usd for t in ts_y)
        reg_pnl = sum(t.pnl_usd for t in reg_y)
        net = sum(t.pnl_usd for t in all_y)
        print(
            f"  {y} | {len(all_y):>4} trades"
            f" | TS: {len(ts_y):>3} ({ts_rate:.1f}%)"
            f" ${ts_pnl:>+8,.0f}"
            f" | Reg: {len(reg_y):>3} ${reg_pnl:>+8,.0f}"
            f" | Net: ${net:>+8,.0f}"
        )

    # ── 8. SIMULATION: FILTER OUT WORST HOURS ──
    print("\n\n── 8. SIMULATIONS ──────────────────────────────────────────")

    # Sim A: Drop hours with 0 TIME_STOP
    zero_ts_hours = set()
    for h in range(24):
        ts_h = [t for t in ts if t.entry_time.hour == h]
        if len(ts_h) == 0:
            all_h = [t for t in trades if t.entry_time.hour == h]
            if len(all_h) > 0:
                zero_ts_hours.add(h)

    if zero_ts_hours:
        blocked = [t for t in trades if t.entry_time.hour in zero_ts_hours]
        kept = [t for t in trades if t.entry_time.hour not in zero_ts_hours]
        blocked_ts = [t for t in blocked if t.exit_signal == "TIME_STOP"]
        blocked_pnl = sum(t.pnl_usd for t in blocked)
        kept_pnl = sum(t.pnl_usd for t in kept)
        kept_ts = [t for t in kept if t.exit_signal == "TIME_STOP"]
        print(f"  SIM A: Block hours with 0 TIME_STOP entries: {sorted(zero_ts_hours)}")
        print(f"    Blocked: {len(blocked)} trades, ${blocked_pnl:>+,.0f} P&L, {len(blocked_ts)} TIME_STOP")
        print(f"    Kept: {len(kept)} trades, ${kept_pnl:>+,.0f} P&L, {len(kept_ts)} TIME_STOP")
        print(f"    Impact: ${kept_pnl - sum(t.pnl_usd for t in trades):>+,.0f}")

    # Sim B: Block worst sessions
    for block_label, block_hours in [
        ("Block Asia (0-7)", range(0, 7)),
        ("Block Evening (20-24)", range(20, 24)),
        ("Block Asia+Evening (0-7, 20-24)", list(range(0, 7)) + list(range(20, 24))),
    ]:
        kept = [t for t in trades if t.entry_time.hour not in block_hours]
        kept_ts = [t for t in kept if t.exit_signal == "TIME_STOP"]
        kept_pnl = sum(t.pnl_usd for t in kept)
        delta = kept_pnl - sum(t.pnl_usd for t in trades)
        print(f"\n  SIM B: {block_label}")
        print(f"    Kept: {len(kept)} trades, {len(kept_ts)} TIME_STOP, ${kept_pnl:>+,.0f} (Δ${delta:>+,.0f})")

    # Sim C: Long-only
    longs = [t for t in trades if t.direction == "long"]
    long_pnl = sum(t.pnl_usd for t in longs)
    long_ts_count = sum(1 for t in longs if t.exit_signal == "TIME_STOP")
    print("\n  SIM C: LONG-ONLY")
    print(f"    {len(longs)} trades, {long_ts_count} TIME_STOP, ${long_pnl:>+,.0f}")
    print(f"    Δ vs v2: ${long_pnl - sum(t.pnl_usd for t in trades):>+,.0f}")

    # Sim D: Best hours only (top TS rate hours)
    hour_ts_rate = {}
    for h in range(24):
        ts_h = [t for t in ts if t.entry_time.hour == h]
        all_h = [t for t in trades if t.entry_time.hour == h]
        if all_h:
            hour_ts_rate[h] = len(ts_h) / len(all_h)
    best_hours = [h for h, rate in sorted(hour_ts_rate.items(), key=lambda x: -x[1]) if rate >= 0.10]
    if best_hours:
        kept = [t for t in trades if t.entry_time.hour in best_hours]
        kept_ts = [t for t in kept if t.exit_signal == "TIME_STOP"]
        kept_pnl = sum(t.pnl_usd for t in kept)
        print(f"\n  SIM D: Only hours with ≥10% TIME_STOP rate: {best_hours}")
        print(f"    {len(kept)} trades, {len(kept_ts)} TIME_STOP, ${kept_pnl:>+,.0f}")
        print(f"    TIME_STOP preserved: {len(kept_ts)}/{len(ts)} ({100 * len(kept_ts) / len(ts):.0f}%)")
        print(f"    Δ vs v2: ${kept_pnl - sum(t.pnl_usd for t in trades):>+,.0f}")

    # Sim E: Block worst months
    for block_months, block_label in [
        ([1, 7, 10], "Block Jan+Jul+Oct (worst TS rate)"),
    ]:
        kept = [t for t in trades if t.entry_time.month not in block_months]
        kept_ts = [t for t in kept if t.exit_signal == "TIME_STOP"]
        kept_pnl = sum(t.pnl_usd for t in kept)
        delta = kept_pnl - sum(t.pnl_usd for t in trades)
        print(f"\n  SIM E: {block_label}")
        print(f"    {len(kept)} trades, {len(kept_ts)} TIME_STOP, ${kept_pnl:>+,.0f} (Δ${delta:>+,.0f})")

    # ── 9. COMPOUND SIM: Best combo ──
    print("\n\n── 9. COMPOUND SIMULATIONS ─────────────────────────────────")

    # Try various combinations
    combos = [
        ("Long-only + best hours", lambda t: t.direction == "long" and t.entry_time.hour in best_hours),
        ("Best hours only", lambda t: t.entry_time.hour in best_hours),
        ("Long-only + block evening", lambda t: t.direction == "long" and t.entry_time.hour < 20),
        ("Block 0-6 + block 21-23", lambda t: 6 <= t.entry_time.hour <= 20),
    ]

    v2_pnl = sum(t.pnl_usd for t in trades)
    for label, filt in combos:
        kept = [t for t in trades if filt(t)]
        kept_ts = [t for t in kept if t.exit_signal == "TIME_STOP"]
        kept_pnl = sum(t.pnl_usd for t in kept)
        delta = kept_pnl - v2_pnl
        ts_preserved = 100 * len(kept_ts) / len(ts)
        print(f"  {label}")
        print(
            f"    {len(kept)} trades | {len(kept_ts)} TS ({ts_preserved:.0f}%) | ${kept_pnl:>+,.0f} | Δ${delta:>+,.0f}"
        )

    print()


if __name__ == "__main__":
    main()
