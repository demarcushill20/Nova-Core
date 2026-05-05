"""IRB v5 vault-reference Python backtester (independent of novatrade.backtest).

Source: 100-trading-strategies/Rob Hoffman IRB v5 – Relaxed Reliable Build (Python) 1.md
        in the operator's Obsidian vault.

PURPOSE
    Independent Python implementation of the IRB v5 logic, used to cross-
    validate the in-house `novatrade.backtest.engine.IRBBacktester`. Run this
    against the same 10y EURUSD data the live champion is configured for; if
    headline P&L diverges materially, the gap points to engine drift.

USAGE
    python3 scripts/irb_v5_vault_reference.py [--htf 60min|240min]
"""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO = Path("/home/nova/nova-core")
M5_CSV = REPO / "data/candles/EURUSD_M5_10yr.csv"
OUT_DIR = REPO / "OUTPUT"


# ============================================================
# CONFIG  (verbatim from vault note)
# ============================================================


@dataclass
class IRBConfig:
    initial_capital: float = 100_000.0
    tick_size: float = 0.00001
    enable_longs: bool = True
    enable_shorts: bool = True
    risk_pct: float = 1.0
    qty_step: float = 1.0
    min_qty: float = 1000.0
    max_qty: float = 10_000_000.0
    ema20_len: int = 20
    ema50_len: int = 50
    ema20_slope_lookback: int = 5
    use_htf_filter: bool = True
    htf_timeframe: str = "240min"
    htf_ema_len: int = 20
    require_htf_price_side: bool = False
    irb_percent: float = 45.0
    entry_buffer_ticks: int = 1
    stop_buffer_ticks: int = 1
    signal_expiry_bars: int = 12
    replace_pending_signal: bool = True
    use_body_filter: bool = False
    max_body_percent: float = 45.0
    use_signal_atr_range_filter: bool = True
    signal_atr_len: int = 14
    min_signal_atr_mult: float = 0.0
    max_signal_atr_mult: float = 2.5
    use_pullback_filter: bool = False
    use_session_filter: bool = False
    trade_session: str = "0700-1600"
    max_trades_per_day: int = 3
    cooldown_bars: int = 1
    take_partial: bool = True
    partial_exit_pct: float = 50.0
    partial_rr: float = 1.0
    move_to_be_at_rr: float = 1.0
    runner_atr_len: int = 14
    runner_atr_mult: float = 2.0
    use_time_stop: bool = True
    max_bars_in_trade: int = 20


