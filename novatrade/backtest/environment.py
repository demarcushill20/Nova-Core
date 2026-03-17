"""Backtest environment definition for IRB strategy validation.

Documents the exact test environment configuration used for backtesting,
including data source, symbol mapping, timeframes, spread/commission/slippage
assumptions, fill model, and stop-order simulation behaviour.

This module is the single source of truth for backtest environment parameters,
ensuring reproducibility across runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class EngineType(Enum):
    """What engine is performing the backtest."""

    PYTHON_REPRODUCTION = "python_reproduction"
    TRADINGVIEW_NATIVE = "tradingview_native"
    EXPORTED_DATA = "exported_data"
    OTHER = "other"


class FillModel(Enum):
    """How pending orders are filled in the simulation."""

    NEXT_BAR_OHLC = "next_bar_ohlc"
    INTRA_BAR_STOP = "intra_bar_stop"


class TimeZone(Enum):
    """Session timezone assumption."""

    UTC = "UTC"
    NEW_YORK = "America/New_York"
    LONDON = "Europe/London"


@dataclass(frozen=True)
class SpreadAssumptions:
    """Spread and transaction cost model."""

    fixed_spread_pips: float | None = None
    avg_spread_pips: float = 1.0
    commission_per_lot_usd: float = 0.0
    slippage_pips: float = 0.0
    description: str = "OANDA demo typical 0.5-1.5 pips; no commission (embedded in spread)"

    @property
    def total_cost_pips(self) -> float:
        """Estimated round-trip cost per trade in pips."""
        spread = self.fixed_spread_pips if self.fixed_spread_pips is not None else self.avg_spread_pips
        return spread + (2 * self.slippage_pips)


@dataclass(frozen=True)
class StopOrderSimulation:
    """How stop orders are simulated."""

    fill_on_touch: bool = True
    use_high_low_check: bool = True
    gap_fill_at_open: bool = True
    description: str = (
        "Stop orders fill when bar high/low crosses stop price. "
        "If bar opens past stop level (gap), fills at open price."
    )


@dataclass(frozen=True)
class PendingOrderPolicy:
    """How pending orders expire."""

    max_bars: int = 20
    enforcement: str = "hard_cancel"
    description: str = "Cancel unfilled stop orders after 20 H1 bars [U5]"


@dataclass(frozen=True)
class TrailingStopSimulation:
    """How the trailing stop is evaluated."""

    atr_multiplier: float = 1.5
    evaluated_on_bar_close: bool = True
    only_tightens: bool = True
    description: str = (
        "Trail = best_close - 1.5*ATR(14) for longs, best_close + 1.5*ATR(14) for shorts. "
        "Evaluated on each bar close. Stop can only tighten, never widen."
    )


@dataclass(frozen=True)
class BacktestEnvironment:
    """Complete backtest environment specification.

    Satisfies task requirement: 'Define the backtest environment explicitly.'
    Every field documents what was directly measured vs inferred.
    """

    # --- Engine ---
    engine_type: EngineType = EngineType.PYTHON_REPRODUCTION
    engine_description: str = (
        "Custom Python reproduction of the IRB strategy logic from strategy_spec.yaml v2.0.0. "
        "Not a TradingView Pine compiler — behavioural equivalence validated against "
        "spec rules and Phase 3 static analysis (134/134 rules traced)."
    )

    # --- Data source ---
    data_source: str = "OHLCV candle arrays (H1 + H4) supplied by caller"
    data_provider_note: str = (
        "For live backtest: MetaApi get_candles() from OANDA demo feed. For unit tests: synthetic candle fixtures."
    )

    # --- Symbol ---
    symbol_display: str = "EURUSD"
    symbol_broker: str = "EURUSD.sim"
    digits: int = 5
    pip_value: float = 0.0001
    point_value: float = 0.00001
    pip_value_per_standard_lot: float = 10.0

    # --- Timeframes ---
    primary_timeframe: str = "H1"
    higher_timeframe: str = "H4"
    h1_bars_per_day: int = 24
    h4_bars_per_day: int = 6
    h1_to_h4_ratio: int = 4

    # --- Session ---
    timezone: TimeZone = TimeZone.UTC
    trading_hours: str = "24/5"
    session_filter: str | None = None

    # --- Costs ---
    spread: SpreadAssumptions = field(default_factory=SpreadAssumptions)

    # --- Fill model ---
    fill_model: FillModel = FillModel.INTRA_BAR_STOP
    fill_description: str = (
        "Stop orders placed on bar N. On subsequent bars, if high >= buy-stop price "
        "(or low <= sell-stop price), order fills at the stop price. If bar opens past "
        "the stop level, fills at open (gap simulation)."
    )

    # --- Order simulation ---
    stop_order_sim: StopOrderSimulation = field(default_factory=StopOrderSimulation)
    pending_order_policy: PendingOrderPolicy = field(default_factory=PendingOrderPolicy)
    trailing_stop_sim: TrailingStopSimulation = field(default_factory=TrailingStopSimulation)

    # --- Position sizing ---
    initial_equity: float = 100_000.0
    risk_fraction: float = 0.01
    min_volume: float = 0.01
    max_volume: float = 1.00

    # --- Strategy parameters ---
    irb_threshold: float = 0.45
    ema_period: int = 20
    atr_period: int = 14
    adx_period: int = 14
    trend_slope_threshold: float = 0.4
    mtf_lookback: int = 5
    adx_threshold: float = 20.0
    overextension_threshold: float = 2.0
    trigger_window_bars: int = 20
    time_stop_bars: int = 40
    trail_atr_multiplier: float = 1.5
    warmup_bars: int = 34
    pip_buffer: float = 0.0001

    # --- Measurement vs inference ---
    directly_measured: tuple[str, ...] = (
        "Pine syntax/compile readiness (Phase 3 static analysis, 45 checks)",
        "Spec-to-code fidelity (Phase 3 alignment, 134/134 rules)",
        "Anti-repaint compliance (AR1-AR4)",
        "State machine completeness (5 states, all transitions)",
        "Signal generation logic (analytical trace)",
        "Position sizing formula correctness",
    )
    inferred_or_estimated: tuple[str, ...] = (
        "Trade frequency (from EURUSD H1 characteristics)",
        "Win rate / profit factor (from strategy structure)",
        "Drawdown (upper-bound estimate from risk-per-trade model)",
        "Fill rates for stop orders (estimated 40-60%)",
    )
    not_measured: tuple[str, ...] = (
        "Exact trade counts per window (requires live data)",
        "Exact P&L figures (requires live data)",
        "Sensitivity to parameter changes (requires parameter sweep)",
    )

    def to_dict(self) -> dict:
        """Serialise environment spec to a flat dictionary."""
        return {
            "engine_type": self.engine_type.value,
            "engine_description": self.engine_description,
            "data_source": self.data_source,
            "symbol": self.symbol_display,
            "symbol_broker": self.symbol_broker,
            "digits": self.digits,
            "pip_value": self.pip_value,
            "pip_value_per_standard_lot": self.pip_value_per_standard_lot,
            "primary_timeframe": self.primary_timeframe,
            "higher_timeframe": self.higher_timeframe,
            "timezone": self.timezone.value,
            "trading_hours": self.trading_hours,
            "spread_avg_pips": self.spread.avg_spread_pips,
            "slippage_pips": self.spread.slippage_pips,
            "commission_per_lot_usd": self.spread.commission_per_lot_usd,
            "fill_model": self.fill_model.value,
            "stop_order_simulation": self.stop_order_sim.description,
            "pending_order_expiry_bars": self.pending_order_policy.max_bars,
            "trailing_stop_atr_multiplier": self.trail_atr_multiplier,
            "initial_equity": self.initial_equity,
            "risk_fraction": self.risk_fraction,
            "warmup_bars": self.warmup_bars,
            "irb_threshold": self.irb_threshold,
            "trend_slope_threshold": self.trend_slope_threshold,
            "adx_threshold": self.adx_threshold,
            "overextension_threshold": self.overextension_threshold,
            "trigger_window_bars": self.trigger_window_bars,
            "time_stop_bars": self.time_stop_bars,
            "directly_measured": list(self.directly_measured),
            "inferred_or_estimated": list(self.inferred_or_estimated),
            "not_measured": list(self.not_measured),
        }


# Pre-built default environment matching strategy_spec.yaml v2.0.0
DEFAULT_ENVIRONMENT = BacktestEnvironment()
