"""Champion-Challenger backtest comparison engine.

Compares two TradingView strategy CSV exports across multiple time horizons
and produces structured results with verdicts.

Usage as library:
    from novatrade.backtest.champion_challenger import compare, format_report
    result = compare("data/champion.csv", "data/challenger.csv", "v2.0.0", "v5.0.0")
    print(format_report(result))
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.tv_robustness_report import Trade, analyze_period, parse_tv_csv


# ── Data Structures ─────────────────────────────────────────────────────────


@dataclass
class ExitStats:
    signal: str
    count: int
    total_pnl: float
    avg_pnl: float
    win_rate: float


@dataclass
class DirectionStats:
    direction: str
    count: int
    net_pnl: float
    win_rate: float
    avg_pnl: float


@dataclass
class Criterion:
    name: str
    challenger_wins: bool
    detail: str


@dataclass
class Verdict:
    winner: str  # "champion", "challenger", or "draw"
    champion_label: str
    challenger_label: str
    criteria: list[Criterion]
    score: str
    recommendation: str


@dataclass
class ComparisonResult:
    champion_label: str
    challenger_label: str
    champion_trades: list[Trade]
    challenger_trades: list[Trade]
    champion_stats: dict
    challenger_stats: dict
    period_comparisons: dict = field(default_factory=dict)
    yearly_comparison: dict = field(default_factory=dict)
    monthly_comparison: dict = field(default_factory=dict)
    champion_exits: dict = field(default_factory=dict)
    challenger_exits: dict = field(default_factory=dict)
    champion_directions: dict = field(default_factory=dict)
    challenger_directions: dict = field(default_factory=dict)
    stagnation_impact: dict = field(default_factory=dict)
    verdict: Verdict | None = None


# ── Analysis Helpers ────────────────────────────────────────────────────────


def _exit_breakdown(trades: list[Trade]) -> dict[str, ExitStats]:
    by_exit: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_exit[t.exit_signal].append(t.pnl_usd)
    result = {}
    for sig, pnls in sorted(by_exit.items(), key=lambda x: -sum(x[1])):
        wins = sum(1 for p in pnls if p >= 0)
        result[sig] = ExitStats(
            signal=sig,
            count=len(pnls),
            total_pnl=sum(pnls),
            avg_pnl=sum(pnls) / len(pnls),
            win_rate=100 * wins / len(pnls),
        )
    return result


def _direction_breakdown(trades: list[Trade]) -> dict[str, DirectionStats]:
    result = {}
    for d in ["long", "short"]:
        dt = [t for t in trades if t.direction == d]
        if not dt:
            continue
        net = sum(t.pnl_usd for t in dt)
        wins = sum(1 for t in dt if t.pnl_usd >= 0)
        result[d] = DirectionStats(
            direction=d,
            count=len(dt),
            net_pnl=net,
            win_rate=100 * wins / len(dt),
            avg_pnl=net / len(dt),
        )
    return result


def _period_trades(trades: list[Trade], days: int | None, ref_date: datetime) -> list[Trade]:
    if days is None:
        return trades
    cutoff = ref_date - timedelta(days=days)
    return [t for t in trades if t.exit_time >= cutoff]


def _monthly_pnl(trades: list[Trade]) -> dict[str, float]:
    result: dict[str, float] = defaultdict(float)
    for t in trades:
        result[t.exit_time.strftime("%Y-%m")] += t.pnl_usd
    return dict(result)


def _yearly_pnl(trades: list[Trade]) -> tuple[dict[str, float], dict[str, int]]:
    pnl: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for t in trades:
        y = t.exit_time.strftime("%Y")
        pnl[y] += t.pnl_usd
        counts[y] += 1
    return dict(pnl), dict(counts)


def _build_verdict(
    champ_stats: dict,
    chall_stats: dict,
    champ_label: str,
    chall_label: str,
    yearly_champ_wins: int,
    yearly_chall_wins: int,
    monthly_champ_wins: int,
    monthly_chall_wins: int,
) -> Verdict:
    criteria = []

    checks = [
        ("Net P&L", chall_stats["net_pnl"] > champ_stats["net_pnl"],
         f"{chall_label} ${chall_stats['net_pnl']:+,.0f} vs {champ_label} ${champ_stats['net_pnl']:+,.0f}"),
        ("Consistency", chall_stats["consistency"] > champ_stats["consistency"],
         f"{chall_label} {chall_stats['consistency']:.2f} vs {champ_label} {champ_stats['consistency']:.2f}"),
        ("Year wins", yearly_chall_wins > yearly_champ_wins,
         f"{chall_label} {yearly_chall_wins} vs {champ_label} {yearly_champ_wins}"),
        ("Month wins", monthly_chall_wins > monthly_champ_wins,
         f"{chall_label} {monthly_chall_wins} vs {champ_label} {monthly_champ_wins}"),
        ("Lower max DD", chall_stats["max_dd"] < champ_stats["max_dd"],
         f"{chall_label} ${chall_stats['max_dd']:,.0f} vs {champ_label} ${champ_stats['max_dd']:,.0f}"),
        ("Fewer consec loss", chall_stats["max_consec_loss"] < champ_stats["max_consec_loss"],
         f"{chall_label} {chall_stats['max_consec_loss']} vs {champ_label} {champ_stats['max_consec_loss']}"),
        ("Higher trade WR", chall_stats["trade_win_rate"] > champ_stats["trade_win_rate"],
         f"{chall_label} {chall_stats['trade_win_rate']:.1f}% vs {champ_label} {champ_stats['trade_win_rate']:.1f}%"),
    ]

    chall_score = 0
    for name, chall_wins, detail in checks:
        criteria.append(Criterion(name=name, challenger_wins=chall_wins, detail=detail))
        if chall_wins:
            chall_score += 1

    total = len(checks)
    if chall_score > total // 2:
        winner = "challenger"
        rec = f"{chall_label} wins {chall_score}/{total} criteria — promote to champion"
    elif chall_score == total // 2:
        winner = "draw"
        rec = f"No clear winner ({chall_score}/{total}) — keep {champ_label} as champion"
    else:
        winner = "champion"
        rec = f"{champ_label} wins {total - chall_score}/{total} criteria — remains champion"

    return Verdict(
        winner=winner,
        champion_label=champ_label,
        challenger_label=chall_label,
        criteria=criteria,
        score=f"{chall_score}/{total} challenger",
        recommendation=rec,
    )


# ── Main Comparison Engine ──────────────────────────────────────────────────


def compare(
    champion_csv: str | Path,
    challenger_csv: str | Path,
    champion_label: str = "champion",
    challenger_label: str = "challenger",
) -> ComparisonResult:
    """Run full champion vs challenger comparison."""
    champ_trades = parse_tv_csv(champion_csv)
    chall_trades = parse_tv_csv(challenger_csv)

    champ_stats = analyze_period(champ_trades, champion_label)
    chall_stats = analyze_period(chall_trades, challenger_label)

    result = ComparisonResult(
        champion_label=champion_label,
        challenger_label=challenger_label,
        champion_trades=champ_trades,
        challenger_trades=chall_trades,
        champion_stats=champ_stats,
        challenger_stats=chall_stats,
    )

    # Exit type breakdown
    result.champion_exits = _exit_breakdown(champ_trades)
    result.challenger_exits = _exit_breakdown(chall_trades)

    # Direction breakdown
    result.champion_directions = _direction_breakdown(champ_trades)
    result.challenger_directions = _direction_breakdown(chall_trades)

    # Period comparisons (1yr, 3yr, 5yr, full)
    ref_champ = champ_trades[-1].exit_time
    ref_chall = chall_trades[-1].exit_time
    for label, days in [("1yr", 365), ("3yr", 3 * 365), ("5yr", 5 * 365), ("full", None)]:
        cp = _period_trades(champ_trades, days, ref_champ)
        cl = _period_trades(chall_trades, days, ref_chall)
        result.period_comparisons[label] = {
            "champion": analyze_period(cp, f"{champion_label} {label}"),
            "challenger": analyze_period(cl, f"{challenger_label} {label}"),
        }

    # Year-by-year
    champ_yearly, champ_yearly_n = _yearly_pnl(champ_trades)
    chall_yearly, chall_yearly_n = _yearly_pnl(chall_trades)
    all_years = sorted(set(list(champ_yearly.keys()) + list(chall_yearly.keys())))
    yearly_champ_wins = yearly_chall_wins = 0
    for y in all_years:
        cp = champ_yearly.get(y, 0)
        cl = chall_yearly.get(y, 0)
        delta = cl - cp
        winner = challenger_label if delta > 0 else champion_label if delta < 0 else "TIE"
        if delta > 0:
            yearly_chall_wins += 1
        elif delta < 0:
            yearly_champ_wins += 1
        result.yearly_comparison[y] = {
            "champion_pnl": cp,
            "challenger_pnl": cl,
            "champion_trades": champ_yearly_n.get(y, 0),
            "challenger_trades": chall_yearly_n.get(y, 0),
            "delta": delta,
            "winner": winner,
        }

    # Month-by-month
    champ_monthly = _monthly_pnl(champ_trades)
    chall_monthly = _monthly_pnl(chall_trades)
    all_months = sorted(set(list(champ_monthly.keys()) + list(chall_monthly.keys())))
    monthly_champ_wins = monthly_chall_wins = ties = 0
    for m in all_months:
        cp = champ_monthly.get(m, 0)
        cl = chall_monthly.get(m, 0)
        delta = cl - cp
        if delta > 0:
            winner = challenger_label
            monthly_chall_wins += 1
        elif delta < 0:
            winner = champion_label
            monthly_champ_wins += 1
        else:
            winner = "TIE"
            ties += 1
        result.monthly_comparison[m] = {
            "champion_pnl": cp,
            "challenger_pnl": cl,
            "delta": delta,
            "winner": winner,
        }

    # Stagnation guard impact (if STAG_EXIT exists in challenger)
    chall_stag = [t for t in chall_trades if t.exit_signal == "STAG_EXIT"]
    champ_ts = [t for t in champ_trades if t.exit_signal == "TIME_STOP"]
    chall_ts = [t for t in chall_trades if t.exit_signal == "TIME_STOP"]
    if chall_stag:
        stag_pnls = [t.pnl_usd for t in chall_stag]
        result.stagnation_impact = {
            "stag_count": len(chall_stag),
            "stag_total_pnl": sum(stag_pnls),
            "stag_avg_pnl": sum(stag_pnls) / len(stag_pnls),
            "stag_win_rate": 100 * sum(1 for p in stag_pnls if p >= 0) / len(stag_pnls),
            "champion_timestop_count": len(champ_ts),
            "challenger_timestop_count": len(chall_ts),
            "timestop_delta": len(chall_ts) - len(champ_ts),
            "champion_timestop_pnl": sum(t.pnl_usd for t in champ_ts),
            "challenger_timestop_pnl": sum(t.pnl_usd for t in chall_ts),
        }

    # Verdict
    result.verdict = _build_verdict(
        champ_stats, chall_stats,
        champion_label, challenger_label,
        yearly_champ_wins, yearly_chall_wins,
        monthly_champ_wins, monthly_chall_wins,
    )

    return result


# ── Formatters ──────────────────────────────────────────────────────────────


def format_report(r: ComparisonResult) -> str:
    """Full text report for CLI output."""
    lines: list[str] = []
    w = lines.append

    cl = r.champion_label
    ll = r.challenger_label

    w("=" * 90)
    w(f"  NOVATRADE IRB — HEAD-TO-HEAD: {cl} (CHAMPION) vs {ll} (CHALLENGER)")
    w("=" * 90)
    ct = r.champion_trades
    lt = r.challenger_trades
    w(f"  {cl}: {len(ct)} trades | {ct[0].entry_time:%Y-%m-%d} → {ct[-1].exit_time:%Y-%m-%d}")
    w(f"  {ll}: {len(lt)} trades | {lt[0].entry_time:%Y-%m-%d} → {lt[-1].exit_time:%Y-%m-%d}")

    # Exit type comparison
    w(f"\n{'─' * 90}")
    w("  EXIT TYPE COMPARISON")
    w(f"{'─' * 90}")
    all_exits = sorted(set(list(r.champion_exits.keys()) + list(r.challenger_exits.keys())))
    w(f"  {'Exit Type':>18} | {cl+' #':>8} {cl+' P&L':>10} {cl+' Avg':>8} | {ll+' #':>8} {ll+' P&L':>10} {ll+' Avg':>8} | {'Δ P&L':>10}")
    w("  " + "-" * 86)
    for sig in all_exits:
        ce = r.champion_exits.get(sig)
        le = r.challenger_exits.get(sig)
        cc = ce.count if ce else 0
        cp = ce.total_pnl if ce else 0
        ca = ce.avg_pnl if ce else 0
        lc = le.count if le else 0
        lp = le.total_pnl if le else 0
        la = le.avg_pnl if le else 0
        w(f"  {sig:>18} | {cc:>8} ${cp:>+9,.0f} ${ca:>+7,.0f} | {lc:>8} ${lp:>+9,.0f} ${la:>+7,.0f} | ${lp - cp:>+9,.0f}")

    # Direction comparison
    w(f"\n{'─' * 90}")
    w("  DIRECTION COMPARISON")
    w(f"{'─' * 90}")
    for d in ["long", "short"]:
        cd = r.champion_directions.get(d)
        ld = r.challenger_directions.get(d)
        if cd and ld:
            w(f"  {d.upper():>5} | {cl}: {cd.count:4d} trades ${cd.net_pnl:>+9,.0f} {cd.win_rate:.0f}% WR | {ll}: {ld.count:4d} trades ${ld.net_pnl:>+9,.0f} {ld.win_rate:.0f}% WR | Δ ${ld.net_pnl - cd.net_pnl:>+9,.0f}")

    # Full period metrics table
    cs = r.champion_stats
    ls = r.challenger_stats
    w(f"\n{'─' * 90}")
    w("  FULL PERIOD — 10 YEAR")
    w(f"{'─' * 90}")

    metrics = [
        ("Net P&L", "net_pnl", True, "$", "+,.0f"),
        ("Trades", "trades", None, "", "d"),
        ("Trade Win Rate", "trade_win_rate", True, "", ".1f"),
        ("Profitable Months", "profitable_months", True, "", "d"),
        ("% Prof Months", "pct_profitable_months", True, "", ".0%"),
        ("Max Consec Win", "max_consec_win", True, "", "d"),
        ("Max Consec Loss", "max_consec_loss", False, "", "d"),
        ("Max Drawdown", "max_dd", False, "$", ",.0f"),
        ("Max DD Duration", "max_dd_months", False, "", "d"),
        ("W/L Ratio", "wl_ratio", True, "", ".2f"),
        ("Streak Ratio", "streak_ratio", True, "", ".2f"),
        ("CONSISTENCY", "consistency", True, "", ".2f"),
    ]

    w(f"  {'Metric':>22} | {cl:>12} | {ll:>12} | {'Better':>8}")
    w("  " + "-" * 65)
    for label, key, higher_better, prefix, fmt in metrics:
        cv = cs[key]
        lv = ls[key]
        cv_s = f"{prefix}{cv:{fmt}}"
        lv_s = f"{prefix}{lv:{fmt}}"
        if higher_better is None:
            winner = ""
        elif higher_better:
            winner = ll if lv > cv else cl if cv > lv else "TIE"
        else:
            winner = ll if lv < cv else cl if cv < lv else "TIE"
        bold = " ◀◀" if label == "CONSISTENCY" else ""
        w(f"  {label:>22} | {cv_s:>12} | {lv_s:>12} | {winner:>8}{bold}")

    # Period breakdown
    for period_key, period_label in [("5yr", "5 YEAR"), ("3yr", "3 YEAR"), ("1yr", "1 YEAR")]:
        pc = r.period_comparisons[period_key]
        pcs = pc["champion"]
        pls = pc["challenger"]
        w(f"\n{'─' * 90}")
        w(f"  {period_label} PERIOD")
        w(f"{'─' * 90}")
        w(f"  {'':>22} | {cl:>12} | {ll:>12} | {'Winner':>8}")
        w("  " + "-" * 60)
        for label, key, higher_better in [
            ("Net P&L", "net_pnl", True), ("Trades", "trades", None),
            ("Trade Win Rate %", "trade_win_rate", True), ("Prof Months", "profitable_months", True),
            ("Max DD", "max_dd", False), ("Consec Loss Mo", "max_consec_loss", False),
            ("W/L Ratio", "wl_ratio", True), ("CONSISTENCY", "consistency", True),
        ]:
            cv = pcs[key]
            lv = pls[key]
            if isinstance(cv, float):
                cv_s = f"${cv:>+,.0f}" if key in ("net_pnl", "max_dd") else f"{cv:.2f}"
                lv_s = f"${lv:>+,.0f}" if key in ("net_pnl", "max_dd") else f"{lv:.2f}"
            else:
                cv_s = str(cv)
                lv_s = str(lv)
            if higher_better is None:
                winner = ""
            elif higher_better:
                winner = ll if lv > cv else cl if cv > lv else "TIE"
            else:
                winner = ll if lv < cv else cl if cv < lv else "TIE"
            bold = " ◀" if label == "CONSISTENCY" else ""
            w(f"  {label:>22} | {cv_s:>12} | {lv_s:>12} | {winner:>8}{bold}")

    # Year-by-year
    w(f"\n{'─' * 90}")
    w("  YEAR-BY-YEAR COMPARISON")
    w(f"{'─' * 90}")
    w(f"  {'Year':>6} | {cl+' Trades':>9} {cl+' P&L':>10} | {ll+' Trades':>9} {ll+' P&L':>10} | {'Δ P&L':>10} {'Winner':>8}")
    w("  " + "-" * 78)
    champ_yr_wins = chall_yr_wins = 0
    for y in sorted(r.yearly_comparison.keys()):
        yc = r.yearly_comparison[y]
        w(f"  {y:>6} | {yc['champion_trades']:>9} ${yc['champion_pnl']:>+9,.0f} | {yc['challenger_trades']:>9} ${yc['challenger_pnl']:>+9,.0f} | ${yc['delta']:>+9,.0f} {yc['winner']:>8}")
        if yc["delta"] > 0:
            chall_yr_wins += 1
        elif yc["delta"] < 0:
            champ_yr_wins += 1
    w(f"\n  Year wins: {cl}={champ_yr_wins} | {ll}={chall_yr_wins}")

    # Month-by-month
    w(f"\n{'─' * 90}")
    w("  MONTH-BY-MONTH COMPARISON")
    w(f"{'─' * 90}")
    w(f"  {'Month':>8} | {cl+' P&L':>10} | {ll+' P&L':>10} | {'Δ':>10} | W")
    w("  " + "-" * 55)
    champ_mo = chall_mo = ties = 0
    for m in sorted(r.monthly_comparison.keys()):
        mc = r.monthly_comparison[m]
        wn = mc["winner"]
        short_w = ll[:2] if wn == ll else cl[:2] if wn == cl else "="
        w(f"  {m:>8} | ${mc['champion_pnl']:>+9,.0f} | ${mc['challenger_pnl']:>+9,.0f} | ${mc['delta']:>+9,.0f} | {short_w}")
        if mc["delta"] > 0:
            chall_mo += 1
        elif mc["delta"] < 0:
            champ_mo += 1
        else:
            ties += 1
    w(f"\n  Month wins: {cl}={champ_mo} | {ll}={chall_mo} | ties={ties}")

    # Stagnation impact
    if r.stagnation_impact:
        si = r.stagnation_impact
        w(f"\n{'─' * 90}")
        w("  STAGNATION GUARD IMPACT")
        w(f"{'─' * 90}")
        w(f"  STAG_EXIT: {si['stag_count']} trades, ${si['stag_total_pnl']:>+,.0f} total, ${si['stag_avg_pnl']:>+,.0f} avg, {si['stag_win_rate']:.0f}% WR")
        w(f"  TIME_STOP: {cl} {si['champion_timestop_count']} (${si['champion_timestop_pnl']:>+,.0f}) → {ll} {si['challenger_timestop_count']} (${si['challenger_timestop_pnl']:>+,.0f}) | Δ {si['timestop_delta']:+d} trades")
        if si["timestop_delta"] >= 0:
            w(f"  ✓ TIME_STOP preserved")
        else:
            w(f"  ⚠ TIME_STOP DROPPED by {abs(si['timestop_delta'])} trades!")

    # Verdict
    v = r.verdict
    w(f"\n{'─' * 90}")
    w("  FINAL VERDICT")
    w(f"{'─' * 90}")
    for c in v.criteria:
        marker = "✓" if c.challenger_wins else "✗"
        who = ll if c.challenger_wins else cl
        w(f"  {marker} {c.name:>25}: {who} — {c.detail}")
    w(f"\n  Score: {v.score}")
    w(f"\n  ╔══════════════════════════════════════════════════╗")
    if v.winner == "challenger":
        w(f"  ║  NEW CHAMPION: {ll:<35}║")
    elif v.winner == "draw":
        w(f"  ║  DRAW — {cl} remains champion{' ' * 15}║")
    else:
        w(f"  ║  CHAMPION: {cl} (still king){' ' * 15}║")
    w(f"  ║  {v.recommendation[:48]:<48}║")
    w(f"  ╚══════════════════════════════════════════════════╝")
    w("")

    return "\n".join(lines)


def format_markdown(r: ComparisonResult) -> str:
    """Markdown report for vault storage."""
    cl = r.champion_label
    ll = r.challenger_label
    cs = r.champion_stats
    ls = r.challenger_stats
    v = r.verdict

    winner_text = f"{ll} wins" if v.winner == "challenger" else f"{cl} remains champion" if v.winner == "champion" else "Draw"

    lines: list[str] = []
    w = lines.append

    w(f"# {ll} vs {cl} — Champion-Challenger Analysis\n")
    w(f"**Result: {winner_text}** (Score: {v.score})\n")

    # Head-to-head table
    w("## Head-to-Head (EURUSD H1, 10yr)\n")
    w("| Metric | Champion ({}) | Challenger ({}) | Winner |".format(cl, ll))
    w("|--------|-------------|----------------|--------|")
    w(f"| Net P&L | ${cs['net_pnl']:+,.0f} | ${ls['net_pnl']:+,.0f} | {cl if cs['net_pnl'] > ls['net_pnl'] else ll} |")
    w(f"| Trades | {cs['trades']} | {ls['trades']} | — |")
    w(f"| Consistency | {cs['consistency']:.2f} | {ls['consistency']:.2f} | {cl if cs['consistency'] > ls['consistency'] else ll} |")
    w(f"| Trade Win Rate | {cs['trade_win_rate']:.1f}% | {ls['trade_win_rate']:.1f}% | {cl if cs['trade_win_rate'] > ls['trade_win_rate'] else ll} |")
    w(f"| Max Drawdown | ${cs['max_dd']:,.0f} | ${ls['max_dd']:,.0f} | {cl if cs['max_dd'] < ls['max_dd'] else ll} |")
    w(f"| Max DD Duration | {cs['max_dd_months']} mo | {ls['max_dd_months']} mo | {cl if cs['max_dd_months'] < ls['max_dd_months'] else ll} |")

    # Exit comparison
    all_exits = sorted(set(list(r.champion_exits.keys()) + list(r.challenger_exits.keys())))
    w("\n## Exit Type Comparison\n")
    w(f"| Exit Type | {cl} Count | {cl} P&L | {ll} Count | {ll} P&L | Δ P&L |")
    w("|-----------|-------|------|-------|------|-------|")
    for sig in all_exits:
        ce = r.champion_exits.get(sig)
        le = r.challenger_exits.get(sig)
        cc = ce.count if ce else 0
        cp = ce.total_pnl if ce else 0
        lc = le.count if le else 0
        lp = le.total_pnl if le else 0
        w(f"| {sig} | {cc} | ${cp:+,.0f} | {lc} | ${lp:+,.0f} | ${lp - cp:+,.0f} |")

    # Period breakdown
    w("\n## Period Breakdown\n")
    w(f"| Period | {cl} P&L | {ll} P&L | {cl} C-Score | {ll} C-Score | Winner |")
    w("|--------|------|------|---------|---------|--------|")
    for pk, pl in [("1yr", "1yr"), ("3yr", "3yr"), ("5yr", "5yr"), ("full", "10yr")]:
        pc = r.period_comparisons[pk]
        pcs = pc["champion"]
        pls = pc["challenger"]
        pw = ll if pls["net_pnl"] > pcs["net_pnl"] else cl
        w(f"| {pl} | ${pcs['net_pnl']:+,.0f} | ${pls['net_pnl']:+,.0f} | {pcs['consistency']:.2f} | {pls['consistency']:.2f} | {pw} |")

    # Year-by-year
    w("\n## Year-by-Year\n")
    w(f"| Year | {cl} | {ll} | Winner |")
    w("|------|------|------|--------|")
    champ_yr = chall_yr = 0
    for y in sorted(r.yearly_comparison.keys()):
        yc = r.yearly_comparison[y]
        w(f"| {y} | ${yc['champion_pnl']:+,.0f} | ${yc['challenger_pnl']:+,.0f} | {yc['winner']} |")
        if yc["delta"] > 0:
            chall_yr += 1
        elif yc["delta"] < 0:
            champ_yr += 1
    w(f"\nYear wins: {cl}={champ_yr} | {ll}={chall_yr}")

    # Stagnation
    if r.stagnation_impact:
        si = r.stagnation_impact
        w("\n## Stagnation Guard Impact\n")
        w(f"- STAG_EXIT: {si['stag_count']} trades, ${si['stag_total_pnl']:+,.0f} total, {si['stag_win_rate']:.0f}% WR")
        w(f"- TIME_STOP: {si['champion_timestop_count']} → {si['challenger_timestop_count']} ({si['timestop_delta']:+d} trades)")

    # Verdict
    w("\n## Verdict\n")
    for c in v.criteria:
        marker = "✓" if c.challenger_wins else "✗"
        w(f"- {marker} **{c.name}**: {c.detail}")
    w(f"\n**{v.recommendation}**")

    return "\n".join(lines)


def format_telegram(r: ComparisonResult) -> str:
    """Concise Telegram notification."""
    v = r.verdict
    cs = r.champion_stats
    ls = r.challenger_stats
    cl = r.champion_label
    ll = r.challenger_label

    if v.winner == "challenger":
        emoji = "🏆"
        headline = f"NEW CHAMPION: {ll}"
    elif v.winner == "draw":
        emoji = "🤝"
        headline = f"DRAW — {cl} stays"
    else:
        emoji = "👑"
        headline = f"{cl} DEFENDS"

    chall_yr = sum(1 for y in r.yearly_comparison.values() if y["delta"] > 0)
    champ_yr = sum(1 for y in r.yearly_comparison.values() if y["delta"] < 0)

    msg = f"""{emoji} IRB Champion-Challenger Result

{headline} (Score: {v.score})

{cl}: ${cs['net_pnl']:+,.0f} | C={cs['consistency']:.2f}
{ll}: ${ls['net_pnl']:+,.0f} | C={ls['consistency']:.2f}

Year wins: {cl}={champ_yr} | {ll}={chall_yr}"""

    if r.stagnation_impact:
        si = r.stagnation_impact
        msg += f"\nTIME_STOP: {si['champion_timestop_count']}→{si['challenger_timestop_count']} ({si['timestop_delta']:+d})"

    return msg
