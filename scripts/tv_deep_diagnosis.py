"""Deep diagnostic analysis for IRB strategy improvement.

Analyzes: time-of-day, day-of-week, seasonality, holding period,
direction splits, regime patterns, and generates actionable filters.
"""

from __future__ import annotations

import sys
from collections import defaultdict

from tv_robustness_report import Trade, parse_tv_csv


def hour_analysis(trades: list[Trade]):
    """Analyze P&L by entry hour (UTC)."""
    print("\n── ENTRY HOUR ANALYSIS (UTC) ──────────────────────────────────")
    hour_pnl: dict[int, list[float]] = defaultdict(list)
    for t in trades:
        hour_pnl[t.entry_time.hour].append(t.pnl_usd)

    print(f"  {'Hour':>4} | {'Trades':>6} | {'Win%':>5} | {'Avg P&L':>8} | {'Total P&L':>10} | Bar")
    print("-" * 72)
    for h in range(24):
        if h not in hour_pnl:
            continue
        pnls = hour_pnl[h]
        wins = sum(1 for p in pnls if p >= 0)
        wr = 100 * wins / len(pnls)
        avg = sum(pnls) / len(pnls)
        total = sum(pnls)
        if total >= 0:
            bar = "+" * max(1, min(int(total / 200), 25))
        else:
            bar = "-" * max(1, min(int(abs(total) / 200), 25))
        print(f"  {h:4d} | {len(pnls):6d} | {wr:4.0f}% | ${avg:>+7,.0f} | ${total:>+9,.0f} | {bar}")

    # Summary: best/worst hours
    totals = {h: sum(pnls) for h, pnls in hour_pnl.items()}
    best_h = max(totals, key=lambda k: totals[k])
    worst_h = min(totals, key=lambda k: totals[k])
    print(f"\n  Best hour:  {best_h:02d}:00 UTC (${totals[best_h]:>+,.0f})")
    print(f"  Worst hour: {worst_h:02d}:00 UTC (${totals[worst_h]:>+,.0f})")

    # Profitable hours vs unprofitable
    prof_hours = [h for h, t in totals.items() if t > 0]
    loss_hours = [h for h, t in totals.items() if t <= 0]
    prof_total = sum(totals[h] for h in prof_hours)
    loss_total = sum(totals[h] for h in loss_hours)
    print(f"\n  Profitable hours ({len(prof_hours)}): {sorted(prof_hours)} → ${prof_total:>+,.0f}")
    print(f"  Losing hours ({len(loss_hours)}):     {sorted(loss_hours)} → ${loss_total:>+,.0f}")


def dow_analysis(trades: list[Trade]):
    """Analyze P&L by day of week."""
    print("\n── DAY OF WEEK ANALYSIS ───────────────────────────────────────")
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow_pnl: dict[int, list[float]] = defaultdict(list)
    for t in trades:
        dow_pnl[t.entry_time.weekday()].append(t.pnl_usd)

    print(f"  {'Day':>4} | {'Trades':>6} | {'Win%':>5} | {'Avg P&L':>8} | {'Total P&L':>10} | Bar")
    print("-" * 72)
    for d in range(7):
        if d not in dow_pnl:
            continue
        pnls = dow_pnl[d]
        wins = sum(1 for p in pnls if p >= 0)
        wr = 100 * wins / len(pnls)
        avg = sum(pnls) / len(pnls)
        total = sum(pnls)
        if total >= 0:
            bar = "+" * max(1, min(int(total / 200), 25))
        else:
            bar = "-" * max(1, min(int(abs(total) / 200), 25))
        print(f"  {days[d]:>4} | {len(pnls):6d} | {wr:4.0f}% | ${avg:>+7,.0f} | ${total:>+9,.0f} | {bar}")


def month_seasonality(trades: list[Trade]):
    """Analyze P&L by calendar month across all years."""
    print("\n── MONTH SEASONALITY (ALL YEARS) ─────────────────────────────")
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    month_pnl: dict[int, list[float]] = defaultdict(list)
    for t in trades:
        month_pnl[t.exit_time.month].append(t.pnl_usd)

    print(f"  {'Month':>5} | {'Trades':>6} | {'Win%':>5} | {'Avg P&L':>8} | {'Total P&L':>10} | Bar")
    print("-" * 72)
    for m in range(1, 13):
        if m not in month_pnl:
            continue
        pnls = month_pnl[m]
        wins = sum(1 for p in pnls if p >= 0)
        wr = 100 * wins / len(pnls)
        avg = sum(pnls) / len(pnls)
        total = sum(pnls)
        if total >= 0:
            bar = "+" * max(1, min(int(total / 200), 25))
        else:
            bar = "-" * max(1, min(int(abs(total) / 200), 25))
        print(f"  {month_names[m - 1]:>5} | {len(pnls):6d} | {wr:4.0f}% | ${avg:>+7,.0f} | ${total:>+9,.0f} | {bar}")


