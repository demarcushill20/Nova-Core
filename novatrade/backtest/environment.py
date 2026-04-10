"""Backtest environment definition for IRB strategy validation.

Documents the exact test environment configuration used for backtesting,
including data source, symbol mapping, timeframes, spread/commission/slippage
assumptions, fill model, and stop-order simulation behaviour.

This module is the single source of truth for backtest environment parameters,
ensuring reproducibility across runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)


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
    # Non-zero defaults ensure backtests always model real execution costs.
    # Zero-cost backtests are rejected by the Stage B realism gate.
    commission_per_lot_usd: float = 3.5  # typical ECN round-turn commission (USD)
    slippage_pips: float = 0.2  # conservative estimate for major pairs
    description: str = "OANDA demo typical 0.5-1.5 pips; ECN commission + slippage modelled"

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
    risk_fraction: float = 0.015  # 1.5% risk per trade ($1,500 on $100K)
    min_volume: float = 0.01
    max_volume: float = 50.00  # Maximum for $100K accounts

    # --- Strategy parameters ---
    # Tightened from 0.45 per P1.2: strict rejection quality, reducing false signals
    irb_threshold: float = 0.40
    ema_period: int = 20
    ema_fast_period: int = 10
    ema_slow_period: int = 50
    atr_period: int = 14
    adx_period: int = 14
    trend_slope_threshold: float = 0.4
    mtf_lookback: int = 5
    adx_threshold: float = 25.0
    overextension_threshold: float = 2.0
    trigger_window_bars: int = 20
    time_stop_bars: int = 40
    # Increased from 1.5 per P0.1: wider trails reduce premature stop-outs in high-vol regime
    trail_atr_multiplier: float = 2.0
    trail_step_pips: float = 10.0  # minimum distance to move before updating SL
    trail_cooldown_bars: int = 4  # minimum bars between SL modifications
    trail_ema_period: int = 0  # 0 = use ATR trailing; >0 = use EMA trailing
    ema_confirm_bars: int = 2  # 0 = disabled; >0 = N bars closing above/below EMA fast
    use_ema_stack_filter: bool = False  # EMA 10 > 20 > 50 ordering
    warmup_bars: int = 34
    pip_buffer: float = 0.0001

    # --- Enhanced exit parameters (v4) ---
    breakeven_r: float = 1.0  # 0 = disabled; >0 = move SL to breakeven after N*R profit
    trail_delay_bars: int = 0  # 0 = trail immediately; >0 = don't trail for first N bars

    # --- Enhanced filter parameters (v4) ---
    use_volatility_filter: bool = True  # Only trade when ATR > SMA(ATR, vol_atr_ma_period)
    volatility_atr_ma_period: int = 50  # Period for ATR moving average filter
    # --- Risk management (v4) ---
    max_consecutive_losses: int = 0  # 0 = disabled; >0 = pause trading after N consecutive losses

    # --- v5 features ---
    use_simple_trend_filter: bool = False  # v5: ema20>ema50 + direction vs normalized slope
    ema_slope_lookback: int = 5  # v5: bars to look back for EMA direction
    partial_exit_enabled: bool = False  # v5: close partial at R target
    partial_exit_pct: float = 50.0  # v5: percent of position to close at target
    partial_r_target: float = 1.0  # v5: R-multiple for partial profit target
    max_trades_per_day: int = 0  # v5: daily trade limit (0=disabled)
    cooldown_bars: int = 0  # v5: bars to wait after going flat (0=disabled)
    revalidate_pending: bool = False  # v5: re-check trend/HTF each bar for pending orders
    min_signal_atr_mult: float = 0.0  # v5: min signal range / ATR (0=disabled)

    # --- ATR-adaptive stop loss (Quick Win) ---
    atr_sl_floor_multiplier: float = 1.0  # min SL distance = ATR * this multiplier (0=disabled)

    # --- Spread-aware stop loss cushion ---
    # Extra SL distance (in pips) added beyond the candle wick + pip_buffer so the
    # broker's spread-adjusted trigger (bid for longs, ask for shorts) doesn't
    # stop the trade out prematurely. 0 = disabled (legacy behaviour).
    # NOTE: backtest also deducts avg_spread_pips from PnL at exit, so the spread
    # cost is modeled twice — once in SL distance and once in PnL. This makes
    # backtest results conservatively pessimistic vs live execution.
    sl_spread_buffer_pips: float = 1.0

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

    # --- Timeframe constants for factory methods ---
    _TIMEFRAME_MINUTES: ClassVar[dict[str, int]] = {
        "M1": 1,
        "M5": 5,
        "M15": 15,
        "M30": 30,
        "H1": 60,
        "H4": 240,
        "D1": 1440,
    }

    @classmethod
    def for_m5(cls, **overrides: object) -> BacktestEnvironment:
        """Factory for M5-primary / H1-higher backtest environment.

        Sets timeframe fields for M5:H1 pair (ratio=12, 288 bars/day on M5,
        24 bars/day on H1).  All other fields default or can be overridden.
        """
        defaults = {
            "primary_timeframe": "M5",
            "higher_timeframe": "H1",
            "h1_to_h4_ratio": 12,
            "h1_bars_per_day": 288,
            "h4_bars_per_day": 24,
        }
        defaults.update(overrides)  # type: ignore[arg-type]
        return cls(**defaults)  # type: ignore[arg-type]

    @classmethod
    def for_timeframe(
        cls,
        primary_tf: str,
        higher_tf: str,
        **overrides: object,
    ) -> BacktestEnvironment:
        """Generic factory that auto-computes ratio and bars/day from timeframe strings.

        Supported timeframe strings: M1, M5, M15, M30, H1, H4, D1.

        Args:
            primary_tf: Primary (lower) timeframe string, e.g. "M5".
            higher_tf:  Higher timeframe string, e.g. "H1".
            **overrides: Additional field overrides.

        Returns:
            BacktestEnvironment configured for the given timeframe pair.

        Raises:
            ValueError: If timeframe strings are unrecognised or ratio is invalid.
        """
        tf_map = cls._TIMEFRAME_MINUTES
        primary_minutes = tf_map.get(primary_tf)
        higher_minutes = tf_map.get(higher_tf)

        if primary_minutes is None:
            raise ValueError(f"Unknown primary timeframe: {primary_tf!r}")
        if higher_minutes is None:
            raise ValueError(f"Unknown higher timeframe: {higher_tf!r}")
        if higher_minutes <= primary_minutes:
            raise ValueError(
                f"Higher timeframe ({higher_tf}={higher_minutes}m) must be larger "
                f"than primary ({primary_tf}={primary_minutes}m)"
            )
        if higher_minutes % primary_minutes != 0:
            raise ValueError(
                f"Higher timeframe ({higher_tf}={higher_minutes}m) must be evenly "
                f"divisible by primary ({primary_tf}={primary_minutes}m)"
            )

        ratio = higher_minutes // primary_minutes
        primary_bars_per_day = (24 * 60) // primary_minutes
        higher_bars_per_day = (24 * 60) // higher_minutes

        defaults = {
            "primary_timeframe": primary_tf,
            "higher_timeframe": higher_tf,
            "h1_to_h4_ratio": ratio,
            "h1_bars_per_day": primary_bars_per_day,
            "h4_bars_per_day": higher_bars_per_day,
        }
        defaults.update(overrides)  # type: ignore[arg-type]
        return cls(**defaults)  # type: ignore[arg-type]

    @classmethod
    def for_v5_m5(cls, **overrides: object) -> BacktestEnvironment:
        """Factory for IRB v5 Relaxed Reliable Build on M5:H1."""
        defaults = {
            "primary_timeframe": "M5",
            "higher_timeframe": "H1",
            "h1_to_h4_ratio": 12,
            "h1_bars_per_day": 288,
            "h4_bars_per_day": 24,
            # v5 strategy parameters
            "irb_threshold": 0.45,
            "ema_period": 20,
            "ema_fast_period": 20,
            "ema_slow_period": 50,
            "atr_period": 14,
            "adx_period": 14,
            "trend_slope_threshold": 0.0,  # not used in simple mode
            "adx_threshold": 0.0,  # v5 doesn't use ADX
            "overextension_threshold": 2.5,
            "trigger_window_bars": 12,
            "time_stop_bars": 20,
            "trail_atr_multiplier": 2.0,
            "warmup_bars": 60,
            "breakeven_r": 1.0,
            "use_ema_stack_filter": False,
            "use_simple_trend_filter": True,
            "ema_slope_lookback": 5,
            "partial_exit_enabled": True,
            "partial_exit_pct": 50.0,
            "partial_r_target": 1.0,
            "max_trades_per_day": 3,
            "cooldown_bars": 1,
            "revalidate_pending": True,
            "min_signal_atr_mult": 0.0,
            "max_consecutive_losses": 0,
        }
        defaults.update(overrides)  # type: ignore[arg-type]
        return cls(**defaults)  # type: ignore[arg-type]

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
            "use_simple_trend_filter": self.use_simple_trend_filter,
            "partial_exit_enabled": self.partial_exit_enabled,
            "partial_exit_pct": self.partial_exit_pct,
            "partial_r_target": self.partial_r_target,
            "max_trades_per_day": self.max_trades_per_day,
            "cooldown_bars": self.cooldown_bars,
            "revalidate_pending": self.revalidate_pending,
            "directly_measured": list(self.directly_measured),
            "inferred_or_estimated": list(self.inferred_or_estimated),
            "not_measured": list(self.not_measured),
        }

    @classmethod
    def from_champion_config(cls, config_path: str | Path) -> BacktestEnvironment:
        """Load BacktestEnvironment from champion strategy configuration file.

        Args:
            config_path: Path to the champion strategy YAML file

        Returns:
            BacktestEnvironment configured with champion parameters

        Raises:
            FileNotFoundError: If config file doesn't exist
            ImportError: If PyYAML is not available
            ValueError: If config format is invalid
        """
        if yaml is None:
            raise ImportError("PyYAML is required for champion config loading: pip install PyYAML")

        config_path = Path(config_path)
        if not config_path.exists():
            raise FileNotFoundError(f"Champion config not found: {config_path}")

        log.info(f"Loading champion config from {config_path}")

        try:
            with open(config_path) as f:
                config = yaml.safe_load(f)
        except Exception as e:
            raise ValueError(f"Failed to parse champion config {config_path}: {e}") from e

        # Extract strategy parameters and map to BacktestEnvironment fields
        overrides: dict[str, Any] = {}

        # Map champion config fields to environment fields
        field_mapping = {
            "primary_timeframe": "primary_timeframe",
            "higher_timeframe": "higher_timeframe",
            "irb_threshold": "irb_threshold",
            "ema_period": "ema_period",
            "ema_fast_period": "ema_fast_period",
            "ema_slow_period": "ema_slow_period",
            "atr_period": "atr_period",
            "adx_period": "adx_period",
            "adx_threshold": "adx_threshold",
            "overextension_threshold": "overextension_threshold",
            "trigger_window_bars": "trigger_window_bars",
            "time_stop_bars": "time_stop_bars",
            "trail_atr_multiplier": "trail_atr_multiplier",
            "use_simple_trend_filter": "use_simple_trend_filter",
            "ema_slope_lookback": "ema_slope_lookback",
            "use_ema_stack_filter": "use_ema_stack_filter",
            "partial_exit_enabled": "partial_exit_enabled",
            "partial_exit_pct": "partial_exit_pct",
            "partial_r_target": "partial_r_target",
            "max_trades_per_day": "max_trades_per_day",
            "cooldown_bars": "cooldown_bars",
            "revalidate_pending": "revalidate_pending",
            "min_signal_atr_mult": "min_signal_atr_mult",
            "max_consecutive_losses": "max_consecutive_losses",
            "breakeven_r": "breakeven_r",
            "trail_delay_bars": "trail_delay_bars",
            "use_volatility_filter": "use_volatility_filter",
        }

        # Apply mappings from config
        for config_key, env_field in field_mapping.items():
            if config_key in config:
                overrides[env_field] = config[config_key]

        # Special handling for timeframes - compute ratios
        if "primary_timeframe" in overrides and "higher_timeframe" in overrides:
            return cls.for_timeframe(overrides.pop("primary_timeframe"), overrides.pop("higher_timeframe"), **overrides)
        else:
            return cls(**overrides)  # type: ignore[arg-type]

    @classmethod
    def from_current_champion(cls) -> BacktestEnvironment:
        """Load BacktestEnvironment from current champion registry.

        Reads the champion registry and loads the current champion configuration.
        Falls back to default v5 M5 champion if registry is not available.

        Returns:
            BacktestEnvironment configured with current champion parameters
        """
        champion_registry_path = Path("configs/champion_registry.yaml")

        if not champion_registry_path.exists():
            log.warning("Champion registry not found, using default v5 M5 champion")
            return cls.for_v5_m5()

        try:
            if yaml is None:
                log.warning("PyYAML not available, using default v5 M5 champion")
                return cls.for_v5_m5()

            with open(champion_registry_path) as f:
                registry = yaml.safe_load(f)

            current_champion = registry.get("current_champion", {})
            config_path = current_champion.get("config_path")

            if config_path:
                log.info(
                    f"Loading current champion: {current_champion.get('label', 'Unknown')} "
                    f"v{current_champion.get('version', 'Unknown')}"
                )
                return cls.from_champion_config(config_path)
            else:
                log.warning("No config_path in champion registry, using default v5 M5 champion")
                return cls.for_v5_m5()

        except Exception as e:
            log.error(f"Failed to load champion registry: {e}")
            log.warning("Falling back to default v5 M5 champion")
            return cls.for_v5_m5()


# Pre-built default environment matching strategy_spec.yaml v2.0.0
DEFAULT_ENVIRONMENT = BacktestEnvironment()

# Champion environment - loads current champion parameters
CHAMPION_ENVIRONMENT = BacktestEnvironment.from_current_champion()
