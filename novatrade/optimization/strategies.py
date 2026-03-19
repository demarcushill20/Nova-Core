"""Experiment strategy selection for AutoResearch campaign loops.

Determines which search strategy to use for each experiment index
(local perturbation, random exploration, Bayesian, ablation, or
parameter combination) according to a configurable budget split.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from novatrade.cli.config_schema import StrategyConfig
from novatrade.optimization.sweep_engine import (
    _resolve_params,
    _snap_to_type,
    generate_random_configs,
)

# ---------------------------------------------------------------------------
# Strategy enum and budget
# ---------------------------------------------------------------------------


class ExperimentStrategy(Enum):
    """Which search method to use for an experiment."""

    LOCAL = "local"
    EXPLORE = "explore"
    BAYESIAN = "bayesian"
    ABLATION = "ablation"
    COMBINATION = "combination"


@dataclass
class StrategyBudget:
    """Fraction of total experiments allocated to each strategy."""

    local_pct: float = 0.60
    explore_pct: float = 0.15
    bayesian_pct: float = 0.15
    ablation_pct: float = 0.05
    combination_pct: float = 0.05


# ---------------------------------------------------------------------------
# Strategy selector
# ---------------------------------------------------------------------------


def select_strategy(
    experiment_index: int,
    budget: StrategyBudget | None = None,
    total_experiments: int = 100,
) -> ExperimentStrategy:
    """Deterministically assign a strategy based on experiment index.

    Maps the experiment index to a strategy based on cumulative budget
    percentages.  Uses integer cutoffs to avoid floating-point boundary
    errors.

    Args:
        experiment_index: Zero-based experiment number.
        budget: Budget allocation (defaults to 60/15/15/5/5 split).
        total_experiments: Total experiments in the campaign.

    Returns:
        The ExperimentStrategy to use for this index.
    """
    b = budget or StrategyBudget()
    n = max(total_experiments, 1)

    # Compute integer cutoffs to avoid floating-point boundary issues
    cut_local = round(b.local_pct * n)
    cut_explore = round((b.local_pct + b.explore_pct) * n)
    cut_bayesian = round((b.local_pct + b.explore_pct + b.bayesian_pct) * n)
    cut_ablation = round((b.local_pct + b.explore_pct + b.bayesian_pct + b.ablation_pct) * n)

    if experiment_index < cut_local:
        return ExperimentStrategy.LOCAL
    if experiment_index < cut_explore:
        return ExperimentStrategy.EXPLORE
    if experiment_index < cut_bayesian:
        return ExperimentStrategy.BAYESIAN
    if experiment_index < cut_ablation:
        return ExperimentStrategy.ABLATION
    return ExperimentStrategy.COMBINATION


# ---------------------------------------------------------------------------
# Candidate generators
# ---------------------------------------------------------------------------


def generate_local_candidate(
    best_config: dict,
    params: list[str] | None = None,
    scale: float = 0.05,
    seed: int | None = None,
) -> dict:
    """Small perturbation of the best config.

    For each parameter, adds a random offset drawn from
    ``Uniform(-scale * range, +scale * range)`` and clips to bounds.

    Args:
        best_config: Current best parameter dict.
        params: Subset of parameters to perturb (None = all optimisable).
        scale: Perturbation scale as fraction of parameter range.
        seed: RNG seed for reproducibility.

    Returns:
        New parameter dict with perturbed values.
    """
    rng = np.random.default_rng(seed)
    resolved = _resolve_params(params)
    bounds = StrategyConfig.PARAMETER_BOUNDS

    candidate: dict = {}
    for p in resolved:
        b = bounds[p]
        base_val = best_config.get(p, (b.min_val + b.max_val) / 2)
        span = b.max_val - b.min_val
        offset = rng.uniform(-scale * span, scale * span)
        val = float(np.clip(base_val + offset, b.min_val, b.max_val))
        candidate[p] = _snap_to_type(p, val)
    return candidate


def generate_explore_candidate(
    params: list[str] | None = None,
    seed: int | None = None,
) -> dict:
    """Generate a fully random candidate within bounds.

    Delegates to ``generate_random_configs`` with n=1.

    Args:
        params: Subset of parameters (None = all optimisable).
        seed: RNG seed for reproducibility.

    Returns:
        Single random parameter dict.
    """
    configs = generate_random_configs(n=1, params=params, seed=seed)
    return configs[0]


def generate_combination_candidate(
    top_configs: list[dict],
    params: list[str] | None = None,
    seed: int | None = None,
) -> dict:
    """Combine parameters from multiple top configs.

    For each parameter, randomly picks one of the top configs and uses
    that config's value.  Creates a "child" that combines good parameter
    regions from different parents.

    Args:
        top_configs: List of high-performing parameter dicts (>= 2 recommended).
        params: Subset of parameters (None = all optimisable).
        seed: RNG seed for reproducibility.

    Returns:
        Combined parameter dict.

    Raises:
        ValueError: If top_configs is empty.
    """
    if not top_configs:
        raise ValueError("top_configs must be non-empty")

    rng = np.random.default_rng(seed)
    resolved = _resolve_params(params)
    bounds = StrategyConfig.PARAMETER_BOUNDS

    candidate: dict = {}
    for p in resolved:
        # Pick a random parent
        parent = top_configs[rng.integers(0, len(top_configs))]
        b = bounds[p]
        val = parent.get(p, (b.min_val + b.max_val) / 2)
        # Clip in case parent had out-of-range values
        val = float(np.clip(val, b.min_val, b.max_val))
        candidate[p] = _snap_to_type(p, val)
    return candidate
