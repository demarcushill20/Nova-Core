"""Run a 10-year backtest of the current live IRB v5 M5 champion strategy.

Loads EURUSD M5 + H1 historical candles (2016-01-04 → 2026-03-27),
runs the validated IRB v5 engine with the live champion config, and
emits total / yearly / monthly performance breakdowns.

Usage:
    python3 scripts/run_10yr_backtest.py
"""

from __future__ import annotations

import csv
import math
import sys
from collections import defaultdict
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
DEFAULT_CONFIG = REPO / "configs/strategies/irb_v5_m5_champion.yaml"
CONFIG_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG
OUT_DIR = REPO / "OUTPUT"


def _parse_ts(s: str) -> float:
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        pass
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def load_candles(path: Path, symbol: str, timeframe: str) -> list[Candle]:
    candles: list[Candle] = []
    with path.open("r") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            ts = _parse_ts(row["timestamp"])
            o = float(row["open"])
            h = float(row["high"])
            lo = float(row["low"])
            c = float(row["close"])
            v = float(row.get("volume") or 0.0)
            if any(math.isnan(x) for x in (o, h, lo, c)):
                raise ValueError(f"NaN at {path.name} row {i}")
            candles.append(
                Candle(
                    timestamp=ts,
                    open=o,
                    high=h,
                    low=lo,
                    close=c,
                    volume=v,
                    symbol=symbol,
                    timeframe=timeframe,
                )
            )
    candles.sort(key=lambda x: x.timestamp)
    return candles


def fmt_money(x: float) -> str:
    sign = "-" if x < 0 else " "
    return f"{sign}${abs(x):>11,.2f}"