# ============================================================
# INDICATORS / HELPERS
# ============================================================


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def atr(df: pd.DataFrame, length: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def calc_qty(entry_price: float, stop_price: float, equity: float, cfg: IRBConfig) -> float:
    risk_cash = equity * (cfg.risk_pct / 100.0)
    stop_dist = abs(entry_price - stop_price)
    raw_qty = (risk_cash / stop_dist) if stop_dist > 0 else 0.0
    rounded = floor_to_step(raw_qty, cfg.qty_step)
    if rounded < cfg.min_qty:
        return 0.0
    return min(rounded, cfg.max_qty)


def parse_session(session_str: str) -> tuple[int, int, int, int]:
    start, end = session_str.split("-")
    return int(start[:2]), int(start[2:]), int(end[:2]), int(end[2:])


def in_session(ts: pd.Timestamp, session_str: str) -> bool:
    sh, sm, eh, em = parse_session(session_str)
    t = ts.time()
    return (t.hour, t.minute) >= (sh, sm) and (t.hour, t.minute) <= (eh, em)


def ohlc_path(o: float, h: float, lo: float, c: float) -> list[float]:
    if abs(o - h) < abs(o - lo):
        return [o, h, lo, c]
    return [o, lo, h, c]


def crossed_up(start: float, end: float, level: float) -> bool:
    return start <= level <= end


def crossed_down(start: float, end: float, level: float) -> bool:
    return end <= level <= start


# ============================================================
# DATA PREP
# ============================================================


def prepare_dataframe(raw_df: pd.DataFrame, cfg: IRBConfig) -> pd.DataFrame:
    df = raw_df.copy()
    rename_map = {c: c.lower().strip() for c in df.columns}
    df = df.rename(columns=rename_map)
    if "time" not in df.columns and "timestamp" in df.columns:
        df = df.rename(columns={"timestamp": "time"})

    required = {"time", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values("time").reset_index(drop=True)
    df = df[["time", "open", "high", "low", "close"]].copy()

    df["ema20"] = ema(df["close"], cfg.ema20_len)
    df["ema50"] = ema(df["close"], cfg.ema50_len)
    df["signal_atr"] = atr(df, cfg.signal_atr_len)
    df["runner_atr"] = atr(df, cfg.runner_atr_len)

    htf = (
        df.set_index("time")
        .resample(cfg.htf_timeframe, label="right", closed="right")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
    )
    htf["htf_ema"] = ema(htf["close"], cfg.htf_ema_len)
    htf["htf_ema_prev"] = htf["htf_ema"].shift(1)

    htf_merged = htf[["close", "htf_ema", "htf_ema_prev"]].reset_index().rename(columns={"close": "htf_close"})
    df = pd.merge_asof(
        df.sort_values("time"),
        htf_merged.sort_values("time"),
        on="time",
        direction="backward",
    )

    irb_pct = cfg.irb_percent / 100.0
    max_body_pct = cfg.max_body_percent / 100.0

    df["bull_trend"] = (df["ema20"] > df["ema50"]) & (df["ema20"] > df["ema20"].shift(cfg.ema20_slope_lookback))
    df["bear_trend"] = (df["ema20"] < df["ema50"]) & (df["ema20"] < df["ema20"].shift(cfg.ema20_slope_lookback))

    df["htf_slope_up"] = df["htf_ema"] > df["htf_ema_prev"]
    df["htf_slope_down"] = df["htf_ema"] < df["htf_ema_prev"]
    df["htf_price_long_ok"] = df["htf_close"] > df["htf_ema"]
    df["htf_price_short_ok"] = df["htf_close"] < df["htf_ema"]

    df["htf_long_ok"] = ~cfg.use_htf_filter | (
        df["htf_slope_up"] & (~cfg.require_htf_price_side | df["htf_price_long_ok"])
    )
    df["htf_short_ok"] = ~cfg.use_htf_filter | (
        df["htf_slope_down"] & (~cfg.require_htf_price_side | df["htf_price_short_ok"])
    )

    df["bar_range"] = df["high"] - df["low"]
    df["body_size"] = (df["close"] - df["open"]).abs()
    df["up_threshold"] = df["high"] - (df["bar_range"] * irb_pct)
    df["down_threshold"] = df["low"] + (df["bar_range"] * irb_pct)

    df["bull_irb"] = (df["bar_range"] > 0) & (df["open"] <= df["up_threshold"]) & (df["close"] <= df["up_threshold"])
    df["bear_irb"] = (
        (df["bar_range"] > 0) & (df["open"] >= df["down_threshold"]) & (df["close"] >= df["down_threshold"])
    )

    df["body_ok"] = (df["bar_range"] > 0) & (
        (~cfg.use_body_filter) | (df["body_size"] <= df["bar_range"] * max_body_pct)
    )

    df["signal_range_ok"] = ~cfg.use_signal_atr_range_filter | (
        (df["bar_range"] >= df["signal_atr"] * cfg.min_signal_atr_mult)
        & (df["bar_range"] <= df["signal_atr"] * cfg.max_signal_atr_mult)
    )

    df["pullback_long_ok"] = (~cfg.use_pullback_filter) | (df["low"] <= df["ema20"]) | (df["low"] <= df["ema50"])
    df["pullback_short_ok"] = (~cfg.use_pullback_filter) | (df["high"] >= df["ema20"]) | (df["high"] >= df["ema50"])

    df["long_setup"] = (
        cfg.enable_longs
        & df["bull_trend"]
        & df["htf_long_ok"]
        & df["bull_irb"]
        & df["body_ok"]
        & df["signal_range_ok"]
        & df["pullback_long_ok"]
    )
    df["short_setup"] = (
        cfg.enable_shorts
        & df["bear_trend"]
        & df["htf_short_ok"]
        & df["bear_irb"]
        & df["body_ok"]
        & df["signal_range_ok"]
        & df["pullback_short_ok"]
    )
    return df


# ============================================================
# BACKTEST
# ============================================================


def backtest_irb_strategy(raw_df: pd.DataFrame, cfg: IRBConfig) -> dict[str, Any]:
    df = prepare_dataframe(raw_df, cfg)
    entry_buffer = cfg.tick_size * cfg.entry_buffer_ticks
    stop_buffer = cfg.tick_size * cfg.stop_buffer_ticks

    equity = cfg.initial_capital
    equity_curve: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    position: dict[str, Any] | None = None
    trades_today = 0
    last_flat_bar: int | None = None
    prev_date = None

    for i, row in df.iterrows():
        ts = row["time"]
        bar_date = ts.date()
        if prev_date is None or bar_date != prev_date:
            trades_today = 0
        prev_date = bar_date

        session_ok = (not cfg.use_session_filter) or in_session(ts, cfg.trade_session)
        cooldown_ok = (last_flat_bar is None) or ((i - last_flat_bar) > cfg.cooldown_bars)
        trade_budget_ok = trades_today < cfg.max_trades_per_day
        entry_context_ok = (position is None) and session_ok and cooldown_ok and trade_budget_ok

        # 1) Fill pending entry from prior bars
        if pending is not None and position is None and i > pending["signal_bar"]:
            still_valid = (
                session_ok
                and cooldown_ok
                and trade_budget_ok
                and ((i - pending["signal_bar"]) <= cfg.signal_expiry_bars)
                and (row["bull_trend"] if pending["side"] == "long" else row["bear_trend"])
                and (row["htf_long_ok"] if pending["side"] == "long" else row["htf_short_ok"])
            )
            if not still_valid:
                pending = None
            else:
                path = ohlc_path(row["open"], row["high"], row["low"], row["close"])
                filled = False
                for p0, p1 in pairwise(path):
                    if pending["side"] == "long" and p1 >= p0:
                        if crossed_up(p0, p1, pending["entry_price"]):
                            qty_total = pending["qty"]
                            tp_qty = qty_total * (cfg.partial_exit_pct / 100.0) if cfg.take_partial else 0.0
                            runner_qty = qty_total - tp_qty
                            position = {
                                "side": "long",
                                "entry_time": ts,
                                "entry_bar": i,
                                "entry_price": pending["entry_price"],
                                "qty_total": qty_total,
                                "tp_qty_open": tp_qty,
                                "runner_qty_open": runner_qty,
                                "initial_stop": pending["stop_price"],
                                "highest_since_entry": row["high"],
                                "lowest_since_entry": np.nan,
                            }
                            trades_today += 1
                            trades.append(
                                {
                                    "time": ts,
                                    "type": "entry",
                                    "side": "long",
                                    "price": pending["entry_price"],
                                    "qty": qty_total,
                                    "equity_after": equity,
                                }
                            )
                            pending = None
                            filled = True
                            break
                    elif pending["side"] == "short" and p1 <= p0 and crossed_down(p0, p1, pending["entry_price"]):
                        qty_total = pending["qty"]
                        tp_qty = qty_total * (cfg.partial_exit_pct / 100.0) if cfg.take_partial else 0.0
                        runner_qty = qty_total - tp_qty
                        position = {
                            "side": "short",
                            "entry_time": ts,
                            "entry_bar": i,
                            "entry_price": pending["entry_price"],
                            "qty_total": qty_total,
                            "tp_qty_open": tp_qty,
                            "runner_qty_open": runner_qty,
                            "initial_stop": pending["stop_price"],
                            "highest_since_entry": np.nan,
                            "lowest_since_entry": row["low"],
                        }
                        trades_today += 1
                        trades.append(
                            {
                                "time": ts,
                                "type": "entry",
                                "side": "short",
                                "price": pending["entry_price"],
                                "qty": qty_total,
                                "equity_after": equity,
                            }
                        )
                        pending = None
                        filled = True
                        break
                if filled:
                    equity_curve.append({"time": ts, "equity": equity})
                    continue

        # 2) Manage open position
        if position is not None:
            side = position["side"]
            entry_price = position["entry_price"]
            initial_stop = position["initial_stop"]
            prev_runner_atr = df.iloc[i - 1]["runner_atr"] if i > 0 else row["runner_atr"]

            if side == "long":
                high_ref = position["highest_since_entry"]
                risk_unit = max(entry_price - initial_stop, cfg.tick_size)
                partial_price = entry_price + (risk_unit * cfg.partial_rr)
                be_trigger = entry_price + (risk_unit * cfg.move_to_be_at_rr)
                be_active = high_ref >= be_trigger
                atr_runner_stop = high_ref - (prev_runner_atr * cfg.runner_atr_mult)
                runner_stop = max(entry_price, atr_runner_stop) if be_active else initial_stop
            else:
                low_ref = position["lowest_since_entry"]
                risk_unit = max(initial_stop - entry_price, cfg.tick_size)
                partial_price = entry_price - (risk_unit * cfg.partial_rr)
                be_trigger = entry_price - (risk_unit * cfg.move_to_be_at_rr)
                be_active = low_ref <= be_trigger
                atr_runner_stop = low_ref + (prev_runner_atr * cfg.runner_atr_mult)
                runner_stop = min(entry_price, atr_runner_stop) if be_active else initial_stop

            path = ohlc_path(row["open"], row["high"], row["low"], row["close"])

            def close_tranche(exit_price: float, qty: float, reason: str) -> None:
                nonlocal equity, position, last_flat_bar
                if qty <= 0 or position is None:
                    return
                if position["side"] == "long":
                    pnl = (exit_price - position["entry_price"]) * qty
                else:
                    pnl = (position["entry_price"] - exit_price) * qty
                equity += pnl
                trades.append(
                    {
                        "time": ts,
                        "type": "exit",
                        "side": position["side"],
                        "price": exit_price,
                        "qty": qty,
                        "reason": reason,
                        "pnl": pnl,
                        "equity_after": equity,
                    }
                )

            for p0, p1 in pairwise(path):
                if position is None:
                    break
                if position["side"] == "long":
                    if p1 >= p0:
                        if position["tp_qty_open"] > 0 and crossed_up(p0, p1, partial_price):
                            qty = position["tp_qty_open"]
                            close_tranche(partial_price, qty, "partial_tp")
                            position["tp_qty_open"] = 0.0
                    else:
                        stop_events = []
                        if position["runner_qty_open"] > 0 and crossed_down(p0, p1, runner_stop):
                            stop_events.append(("runner_stop", runner_stop, "runner_qty_open"))
                        if position["tp_qty_open"] > 0 and crossed_down(p0, p1, initial_stop):
                            stop_events.append(("tp_stop", initial_stop, "tp_qty_open"))
                        stop_events.sort(key=lambda x: x[1], reverse=True)
                        for reason, stop_level, qty_field in stop_events:
                            if position is None:
                                break
                            qty = position[qty_field]
                            if qty > 0:
                                close_tranche(stop_level, qty, reason)
                                position[qty_field] = 0.0
                else:
                    if p1 <= p0:
                        if position["tp_qty_open"] > 0 and crossed_down(p0, p1, partial_price):
                            qty = position["tp_qty_open"]
                            close_tranche(partial_price, qty, "partial_tp")
                            position["tp_qty_open"] = 0.0
                    else:
                        stop_events = []
                        if position["runner_qty_open"] > 0 and crossed_up(p0, p1, runner_stop):
                            stop_events.append(("runner_stop", runner_stop, "runner_qty_open"))
                        if position["tp_qty_open"] > 0 and crossed_up(p0, p1, initial_stop):
                            stop_events.append(("tp_stop", initial_stop, "tp_qty_open"))
                        stop_events.sort(key=lambda x: x[1])
                        for reason, stop_level, qty_field in stop_events:
                            if position is None:
                                break
                            qty = position[qty_field]
                            if qty > 0:
                                close_tranche(stop_level, qty, reason)
                                position[qty_field] = 0.0

                if position is not None and position["tp_qty_open"] <= 0 and position["runner_qty_open"] <= 0:
                    last_flat_bar = i
                    position = None
                    break

            if position is not None and cfg.use_time_stop and (i - position["entry_bar"]) >= cfg.max_bars_in_trade:
                remaining_qty = position["tp_qty_open"] + position["runner_qty_open"]
                if remaining_qty > 0:
                    close_tranche(row["close"], remaining_qty, "time_stop")
                last_flat_bar = i
                position = None

            if position is not None:
                if position["side"] == "long":
                    position["highest_since_entry"] = max(position["highest_since_entry"], row["high"])
                else:
                    position["lowest_since_entry"] = min(position["lowest_since_entry"], row["low"])

        # 3) Create / replace pending signal at end of bar
        session_ok = (not cfg.use_session_filter) or in_session(ts, cfg.trade_session)
        cooldown_ok = (last_flat_bar is None) or ((i - last_flat_bar) > cfg.cooldown_bars)
        trade_budget_ok = trades_today < cfg.max_trades_per_day
        entry_context_ok = (position is None) and session_ok and cooldown_ok and trade_budget_ok

        if position is None and entry_context_ok:
            if bool(row["long_setup"]) and (pending is None or cfg.replace_pending_signal):
                entry_price = row["high"] + entry_buffer
                stop_price = row["low"] - stop_buffer
                qty = calc_qty(entry_price, stop_price, equity, cfg)
                pending = {
                    "side": "long",
                    "entry_price": entry_price,
                    "stop_price": stop_price,
                    "qty": qty,
                    "signal_bar": i,
                    "signal_time": ts,
                }
            if bool(row["short_setup"]) and (pending is None or cfg.replace_pending_signal):
                entry_price = row["low"] - entry_buffer
                stop_price = row["high"] + stop_buffer
                qty = calc_qty(entry_price, stop_price, equity, cfg)
                pending = {
                    "side": "short",
                    "entry_price": entry_price,
                    "stop_price": stop_price,
                    "qty": qty,
                    "signal_bar": i,
                    "signal_time": ts,
                }

        equity_curve.append({"time": ts, "equity": equity})

    equity_df = pd.DataFrame(equity_curve)
    trades_df = pd.DataFrame(trades)
    return {
        "data": df,
        "equity_curve": equity_df,
        "trades": trades_df,
        "final_equity": equity,
        "net_profit": equity - cfg.initial_capital,
    }


# ============================================================
# REPORTING
# ============================================================


def fmt_money(x: float) -> str:
    sign = "-" if x < 0 else " "
    return f"{sign}${abs(x):>11,.2f}"


def make_report(results: dict[str, Any], cfg: IRBConfig, window_label: str) -> str:
    init_eq = cfg.initial_capital
    final_eq = results["final_equity"]
    net = results["net_profit"]
    trades_df = results["trades"]

    # round-trip per entry: each entry has 1+ exit rows
    entries = trades_df[trades_df["type"] == "entry"]
    exits = trades_df[trades_df["type"] == "exit"]
    n_entries = len(entries)

    if "pnl" in exits.columns:
        # group exits by entry — can be 1 (single-leg) or 2 (partial+runner)
        # Pair each entry with the subsequent exits up to the next entry
        round_trip_pnl: list[float] = []
        round_trip_year: list[int] = []
        round_trip_side: list[str] = []
        i_exits = 0
        exits_list = exits.to_dict("records")
        entries_list = entries.to_dict("records")
        for k, ent in enumerate(entries_list):
            ent_time = ent["time"]
            next_time = entries_list[k + 1]["time"] if k + 1 < len(entries_list) else None
            pnl_sum = 0.0
            while i_exits < len(exits_list):
                xt = exits_list[i_exits]
                if next_time is not None and xt["time"] >= next_time:
                    break
                pnl_sum += xt["pnl"]
                i_exits += 1
            round_trip_pnl.append(pnl_sum)
            round_trip_year.append(pd.Timestamp(ent_time).year)
            round_trip_side.append(ent["side"])
    else:
        round_trip_pnl, round_trip_year, round_trip_side = [], [], []

    wins = [p for p in round_trip_pnl if p > 0]
    losses = [p for p in round_trip_pnl if p < 0]
    gross_w = sum(wins)
    gross_l = -sum(losses)
    pf = gross_w / gross_l if gross_l > 0 else (float("inf") if gross_w > 0 else 0.0)

    # equity curve drawdown
    eq = results["equity_curve"]["equity"].to_numpy()
    peaks = np.maximum.accumulate(eq)
    dds = peaks - eq
    max_dd = float(dds.max()) if len(dds) else 0.0
    max_dd_pct = (max_dd / peaks[dds.argmax()] * 100.0) if max_dd > 0 else 0.0

    # year buckets
    by_year: dict[int, list[float]] = defaultdict(list)
    for y, pnl_val in zip(round_trip_year, round_trip_pnl, strict=False):
        by_year[y].append(pnl_val)

    lines: list[str] = []
    p = lines.append
    p(f"# IRB v5 Vault Reference — Backtest Report ({window_label})")
    p("")
    p("**Source:** vault Python (`Rob Hoffman IRB v5 – Relaxed Reliable Build (Python) 1.md`)")
    p("**Engine:** independent Python implementation (this script)")
    p(f"**HTF:** `{cfg.htf_timeframe}`  · **Risk:** {cfg.risk_pct}%  · **Max trades/day:** {cfg.max_trades_per_day}")
    p(f"**Initial equity:** ${init_eq:,.2f}")
    p("")
    p("## Headline")
    p("")
    p(f"- Net P&L:                **{fmt_money(net)}**  ({net / init_eq * 100:+.2f}%)")
    p(f"- Final equity:           ${final_eq:,.2f}")
    p(f"- Round-trip trades:      {n_entries:,}")
    p(f"- Wins / Losses:          {len(wins)} / {len(losses)}  (WR {len(wins) / max(1, n_entries) * 100:.2f}%)")
    p(f"- Profit factor:          {pf:.3f}")
    p(f"- Max drawdown:           {fmt_money(-max_dd)}  ({max_dd_pct:.2f}% of peak)")
    p("")

    if by_year:
        p("## Year-by-Year")
        p("")
        p("| Year | Trades | Wins | Losses | Win % | Net P&L | PF |")
        p("|-----:|-------:|-----:|-------:|------:|--------:|---:|")
        for y in sorted(by_year):
            ps = by_year[y]
            yw = [v for v in ps if v > 0]
            yl = [v for v in ps if v < 0]
            ypf = (sum(yw) / -sum(yl)) if yl else (float("inf") if yw else 0.0)
            wr = len(yw) / max(1, len(ps)) * 100.0
            ypf_s = "∞" if ypf == float("inf") else f"{ypf:.2f}"
            p(f"| {y} | {len(ps)} | {len(yw)} | {len(yl)} | {wr:.1f}% | {fmt_money(sum(ps))} | {ypf_s} |")
        p("")

    # Side breakdown
    longs = [p_ for p_, s in zip(round_trip_pnl, round_trip_side, strict=False) if s == "long"]
    shorts = [p_ for p_, s in zip(round_trip_pnl, round_trip_side, strict=False) if s == "short"]

    def _stats(arr: list[float]) -> tuple[int, float, float]:
        if not arr:
            return 0, 0.0, 0.0
        w = sum(x for x in arr if x > 0)
        lo = -sum(x for x in arr if x < 0)
        return len(arr), sum(arr), (w / lo if lo > 0 else (float("inf") if w else 0.0))

    nL, npL, pfL = _stats(longs)
    nS, npS, pfS = _stats(shorts)
    p("## Side Breakdown")
    p("")
    p("| Side | Trades | Net | PF |")
    p("|:-----|------:|--------:|---:|")
    p(f"| Long  | {nL} | {fmt_money(npL)} | {pfL:.2f} |")
    p(f"| Short | {nS} | {fmt_money(npS)} | {pfS:.2f} |")
    p("")

    # Exit reason mix
    if "reason" in exits.columns and len(exits) > 0:
        reason_counts = exits["reason"].value_counts()
        p("## Exit reason mix")
        p("")
        for r, n in reason_counts.items():
            p(f"- {r}: {n}  ({n / len(exits) * 100:.1f}%)")
        p("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--htf", default="60min", help="HTF timeframe ('60min' for H1, '240min' for H4)")
    parser.add_argument("--max-trades", type=int, default=3)
    parser.add_argument("--csv", default=str(M5_CSV))
    args = parser.parse_args()

    print(f"[load] {args.csv}")
    raw = pd.read_csv(args.csv)
    print(f"        {len(raw):,} bars  ({raw.iloc[0, 0]} → {raw.iloc[-1, 0]})")

    cfg = IRBConfig(htf_timeframe=args.htf, max_trades_per_day=args.max_trades)
    print(
        f"[run] vault reference (htf={args.htf}, max_trades/day={args.max_trades}, "
        f"partial 50% @ 1R, BE @ 1R, ATR trail {cfg.runner_atr_mult}, time-stop {cfg.max_bars_in_trade})"
    )
    results = backtest_irb_strategy(raw, cfg)

    win_lo = pd.to_datetime(raw.iloc[0, 0]).date()
    win_hi = pd.to_datetime(raw.iloc[-1, 0]).date()
    report = make_report(results, cfg, f"{win_lo} → {win_hi}")
    print()
    print(report)

    OUT_DIR.mkdir(exist_ok=True)
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_md = OUT_DIR / f"backtest_vault_irb_v5_{ts_tag}.md"
    out_md.write_text(report)
    print(f"\n[saved] {out_md}")

    out_csv = OUT_DIR / f"backtest_vault_irb_v5_{ts_tag}_trades.csv"
    results["trades"].to_csv(out_csv, index=False)
    print(f"[saved] {out_csv}")


if __name__ == "__main__":
    main()