def holding_period_analysis(trades: list[Trade]):
    """Analyze P&L by holding period."""
    print("\n── HOLDING PERIOD ANALYSIS ────────────────────────────────────")

    buckets = [
        ("0-2h", 0, 2),
        ("2-6h", 2, 6),
        ("6-12h", 6, 12),
        ("12-24h", 12, 24),
        ("24-48h", 24, 48),
        ("48-96h", 48, 96),
        ("96h+", 96, 9999),
    ]

    print(f"  {'Period':>8} | {'Trades':>6} | {'Win%':>5} | {'Avg P&L':>8} | {'Total P&L':>10} | Bar")
    print("-" * 72)
    for label, lo, hi in buckets:
        pnls = []
        for t in trades:
            hours = (t.exit_time - t.entry_time).total_seconds() / 3600
            if lo <= hours < hi:
                pnls.append(t.pnl_usd)
        if not pnls:
            continue
        wins = sum(1 for p in pnls if p >= 0)
        wr = 100 * wins / len(pnls)
        avg = sum(pnls) / len(pnls)
        total = sum(pnls)
        if total >= 0:
            bar = "+" * max(1, min(int(total / 200), 25))
        else:
            bar = "-" * max(1, min(int(abs(total) / 200), 25))
        print(f"  {label:>8} | {len(pnls):6d} | {wr:4.0f}% | ${avg:>+7,.0f} | ${total:>+9,.0f} | {bar}")


def direction_deep(trades: list[Trade]):
    """Deep direction analysis with exit type breakdown."""
    print("\n── DIRECTION × EXIT TYPE ──────────────────────────────────────")
    combos: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        key = f"{t.direction:>5} × {t.exit_signal}"
        combos[key].append(t.pnl_usd)

    print(f"  {'Combo':>25} | {'Trades':>6} | {'Win%':>5} | {'Avg P&L':>8} | {'Total P&L':>10}")
    print("-" * 72)
    for key in sorted(combos, key=lambda k: -sum(combos[k])):
        pnls = combos[key]
        wins = sum(1 for p in pnls if p >= 0)
        wr = 100 * wins / len(pnls)
        avg = sum(pnls) / len(pnls)
        total = sum(pnls)
        print(f"  {key:>25} | {len(pnls):6d} | {wr:4.0f}% | ${avg:>+7,.0f} | ${total:>+9,.0f}")


def time_stop_analysis(trades: list[Trade]):
    """Analyze TIME_STOP winners specifically."""
    print("\n── TIME_STOP WINNER PROFILE ───────────────────────────────────")
    ts_trades = [t for t in trades if t.exit_signal == "TIME_STOP"]
    if not ts_trades:
        ts_trades = [t for t in trades if "Time Stop" in t.exit_signal or "TIME" in t.exit_signal.upper()]
    if not ts_trades:
        print("  No TIME_STOP trades found")
        return

    pnls = [t.pnl_usd for t in ts_trades]
    holds = [(t.exit_time - t.entry_time).total_seconds() / 3600 for t in ts_trades]

    print(f"  Count:        {len(ts_trades)}")
    print(f"  Win rate:     {100 * sum(1 for p in pnls if p >= 0) / len(pnls):.0f}%")
    print(f"  Avg P&L:      ${sum(pnls) / len(pnls):,.0f}")
    print(f"  Median P&L:   ${sorted(pnls)[len(pnls) // 2]:,.0f}")
    print(f"  Total P&L:    ${sum(pnls):,.0f}")
    print(f"  Avg hold:     {sum(holds) / len(holds):.0f}h")
    print(f"  Min hold:     {min(holds):.0f}h")
    print(f"  Max hold:     {max(holds):.0f}h")

    # By direction
    for d in ["long", "short"]:
        dt = [t for t in ts_trades if t.direction == d]
        if dt:
            dp = [t.pnl_usd for t in dt]
            print(f"\n  {d.upper()} TIME_STOP:")
            print(f"    Count:   {len(dt)}")
            print(f"    Avg P&L: ${sum(dp) / len(dp):,.0f}")
            print(f"    Total:   ${sum(dp):,.0f}")

    # By year
    print("\n  TIME_STOP by year:")
    year_ts: dict[str, list[float]] = defaultdict(list)
    for t in ts_trades:
        year_ts[t.exit_time.strftime("%Y")].append(t.pnl_usd)
    for y in sorted(year_ts):
        yp = year_ts[y]
        print(f"    {y}: {len(yp)} trades, ${sum(yp):>+,.0f} total, ${sum(yp) / len(yp):>+,.0f} avg")


