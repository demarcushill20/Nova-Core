"""Research: What early-bar signal separates big losers from TIME_STOP winners?

Goal: Find a v5 exit rule that cuts losers without touching TIME_STOP.
Approach: Analyze adverse excursion, hold duration, and P&L distribution
across exit types to find where the edge is.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.tv_robustness_report import parse_tv_csv

V2_PATH = "data/irb_v2_tv_1222trades_10yr.csv"
V4_PATH = "data/irb_v4_tv_results.csv"


def hours_held(t):
    return (t.exit_time - t.entry_time).total_seconds() / 3600


def main():
    v2 = parse_tv_csv(V2_PATH)
    v4 = parse_tv_csv(V4_PATH)

    print("=" * 80)
    print("  v5 RESEARCH — Finding the right exit improvement")
    print("=" * 80)

    # ── v2 Hold Duration Distribution by Exit Type ──
    print("\n── v2 HOLD DURATION BY EXIT TYPE ────────────────────────────")
    by_exit = defaultdict(list)
    for t in v2:
        by_exit[t.exit_signal].append(t)

    for sig in sorted(by_exit.keys()):
        trades = by_exit[sig]
        durations = [hours_held(t) for t in trades]
        pnls = [t.pnl_usd for t in trades]
        adv = [t.adverse_usd for t in trades]
        fav = [t.favorable_usd for t in trades]
        wins = sum(1 for p in pnls if p >= 0)
        print(f"\n  {sig} ({len(trades)} trades, {100*wins/len(trades):.0f}% WR, ${sum(pnls):+,.0f} total)")
        print(f"    Duration:  min={min(durations):.0f}h  median={sorted(durations)[len(durations)//2]:.0f}h  max={max(durations):.0f}h")
        print(f"    P&L:       min=${min(pnls):+,.0f}  avg=${sum(pnls)/len(pnls):+,.0f}  max=${max(pnls):+,.0f}")
        print(f"    Adverse:   min=${min(adv):,.0f}  avg=${sum(adv)/len(adv):,.0f}  max=${max(adv):,.0f}")
        print(f"    Favorable: min=${min(fav):,.0f}  avg=${sum(fav)/len(fav):,.0f}  max=${max(fav):,.0f}")

    # ── v2 Regular Exit Trades: Duration vs P&L ──
    print("\n\n── v2 REGULAR EXITS: P&L BY HOLD DURATION BUCKET ──────────")
    regular = [t for t in v2 if t.exit_signal in ("Long Exit", "Short Exit")]

    buckets = [
        ("0-4h", 0, 4),
        ("4-8h", 4, 8),
        ("8-12h", 8, 12),
        ("12-16h", 12, 16),
        ("16-20h", 16, 20),
        ("20-24h", 20, 24),
        ("24-30h", 24, 30),
        ("30-40h", 30, 40),
    ]

    print(f"  {'Bucket':>8} | {'Count':>5} | {'Avg P&L':>8} | {'Total':>10} | {'WR':>5} | {'Avg Adv':>8} | {'Avg Fav':>8}")
    print("  " + "-" * 75)
    for label, lo, hi in buckets:
        bt = [t for t in regular if lo <= hours_held(t) < hi]
        if not bt:
            continue
        pnls = [t.pnl_usd for t in bt]
        adv = [t.adverse_usd for t in bt]
        fav = [t.favorable_usd for t in bt]
        wins = sum(1 for p in pnls if p >= 0)
        print(f"  {label:>8} | {len(bt):>5} | ${sum(pnls)/len(pnls):>+7,.0f} | ${sum(pnls):>+9,.0f} | {100*wins/len(bt):4.0f}% | ${sum(adv)/len(adv):>7,.0f} | ${sum(fav)/len(fav):>7,.0f}")

    # ── v2 Regular Exits: Adverse Excursion Distribution ──
    print("\n\n── v2 REGULAR EXITS: P&L BY ADVERSE EXCURSION BUCKET ─────")
    adv_buckets = [
        ("$0-25", 0, 25),
        ("$25-50", 25, 50),
        ("$50-75", 50, 75),
        ("$75-100", 75, 100),
        ("$100-150", 100, 150),
        ("$150-200", 150, 200),
        ("$200-300", 200, 300),
        ("$300+", 300, 99999),
    ]

    print(f"  {'Adverse':>10} | {'Count':>5} | {'Avg P&L':>8} | {'Total':>10} | {'WR':>5} | {'Avg Hold':>8}")
    print("  " + "-" * 65)
    for label, lo, hi in adv_buckets:
        bt = [t for t in regular if lo <= abs(t.adverse_usd) < hi]
        if not bt:
            continue
        pnls = [t.pnl_usd for t in bt]
        durs = [hours_held(t) for t in bt]
        wins = sum(1 for p in pnls if p >= 0)
        print(f"  {label:>10} | {len(bt):>5} | ${sum(pnls)/len(pnls):>+7,.0f} | ${sum(pnls):>+9,.0f} | {100*wins/len(bt):4.0f}% | {sum(durs)/len(durs):>6.0f}h")

    # ── v2 TIME_STOP: Favorable excursion at various bar checkpoints ──
    print("\n\n── v2 TIME_STOP: FAVORABLE EXCURSION STATS ─────────────────")
    ts = [t for t in v2 if t.exit_signal == "TIME_STOP"]
    print(f"  TIME_STOP trades: {len(ts)}")
    fav_ts = [t.favorable_usd for t in ts]
    adv_ts = [t.adverse_usd for t in ts]
    print(f"    Favorable: min=${min(fav_ts):,.0f}  avg=${sum(fav_ts)/len(fav_ts):,.0f}  max=${max(fav_ts):,.0f}")
    print(f"    Adverse:   min=${min(adv_ts):,.0f}  avg=${sum(adv_ts)/len(adv_ts):,.0f}  max=${max(adv_ts):,.0f}")

    # How many TIME_STOP had high adverse excursion?
    print("\n  TIME_STOP by adverse excursion:")
    for threshold in [25, 50, 75, 100, 150, 200, 300]:
        above = [t for t in ts if abs(t.adverse_usd) >= threshold]
        pnl = sum(t.pnl_usd for t in above)
        print(f"    Adverse ≥${threshold}: {len(above)}/{len(ts)} trades (${pnl:+,.0f})")

    # ── v4 STAG_EXIT Trades: What happened to them? ──
    print("\n\n── v4 STAG_EXIT ANALYSIS ────────────────────────────────────")
    stag = [t for t in v4 if t.exit_signal == "STAG_EXIT"]
    print(f"  STAG_EXIT: {len(stag)} trades")
    for t in sorted(stag, key=lambda x: x.pnl_usd):
        print(f"    #{t.number:4d} {t.direction:>5} {t.entry_time:%Y-%m-%d %H:%M} → {t.exit_time:%Y-%m-%d %H:%M} | ${t.pnl_usd:>+6,.0f} | fav=${t.favorable_usd:>4,.0f} adv=${t.adverse_usd:>4,.0f} | {hours_held(t):.0f}h")

    # ── What v2 exit happened at similar times? (trades that STAG_EXIT replaced) ──
    print("\n\n── STAG_EXIT vs WHAT V2 DID WITH SAME TRADES ───────────────")
    print("  (Matching by entry time ± 1h to find corresponding v2 trades)")
    stag_matched = 0
    stag_better = 0
    stag_worse = 0
    stag_saved = 0.0
    for st in stag:
        # Find v2 trade with closest entry time
        best_match = None
        best_diff = 99999
        for v2t in v2:
            diff = abs((st.entry_time - v2t.entry_time).total_seconds())
            if diff < best_diff and st.direction == v2t.direction:
                best_diff = diff
                best_match = v2t
        if best_match and best_diff < 3600:  # within 1 hour
            stag_matched += 1
            delta = st.pnl_usd - best_match.pnl_usd
            saved = delta  # positive = stag saved money
            stag_saved += saved
            marker = "✓ SAVED" if delta > 0 else "✗ WORSE"
            if delta > 0:
                stag_better += 1
            else:
                stag_worse += 1
            print(f"    #{st.number:4d} STAG=${st.pnl_usd:>+6,.0f} vs v2=${best_match.pnl_usd:>+6,.0f} (v2 exit: {best_match.exit_signal:>10}, held {hours_held(best_match):.0f}h) | Δ${delta:>+6,.0f} {marker}")

    print(f"\n  Matched: {stag_matched}/{len(stag)}")
    print(f"  STAG better: {stag_better} | STAG worse: {stag_worse}")
    print(f"  Net impact: ${stag_saved:>+,.0f}")

    # ── KEY QUESTION: Among regular exit trades, which ones hurt most? ──
    print("\n\n── BIGGEST LOSERS AMONG REGULAR EXITS ───────────────────────")
    regular_sorted = sorted(regular, key=lambda t: t.pnl_usd)
    print("  Bottom 20 regular exit trades:")
    for t in regular_sorted[:20]:
        print(f"    #{t.number:4d} {t.direction:>5} ${t.pnl_usd:>+6,.0f} | held {hours_held(t):>4.0f}h | adv=${t.adverse_usd:>4,.0f} fav=${t.favorable_usd:>4,.0f} | {t.exit_signal}")

    # ── P&L if we cut all trades with adverse > threshold early ──
    print("\n\n── SIMULATION: CUT TRADES WITH HIGH ADVERSE EXCURSION ──────")
    print("  (What if we added a hard stop at various levels?)")
    print(f"  {'Hard Stop':>10} | {'Would Cut':>9} | {'Cut P&L':>9} | {'TS Lost':>7} | {'TS P&L Lost':>11} | {'Net Impact':>10}")
    print("  " + "-" * 75)

    for stop_level in [50, 75, 100, 125, 150, 175, 200, 250, 300]:
        # Regular exits with adverse > stop_level
        would_cut = [t for t in regular if abs(t.adverse_usd) >= stop_level]
        cut_pnl = sum(t.pnl_usd for t in would_cut)
        # TIME_STOP trades with adverse > stop_level (would be killed)
        ts_killed = [t for t in ts if abs(t.adverse_usd) >= stop_level]
        ts_pnl_lost = sum(t.pnl_usd for t in ts_killed)
        # Net: we save on regular exits but lose TIME_STOP winners
        # Assume cut trades would have lost ~$stop_level instead of their actual P&L
        savings = sum(max(0, -t.pnl_usd - stop_level) for t in would_cut)  # how much we save
        net = savings - ts_pnl_lost if ts_killed else savings
        print(f"  ${stop_level:>8} | {len(would_cut):>9} | ${cut_pnl:>+8,.0f} | {len(ts_killed):>7} | ${ts_pnl_lost:>+10,.0f} | ${net:>+9,.0f}")

    print()


if __name__ == "__main__":
    main()
