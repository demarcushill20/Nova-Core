"""Year/month breakdown of the validated Pine v5 IRB champion baseline.

Reads the authoritative Pine v5 backtest export (1,204 trades, 2016→2026,
$100K starting capital), which is the certified baseline for the live
'IRB v5 Relaxed Reliable Build (5min Champion)' strategy.

Usage:
    python3 scripts/analyze_pine_v5_10yr.py
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/home/nova/nova-core")
SRC = REPO / "data/irb_novatrade_irb_v5_results_extracted.csv"
OUT_DIR = REPO / "OUTPUT"


def fmt_money(x: float) -> str:
    sign = "-" if x < 0 else " "
    return f"{sign}${abs(x):>11,.2f}"


def main() -> None:  # noqa: C901
    init_eq = 100_000.0

    # Each trade has two rows in the CSV: an "Entry X" and "Exit X".
    # The Net P&L column is filled on BOTH rows with the same per-trade total,
    # so we key off the Exit row to bucket trades by close time.
    by_year: dict[int, list[float]] = defaultdict(list)
    by_ym: dict[tuple[int, int], list[float]] = defaultdict(list)
    all_pnls: list[tuple[datetime, float, str, str]] = []

    with SRC.open("r") as f:
        for row in csv.DictReader(f):
            ttype = row["Type"].strip()
            if not ttype.lower().startswith("exit"):
                continue
            dt = datetime.strptime(row["Date and time"], "%Y-%m-%d %H:%M")
            pnl = float(row["Net P&L USD"])
            side = "long" if "long" in ttype.lower() else "short"
            signal = row["Signal"].strip()
            by_year[dt.year].append(pnl)
            by_ym[(dt.year, dt.month)].append(pnl)
            all_pnls.append((dt, pnl, side, signal))

    all_pnls.sort(key=lambda r: r[0])

    # Equity curve & drawdown
    equity = init_eq
    peak = init_eq
    max_dd = 0.0
    max_dd_pct = 0.0
    max_dd_at: datetime | None = None
    for dt, pnl, _, _ in all_pnls:
        equity += pnl
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak * 100.0
            max_dd_at = dt

    def _stats(pnls: list[float]) -> dict:
        if not pnls:
            return {"trades": 0, "net": 0.0, "wins": 0, "losses": 0, "wr": 0.0, "pf": 0.0, "avg": 0.0}
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_w = sum(wins)
        gross_l = -sum(losses)
        pf = gross_w / gross_l if gross_l > 0 else (float("inf") if gross_w > 0 else 0.0)
        return {
            "trades": len(pnls),
            "net": sum(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "wr": len(wins) / len(pnls) * 100.0,
            "pf": pf,
            "avg": sum(pnls) / len(pnls),
        }

    pnls = [p for _, p, _, _ in all_pnls]
    overall = _stats(pnls)
    final_eq = init_eq + overall["net"]
    n_years = (all_pnls[-1][0] - all_pnls[0][0]).days / 365.25
    cagr = (final_eq / init_eq) ** (1 / n_years) - 1

    years = sorted(by_year.keys())
    months = sorted(by_ym.keys())

    lines: list[str] = []
    p = lines.append

    p("# IRB v5 M5 Champion — 10-Year Backtest Report (Pine v5 Baseline)")
    p("")
    p("**Source:** `data/irb_novatrade_irb_v5_results_extracted.csv`  ")
    p("**Strategy:** Rob Hoffman IRB v5 — Relaxed Reliable Build (5-min Champion)  ")
    p("**Symbol / TF:** EURUSD  M5 (primary) + H1 (higher)  ")
    p(f"**Window:** {all_pnls[0][0].date()} → {all_pnls[-1][0].date()}  ")
    p(f"**Initial equity:** ${init_eq:,.2f}  ")
    p("**Position sizing:** fixed 100,000 qty (≈1 lot)  per Pine baseline  ")
    p("")
    p("## Headline")
    p("")
    p(f"- Net P&L:                **{fmt_money(overall['net'])}**  ({overall['net'] / init_eq * 100:+.2f}%)")
    p(f"- Final equity:           ${final_eq:,.2f}")
    p(f"- CAGR (~{n_years:.2f}y):           {cagr * 100:.2f}%")
    p(f"- Total trades:           {overall['trades']:,}")
    p(f"- Win rate:               {overall['wr']:.2f}%   ({overall['wins']}W / {overall['losses']}L)")
    p(f"- Profit factor:          {overall['pf']:.3f}")
    p(f"- Avg P&L / trade:        {fmt_money(overall['avg'])}")
    p(f"- Largest win:            {fmt_money(max(pnls))}")
    p(f"- Largest loss:           {fmt_money(min(pnls))}")
    p(
        f"- Max drawdown:           {fmt_money(-max_dd)}  ({max_dd_pct:.2f}% of peak)  "
        f"near {max_dd_at.date() if max_dd_at else 'n/a'}"
    )
    p("")

    # Year-by-year
    p("## Year-by-Year")
    p("")
    p("| Year | Trades | Wins | Losses | Win % | Net P&L | Return % | PF | Avg/Trade |")
    p("|-----:|-------:|-----:|-------:|------:|--------:|---------:|---:|----------:|")
    running_eq = init_eq
    for y in years:
        s = _stats(by_year[y])
        ret = s["net"] / running_eq * 100.0
        running_eq += s["net"]
        pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        p(
            f"| {y} | {s['trades']} | {s['wins']} | {s['losses']} | {s['wr']:.1f}% | "
            f"{fmt_money(s['net'])} | {ret:+.2f}% | {pf_s} | {fmt_money(s['avg'])} |"
        )
    p("")

    # Month-by-month — wide table grouped by year
    p("## Month-by-Month (Net P&L USD)")
    p("")
    months_hdr = ["Year", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Total"]
    p("| " + " | ".join(months_hdr) + " |")
    p("|" + "|".join(["----:"] * len(months_hdr)) + "|")
    for y in years:
        cells = [str(y)]
        ytot = 0.0
        for m in range(1, 13):
            v = sum(by_ym.get((y, m), []))
            ytot += v
            cells.append(f"{v:,.0f}" if v != 0 or (y, m) in by_ym else "—")
        cells.append(f"**{ytot:,.0f}**")
        p("| " + " | ".join(cells) + " |")
    p("")

    # Detailed monthly table
    p("## Month-by-Month (Detailed)")
    p("")
    p("| Month | Trades | Wins | Losses | Win % | Net P&L | PF |")
    p("|:------|-------:|-----:|-------:|------:|--------:|---:|")
    for ym in months:
        y, m = ym
        s = _stats(by_ym[ym])
        pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        p(
            f"| {y}-{m:02d} | {s['trades']} | {s['wins']} | {s['losses']} | {s['wr']:.1f}% | "
            f"{fmt_money(s['net'])} | {pf_s} |"
        )
    p("")

    # Consistency
    profitable_years = sum(1 for y in years if sum(by_year[y]) > 0)
    profitable_months = sum(1 for ym in months if sum(by_ym[ym]) > 0)
    best_m = max(months, key=lambda k: sum(by_ym[k]))
    worst_m = min(months, key=lambda k: sum(by_ym[k]))
    best_y = max(years, key=lambda y: sum(by_year[y]))
    worst_y = min(years, key=lambda y: sum(by_year[y]))

    p("## Consistency")
    p("")
    p(f"- Profitable years:   {profitable_years}/{len(years)}  ({profitable_years / len(years) * 100:.1f}%)")
    p(f"- Profitable months:  {profitable_months}/{len(months)}  ({profitable_months / len(months) * 100:.1f}%)")
    p(f"- Best month:         {best_m[0]}-{best_m[1]:02d} → {fmt_money(sum(by_ym[best_m]))}")
    p(f"- Worst month:        {worst_m[0]}-{worst_m[1]:02d} → {fmt_money(sum(by_ym[worst_m]))}")
    p(f"- Best year:          {best_y} → {fmt_money(sum(by_year[best_y]))}")
    p(f"- Worst year:         {worst_y} → {fmt_money(sum(by_year[worst_y]))}")
    p("")

    # Streaks
    longest_w = cur_w = 0
    longest_l = cur_l = 0
    for _, pnl, _, _ in all_pnls:
        if pnl > 0:
            cur_w += 1
            cur_l = 0
            longest_w = max(longest_w, cur_w)
        elif pnl < 0:
            cur_l += 1
            cur_w = 0
            longest_l = max(longest_l, cur_l)
    p(f"- Longest win streak:  {longest_w} trades")
    p(f"- Longest loss streak: {longest_l} trades")
    p("")

    # Side breakdown
    long_pnls = [p for _, p, side, _ in all_pnls if side == "long"]
    short_pnls = [p for _, p, side, _ in all_pnls if side == "short"]
    sl = _stats(long_pnls)
    ss = _stats(short_pnls)
    p("## Side Breakdown")
    p("")
    p("| Side | Trades | Win % | Net P&L | PF | Avg/Trade |")
    p("|:-----|------:|------:|--------:|---:|----------:|")
    for label, s in (("Long", sl), ("Short", ss)):
        pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        p(f"| {label}  | {s['trades']} | {s['wr']:.1f}% | {fmt_money(s['net'])} | {pf_s} | {fmt_money(s['avg'])} |")
    p("")

    # Exit reason mix
    from collections import Counter

    er = Counter(sig for _, _, _, sig in all_pnls)
    p("## Exit-Signal Mix")
    p("")
    p("| Signal | Count | % | Net P&L |")
    p("|:-------|------:|--:|--------:|")
    for sig, n in er.most_common():
        net = sum(p for _, p, _, s in all_pnls if s == sig)
        p(f"| {sig} | {n} | {n / len(all_pnls) * 100:.1f}% | {fmt_money(net)} |")
    p("")

    p("## Notes")
    p("")
    p("- Position size in this Pine baseline is fixed (qty=100,000), not the 0.33%-risk")
    p("  fraction the live `HardRiskSupervisor` enforces in production. With dynamic")
    p("  0.33%-risk sizing, per-trade USD P&L scales by stop distance, so absolute")
    p("  $ figures will differ — direction, win rate, and PF should hold.")
    p("- The validated Pine v5 strategy shows **PF 1.15, 63.6% profitable months over 24-yr**;")
    p("  the 10-yr window is a subset.")
    p("- The internal Python `IRBBacktester` re-run on the same data diverges from")
    p("  this Pine baseline (known parity gap — see `/novatrade-parity-check`). Do")
    p("  not treat the Python re-simulation as authoritative until parity is closed.")

    report = "\n".join(lines)
    print(report)

    OUT_DIR.mkdir(exist_ok=True)
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"backtest_10yr_pine_v5_baseline_{ts_tag}.md"
    out.write_text(report)
    print(f"\n[saved] {out}")


if __name__ == "__main__":
    main()
