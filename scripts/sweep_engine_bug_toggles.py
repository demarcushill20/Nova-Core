"""Sweep B1 / B2 / B3 engine-bug toggles on the live champion config.

Runs the 10y M5 EURUSD backtest 5 times: baseline (no toggles), each toggle
individually, and all three combined. Reports headline metrics so we can
see which toggle moves the needle and by how much.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.cli.config_schema import StrategyConfig
from novatrade.models import Candle

REPO = Path("/home/nova/nova-core")
M5_CSV = REPO / "data/candles/EURUSD_M5_10yr.csv"
H1_CSV = REPO / "data/candles/EURUSD_H1_10yr.csv"
CFG = REPO / "configs/strategies/irb_v5_m5_champion.yaml"


def parse_ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def load_candles(path: Path, tf: str) -> list[Candle]:
    out = []
    with path.open() as f:
        r = csv.DictReader(f)
        for row in r:
            out.append(
                Candle(
                    timestamp=parse_ts(row["timestamp"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                    symbol="EURUSD",
                    timeframe=tf,
                )
            )
    out.sort(key=lambda c: c.timestamp)
    return out


def make_env(toggles: frozenset[str]) -> BacktestEnvironment:
    cfg = StrategyConfig.from_yaml(CFG)
    env_kwargs = cfg.to_environment_kwargs()
    base = BacktestEnvironment.for_v5_m5()
    extra = {
        "primary_timeframe": "M5",
        "higher_timeframe": "H1",
        "h1_to_h4_ratio": 12,
        "h1_bars_per_day": 288,
        "h4_bars_per_day": 24,
        "parity_audit_toggles": toggles,
    }
    # B4: execution-buffer match — when "b4_minimal_pip_buffer" toggle is set,
    # collapse nova's 1-pip pip_buffer + 1-pip sl_spread_buffer to match
    # vault's 0.1-pip-tick approach. Implemented as env overrides because
    # these aren't gated by parity_audit_toggles in the engine.
    if "b4_minimal_pip_buffer" in toggles:
        extra["pip_buffer"] = 0.00001
        extra["sl_spread_buffer_pips"] = 0.0
        extra["atr_sl_floor_multiplier"] = 0.0  # vault has no ATR floor either
    valid = {k: v for k, v in env_kwargs.items() if k in base.__dataclass_fields__}
    valid.update(extra)
    return replace(base, **valid)


def run_one(label: str, toggles: frozenset[str], m5: list[Candle], h1: list[Candle]) -> dict:
    env = make_env(toggles)
    bt = IRBBacktester(env=env)
    res = bt.run(m5, h1)

    init_eq = env.initial_equity
    trades = res.trades
    if not trades:
        return {
            "label": label,
            "toggles": sorted(toggles),
            "trades": 0,
            "net": 0.0,
            "wr": 0.0,
            "pf": 0.0,
            "max_dd_pct": 0.0,
            "exit_mix": {},
        }

    pnls = [t.pnl_usd for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = sum(wins) / -sum(losses) if losses and sum(losses) < 0 else (float("inf") if wins else 0.0)

    # equity curve & drawdown
    sorted_trades = sorted(trades, key=lambda t: m5[t.exit_bar].timestamp if 0 <= t.exit_bar < len(m5) else 0)
    eq = init_eq
    peak = init_eq
    max_dd = 0.0
    max_dd_pct = 0.0
    for t in sorted_trades:
        eq += t.pnl_usd
        peak = max(peak, eq)
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak * 100.0 if peak else 0.0

    exit_mix = Counter(t.exit_reason.name for t in trades)

    # profitable years
    by_year: dict[int, list[float]] = {}
    for t in trades:
        y = datetime.utcfromtimestamp(m5[t.exit_bar].timestamp).year if 0 <= t.exit_bar < len(m5) else 0
        by_year.setdefault(y, []).append(t.pnl_usd)
    prof_yrs = sum(1 for _y, ps in by_year.items() if sum(ps) > 0)

    return {
        "label": label,
        "toggles": sorted(toggles),
        "trades": len(trades),
        "net": sum(pnls),
        "wr": len(wins) / len(pnls) * 100.0,
        "pf": pf,
        "max_dd_pct": max_dd_pct,
        "exit_mix": dict(exit_mix),
        "prof_years": f"{prof_yrs}/{len(by_year)}",
        "final_eq": init_eq + sum(pnls),
    }


def main() -> None:
    print("[load] M5 + H1 candles...")
    m5 = load_candles(M5_CSV, "M5")
    h1 = load_candles(H1_CSV, "H1")
    print(f"        M5={len(m5):,}  H1={len(h1):,}")
    print()

    runs = [
        ("baseline (no toggles)", frozenset()),
        ("B4 only (min pip_buffer)", frozenset(["b4_minimal_pip_buffer"])),
        ("B1+B2 (trail+BE high)", frozenset(["b1_trail_high_anchor", "b2_be_high_trigger"])),
        ("B4+B3 (buffer + replace)", frozenset(["b4_minimal_pip_buffer", "b3_pending_replace_any_side"])),
        (
            "B1+B2+B4 (anchors + buffer)",
            frozenset(["b1_trail_high_anchor", "b2_be_high_trigger", "b4_minimal_pip_buffer"]),
        ),
        (
            "ALL (B1+B2+B3+B4)",
            frozenset(
                ["b1_trail_high_anchor", "b2_be_high_trigger", "b3_pending_replace_any_side", "b4_minimal_pip_buffer"]
            ),
        ),
    ]

    results: list[dict] = []
    for label, toggles in runs:
        print(f"[run] {label}...")
        r = run_one(label, toggles, m5, h1)
        results.append(r)
        print(
            f"      trades={r['trades']:,}  net=${r['net']:+,.2f}  WR={r['wr']:.1f}%  "
            f"PF={r['pf']:.3f}  maxDD={r['max_dd_pct']:.1f}%  prof_yrs={r['prof_years']}"
        )
    print()

    # Summary table
    print("=" * 100)
    print(f"{'Run':>30} | {'Trades':>8} | {'Net P&L':>14} | {'WR':>7} | {'PF':>6} | {'Max DD':>7} | {'Years':>5}")
    print("-" * 100)
    for r in results:
        print(
            f"{r['label']:>30} | {r['trades']:>8,} | ${r['net']:>+12,.2f} | "
            f"{r['wr']:>5.1f}% | {r['pf']:>6.3f} | {r['max_dd_pct']:>5.1f}% | {r['prof_years']:>5}"
        )
    print()
    print("[ref] vault matched-risk: trades=32,815  net=+$45,309.55  WR=49.3%  PF=1.081  maxDD=6.9%  prof_yrs=11/11")

    # Save report
    out_md = REPO / "OUTPUT" / f"sweep_engine_bug_toggles_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
    ref_line = (
        "**Reference (vault matched-risk):** trades=32,815  net=+$45,310  WR=49.3%  PF=1.081  maxDD=6.9%  11/11 years"
    )
    lines = [
        "# Engine-bug toggle sweep — vault matched config",
        "",
        ref_line,
        "",
        "| Run | Trades | Net P&L | WR | PF | Max DD | Profitable yrs | Exit mix |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in results:
        mix = " · ".join(f"{k}:{v}" for k, v in sorted(r["exit_mix"].items(), key=lambda x: -x[1]))
        lines.append(
            f"| {r['label']} | {r['trades']:,} | ${r['net']:+,.2f} | {r['wr']:.1f}% | "
            f"{r['pf']:.3f} | {r['max_dd_pct']:.1f}% | {r['prof_years']} | {mix} |"
        )
    out_md.write_text("\n".join(lines))
    print(f"[saved] {out_md}")


if __name__ == "__main__":
    main()