def loser_analysis(trades: list[Trade]):
    """Analyze characteristics of losing trades."""
    print("\n── LOSER PROFILE ─────────────────────────────────────────────")
    losers = [t for t in trades if t.pnl_usd < 0]
    winners = [t for t in trades if t.pnl_usd >= 0]

    print(f"  Total losers:  {len(losers)} ({100 * len(losers) / len(trades):.0f}%)")
    print(f"  Total winners: {len(winners)} ({100 * len(winners) / len(trades):.0f}%)")

    # Loss distribution
    loss_buckets = [
        ("$0-50", 0, 50),
        ("$50-100", 50, 100),
        ("$100-200", 100, 200),
        ("$200-400", 200, 400),
        ("$400+", 400, 9999),
    ]

    print("\n  Loss size distribution:")
    for label, lo, hi in loss_buckets:
        count = sum(1 for t in losers if lo <= abs(t.pnl_usd) < hi)
        total = sum(t.pnl_usd for t in losers if lo <= abs(t.pnl_usd) < hi)
        print(f"    {label:>10}: {count:4d} trades → ${total:>+9,.0f}")

    # Holding period of losers vs winners
    loser_holds = [(t.exit_time - t.entry_time).total_seconds() / 3600 for t in losers]
    winner_holds = [(t.exit_time - t.entry_time).total_seconds() / 3600 for t in winners]

    print("\n  Avg holding period:")
    print(f"    Winners: {sum(winner_holds) / len(winner_holds):.1f}h")
    print(f"    Losers:  {sum(loser_holds) / len(loser_holds):.1f}h")

    # 1-hour losers (stopped immediately)
    one_hour = [t for t in losers if (t.exit_time - t.entry_time).total_seconds() / 3600 <= 1.5]
    print("\n  Quick stops (≤1.5h hold):")
    print(f"    Count: {len(one_hour)} ({100 * len(one_hour) / len(losers):.0f}% of all losers)")
    print(f"    Total P&L: ${sum(t.pnl_usd for t in one_hour):>+,.0f}")
    print(f"    Avg P&L:   ${sum(t.pnl_usd for t in one_hour) / len(one_hour) if one_hour else 0:>+,.0f}")

    # Favorable excursion on losers (did it ever go positive?)
    if losers[0].favorable_usd is not None:
        fav_positive = [t for t in losers if t.favorable_usd > 50]
        print("\n  Losers that went $50+ positive before stopping:")
        print(f"    Count: {len(fav_positive)} ({100 * len(fav_positive) / len(losers):.0f}% of losers)")
        print(f"    Total lost: ${sum(t.pnl_usd for t in fav_positive):>+,.0f}")
        print("    → These are trades that COULD have been winners with better exit management")


