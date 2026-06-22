"""Ed Seykota trend-following system (reverse-engineered from the public rules).

Seykota left no book; his approach is reconstructed from interviews and the
mechanical trend systems he influenced (Donchian, Dennis/Turtles). The five
public rules are:

  1. Trend filter   -- EMA(100): long-only bias above it, short-only below.
  2. Entry signal   -- new 20-bar high (long) / 20-bar low (short)  [Donchian].
  3. Pyramiding     -- add up to 3 units as price moves favourably.
  4. Risk mgmt      -- ATR-based trailing stop (let winners run, cut losers).
  5. Sizing         -- fixed % risk per unit.

This class implements rules 1, 2, 4 directly by extending ``BreakoutStrategy``
(which already does the 20-bar Donchian breakout + ATR trailing stop) and
adding the EMA-100 directional filter -- the one genuine structural difference
from a plain Donchian breakout. Rule 5 (fixed-% sizing) is supplied by the
backtest ``BacktestEnvironment.risk_fraction``.

KNOWN LIMITATION -- pyramiding (rule 3): the single-position
``StrategyBacktesterAdapter`` holds at most one unit per symbol, so adding
units is NOT modelled here. Pyramiding amplifies an existing edge (and its
drawdown); it does not create one. The honest first question is therefore
"does the 1-unit core have a positive expectancy net of cost?" -- which this
class is built to answer. Multi-unit scaling would require an engine that
supports incremental position adds.
"""

from __future__ import annotations

import math
from typing import Any

from novatrade.backtest.engine import compute_ema
from novatrade.models import Candle
from novatrade.strategies.base import EntrySignal
from novatrade.strategies.breakout import BreakoutStrategy


class SeykotaTrendStrategy(BreakoutStrategy):
    """20-bar Donchian breakout gated by an EMA-100 trend filter + ATR trail.

    Entry:
    - close > EMA(N) AND close breaks above the prior 20-bar high  -> LONG
    - close < EMA(N) AND close breaks below the prior 20-bar low    -> SHORT

    Exit: inherited from ``BreakoutStrategy`` (ATR trailing stop / opposite
    channel touch / time stop).
    """

    name = "seykota_trend"
    family = "trend_following"

    def __init__(
        self,
        channel_period: int = 20,
        atr_period: int = 14,
        trail_atr_mult: float = 2.0,
        time_stop_bars: int = 200,
        ema_period: int = 100,
        warmup_bars: int | None = None,
        pip_buffer: float = 0.0001,
    ) -> None:
        # Warmup must cover the slowest indicator (the EMA), else the filter
        # reads a not-yet-converged EMA and lets through false-biased entries.
        if warmup_bars is None:
            warmup_bars = ema_period + channel_period
        super().__init__(
            channel_period=channel_period,
            atr_period=atr_period,
            trail_atr_mult=trail_atr_mult,
            time_stop_bars=time_stop_bars,
            warmup_bars=warmup_bars,
            pip_buffer=pip_buffer,
        )
        self.ema_period = ema_period

    def compute_indicators(self, candles: list[Candle]) -> dict[str, list[float]]:
        ind = super().compute_indicators(candles)
        closes = [c.close for c in candles]
        ind["ema"] = compute_ema(closes, self.ema_period) if closes else []
        return ind

    def check_entry(
        self,
        bar_index: int,
        candles: list[Candle],
        indicators: dict[str, list[float]],
        h4_candles: list[Candle] | None = None,
    ) -> EntrySignal | None:
        sig = super().check_entry(bar_index, candles, indicators, h4_candles)
        if sig is None:
            return None

        ema = indicators.get("ema", [])
        i = bar_index
        if i >= len(ema) or math.isnan(ema[i]):
            return None

        close = candles[i].close
        # Trend filter: only trade in the direction of the EMA-100 bias.
        if sig.side == "LONG" and close <= ema[i]:
            return None
        if sig.side == "SHORT" and close >= ema[i]:
            return None

        sig.metadata["ema"] = ema[i]
        sig.metadata["ema_period"] = self.ema_period
        return sig

    def get_default_doctrine(self, pair: str, timeframe: str) -> dict[str, Any]:
        doc = super().get_default_doctrine(pair, timeframe)
        doc["name"] = f"seykota_trend_{pair.lower()}_{timeframe.lower()}"
        doc["description"] = f"Seykota EMA-100 + 20-bar Donchian breakout for {pair} on {timeframe}"
        doc["concept"] = (
            "Reverse-engineered Ed Seykota trend system: EMA-100 directional "
            "filter gating a 20-bar Donchian breakout, ATR trailing-stop exit. "
            "Pyramiding (rule 3) not modelled by the single-position engine."
        )
        doc["setup_type"] = "trend_following"
        doc["entry_signal"] = "ema_filtered_donchian_breakout"
        doc["confirmations"] = ["ema100_trend_filter", "atr_filter"]
        doc["filters_mandatory"] = ["ema100_trend_filter", "donchian_breakout"]
        doc["mutable_params"]["ema_period"] = {"default": 100, "min": 20, "max": 200, "step": 5}
        return doc

    def get_parameter_bounds(self) -> dict[str, tuple[float, float]]:
        bounds = super().get_parameter_bounds()
        bounds["ema_period"] = (20, 200)
        return bounds
