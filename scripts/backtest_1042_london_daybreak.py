#!/usr/bin/env python3
"""Backtest task-1042: London Day Break / London Daybreak Strategy (MTI / Team Freedom).

Spec (from TASKS/1042...):
  - Primary chart: 1-hour candles, time zone America/New_York (NOT broker time).
  - Setup candle: the 3:00 AM ET 1-hour candle. Decision at its close (= 4:00 AM ET open).
  - Use the previous 5 candles to decide a Buy-Low vs Sell-High scenario.
  - Place a pending STOP order 10 pips beyond the 3:00 AM candle (high for buy, low for sell).
  - Stop-loss at the OPPOSITE wick of the 3:00 AM candle.
  - Target: minimum 50 pips, or 1:1 if the stop distance is larger than 50 pips
      => target_pips = max(50, stop_distance_pips).
  - Cancel any untriggered pending order at 12:00 PM ET.

This is a time-of-day session breakout, so it does NOT fit the generic bar-by-bar
StrategyBacktesterAdapter (which has no notion of pending orders / session windows).
We implement a faithful, self-contained event simulator with:
  - explicit UTC -> America/New_York conversion (DST-aware via zoneinfo)
  - 1-bar pending-order fill logic (stop order fills at the level when the bar trades through it)
  - conservative intrabar resolution: if a single bar spans both SL and TP, count SL (loss)
  - cancel-at-noon-ET for untriggered orders
  - once filled, hold to SL or TP (optional --max-hold-bars hard cap)

We run GROSS (zero cost) and NET (ECN cost in pips) to separate raw entry edge from
cost drag, matching the house harness convention used in tasks 1024/1028/1030.

The 5-candle bias rule is genuinely under-specified in the source ("use the previous
five candles to decide a Buy Low or Sell High scenario"). We test the most
defensible readings and report each:
  - bias=candle         : the canonical MTI Daybreak reading (task-1043). The 3AM
      setup candle's body decides direction -- a BULL candle (close > open) arms a
      buy-stop, a BEAR candle (close < open) arms a sell-stop. A DOJI (|close-open|
      <= doji_tolerance_pips) is NEUTRAL -> no trade (count_doji_as_neutral).
  - bias=close-position : Buy-Low if the 3AM close sits in the lower half of the
      prior-5-candle range, else Sell-High (mean-reversion-to-range reading of
      "Buy Low / Sell High").
  - bias=trend          : Buy if 3AM close > prior-5 average close, else Sell
      (momentum reading).
  - bias=straddle       : take BOTH directions (OCO breakout straddle); the first
      side to trigger wins, the other is cancelled.

Task-1044 config knobs (defaults preserve the task-1042 baseline so prior runs
reproduce exactly): allow_long / allow_short side gating, max_trades_per_day,
min_risk_pips / max_risk_pips stop-distance filter, doji_tolerance_pips, and a
split spread / commission / slippage cost model (summed into round-turn pips;
falls back to --cost-pips when none are supplied).
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
PIP = 0.0001  # EURUSD


@dataclass
class Bar:
    ts_utc: datetime
    ts_ny: datetime
    o: float
    h: float
    lo: float
    c: float


def load_bars(path: Path, data_tz: str) -> list[Bar]:
    src_tz = timezone.utc if data_tz.upper() == "UTC" else ZoneInfo(data_tz)
    bars: list[Bar] = []
    with path.open() as f:
        for r in csv.DictReader(f):
            ts = datetime.fromisoformat(r["timestamp"]).replace(tzinfo=src_tz)
            ts_utc = ts.astimezone(timezone.utc)
            bars.append(
                Bar(
                    ts_utc=ts_utc,
                    ts_ny=ts_utc.astimezone(NY),
                    o=float(r["open"]),
                    h=float(r["high"]),
                    lo=float(r["low"]),
                    c=float(r["close"]),
                )
            )
    bars.sort(key=lambda b: b.ts_utc)
    return bars


@dataclass
class Trade:
    day: str
    side: str  # "buy" / "sell"
    entry: float
    stop: float
    target: float
    stop_pips: float
    target_pips: float
    rr: float
    outcome: str  # "tp" / "sl"
    r_gross: float
    r_net: float


def _resolve(
    side: str,
    entry: float,
    stop: float,
    target: float,
    bars: list[Bar],
    start_i: int,
    cost_pips: float,
    stop_pips: float,
    target_pips: float,
    max_hold: int,
) -> tuple[str, float, float]:
    """Walk bars from start_i (the trigger bar) to SL/TP. Conservative: SL wins ties."""
    rr_gross_win = target_pips / stop_pips
    end = len(bars) if max_hold <= 0 else min(len(bars), start_i + max_hold)
    for j in range(start_i, end):
        b = bars[j]
        if side == "buy":
            hit_sl = b.lo <= stop
            hit_tp = b.h >= target
        else:
            hit_sl = b.h >= stop
            hit_tp = b.lo <= target
        if hit_sl and hit_tp:
            # conservative: assume stop first
            return "sl", -1.0, -(stop_pips + cost_pips) / stop_pips
        if hit_sl:
            return "sl", -1.0, -(stop_pips + cost_pips) / stop_pips
        if hit_tp:
            return "tp", rr_gross_win, (target_pips - cost_pips) / stop_pips
    # unresolved by max_hold / end of data -> close flat (ignore, but count as scratch)
    return "open", 0.0, 0.0


def simulate(  # noqa: C901
    bars: list[Bar],
    *,
    bias: str,
    buffer_pips: float,
    min_target_pips: float,
    cost_pips: float,
    max_hold_bars: int,
    cancel_hour_et: int = 12,
    allow_long: bool = True,
    allow_short: bool = True,
    max_trades_per_day: int = 1,
    min_risk_pips: float = 0.0,
    max_risk_pips: float = 0.0,
    doji_tolerance_pips: float = 0.0,
    count_doji_as_neutral: bool = True,
) -> list[Trade]:
    # index bars by NY calendar day for setup lookup
    trades: list[Trade] = []
    per_day: dict[str, int] = {}
    n = len(bars)
    for i in range(5, n):
        b = bars[i]
        if b.ts_ny.hour != 3:  # 3:00 AM ET setup candle
            continue
        prev5 = bars[i - 5 : i]
        if len(prev5) < 5:
            continue
        day = b.ts_ny.strftime("%Y-%m-%d")
        if max_trades_per_day > 0 and per_day.get(day, 0) >= max_trades_per_day:
            continue

        # ---- direction decision ----
        sides: list[str] = []
        if bias == "candle":  # canonical MTI Daybreak: setup-candle body decides
            body_pips = (b.c - b.o) / PIP
            if count_doji_as_neutral and abs(body_pips) <= doji_tolerance_pips:
                continue  # doji -> neutral -> no trade
            sides = ["buy"] if body_pips > 0 else ["sell"]
        elif bias == "straddle":
            sides = ["buy", "sell"]
        elif bias == "trend":
            avg_c = sum(p.c for p in prev5) / 5
            sides = ["buy"] if b.c > avg_c else ["sell"]
        else:  # close-position (default) -- "Buy Low / Sell High"
            rng_hi = max(p.h for p in prev5)
            rng_lo = min(p.lo for p in prev5)
            mid = (rng_hi + rng_lo) / 2
            sides = ["buy"] if b.c <= mid else ["sell"]

        # ---- side gating (allow_long / allow_short) ----
        sides = [s for s in sides if (s == "buy" and allow_long) or (s == "sell" and allow_short)]
        if not sides:
            continue

        # ---- build pending orders for chosen side(s) ----
        orders = []
        for side in sides:
            if side == "buy":
                entry = b.h + buffer_pips * PIP
                stop = b.lo  # opposite wick
            else:
                entry = b.lo - buffer_pips * PIP
                stop = b.h
            stop_pips = abs(entry - stop) / PIP
            # risk filter: skip setups whose stop distance is out of bounds
            if min_risk_pips > 0 and stop_pips < min_risk_pips:
                continue
            if max_risk_pips > 0 and stop_pips > max_risk_pips:
                continue
            target_pips = max(min_target_pips, stop_pips)  # min 50p, else 1:1
            if side == "buy":
                target = entry + target_pips * PIP
            else:
                target = entry - target_pips * PIP
            orders.append((side, entry, stop, target, stop_pips, target_pips))

        # ---- scan from 4AM ET up to (but not including) cancel_hour_et ET for a trigger ----
        triggered = None
        for j in range(i + 1, n):
            bj = bars[j]
            # same NY day, before noon ET
            if bj.ts_ny.strftime("%Y-%m-%d") != day:
                break
            if bj.ts_ny.hour >= cancel_hour_et:
                break
            for side, entry, stop, target, stop_pips, target_pips in orders:
                if side == "buy" and bj.h >= entry:
                    triggered = (j, side, entry, stop, target, stop_pips, target_pips)
                    break
                if side == "sell" and bj.lo <= entry:
                    triggered = (j, side, entry, stop, target, stop_pips, target_pips)
                    break
            if triggered:
                break

        if not orders or not triggered:
            continue

        j, side, entry, stop, target, stop_pips, target_pips = triggered
        outcome, r_gross, r_net = _resolve(
            side, entry, stop, target, bars, j, cost_pips, stop_pips, target_pips, max_hold_bars
        )
        if outcome == "open":
            continue
        per_day[day] = per_day.get(day, 0) + 1
        trades.append(
            Trade(
                day=day,
                side=side,
                entry=entry,
                stop=stop,
                target=target,
                stop_pips=stop_pips,
                target_pips=target_pips,
                rr=target_pips / stop_pips,
                outcome=outcome,
                r_gross=r_gross,
                r_net=r_net,
            )
        )
    return trades


def summarize(trades: list[Trade], use_net: bool, risk_frac: float, init_eq: float) -> dict:
    if not trades:
        return {"n": 0}
    rs = [t.r_net if use_net else t.r_gross for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gp = sum(wins)
    gl = -sum(losses)
    pf = (gp / gl) if gl > 0 else float("inf")
    eq = init_eq
    peak = eq
    maxdd = 0.0
    for t in trades:
        r = t.r_net if use_net else t.r_gross
        eq += eq * risk_frac * r
        peak = max(peak, eq)
        if peak > 0:
            maxdd = max(maxdd, (peak - eq) / peak)
    n_win = sum(1 for r in rs if r > 0)
    return {
        "n": len(trades),
        "win_rate": n_win / len(trades),
        "pf": pf,
        "expectancy_r": sum(rs) / len(rs),
        "total_r": sum(rs),
        "final_eq": eq,
        "return_pct": (eq / init_eq - 1) * 100,
        "maxdd_pct": maxdd * 100,
        "avg_stop_pips": sum(t.stop_pips for t in trades) / len(trades),
        "avg_rr": sum(t.rr for t in trades) / len(trades),
        "n_buy": sum(1 for t in trades if t.side == "buy"),
        "n_sell": sum(1 for t in trades if t.side == "sell"),
    }


def _print(tag: str, m: dict) -> None:
    print(f"\n=== TASK-1042  London Daybreak ({tag}) ===")
    if m.get("n", 0) == 0:
        print("No trades generated.")
        return
    print(f"trades         : {m['n']:,}   (buy {m['n_buy']} / sell {m['n_sell']})")
    print(f"win rate       : {m['win_rate'] * 100:.1f}%")
    print(f"profit factor  : {m['pf']:.3f}")
    print(f"expectancy R   : {m['expectancy_r']:+.4f}")
    print(f"total R        : {m['total_r']:+.2f}")
    print(f"avg stop / RR  : {m['avg_stop_pips']:.1f} pips  /  {m['avg_rr']:.2f}:1")
    print(f"final equity   : ${m['final_eq']:,.0f}  ({m['return_pct']:+.1f}%)")
    print(f"max drawdown   : {m['maxdd_pct']:.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--data-tz", default="UTC", help="source timezone of CSV timestamps (default UTC)")
    ap.add_argument(
        "--bias",
        default="close-position",
        choices=["candle", "close-position", "trend", "straddle"],
        help="direction rule; 'candle' = canonical MTI Daybreak (setup-candle body)",
    )
    ap.add_argument("--buffer-pips", type=float, default=10.0)
    ap.add_argument("--min-target-pips", type=float, default=50.0)
    ap.add_argument(
        "--cost-pips", type=float, default=1.5, help="round-turn ECN cost in pips (fallback when no split cost given)"
    )
    ap.add_argument("--spread", type=float, default=0.0, help="spread in pips (split cost model)")
    ap.add_argument("--commission", type=float, default=0.0, help="commission in pips round-turn")
    ap.add_argument("--slippage", type=float, default=0.0, help="slippage in pips round-turn")
    ap.add_argument("--max-hold-bars", type=int, default=0, help="0 = hold to SL/TP, else hard cap (H1 bars)")
    ap.add_argument("--allow-long", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--allow-short", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--max-trades-per-day", type=int, default=1, help="0 = unlimited")
    ap.add_argument("--min-risk-pips", type=float, default=0.0, help="0 = no floor on stop distance")
    ap.add_argument("--max-risk-pips", type=float, default=0.0, help="0 = no cap on stop distance")
    ap.add_argument(
        "--doji-tolerance-pips", type=float, default=0.0, help="|close-open| <= this => doji/neutral (candle bias only)"
    )
    ap.add_argument("--count-doji-as-neutral", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--risk", type=float, default=0.0033)
    ap.add_argument("--initial-equity", type=float, default=100_000.0)
    args = ap.parse_args()

    # split cost model: sum spread+commission+slippage if any supplied, else --cost-pips
    split = args.spread + args.commission + args.slippage
    eff_cost = split if split > 0 else args.cost_pips

    bars = load_bars(args.data, args.data_tz)
    print(f"Loaded {len(bars):,} H1 bars  {bars[0].ts_ny:%Y-%m-%d} -> {bars[-1].ts_ny:%Y-%m-%d} (NY)")
    print(
        f"bias={args.bias}  buffer={args.buffer_pips}p  min_target={args.min_target_pips}p  "
        f"cost={eff_cost}p  max_hold={args.max_hold_bars or 'none'}"
    )
    print(
        f"long={args.allow_long}  short={args.allow_short}  max/day={args.max_trades_per_day}  "
        f"risk_pips=[{args.min_risk_pips or '-'},{args.max_risk_pips or '-'}]  "
        f"doji_tol={args.doji_tolerance_pips}p"
    )

    trades = simulate(
        bars,
        bias=args.bias,
        buffer_pips=args.buffer_pips,
        min_target_pips=args.min_target_pips,
        cost_pips=eff_cost,
        max_hold_bars=args.max_hold_bars,
        allow_long=args.allow_long,
        allow_short=args.allow_short,
        max_trades_per_day=args.max_trades_per_day,
        min_risk_pips=args.min_risk_pips,
        max_risk_pips=args.max_risk_pips,
        doji_tolerance_pips=args.doji_tolerance_pips,
        count_doji_as_neutral=args.count_doji_as_neutral,
    )
    gross = summarize(trades, use_net=False, risk_frac=args.risk, init_eq=args.initial_equity)
    net = summarize(trades, use_net=True, risk_frac=args.risk, init_eq=args.initial_equity)
    _print("GROSS (zero cost)", gross)
    _print("NET of ECN cost", net)


if __name__ == "__main__":
    main()