def filter_simulation(trades: list[Trade]):
    """Simulate potential improvement filters."""
    print("\n══════════════════════════════════════════════════════════════════")
    print("  FILTER SIMULATIONS — What If We Changed...")
    print("══════════════════════════════════════════════════════════════════")

    base_pnl = sum(t.pnl_usd for t in trades)
    base_count = len(trades)

    # Sim 1: Long-only
    longs = [t for t in trades if t.direction == "long"]
    long_pnl = sum(t.pnl_usd for t in longs)
    print("\n  1. LONG ONLY (drop all shorts)")
    print(f"     Trades: {len(longs)} (was {base_count})")
    print(f"     Net P&L: ${long_pnl:>+,.0f} (was ${base_pnl:>+,.0f})")
    print(f"     Δ: ${long_pnl - base_pnl:>+,.0f}")

    # Sim 2: Skip worst entry hours
    hour_pnl: dict[int, float] = defaultdict(float)
    for t in trades:
        hour_pnl[t.entry_time.hour] += t.pnl_usd
    worst_hours = sorted(hour_pnl, key=lambda k: hour_pnl[k])[:5]
    filtered = [t for t in trades if t.entry_time.hour not in worst_hours]
    f_pnl = sum(t.pnl_usd for t in filtered)
    print(f"\n  2. SKIP WORST 5 ENTRY HOURS {worst_hours}")
    print(f"     Trades: {len(filtered)} (was {base_count})")
    print(f"     Net P&L: ${f_pnl:>+,.0f} (was ${base_pnl:>+,.0f})")
    print(f"     Δ: ${f_pnl - base_pnl:>+,.0f}")

    # Sim 3: Skip worst day of week
    dow_pnl: dict[int, float] = defaultdict(float)
    for t in trades:
        dow_pnl[t.entry_time.weekday()] += t.pnl_usd
    worst_dow = min(dow_pnl, key=lambda k: dow_pnl[k])
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    filtered = [t for t in trades if t.entry_time.weekday() != worst_dow]
    f_pnl = sum(t.pnl_usd for t in filtered)
    print(f"\n  3. SKIP WORST DAY ({days[worst_dow]})")
    print(f"     Trades: {len(filtered)} (was {base_count})")
    print(f"     Net P&L: ${f_pnl:>+,.0f} (was ${base_pnl:>+,.0f})")
    print(f"     Δ: ${f_pnl - base_pnl:>+,.0f}")

    # Sim 4: Skip worst 2 calendar months
    cal_pnl: dict[int, float] = defaultdict(float)
    for t in trades:
        cal_pnl[t.entry_time.month] += t.pnl_usd
    worst_months = sorted(cal_pnl, key=lambda k: cal_pnl[k])[:2]
    month_names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    filtered = [t for t in trades if t.entry_time.month not in worst_months]
    f_pnl = sum(t.pnl_usd for t in filtered)
    print(f"\n  4. SKIP WORST 2 CALENDAR MONTHS ({', '.join(month_names[m] for m in worst_months)})")
    print(f"     Trades: {len(filtered)} (was {base_count})")
    print(f"     Net P&L: ${f_pnl:>+,.0f} (was ${base_pnl:>+,.0f})")
    print(f"     Δ: ${f_pnl - base_pnl:>+,.0f}")

    # Sim 5: Combined best filter
    # Long-only + skip worst hours + skip worst months
    combined = [t for t in trades if t.entry_time.hour not in worst_hours and t.entry_time.month not in worst_months]
    c_pnl = sum(t.pnl_usd for t in combined)
    print("\n  5. COMBINED: Skip worst 5 hours + worst 2 months")
    print(f"     Trades: {len(combined)} (was {base_count})")
    print(f"     Net P&L: ${c_pnl:>+,.0f} (was ${base_pnl:>+,.0f})")
    print(f"     Δ: ${c_pnl - base_pnl:>+,.0f}")

    # Sim 6: Only take trades during London/NY overlap (12-16 UTC)
    overlap = [t for t in trades if 12 <= t.entry_time.hour <= 16]
    o_pnl = sum(t.pnl_usd for t in overlap)
    print("\n  6. LONDON/NY OVERLAP ONLY (12:00-16:59 UTC)")
    print(f"     Trades: {len(overlap)} (was {base_count})")
    print(f"     Net P&L: ${o_pnl:>+,.0f} (was ${base_pnl:>+,.0f})")
    print(f"     Δ: ${o_pnl - base_pnl:>+,.0f}")

    # Sim 7: Skip small favorable excursion trades (trades that never went positive enough)
    # This simulates a "minimum move confirmation" filter
    quick_losers = [t for t in trades if t.pnl_usd < 0 and t.favorable_usd < 20]
    ql_cost = sum(t.pnl_usd for t in quick_losers)
    print("\n  7. INSIGHT: Losers with <$20 favorable excursion")
    print(f"     Count: {len(quick_losers)} trades")
    print(f"     Total cost: ${ql_cost:>+,.0f}")
    print("     → These trades never moved in favor at all. A tighter entry filter would skip them.")

    # Sim 8: What if we flipped short signals to long?
    # (check if short entries would have been better as longs)
    print("\n  8. DIRECTIONAL EDGE CHECK")
    long_ts = [t for t in trades if t.direction == "long" and "TIME" in t.exit_signal.upper()]
    short_ts = [t for t in trades if t.direction == "short" and "TIME" in t.exit_signal.upper()]
    long_other = [t for t in trades if t.direction == "long" and "TIME" not in t.exit_signal.upper()]
    short_other = [t for t in trades if t.direction == "short" and "TIME" not in t.exit_signal.upper()]
    print(f"     Long TIME_STOP:  {len(long_ts)} trades, ${sum(t.pnl_usd for t in long_ts):>+,.0f}")
    print(f"     Short TIME_STOP: {len(short_ts)} trades, ${sum(t.pnl_usd for t in short_ts):>+,.0f}")
    print(f"     Long other exit: {len(long_other)} trades, ${sum(t.pnl_usd for t in long_other):>+,.0f}")
    print(f"     Short other exit:{len(short_other)} trades, ${sum(t.pnl_usd for t in short_other):>+,.0f}")


