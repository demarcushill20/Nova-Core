"""Robustness report from TradingView strategy tester CSV export.

Parses TradingView's dual-row (Entry+Exit) CSV format and generates:
- Monthly P&L waterfall with visual bars
- Yearly breakdown
- Rolling 12-month returns
- Consecutive win/loss streaks
- Consistency scoring with diagnosis
- Period breakdown (10yr/5yr/3yr/1yr)
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Trade:
    number: int
    direction: str  # "long" or "short"
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    signal: str
    exit_signal: str
    pnl_usd: float
    pnl_pct: float
    favorable_usd: float
    adverse_usd: float
    cumulative_pnl: float


def parse_tv_csv(path: str | Path) -> list[Trade]:
    """Parse TradingView Strategy Tester CSV into Trade objects.

    TradingView exports 2 rows per trade: Exit row first, then Entry row.
    Both rows share the same Trade # and P&L values.
    """
    trades: dict[int, dict] = {}

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trade_num = int(row["Trade #"])
            row_type = row["Type"].strip()  # "Entry long", "Exit long", etc.

            if trade_num not in trades:
                trades[trade_num] = {}

            if row_type.startswith("Entry"):
                trades[trade_num]["entry_time"] = datetime.strptime(row["Date and time"].strip(), "%Y-%m-%d %H:%M")
                trades[trade_num]["entry_price"] = float(row["Price USD"])
                trades[trade_num]["direction"] = "long" if "long" in row_type else "short"
                trades[trade_num]["signal"] = row["Signal"].strip()
            elif row_type.startswith("Exit"):
                trades[trade_num]["exit_time"] = datetime.strptime(row["Date and time"].strip(), "%Y-%m-%d %H:%M")
                trades[trade_num]["exit_price"] = float(row["Price USD"])
                trades[trade_num]["exit_signal"] = row["Signal"].strip()
                trades[trade_num]["pnl_usd"] = float(row["Net P&L USD"])
                trades[trade_num]["pnl_pct"] = float(row["Net P&L %"])
                trades[trade_num]["favorable_usd"] = float(row["Favorable excursion USD"])
                trades[trade_num]["adverse_usd"] = float(row["Adverse excursion USD"])
                trades[trade_num]["cumulative_pnl"] = float(row["Cumulative P&L USD"])

    result = []
    for num in sorted(trades.keys()):
        t = trades[num]
        if "entry_time" not in t or "exit_time" not in t:
            continue
        result.append(
            Trade(
                number=num,
                direction=t["direction"],
                entry_time=t["entry_time"],
                exit_time=t["exit_time"],
                entry_price=t["entry_price"],
                exit_price=t["exit_price"],
                signal=t["signal"],
                exit_signal=t.get("exit_signal", ""),
                pnl_usd=t["pnl_usd"],
                pnl_pct=t["pnl_pct"],
                favorable_usd=t["favorable_usd"],
                adverse_usd=t["adverse_usd"],
                cumulative_pnl=t["cumulative_pnl"],
            )
        )

    return result


def analyze_period(trades: list[Trade], label: str) -> dict:
    """Analyze a subset of trades and return metrics dict."""
    if not trades:
        return {"label": label, "trades": 0}

    monthly_pnl: dict[str, float] = defaultdict(float)
    monthly_trades: dict[str, int] = defaultdict(int)
    monthly_wins: dict[str, int] = defaultdict(int)
    monthly_losses: dict[str, int] = defaultdict(int)

    winners = 0
    losers = 0
    total_win = 0.0
    total_loss = 0.0
    biggest_win = 0.0
    biggest_loss = 0.0

    for t in trades:
        key = t.exit_time.strftime("%Y-%m")
        monthly_pnl[key] += t.pnl_usd
        monthly_trades[key] += 1
        if t.pnl_usd >= 0:
            monthly_wins[key] += 1
            winners += 1
            total_win += t.pnl_usd
            biggest_win = max(biggest_win, t.pnl_usd)
        else:
            monthly_losses[key] += 1
            losers += 1
            total_loss += t.pnl_usd
            biggest_loss = min(biggest_loss, t.pnl_usd)

    months = sorted(monthly_pnl.keys())
    profitable_months = sum(1 for m in months if monthly_pnl[m] >= 0)
    losing_months = len(months) - profitable_months

    # Consecutive month streaks
    consec_win = max_consec_win = 0
    consec_loss = max_consec_loss = 0
    for m in months:
        if monthly_pnl[m] >= 0:
            consec_win += 1
            consec_loss = 0
            max_consec_win = max(max_consec_win, consec_win)
        else:
            consec_loss += 1
            consec_win = 0
            max_consec_loss = max(max_consec_loss, consec_loss)

    # Max drawdown (monthly cumulative)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    dd_months = 0
    max_dd_months = 0
    for m in months:
        cum += monthly_pnl[m]
        if cum > peak:
            peak = cum
            dd_months = 0
        dd = peak - cum
        max_dd = max(max_dd, dd)
        if cum < peak:
            dd_months += 1
            max_dd_months = max(max_dd_months, dd_months)

    total_months = len(months)
    pct_profitable = profitable_months / total_months if total_months else 0
    avg_win = total_win / winners if winners else 0
    avg_loss = abs(total_loss / losers) if losers else 1
    wl_ratio = avg_win / avg_loss if avg_loss else avg_win
    streak_ratio = max_consec_win / max_consec_loss if max_consec_loss else max_consec_win
    consistency = pct_profitable * wl_ratio * streak_ratio

    net_pnl = sum(t.pnl_usd for t in trades)
    trade_win_rate = 100 * winners / len(trades) if trades else 0

    return {
        "label": label,
        "trades": len(trades),
        "net_pnl": net_pnl,
        "trade_win_rate": trade_win_rate,
        "winners": winners,
        "losers": losers,
        "avg_win_trade": avg_win if winners else 0,
        "avg_loss_trade": abs(total_loss / losers) if losers else 0,
        "biggest_win": biggest_win,
        "biggest_loss": biggest_loss,
        "total_months": total_months,
        "profitable_months": profitable_months,
        "losing_months": losing_months,
        "pct_profitable_months": pct_profitable,
        "avg_win_month": sum(monthly_pnl[m] for m in months if monthly_pnl[m] >= 0) / profitable_months
        if profitable_months
        else 0,
        "avg_loss_month": abs(sum(monthly_pnl[m] for m in months if monthly_pnl[m] < 0)) / losing_months
        if losing_months
        else 0,
        "max_consec_win": max_consec_win,
        "max_consec_loss": max_consec_loss,
        "max_dd": max_dd,
        "max_dd_months": max_dd_months,
        "wl_ratio": wl_ratio,
        "streak_ratio": streak_ratio,
        "consistency": consistency,
        "monthly_pnl": monthly_pnl,
        "monthly_trades": monthly_trades,
        "monthly_wins": monthly_wins,
        "monthly_losses": monthly_losses,
        "months": months,
    }


def print_waterfall(stats: dict):
    """Print monthly P&L waterfall chart."""
    months = stats["months"]
    monthly_pnl = stats["monthly_pnl"]
    monthly_trades = stats["monthly_trades"]
    monthly_wins = stats["monthly_wins"]
    monthly_losses = stats["monthly_losses"]

    print(f"\n{'Month':>8} | {'Trades':>6} | {'W/L':>5} | {'P&L':>10} | {'Cum P&L':>10} | Bar")
    print("-" * 76)

    cum = 0.0
    for m in months:
        pnl = monthly_pnl[m]
        cum += pnl
        w = monthly_wins[m]
        lo = monthly_losses[m]
        n = monthly_trades[m]
        if pnl >= 0:
            marker = "+" * min(int(pnl / 100) + 1, 30)
        else:
            marker = "-" * min(int(abs(pnl) / 100) + 1, 30)
        print(f"  {m} | {n:6d} | {w:2d}/{lo:<2d} | ${pnl:>+9,.0f} | ${cum:>+9,.0f} | {marker}")


def print_yearly(stats: dict):
    """Print yearly breakdown."""
    months = stats["months"]
    monthly_pnl = stats["monthly_pnl"]
    monthly_trades = stats["monthly_trades"]

    yearly_pnl: dict[str, float] = defaultdict(float)
    yearly_trades: dict[str, int] = defaultdict(int)
    yearly_months_prof: dict[str, int] = defaultdict(int)
    yearly_months_total: dict[str, int] = defaultdict(int)

    for m in months:
        year = m[:4]
        yearly_pnl[year] += monthly_pnl[m]
        yearly_trades[year] += monthly_trades[m]
        yearly_months_total[year] += 1
        if monthly_pnl[m] >= 0:
            yearly_months_prof[year] += 1

    print(f"\n{'Year':>6} | {'Trades':>6} | {'P&L':>10} | {'Prof Mo':>8} | {'Mo Win%':>7} | {'Avg/Mo':>8}")
    print("-" * 65)
    for year in sorted(yearly_pnl.keys()):
        yt = yearly_trades[year]
        yp = yearly_pnl[year]
        pm = yearly_months_prof[year]
        tm = yearly_months_total[year]
        pct = 100 * pm / tm if tm else 0
        avg_mo = yp / tm if tm else 0
        print(f"  {year} | {yt:6d} | ${yp:>+9,.0f} | {pm:>4d}/{tm:<3d} | {pct:5.0f}% | ${avg_mo:>+7,.0f}")


def print_rolling_12(stats: dict):
    """Print rolling 12-month P&L windows."""
    months = stats["months"]
    monthly_pnl = stats["monthly_pnl"]

    if len(months) < 12:
        print("\n  (Need 12+ months for rolling analysis)")
        return

    all_pnls = [monthly_pnl[m] for m in months]

    print(f"\n{'Window':>19} | {'P&L':>10} | {'Win Mo':>6} | Bar")
    print("-" * 65)

    rolling_pnls = []
    for i in range(len(months) - 11):
        window = all_pnls[i : i + 12]
        total = sum(window)
        win_mo = sum(1 for p in window if p >= 0)
        rolling_pnls.append(total)
        end = months[i + 11]
        if total >= 0:
            bar = "+" * max(1, min(int(total / 200), 25))
        else:
            bar = "-" * max(1, min(int(abs(total) / 200), 25))
        print(f"  {months[i]}→{end} | ${total:>+9,.0f} | {win_mo:>3}/12 | {bar}")

    positive_windows = sum(1 for p in rolling_pnls if p >= 0)
    pct = 100 * positive_windows / len(rolling_pnls)
    print(f"\n  Positive 12-month windows: {positive_windows}/{len(rolling_pnls)} ({pct:.0f}%)")


def print_exit_analysis(trades: list[Trade]):
    """Analyze exit signal types."""
    exit_stats: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        exit_stats[t.exit_signal].append(t.pnl_usd)

    print(f"\n{'Exit Type':>20} | {'Count':>5} | {'Avg P&L':>8} | {'Total P&L':>10} | {'Win%':>5}")
    print("-" * 65)
    for sig in sorted(exit_stats.keys(), key=lambda s: -sum(exit_stats[s])):
        pnls = exit_stats[sig]
        avg = sum(pnls) / len(pnls)
        total = sum(pnls)
        wins = sum(1 for p in pnls if p >= 0)
        wr = 100 * wins / len(pnls)
        print(f"  {sig:>18} | {len(pnls):5d} | ${avg:>+7,.0f} | ${total:>+9,.0f} | {wr:4.0f}%")


def print_direction_analysis(trades: list[Trade]):
    """Analyze long vs short performance."""
    for direction in ["long", "short"]:
        dt = [t for t in trades if t.direction == direction]
        if not dt:
            continue
        net = sum(t.pnl_usd for t in dt)
        wins = sum(1 for t in dt if t.pnl_usd >= 0)
        wr = 100 * wins / len(dt)
        avg = net / len(dt)
        print(
            f"  {direction.upper():>5}: {len(dt):4d} trades"
            f" | ${net:>+9,.0f} net | {wr:.0f}% win rate"
            f" | ${avg:>+6,.0f} avg"
        )


def print_summary(stats: dict):
    """Print robustness summary box."""
    s = stats
    print(f"  Net P&L:                  ${s['net_pnl']:>+,.0f}")
    print(f"  Trades:                   {s['trades']} ({s['winners']}W / {s['losers']}L)")
    print(f"  Trade win rate:           {s['trade_win_rate']:.1f}%")
    print(f"  Avg win trade:            ${s['avg_win_trade']:,.0f}")
    print(f"  Avg loss trade:           ${-s['avg_loss_trade']:,.0f}")
    print(f"  Biggest win:              ${s['biggest_win']:,.0f}")
    print(f"  Biggest loss:             ${s['biggest_loss']:,.0f}")
    print()
    pm_pct = f"{100 * s['pct_profitable_months']:.0f}%"
    print(f"  Profitable months:        {s['profitable_months']}/{s['total_months']} ({pm_pct})")
    print(f"  Losing months:            {s['losing_months']}/{s['total_months']}")
    print(f"  Avg winning month:        ${s['avg_win_month']:,.0f}")
    print(f"  Avg losing month:         ${-s['avg_loss_month']:,.0f}")
    print(f"  Max consecutive winners:  {s['max_consec_win']} months")
    print(f"  Max consecutive losers:   {s['max_consec_loss']} months")
    print(f"  Max drawdown:             ${s['max_dd']:,.0f}")
    print(f"  Max DD duration:          {s['max_dd_months']} months")
    print(f"  Win/Loss month ratio:     {s['wl_ratio']:.2f}")
    print(f"  Streak ratio:             {s['streak_ratio']:.2f}")


def print_consistency_box(stats: dict):
    """Print consistency score with diagnosis."""
    c = stats["consistency"]
    print("\n  ╔══════════════════════════════════════╗")
    print(f"  ║  CONSISTENCY SCORE:  {c:>5.2f}           ║")
    print("  ║                                      ║")
    print("  ║  = %profitable × W/L ratio           ║")
    print("  ║    × streak ratio                     ║")
    formula = f"{stats['pct_profitable_months']:.2f} × {stats['wl_ratio']:.2f} × {stats['streak_ratio']:.2f}"
    print(f"  ║  = {formula:<30s}    ║")
    print("  ║                                      ║")
    print("  ║  Target: > 1.0 = tradeable           ║")
    print("  ║          > 2.0 = strong               ║")
    print("  ║          > 3.0 = exceptional           ║")
    print("  ╚══════════════════════════════════════╝")


def print_diagnosis(stats: dict):  # noqa: C901
    """Print diagnostic warnings and recommendations."""
    s = stats
    issues = []
    strengths = []

    if s["pct_profitable_months"] < 0.55:
        issues.append(f"Monthly win rate {100 * s['pct_profitable_months']:.0f}% < 55% — too many losing months")
    else:
        strengths.append(f"Monthly win rate {100 * s['pct_profitable_months']:.0f}% — solid consistency")

    if s["max_consec_loss"] >= 4:
        issues.append(f"{s['max_consec_loss']} consecutive losing months — psychological danger zone")
    elif s["max_consec_loss"] <= 2:
        strengths.append(f"Max {s['max_consec_loss']} consecutive losing months — manageable drawdowns")

    if s["max_dd"] > 5000:
        issues.append(f"Max drawdown ${s['max_dd']:,.0f} exceeds $5K — size risk")
    elif s["max_dd"] < 2000:
        strengths.append(f"Max drawdown ${s['max_dd']:,.0f} — contained risk")

    if s["wl_ratio"] < 1.2:
        issues.append(f"W/L ratio {s['wl_ratio']:.2f} — edge is thin")
    elif s["wl_ratio"] > 1.5:
        strengths.append(f"W/L ratio {s['wl_ratio']:.2f} — winners meaningfully larger than losers")

    if s["trade_win_rate"] < 45:
        issues.append(f"Trade win rate {s['trade_win_rate']:.0f}% — needs larger winners to compensate")
    elif s["trade_win_rate"] > 55:
        strengths.append(f"Trade win rate {s['trade_win_rate']:.0f}% — winning more often than losing")

    if s["consistency"] < 1.0:
        issues.append("Consistency score < 1.0 — NOT TRADEABLE in current form")
    elif s["consistency"] < 2.0:
        issues.append("Consistency score 1.0-2.0 — MARGINAL, needs improvement")
    elif s["consistency"] >= 2.0:
        strengths.append(f"Consistency score {s['consistency']:.2f} — TRADEABLE")

    if strengths:
        print("  Strengths:")
        for st in strengths:
            print(f"    ✓ {st}")
    if issues:
        print("  Issues:")
        for iss in issues:
            print(f"    ⚠ {iss}")


def run_report(csv_path: str):
    trades = parse_tv_csv(csv_path)
    if not trades:
        print("ERROR: No trades parsed from CSV")
        sys.exit(1)

    first = trades[0].entry_time
    last = trades[-1].exit_time
    span_years = (last - first).days / 365.25

    print("=" * 76)
    print("  NOVATRADE IRB v2.0.0 — TRADINGVIEW ROBUSTNESS REPORT")
    print("=" * 76)
    print("  Source: TradingView Strategy Tester CSV Export")
    print("  Pair: EURUSD H1")
    print(f"  Period: {first:%Y-%m-%d} to {last:%Y-%m-%d} ({span_years:.1f} years)")
    print(f"  Total trades: {len(trades)}")
    print(f"  Final cumulative P&L: ${trades[-1].cumulative_pnl:,.0f}")
    print("=" * 76)

    # Full period analysis
    full = analyze_period(trades, "FULL")

    # ── Direction Analysis ──
    print("\n── DIRECTION ANALYSIS ────────────────────────────────────────")
    print_direction_analysis(trades)

    # ── Exit Type Analysis ──
    print("\n── EXIT TYPE ANALYSIS ────────────────────────────────────────")
    print_exit_analysis(trades)

    # ── Monthly P&L Waterfall ──
    print("\n── MONTHLY P&L WATERFALL ─────────────────────────────────────")
    print_waterfall(full)

    # ── Yearly Breakdown ──
    print("\n── YEARLY BREAKDOWN ──────────────────────────────────────────")
    print_yearly(full)

    # ── Rolling 12-Month ──
    print("\n── ROLLING 12-MONTH P&L ──────────────────────────────────────")
    print_rolling_12(full)

    # ── Robustness Summary ──
    print("\n── ROBUSTNESS SUMMARY ────────────────────────────────────────")
    print_summary(full)
    print_consistency_box(full)

    # ── Diagnosis ──
    print("\n── DIAGNOSIS ─────────────────────────────────────────────────")
    print_diagnosis(full)

    # ── Period Breakdown ──
    print("\n" + "=" * 76)
    print("  PERIOD BREAKDOWN — Profitability Across Timeframes")
    print("=" * 76)

    now = last
    periods = [
        ("Last 1 Year", 365),
        ("Last 3 Years", 3 * 365),
        ("Last 5 Years", 5 * 365),
        ("Full Period", None),
    ]

    for label, days in periods:
        if days:
            cutoff = datetime(now.year, now.month, now.day)
            from datetime import timedelta

            cutoff = cutoff - timedelta(days=days)
            period_trades = [t for t in trades if t.exit_time >= cutoff]
        else:
            period_trades = trades

        if not period_trades:
            print(f"\n── {label} ── NO TRADES")
            continue

        ps = analyze_period(period_trades, label)
        pf = period_trades[0].entry_time
        pl = period_trades[-1].exit_time

        print(f"\n── {label} ({pf:%Y-%m-%d} → {pl:%Y-%m-%d}) ──")
        print(f"  Trades: {ps['trades']} | Net: ${ps['net_pnl']:>+,.0f} | Trade WR: {ps['trade_win_rate']:.0f}%")
        pm_pct = f"{100 * ps['pct_profitable_months']:.0f}%"
        print(f"  Months: {ps['profitable_months']}/{ps['total_months']} profitable ({pm_pct})")
        print(f"  Max DD: ${ps['max_dd']:,.0f} | Consec Loss: {ps['max_consec_loss']} mo | W/L: {ps['wl_ratio']:.2f}")
        print(f"  Consistency: {ps['consistency']:.2f}", end="")
        if ps["consistency"] >= 2.0:
            print(" ✓ STRONG")
        elif ps["consistency"] >= 1.0:
            print(" ~ MARGINAL")
        else:
            print(" ✗ WEAK")

    # ── Worst Months ──
    print("\n── TOP 5 WORST MONTHS ────────────────────────────────────────")
    month_list = [(m, full["monthly_pnl"][m]) for m in full["months"]]
    month_list.sort(key=lambda x: x[1])
    for m, pnl in month_list[:5]:
        nt = full["monthly_trades"][m]
        print(f"  {m}: ${pnl:>+9,.0f} ({nt} trades)")

    # ── Best Months ──
    print("\n── TOP 5 BEST MONTHS ─────────────────────────────────────────")
    for m, pnl in reversed(month_list[-5:]):
        nt = full["monthly_trades"][m]
        print(f"  {m}: ${pnl:>+9,.0f} ({nt} trades)")

    # ── Top Trades ──
    print("\n── TOP 5 BEST TRADES ─────────────────────────────────────────")
    by_pnl = sorted(trades, key=lambda t: -t.pnl_usd)
    for t in by_pnl[:5]:
        held = (t.exit_time - t.entry_time).total_seconds() / 3600
        print(
            f"  #{t.number:4d} {t.direction:>5}"
            f" {t.entry_time:%Y-%m-%d %H:%M}"
            f" → {t.exit_time:%Y-%m-%d %H:%M}"
            f" | ${t.pnl_usd:>+9,.0f} | {held:.0f}h"
            f" | {t.exit_signal}"
        )

    print("\n── TOP 5 WORST TRADES ────────────────────────────────────────")
    for t in by_pnl[-5:]:
        held = (t.exit_time - t.entry_time).total_seconds() / 3600
        print(
            f"  #{t.number:4d} {t.direction:>5}"
            f" {t.entry_time:%Y-%m-%d %H:%M}"
            f" → {t.exit_time:%Y-%m-%d %H:%M}"
            f" | ${t.pnl_usd:>+9,.0f} | {held:.0f}h"
            f" | {t.exit_signal}"
        )

    print()


if __name__ == "__main__":
    csv_file = sys.argv[1] if len(sys.argv) > 1 else "data/irb_v2_tv_1222trades_10yr.csv"
    run_report(csv_file)
