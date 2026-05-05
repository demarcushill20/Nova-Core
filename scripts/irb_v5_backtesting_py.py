"""IRB v5 implemented in kernc/backtesting.py — independent cross-engine.

Mirrors `100-trading-strategies/Rob Hoffman IRB v5 – Relaxed Reliable Build
(Python) 1.md` step for step. backtesting.py manages stop-order fills and
exit triggering; we manage signal-expiry, partial close, BE-activation,
and ATR-trail in `next()`.

Usage:
    python3 scripts/irb_v5_backtesting_py.py [--htf 60min|240min] [--risk 0.33]
"""

from __future__ import annotations

import argparse
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from backtesting import Backtest, Strategy

REPO = Path("/home/nova/nova-core")
M5_CSV = REPO / "data/candles/EURUSD_M5_10yr.csv"
OUT_DIR = REPO / "OUTPUT"


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def prepare_data(csv_path: Path, htf: str = "60min") -> pd.DataFrame:
    raw = pd.read_csv(csv_path)
    raw["timestamp"] = pd.to_datetime(raw["timestamp"])
    df = raw.rename(columns={"timestamp": "time"}).set_index("time").sort_index()

    df["ema20"] = ema(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["atr14"] = atr(df, 14)

    htf_df = (
        df[["open", "high", "low", "close"]]
        .resample(htf, label="right", closed="right")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
    )
    htf_df["htf_ema"] = ema(htf_df["close"], 20)
    htf_df["htf_ema_prev"] = htf_df["htf_ema"].shift(1)

    df = df.reset_index()
    htf_merged = htf_df[["htf_ema", "htf_ema_prev"]].reset_index()
    df = pd.merge_asof(df.sort_values("time"), htf_merged.sort_values("time"), on="time", direction="backward")
    df = df.set_index("time")

    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    return df


class IRBv5BTPY(Strategy):
    irb_pct = 0.45
    slope_lb = 5
    risk_pct = 0.33
    partial_pct = 50
    partial_rr = 1.0
    be_at_rr = 1.0
    runner_atr_mult = 2.0
    max_signal_atr_mult = 2.5
    time_stop = 20
    cooldown = 1
    signal_expiry = 12
    max_trades_day = 999
    tick = 0.00001

    def init(self) -> None:
        self.ema20 = self.I(lambda: self.data.ema20, name="ema20", overlay=True)
        self.ema50 = self.I(lambda: self.data.ema50, name="ema50", overlay=True)
        self.atr14 = self.I(lambda: self.data.atr14, name="atr14", overlay=False)
        self.htf_ema = self.I(lambda: self.data.htf_ema, name="htf_ema", overlay=False)
        self.htf_ema_prev = self.I(lambda: self.data.htf_ema_prev, name="htf_ema_prev", overlay=False)

        # Position-management state
        self.pending_order_bar: int = -1
        self.pending_side: str | None = None
        self.position_entry_price: float = 0.0
        self.position_initial_stop: float = 0.0
        self.position_high: float = 0.0
        self.position_low: float = 0.0
        self.position_entry_bar: int = -1
        self.position_partial_taken: bool = False
        self.position_be_active: bool = False
        self.had_position_last_bar: bool = False

        self.trades_today = 0
        self.prev_date = None
        self.last_flat_bar: int = -10_000

    def _calc_size_fraction(self, entry: float, stop: float) -> float:
        """Return fraction-of-equity sizing such that |entry-stop| × size = risk%."""
        risk_cash = self.equity * (self.risk_pct / 100.0)
        stop_dist = abs(entry - stop)
        if stop_dist <= 0:
            return 0.0
        notional = risk_cash / stop_dist * entry  # USD notional
        frac = notional / self.equity
        # backtesting.py: size as 0 < frac < 1 means fraction of available cash;
        # frac > 1 not allowed without margin. Cap at 0.99.
        return min(frac, 0.99)

    def next(self) -> None:
        i = len(self.data) - 1
        if i < 51:
            return

        ts = self.data.index[i]
        bar_date = ts.date()
        if self.prev_date is None or bar_date != self.prev_date:
            self.trades_today = 0
        self.prev_date = bar_date

        h = self.data.High[-1]
        lo = self.data.Low[-1]
        c = self.data.Close[-1]
        o = self.data.Open[-1]

        ema20 = self.ema20[-1]
        ema50 = self.ema50[-1]
        atrv = self.atr14[-1]
        htf_ema = self.htf_ema[-1]
        htf_prev = self.htf_ema_prev[-1]

        if any(math.isnan(x) for x in (ema20, ema50, atrv)) or atrv <= 0 or math.isnan(htf_ema) or math.isnan(htf_prev):
            return

        # Detect transition: had position last bar, now flat (= just exited)
        currently_in_position = bool(self.position)
        if self.had_position_last_bar and not currently_in_position:
            self.last_flat_bar = i
            self._reset_position_state()

        # Detect transition: just opened a position (had no position last bar, do now)
        if currently_in_position and not self.had_position_last_bar:
            # Capture entry parameters from the active trade
            for trade in self.trades:
                self.position_entry_price = trade.entry_price
                self.position_initial_stop = trade.sl if trade.sl is not None else 0.0
                self.position_high = max(h, trade.entry_price)
                self.position_low = min(lo, trade.entry_price)
                self.position_entry_bar = i
                self.position_partial_taken = False
                self.position_be_active = False
                self.trades_today += 1
                break

        self.had_position_last_bar = currently_in_position

        # ---------------- Manage open position ----------------
        if currently_in_position:
            entry = self.position_entry_price
            init_stop = self.position_initial_stop
            risk_unit = max(abs(entry - init_stop), self.tick)

            for trade in self.trades:
                if trade.is_long:
                    # update watermark
                    self.position_high = max(self.position_high, h)
                    # BE activation
                    if not self.position_be_active and self.position_high >= entry + self.be_at_rr * risk_unit:
                        self.position_be_active = True
                    # Partial TP at +1R intra-bar
                    partial_target = entry + self.partial_rr * risk_unit
                    if not self.position_partial_taken and h >= partial_target:
                        trade.close(portion=self.partial_pct / 100.0)
                        self.position_partial_taken = True
                    # ATR trail (only after BE active)
                    if self.position_be_active:
                        atr_trail = self.position_high - self.runner_atr_mult * atrv
                        new_sl = max(entry, atr_trail)
                        if trade.sl is None or new_sl > trade.sl:
                            trade.sl = new_sl
                    # Time stop
                    if (i - self.position_entry_bar) >= self.time_stop:
                        trade.close()
                        return
                else:  # short
                    self.position_low = min(self.position_low, lo)
                    if not self.position_be_active and self.position_low <= entry - self.be_at_rr * risk_unit:
                        self.position_be_active = True
                    partial_target = entry - self.partial_rr * risk_unit
                    if not self.position_partial_taken and lo <= partial_target:
                        trade.close(portion=self.partial_pct / 100.0)
                        self.position_partial_taken = True
                    if self.position_be_active:
                        atr_trail = self.position_low + self.runner_atr_mult * atrv
                        new_sl = min(entry, atr_trail)
                        if trade.sl is None or new_sl < trade.sl:
                            trade.sl = new_sl
                    if (i - self.position_entry_bar) >= self.time_stop:
                        trade.close()
                        return
                break  # only manage first trade
            return  # in-position bars don't generate new signals

        # Cancel stale stop orders (signal expiry)
        for order in list(self.orders):
            if hasattr(order, "_placed_at_bar"):
                age = i - order._placed_at_bar
                if age > self.signal_expiry:
                    order.cancel()

        # ---------------- Entry context ----------------
        cooldown_ok = (i - self.last_flat_bar) > self.cooldown
        budget_ok = self.trades_today < self.max_trades_day
        if not (cooldown_ok and budget_ok):
            return
        if self.orders:
            # already have a pending stop-buy/sell; let it run or expire
            return

        # ---------------- Signal evaluation ----------------
        bar_range = h - lo
        if bar_range <= 0 or bar_range > self.max_signal_atr_mult * atrv:
            return

        if i < self.slope_lb:
            return
        ema20_lb = self.ema20[-1 - self.slope_lb]
        if math.isnan(ema20_lb):
            return

        bull_trend = ema20 > ema50 and ema20 > ema20_lb
        bear_trend = ema20 < ema50 and ema20 < ema20_lb
        htf_up = htf_ema > htf_prev
        htf_dn = htf_ema < htf_prev

        up_th = h - bar_range * self.irb_pct
        dn_th = lo + bar_range * self.irb_pct
        bull_irb = (o <= up_th) and (c <= up_th)
        bear_irb = (o >= dn_th) and (c >= dn_th)

        if bull_trend and htf_up and bull_irb:
            entry_p = h + self.tick
            stop_p = lo - self.tick
            frac = self._calc_size_fraction(entry_p, stop_p)
            if frac > 0.001:
                order = self.buy(size=frac, stop=entry_p, sl=stop_p)
                if order is not None:
                    order._placed_at_bar = i
        elif bear_trend and htf_dn and bear_irb:
            entry_p = lo - self.tick
            stop_p = h + self.tick
            frac = self._calc_size_fraction(entry_p, stop_p)
            if frac > 0.001:
                order = self.sell(size=frac, stop=entry_p, sl=stop_p)
                if order is not None:
                    order._placed_at_bar = i

    def _reset_position_state(self) -> None:
        self.position_entry_price = 0.0
        self.position_initial_stop = 0.0
        self.position_high = 0.0
        self.position_low = 0.0
        self.position_entry_bar = -1
        self.position_partial_taken = False
        self.position_be_active = False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--htf", default="60min")
    parser.add_argument("--risk", type=float, default=0.33)
    parser.add_argument("--max-trades-day", type=int, default=999)
    parser.add_argument("--csv", default=str(M5_CSV))
    parser.add_argument("--years", type=int, default=10, help="Backtest window in years (10 = full)")
    args = parser.parse_args()

    print(f"[load] {args.csv}")
    df = prepare_data(Path(args.csv), htf=args.htf)
    if args.years < 10:
        cutoff = df.index[-1] - pd.DateOffset(years=args.years)
        df = df[df.index >= cutoff]
    print(f"        {len(df):,} bars  ({df.index[0].date()} → {df.index[-1].date()})")

    bt = Backtest(df, IRBv5BTPY, cash=100_000, commission=0.0, margin=0.02, exclusive_orders=True, trade_on_close=False)
    print(f"[run] backtesting.py · risk={args.risk}% · htf={args.htf} · max_trades_day={args.max_trades_day}")
    stats = bt.run(risk_pct=args.risk, max_trades_day=args.max_trades_day)

    init_eq = 100_000.0
    final_eq = stats["Equity Final [$]"]
    net = final_eq - init_eq
    n_trades = int(stats["# Trades"])
    wr = float(stats["Win Rate [%]"])
    pf = float(stats["Profit Factor"]) if not math.isnan(stats["Profit Factor"]) else 0.0
    max_dd = float(stats["Max. Drawdown [%]"])

    print()
    print("=" * 70)
    print(f"backtesting.py result · {df.index[0].date()} → {df.index[-1].date()}")
    print("=" * 70)
    print(f"  Net P&L:       ${net:+,.2f}  ({net / init_eq * 100:+.2f}%)")
    print(f"  Final equity:  ${final_eq:,.2f}")
    print(f"  Trades:        {n_trades:,}")
    print(f"  Win rate:      {wr:.2f}%")
    print(f"  Profit factor: {pf:.3f}")
    print(f"  Max DD:        {max_dd:.2f}%")
    print()
    print("[ref] vault matched-risk (10y full): +$45,310 · 32,815 trades · WR 49.34% · PF 1.081 · DD 6.87%")

    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_md = OUT_DIR / f"backtest_btpy_irb_v5_{ts_tag}.md"
    lines = [
        f"# IRB v5 — backtesting.py · {df.index[0].date()} → {df.index[-1].date()}",
        "",
        f"- Net P&L:       ${net:+,.2f}  ({net / init_eq * 100:+.2f}%)",
        f"- Final equity:  ${final_eq:,.2f}",
        f"- Trades:        {n_trades:,}",
        f"- Win rate:      {wr:.2f}%",
        f"- Profit factor: {pf:.3f}",
        f"- Max DD:        {max_dd:.2f}%",
        "",
        "## Full backtesting.py stats",
        "```",
        str(stats),
        "```",
    ]
    out_md.write_text("\n".join(lines))
    print(f"[saved] {out_md}")


if __name__ == "__main__":
    main()
