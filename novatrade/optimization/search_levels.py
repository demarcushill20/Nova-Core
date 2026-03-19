"""Constrained search-level DSL for AutoResearch parameter exploration.

Three tiers of search freedom, each auditable and bounded:

Level 1 — Signal parameters only.
    Optimise values within PARAMETER_BOUNDS.  Excludes risk_fraction,
    anything touching accounting/execution, and anything that changes
    data alignment.

Level 2 — Rule toggles via constrained DSL.
    Enable/disable registered filters, choose threshold presets,
    select exit-rule variants, toggle session filters, and regime
    switch toggles.  No freeform code edits.

Level 3 — Structural changes through templates.
    Choose from registered indicator modules and registered composition
    patterns.  Reduces search entropy and keeps auditability high.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Search level enum
# ---------------------------------------------------------------------------


class SearchLevel(Enum):
    """Which tier of search freedom the agent is authorised to use."""

    LEVEL_1 = "parameters_only"
    LEVEL_2 = "rule_toggles"
    LEVEL_3 = "structural_templates"


# ---------------------------------------------------------------------------
# Level 1: Parameter-only search (signal tuning)
# ---------------------------------------------------------------------------

# These parameters are ALWAYS excluded from Level 1 optimisation.
# They affect accounting, execution, or data alignment — not signal quality.
EXCLUDED_FROM_LEVEL1: frozenset[str] = frozenset(
    {
        "risk_fraction",  # sizing pass — separate concern
        "initial_equity",  # accounting invariant
        "commission_per_lot_usd",  # execution cost model
        "slippage_pips",  # execution cost model
        "avg_spread_pips",  # execution cost model
        "stop_first_on_ambiguity",  # fill model policy
        "warmup_offset",  # data alignment
        "data_start_index",  # data alignment
        "candle_alignment_mode",  # data alignment
    }
)


def validate_level1_params(candidate: dict[str, Any]) -> list[str]:
    """Check that a candidate dict contains only Level 1 parameters.

    Returns a list of violation descriptions (empty = clean).
    """
    from novatrade.cli.config_schema import StrategyConfig

    violations: list[str] = []
    allowed = set(StrategyConfig.OPTIMIZABLE_PARAMS)

    for key in candidate:
        if key in EXCLUDED_FROM_LEVEL1:
            violations.append(
                f"'{key}' is excluded from Level 1 (affects "
                f"{'sizing' if key == 'risk_fraction' else 'accounting/execution/alignment'})"
            )
        elif key not in allowed:
            violations.append(f"'{key}' is not in OPTIMIZABLE_PARAMS — Level 1 only permits signal parameters")
    return violations


# ---------------------------------------------------------------------------
# Level 2: Rule toggle DSL
# ---------------------------------------------------------------------------

# Registered filter modules — the ONLY filters the agent may toggle.
# Each entry maps a filter name to its description and default state.
REGISTERED_FILTERS: dict[str, dict[str, Any]] = {
    "trend_alignment": {
        "description": "Require H4 trend alignment before H1 entry",
        "default_enabled": True,
        "category": "directional",
    },
    "adx_strength": {
        "description": "Require ADX above threshold for trend strength",
        "default_enabled": True,
        "category": "volatility",
    },
    "overextension_guard": {
        "description": "Reject entries when price is overextended from EMA",
        "default_enabled": True,
        "category": "mean_reversion",
    },
    "session_london": {
        "description": "Only trade during London session (07:00-16:00 UTC)",
        "default_enabled": False,
        "category": "session",
    },
    "session_newyork": {
        "description": "Only trade during New York session (13:00-21:00 UTC)",
        "default_enabled": False,
        "category": "session",
    },
    "session_london_ny_overlap": {
        "description": "Only trade during London-NY overlap (13:00-16:00 UTC)",
        "default_enabled": False,
        "category": "session",
    },
    "regime_trending_only": {
        "description": "Only trade when regime detector indicates trending",
        "default_enabled": False,
        "category": "regime",
    },
    "regime_low_volatility_skip": {
        "description": "Skip entries during low-volatility regime",
        "default_enabled": False,
        "category": "regime",
    },
    "friday_close": {
        "description": "Close all positions before Friday market close",
        "default_enabled": False,
        "category": "session",
    },
    "news_blackout": {
        "description": "Pause trading around high-impact news events",
        "default_enabled": False,
        "category": "event",
    },
}

# Threshold presets — named sets of threshold values the agent may choose from.
THRESHOLD_PRESETS: dict[str, dict[str, float]] = {
    "conservative": {
        "irb_threshold": 0.50,
        "adx_threshold": 25.0,
        "overextension_threshold": 2.5,
        "trend_slope_threshold": 0.6,
    },
    "moderate": {
        "irb_threshold": 0.45,
        "adx_threshold": 20.0,
        "overextension_threshold": 2.0,
        "trend_slope_threshold": 0.4,
    },
    "aggressive": {
        "irb_threshold": 0.35,
        "adx_threshold": 17.0,
        "overextension_threshold": 1.7,
        "trend_slope_threshold": 0.2,
    },
}

# Exit-rule variants — registered exit strategies the agent may choose from.
EXIT_RULE_VARIANTS: dict[str, dict[str, Any]] = {
    "trailing_tight": {
        "description": "Tight trailing stop (1.0 ATR)",
        "trail_atr_multiplier": 1.0,
        "time_stop_bars": 30,
    },
    "trailing_standard": {
        "description": "Standard trailing stop (1.5 ATR)",
        "trail_atr_multiplier": 1.5,
        "time_stop_bars": 40,
    },
    "trailing_wide": {
        "description": "Wide trailing stop (2.5 ATR), long time stop",
        "trail_atr_multiplier": 2.5,
        "time_stop_bars": 60,
    },
    "time_only": {
        "description": "Time stop only, no trailing (3.0 ATR = effectively off)",
        "trail_atr_multiplier": 3.0,
        "time_stop_bars": 40,
    },
}

# Session filter choices
SESSION_FILTER_CHOICES: list[str | None] = [
    None,  # no session filter
    "london",  # London session only
    "newyork",  # New York session only
    "london_ny_overlap",  # overlap only
    "asian",  # Asian session only
]


@dataclass(frozen=True)
class Level2Toggle:
    """A single Level 2 rule toggle decision."""

    toggle_type: str  # "filter" | "threshold_preset" | "exit_variant" | "session" | "regime"
    name: str  # registered name
    enabled: bool = True
    value: Any = None  # for threshold presets: the preset name; for session: the choice

    def __post_init__(self) -> None:
        valid_types = {"filter", "threshold_preset", "exit_variant", "session", "regime"}
        if self.toggle_type not in valid_types:
            raise ValueError(f"Unknown toggle_type '{self.toggle_type}', must be one of {valid_types}")


def validate_level2_toggles(toggles: list[Level2Toggle]) -> list[str]:
    """Validate that all Level 2 toggles reference registered modules.

    Returns list of violation descriptions (empty = clean).
    """
    violations: list[str] = []

    for t in toggles:
        if t.toggle_type == "filter":
            if t.name not in REGISTERED_FILTERS:
                violations.append(
                    f"Filter '{t.name}' is not registered. Available: {sorted(REGISTERED_FILTERS.keys())}"
                )
        elif t.toggle_type == "threshold_preset":
            if t.value not in THRESHOLD_PRESETS:
                violations.append(
                    f"Threshold preset '{t.value}' is not registered. Available: {sorted(THRESHOLD_PRESETS.keys())}"
                )
        elif t.toggle_type == "exit_variant":
            if t.name not in EXIT_RULE_VARIANTS:
                violations.append(
                    f"Exit variant '{t.name}' is not registered. Available: {sorted(EXIT_RULE_VARIANTS.keys())}"
                )
        elif t.toggle_type == "session" and t.value not in SESSION_FILTER_CHOICES:
            violations.append(f"Session filter '{t.value}' is not registered. Available: {SESSION_FILTER_CHOICES}")

    return violations


def apply_level2_toggles(
    base_params: dict[str, Any],
    toggles: list[Level2Toggle],
) -> dict[str, Any]:
    """Apply Level 2 toggles to a base parameter dict.

    Modifies only registered fields; unrecognised toggles are rejected
    by validate_level2_toggles (call that first).

    Returns a new dict (does not mutate base_params).
    """
    result = dict(base_params)
    filters_enabled: list[str] = list(result.get("filters_enabled", []))
    session_filter: str | None = result.get("session_filter")

    for t in toggles:
        if t.toggle_type == "filter":
            if t.enabled and t.name not in filters_enabled:
                filters_enabled.append(t.name)
            elif not t.enabled and t.name in filters_enabled:
                filters_enabled.remove(t.name)

        elif t.toggle_type == "threshold_preset":
            preset = THRESHOLD_PRESETS.get(t.value, {})
            result.update(preset)

        elif t.toggle_type == "exit_variant":
            variant = EXIT_RULE_VARIANTS.get(t.name, {})
            result.update({k: v for k, v in variant.items() if k != "description"})

        elif t.toggle_type == "session":
            session_filter = t.value

    result["filters_enabled"] = filters_enabled
    result["session_filter"] = session_filter
    return result


# ---------------------------------------------------------------------------
# Level 3: Structural templates (registered modules + composition patterns)
# ---------------------------------------------------------------------------

# Registered indicator modules — the agent chooses from these, not freeform.
REGISTERED_INDICATORS: dict[str, dict[str, Any]] = {
    "irb": {
        "description": "Imbalance/Recovery/Breakout — primary signal",
        "module": "novatrade.backtest.engine",
        "params": ["irb_threshold", "trigger_window_bars"],
        "category": "signal",
    },
    "ema_trend": {
        "description": "EMA trend direction and slope",
        "module": "novatrade.backtest.engine",
        "params": ["ema_period", "trend_slope_threshold"],
        "category": "trend",
    },
    "adx_filter": {
        "description": "ADX trend strength filter",
        "module": "novatrade.backtest.engine",
        "params": ["adx_period", "adx_threshold"],
        "category": "volatility",
    },
    "atr_sizing": {
        "description": "ATR-based stop distance and trailing",
        "module": "novatrade.backtest.engine",
        "params": ["atr_period", "trail_atr_multiplier"],
        "category": "risk",
    },
    "mtf_confluence": {
        "description": "Multi-timeframe confluence check",
        "module": "novatrade.backtest.engine",
        "params": ["mtf_lookback"],
        "category": "confluence",
    },
}

# Registered composition patterns — how indicators combine.
REGISTERED_COMPOSITIONS: dict[str, dict[str, Any]] = {
    "irb_trend_adx": {
        "description": "IRB signal + EMA trend + ADX strength (default)",
        "indicators": ["irb", "ema_trend", "adx_filter", "atr_sizing"],
        "logic": "irb AND ema_trend AND adx_filter",
    },
    "irb_trend_mtf": {
        "description": "IRB signal + EMA trend + MTF confluence",
        "indicators": ["irb", "ema_trend", "mtf_confluence", "atr_sizing"],
        "logic": "irb AND ema_trend AND mtf_confluence",
    },
    "irb_minimal": {
        "description": "IRB signal + ATR only (fewest parameters)",
        "indicators": ["irb", "atr_sizing"],
        "logic": "irb_only",
    },
    "irb_full": {
        "description": "All indicators combined",
        "indicators": ["irb", "ema_trend", "adx_filter", "atr_sizing", "mtf_confluence"],
        "logic": "irb AND ema_trend AND adx_filter AND mtf_confluence",
    },
}


@dataclass(frozen=True)
class Level3Template:
    """A structural template selection for Level 3."""

    composition: str  # key into REGISTERED_COMPOSITIONS
    indicator_overrides: dict[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.composition not in REGISTERED_COMPOSITIONS:
            raise ValueError(
                f"Composition '{self.composition}' not registered. Available: {sorted(REGISTERED_COMPOSITIONS.keys())}"
            )


def validate_level3_template(template: Level3Template) -> list[str]:
    """Validate a Level 3 structural template selection.

    Returns list of violation descriptions (empty = clean).
    """
    violations: list[str] = []

    if template.composition not in REGISTERED_COMPOSITIONS:
        violations.append(
            f"Composition '{template.composition}' not registered. Available: {sorted(REGISTERED_COMPOSITIONS.keys())}"
        )
        return violations

    comp = REGISTERED_COMPOSITIONS[template.composition]
    registered_indicators = set(comp["indicators"])

    for ind_name in template.indicator_overrides:
        if ind_name not in REGISTERED_INDICATORS:
            violations.append(
                f"Indicator '{ind_name}' is not registered. Available: {sorted(REGISTERED_INDICATORS.keys())}"
            )
        elif ind_name not in registered_indicators:
            violations.append(
                f"Indicator '{ind_name}' is not part of composition "
                f"'{template.composition}'. "
                f"Included: {sorted(registered_indicators)}"
            )

    return violations


def get_active_params_for_template(template: Level3Template) -> list[str]:
    """Return the list of optimisable parameters for a given template.

    Only parameters belonging to active indicators are included.
    This constrains Level 1 search to the template's parameter space.
    """
    comp = REGISTERED_COMPOSITIONS.get(template.composition, {})
    active_indicators = set(comp.get("indicators", []))

    # Apply overrides
    for ind_name, enabled in template.indicator_overrides.items():
        if enabled:
            active_indicators.add(ind_name)
        else:
            active_indicators.discard(ind_name)

    params: list[str] = []
    for ind_name in active_indicators:
        ind = REGISTERED_INDICATORS.get(ind_name, {})
        params.extend(ind.get("params", []))

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for p in params:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


# ---------------------------------------------------------------------------
# Unified search-level validator
# ---------------------------------------------------------------------------


@dataclass
class SearchLevelConfig:
    """Complete search-level configuration for a campaign."""

    level: SearchLevel = SearchLevel.LEVEL_1
    level2_toggles: list[Level2Toggle] = field(default_factory=list)
    level3_template: Level3Template | None = None

    def validate(self) -> list[str]:
        """Validate the entire search-level configuration.

        Returns list of violations (empty = valid).
        """
        violations: list[str] = []

        if self.level == SearchLevel.LEVEL_2:
            violations.extend(validate_level2_toggles(self.level2_toggles))

        if self.level == SearchLevel.LEVEL_3:
            if self.level3_template is None:
                violations.append("Level 3 requires a template selection")
            else:
                violations.extend(validate_level3_template(self.level3_template))

        return violations

    def get_optimisable_params(self) -> list[str]:
        """Return the parameter list appropriate for this search level."""
        from novatrade.cli.config_schema import StrategyConfig

        if self.level == SearchLevel.LEVEL_3 and self.level3_template:
            return get_active_params_for_template(self.level3_template)

        return list(StrategyConfig.OPTIMIZABLE_PARAMS)
