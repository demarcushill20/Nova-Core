"""Quick parity diagnostic: Pine v5 baseline vs internal Python IRBBacktester.

Compares trade counts and aggregate stats year-by-year on the same EURUSD
10yr window, to identify where the Python engine is over-firing.
"""

import csv
import os
from collections import Counter, defaultdict
from datetime import datetime

PINE = "/home/nova/nova-core/data/irb_novatrade_irb_v5_results_extracted.csv"
OUT_DIR = "/home/nova/nova-core/OUTPUT"


def load_pine():
    pine_year = Counter()
    pine_pnls = defaultdict(list)
    pine_signals = Counter()
    with open(PINE) as f:
        for r in csv.DictReader(f):
            if r["Type"].lower().startswith("exit"):
                dt = datetime.strptime(r["Date and time"], "%Y-%m-%d %H:%M")
                pine_year[dt.year] += 1
                pine_pnls[dt.year].append(float(r["Net P&L USD"]))
                pine_signals[r["Signal"].strip()] += 1
    return pine_year, pine_pnls, pine_signals


def load_python():
    out_files = sorted(
        p for p in os.listdir(OUT_DIR) if p.startswith("backtest_10yr_irb_v5_") and p.endswith("_trades.csv")
    )
    last = os.path.join(OUT_DIR, out_files[-1])
    print(f"Python trade CSV: {last}")
    py_year = Counter()
    py_pnls = defaultdict(list)
    py_reasons = Counter()
    py_sides = Counter()
    py_holds = []
    with open(last) as f:
        for r in csv.DictReader(f):
            y = int(r["exit_ts"][:4])
            py_year[y] += 1
            py_pnls[y].append(float(r["pnl_usd"]))
            py_reasons[r["exit_reason"]] += 1
            py_sides[r["side"]] += 1
            py_holds.append(int(r["hold_bars"]))
    return py_year, py_pnls, py_reasons, py_sides, py_holds


def main():
    pine_year, pine_pnls, pine_signals = load_pine()
    py_year, py_pnls, py_reasons, py_sides, py_holds = load_python()

    print()
    print("Yearly trade-count comparison")
    print(f"{'Year':>6} {'Pine':>8} {'Python':>10} {'Ratio':>8}    {'Pine NetPnL':>14}    {'Py NetPnL':>14}")
    for y in sorted(set(pine_year) | set(py_year)):
        p = pine_year.get(y, 0)
        q = py_year.get(y, 0)
        ppnl = sum(pine_pnls.get(y, []))
        qpnl = sum(py_pnls.get(y, []))
        ratio = q / max(p, 1)
        print(f"{y:>6} {p:>8} {q:>10} {ratio:>7.1f}x    ${ppnl:>12,.0f}    ${qpnl:>12,.0f}")
    print(
        f"{'Total':>6} {sum(pine_year.values()):>8} {sum(py_year.values()):>10} "
        f"{sum(py_year.values()) / sum(pine_year.values()):>7.1f}x    "
        f"${sum(sum(v) for v in pine_pnls.values()):>12,.0f}    "
        f"${sum(sum(v) for v in py_pnls.values()):>12,.0f}"
    )

    print()
    print("Pine exit-signal mix:")
    for s, n in pine_signals.most_common():
        print(f"  {s:<14} {n:>5}  ({n / sum(pine_signals.values()) * 100:5.1f}%)")

    print()
    print("Python exit-reason mix:")
    for s, n in py_reasons.most_common():
        print(f"  {s:<14} {n:>6}  ({n / sum(py_reasons.values()) * 100:5.1f}%)")

    print()
    print("Python side mix:")
    for s, n in py_sides.most_common():
        print(f"  {s:<14} {n:>6}")

    print()
    py_holds_sorted = sorted(py_holds)
    n = len(py_holds_sorted)
    print("Python hold-bars distribution (M5 bars):")
    print(
        f"  min={py_holds_sorted[0]}  p10={py_holds_sorted[n // 10]}  median={py_holds_sorted[n // 2]}  "
        f"p90={py_holds_sorted[int(n * 0.9)]}  max={py_holds_sorted[-1]}"
    )
    print(f"  mean={sum(py_holds) / n:.1f} bars  ({sum(py_holds) / n * 5:.0f} min, ~{sum(py_holds) / n * 5 / 60:.1f}h)")

    # Estimate effect of partial-exit double-counting
    # _partial_close_position adds a trade record for the partial leg, so each
    # full-volume entry that took a partial counts as 2 trades.
    # Pine TIME_STOP rate ~ 9%. If Python TRAILING_STOP includes partials, that
    # tells us how many.
    trailing = py_reasons.get("TRAILING_STOP", 0)
    sl = py_reasons.get("STOP_LOSS", 0)
    time_stop = py_reasons.get("TIME_STOP", 0)
    total = sum(py_reasons.values())
    print()
    print("Estimating partial-exit duplication:")
    print(f"  Total Python trades: {total}")
    print(f"  STOP_LOSS:           {sl}")
    print(f"  TRAILING_STOP:       {trailing}  (includes partial exits per engine.py:947)")
    print(f"  TIME_STOP:           {time_stop}")
    # If every entry took a 50% partial then partials ≈ # full-position trades
    # Effective unique entries ≈ (SL + TRAIL/2 + TIME_STOP)
    effective = sl + trailing // 2 + time_stop
    print(f"  ~Effective unique entries (assuming half of TRAILING are partials): {effective}")
    print(f"  Pine total trades: {sum(pine_year.values())}")
    print(f"  Remaining mismatch: {effective / sum(pine_year.values()):.1f}x")


if __name__ == "__main__":
    main()
