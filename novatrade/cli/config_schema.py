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
from pydantic import BaseModel, ConfigDict, Field

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
    atr_period: int = Field(default=14, ge=7, le=21)
    adx_period: int = Field(default=14, ge=7, le=21)
    trend_slope_threshold: float = Field(default=0.4, ge=0.1, le=1.0)
    adx_threshold: float = Field(default=20.0, ge=15.0, le=30.0)
    overextension_threshold: float = Field(default=2.0, ge=1.5, le=3.0)
    trail_atr_multiplier: float = Field(default=1.5, ge=1.0, le=3.0)
    trigger_window_bars: int = Field(default=20, ge=10, le=40)
    time_stop_bars: int = Field(default=40, ge=20, le=80)
    mtf_lookback: int = Field(default=5, ge=1, le=20)
    warmup_bars: int = Field(default=34, ge=20, le=60)

    # --- Level 2: filter toggles ----------------------------------------
    filters_enabled: list[str] = Field(default_factory=list)
    session_filter: str | None = None

    # --- Metadata -------------------------------------------------------
    name: str = "irb_baseline"
    version: str = "1.0.0"
    description: str = ""
    parent_config: str | None = None  # lineage tracking

    # --- Class-level constants ------------------------------------------

    PARAMETER_BOUNDS: ClassVar[dict[str, ParameterBounds]] = {
        "irb_threshold": ParameterBounds(min_val=0.30, max_val=0.60, step=0.01),
        "ema_period": ParameterBounds(min_val=10, max_val=50, step=1),
        "atr_period": ParameterBounds(min_val=7, max_val=21, step=1),
        "adx_period": ParameterBounds(min_val=7, max_val=21, step=1),
        "trend_slope_threshold": ParameterBounds(min_val=0.1, max_val=1.0, step=0.05),
        "adx_threshold": ParameterBounds(min_val=15.0, max_val=30.0, step=0.5),
        "overextension_threshold": ParameterBounds(min_val=1.5, max_val=3.0, step=0.1),
        "trail_atr_multiplier": ParameterBounds(min_val=1.0, max_val=3.0, step=0.1),
        "trigger_window_bars": ParameterBounds(min_val=10, max_val=40, step=1),
        "time_stop_bars": ParameterBounds(min_val=20, max_val=80, step=1),
        "mtf_lookback": ParameterBounds(min_val=1, max_val=20, step=1),
        "warmup_bars": ParameterBounds(min_val=20, max_val=60, step=1),
    }

    OPTIMIZABLE_PARAMS: ClassVar[list[str]] = [
        "irb_threshold",
        "ema_period",
        "atr_period",
        "adx_period",
        "trend_slope_threshold",
        "adx_threshold",
        "overextension_threshold",
        "trail_atr_multiplier",
        "trigger_window_bars",
        "time_stop_bars",
        "mtf_lookback",
        "warmup_bars",
    ]

    # --- Conversion methods ---------------------------------------------

    def to_environment_kwargs(self) -> dict[str, Any]:
        """Convert to BacktestEnvironment constructor kwargs.

        Returns only strategy parameters — no metadata, no filters.
        The caller is responsible for setting engine, cost model, and sizing.
        """
        return {
            "irb_threshold": self.irb_threshold,
            "ema_period": self.ema_period,
            "atr_period": self.atr_period,
            "adx_period": self.adx_period,
            "trend_slope_threshold": self.trend_slope_threshold,
            "adx_threshold": self.adx_threshold,
            "overextension_threshold": self.overextension_threshold,
            "trail_atr_multiplier": self.trail_atr_multiplier,
            "trigger_window_bars": self.trigger_window_bars,
            "time_stop_bars": self.time_stop_bars,
            "mtf_lookback": self.mtf_lookback,
            "warmup_bars": self.warmup_bars,
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
