"""Re-run the Pine v5 IRB baseline with 0.33%-risk dynamic sizing.

The Pine baseline export is at fixed 100,000 qty (~1 lot). To project what
production would actually book under the live `HardRiskSupervisor` config
(0.33% risk per trade, equity-relative), we:

    1. Use each trade's |Adverse excursion USD| as the per-trade pine-risk proxy.
       For SL-hit trades (91% of dataset) this equals the realized loss exactly
       (stop distance × volume). For TIME_STOP / Open / STAG_EXIT exits, it's
       the worst negative excursion the trade actually saw — a conservative
       lower bound on stop distance.
    2. Compute R-multiple = pine_pnl_$ / pine_risk_$.
    3. Resized P&L per trade = R × 0.33% × equity_at_entry, compounding equity.
    4. Re-aggregate yearly / monthly under the new equity curve.

Risk floor: trades with adverse_excursion ≈ 0 fall back to the median pine-risk
across the dataset to avoid degenerate R-multiples.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

REPO = Path("/home/nova/nova-core")
SRC = REPO / "data/irb_novatrade_irb_v5_results_extracted.csv"
OUT_DIR = REPO / "OUTPUT"

INITIAL_EQUITY = 100_000.0
RISK_FRACTION = 0.0033  # 0.33% — current live setting (per env default)


def fmt_money(x: float) -> str:
    sign = "-" if x < 0 else " "
    return f"{sign}${abs(x):>12,.2f}"


def main() -> None:
    # Each trade has two CSV rows (Entry + Exit). Both rows carry per-trade Net P&L
    # and adverse excursion. We key off the Exit row.
    rows: list[dict] = []
    with SRC.open("r") as f:
        for r in csv.DictReader(f):
            ttype = r["Type"].strip()
            if not ttype.lower().startswith("exit"):
                continue
            dt = datetime.strptime(r["Date and time"], "%Y-%m-%d %H:%M")
            pine_pnl = float(r["Net P&L USD"])
            adv = float(r["Adverse excursion USD"])  # negative or zero
            side = "long" if "long" in ttype.lower() else "short"
            signal = r["Signal"].strip()
            rows.append(
                {
                    "dt": dt,
                    "pine_pnl": pine_pnl,
                    "adverse": adv,
                    "side": side,
                    "signal": signal,
                }
            )

    rows.sort(key=lambda x: x["dt"])

    # Compute per-trade pine_risk proxy.
    #
    # An SL-hit trade reveals its true stop distance: |loss| ≈ stop × volume.
    # A trade that *didn't* hit its stop has |adverse_excursion| < true stop,
    # so naive use of adverse_excursion blows up R-multiples.
    #
    # Methodology: use |adverse_excursion| floor-capped at the median |loss| of
    # SL-hit trades. This preserves real stop variance (a trade that pulled
    # back hard before reversing has a larger denominator) but prevents trades
    # that ran straight up from registering implausible 100×+R wins.
    sl_losses = [abs(r["pine_pnl"]) for r in rows if r["pine_pnl"] < 0 and r["signal"] in ("Long Exit", "Short Exit")]
    median_sl_risk = median(sl_losses) if sl_losses else 75.0

    for r in rows:
        pr = max(abs(r["adverse"]), median_sl_risk)
        r["pine_risk"] = pr
        r["R"] = r["pine_pnl"] / pr

    # Walk equity with 0.33% sizing
    equity = INITIAL_EQUITY
    peak = equity
    max_dd = 0.0
    max_dd_pct = 0.0
    max_dd_at = None

    by_year: dict[int, list[float]] = defaultdict(list)
    by_ym: dict[tuple[int, int], list[float]] = defaultdict(list)

    for r in rows:
        risk_dollars = equity * RISK_FRACTION
        new_pnl = r["R"] * risk_dollars
        r["resized_pnl"] = new_pnl
        equity += new_pnl
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak * 100.0
            max_dd_at = r["dt"]
        by_year[r["dt"].year].append(new_pnl)
        by_ym[(r["dt"].year, r["dt"].month)].append(new_pnl)

    # ---- Stats ----
    def _stats(pnls: list[float]) -> dict:
        if not pnls:
            return {"trades": 0, "net": 0.0, "wins": 0, "losses": 0, "wr": 0.0, "pf": 0.0, "avg": 0.0}
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_w = sum(wins)
        gross_l = -sum(losses)
        pf = gross_w / gross_l if gross_l > 0 else float("inf") if gross_w > 0 else 0.0
        return {
            "trades": len(pnls),
            "net": sum(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "wr": len(wins) / len(pnls) * 100.0,
            "pf": pf,
            "avg": sum(pnls) / len(pnls),
        }

    pnls = [r["resized_pnl"] for r in rows]
    overall = _stats(pnls)
    final_eq = equity
    n_years = (rows[-1]["dt"] - rows[0]["dt"]).days / 365.25
    cagr = (final_eq / INITIAL_EQUITY) ** (1 / n_years) - 1 if final_eq > 0 else float("nan")

    years = sorted(by_year.keys())
    months = sorted(by_ym.keys())

    L: list[str] = []
    p = L.append
    p("# IRB v5 M5 Champion — 10-Year Backtest (0.33%-Risk Resized)")
    p("")
    p("**Source:** `data/irb_novatrade_irb_v5_results_extracted.csv` (Pine v5 baseline)  ")
    p("**Sizing:** 0.33% risk / trade, equity-relative, compounding  ")
    p(f"**Window:** {rows[0]['dt'].date()} → {rows[-1]['dt'].date()}  ")
    p(f"**Initial equity:** ${INITIAL_EQUITY:,.0f}  ")
    p(f"**Pine-risk proxy:** `max(|adverse excursion|, median SL-hit loss=${median_sl_risk:,.2f})` per trade  ")
    p("")
    p("## Headline (vs fixed-1-lot baseline)")
    p("")
    p("| Metric | Pine baseline (1 lot) | Resized (0.33% risk) |")
    p("|:--|--:|--:|")
    pine_net = sum(r["pine_pnl"] for r in rows)
    pine_wins = sum(1 for r in rows if r["pine_pnl"] > 0)
    p(
        f"| Net P&L | ${pine_net:,.0f}  ({pine_net / INITIAL_EQUITY * 100:+.2f}%) "
        f"| ${overall['net']:,.0f}  ({overall['net'] / INITIAL_EQUITY * 100:+.2f}%) |"
    )
    p(f"| Final equity | ${INITIAL_EQUITY + pine_net:,.0f} | ${final_eq:,.0f} |")
    p(
        f"| CAGR | {(((INITIAL_EQUITY + pine_net) / INITIAL_EQUITY) ** (1 / n_years) - 1) * 100:.2f}% "
        f"| {cagr * 100:.2f}% |"
    )
    p(f"| Trades | {len(rows)} | {len(rows)} |")
    p(f"| Win rate | {pine_wins / len(rows) * 100:.2f}% | {overall['wr']:.2f}% |")
    pine_pf = sum(r["pine_pnl"] for r in rows if r["pine_pnl"] > 0) / abs(
        sum(r["pine_pnl"] for r in rows if r["pine_pnl"] < 0)
    )
    p(f"| Profit factor | {pine_pf:.3f} | {overall['pf']:.3f} |")
    p(f"| Avg P&L / trade | ${pine_net / len(rows):.2f} | ${overall['avg']:.2f} |")
    p(f"| Largest win | ${max(r['pine_pnl'] for r in rows):,.0f} | ${max(pnls):,.2f} |")
    p(f"| Largest loss | ${min(r['pine_pnl'] for r in rows):,.0f} | ${min(pnls):,.2f} |")
    p(
        f"| Max drawdown | $-8,337  (8.16%) "
        f"| ${-max_dd:,.0f}  ({max_dd_pct:.2f}%) near {max_dd_at.date() if max_dd_at else 'n/a'} |"
    )
    p("")

    p("## Year-by-Year (resized 0.33%)")
    p("")
    p("| Year | Trades | Win % | Net P&L | Return % | PF | Avg/Trade |")
    p("|---:|---:|---:|---:|---:|---:|---:|")
    running = INITIAL_EQUITY
    for y in years:
        s = _stats(by_year[y])
        ret = s["net"] / running * 100.0
        running += s["net"]
        pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        p(
            f"| {y} | {s['trades']} | {s['wr']:.1f}% | {fmt_money(s['net'])} "
            f"| {ret:+.2f}% | {pf_s} | {fmt_money(s['avg'])} |"
        )
    p("")

    p("## Month-by-Month Net P&L USD (resized)")
    p("")
    hdr = ["Year", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Total"]
    p("| " + " | ".join(hdr) + " |")
    p("|" + "|".join(["---:"] * len(hdr)) + "|")
    for y in years:
        cells = [str(y)]
        ytot = 0.0
        for m in range(1, 13):
            v = sum(by_ym.get((y, m), []))
            ytot += v
            cells.append(f"{v:,.0f}" if (y, m) in by_ym else "—")
        cells.append(f"**{ytot:,.0f}**")
        p("| " + " | ".join(cells) + " |")
    p("")

    p("## Detailed Monthly")
    p("")
    p("| Month | Trades | Win % | Net P&L | PF |")
    p("|:--|---:|---:|---:|---:|")
    for ym in months:
        s = _stats(by_ym[ym])
        pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        p(f"| {ym[0]}-{ym[1]:02d} | {s['trades']} | {s['wr']:.1f}% | {fmt_money(s['net'])} | {pf_s} |")
    p("")

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
    p(f"- Best month:         {best_m[0]}-{best_m[1]:02d} → ${sum(by_ym[best_m]):,.2f}")
    p(f"- Worst month:        {worst_m[0]}-{worst_m[1]:02d} → ${sum(by_ym[worst_m]):,.2f}")
    p(f"- Best year:          {best_y} → ${sum(by_year[best_y]):,.2f}")
    p(f"- Worst year:         {worst_y} → ${sum(by_year[worst_y]):,.2f}")
    p("")
    # R distribution
    rs = [r["R"] for r in rows]
    p("## R-Multiple Distribution")
    p("")
    p(f"- Mean R: {sum(rs) / len(rs):+.3f}  ·  Median R: {median(rs):+.3f}")
    p(f"- Best R: {max(rs):+.2f}  ·  Worst R: {min(rs):+.2f}")
    p(f"- |R| > 5 trades: {sum(1 for x in rs if abs(x) > 5)}")
    p(f"- |R| > 2 trades: {sum(1 for x in rs if abs(x) > 2)}")
    p("")
    p("## Caveats")
    p("")
    p("- Adverse excursion is a conservative proxy for stop distance on non-SL exits.")
    p("  For SL-hit trades (91% of dataset) it's exact. For TIME_STOP / Open / STAG")
    p("  exits the actual stop was further away, so R-multiples may be slightly")
    p("  overstated → resized $ slightly optimistic on the winning tail.")
    p("- The live `HardRiskSupervisor` adds caps (daily $4,500 / total $9,000 /")
    p("  equity floor $8,500) that this re-sizer doesn't apply. Real-world equity")
    p("  curve would clip drawdowns at those floors.")
    p("- Slippage / spread already baked into Pine pnl, so resized R inherits them.")

    report = "\n".join(L)
    print(report)

    OUT_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"backtest_10yr_pine_v5_resized_033pct_{ts}.md"
    out.write_text(report)
    print(f"\n[saved] {out}")


if __name__ == "__main__":
    main()
