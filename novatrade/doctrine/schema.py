"""Strategy Doctrine schema — the structured format for strategy specifications.

A StrategyDoctrine captures everything the pipeline needs to evaluate a strategy:
concept description, entry/exit rules, mutable/immutable parameters, success
criteria, invalidation rules, execution assumptions, and data requirements.

The doctrine is a superset of StrategyConfig — it adds semantic structure
(what can vary, what cannot, what counts as success) on top of raw parameters.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class ParamBound(BaseModel):
    """Bounds for a single mutable parameter."""

    model_config = ConfigDict(frozen=True)

    default: float
    min: float
    max: float
    step: float | None = None

    @model_validator(mode="after")
    def _check_bounds(self) -> ParamBound:
        if self.min > self.max:
            raise ValueError(f"min ({self.min}) > max ({self.max})")
        if not (self.min <= self.default <= self.max):
            raise ValueError(f"default ({self.default}) not in [{self.min}, {self.max}]")
        if self.step is not None and self.step <= 0:
            raise ValueError(f"step must be positive, got {self.step}")
        return self


class DoctrineData(BaseModel):
    """Data requirements section."""

    model_config = ConfigDict(frozen=True)

    pair: str = Field(description="Currency pair, e.g. EURUSD")
    timeframes: list[str] = Field(
        default_factory=lambda: ["H1"],
        description="Primary + confirmation timeframes",
    )
    min_bars: int = Field(default=5000, ge=100)
    date_range: list[str] | None = Field(
        default=None,
        description="Optional [start, end] ISO date strings",
    )


class DoctrineExecution(BaseModel):
    """Execution assumptions — costs and fill model."""

    model_config = ConfigDict(frozen=True)

    spread_pips: float = Field(default=1.5, ge=0.0)
    slippage_pips: float = Field(default=0.5, ge=0.0)
    commission_per_lot: float = Field(default=7.0, ge=0.0)
    leverage: int = Field(default=30, ge=1)
    fill_policy: Literal["next_bar_open", "close_of_bar"] = "next_bar_open"


class DoctrineValidation(BaseModel):
    """Success criteria / validation thresholds."""

    model_config = ConfigDict(frozen=True)

    min_trades: int = Field(default=50, ge=1)
    min_months: int = Field(default=6, ge=1)
    max_drawdown_pct: float = Field(default=15.0, gt=0.0)
    min_profit_factor: float = Field(default=1.3, gt=0.0)
    min_sharpe: float = Field(default=0.5)
    walk_forward_windows: int = Field(default=6, ge=2)
    walk_forward_efficiency_min: float = Field(default=0.5, ge=0.0, le=1.0)
    max_pbo: float = Field(default=0.50, ge=0.0, le=1.0)


class DoctrineCampaign(BaseModel):
    """AutoResearch campaign configuration."""

    model_config = ConfigDict(frozen=True)

    max_experiments: int = Field(default=500, ge=1)
    search_strategy: Literal["auto", "random", "lhs", "bayesian", "ablation"] = "auto"
    wall_clock_limit_hours: float = Field(default=4.0, gt=0.0)
    stagnation_limit: int = Field(default=50, ge=1)


# ---------------------------------------------------------------------------
# Main Doctrine model
# ---------------------------------------------------------------------------


class StrategyDoctrine(BaseModel):
    """Complete strategy doctrine — single source of truth for the pipeline.

    Separates mutable parameters (optimizer can change) from immutable
    parameters (locked by the trader). Captures success criteria and
    invalidation rules alongside the strategy concept.
    """

    model_config = ConfigDict(frozen=True)

    # --- Meta ---------------------------------------------------------------
    name: str = Field(description="Short strategy name")
    version: str = Field(default="1.0.0")
    author: str = Field(default="nova")
    description: str = Field(default="")
    parent: str | None = Field(default=None, description="Parent doctrine ID for lineage")

    # --- Strategy concept ---------------------------------------------------
    concept: str = Field(description="Plain-text strategy description")
    setup_type: Literal["reversal", "breakout", "mean_reversion", "momentum", "trend_following"] = "reversal"
    entry_signal: str = Field(
        default="irb_rejection",
        description="Primary entry signal identifier",
    )
    confirmations: list[str] = Field(
        default_factory=lambda: ["ema_trend"],
        description="Confirmation indicator identifiers",
    )
    exit_method: str = Field(
        default="trailing_atr",
        description="Exit method identifier",
    )

    # --- Data requirements --------------------------------------------------
    data: DoctrineData

    # --- Parameters ---------------------------------------------------------
    mutable_params: dict[str, ParamBound] = Field(
        default_factory=dict,
        description="Parameters the optimizer is allowed to change",
    )
    immutable_params: dict[str, float | int | str | bool] = Field(
        default_factory=dict,
        description="Parameters locked by the trader",
    )
    extra_params: dict[str, Any] = Field(
        default_factory=dict,
        description="Escape hatch for future strategy types",
    )

    # --- Filters ------------------------------------------------------------
    filters_mandatory: list[str] = Field(
        default_factory=list,
        description="Filters that must always be enabled",
    )
    filters_optional: list[str] = Field(
        default_factory=list,
        description="Filters the optimizer may toggle",
    )
    filters_forbidden: list[str] = Field(
        default_factory=list,
        description="Filters that must never be enabled",
    )

    # --- Execution, validation, campaign ------------------------------------
    execution: DoctrineExecution = Field(default_factory=DoctrineExecution)
    validation: DoctrineValidation = Field(default_factory=DoctrineValidation)
    campaign: DoctrineCampaign = Field(default_factory=DoctrineCampaign)

    # --- Invalidation rules -------------------------------------------------
    invalidation_rules: list[str] = Field(
        default_factory=lambda: [
            "profit_factor < 1.0 after Stage A",
            "max_drawdown > 20% under stress",
        ],
        description="Conditions that kill the strategy entirely",
    )

    # --- Validation ---------------------------------------------------------

    @model_validator(mode="after")
    def _check_filter_consistency(self) -> StrategyDoctrine:
        """Ensure no filter appears in multiple categories."""
        mandatory = set(self.filters_mandatory)
        optional = set(self.filters_optional)
        forbidden = set(self.filters_forbidden)

        m_o = mandatory & optional
        if m_o:
            raise ValueError(f"Filters in both mandatory and optional: {m_o}")
        m_f = mandatory & forbidden
        if m_f:
            raise ValueError(f"Filters in both mandatory and forbidden: {m_f}")
        o_f = optional & forbidden
        if o_f:
            raise ValueError(f"Filters in both optional and forbidden: {o_f}")
        return self

    # --- Serialization ------------------------------------------------------

    def content_hash(self) -> str:
        """SHA-256 content hash (16 hex chars) for dedup and identity."""
        data = self.model_dump(mode="json")
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_yaml(self, path: Path) -> None:
        """Serialize to YAML file. Creates parent dirs if needed."""
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
    def from_yaml(cls, path: Path) -> StrategyDoctrine:
        """Load and validate a doctrine from a YAML file."""
        path = Path(path)
        with path.open("r") as f:
            data = yaml.safe_load(f)
        if data is None:
            data = {}
        return cls.model_validate(data)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (JSON-compatible types)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyDoctrine:
        """Load from a plain dict."""
        return cls.model_validate(data)
