"""IRB (Imbalance/Recovery/Breakout) strategy wrapper.

Wraps the existing IRBBacktester logic from ``novatrade.backtest.engine`` into
the BaseStrategy interface. This is a thin delegation layer -- all actual logic
stays in the engine module to maintain zero regression risk.
"""

from __future__ import annotations

import math
from typing import Any

from novatrade.backtest.engine import compute_adx, compute_atr, compute_bbw, compute_ema
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle
from novatrade.monitor.signal_monitor import record_signal
from novatrade.strategies.base import BaseStrategy, EntrySignal, ExitSignal


class IRBStrategy(BaseStrategy):
    """IRB reversal strategy -- wraps the existing IRBBacktester.

    This strategy detects Imbalance/Recovery/Breakout candle patterns aligned
    with trend direction on H1 and confirmed by H4 EMA alignment. It uses
    ADX as a sideways filter and overextension as a volatility filter.

    All computation is delegated to the existing ``compute_ema``,
    ``compute_atr``, ``compute_adx`` functions from the backtest engine.
    """

    name = "irb"
    family = "reversal"

    def __init__(self, env: BacktestEnvironment | None = None) -> None:
        self.env = env or BacktestEnvironment()

    def compute_indicators(self, candles: list[Candle]) -> dict[str, list[float]]:
        """Compute EMA, ATR, and ADX indicators for the IRB strategy."""
        if not candles:
            return {"ema": [], "atr": [], "adx": []}

        closes = [c.close for c in candles]
        indicators = {
            "ema": compute_ema(closes, self.env.ema_period),
            "atr": compute_atr(candles, self.env.atr_period),
            "adx": compute_adx(candles, self.env.adx_period),
        }
        if self.env.use_regime_gate:
            indicators["bbw"] = compute_bbw(closes, self.env.regime_bbw_period)
        return indicators

    def generate_signals(self, candles: list[Candle], indicators: dict[str, list[float]]) -> list[EntrySignal]:
        """Scan all bars for IRB entry signals."""
        signals: list[EntrySignal] = []
        for i in range(len(candles)):
            sig = self.check_entry(i, candles, indicators)
            if sig is not None:
                signals.append(sig)
        return signals

    def check_entry(
        self,
        bar_index: int,
        candles: list[Candle],
        indicators: dict[str, list[float]],
        h4_candles: list[Candle] | None = None,
    ) -> EntrySignal | None:
        """Check IRB geometry and filters at bar_index.

        This replicates the signal detection from IRBBacktester._evaluate_signal
        but returns an EntrySignal instead of mutating internal state. The MTF
        alignment check is skipped when h4_candles is not provided.
        """
        e = self.env
        i = bar_index

        if i < e.warmup_bars or i >= len(candles):
            return None

        # Record that strategy evaluation occurred (for signal rate monitoring)
        record_signal("any")

        bar = candles[i]
        ema = indicators.get("ema", [])
        atr = indicators.get("atr", [])
        adx = indicators.get("adx", [])

        if i >= len(ema) or i >= len(atr) or i >= len(adx):
            return None
        if math.isnan(ema[i]) or math.isnan(atr[i]) or math.isnan(adx[i]):
            return None
        if atr[i] <= 0:
            return None

        # --- IRB geometry ---
        rng = bar.high - bar.low
        if rng <= 0:
            return None

        up_threshold = bar.high - (e.irb_threshold * rng)
        dn_threshold = bar.low + (e.irb_threshold * rng)
        is_uptrend_irb = bar.open <= up_threshold and bar.close <= up_threshold
        is_downtrend_irb = bar.open >= dn_threshold and bar.close >= dn_threshold

        if not is_uptrend_irb and not is_downtrend_irb:
            return None

        # --- Trend filter ---
        lookback = min(20, i)
        if lookback < 1:
            return None
        ema_slope = (ema[i] - ema[i - lookback]) / atr[i]

        trend_up = ema_slope >= e.trend_slope_threshold
        trend_dn = ema_slope <= -e.trend_slope_threshold

        side: str | None = None
        if is_uptrend_irb and trend_up:
            side = "LONG"
        elif is_downtrend_irb and trend_dn:
            side = "SHORT"
        else:
            return None

        # --- ADX sideways filter ---
        if math.isnan(adx[i]) or adx[i] < e.adx_threshold:
            return None

        # --- Overextension filter ---
        if rng / atr[i] > e.overextension_threshold:
            return None

        # --- Tier 1 regime gate: skip when BBW indicates ranging/squeeze ---
        if e.use_regime_gate:
            bbw = indicators.get("bbw", [])
            if i < len(bbw) and not math.isnan(bbw[i]) and bbw[i] < e.regime_bbw_threshold:
                return None

        # --- Compute entry/stop levels ---
        if side == "LONG":
            entry_price = bar.high + e.pip_buffer
            stop_loss = bar.low - e.pip_buffer
            # ATR-adaptive SL floor: widen SL if candle geometry is too tight
            if e.atr_sl_floor_multiplier > 0:
                min_sl_dist = atr[i] * e.atr_sl_floor_multiplier
                if (entry_price - stop_loss) < min_sl_dist:
                    stop_loss = entry_price - min_sl_dist
        else:
            entry_price = bar.low - e.pip_buffer
            stop_loss = bar.high + e.pip_buffer
            # ATR-adaptive SL floor: widen SL if candle geometry is too tight
            if e.atr_sl_floor_multiplier > 0:
                min_sl_dist = atr[i] * e.atr_sl_floor_multiplier
                if (stop_loss - entry_price) < min_sl_dist:
                    stop_loss = entry_price + min_sl_dist

        # Record trade signal for monitoring
        record_signal("trade")

        return EntrySignal(
            bar_index=i,
            side=side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            metadata={
                "irb_range": rng,
                "ema_slope": ema_slope,
                "adx_value": adx[i],
                "overextension_ratio": rng / atr[i],
            },
        )

    def check_exit(
        self,
        bar_index: int,
        candles: list[Candle],
        indicators: dict[str, list[float]],
        position: dict[str, Any] | None = None,
    ) -> ExitSignal | None:
        """Check IRB exit conditions: stop-loss, trailing stop, time stop.

        Args:
            position: Must include keys: side, entry_price, stop_loss,
                      entry_bar, current_stop, best_close.
        """
        if position is None or bar_index >= len(candles):
            return None

        bar = candles[bar_index]
        atr = indicators.get("atr", [])
        e = self.env

        pos_side = position.get("side", "LONG")
        current_stop = position.get("current_stop", position.get("stop_loss", 0.0))
        best_close = position.get("best_close", bar.close)
        entry_bar = position.get("entry_bar", 0)
        bars_held = bar_index - entry_bar

        # --- Stop-loss ---
        if pos_side == "LONG" and bar.low <= current_stop:
            return ExitSignal(bar_index=bar_index, exit_price=current_stop, reason="stop_loss")
        if pos_side == "SHORT" and bar.high >= current_stop:
            return ExitSignal(bar_index=bar_index, exit_price=current_stop, reason="stop_loss")

        # --- Time stop ---
        if bars_held >= e.time_stop_bars:
            return ExitSignal(bar_index=bar_index, exit_price=bar.close, reason="time_stop")

        # --- Trailing stop update and check ---
        if bar_index < len(atr) and not math.isnan(atr[bar_index]) and atr[bar_index] > 0:
            atr_val = atr[bar_index]
            if pos_side == "LONG":
                new_best = max(best_close, bar.close)
                new_trail = new_best - e.trail_atr_multiplier * atr_val
                if new_trail > current_stop and bar.low <= new_trail:
                    return ExitSignal(bar_index=bar_index, exit_price=new_trail, reason="trailing_stop")
            else:
                new_best = min(best_close, bar.close)
                new_trail = new_best + e.trail_atr_multiplier * atr_val
                if new_trail < current_stop and bar.high >= new_trail:
                    return ExitSignal(bar_index=bar_index, exit_price=new_trail, reason="trailing_stop")

        return None

    def get_default_doctrine(self, pair: str, timeframe: str) -> dict[str, Any]:
        """Return IRB-specific default doctrine."""
        return {
            "name": f"irb_{pair.lower()}_{timeframe.lower()}",
            "version": "1.0.0",
            "description": f"IRB reversal strategy for {pair} on {timeframe}",
            "concept": (
                "Detects Imbalance/Recovery/Breakout candle patterns where price "
                "rejects from a level, confirmed by EMA trend alignment on H1 and H4, "
                "filtered by ADX for sideways markets and overextension for volatility."
            ),
            "setup_type": "reversal",
            "entry_signal": "irb_rejection",
            "confirmations": ["ema_trend", "h4_alignment", "adx_filter"],
            "exit_method": "trailing_atr",
            "data": {
                "pair": pair,
                "timeframes": [timeframe, "H4"],
                "min_bars": 5000,
            },
            "mutable_params": {
                "irb_threshold": {"default": 0.45, "min": 0.30, "max": 0.60, "step": 0.01},
                "ema_period": {"default": 20, "min": 10, "max": 50, "step": 1},
                "atr_period": {"default": 14, "min": 7, "max": 21, "step": 1},
                "adx_period": {"default": 14, "min": 7, "max": 21, "step": 1},
                "trend_slope_threshold": {"default": 0.4, "min": 0.1, "max": 1.0, "step": 0.05},
                "adx_threshold": {"default": 20.0, "min": 15.0, "max": 30.0, "step": 0.5},
                "overextension_threshold": {"default": 2.0, "min": 1.5, "max": 3.0, "step": 0.1},
                "trail_atr_multiplier": {"default": 1.5, "min": 1.0, "max": 3.0, "step": 0.1},
                "trigger_window_bars": {"default": 20, "min": 10, "max": 40, "step": 1},
                "time_stop_bars": {"default": 40, "min": 20, "max": 80, "step": 1},
                "mtf_lookback": {"default": 5, "min": 1, "max": 20, "step": 1},
                "warmup_bars": {"default": 34, "min": 20, "max": 60, "step": 1},
            },
            "immutable_params": {
                "pip_buffer": 0.0001,
            },
            "filters_mandatory": ["irb_geometry", "trend_filter", "adx_filter"],
            "filters_optional": ["mtf_alignment", "overextension_filter"],
            "execution": {
                "spread_pips": 1.0,
                "slippage_pips": 0.0,
                "commission_per_lot": 0.0,
                "leverage": 100,
                "fill_policy": "next_bar_open",
            },
            "validation": {
                "min_trades": 50,
                "min_months": 6,
                "max_drawdown_pct": 15.0,
                "min_profit_factor": 1.3,
                "min_sharpe": 0.5,
            },
        }

    def get_parameter_bounds(self) -> dict[str, tuple[float, float]]:
        """Return IRB parameter bounds for optimization."""
        return {
            "irb_threshold": (0.30, 0.60),
            "ema_period": (10, 50),
            "atr_period": (7, 21),
            "adx_period": (7, 21),
            "trend_slope_threshold": (0.1, 1.0),
            "adx_threshold": (15.0, 30.0),
            "overextension_threshold": (1.5, 3.0),
            "trail_atr_multiplier": (1.0, 3.0),
            "trigger_window_bars": (10, 40),
            "time_stop_bars": (20, 80),
            "mtf_lookback": (1, 20),
            "warmup_bars": (20, 60),
        }
