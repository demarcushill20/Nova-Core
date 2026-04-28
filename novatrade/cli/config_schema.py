"""Pydantic v2 strategy configuration schema for YAML-based configs.

Defines StrategyConfig — the canonical representation of a strategy's tunable
parameters.  This schema enforces bounds, supports YAML serialisation, and
provides conversion to/from BacktestEnvironment kwargs.

Alpha/sizing separation: risk_fraction and position-sizing params are NOT
included here.  StrategyConfig owns signal generation and exit parameters only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, ClassVar

# ---------------------------------------------------------------------------
# YAML support — pyyaml required, hard dependency
# ---------------------------------------------------------------------------
import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Parameter bounds descriptor (for search engines)
# ---------------------------------------------------------------------------


class ParameterBounds(BaseModel):
    """Min/max/step bounds for a single optimisable parameter."""

    model_config = ConfigDict(frozen=True)

    min_val: float
    max_val: float
    step: float | None = None


# ---------------------------------------------------------------------------
# Strategy configuration
# ---------------------------------------------------------------------------


class StrategyConfig(BaseModel):
    """YAML-serialisable strategy configuration.

    Covers signal parameters, filter toggles, and metadata.
    Excludes sizing params (alpha/sizing separation).
    """

    model_config = ConfigDict(frozen=True)

    # --- Strategy type selector -------------------------------------------
    strategy_type: str = Field(
        default="irb",
        description="Strategy type identifier. Must match a registered strategy name.",
    )

    # --- Signal parameters (Level 1) ------------------------------------
    irb_threshold: float = Field(default=0.45, ge=0.30, le=0.60)
    ema_period: int = Field(default=20, ge=10, le=50)
    ema_fast_period: int = Field(default=10, ge=5, le=20)
    ema_slow_period: int = Field(default=50, ge=30, le=100)
    atr_period: int = Field(default=14, ge=7, le=21)
    adx_period: int = Field(default=14, ge=7, le=21)
    trend_slope_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    adx_threshold: float = Field(default=20.0, ge=0.0, le=30.0)
    overextension_threshold: float = Field(default=2.0, ge=1.5, le=3.0)
    trail_atr_multiplier: float = Field(default=1.5, ge=1.0, le=3.0)
    trail_step_pips: float = Field(default=10.0, ge=5.0, le=20.0)
    trail_cooldown_bars: int = Field(default=4, ge=1, le=10)
    trail_ema_period: int = Field(default=0, ge=0, le=100)
    ema_confirm_bars: int = Field(default=0, ge=0, le=10)
    use_ema_stack_filter: bool = Field(default=False)
    trigger_window_bars: int = Field(default=20, ge=10, le=240)  # le=240 for M5 (6x H1)
    time_stop_bars: int = Field(default=40, ge=20, le=480)  # le=480 for M5 (6x H1)
    mtf_lookback: int = Field(default=5, ge=1, le=50)
    warmup_bars: int = Field(default=34, ge=20, le=600)  # le=600 for M5 (6x H1)

    # --- Enhanced exit parameters (v4) ----------------------------------
    breakeven_r: float = Field(default=0.0, ge=0.0, le=3.0)
    trail_delay_bars: int = Field(default=0, ge=0, le=120)  # le=120 for M5 (6x H1)

    # --- Enhanced filter parameters (v4) --------------------------------
    use_volatility_filter: bool = Field(default=False)
    volatility_atr_ma_period: int = Field(default=50, ge=20, le=100)
    # --- Risk management (v4) -------------------------------------------
    max_consecutive_losses: int = Field(default=0, ge=0, le=20)

    # --- v5 features -----------------------------------------------------
    use_simple_trend_filter: bool = Field(default=False)
    ema_slope_lookback: int = Field(default=5, ge=1, le=20)
    partial_exit_enabled: bool = Field(default=False)
    partial_exit_pct: float = Field(default=50.0, ge=10.0, le=90.0)
    partial_r_target: float = Field(default=1.0, ge=0.5, le=3.0)
    max_trades_per_day: int = Field(default=0, ge=0, le=20)
    cooldown_bars: int = Field(default=0, ge=0, le=50)
    parity_audit_toggles: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("parity_audit_toggles", mode="before")
    @classmethod
    def _validate_parity_toggles(cls, v):
        if v is None:
            return frozenset()
        if isinstance(v, (list, tuple, set, frozenset)):
            return frozenset(str(x) for x in v)
        raise ValueError(
            "parity_audit_toggles must be a list/set of strings (or omitted). "
            "Live configs MUST leave this empty/omitted."
        )

    revalidate_pending: bool = Field(default=False)
    min_signal_atr_mult: float = Field(default=0.0, ge=0.0, le=2.0)
    # --- v5 Stagnation Guard (Pine USE_STAG / STAG_BARS / STAG_ATR) ---
    use_stagnation_guard: bool = Field(default=False)
    stag_bars: int = Field(default=12, ge=3, le=20)
    stag_atr_mult: float = Field(default=0.3, ge=0.1, le=2.0)
    # --- Stop-geometry overrides ----------------------------------------
    # Surfaced from BacktestEnvironment defaults so per-strategy YAMLs can
    # control stop placement. Pine v5 does NOT use either floor — set both to
    # 0.0 in pine-aligned configs to match Pine stop geometry exactly. The
    # current live champion keeps both at 1.0 to match historical engine
    # behaviour; do not change live defaults without parity revalidation.
    atr_sl_floor_multiplier: float = Field(default=1.0, ge=0.0, le=3.0)
    sl_spread_buffer_pips: float = Field(default=1.0, ge=0.0, le=5.0)

    # --- Level 2: filter toggles ----------------------------------------
    filters_enabled: list[str] = Field(default_factory=list)
    session_filter: str | None = None

    # --- Timeframe configuration -----------------------------------------
    primary_timeframe: str = Field(
        default="H1",
        description="Primary bar timeframe for signal generation (e.g. H1, M5).",
    )
    higher_timeframe: str = Field(
        default="H4",
        description="Higher timeframe for multi-timeframe analysis (e.g. H4 for H1, H1 for M5).",
    )

    # --- Metadata -------------------------------------------------------
    name: str = "irb_baseline"
    version: str = "1.0.0"
    description: str = ""
    parent_config: str | None = None  # lineage tracking

    # --- Class-level constants ------------------------------------------

    PARAMETER_BOUNDS: ClassVar[dict[str, ParameterBounds]] = {
        "irb_threshold": ParameterBounds(min_val=0.30, max_val=0.60, step=0.01),
        "ema_period": ParameterBounds(min_val=10, max_val=50, step=1),
        "ema_fast_period": ParameterBounds(min_val=5, max_val=20, step=1),
        "ema_slow_period": ParameterBounds(min_val=30, max_val=100, step=1),
        "atr_period": ParameterBounds(min_val=7, max_val=21, step=1),
        "adx_period": ParameterBounds(min_val=7, max_val=21, step=1),
        "trend_slope_threshold": ParameterBounds(min_val=0.1, max_val=1.0, step=0.05),
        "adx_threshold": ParameterBounds(min_val=15.0, max_val=30.0, step=0.5),
        "overextension_threshold": ParameterBounds(min_val=1.5, max_val=3.0, step=0.1),
        "trail_atr_multiplier": ParameterBounds(min_val=1.0, max_val=3.0, step=0.1),
        "trail_step_pips": ParameterBounds(min_val=5.0, max_val=20.0, step=1.0),
        "trail_cooldown_bars": ParameterBounds(min_val=1, max_val=10, step=1),
        "trail_ema_period": ParameterBounds(min_val=10, max_val=60, step=1),
        "ema_confirm_bars": ParameterBounds(min_val=1, max_val=5, step=1),
        "trigger_window_bars": ParameterBounds(min_val=10, max_val=40, step=1),
        "time_stop_bars": ParameterBounds(min_val=20, max_val=80, step=1),
        "mtf_lookback": ParameterBounds(min_val=1, max_val=50, step=1),
        "warmup_bars": ParameterBounds(min_val=20, max_val=100, step=1),
        "breakeven_r": ParameterBounds(min_val=0.5, max_val=3.0, step=0.1),
        "trail_delay_bars": ParameterBounds(min_val=1, max_val=20, step=1),
        "volatility_atr_ma_period": ParameterBounds(min_val=20, max_val=100, step=5),
        "max_consecutive_losses": ParameterBounds(min_val=3, max_val=20, step=1),
        # v5 params
        "ema_slope_lookback": ParameterBounds(min_val=1, max_val=20, step=1),
        "partial_exit_pct": ParameterBounds(min_val=10.0, max_val=90.0, step=5.0),
        "partial_r_target": ParameterBounds(min_val=0.5, max_val=3.0, step=0.1),
        "max_trades_per_day": ParameterBounds(min_val=1, max_val=20, step=1),
        "cooldown_bars": ParameterBounds(min_val=0, max_val=50, step=1),
        "min_signal_atr_mult": ParameterBounds(min_val=0.0, max_val=2.0, step=0.1),
    }

    # M5-specific parameter bounds for bar-based params.
    # Bar-count params (trigger_window, time_stop, warmup) scaled ~6x from H1
    # to cover approximately half the calendar-time range, allowing tighter
    # scalping configs on the faster timeframe.  trail_delay_bars uses 1-120
    # (min_val=1 consistent with H1).
    # Non-bar-based params (EMA periods, ATR periods, thresholds) stay the same.
    M5_PARAMETER_BOUNDS: ClassVar[dict[str, ParameterBounds]] = {
        "irb_threshold": ParameterBounds(min_val=0.30, max_val=0.60, step=0.01),
        "ema_period": ParameterBounds(min_val=10, max_val=50, step=1),
        "ema_fast_period": ParameterBounds(min_val=5, max_val=20, step=1),
        "ema_slow_period": ParameterBounds(min_val=30, max_val=100, step=1),
        "atr_period": ParameterBounds(min_val=7, max_val=21, step=1),
        "adx_period": ParameterBounds(min_val=7, max_val=21, step=1),
        "trend_slope_threshold": ParameterBounds(min_val=0.1, max_val=1.0, step=0.05),
        "adx_threshold": ParameterBounds(min_val=15.0, max_val=30.0, step=0.5),
        "overextension_threshold": ParameterBounds(min_val=1.5, max_val=3.0, step=0.1),
        "trail_atr_multiplier": ParameterBounds(min_val=1.0, max_val=3.0, step=0.1),
        "trail_step_pips": ParameterBounds(min_val=5.0, max_val=20.0, step=1.0),
        "trail_cooldown_bars": ParameterBounds(min_val=1, max_val=10, step=1),
        "trail_ema_period": ParameterBounds(min_val=10, max_val=60, step=1),
        "ema_confirm_bars": ParameterBounds(min_val=1, max_val=5, step=1),
        # Bar-based params: ~6x scaling from H1 (half the calendar-time range
        # to allow tighter scalping configurations on M5)
        "trigger_window_bars": ParameterBounds(min_val=60, max_val=240, step=1),
        "time_stop_bars": ParameterBounds(min_val=120, max_val=480, step=1),
        "mtf_lookback": ParameterBounds(min_val=1, max_val=50, step=1),
        "warmup_bars": ParameterBounds(min_val=120, max_val=600, step=1),
        "breakeven_r": ParameterBounds(min_val=0.5, max_val=3.0, step=0.1),
        # trail_delay_bars: min_val=1 consistent with H1 bounds
        "trail_delay_bars": ParameterBounds(min_val=1, max_val=120, step=1),
        "volatility_atr_ma_period": ParameterBounds(min_val=20, max_val=100, step=5),
        "max_consecutive_losses": ParameterBounds(min_val=3, max_val=20, step=1),
        # v5 params
        "ema_slope_lookback": ParameterBounds(min_val=1, max_val=20, step=1),
        "partial_exit_pct": ParameterBounds(min_val=10.0, max_val=90.0, step=5.0),
        "partial_r_target": ParameterBounds(min_val=0.5, max_val=3.0, step=0.1),
        "max_trades_per_day": ParameterBounds(min_val=1, max_val=20, step=1),
        "cooldown_bars": ParameterBounds(min_val=0, max_val=50, step=1),
        "min_signal_atr_mult": ParameterBounds(min_val=0.0, max_val=2.0, step=0.1),
    }

    OPTIMIZABLE_PARAMS: ClassVar[list[str]] = [
        "irb_threshold",
        "ema_period",
        "ema_fast_period",
        "ema_slow_period",
        "atr_period",
        "adx_period",
        "trend_slope_threshold",
        "adx_threshold",
        "overextension_threshold",
        "trail_atr_multiplier",
        "trail_step_pips",
        "trail_cooldown_bars",
        "trail_ema_period",
        "ema_confirm_bars",
        "trigger_window_bars",
        "time_stop_bars",
        "mtf_lookback",
        "warmup_bars",
        "breakeven_r",
        "trail_delay_bars",
        "volatility_atr_ma_period",
        "max_consecutive_losses",
        # v5 params
        "ema_slope_lookback",
        "partial_exit_pct",
        "partial_r_target",
        "max_trades_per_day",
        "cooldown_bars",
        "min_signal_atr_mult",
    ]

    # M5-specific optimizable params — same list, uses M5_PARAMETER_BOUNDS
    OPTIMIZABLE_PARAMS_M5: ClassVar[list[str]] = OPTIMIZABLE_PARAMS.copy()

    # --- Conversion methods ---------------------------------------------

    def to_environment_kwargs(self) -> dict[str, Any]:
        """Convert to BacktestEnvironment constructor kwargs.

        Returns only strategy parameters — no metadata, no filters.
        The caller is responsible for setting engine, cost model, and sizing.
        """
        return {
            "irb_threshold": self.irb_threshold,
            "ema_period": self.ema_period,
            "ema_fast_period": self.ema_fast_period,
            "ema_slow_period": self.ema_slow_period,
            "atr_period": self.atr_period,
            "adx_period": self.adx_period,
            "trend_slope_threshold": self.trend_slope_threshold,
            "adx_threshold": self.adx_threshold,
            "overextension_threshold": self.overextension_threshold,
            "trail_atr_multiplier": self.trail_atr_multiplier,
            "trail_step_pips": self.trail_step_pips,
            "trail_cooldown_bars": self.trail_cooldown_bars,
            "trail_ema_period": self.trail_ema_period,
            "ema_confirm_bars": self.ema_confirm_bars,
            "use_ema_stack_filter": self.use_ema_stack_filter,
            "trigger_window_bars": self.trigger_window_bars,
            "time_stop_bars": self.time_stop_bars,
            "mtf_lookback": self.mtf_lookback,
            "warmup_bars": self.warmup_bars,
            "breakeven_r": self.breakeven_r,
            "trail_delay_bars": self.trail_delay_bars,
            "use_volatility_filter": self.use_volatility_filter,
            "volatility_atr_ma_period": self.volatility_atr_ma_period,
            "max_consecutive_losses": self.max_consecutive_losses,
            # v5 params
            "use_simple_trend_filter": self.use_simple_trend_filter,
            "ema_slope_lookback": self.ema_slope_lookback,
            "partial_exit_enabled": self.partial_exit_enabled,
            "partial_exit_pct": self.partial_exit_pct,
            "partial_r_target": self.partial_r_target,
            "max_trades_per_day": self.max_trades_per_day,
            "cooldown_bars": self.cooldown_bars,
            "revalidate_pending": self.revalidate_pending,
            "min_signal_atr_mult": self.min_signal_atr_mult,
            "use_stagnation_guard": self.use_stagnation_guard,
            "stag_bars": self.stag_bars,
            "stag_atr_mult": self.stag_atr_mult,
            "atr_sl_floor_multiplier": self.atr_sl_floor_multiplier,
            "sl_spread_buffer_pips": self.sl_spread_buffer_pips,
            "parity_audit_toggles": self.parity_audit_toggles,
        }

    def content_hash(self) -> str:
        """SHA-256 content hash of all parameter values (16 hex chars).

        Deterministic: sorted keys, separators stripped.
        Used for dedup and cache keys.
        """
        params = self.to_environment_kwargs()
        # Include filter state in hash — filters change signal behaviour
        params["filters_enabled"] = sorted(self.filters_enabled)
        params["session_filter"] = self.session_filter
        raw = json.dumps(params, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @classmethod
    def from_yaml(cls, path: Path) -> StrategyConfig:
        """Load a StrategyConfig from a YAML file.

        Args:
            path: Path to the YAML config file.

        Returns:
            Validated StrategyConfig instance.

        Raises:
            FileNotFoundError: If path does not exist.
            yaml.YAMLError: If YAML is malformed.
            pydantic.ValidationError: If values violate bounds.
        """
        path = Path(path)
        with path.open("r") as f:
            data = yaml.safe_load(f)
        if data is None:
            data = {}
        return cls.model_validate(data)

    def to_yaml(self, path: Path) -> None:
        """Serialise this config to a YAML file.

        Creates parent directories if they don't exist.

        Args:
            path: Destination file path.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json")
        with path.open("w") as f:
            yaml.safe_dump(
                data,
                f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )

    @classmethod
    def from_environment(cls, env: Any) -> StrategyConfig:
        """Extract a StrategyConfig from an existing BacktestEnvironment.

        Reads strategy parameters from the environment dataclass and creates
        a StrategyConfig with those values.  Non-strategy fields (engine,
        costs, sizing) are ignored.

        Args:
            env: A BacktestEnvironment instance (or any object with matching
                 attribute names).

        Returns:
            StrategyConfig with parameter values from the environment.
        """
        kwargs: dict[str, Any] = {}
        for param in cls.OPTIMIZABLE_PARAMS:
            if hasattr(env, param):
                kwargs[param] = getattr(env, param)
        # Also extract non-optimisable params that exist on both
        if hasattr(env, "session_filter"):
            kwargs["session_filter"] = env.session_filter
        return cls.model_validate(kwargs)
