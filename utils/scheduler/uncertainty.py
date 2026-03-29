"""Uncertainty Modeling & Schedule Confidence for the Dynamic Block Scheduler.

Phase 6: Monte Carlo simulation of block plans, plan variant generation,
variant selection, and human-readable confidence reporting.

Uses triangular distributions (optimistic / expected / pessimistic) to model
task duration uncertainty and estimates the probability that a block plan
completes on time.
"""

from __future__ import annotations

import random
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from utils.scheduler.block import BlockPlan
from utils.scheduler.packer import PackerConfig, pack_block
from utils.scheduler.scorer import ScoredTask

# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class SimulationResult(BaseModel):
    """Aggregate statistics from a Monte Carlo simulation of a block plan."""

    model_config = ConfigDict(extra="forbid")

    p_all_committed_complete: float = Field(
        ge=0.0,
        le=1.0,
        description="Probability all committed tasks finish within the block",
    )
    p_buffer_exhausted: float = Field(
        ge=0.0,
        le=1.0,
        description="Probability the buffer is fully consumed",
    )
    p_tasks_deferred: float = Field(
        ge=0.0,
        le=1.0,
        description="Probability at least one task gets deferred",
    )
    fragility: float = Field(
        ge=0.0,
        description="Sensitivity to a single task overrun causing cascade",
    )
    expected_buffer_remaining_min: float = Field(
        description="Mean remaining buffer across runs that finish in time",
    )
    overload_risk: float = Field(
        ge=0.0,
        le=1.0,
        description="Probability committed work exceeds block capacity",
    )
    confidence_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Alias for p_all_committed_complete",
    )
    simulation_count: int = Field(
        ge=1,
        description="Number of Monte Carlo iterations run",
    )


class PlanVariant(str, Enum):
    """Preset packing aggressiveness levels."""

    AGGRESSIVE = "aggressive"
    BALANCED = "balanced"
    CONSERVATIVE = "conservative"


class VariantResult(BaseModel):
    """A block plan paired with its simulation statistics."""

    model_config = ConfigDict(extra="forbid")

    variant: PlanVariant
    plan: BlockPlan
    simulation: SimulationResult
    committed_ratio: float = Field(
        ge=0.0,
        le=1.0,
        description="Fraction of block allocated to committed work",
    )


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


def simulate_block(
    plan: BlockPlan,
    n_simulations: int = 200,
    seed: int | None = None,
) -> SimulationResult:
    """Run a Monte Carlo simulation of *plan* and return aggregate statistics.

    Each iteration samples task durations from a triangular distribution
    defined by (optimistic, pessimistic, expected) and checks whether the
    block's capacity constraints hold.

    Parameters
    ----------
    plan:
        A packed BlockPlan (from ``pack_block``).
    n_simulations:
        Number of Monte Carlo iterations.  200 gives stable results in <50 ms.
    seed:
        Optional RNG seed for reproducibility.
    """
    if n_simulations < 1:
        n_simulations = 1

    rng = random.Random(seed)  # noqa: S311 — Monte Carlo simulation, not crypto

    block_dur = plan.block_duration_min
    committed_capacity = plan.total_committed_min
    buffer_capacity = plan.total_buffer_min

    # Pre-extract slot data to avoid repeated attribute lookups
    slot_params: list[tuple[float, float, float, float]] = []
    for slot in plan.scheduled_slots:
        wu = slot.scored_task.work_unit
        opt = wu.estimated_optimistic_min
        pess = wu.estimated_pessimistic_min
        exp = wu.estimated_expected_min
        switch = slot.switch_cost_min
        slot_params.append((opt, pess, exp, switch))

    # Handle empty plans
    if not slot_params:
        return SimulationResult(
            p_all_committed_complete=1.0,
            p_buffer_exhausted=0.0,
            p_tasks_deferred=0.0,
            fragility=0.0,
            expected_buffer_remaining_min=buffer_capacity,
            overload_risk=0.0,
            confidence_score=1.0,
            simulation_count=n_simulations,
        )

    # Compute per-task variance for fragility calculation
    task_variances: list[float] = []
    for opt, pess, exp, _switch in slot_params:
        # Variance of triangular distribution = (a^2 + b^2 + c^2 - ab - ac - bc) / 18
        var = (opt**2 + pess**2 + exp**2 - opt * pess - opt * exp - pess * exp) / 18.0
        task_variances.append(var)

    max_variance = max(task_variances) if task_variances else 0.0

    # Run simulations
    all_complete_count = 0
    buffer_exhausted_count = 0
    tasks_deferred_count = 0
    overload_count = 0
    buffer_remaining_values: list[float] = []

    for _ in range(n_simulations):
        total_time = 0.0
        for opt, pess, exp, switch in slot_params:
            # Triangular distribution: low=optimistic, high=pessimistic, mode=expected
            if opt == pess:
                # Zero-variance: all three are equal
                duration = opt
            else:
                duration = rng.triangular(opt, pess, exp)
            total_time += duration + switch

        # Did all tasks fit within the block?
        if total_time <= block_dur:
            all_complete_count += 1
            buffer_remaining_values.append(block_dur - total_time)

        # Did we exceed committed + buffer?
        if total_time > committed_capacity + buffer_capacity:
            buffer_exhausted_count += 1

        # Did we exceed committed capacity (tasks deferred)?
        if total_time > committed_capacity:
            tasks_deferred_count += 1

        # Did we exceed block duration (overload)?
        if total_time > block_dur:
            overload_count += 1

    n = n_simulations

    # Fragility: ratio of max task variance to buffer squared, clamped to [0, 1]
    if buffer_capacity > 0:
        fragility = min(1.0, max_variance / (buffer_capacity**2))
    else:
        # No buffer => maximally fragile if there is any variance
        fragility = 1.0 if max_variance > 0 else 0.0

    expected_buffer_remaining = (
        sum(buffer_remaining_values) / len(buffer_remaining_values) if buffer_remaining_values else 0.0
    )

    p_all = all_complete_count / n
    return SimulationResult(
        p_all_committed_complete=round(p_all, 6),
        p_buffer_exhausted=round(buffer_exhausted_count / n, 6),
        p_tasks_deferred=round(tasks_deferred_count / n, 6),
        fragility=round(fragility, 6),
        expected_buffer_remaining_min=round(expected_buffer_remaining, 2),
        overload_risk=round(overload_count / n, 6),
        confidence_score=round(p_all, 6),
        simulation_count=n,
    )