def main() -> None:  # noqa: C901  # diagnostic script; complexity acceptable
    print(f"[config] {CONFIG_PATH.name}")
    cfg = StrategyConfig.from_yaml(CONFIG_PATH)
    env_kwargs = cfg.to_environment_kwargs()

    primary_tf = cfg.primary_timeframe
    higher_tf = cfg.higher_timeframe
    print(f"[load] primary={primary_tf} higher={higher_tf}")

    # Load primary timeframe data
    if primary_tf == "M5":
        m5 = load_candles(M5_CSV, "EURUSD", "M5")
        primary = m5
    elif primary_tf == "H1":
        primary = load_candles(H1_CSV, "EURUSD", "H1")
    else:
        raise SystemExit(f"Unsupported primary timeframe: {primary_tf}")
    print(
        f"        primary {len(primary):,} bars  "
        f"({datetime.utcfromtimestamp(primary[0].timestamp).date()} → "
        f"{datetime.utcfromtimestamp(primary[-1].timestamp).date()})"
    )

    # Load / derive higher timeframe data
    if higher_tf == "H1":
        higher = load_candles(H1_CSV, "EURUSD", "H1")
    elif higher_tf == "H4":
        from novatrade.cli.commands.data import aggregate_h1_to_h4

        h1_for_agg = primary if primary_tf == "H1" else load_candles(H1_CSV, "EURUSD", "H1")
        higher = aggregate_h1_to_h4(h1_for_agg)
    else:
        raise SystemExit(f"Unsupported higher timeframe: {higher_tf}")
    print(f"        higher {len(higher):,} bars")

    # Set H1↔H4 (or M5↔H1) ratio for the env
    primary_higher_ratio = {("M5", "H1"): 12, ("H1", "H4"): 4}.get((primary_tf, higher_tf), 4)

    # Need to start from the right factory: for_v5_m5 sets M5 ratios; for H1 we
    # need different ratios.
    base_env = BacktestEnvironment.for_v5_m5()
    extra_overrides = {
        "primary_timeframe": primary_tf,
        "higher_timeframe": higher_tf,
        "h1_to_h4_ratio": primary_higher_ratio,
        "h1_bars_per_day": 24 if primary_tf == "H1" else 288,
        "h4_bars_per_day": 6 if higher_tf == "H4" else 24,
    }
    valid = {k: v for k, v in env_kwargs.items() if k in base_env.__dataclass_fields__}
    valid.update(extra_overrides)
    env = replace(base_env, **valid)

    print(
        f"[env] initial_equity=${env.initial_equity:,.0f}  risk_fraction={env.risk_fraction * 100:.2f}%  "
        f"primary={env.primary_timeframe} higher={env.higher_timeframe}"
    )
    m5 = primary  # alias for the rest of the script
    h1 = higher

    print("[run] IRBBacktester ...")
    bt = IRBBacktester(env=env)
    res = bt.run(m5, h1)
    print(f"[done] trades={len(res.trades):,}  total_bars={res.total_bars:,}  final_equity=${res.final_equity:,.2f}")

    # ---- Aggregate ----
    init_eq = env.initial_equity
    trades = res.trades
    if not trades:
        print("No trades.")
        return

    # The engine doesn't populate entry_timestamp/exit_timestamp on
    # CompletedTrade, so derive them from the candle bar indices.
    def _ts_of(bar_idx: int) -> float:
        if 0 <= bar_idx < len(m5):
            return m5[bar_idx].timestamp
        return 0.0

    for t in trades:
        t.entry_timestamp = _ts_of(t.entry_bar)
        t.exit_timestamp = _ts_of(t.exit_bar)

    # Sort by exit_timestamp (closing time defines the period bucket)
    trades_sorted = sorted(trades, key=lambda t: t.exit_timestamp)

    # Equity curve & drawdown (chronological, by close time)
    equity = init_eq
    peak = init_eq
    max_dd = 0.0
    max_dd_pct = 0.0
    cum_pnl_by_ym: dict[tuple[int, int], list[float]] = defaultdict(list)
    cum_pnl_by_y: dict[int, list[float]] = defaultdict(list)
    eq_curve: list[tuple[float, float]] = []

    for t in trades_sorted:
        equity += t.pnl_usd
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak * 100.0
        eq_curve.append((t.exit_timestamp, equity))
        dt = datetime.utcfromtimestamp(t.exit_timestamp)
        cum_pnl_by_y[dt.year].append(t.pnl_usd)
        cum_pnl_by_ym[(dt.year, dt.month)].append(t.pnl_usd)

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

    all_pnls = [t.pnl_usd for t in trades_sorted]
    overall = _stats(all_pnls)
    overall["max_dd"] = max_dd
    overall["max_dd_pct"] = max_dd_pct

    years = sorted(cum_pnl_by_y.keys())
    months = sorted(cum_pnl_by_ym.keys())

    # ---- Print ----
    lines: list[str] = []
    p = lines.append

    p("# IRB v5 M5 Champion — 10-Year Backtest Report")
    p("")
    p(f"**Strategy:** `{cfg.name}` v{cfg.version}")
    p("**Symbol / TF:** EURUSD  M5 (primary) + H1 (higher)")
    win_start = datetime.utcfromtimestamp(m5[0].timestamp).date()
    win_end = datetime.utcfromtimestamp(m5[-1].timestamp).date()
    p(f"**Window:** {win_start} → {win_end}")
    p(f"**Initial equity:** ${init_eq:,.2f}")
    cost_model = (
        f"spread={env.spread.avg_spread_pips}pip  "
        f"slip={env.spread.slippage_pips}pip  "
        f"comm=${env.spread.commission_per_lot_usd}/lot"
    )
    p(f"**Risk / trade:** {env.risk_fraction * 100:.2f}%   **Cost model:** {cost_model}")
    p("")
    p("## Headline")
    p("")
    final_eq = init_eq + overall["net"]
    total_ret_pct = overall["net"] / init_eq * 100.0
    n_years = (m5[-1].timestamp - m5[0].timestamp) / (365.25 * 86400)
    cagr = (final_eq / init_eq) ** (1 / n_years) - 1 if n_years > 0 and final_eq > 0 else float("nan")
    p(f"- Net P&L:                **{fmt_money(overall['net'])}**  ({total_ret_pct:+.2f}%)")
    p(f"- Final equity:           ${final_eq:,.2f}")
    p(f"- CAGR (~{n_years:.2f}y):           {cagr * 100:.2f}%")
    p(f"- Total trades:           {overall['trades']:,}")
    p(f"- Win rate:               {overall['wr']:.2f}%   ({overall['wins']}W / {overall['losses']}L)")
    p(f"- Profit factor:          {overall['pf']:.3f}")
    p(f"- Avg P&L / trade:        {fmt_money(overall['avg'])}")
    p(f"- Max drawdown:           {fmt_money(-overall['max_dd'])}  ({overall['max_dd_pct']:.2f}% of peak)")
    p("")

    # Yearly table
    p("## Year-by-Year")
    p("")
    p("| Year | Trades | Wins | Losses | Win % | Net P&L | Return % | Profit Factor |")
    p("|-----:|-------:|-----:|-------:|------:|--------:|---------:|--------------:|")
    running_eq = init_eq
    for y in years:
        s = _stats(cum_pnl_by_y[y])
        ret_pct = s["net"] / running_eq * 100.0
        running_eq += s["net"]
        pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        p(
            f"| {y} | {s['trades']} | {s['wins']} | {s['losses']} | "
            f"{s['wr']:.1f}% | {fmt_money(s['net'])} | "
            f"{ret_pct:+.2f}% | {pf_s} |"
        )
    p("")

    # Monthly table
    p("## Month-by-Month")
    p("")
    p("| Month | Trades | Wins | Losses | Win % | Net P&L | Profit Factor |")
    p("|:------|-------:|-----:|-------:|------:|--------:|--------------:|")
    for ym in months:
        y, m = ym
        s = _stats(cum_pnl_by_ym[ym])
        pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        p(
            f"| {y}-{m:02d} | {s['trades']} | {s['wins']} | {s['losses']} | "
            f"{s['wr']:.1f}% | {fmt_money(s['net'])} | {pf_s} |"
        )
    p("")

    # Profitability frequency
    profitable_years = sum(1 for y in years if sum(cum_pnl_by_y[y]) > 0)
    profitable_months = sum(1 for ym in months if sum(cum_pnl_by_ym[ym]) > 0)
    p("## Consistency")
    p("")
    p(f"- Profitable years:   {profitable_years}/{len(years)}  ({profitable_years / len(years) * 100:.1f}%)")
    p(f"- Profitable months:  {profitable_months}/{len(months)}  ({profitable_months / len(months) * 100:.1f}%)")
    best_m_key = max(months, key=lambda k: sum(cum_pnl_by_ym[k]))
    worst_m_key = min(months, key=lambda k: sum(cum_pnl_by_ym[k]))
    best_m_val = max(sum(cum_pnl_by_ym[k]) for k in months)
    worst_m_val = min(sum(cum_pnl_by_ym[k]) for k in months)
    p(f"- Best month:         {best_m_key} → {fmt_money(best_m_val)}")
    p(f"- Worst month:        {worst_m_key} → {fmt_money(worst_m_val)}")
    best_y_key = max(years, key=lambda y: sum(cum_pnl_by_y[y]))
    worst_y_key = min(years, key=lambda y: sum(cum_pnl_by_y[y]))
    best_y_val = max(sum(cum_pnl_by_y[y]) for y in years)
    worst_y_val = min(sum(cum_pnl_by_y[y]) for y in years)
    p(f"- Best year:          {best_y_key} → {fmt_money(best_y_val)}")
    p(f"- Worst year:         {worst_y_key} → {fmt_money(worst_y_val)}")
    p("")

    # Exit reason mix
    from collections import Counter

    er = Counter(t.exit_reason.name if hasattr(t.exit_reason, "name") else str(t.exit_reason) for t in trades_sorted)
    p("## Exit Reasons")
    p("")
    p("| Reason | Count | % |")
    p("|:-------|------:|--:|")
    for reason, n in er.most_common():
        p(f"| {reason} | {n} | {n / len(trades_sorted) * 100:.1f}% |")
    p("")

    # Side breakdown
    longs = [
        t.pnl_usd
        for t in trades_sorted
        if (t.side.name if hasattr(t.side, "name") else str(t.side)).upper().startswith("LONG")
    ]
    shorts = [
        t.pnl_usd
        for t in trades_sorted
        if (t.side.name if hasattr(t.side, "name") else str(t.side)).upper().startswith("SHORT")
    ]
    sl = _stats(longs)
    ss = _stats(shorts)
    p("## Side Breakdown")
    p("")
    p("| Side | Trades | Win % | Net P&L | PF |")
    p("|:-----|------:|------:|--------:|---:|")
    p(f"| Long  | {sl['trades']} | {sl['wr']:.1f}% | {fmt_money(sl['net'])} | {sl['pf']:.2f} |")
    p(f"| Short | {ss['trades']} | {ss['wr']:.1f}% | {fmt_money(ss['net'])} | {ss['pf']:.2f} |")
    p("")

    report_md = "\n".join(lines)
    print()
    print(report_md)

    OUT_DIR.mkdir(exist_ok=True)
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_md = OUT_DIR / f"backtest_10yr_irb_v5_{ts_tag}.md"
    out_md.write_text(report_md)

    out_csv = OUT_DIR / f"backtest_10yr_irb_v5_{ts_tag}_trades.csv"
    with out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "trade_id",
                "side",
                "entry_ts",
                "exit_ts",
                "entry_price",
                "exit_price",
                "stop_loss",
                "volume",
                "pnl_pips",
                "pnl_usd",
                "risk_r",
                "hold_bars",
                "exit_reason",
            ]
        )
        for t in trades_sorted:
            w.writerow(
                [
                    t.trade_id,
                    t.side.name if hasattr(t.side, "name") else str(t.side),
                    datetime.utcfromtimestamp(t.entry_timestamp).isoformat(),
                    datetime.utcfromtimestamp(t.exit_timestamp).isoformat(),
                    f"{t.entry_price:.5f}",
                    f"{t.exit_price:.5f}",
                    f"{t.stop_loss:.5f}",
                    f"{t.volume:.4f}",
                    f"{t.pnl_pips:.2f}",
                    f"{t.pnl_usd:.2f}",
                    f"{t.risk_r:.3f}",
                    t.hold_bars,
                    t.exit_reason.name if hasattr(t.exit_reason, "name") else str(t.exit_reason),
                ]
            )

    print(f"\n[saved] {out_md}")
    print(f"[saved] {out_csv}")


if __name__ == "__main__":
    main()
