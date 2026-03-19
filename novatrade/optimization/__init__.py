"""NovaTrade optimization — parameter search, ablation, and strategy tuning.

Exports:
    ParameterBounds: Min/max/step for a single parameter.
    StrategyConfig: Pydantic v2 strategy configuration schema.
    WalkForwardConfig, WalkForwardResult, run_walk_forward: Walk-forward validation.
    PerturbationConfig, PerturbationResult, run_perturbation_test: Parameter stability.
    StressConfig, StressResult, run_stress_test: Cost stress testing.
    SweepConfig, SweepExperiment, SweepResult, run_sweep: Parameter sweep.
    AblationResult, run_ablation: Filter ablation testing.
    ExperimentStrategy, StrategyBudget, select_strategy: Campaign strategy selection.
"""

from __future__ import annotations

from novatrade.cli.config_schema import ParameterBounds, StrategyConfig
from novatrade.optimization.ablation import AblationResult, run_ablation
from novatrade.optimization.perturbation import (
    PerturbationConfig,
    PerturbationResult,
    run_perturbation_test,
)
from novatrade.optimization.strategies import (
    ExperimentStrategy,
    StrategyBudget,
    generate_combination_candidate,
    generate_explore_candidate,
    generate_local_candidate,
    select_strategy,
)
from novatrade.optimization.stress import StressConfig, StressResult, run_stress_test
from novatrade.optimization.sweep_engine import (
    SweepConfig,
    SweepExperiment,
    SweepResult,
    generate_latin_hypercube,
    generate_random_configs,
    run_sweep,
)
from novatrade.optimization.walkforward import (
    WalkForwardConfig,
    WalkForwardResult,
    run_walk_forward,
)

__all__ = [
    "AblationResult",
    "ExperimentStrategy",
    "ParameterBounds",
    "PerturbationConfig",
    "PerturbationResult",
    "StrategyBudget",
    "StrategyConfig",
    "StressConfig",
    "StressResult",
    "SweepConfig",
    "SweepExperiment",
    "SweepResult",
    "WalkForwardConfig",
    "WalkForwardResult",
    "generate_combination_candidate",
    "generate_explore_candidate",
    "generate_latin_hypercube",
    "generate_local_candidate",
    "generate_random_configs",
    "run_ablation",
    "run_perturbation_test",
    "run_stress_test",
    "run_sweep",
    "run_walk_forward",
    "select_strategy",
]
