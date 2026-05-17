"""Analyze the TIMBOT XAUUSD 15m backtest trade list.

Each trade number has two rows (Entry + Exit) with the same Net P&L.
We dedupe by trade number and compute real metrics.
"""

import csv
import statistics
import sys
from collections import Counter
from pathlib import Path

CSV = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("backtest_XAUUSD_15m_2026-05-17.csv")
INITIAL_CAPITAL = 100_000.0

trades: dict = {}  # trade_no -> dict
with CSV.open() as f:
    for row in csv.DictReader(f):
        no = int(row["Trade #"])
        pnl = float(row["Net P&L USD"])
        sig = row["Signal"]
        typ = row["Type"]
        val = float(row["Size (value)"])
        # keep one record per trade; prefer the Exit row's signal as exit reason
        rec = trades.setdefault(no, {"pnl": pnl, "value": val})
        if typ.startswith("Exit"):
            rec["exit_reason"] = sig
            rec["pnl"] = pnl
            rec["value"] = val

pnls = [t["pnl"] for t in trades.values()]
n = len(pnls)
wins = [p for p in pnls if p > 0]
losses = [p for p in pnls if p < 0]
flat = [p for p in pnls if p == 0]

gross_profit = sum(wins)
gross_loss = -sum(losses)
net = sum(pnls)
pf = gross_profit / gross_loss if gross_loss else float("inf")

# equity curve + max drawdown (on closed-trade equity)
equity = INITIAL_CAPITAL
peak = equity
max_dd = 0.0
max_dd_pct = 0.0
curve = []
for p in pnls:
    equity += p
    curve.append(equity)
    peak = max(peak, equity)
    dd = peak - equity
    if dd > max_dd:
        max_dd = dd
        max_dd_pct = dd / peak * 100

exit_reasons = Counter(t.get("exit_reason", "?") for t in trades.values())

# margin-call trades
mc = [no for no, t in trades.items() if t.get("exit_reason") == "Margin call"]

# "instant" runner exits: HTF runner exits that returned ~commission only
tiny_runner = [no for no, t in trades.items() if t.get("exit_reason") == "HTF runner" and abs(t["pnl"]) < 25]

print("=" * 60)
print("TIMBOT — XAUUSD 15m backtest analysis")
print("=" * 60)
print("Window               : 2025-11-30 -> 2026-05-14 (~5.5 months)")
print(f"Inferred capital     : ${INITIAL_CAPITAL:,.0f}")
print(f"Closed trades        : {n}")
print(f"  Wins / Losses / 0  : {len(wins)} / {len(losses)} / {len(flat)}")
print(f"Win rate             : {len(wins) / n * 100:.1f}%")
print(f"Win rate (excl. 0)   : {len(wins) / (len(wins) + len(losses)) * 100:.1f}%")
print(f"Net profit           : ${net:,.2f}  ({net / INITIAL_CAPITAL * 100:.1f}%)")
print(f"Gross profit         : ${gross_profit:,.2f}")
print(f"Gross loss           : ${gross_loss:,.2f}")
print(f"Profit factor        : {pf:.2f}")
print(f"Avg trade            : ${net / n:,.2f}")
print(f"Avg win              : ${statistics.mean(wins):,.2f}")
print(f"Avg loss             : ${statistics.mean(losses):,.2f}")
print(f"Largest win          : ${max(wins):,.2f}")
print(f"Largest loss         : ${min(losses):,.2f}")
print(f"Max drawdown         : ${max_dd:,.2f}  ({max_dd_pct:.2f}%)")
print(f"Final equity         : ${curve[-1]:,.2f}")
print()
print("Exit-reason breakdown:")
for reason, c in exit_reasons.most_common():
    rp = sum(t["pnl"] for t in trades.values() if t.get("exit_reason") == reason)
    print(f"  {reason:<14} {c:>4}  net ${rp:,.2f}")
print()
print(f"MARGIN-CALL trades   : {len(mc)}  ({len(mc) / n * 100:.0f}% of all trades)")
print(f"  trade #s           : {mc}")
print()
print(f"Near-zero 'HTF runner' exits (|P&L|<$25): {len(tiny_runner)}")
print("  -> runner limit filled instantly at/through entry")
print(f"  trade #s           : {tiny_runner}")
