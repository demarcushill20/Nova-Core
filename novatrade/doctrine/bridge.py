"""Doctrine-to-StrategyConfig bridge.

Maps a StrategyDoctrine's mutable parameters (with defaults) to a valid
StrategyConfig instance. This is the glue between the human-facing doctrine
format and the engine-facing config format.
"""

from __future__ import annotations

from typing import Any

from novatrade.cli.config_schema import StrategyConfig
from novatrade.doctrine.schema import StrategyDoctrine


def doctrine_to_config(
    doctrine: StrategyDoctrine,
    *,
    overrides: dict[str, Any] | None = None,
) -> StrategyConfig:
    """Convert a StrategyDoctrine to a StrategyConfig.

    Takes the default value from each mutable parameter, applies any
    overrides, and creates a validated StrategyConfig. Only parameters
    that exist in StrategyConfig are mapped; extra doctrine params are
    silently ignored (they belong to future strategy types).

    Args:
        doctrine: The source doctrine.
        overrides: Optional parameter overrides (e.g. from optimizer).

    Returns:
        A validated StrategyConfig instance.

    Raises:
        pydantic.ValidationError: If resulting values violate StrategyConfig bounds.
    """
    # Start with defaults from mutable params
    params: dict[str, Any] = {}
    for name, bound in doctrine.mutable_params.items():
        params[name] = bound.default

    # Apply overrides
    if overrides:
        params.update(overrides)

    # Filter to only StrategyConfig-recognized fields
    config_fields = set(StrategyConfig.model_fields.keys())
    filtered: dict[str, Any] = {k: v for k, v in params.items() if k in config_fields}

    # Add metadata
    filtered["name"] = doctrine.name
    filtered["version"] = doctrine.version
    filtered["description"] = doctrine.description

    # Map mandatory filters
    filters: list[str] = list(doctrine.filters_mandatory)
    filtered["filters_enabled"] = filters

    return StrategyConfig.model_validate(filtered)


def config_to_doctrine_params(config: StrategyConfig) -> dict[str, float]:
    """Extract parameter values from a StrategyConfig as a flat dict.

    Useful for comparing doctrine defaults with optimizer-found values.

    Args:
        config: A StrategyConfig instance.

    Returns:
        Dict of parameter name → value for all optimizable params.
    """
    return {name: getattr(config, name) for name in StrategyConfig.OPTIMIZABLE_PARAMS if hasattr(config, name)}


def validate_doctrine_config_compatibility(
    doctrine: StrategyDoctrine,
) -> list[str]:
    """Check that a doctrine can produce a valid StrategyConfig.

    Returns a list of issues (empty = compatible).
    """
    issues: list[str] = []

    # Check that mutable param defaults fall within StrategyConfig bounds
    for name, bound in doctrine.mutable_params.items():
        if name in StrategyConfig.PARAMETER_BOUNDS:
            sc_bound = StrategyConfig.PARAMETER_BOUNDS[name]
            if bound.default < sc_bound.min_val:
                issues.append(f"{name}: doctrine default {bound.default} < StrategyConfig min {sc_bound.min_val}")
            if bound.default > sc_bound.max_val:
                issues.append(f"{name}: doctrine default {bound.default} > StrategyConfig max {sc_bound.max_val}")

    # Try to build a config and catch validation errors
    try:
        doctrine_to_config(doctrine)
    except Exception as e:
        issues.append(f"Config construction failed: {e}")

    return issues
