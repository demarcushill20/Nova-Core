"""Robustness report for NovaTrade IRB strategy.

Generates monthly P&L waterfall, consecutive win/loss streaks,
rolling returns, yearly breakdown, and consistency scoring.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, "/home/nova/nova-core")

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.data.loader import load_candles


def run_report():  # noqa: C901
    # Load data
    h1 = load_candles("/home/nova/nova-core/data/candles/EURUSD_H1.csv")
    h4 = load_candles("/home/nova/nova-core/data/candles/EURUSD_H4.csv")

    # Run backtest
    env = BacktestEnvironment()
    bt = IRBBacktester(env)
    result = bt.run(h1, h4)
    trades = result.trades

    print("=" * 72)
    print("  NOVATRADE IRB v2.0.0 — ROBUSTNESS REPORT")
    print("=" * 72)
    print(f"  Data: EURUSD H1 | {len(h1)} bars | {len(h4)} H4 bars")
    first_dt = datetime.utcfromtimestamp(h1[0].timestamp)
    last_dt = datetime.utcfromtimestamp(h1[-1].timestamp)
    print(f"  Period: {first_dt:%Y-%m-%d} to {last_dt:%Y-%m-%d}")
    span_years = (last_dt - first_dt).days / 365.25
    print(f"  Span: {span_years:.1f} years")
    print(f"  Total trades: {len(trades)}")
    print(f"  Final equity: ${result.final_equity:,.0f}")
    print(f"  Net P&L: ${result.final_equity - env.initial_equity:,.0f}")
    print("=" * 72)

    # ── Monthly P&L ──────────────────────────────────────────────────────
    monthly_pnl = defaultdict(float)
    monthly_trades = defaultdict(int)
    monthly_wins = defaultdict(int)
    monthly_losses = defaultdict(int)

    for t in trades:
        # Timestamps may be 0 — fall back to bar index → candle timestamp
        if t.exit_timestamp and t.exit_timestamp > 0:
            dt = datetime.utcfromtimestamp(t.exit_timestamp)
        else:
            bar_idx = min(t.exit_bar, len(h1) - 1)
            dt = datetime.utcfromtimestamp(h1[bar_idx].timestamp)
        key = dt.strftime("%Y-%m")
        pnl = t.pnl_usd
        monthly_pnl[key] += pnl
        monthly_trades[key] += 1
        if pnl >= 0:
            monthly_wins[key] += 1
        else:
            monthly_losses[key] += 1

    months = sorted(monthly_pnl.keys())

    print("\n── MONTHLY P&L WATERFALL ──────────────────────────────────────")
    print(f"{'Month':>8} | {'Trades':>6} | {'W/L':>5} | {'P&L':>10} | {'Cum P&L':>10} | Bar")
    print("-" * 72)

    cum = 0
    max_cum = 0
    max_dd = 0
    dd_months = 0
    max_dd_months = 0
    profitable_months = 0
    losing_months = 0
    consec_win = 0
    max_consec_win = 0
    consec_loss = 0
    max_consec_loss = 0
    win_pnls = []
    loss_pnls = []
    all_monthly_pnls = []

    for m in months:
        pnl = monthly_pnl[m]
        cum += pnl
        all_monthly_pnls.append(pnl)

        if cum > max_cum:
            max_cum = cum
            dd_months = 0
        dd = max_cum - cum
        if dd > max_dd:
            max_dd = dd
        if cum < max_cum:
            dd_months += 1
            max_dd_months = max(max_dd_months, dd_months)

        w = monthly_wins[m]
        losses = monthly_losses[m]
        n = monthly_trades[m]

        if pnl >= 0:
            profitable_months += 1
            consec_win += 1
            consec_loss = 0
            max_consec_win = max(max_consec_win, consec_win)
            win_pnls.append(pnl)
            marker = "+" * min(int(pnl / 200) + 1, 20)
        else:
            losing_months += 1
            consec_loss += 1
            consec_win = 0
            max_consec_loss = max(max_consec_loss, consec_loss)
            loss_pnls.append(pnl)
            marker = "-" * min(int(abs(pnl) / 200) + 1, 20)

        print(f"  {m} | {n:6d} | {w:2d}/{losses:<2d} | ${pnl:>+9,.0f} | ${cum:>+9,.0f} | {marker}")

    total_months = len(months)

    # ── Yearly Breakdown ─────────────────────────────────────────────────
    yearly_pnl = defaultdict(float)
    yearly_trades = defaultdict(int)
    yearly_months_profitable = defaultdict(int)
    yearly_months_total = defaultdict(int)

    for m in months:
        year = m[:4]
        yearly_pnl[year] += monthly_pnl[m]
        yearly_trades[year] += monthly_trades[m]
        yearly_months_total[year] += 1
        if monthly_pnl[m] >= 0:
            yearly_months_profitable[year] += 1

    print("\n── YEARLY BREAKDOWN ──────────────────────────────────────────")
    print(f"{'Year':>6} | {'Trades':>6} | {'P&L':>10} | {'Prof Mo':>8} | {'Mo Win%':>7}")
    print("-" * 55)
    for year in sorted(yearly_pnl.keys()):
        yt = yearly_trades[year]
        yp = yearly_pnl[year]
        pm = yearly_months_profitable[year]
        tm = yearly_months_total[year]
        pct = 100 * pm / tm if tm else 0
        print(f"  {year} | {yt:6d} | ${yp:>+9,.0f} | {pm:>4d}/{tm:<3d} | {pct:5.0f}%")

    # ── Rolling 12-Month Return ──────────────────────────────────────────
    if len(all_monthly_pnls) >= 12:
        print("\n── ROLLING 12-MONTH P&L ──────────────────────────────────────")
        for i in range(len(months) - 11):
            window = all_monthly_pnls[i : i + 12]
            total = sum(window)
            win_mo = sum(1 for p in window if p >= 0)
            end_month = months[i + 11]
            bar = "+" * max(1, int(total / 500)) if total >= 0 else "-" * max(1, int(abs(total) / 500))
            print(f"  {months[i]}→{end_month} | ${total:>+9,.0f} | {win_mo}/12 mo | {bar}")

    # ── Consistency Score ────────────────────────────────────────────────
    pct_profitable = profitable_months / total_months if total_months else 0
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
    avg_loss = abs(sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 1
    win_loss_ratio = avg_win / avg_loss if avg_loss else 0
    streak_ratio = max_consec_win / max_consec_loss if max_consec_loss else max_consec_win

    consistency_score = pct_profitable * win_loss_ratio * streak_ratio

    print("\n── ROBUSTNESS SUMMARY ────────────────────────────────────────")
    print(f"  Profitable months:        {profitable_months}/{total_months} ({100 * pct_profitable:.0f}%)")
    print(f"  Losing months:            {losing_months}/{total_months} ({100 * losing_months / total_months:.0f}%)")
    print(f"  Max consecutive winners:  {max_consec_win}")
    print(f"  Max consecutive losers:   {max_consec_loss}")
    print(f"  Avg winning month:        ${avg_win:,.0f}")
    print(f"  Avg losing month:         ${-abs(sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0:,.0f}")
    print(f"  Win/Loss month ratio:     {win_loss_ratio:.2f}")
    print(f"  Max drawdown:             ${max_dd:,.0f}")
    print(f"  Max DD duration:          {max_dd_months} months")
    print()
    print("  ╔══════════════════════════════════════╗")
    print(f"  ║  CONSISTENCY SCORE:  {consistency_score:.2f}           ║")
    print("  ║                                      ║")
    print("  ║  Formula: %profitable × W/L ratio    ║")
    print("  ║           × streak ratio              ║")
    print("  ║                                      ║")
    print("  ║  Target: > 1.0 = tradeable           ║")
    print("  ║          > 2.0 = strong               ║")
    print("  ║          > 3.0 = exceptional           ║")
    print("  ╚══════════════════════════════════════╝")
    print()

    # ── Diagnosis ────────────────────────────────────────────────────────
    print("── DIAGNOSIS ─────────────────────────────────────────────────")
    if pct_profitable < 0.55:
        print("  ⚠ Monthly win rate below 55% — strategy has too many losing months")
    if max_consec_loss >= 4:
        print(f"  ⚠ {max_consec_loss} consecutive losing months — psychological danger zone")
    if max_dd > 5000:
        print(f"  ⚠ Max drawdown ${max_dd:,.0f} exceeds $5K — risk management needs tightening")
    if win_loss_ratio < 1.2:
        print("  ⚠ Avg win month barely exceeds avg loss month — edge is thin")
    if consistency_score < 1.0:
        print("  ✗ Consistency score < 1.0 — NOT TRADEABLE in current form")
        print("    Needs: better monthly win rate, larger avg wins, or smaller avg losses")
    elif consistency_score < 2.0:
        print("  ~ Consistency score 1.0-2.0 — MARGINAL, needs improvement")
    else:
        print("  ✓ Consistency score > 2.0 — TRADEABLE")

    print()


if __name__ == "__main__":
    run_report()
