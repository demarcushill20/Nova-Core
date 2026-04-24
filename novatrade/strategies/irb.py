"""IRB (Imbalance/Recovery/Breakout) strategy wrapper.

Wraps the existing IRBBacktester logic from ``novatrade.backtest.engine`` into
the BaseStrategy interface. This is a thin delegation layer -- all actual logic
stays in the engine module to maintain zero regression risk.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from novatrade.backtest.engine import compute_adx, compute_atr, compute_ema
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle
from novatrade.monitor.signal_monitor import record_signal
from novatrade.notify import rejection_telegram
from novatrade.strategies.base import BaseStrategy, EntrySignal, ExitSignal

log = logging.getLogger(__name__)

# Session filter: London+NY overlap window (UTC hours)
SESSION_START_HOUR_UTC = 7
SESSION_END_HOUR_UTC = 16

# ADX slope lookback for trend-strengthening check
ADX_SLOPE_LOOKBACK = 3


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
        # v5 simple trend filter needs ema_fast / ema_slow
        if self.env.use_simple_trend_filter:
            indicators["ema_fast"] = compute_ema(closes, self.env.ema_fast_period)
            indicators["ema_slow"] = compute_ema(closes, self.env.ema_slow_period)
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
        side: str | None = None
        ema_slope: float = 0.0

        if e.use_simple_trend_filter:
            # v5 simple trend: ema_fast > ema_slow + ema_fast rising over slope_lookback
            # Mirrors vault reference (Rob Hoffman IRB v5 - Relaxed Reliable Build, Python).
            ema_fast = indicators.get("ema_fast", [])
            ema_slow = indicators.get("ema_slow", [])
            if i >= len(ema_fast) or i >= len(ema_slow):
                log.info("IRB REJECTED [v5_ema_length] bar=%d ema_fast=%d ema_slow=%d", i, len(ema_fast), len(ema_slow))
                return None
            lb = min(e.ema_slope_lookback, i)
            if lb < 1:
                log.info("IRB REJECTED [v5_slope_lookback] bar=%d lookback=%d", i, lb)
                return None
            ef = ema_fast[i]
            es = ema_slow[i]
            ef_prev = ema_fast[i - lb]
            if math.isnan(ef) or math.isnan(es) or math.isnan(ef_prev):
                log.info("IRB REJECTED [v5_nan_ema] bar=%d ef=%.5f es=%.5f ef_prev=%.5f", i, ef, es, ef_prev)
                return None
            bull_trend = ef > es and ef > ef_prev
            bear_trend = ef < es and ef < ef_prev
            if is_uptrend_irb and bull_trend:
                side = "LONG"
            elif is_downtrend_irb and bear_trend:
                side = "SHORT"
            else:
                log.info(
                    "IRB REJECTED [trend_filter] bar=%d irb_up=%s irb_dn=%s "
                    "bull=%s bear=%s ef=%.5f es=%.5f ef_prev=%.5f",
                    i,
                    is_uptrend_irb,
                    is_downtrend_irb,
                    bull_trend,
                    bear_trend,
                    ef,
                    es,
                    ef_prev,
                )
                return None

            # --- v5 HTF EMA slope filter (vault Pine: htfLongOk = htfSlopeUp) ---
            # Mirrors the vault Python reference's htf_long_ok / htf_short_ok gates.
            # Uses the higher-timeframe candle buffer (H1 for M5 primary, H4 for
            # H1 primary) and the configured ema_period as the HTF EMA length.
            if h4_candles and len(h4_candles) >= 2:
                htf_lookback = max(1, e.mtf_lookback)
                if len(h4_candles) > htf_lookback:
                    htf_closes = [c.close for c in h4_candles]
                    htf_ema_arr = compute_ema(htf_closes, e.ema_period)
                    if len(htf_ema_arr) > htf_lookback:
                        htf_ema_now = htf_ema_arr[-1]
                        htf_ema_prev = htf_ema_arr[-1 - htf_lookback]
                        if not math.isnan(htf_ema_now) and not math.isnan(htf_ema_prev):
                            htf_rising = htf_ema_now > htf_ema_prev
                            htf_falling = htf_ema_now < htf_ema_prev
                            if side == "LONG" and not htf_rising:
                                msg = (
                                    f"bar={i} side=LONG "
                                    f"htf_ema_now={htf_ema_now:.5f} htf_ema_prev={htf_ema_prev:.5f} (not rising)"
                                )
                                log.info("IRB REJECTED [htf_ema] %s", msg)
                                rejection_telegram("htf_ema", msg)
                                return None
                            if side == "SHORT" and not htf_falling:
                                msg = (
                                    f"bar={i} side=SHORT "
                                    f"htf_ema_now={htf_ema_now:.5f} htf_ema_prev={htf_ema_prev:.5f} (not falling)"
                                )
                                log.info("IRB REJECTED [htf_ema] %s", msg)
                                rejection_telegram("htf_ema", msg)
                                return None
            # v5 mode SKIPS ADX threshold and ADX slope filters (vault reference does not check ADX).
        else:
            lookback = min(20, i)
            if lookback < 1:
                log.info("IRB REJECTED [v4_lookback] bar=%d lookback=%d", i, lookback)
                return None
            ema_slope = (ema[i] - ema[i - lookback]) / atr[i]

            trend_up = ema_slope >= e.trend_slope_threshold
            trend_dn = ema_slope <= -e.trend_slope_threshold

            if is_uptrend_irb and trend_up:
                side = "LONG"
            elif is_downtrend_irb and trend_dn:
                side = "SHORT"
            else:
                log.info(
                    "IRB REJECTED [v4_trend] bar=%d irb_up=%s irb_dn=%s slope=%.4f threshold=%.4f",
                    i,
                    is_uptrend_irb,
                    is_downtrend_irb,
                    ema_slope,
                    e.trend_slope_threshold,
                )
                return None

            # --- ADX sideways filter (v4 only) ---
            if math.isnan(adx[i]) or adx[i] < e.adx_threshold:
                log.info(
                    "IRB REJECTED [adx_threshold] bar=%d adx=%.2f threshold=%.2f",
                    i,
                    adx[i],
                    e.adx_threshold,
                )
                return None

            # --- ADX slope check: trend must be strengthening (v4 only) ---
            if (
                i >= ADX_SLOPE_LOOKBACK
                and not math.isnan(adx[i - ADX_SLOPE_LOOKBACK])
                and adx[i] < adx[i - ADX_SLOPE_LOOKBACK]
            ):
                log.info(
                    "IRB REJECTED [adx_slope] bar=%d adx=%.2f adx_prev=%.2f (declining)",
                    i,
                    adx[i],
                    adx[i - ADX_SLOPE_LOOKBACK],
                )
                return None

        # --- Session filter: only trade during London+NY overlap (07-16 UTC) ---
        if e.session_filter == "london" and bar.timestamp > 0:
            bar_hour = datetime.fromtimestamp(bar.timestamp, tz=timezone.utc).hour
            if not (SESSION_START_HOUR_UTC <= bar_hour < SESSION_END_HOUR_UTC):
                msg = (
                    f"bar={i} side={side} hour={bar_hour} (outside {SESSION_START_HOUR_UTC}-{SESSION_END_HOUR_UTC} UTC)"
                )
                log.info("IRB REJECTED [session] %s", msg)
                rejection_telegram("session", msg)
                return None

        # --- Overextension filter (max signal range / ATR) ---
        overext_ratio = rng / atr[i]
        if overext_ratio > e.overextension_threshold:
            msg = f"bar={i} side={side} ratio={overext_ratio:.3f} threshold={e.overextension_threshold:.3f}"
            log.info("IRB REJECTED [overextension] %s", msg)
            rejection_telegram("overextension", msg)
            return None

        # --- v5 minimum signal size filter (min signal range / ATR) ---
        if e.min_signal_atr_mult > 0 and overext_ratio < e.min_signal_atr_mult:
            msg = f"bar={i} side={side} ratio={overext_ratio:.3f} min={e.min_signal_atr_mult:.3f}"
            log.info("IRB REJECTED [min_signal_size] %s", msg)
            rejection_telegram("min_signal_size", msg)
            return None

        # --- Compute entry/stop levels ---
        # Spread cushion: widen SL by the configured spread buffer so the
        # broker's bid-side (long) / ask-side (short) trigger doesn't clip the
        # stop inside the intended wick-plus-buffer distance.
        spread_cushion = max(0.0, e.sl_spread_buffer_pips) * e.pip_value
        if side == "LONG":
            entry_price = bar.high + e.pip_buffer
            stop_loss = bar.low - e.pip_buffer - spread_cushion
            # ATR-adaptive SL floor: widen SL if candle geometry is too tight
            if e.atr_sl_floor_multiplier > 0:
                min_sl_dist = atr[i] * e.atr_sl_floor_multiplier
                if (entry_price - stop_loss) < min_sl_dist:
                    stop_loss = entry_price - min_sl_dist
        else:
            entry_price = bar.low - e.pip_buffer
            stop_loss = bar.high + e.pip_buffer + spread_cushion
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

        Note:
            In v5 mode (``use_simple_trend_filter=True``) this method returns
            ``None`` because the live engine handles all exit logic via
            ``LiveStrategyEngine._check_v5_exit``. Mixing the v4 trailing
            stop here with the v5 partial/breakeven/runner-trail flow would
            double-count exits.
        """
        if self.env.use_simple_trend_filter:
            return None
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
                "adx_threshold": {"default": 25.0, "min": 15.0, "max": 30.0, "step": 0.5},
                "overextension_threshold": {"default": 2.0, "min": 1.5, "max": 3.0, "step": 0.1},
                "trail_atr_multiplier": {"default": 1.5, "min": 1.0, "max": 3.0, "step": 0.1},
                "trigger_window_bars": {"default": 20, "min": 10, "max": 40, "step": 1},
                "time_stop_bars": {"default": 40, "min": 20, "max": 80, "step": 1},
                "mtf_lookback": {"default": 5, "min": 1, "max": 20, "step": 1},
                "warmup_bars": {"default": 34, "min": 20, "max": 60, "step": 1},
                "sl_spread_buffer_pips": {"default": 1.0, "min": 0.0, "max": 3.0, "step": 0.1},
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