def consistency_if_filtered(trades: list[Trade], label: str):
    """Quick consistency calc for a filtered set."""
    monthly_pnl: dict[str, float] = defaultdict(float)
    for t in trades:
        monthly_pnl[t.exit_time.strftime("%Y-%m")] += t.pnl_usd
    months = sorted(monthly_pnl)
    if not months:
        return
    prof = sum(1 for m in months if monthly_pnl[m] >= 0)
    pct = prof / len(months)

    avg_w = 0.0
    avg_l = 1.0
    w_months = [monthly_pnl[m] for m in months if monthly_pnl[m] >= 0]
    l_months = [monthly_pnl[m] for m in months if monthly_pnl[m] < 0]
    if w_months:
        avg_w = sum(w_months) / len(w_months)
    if l_months:
        avg_l = abs(sum(l_months) / len(l_months))
    wl = avg_w / avg_l if avg_l else avg_w

    cw = cl = mcw = mcl = 0
    for m in months:
        if monthly_pnl[m] >= 0:
            cw += 1
            cl = 0
            mcw = max(mcw, cw)
        else:
            cl += 1
            cw = 0
            mcl = max(mcl, cl)
    sr = mcw / mcl if mcl else mcw
    score = pct * wl * sr

    net = sum(t.pnl_usd for t in trades)
    print(f"\n  [{label}]")
    pct_str = f"{100 * pct:.0f}%"
    print(
        f"  Trades: {len(trades)} | Net: ${net:>+,.0f}"
        f" | Months: {prof}/{len(months)} ({pct_str})"
        f" | Consistency: {score:.2f}"
    )


def main(csv_path: str):
    trades = parse_tv_csv(csv_path)
    if not trades:
        print("No trades found")
        sys.exit(1)

    print("=" * 70)
    print("  IRB v2.0.0 — DEEP DIAGNOSTIC ANALYSIS")
    print(f"  {len(trades)} trades, {trades[0].entry_time:%Y-%m-%d} to {trades[-1].exit_time:%Y-%m-%d}")
    print("=" * 70)

    hour_analysis(trades)
    dow_analysis(trades)
    month_seasonality(trades)
    holding_period_analysis(trades)
    direction_deep(trades)
    time_stop_analysis(trades)
    loser_analysis(trades)
    filter_simulation(trades)

    # Show consistency scores for top filter combos
    print("\n══════════════════════════════════════════════════════════════════")
    print("  CONSISTENCY SCORES FOR FILTERED VARIANTS")
    print("══════════════════════════════════════════════════════════════════")

    consistency_if_filtered(trades, "BASELINE (all trades)")
    consistency_if_filtered([t for t in trades if t.direction == "long"], "LONG ONLY")

    hour_pnl: dict[int, float] = defaultdict(float)
    for t in trades:
        hour_pnl[t.entry_time.hour] += t.pnl_usd
    worst_5h = sorted(hour_pnl, key=lambda k: hour_pnl[k])[:5]
    consistency_if_filtered([t for t in trades if t.entry_time.hour not in worst_5h], f"SKIP WORST 5 HOURS {worst_5h}")

    cal_pnl: dict[int, float] = defaultdict(float)
    for t in trades:
        cal_pnl[t.entry_time.month] += t.pnl_usd
    worst_2m = sorted(cal_pnl, key=lambda k: cal_pnl[k])[:2]
    month_names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    consistency_if_filtered(
        [t for t in trades if t.entry_time.month not in worst_2m],
        f"SKIP WORST 2 MONTHS ({', '.join(month_names[m] for m in worst_2m)})",
    )

    consistency_if_filtered(
        [t for t in trades if t.entry_time.hour not in worst_5h and t.entry_time.month not in worst_2m],
        f"COMBINED: skip hours {worst_5h} + months ({', '.join(month_names[m] for m in worst_2m)})",
    )

    consistency_if_filtered(
        [t for t in trades if t.direction == "long" and t.entry_time.hour not in worst_5h],
        "LONG ONLY + SKIP WORST 5 HOURS",
    )

    print()


if __name__ == "__main__":
    csv_file = sys.argv[1] if len(sys.argv) > 1 else "data/irb_v2_tv_1222trades_10yr.csv"
    main(csv_file)