# ---------------------------------------------------------------------------
# Plan variant generation
# ---------------------------------------------------------------------------

# Preset ratios for each variant
_VARIANT_RATIOS: dict[PlanVariant, tuple[float, float, float]] = {
    PlanVariant.AGGRESSIVE: (0.85, 0.10, 0.05),
    PlanVariant.BALANCED: (0.75, 0.17, 0.08),
    PlanVariant.CONSERVATIVE: (0.65, 0.25, 0.10),
}


def generate_plan_variants(
    scored_tasks: list[ScoredTask],
    block_duration_min: float = 480.0,
    n_simulations: int = 200,
    packer_config: PackerConfig | None = None,
    seed: int | None = None,
) -> list[VariantResult]:
    """Generate AGGRESSIVE, BALANCED, and CONSERVATIVE plan variants.

    For each variant the scored tasks are packed with the variant's capacity
    ratios (other packer settings are inherited from *packer_config*), then
    simulated with ``simulate_block``.

    Returns a list of three ``VariantResult`` objects in variant order.
    """
    results: list[VariantResult] = []

    for variant in (PlanVariant.AGGRESSIVE, PlanVariant.BALANCED, PlanVariant.CONSERVATIVE):
        committed, buffer, filler = _VARIANT_RATIOS[variant]

        # Build variant-specific config, inheriting non-ratio fields
        if packer_config is not None:
            cfg = packer_config.model_copy(
                update={
                    "committed_ratio": committed,
                    "buffer_ratio": buffer,
                    "filler_ratio": filler,
                },
            )
        else:
            cfg = PackerConfig(
                committed_ratio=committed,
                buffer_ratio=buffer,
                filler_ratio=filler,
            )

        plan = pack_block(scored_tasks, block_duration_min=block_duration_min, config=cfg)
        sim = simulate_block(plan, n_simulations=n_simulations, seed=seed)

        results.append(
            VariantResult(
                variant=variant,
                plan=plan,
                simulation=sim,
                committed_ratio=committed,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Variant selection
# ---------------------------------------------------------------------------


def select_best_variant(
    variants: list[VariantResult],
    min_confidence: float = 0.50,
    max_fragility: float = 0.60,
    max_overload_risk: float = 0.30,
) -> VariantResult:
    """Select the best plan variant given quality thresholds.

    Logic:
      1. Filter out variants that fail any threshold.
      2. If BALANCED passes, return it (default preference).
      3. Otherwise return the highest-confidence passing variant.
      4. If nothing passes, return CONSERVATIVE (safest fallback).
    """
    passing: list[VariantResult] = [
        v
        for v in variants
        if (
            v.simulation.confidence_score >= min_confidence
            and v.simulation.fragility <= max_fragility
            and v.simulation.overload_risk <= max_overload_risk
        )
    ]

    if not passing:
        # Fallback: return CONSERVATIVE if present, else the last variant
        for v in variants:
            if v.variant == PlanVariant.CONSERVATIVE:
                return v
        return variants[-1] if variants else variants[0]

    # Prefer BALANCED
    for v in passing:
        if v.variant == PlanVariant.BALANCED:
            return v

    # Otherwise highest confidence
    passing.sort(key=lambda v: v.simulation.confidence_score, reverse=True)
    return passing[0]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_confidence_report(
    result: SimulationResult,
    variant: PlanVariant = PlanVariant.BALANCED,
) -> str:
    """Return a human-readable confidence report for a simulation result.

    Includes a one-line summary and a multi-line detail section.
    """
    pct = int(result.confidence_score * 100)
    buf_h = int(result.expected_buffer_remaining_min // 60)
    buf_m = int(result.expected_buffer_remaining_min % 60)
    buf_str = f"{buf_h}h{buf_m:02d}m" if buf_h else f"{buf_m}m"

    summary = f"Block confidence: {pct}% ({variant.value}), buffer: {buf_str} expected"

    detail_lines = [
        summary,
        "",
        "--- Simulation Detail ---",
        f"  Variant           : {variant.value}",
        f"  Simulations       : {result.simulation_count}",
        f"  Confidence        : {result.confidence_score:.1%}",
        f"  Overload risk     : {result.overload_risk:.1%}",
        f"  Buffer exhausted  : {result.p_buffer_exhausted:.1%}",
        f"  Tasks deferred    : {result.p_tasks_deferred:.1%}",
        f"  Fragility         : {result.fragility:.3f}",
        f"  Expected buffer   : {result.expected_buffer_remaining_min:.1f} min",
    ]

    return "\n".join(detail_lines)
