#!/usr/bin/env python3
"""Backtest task-1029: MTF EMA + MACD Divergence strategy (EUR/USD M5).

Spec (from the operator's written system breakdown of the referenced video):
  - Instrument / TF : EUR/USD, 5-minute base chart
  - HTF bias filters: 50 EMA on 15-minute  AND  50 EMA on 1-hour
  - Entry signal    : MACD (12/26/9, default) divergence vs price, CONFIRMED
                      by a MACD-line / signal-line crossover in the bias
                      direction
  - Stop            : beyond the swing point that formed the divergence
  - Target          : 2R (reward-to-risk 2:1)

Faithful mechanical reading:
  LONG  when  close > EMA15m_50  AND  close > EMA1h_50   (both HTFs bullish)
              AND a *bullish* MACD divergence (price lower-low, MACD higher-low)
              AND MACD line crosses ABOVE its signal line on the trigger bar.
  SHORT when  close < EMA15m_50  AND  close < EMA1h_50   (both HTFs bearish)
              AND a *bearish* MACD divergence (price higher-high, MACD lower-high)
              AND MACD line crosses BELOW its signal line on the trigger bar.

  stop  = swing pivot (low for LONG / high for SHORT) -/+ buffer
  tp    = entry +/- 2 * |entry - stop|

The 15m and 1h EMAs are derived from the M5 feed by bucketing closes; each M5
bar is assigned the EMA of the *last completed* HTF bucket -> no look-ahead.
Divergence uses fractal pivots confirmed L bars after they form -> no look-ahead.
Entries are confirmed on the closed bar and filled next bar by the shared
``StrategyBacktesterAdapter``.

We run TWICE per dataset:
  1. NET   -- ECN costs (spread + commission + slippage) deducted (live verdict)
  2. GROSS -- zero cost, to isolate whether the ENTRY has any raw edge.

Same metric set, cost model, and engine as tasks 1024 / 1026 / 1028 for direct
comparability.

Example
-------
    python scripts/backtest_1029_macd_divergence.py \
        --data data/candles/EURUSD_M5_10yr.csv --symbol EURUSD --timeframe M5 \
        --spread 0.5 --commission 3.5 --slippage 0.2
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_1024_three_ema_atr import summarize
from backtest_three_ema_stoch_rsi import load_candles

from novatrade.backtest.engine import StrategyBacktesterAdapter
from novatrade.backtest.environment import BacktestEnvironment, SpreadAssumptions
from novatrade.models import Candle
from novatrade.strategies.base import BaseStrategy, EntrySignal, ExitSignal

# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------


def _ema(values: list[float], period: int) -> list[float]:
    """Recursive EMA, seeded at the first finite value. NaN-padded leading."""
    n = len(values)
    out = [float("nan")] * n
    k = 2.0 / (period + 1.0)
    ema: float | None = None
    for i, v in enumerate(values):
        if math.isnan(v):
            continue
        ema = v if ema is None else v * k + ema * (1.0 - k)
        out[i] = ema
    return out


def _htf_ema(candles: list[Candle], bucket_seconds: int, period: int) -> list[float]:
    """EMA(period) of higher-timeframe bucket closes, mapped onto each M5 bar.

    Each M5 bar gets the EMA value of the LAST COMPLETED bucket (strictly before
    the current bucket) -> no look-ahead into the still-forming HTF candle.
    """
    n = len(candles)
    out = [float("nan")] * n
    k = 2.0 / (period + 1.0)
    ema: float | None = None
    cur_bucket: int | None = None
    cur_close = 0.0
    last_completed = float("nan")
    for i, c in enumerate(candles):
        b = int(c.timestamp // bucket_seconds)
        if cur_bucket is None:
            cur_bucket, cur_close = b, c.close
        elif b != cur_bucket:
            ema = cur_close if ema is None else cur_close * k + ema * (1.0 - k)
            last_completed = ema
            cur_bucket, cur_close = b, c.close
        else:
            cur_close = c.close
        out[i] = last_completed
    return out


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class MacdDivergenceMtfStrategy(BaseStrategy):
    """MTF EMA bias + MACD divergence (crossover-confirmed) + 2R target."""

    name = "macd_divergence_mtf"
    family = "reversal"

    def __init__(
        self,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        ema_period: int = 50,
        pivot_lag: int = 3,
        m15_seconds: int = 15 * 60,
        h1_seconds: int = 60 * 60,
        tp_rr: float = 2.0,
        stop_buffer_pips: float = 1.5,
        pip: float = 0.0001,
        warmup_bars: int = 60,
        require_both_htf: bool = True,
    ) -> None:
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.ema_period = ema_period
        self.pivot_lag = pivot_lag
        self.m15_seconds = m15_seconds
        self.h1_seconds = h1_seconds
        self.tp_rr = tp_rr
        self.stop_buffer = stop_buffer_pips * pip
        self.warmup_bars = warmup_bars
        self.require_both_htf = require_both_htf

    # -- indicators --------------------------------------------------------
    def compute_indicators(self, candles: list[Candle]) -> dict[str, list[float]]:
        n = len(candles)
        if n == 0:
            return {}
        closes = [c.close for c in candles]
        macd_line = [
            (a - b) if not (math.isnan(a) or math.isnan(b)) else float("nan")
            for a, b in zip(_ema(closes, self.macd_fast), _ema(closes, self.macd_slow), strict=False)
        ]
        signal = _ema(macd_line, self.macd_signal)

        ema15 = _htf_ema(candles, self.m15_seconds, self.ema_period)
        ema1h = _htf_ema(candles, self.h1_seconds, self.ema_period)

        # Confirmed fractal pivots (lag L). At bar i, a pivot at j=i-L is
        # confirmed when low[j]/high[j] is the extreme of window [j-L, j+L].
        L = self.pivot_lag
        # For each bar i, expose the LAST TWO confirmed pivot lows/highs:
        # price + MACD value at the pivot bar.
        pl_price = [float("nan")] * n  # last confirmed pivot-low price
        pl_macd = [float("nan")] * n
        pl_price2 = [float("nan")] * n  # previous (older) confirmed pivot-low
        pl_macd2 = [float("nan")] * n
        ph_price = [float("nan")] * n
        ph_macd = [float("nan")] * n
        ph_price2 = [float("nan")] * n
        ph_macd2 = [float("nan")] * n

        lows: list[tuple[float, float]] = []  # (price, macd) confirmed pivot lows
        highs: list[tuple[float, float]] = []
        for i in range(n):
            j = i - L
            if j - L >= 0 and j + L < n + 0 and i >= 0 and j - L >= 0:
                # window [j-L, j+L] is fully available once i >= j+L == i, true.
                lo = min(candles[w].low for w in range(j - L, j + L + 1))
                hi = max(candles[w].high for w in range(j - L, j + L + 1))
                if candles[j].low == lo and not math.isnan(macd_line[j]):
                    lows.append((candles[j].low, macd_line[j]))
                if candles[j].high == hi and not math.isnan(macd_line[j]):
                    highs.append((candles[j].high, macd_line[j]))
            if lows:
                pl_price[i], pl_macd[i] = lows[-1]
                if len(lows) >= 2:
                    pl_price2[i], pl_macd2[i] = lows[-2]
            if highs:
                ph_price[i], ph_macd[i] = highs[-1]
                if len(highs) >= 2:
                    ph_price2[i], ph_macd2[i] = highs[-2]

        return {
            "macd": macd_line,
            "signal": signal,
            "ema15": ema15,
            "ema1h": ema1h,
            "pl_price": pl_price,
            "pl_macd": pl_macd,
            "pl_price2": pl_price2,
            "pl_macd2": pl_macd2,
            "ph_price": ph_price,
            "ph_macd": ph_macd,
            "ph_price2": ph_price2,
            "ph_macd2": ph_macd2,
        }

    # -- signals -----------------------------------------------------------
    def generate_signals(self, candles: list[Candle], indicators: dict[str, list[float]]) -> list[EntrySignal]:
        out: list[EntrySignal] = []
        for i in range(len(candles)):
            sig = self.check_entry(i, candles, indicators)
            if sig is not None:
                out.append(sig)
        return out

    def check_entry(
        self,
        bar_index: int,
        candles: list[Candle],
        indicators: dict[str, list[float]],
        h4_candles: list[Candle] | None = None,
    ) -> EntrySignal | None:
        i = bar_index
        if i < self.warmup_bars or i < 1 or i >= len(candles):
            return None
        macd = indicators["macd"]
        sig = indicators["signal"]
        ema15 = indicators["ema15"]
        ema1h = indicators["ema1h"]
        if any(math.isnan(x[i]) or math.isnan(x[i - 1]) for x in (macd, sig)):
            return None
        if math.isnan(ema15[i]) or math.isnan(ema1h[i]):
            return None

        bar = candles[i]
        cross_up = macd[i - 1] <= sig[i - 1] and macd[i] > sig[i]
        cross_dn = macd[i - 1] >= sig[i - 1] and macd[i] < sig[i]

        bull_bias = bar.close > ema15[i] and bar.close > ema1h[i]
        bear_bias = bar.close < ema15[i] and bar.close < ema1h[i]
        if not self.require_both_htf:
            bull_bias = bar.close > ema15[i] or bar.close > ema1h[i]
            bear_bias = bar.close < ema15[i] or bar.close < ema1h[i]

        # ----- LONG: bullish divergence + cross up, in bullish bias -----
        if cross_up and bull_bias:
            p1, m1 = indicators["pl_price2"][i], indicators["pl_macd2"][i]  # older
            p2, m2 = indicators["pl_price"][i], indicators["pl_macd"][i]  # recent
            # price lower-low but MACD higher-low => bullish divergence
            if not any(math.isnan(v) for v in (p1, m1, p2, m2)) and p2 < p1 and m2 > m1:
                swing_low = min(p2, bar.low)
                stop = swing_low - self.stop_buffer
                risk = bar.close - stop
                if risk > 0:
                    tp = bar.close + self.tp_rr * risk
                    return EntrySignal(
                        bar_index=i,
                        side="LONG",
                        entry_price=bar.close,
                        stop_loss=stop,
                        metadata={"take_profit": tp, "swing": swing_low, "setup": "bull_div"},
                    )

        # ----- SHORT: bearish divergence + cross down, in bearish bias -----
        if cross_dn and bear_bias:
            p1, m1 = indicators["ph_price2"][i], indicators["ph_macd2"][i]
            p2, m2 = indicators["ph_price"][i], indicators["ph_macd"][i]
            # price higher-high but MACD lower-high => bearish divergence
            if not any(math.isnan(v) for v in (p1, m1, p2, m2)) and p2 > p1 and m2 < m1:
                swing_high = max(p2, bar.high)
                stop = swing_high + self.stop_buffer
                risk = stop - bar.close
                if risk > 0:
                    tp = bar.close - self.tp_rr * risk
                    return EntrySignal(
                        bar_index=i,
                        side="SHORT",
                        entry_price=bar.close,
                        stop_loss=stop,
                        metadata={"take_profit": tp, "swing": swing_high, "setup": "bear_div"},
                    )
        return None

    # -- exits: fixed stop / 2R target (set at entry, never moved) ----------
    def check_exit(
        self,
        bar_index: int,
        candles: list[Candle],
        indicators: dict[str, list[float]],
        position: dict[str, Any] | None = None,
    ) -> ExitSignal | None:
        if position is None or bar_index >= len(candles):
            return None
        bar = candles[bar_index]
        side = position.get("side", "LONG")
        entry = position.get("entry_price", 0.0)
        stop = position.get("stop_loss", 0.0)
        # The engine's position dict drops EntrySignal.metadata, so reconstruct
        # the fixed 2R target from entry/stop (set at entry, never moved).
        risk = abs(entry - stop)
        if side == "LONG":
            tp = entry + self.tp_rr * risk
            if bar.low <= stop:  # stop checked first (conservative)
                return ExitSignal(bar_index=bar_index, exit_price=stop, reason="stop_loss")
            if bar.high >= tp:
                return ExitSignal(bar_index=bar_index, exit_price=tp, reason="signal_exit")
        else:
            tp = entry - self.tp_rr * risk
            if bar.high >= stop:
                return ExitSignal(bar_index=bar_index, exit_price=stop, reason="stop_loss")
            if bar.low <= tp:
                return ExitSignal(bar_index=bar_index, exit_price=tp, reason="signal_exit")
        return None

    def get_default_doctrine(self, pair: str, timeframe: str) -> dict[str, Any]:
        return {"name": f"macd_div_mtf_{pair.lower()}_{timeframe.lower()}", "version": "1.0.0"}

    def get_parameter_bounds(self) -> dict[str, tuple[float, float]]:
        return {"pivot_lag": (2, 6), "tp_rr": (1.0, 4.0), "stop_buffer_pips": (0.0, 5.0)}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_once(candles, *, gross: bool, args) -> dict:
    spread = SpreadAssumptions(
        avg_spread_pips=0.0 if gross else args.spread,
        commission_per_lot_usd=0.0 if gross else args.commission,
        slippage_pips=0.0 if gross else args.slippage,
    )
    env = BacktestEnvironment(
        initial_equity=args.initial_equity,
        risk_fraction=args.risk,
        spread=spread,
    )
    strategy = MacdDivergenceMtfStrategy(
        tp_rr=args.tp_rr,
        pivot_lag=args.pivot_lag,
        stop_buffer_pips=args.stop_buffer,
        require_both_htf=not args.single_htf,
    )
    bt = StrategyBacktesterAdapter(strategy, env)
    result = bt.run(candles)
    return summarize(result, candles, args.initial_equity)


def _print(tag: str, m: dict, args) -> None:
    print(f"\n=== TASK-1029  MTF EMA + MACD divergence ({tag}) ===")
    print(
        f"params: TP={args.tp_rr:.1f}R  pivot_lag={args.pivot_lag}  "
        f"stop_buf={args.stop_buffer}p  htf={'1-of-2' if args.single_htf else 'both'}  "
        f"EMA50(15m,1h) bias"
    )
    if m.get("n_trades", 0) == 0:
        print("No trades generated.")
        return
    print(f"trades         : {m['n_trades']:,}")
    print(f"win rate       : {m['win_rate'] * 100:.1f}%")
    print(f"profit factor  : {m['profit_factor']:.3f}")
    print(f"expectancy R   : {m['expectancy_r']:+.4f}")
    print(f"total R        : {m['total_r']:+.2f}")
    print(f"total PnL      : ${m['total_pnl_usd']:,.0f}  ({m['return_pct']:+.1f}%)")
    print(f"final equity   : ${m['final_equity']:,.0f}")
    print(f"max drawdown   : {m['max_drawdown_pct']:.1f}%")
    print(f"positive months: {m['positive_months']}/{m['n_months']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--spread", type=float, default=0.5)
    ap.add_argument("--commission", type=float, default=3.5)
    ap.add_argument("--slippage", type=float, default=0.2)
    ap.add_argument("--risk", type=float, default=0.0033)
    ap.add_argument("--initial-equity", type=float, default=100_000.0)
    ap.add_argument("--max-rows", type=int, default=0)
    ap.add_argument("--tp-rr", type=float, default=2.0)
    ap.add_argument("--pivot-lag", type=int, default=3)
    ap.add_argument("--stop-buffer", type=float, default=1.5, help="stop buffer in pips beyond the swing")
    ap.add_argument("--single-htf", action="store_true", help="require only ONE of the two HTF EMAs (relaxed)")
    args = ap.parse_args()

    candles = load_candles(args.data, args.symbol, args.timeframe)
    if args.max_rows > 0:
        candles = candles[: args.max_rows]
    print(f"Loaded {len(candles):,} candles ({args.symbol} {args.timeframe}) from {args.data}")

    net = run_once(candles, gross=False, args=args)
    gross = run_once(candles, gross=True, args=args)
    _print("NET of ECN cost", net, args)
    _print("GROSS (zero cost)", gross, args)


if __name__ == "__main__":
    main()
