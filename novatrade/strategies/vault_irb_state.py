"""Vault IRB v5 — stateful per-bar strategy core.

Refactor of the vault Python reference (`100-trading-strategies/Rob Hoffman
IRB v5 - Relaxed Reliable Build (Python) 1.md`, runnable at
`scripts/irb_v5_vault_reference.py`) into a stateful class that processes
ONE BAR AT A TIME.

This is the strategy-logic core for both:
  - Batch backtests (`run_vault_strategy_batch`) — produces the canonical
    +$45,310 / 11/11 yrs / PF 1.081 result on 10y EURUSD M5 matched-risk.
  - Live streaming (`novatrade.strategy.vault_engine.VaultLiveEngine`) — the
    Stage 3a-native wrapper that drives this class one bar at a time.

The class state machine is identical to the vault Python reference loop
(commit history of changes is in the wrapper PR, not here). DO NOT change
the per-bar logic without re-running the regression: vault Python script
must continue to produce final_equity=$145,309.55.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import numpy as np
import pandas as pd

# ============================================================
# CONFIG (verbatim from vault note)
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
# HELPERS (verbatim from vault note)
# ============================================================


def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()


def atr(df: pd.DataFrame, length: int) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
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
# DATA PREP (verbatim from vault note)
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
# STATEFUL STRATEGY CORE
# ============================================================


@dataclass
class TradeEvent:
    """Per-bar event emitted by IRBStrategyState. Mirrors vault's trade ledger row.

    `type`: "entry", "exit"
    `side`: "long", "short"
    `reason` (exit only): "partial_tp", "tp_stop", "runner_stop", "time_stop"
    """

    time: Any
    type: str
    side: str
    price: float
    qty: float
    equity_after: float
    reason: str | None = None
    pnl: float | None = None


class IRBStrategyState:
    """Stateful IRB v5 implementation. Process one bar at a time.

    Used by both the batch entry point (`run_vault_strategy_batch`) and the
    live wrapper (`novatrade.strategy.vault_engine.VaultLiveEngine`). Logic
    is byte-identical to the vault Python reference loop — verified by the
    regression test in `tests/test_vault_irb_state_parity.py`.
    """

    def __init__(self, cfg: IRBConfig) -> None:
        self.cfg = cfg
        self.entry_buffer = cfg.tick_size * cfg.entry_buffer_ticks
        self.stop_buffer = cfg.tick_size * cfg.stop_buffer_ticks

        self.equity = cfg.initial_capital
        self.pending: dict[str, Any] | None = None
        self.position: dict[str, Any] | None = None
        self.trades_today = 0
        self.last_flat_bar: int | None = None
        self.prev_date = None

        # Accumulators (ledgers + curve), grown on each process_bar call
        self.trades: list[TradeEvent] = []
        self.equity_curve: list[dict[str, Any]] = []

    def _emit_entry(self, ts: Any, side: str, price: float, qty: float) -> None:
        ev = TradeEvent(
            time=ts,
            type="entry",
            side=side,
            price=price,
            qty=qty,
            equity_after=self.equity,
        )
        self.trades.append(ev)

    def _close_tranche(self, ts: Any, exit_price: float, qty: float, reason: str) -> None:
        if qty <= 0 or self.position is None:
            return
        if self.position["side"] == "long":
            pnl = (exit_price - self.position["entry_price"]) * qty
        else:
            pnl = (self.position["entry_price"] - exit_price) * qty
        self.equity += pnl
        ev = TradeEvent(
            time=ts,
            type="exit",
            side=self.position["side"],
            price=exit_price,
            qty=qty,
            equity_after=self.equity,
            reason=reason,
            pnl=pnl,
        )
        self.trades.append(ev)

    def process_bar(self, i: int, row: pd.Series, df: pd.DataFrame) -> list[TradeEvent]:
        """Run one bar of vault's per-bar logic. Returns the list of events
        emitted on THIS bar (subset of self.trades).

        Mirrors `backtest_irb_strategy()`'s for-loop body verbatim; logic
        unchanged from the vault Python reference. The only structural
        change is the closure-style `close_tranche` is now a method, and
        the `equity` / `pending` / `position` / `trades_today` /
        `last_flat_bar` / `prev_date` variables are instance attributes.
        """
        cfg = self.cfg
        ts = row["time"]
        bar_date = ts.date()

        events_before = len(self.trades)

        # Day boundary
        if self.prev_date is None or bar_date != self.prev_date:
            self.trades_today = 0
        self.prev_date = bar_date

        session_ok = (not cfg.use_session_filter) or in_session(ts, cfg.trade_session)
        cooldown_ok = (self.last_flat_bar is None) or ((i - self.last_flat_bar) > cfg.cooldown_bars)
        trade_budget_ok = self.trades_today < cfg.max_trades_per_day

        # 1) Fill pending entry from prior bars
        if self.pending is not None and self.position is None and i > self.pending["signal_bar"]:
            still_valid = (
                session_ok
                and cooldown_ok
                and trade_budget_ok
                and ((i - self.pending["signal_bar"]) <= cfg.signal_expiry_bars)
                and (row["bull_trend"] if self.pending["side"] == "long" else row["bear_trend"])
                and (row["htf_long_ok"] if self.pending["side"] == "long" else row["htf_short_ok"])
            )
            if not still_valid:
                self.pending = None
            else:
                path = ohlc_path(row["open"], row["high"], row["low"], row["close"])
                filled = False
                for p0, p1 in pairwise(path):
                    if self.pending["side"] == "long" and p1 >= p0:
                        if crossed_up(p0, p1, self.pending["entry_price"]):
                            qty_total = self.pending["qty"]
                            tp_qty = qty_total * (cfg.partial_exit_pct / 100.0) if cfg.take_partial else 0.0
                            runner_qty = qty_total - tp_qty
                            self.position = {
                                "side": "long",
                                "entry_time": ts,
                                "entry_bar": i,
                                "entry_price": self.pending["entry_price"],
                                "qty_total": qty_total,
                                "tp_qty_open": tp_qty,
                                "runner_qty_open": runner_qty,
                                "initial_stop": self.pending["stop_price"],
                                "highest_since_entry": row["high"],
                                "lowest_since_entry": np.nan,
                            }
                            self.trades_today += 1
                            self._emit_entry(ts, "long", self.pending["entry_price"], qty_total)
                            self.pending = None
                            filled = True
                            break
                    elif (
                        self.pending["side"] == "short"
                        and p1 <= p0
                        and crossed_down(p0, p1, self.pending["entry_price"])
                    ):
                        qty_total = self.pending["qty"]
                        tp_qty = qty_total * (cfg.partial_exit_pct / 100.0) if cfg.take_partial else 0.0
                        runner_qty = qty_total - tp_qty
                        self.position = {
                            "side": "short",
                            "entry_time": ts,
                            "entry_bar": i,
                            "entry_price": self.pending["entry_price"],
                            "qty_total": qty_total,
                            "tp_qty_open": tp_qty,
                            "runner_qty_open": runner_qty,
                            "initial_stop": self.pending["stop_price"],
                            "highest_since_entry": np.nan,
                            "lowest_since_entry": row["low"],
                        }
                        self.trades_today += 1
                        self._emit_entry(ts, "short", self.pending["entry_price"], qty_total)
                        self.pending = None
                        filled = True
                        break
                if filled:
                    self.equity_curve.append({"time": ts, "equity": self.equity})
                    return self.trades[events_before:]

        # 2) Manage open position
        if self.position is not None:
            side = self.position["side"]
            entry_price = self.position["entry_price"]
            initial_stop = self.position["initial_stop"]
            prev_runner_atr = df.iloc[i - 1]["runner_atr"] if i > 0 else row["runner_atr"]

            if side == "long":
                high_ref = self.position["highest_since_entry"]
                risk_unit = max(entry_price - initial_stop, cfg.tick_size)
                partial_price = entry_price + (risk_unit * cfg.partial_rr)
                be_trigger = entry_price + (risk_unit * cfg.move_to_be_at_rr)
                be_active = high_ref >= be_trigger
                atr_runner_stop = high_ref - (prev_runner_atr * cfg.runner_atr_mult)
                runner_stop = max(entry_price, atr_runner_stop) if be_active else initial_stop
            else:
                low_ref = self.position["lowest_since_entry"]
                risk_unit = max(initial_stop - entry_price, cfg.tick_size)
                partial_price = entry_price - (risk_unit * cfg.partial_rr)
                be_trigger = entry_price - (risk_unit * cfg.move_to_be_at_rr)
                be_active = low_ref <= be_trigger
                atr_runner_stop = low_ref + (prev_runner_atr * cfg.runner_atr_mult)
                runner_stop = min(entry_price, atr_runner_stop) if be_active else initial_stop

            self.position["runner_stop"] = runner_stop

            path = ohlc_path(row["open"], row["high"], row["low"], row["close"])

            for p0, p1 in pairwise(path):
                if self.position is None:
                    break
                if self.position["side"] == "long":
                    if p1 >= p0:
                        if self.position["tp_qty_open"] > 0 and crossed_up(p0, p1, partial_price):
                            qty = self.position["tp_qty_open"]
                            self._close_tranche(ts, partial_price, qty, "partial_tp")
                            self.position["tp_qty_open"] = 0.0
                    else:
                        stop_events: list[tuple[str, float, str]] = []
                        if self.position["runner_qty_open"] > 0 and crossed_down(p0, p1, runner_stop):
                            stop_events.append(("runner_stop", runner_stop, "runner_qty_open"))
                        if self.position["tp_qty_open"] > 0 and crossed_down(p0, p1, initial_stop):
                            stop_events.append(("tp_stop", initial_stop, "tp_qty_open"))
                        stop_events.sort(key=lambda x: x[1], reverse=True)
                        for reason, stop_level, qty_field in stop_events:
                            if self.position is None:
                                break
                            qty = self.position[qty_field]
                            if qty > 0:
                                self._close_tranche(ts, stop_level, qty, reason)
                                self.position[qty_field] = 0.0
                else:
                    if p1 <= p0:
                        if self.position["tp_qty_open"] > 0 and crossed_down(p0, p1, partial_price):
                            qty = self.position["tp_qty_open"]
                            self._close_tranche(ts, partial_price, qty, "partial_tp")
                            self.position["tp_qty_open"] = 0.0
                    else:
                        stop_events = []
                        if self.position["runner_qty_open"] > 0 and crossed_up(p0, p1, runner_stop):
                            stop_events.append(("runner_stop", runner_stop, "runner_qty_open"))
                        if self.position["tp_qty_open"] > 0 and crossed_up(p0, p1, initial_stop):
                            stop_events.append(("tp_stop", initial_stop, "tp_qty_open"))
                        stop_events.sort(key=lambda x: x[1])
                        for reason, stop_level, qty_field in stop_events:
                            if self.position is None:
                                break
                            qty = self.position[qty_field]
                            if qty > 0:
                                self._close_tranche(ts, stop_level, qty, reason)
                                self.position[qty_field] = 0.0

                if (
                    self.position is not None
                    and self.position["tp_qty_open"] <= 0
                    and self.position["runner_qty_open"] <= 0
                ):
                    self.last_flat_bar = i
                    self.position = None
                    break

            if (
                self.position is not None
                and cfg.use_time_stop
                and (i - self.position["entry_bar"]) >= cfg.max_bars_in_trade
            ):
                remaining_qty = self.position["tp_qty_open"] + self.position["runner_qty_open"]
                if remaining_qty > 0:
                    self._close_tranche(ts, row["close"], remaining_qty, "time_stop")
                self.last_flat_bar = i
                self.position = None

            if self.position is not None:
                if self.position["side"] == "long":
                    self.position["highest_since_entry"] = max(self.position["highest_since_entry"], row["high"])
                else:
                    self.position["lowest_since_entry"] = min(self.position["lowest_since_entry"], row["low"])

        # 3) Create / replace pending signal at end of bar
        session_ok = (not cfg.use_session_filter) or in_session(ts, cfg.trade_session)
        cooldown_ok = (self.last_flat_bar is None) or ((i - self.last_flat_bar) > cfg.cooldown_bars)
        trade_budget_ok = self.trades_today < cfg.max_trades_per_day
        entry_context_ok = (self.position is None) and session_ok and cooldown_ok and trade_budget_ok

        if self.position is None and entry_context_ok:
            if bool(row["long_setup"]) and (self.pending is None or cfg.replace_pending_signal):
                entry_price = row["high"] + self.entry_buffer
                stop_price = row["low"] - self.stop_buffer
                qty = calc_qty(entry_price, stop_price, self.equity, cfg)
                self.pending = {
                    "side": "long",
                    "entry_price": entry_price,
                    "stop_price": stop_price,
                    "qty": qty,
                    "signal_bar": i,
                    "signal_time": ts,
                }
            if bool(row["short_setup"]) and (self.pending is None or cfg.replace_pending_signal):
                entry_price = row["low"] - self.entry_buffer
                stop_price = row["high"] + self.stop_buffer
                qty = calc_qty(entry_price, stop_price, self.equity, cfg)
                self.pending = {
                    "side": "short",
                    "entry_price": entry_price,
                    "stop_price": stop_price,
                    "qty": qty,
                    "signal_bar": i,
                    "signal_time": ts,
                }

        self.equity_curve.append({"time": ts, "equity": self.equity})
        return self.trades[events_before:]


# ============================================================
# BATCH ENTRY POINT
# ============================================================


def run_vault_strategy_batch(raw_df: pd.DataFrame, cfg: IRBConfig) -> dict[str, Any]:
    """Run the full vault strategy as a batch backtest. Drives IRBStrategyState
    bar-by-bar over a pre-computed DataFrame. Output schema matches the original
    `backtest_irb_strategy` function for drop-in compatibility with the script.
    """
    df = prepare_dataframe(raw_df, cfg)
    state = IRBStrategyState(cfg)

    for i, row in df.iterrows():
        state.process_bar(i, row, df)

    # Convert TradeEvent list to DataFrame matching the original schema
    trade_records: list[dict[str, Any]] = []
    for ev in state.trades:
        rec = {
            "time": ev.time,
            "type": ev.type,
            "side": ev.side,
            "price": ev.price,
            "qty": ev.qty,
            "equity_after": ev.equity_after,
        }
        if ev.reason is not None:
            rec["reason"] = ev.reason
        if ev.pnl is not None:
            rec["pnl"] = ev.pnl
        trade_records.append(rec)
    trades_df = pd.DataFrame(trade_records)

    equity_df = pd.DataFrame(state.equity_curve)
    return {
        "data": df,
        "equity_curve": equity_df,
        "trades": trades_df,
        "final_equity": state.equity,
        "net_profit": state.equity - cfg.initial_capital,
    }
