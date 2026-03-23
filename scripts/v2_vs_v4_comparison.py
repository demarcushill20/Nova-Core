"""Head-to-head comparison: IRB v2 (champion) vs v4 (challenger).

Full 10yr, 5yr, 3yr, year-by-year, and month-by-month comparison.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, "/home/nova/nova-core")
from scripts.tv_robustness_report import Trade, analyze_period, parse_tv_csv

V2_PATH = "/home/nova/nova-core/data/irb_v2_tv_1222trades_10yr.csv"
V4_PATH = "/home/nova/nova-core/data/irb_v4_tv_results.csv"


def exit_breakdown(trades: list[Trade]) -> dict[str, dict]:
    """Return {exit_signal: {count, total_pnl, avg_pnl, win_rate}}."""
    by_exit: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_exit[t.exit_signal].append(t.pnl_usd)
    result = {}
    for sig, pnls in sorted(by_exit.items(), key=lambda x: -sum(x[1])):
        wins = sum(1 for p in pnls if p >= 0)
        result[sig] = {
            "count": len(pnls),
            "total_pnl": sum(pnls),
            "avg_pnl": sum(pnls) / len(pnls),
            "win_rate": 100 * wins / len(pnls),
        }
    return result


def direction_stats(trades: list[Trade]) -> dict[str, dict]:
    result = {}
    for d in ["long", "short"]:
        dt = [t for t in trades if t.direction == d]
        if not dt:
            continue
        net = sum(t.pnl_usd for t in dt)
        wins = sum(1 for t in dt if t.pnl_usd >= 0)
        result[d] = {
            "count": len(dt),
            "net_pnl": net,
            "win_rate": 100 * wins / len(dt),
            "avg": net / len(dt),
        }
    return result


def period_trades(trades: list[Trade], days: int | None, ref_date: datetime) -> list[Trade]:
    if days is None:
        return trades
    cutoff = ref_date - timedelta(days=days)
    return [t for t in trades if t.exit_time >= cutoff]


def compare_metric(v2_val: float, v4_val: float, higher_is_better: bool = True, fmt: str = "+,.0f") -> str:
    diff = v4_val - v2_val
    arrow = ""
    if higher_is_better:
        arrow = "▲" if diff > 0 else "▼" if diff < 0 else "="
    else:
        arrow = "▼" if diff < 0 else "▲" if diff > 0 else "="
        # For lower-is-better, positive diff is bad
    return f"${diff:{fmt}} {arrow}" if "," in fmt else f"{diff:{fmt}} {arrow}"


def print_header():
    print("=" * 90)
    print("  NOVATRADE IRB — HEAD-TO-HEAD: v2.0.0 (CHAMPION) vs v4.0.0 (CHALLENGER)")
    print("=" * 90)


def print_section(title: str):
    print(f"\n{'─' * 90}")
    print(f"  {title}")
    print(f"{'─' * 90}")


def run():  # noqa: C901
    v2_trades = parse_tv_csv(V2_PATH)
    v4_trades = parse_tv_csv(V4_PATH)

    print_header()
    print(f"  v2: {len(v2_trades)} trades | {v2_trades[0].entry_time:%Y-%m-%d} → {v2_trades[-1].exit_time:%Y-%m-%d}")
    print(f"  v4: {len(v4_trades)} trades | {v4_trades[0].entry_time:%Y-%m-%d} → {v4_trades[-1].exit_time:%Y-%m-%d}")

    # ── EXIT TYPE COMPARISON ──
    print_section("EXIT TYPE COMPARISON")
    v2_exits = exit_breakdown(v2_trades)
    v4_exits = exit_breakdown(v4_trades)
    all_exits = sorted(set(list(v2_exits.keys()) + list(v4_exits.keys())))

    hdr = (
        f"  {'Exit Type':>18} | {'v2 Count':>8} {'v2 P&L':>10} {'v2 Avg':>8}"
        f" | {'v4 Count':>8} {'v4 P&L':>10} {'v4 Avg':>8} | {'Δ P&L':>10}"
    )
    print(hdr)
    print("  " + "-" * 86)
    for sig in all_exits:
        v2e = v2_exits.get(sig, {"count": 0, "total_pnl": 0, "avg_pnl": 0, "win_rate": 0})
        v4e = v4_exits.get(sig, {"count": 0, "total_pnl": 0, "avg_pnl": 0, "win_rate": 0})
        delta = v4e["total_pnl"] - v2e["total_pnl"]
        v2_part = f"{v2e['count']:>8} ${v2e['total_pnl']:>+9,.0f} ${v2e['avg_pnl']:>+7,.0f}"
        v4_part = f"{v4e['count']:>8} ${v4e['total_pnl']:>+9,.0f} ${v4e['avg_pnl']:>+7,.0f}"
        print(f"  {sig:>18} | {v2_part} | {v4_part} | ${delta:>+9,.0f}")

    v2_total = sum(e["total_pnl"] for e in v2_exits.values())
    v4_total = sum(e["total_pnl"] for e in v4_exits.values())
    print(
        f"  {'TOTAL':>18} | {len(v2_trades):>8} ${v2_total:>+9,.0f}         "
        f" | {len(v4_trades):>8} ${v4_total:>+9,.0f}         "
        f" | ${v4_total - v2_total:>+9,.0f}"
    )

    # ── DIRECTION COMPARISON ──
    print_section("DIRECTION COMPARISON")
    v2_dir = direction_stats(v2_trades)
    v4_dir = direction_stats(v4_trades)
    for d in ["long", "short"]:
        if d in v2_dir and d in v4_dir:
            v2d, v4d = v2_dir[d], v4_dir[d]
            v2_s = f"v2: {v2d['count']:4d} trades ${v2d['net_pnl']:>+9,.0f} {v2d['win_rate']:.0f}% WR"
            v4_s = f"v4: {v4d['count']:4d} trades ${v4d['net_pnl']:>+9,.0f} {v4d['win_rate']:.0f}% WR"
            delta_pnl = v4d["net_pnl"] - v2d["net_pnl"]
            print(f"  {d.upper():>5} | {v2_s} | {v4_s} | Δ ${delta_pnl:>+9,.0f}")

    # ── FULL PERIOD HEAD-TO-HEAD ──
    print_section("FULL PERIOD — 10 YEAR")
    v2_full = analyze_period(v2_trades, "v2")
    v4_full = analyze_period(v4_trades, "v4")

    metrics = [
        ("Net P&L", "net_pnl", True, "$", ">+,.0f"),
        ("Trades", "trades", None, "", ">d"),
        ("Trade Win Rate", "trade_win_rate", True, "", ">.1f"),
        ("Avg Win Trade", "avg_win_trade", True, "$", ">,.0f"),
        ("Avg Loss Trade", "avg_loss_trade", False, "$", ">,.0f"),
        ("Profitable Months", "profitable_months", True, "", ">d"),
        ("Total Months", "total_months", None, "", ">d"),
        ("% Prof Months", "pct_profitable_months", True, "", ">.0%"),
        ("Avg Win Month", "avg_win_month", True, "$", ">,.0f"),
        ("Avg Loss Month", "avg_loss_month", False, "$", ">,.0f"),
        ("Max Consec Win", "max_consec_win", True, "", ">d"),
        ("Max Consec Loss", "max_consec_loss", False, "", ">d"),
        ("Max Drawdown", "max_dd", False, "$", ">,.0f"),
        ("Max DD Duration", "max_dd_months", False, "", ">d"),
        ("W/L Ratio", "wl_ratio", True, "", ">.2f"),
        ("Streak Ratio", "streak_ratio", True, "", ">.2f"),
        ("CONSISTENCY", "consistency", True, "", ">.2f"),
    ]

    print(f"  {'Metric':>22} | {'v2':>12} | {'v4':>12} | {'Delta':>12} | {'Better':>6}")
    print("  " + "-" * 75)
    for label, key, higher_better, prefix, fmt in metrics:
        v2v = v2_full[key]
        v4v = v4_full[key]
        v2_str = f"{prefix}{v2v:{fmt}}"
        v4_str = f"{prefix}{v4v:{fmt}}"

        if higher_better is None:
            delta_str = ""
            winner = ""
        else:
            diff = v4v - v2v
            if "%" in fmt:
                delta_str = f"{diff:>+.0%}"
            elif "d" in fmt:
                delta_str = f"{diff:>+d}"
            elif "f" in fmt:
                delta_str = f"{prefix}{diff:>+,.2f}" if "." in fmt else f"{prefix}{diff:>+,.0f}"
            else:
                delta_str = f"{prefix}{diff:>+,.0f}"

            if higher_better:
                winner = "v4" if diff > 0 else "v2" if diff < 0 else "TIE"
            else:
                winner = "v4" if diff < 0 else "v2" if diff > 0 else "TIE"

        bold = " ◀◀" if label == "CONSISTENCY" else ""
        print(f"  {label:>22} | {v2_str:>12} | {v4_str:>12} | {delta_str:>12} | {winner:>6}{bold}")

    # ── PERIOD BREAKDOWN ──
    ref_v2 = v2_trades[-1].exit_time
    ref_v4 = v4_trades[-1].exit_time

    for period_label, days in [("5 YEAR", 5 * 365), ("3 YEAR", 3 * 365), ("1 YEAR", 365)]:
        print_section(f"{period_label} PERIOD")
        v2p = period_trades(v2_trades, days, ref_v2)
        v4p = period_trades(v4_trades, days, ref_v4)
        v2s = analyze_period(v2p, f"v2 {period_label}")
        v4s = analyze_period(v4p, f"v4 {period_label}")

        print(f"  {'':>22} | {'v2':>12} | {'v4':>12} | {'Winner':>6}")
        print("  " + "-" * 60)
        for label, key, higher_better in [
            ("Net P&L", "net_pnl", True),
            ("Trades", "trades", None),
            ("Trade Win Rate %", "trade_win_rate", True),
            ("Prof Months", "profitable_months", True),
            ("Total Months", "total_months", None),
            ("Max DD", "max_dd", False),
            ("Consec Loss Mo", "max_consec_loss", False),
            ("W/L Ratio", "wl_ratio", True),
            ("CONSISTENCY", "consistency", True),
        ]:
            v2v = v2s[key]
            v4v = v4s[key]
            if isinstance(v2v, float):
                v2_str = f"${v2v:>+,.0f}" if key in ("net_pnl", "max_dd") else f"{v2v:.2f}"
                v4_str = f"${v4v:>+,.0f}" if key in ("net_pnl", "max_dd") else f"{v4v:.2f}"
            else:
                v2_str = str(v2v)
                v4_str = str(v4v)

            if higher_better is None:
                winner = ""
            elif higher_better:
                winner = "v4" if v4v > v2v else "v2" if v2v > v4v else "TIE"
            else:
                winner = "v4" if v4v < v2v else "v2" if v2v < v4v else "TIE"

            bold = " ◀" if label == "CONSISTENCY" else ""
            print(f"  {label:>22} | {v2_str:>12} | {v4_str:>12} | {winner:>6}{bold}")

    # ── YEAR-BY-YEAR ──
    print_section("YEAR-BY-YEAR COMPARISON")
    v2_yearly: dict[str, float] = defaultdict(float)
    v4_yearly: dict[str, float] = defaultdict(float)
    v2_yearly_n: dict[str, int] = defaultdict(int)
    v4_yearly_n: dict[str, int] = defaultdict(int)

    for t in v2_trades:
        y = t.exit_time.strftime("%Y")
        v2_yearly[y] += t.pnl_usd
        v2_yearly_n[y] += 1
    for t in v4_trades:
        y = t.exit_time.strftime("%Y")
        v4_yearly[y] += t.pnl_usd
        v4_yearly_n[y] += 1

    all_years = sorted(set(list(v2_yearly.keys()) + list(v4_yearly.keys())))

    hdr = (
        f"  {'Year':>6} | {'v2 Trades':>9} {'v2 P&L':>10}"
        f" | {'v4 Trades':>9} {'v4 P&L':>10}"
        f" | {'Δ P&L':>10} {'Winner':>6}"
    )
    print(hdr)
    print("  " + "-" * 75)
    v2_wins_yr = v4_wins_yr = 0
    for y in all_years:
        v2p = v2_yearly[y]
        v4p = v4_yearly[y]
        delta = v4p - v2p
        winner = "v4" if delta > 0 else "v2" if delta < 0 else "TIE"
        if delta > 0:
            v4_wins_yr += 1
        elif delta < 0:
            v2_wins_yr += 1
        print(
            f"  {y:>6} | {v2_yearly_n[y]:>9} ${v2p:>+9,.0f}"
            f" | {v4_yearly_n[y]:>9} ${v4p:>+9,.0f}"
            f" | ${delta:>+9,.0f} {winner:>6}"
        )

    print(f"\n  Year wins: v2={v2_wins_yr} | v4={v4_wins_yr}")

    # ── MONTH-BY-MONTH ──
    print_section("MONTH-BY-MONTH COMPARISON")
    v2_monthly: dict[str, float] = defaultdict(float)
    v4_monthly: dict[str, float] = defaultdict(float)

    for t in v2_trades:
        m = t.exit_time.strftime("%Y-%m")
        v2_monthly[m] += t.pnl_usd
    for t in v4_trades:
        m = t.exit_time.strftime("%Y-%m")
        v4_monthly[m] += t.pnl_usd

    all_months = sorted(set(list(v2_monthly.keys()) + list(v4_monthly.keys())))

    print(f"  {'Month':>8} | {'v2 P&L':>10} | {'v4 P&L':>10} | {'Δ':>10} | W")
    print("  " + "-" * 55)
    v2_mo_wins = v4_mo_wins = ties = 0
    v2_cum = v4_cum = 0.0
    for m in all_months:
        v2p = v2_monthly[m]
        v4p = v4_monthly[m]
        v2_cum += v2p
        v4_cum += v4p
        delta = v4p - v2p
        if delta > 0:
            w = "v4"
            v4_mo_wins += 1
        elif delta < 0:
            w = "v2"
            v2_mo_wins += 1
        else:
            w = "="
            ties += 1
        print(f"  {m:>8} | ${v2p:>+9,.0f} | ${v4p:>+9,.0f} | ${delta:>+9,.0f} | {w}")

    print(f"\n  Month wins: v2={v2_mo_wins} | v4={v4_mo_wins} | ties={ties}")
    print(f"  Cumulative: v2=${v2_cum:>+,.0f} | v4=${v4_cum:>+,.0f}")

    # ── STAGNATION GUARD IMPACT ──
    print_section("STAGNATION GUARD IMPACT (v4 NEW FEATURE)")
    stag_trades = [t for t in v4_trades if t.exit_signal == "STAG_EXIT"]
    if stag_trades:
        stag_pnls = [t.pnl_usd for t in stag_trades]
        stag_wins = sum(1 for p in stag_pnls if p >= 0)
        print(f"  STAG_EXIT trades: {len(stag_trades)}")
        print(f"  STAG_EXIT total P&L: ${sum(stag_pnls):>+,.0f}")
        print(f"  STAG_EXIT avg P&L: ${sum(stag_pnls) / len(stag_pnls):>+,.0f}")
        print(f"  STAG_EXIT win rate: {100 * stag_wins / len(stag_trades):.0f}%")
        print(f"  STAG_EXIT best: ${max(stag_pnls):>+,.0f}")
        print(f"  STAG_EXIT worst: ${min(stag_pnls):>+,.0f}")

        # Compare TIME_STOP between versions
        v2_ts = [t for t in v2_trades if t.exit_signal == "TIME_STOP"]
        v4_ts = [t for t in v4_trades if t.exit_signal == "TIME_STOP"]
        print("\n  TIME_STOP preservation check:")
        print(f"    v2 TIME_STOP: {len(v2_ts)} trades, ${sum(t.pnl_usd for t in v2_ts):>+,.0f}")
        print(f"    v4 TIME_STOP: {len(v4_ts)} trades, ${sum(t.pnl_usd for t in v4_ts):>+,.0f}")
        ts_delta = len(v4_ts) - len(v2_ts)
        print(f"    Delta: {ts_delta:+d} trades")
        if ts_delta >= 0:
            print(f"    ✓ TIME_STOP count preserved (target: ≥{len(v2_ts)})")
        else:
            print(f"    ⚠ TIME_STOP count DROPPED by {abs(ts_delta)} trades!")
    else:
        print("  No STAG_EXIT trades found — guard may not be triggering")

    # ── FINAL VERDICT ──
    print_section("FINAL VERDICT")
    c2 = v2_full["consistency"]
    c4 = v4_full["consistency"]
    pnl2 = v2_full["net_pnl"]
    pnl4 = v4_full["net_pnl"]

    v4_score = 0
    checks = [
        ("Net P&L", pnl4 > pnl2),
        ("Consistency", c4 > c2),
        ("Year wins", v4_wins_yr > v2_wins_yr),
        ("Month wins", v4_mo_wins > v2_mo_wins),
        ("Lower max DD", v4_full["max_dd"] < v2_full["max_dd"]),
        ("Fewer consec loss", v4_full["max_consec_loss"] < v2_full["max_consec_loss"]),
        ("Higher trade WR", v4_full["trade_win_rate"] > v2_full["trade_win_rate"]),
    ]

    for label, v4_better in checks:
        marker = "✓" if v4_better else "✗"
        who = "v4" if v4_better else "v2"
        print(f"  {marker} {label:>25}: {who}")
        if v4_better:
            v4_score += 1

    print(f"\n  v4 wins {v4_score}/{len(checks)} criteria")

    print("\n  ╔══════════════════════════════════════════════════╗")
    if v4_score > len(checks) // 2:
        print("  ║  CHAMPION: v4.0.0 (stagnation guard)              ║")
        print(f"  ║  v4 ${pnl4:>+,.0f} (C={c4:.2f}) > v2 ${pnl2:>+,.0f} (C={c2:.2f})  ║")
    elif v4_score == len(checks) // 2:
        print("  ║  DRAW — No clear winner                           ║")
        print(f"  ║  v2 ${pnl2:>+,.0f} (C={c2:.2f}) ≈ v4 ${pnl4:>+,.0f} (C={c4:.2f})  ║")
    else:
        print("  ║  CHAMPION: v2.0.0 (still king)                    ║")
        print(f"  ║  v2 ${pnl2:>+,.0f} (C={c2:.2f}) > v4 ${pnl4:>+,.0f} (C={c4:.2f})  ║")
    print("  ╚══════════════════════════════════════════════════╝")
    print()


if __name__ == "__main__":
    run()
