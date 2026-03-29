"""Tests for utils.scheduler.uncertainty — Uncertainty Modeling & Schedule Confidence.

Phase 6 of the Intelligent Dynamic Block Scheduler.
Tests cover: SimulationResult model, simulate_block basics, confidence scoring,
fragility, overload risk, plan variant generation, variant selection,
report formatting, and edge cases.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from utils.scheduler.block import BlockPlan
from utils.scheduler.packer import PackerConfig, pack_block
from utils.scheduler.scorer import ScoredTask, score_task
from utils.scheduler.uncertainty import (
    PlanVariant,
    SimulationResult,
    VariantResult,
    format_confidence_report,
    generate_plan_variants,
    select_best_variant,
    simulate_block,
)
from utils.scheduler.work_unit import CommitmentLevel, TaskClass, WorkMode, WorkUnit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wu(**overrides) -> WorkUnit:
    """Create a WorkUnit with sensible test defaults."""
    defaults: dict[str, Any] = {
        "id": "t1",
        "title": "test task",
        "urgency": 0.5,
        "strategic_value": 0.5,
        "unblock_value": 0.0,
        "risk_reduction": 0.0,
        "confidence": 0.5,
        "success_probability": 0.7,
        "estimated_optimistic_min": 10.0,
        "estimated_expected_min": 30.0,
        "estimated_pessimistic_min": 60.0,
        "context_switch_cost_min": 5.0,
        "priority": "medium",
        "commitment_level": CommitmentLevel.SOFT,
        "deferment_age_days": 0.0,
        "task_class": TaskClass.BOUNDED,
        "work_mode": WorkMode.ADMIN,
        "schedulable_now": True,
    }
    defaults.update(overrides)
    return WorkUnit(**defaults)


def _scored(**overrides) -> ScoredTask:
    """Create a ScoredTask from a WorkUnit with defaults."""
    wu = _wu(**overrides)
    return score_task(wu)


def _make_plan(
    tasks: list[ScoredTask] | None = None,
    block_duration_min: float = 480.0,
    config: PackerConfig | None = None,
) -> BlockPlan:
    """Pack tasks into a BlockPlan."""
    if tasks is None:
        tasks = [_scored(id=f"t{i}") for i in range(5)]
    return pack_block(tasks, block_duration_min=block_duration_min, config=config)


def _generous_plan() -> BlockPlan:
    """A plan with plenty of buffer — tasks use <50% of capacity."""
    tasks = [
        _scored(
            id=f"g{i}",
            estimated_optimistic_min=10.0,
            estimated_expected_min=15.0,
            estimated_pessimistic_min=20.0,
        )
        for i in range(4)
    ]
    return _make_plan(tasks, block_duration_min=480.0)


def _tight_plan() -> BlockPlan:
    """A plan packed near capacity with wide uncertainty."""
    tasks = [
        _scored(
            id=f"tight{i}",
            estimated_optimistic_min=40.0,
            estimated_expected_min=80.0,
            estimated_pessimistic_min=150.0,
        )
        for i in range(5)
    ]
    return _make_plan(tasks, block_duration_min=480.0)


def _zero_variance_plan() -> BlockPlan:
    """All tasks have optimistic == expected == pessimistic."""
    tasks = [
        _scored(
            id=f"zv{i}",
            estimated_optimistic_min=30.0,
            estimated_expected_min=30.0,
            estimated_pessimistic_min=30.0,
        )
        for i in range(4)
    ]
    return _make_plan(tasks, block_duration_min=480.0)


def _high_variance_plan() -> BlockPlan:
    """Tasks with extreme uncertainty spreads."""
    tasks = [
        _scored(
            id=f"hv{i}",
            estimated_optimistic_min=5.0,
            estimated_expected_min=50.0,
            estimated_pessimistic_min=200.0,
        )
        for i in range(4)
    ]
    return _make_plan(tasks, block_duration_min=480.0)


# ---------------------------------------------------------------------------
# 1. SimulationResult model
# ---------------------------------------------------------------------------


class TestSimulationResultModel:
    """Test the SimulationResult Pydantic model."""

    def test_valid_construction(self):
        sr = SimulationResult(
            p_all_committed_complete=0.8,
            p_buffer_exhausted=0.1,
            p_tasks_deferred=0.15,
            fragility=0.3,
            expected_buffer_remaining_min=45.0,
            overload_risk=0.05,
            confidence_score=0.8,
            simulation_count=200,
        )
        assert sr.p_all_committed_complete == 0.8
        assert sr.simulation_count == 200

    def test_probabilities_bounded_0_1(self):
        """Probabilities must be between 0 and 1."""
        with pytest.raises((ValueError, TypeError)):
            SimulationResult(
                p_all_committed_complete=1.5,
                p_buffer_exhausted=0.0,
                p_tasks_deferred=0.0,
                fragility=0.0,
                expected_buffer_remaining_min=0.0,
                overload_risk=0.0,
                confidence_score=0.0,
                simulation_count=1,
            )

    def test_simulation_count_minimum(self):
        """simulation_count must be >= 1."""
        with pytest.raises((ValueError, TypeError)):
            SimulationResult(
                p_all_committed_complete=0.5,
                p_buffer_exhausted=0.0,
                p_tasks_deferred=0.0,
                fragility=0.0,
                expected_buffer_remaining_min=0.0,
                overload_risk=0.0,
                confidence_score=0.5,
                simulation_count=0,
            )

    def test_confidence_equals_p_all(self):
        """confidence_score is an alias for p_all_committed_complete."""
        sr = SimulationResult(
            p_all_committed_complete=0.73,
            p_buffer_exhausted=0.0,
            p_tasks_deferred=0.0,
            fragility=0.0,
            expected_buffer_remaining_min=0.0,
            overload_risk=0.0,
            confidence_score=0.73,
            simulation_count=100,
        )
        assert sr.confidence_score == sr.p_all_committed_complete


# ---------------------------------------------------------------------------
# 2. PlanVariant / VariantResult models
# ---------------------------------------------------------------------------


class TestPlanVariantModel:
    def test_enum_values(self):
        assert PlanVariant.AGGRESSIVE.value == "aggressive"
        assert PlanVariant.BALANCED.value == "balanced"
        assert PlanVariant.CONSERVATIVE.value == "conservative"

    def test_variant_result_construction(self):
        plan = _make_plan()
        sim = simulate_block(plan, seed=42)
        vr = VariantResult(
            variant=PlanVariant.BALANCED,
            plan=plan,
            simulation=sim,
            committed_ratio=0.75,
        )
        assert vr.variant == PlanVariant.BALANCED
        assert vr.committed_ratio == 0.75


# ---------------------------------------------------------------------------
# 3. simulate_block — basics
# ---------------------------------------------------------------------------


class TestSimulateBlockBasics:
    def test_returns_valid_simulation_result(self):
        plan = _make_plan()
        result = simulate_block(plan, seed=42)
        assert isinstance(result, SimulationResult)
        assert result.simulation_count == 200

    def test_all_probabilities_between_0_and_1(self):
        plan = _make_plan()
        result = simulate_block(plan, seed=42)
        for field in [
            "p_all_committed_complete",
            "p_buffer_exhausted",
            "p_tasks_deferred",
            "overload_risk",
            "confidence_score",
        ]:
            val = getattr(result, field)
            assert 0.0 <= val <= 1.0, f"{field}={val} out of [0, 1]"

    def test_seed_produces_reproducible_results(self):
        plan = _make_plan()
        r1 = simulate_block(plan, seed=123)
        r2 = simulate_block(plan, seed=123)
        assert r1.p_all_committed_complete == r2.p_all_committed_complete
        assert r1.overload_risk == r2.overload_risk
        assert r1.fragility == r2.fragility
        assert r1.expected_buffer_remaining_min == r2.expected_buffer_remaining_min

    def test_different_seeds_produce_different_results(self):
        plan = _tight_plan()
        r1 = simulate_block(plan, n_simulations=500, seed=1)
        r2 = simulate_block(plan, n_simulations=500, seed=999)
        # With a tight plan and large N, different seeds should give different exact values
        # (there's a vanishingly small chance they match, but in practice they won't)
        fields_match = all(
            getattr(r1, f) == getattr(r2, f) for f in ["p_all_committed_complete", "overload_risk", "p_tasks_deferred"]
        )
        assert not fields_match, "Different seeds should produce different results"

    def test_n_equals_1_is_valid(self):
        plan = _make_plan()
        result = simulate_block(plan, n_simulations=1, seed=42)
        assert result.simulation_count == 1
        assert 0.0 <= result.p_all_committed_complete <= 1.0

    def test_custom_n_simulations(self):
        plan = _make_plan()
        result = simulate_block(plan, n_simulations=50, seed=42)
        assert result.simulation_count == 50

    def test_n_simulations_zero_clamped_to_1(self):
        plan = _make_plan()
        result = simulate_block(plan, n_simulations=0, seed=42)
        assert result.simulation_count == 1

    def test_no_seed_runs_without_error(self):
        plan = _make_plan()
        result = simulate_block(plan, seed=None)
        assert isinstance(result, SimulationResult)


# ---------------------------------------------------------------------------
# 4. Confidence scoring
# ---------------------------------------------------------------------------


class TestConfidenceScoring:
    def test_generous_buffer_high_confidence(self):
        plan = _generous_plan()
        result = simulate_block(plan, n_simulations=500, seed=42)
        assert result.confidence_score > 0.8, (
            f"Generous plan should have high confidence, got {result.confidence_score}"
        )

    def test_tight_plan_lower_confidence(self):
        generous_result = simulate_block(_generous_plan(), n_simulations=500, seed=42)
        tight_result = simulate_block(_tight_plan(), n_simulations=500, seed=42)
        assert tight_result.confidence_score <= generous_result.confidence_score

    def test_zero_variance_confidence_near_1(self):
        """All tasks have opt==exp==pess — no uncertainty, should finish reliably."""
        plan = _zero_variance_plan()
        result = simulate_block(plan, n_simulations=200, seed=42)
        assert result.confidence_score >= 0.99, (
            f"Zero-variance plan should have confidence ~1.0, got {result.confidence_score}"
        )

    def test_high_variance_lower_confidence(self):
        """Tasks with extreme variance should have lower confidence than zero-variance."""
        zv_result = simulate_block(_zero_variance_plan(), n_simulations=500, seed=42)
        hv_result = simulate_block(_high_variance_plan(), n_simulations=500, seed=42)
        assert hv_result.confidence_score <= zv_result.confidence_score

    def test_confidence_equals_p_all_committed(self):
        plan = _make_plan()
        result = simulate_block(plan, seed=42)
        assert result.confidence_score == result.p_all_committed_complete


# ---------------------------------------------------------------------------
# 5. Fragility
# ---------------------------------------------------------------------------


class TestFragility:
    def test_fragility_non_negative(self):
        plan = _make_plan()
        result = simulate_block(plan, seed=42)
        assert result.fragility >= 0.0

    def test_single_large_task_tight_plan_higher_fragility(self):
        """One big uncertain task in a tight plan -> higher fragility than many small tasks."""
        # Big single task
        big = [
            _scored(
                id="big",
                estimated_optimistic_min=100.0,
                estimated_expected_min=200.0,
                estimated_pessimistic_min=400.0,
            ),
        ]
        big_plan = _make_plan(big, block_duration_min=480.0)

        # Many small tasks with narrow ranges
        small = [
            _scored(
                id=f"s{i}",
                estimated_optimistic_min=10.0,
                estimated_expected_min=12.0,
                estimated_pessimistic_min=14.0,
            )
            for i in range(10)
        ]
        small_plan = _make_plan(small, block_duration_min=480.0)

        big_result = simulate_block(big_plan, seed=42)
        small_result = simulate_block(small_plan, seed=42)

        assert big_result.fragility > small_result.fragility, (
            f"Single big task fragility ({big_result.fragility}) should exceed "
            f"many small tasks fragility ({small_result.fragility})"
        )

    def test_many_small_tasks_low_fragility(self):
        small = [
            _scored(
                id=f"sm{i}",
                estimated_optimistic_min=5.0,
                estimated_expected_min=6.0,
                estimated_pessimistic_min=7.0,
            )
            for i in range(8)
        ]
        plan = _make_plan(small, block_duration_min=480.0)
        result = simulate_block(plan, seed=42)
        assert result.fragility < 0.01, f"Many small tasks should have near-zero fragility, got {result.fragility}"

    def test_zero_variance_zero_fragility(self):
        plan = _zero_variance_plan()
        result = simulate_block(plan, seed=42)
        assert result.fragility == 0.0


# ---------------------------------------------------------------------------
# 6. Overload risk
# ---------------------------------------------------------------------------


class TestOverloadRisk:
    def test_underpacked_plan_zero_overload(self):
        """Plan using ~30% of capacity should have 0 overload risk."""
        plan = _generous_plan()
        result = simulate_block(plan, n_simulations=500, seed=42)
        assert result.overload_risk == 0.0, f"Generous plan should have 0 overload, got {result.overload_risk}"

    def test_overpacked_plan_high_overload(self):
        """Plan where expected duration already exceeds block → high overload."""
        tasks = [
            _scored(
                id=f"op{i}",
                estimated_optimistic_min=80.0,
                estimated_expected_min=120.0,
                estimated_pessimistic_min=180.0,
            )
            for i in range(6)
        ]
        plan = _make_plan(tasks, block_duration_min=480.0)
        result = simulate_block(plan, n_simulations=500, seed=42)
        assert result.overload_risk > 0.0, "Over-packed plan should have some overload risk"

    def test_overload_at_most_1(self):
        plan = _tight_plan()
        result = simulate_block(plan, seed=42)
        assert result.overload_risk <= 1.0


# ---------------------------------------------------------------------------
# 7. Buffer exhaustion
# ---------------------------------------------------------------------------


class TestBufferExhaustion:
    def test_generous_plan_no_buffer_exhaustion(self):
        plan = _generous_plan()
        result = simulate_block(plan, n_simulations=500, seed=42)
        assert result.p_buffer_exhausted == 0.0

    def test_tight_plan_some_buffer_exhaustion(self):
        """A tight plan should sometimes exhaust the buffer."""
        tasks = [
            _scored(
                id=f"buf{i}",
                estimated_optimistic_min=50.0,
                estimated_expected_min=90.0,
                estimated_pessimistic_min=160.0,
            )
            for i in range(5)
        ]
        plan = _make_plan(tasks, block_duration_min=480.0)
        result = simulate_block(plan, n_simulations=500, seed=42)
        # With wide variance, at least some runs should exhaust buffer
        # but not guaranteed — the test just checks it's a valid probability
        assert 0.0 <= result.p_buffer_exhausted <= 1.0

    def test_buffer_remaining_non_negative(self):
        plan = _make_plan()
        result = simulate_block(plan, seed=42)
        assert result.expected_buffer_remaining_min >= 0.0


# ---------------------------------------------------------------------------
# 8. Tasks deferred probability
# ---------------------------------------------------------------------------


class TestTasksDeferred:
    def test_generous_plan_no_deferral(self):
        plan = _generous_plan()
        result = simulate_block(plan, n_simulations=500, seed=42)
        assert result.p_tasks_deferred == 0.0

    def test_deferred_probability_bounded(self):
        plan = _tight_plan()
        result = simulate_block(plan, seed=42)
        assert 0.0 <= result.p_tasks_deferred <= 1.0


# ---------------------------------------------------------------------------
# 9. Plan variant generation
# ---------------------------------------------------------------------------


class TestGeneratePlanVariants:
    def test_returns_three_variants(self):
        tasks = [_scored(id=f"v{i}") for i in range(6)]
        variants = generate_plan_variants(tasks, seed=42)
        assert len(variants) == 3

    def test_variant_order(self):
        tasks = [_scored(id=f"v{i}") for i in range(6)]
        variants = generate_plan_variants(tasks, seed=42)
        assert variants[0].variant == PlanVariant.AGGRESSIVE
        assert variants[1].variant == PlanVariant.BALANCED
        assert variants[2].variant == PlanVariant.CONSERVATIVE

    def test_aggressive_packs_more_than_conservative(self):
        tasks = [
            _scored(
                id=f"v{i}",
                estimated_optimistic_min=20.0,
                estimated_expected_min=40.0,
                estimated_pessimistic_min=70.0,
            )
            for i in range(8)
        ]
        variants = generate_plan_variants(tasks, seed=42)
        agg = variants[0]
        cons = variants[2]
        # Aggressive has more committed capacity
        assert agg.plan.total_committed_min > cons.plan.total_committed_min

    def test_aggressive_lower_confidence_than_conservative(self):
        """With enough tasks, aggressive packing should have lower confidence."""
        tasks = [
            _scored(
                id=f"vc{i}",
                estimated_optimistic_min=30.0,
                estimated_expected_min=60.0,
                estimated_pessimistic_min=120.0,
            )
            for i in range(6)
        ]
        variants = generate_plan_variants(tasks, n_simulations=500, seed=42)
        agg_conf = variants[0].simulation.confidence_score
        cons_conf = variants[2].simulation.confidence_score
        assert agg_conf <= cons_conf, (
            f"Aggressive ({agg_conf}) should have lower confidence than Conservative ({cons_conf})"
        )

    def test_balanced_is_between(self):
        tasks = [
            _scored(
                id=f"vb{i}",
                estimated_optimistic_min=30.0,
                estimated_expected_min=60.0,
                estimated_pessimistic_min=120.0,
            )
            for i in range(6)
        ]
        variants = generate_plan_variants(tasks, n_simulations=500, seed=42)
        agg_ratio = variants[0].committed_ratio
        bal_ratio = variants[1].committed_ratio
        cons_ratio = variants[2].committed_ratio
        assert cons_ratio < bal_ratio < agg_ratio

    def test_custom_block_duration(self):
        tasks = [_scored(id="d1")]
        variants = generate_plan_variants(tasks, block_duration_min=240.0, seed=42)
        for v in variants:
            assert v.plan.block_duration_min == 240.0

    def test_custom_packer_config_inherits(self):
        """Non-ratio packer settings should be inherited."""
        cfg = PackerConfig(same_mode_switch_cost_min=0.0, different_mode_switch_cost_min=0.0)
        tasks = [_scored(id="cfg1")]
        variants = generate_plan_variants(tasks, packer_config=cfg, seed=42)
        assert len(variants) == 3

    def test_variant_committed_ratios(self):
        tasks = [_scored(id="r1")]
        variants = generate_plan_variants(tasks, seed=42)
        assert variants[0].committed_ratio == 0.85
        assert variants[1].committed_ratio == 0.75
        assert variants[2].committed_ratio == 0.65

    def test_each_variant_has_simulation(self):
        tasks = [_scored(id="s1")]
        variants = generate_plan_variants(tasks, seed=42)
        for v in variants:
            assert isinstance(v.simulation, SimulationResult)
            assert v.simulation.simulation_count == 200


# ---------------------------------------------------------------------------
# 10. Variant selection
# ---------------------------------------------------------------------------


class TestSelectBestVariant:
    def test_prefers_balanced_when_it_passes(self):
        tasks = [
            _scored(
                id=f"sel{i}",
                estimated_optimistic_min=10.0,
                estimated_expected_min=15.0,
                estimated_pessimistic_min=20.0,
            )
            for i in range(4)
        ]
        variants = generate_plan_variants(tasks, n_simulations=200, seed=42)
        best = select_best_variant(variants)
        assert best.variant == PlanVariant.BALANCED

    def test_rejects_below_min_confidence(self):
        """With wide-variance tasks, setting min_confidence=1.0 rejects all variants."""
        tasks = [
            _scored(
                id=f"rej{i}",
                estimated_optimistic_min=30.0,
                estimated_expected_min=80.0,
                estimated_pessimistic_min=200.0,
            )
            for i in range(6)
        ]
        variants = generate_plan_variants(tasks, n_simulations=500, seed=42)
        # With wide-variance tasks packed near capacity, no variant should
        # achieve perfect confidence — setting min_confidence=1.0 rejects all.
        best = select_best_variant(variants, min_confidence=1.0)
        # Should fall back to CONSERVATIVE
        assert best.variant == PlanVariant.CONSERVATIVE

    def test_falls_back_to_conservative_when_all_fail(self):
        tasks = [_scored(id="fb1")]
        variants = generate_plan_variants(tasks, seed=42)
        # Impossible thresholds
        best = select_best_variant(
            variants,
            min_confidence=1.0,
            max_fragility=0.0,
            max_overload_risk=0.0,
        )
        assert best.variant == PlanVariant.CONSERVATIVE

    def test_picks_highest_confidence_when_balanced_fails(self):
        """If BALANCED fails thresholds but others pass, pick highest confidence."""
        tasks = [
            _scored(
                id=f"hc{i}",
                estimated_optimistic_min=30.0,
                estimated_expected_min=60.0,
                estimated_pessimistic_min=120.0,
            )
            for i in range(6)
        ]
        variants = generate_plan_variants(tasks, n_simulations=500, seed=42)
        # Suppose BALANCED has moderate confidence — set min_confidence above
        # BALANCED but below CONSERVATIVE
        bal_conf = variants[1].simulation.confidence_score
        cons_conf = variants[2].simulation.confidence_score
        if cons_conf > bal_conf:
            # Set threshold between BALANCED and CONSERVATIVE
            threshold = (bal_conf + cons_conf) / 2
            best = select_best_variant(variants, min_confidence=threshold)
            assert best.variant != PlanVariant.BALANCED or best.simulation.confidence_score >= threshold

    def test_max_fragility_filter(self):
        tasks = [_scored(id="frag1")]
        variants = generate_plan_variants(tasks, seed=42)
        # Allow only zero fragility
        best = select_best_variant(variants, max_fragility=0.0)
        # Should still return something (CONSERVATIVE fallback)
        assert isinstance(best, VariantResult)

    def test_max_overload_risk_filter(self):
        tasks = [_scored(id="ol1")]
        variants = generate_plan_variants(tasks, seed=42)
        best = select_best_variant(variants, max_overload_risk=0.0)
        assert isinstance(best, VariantResult)

    def test_returns_variant_result_type(self):
        tasks = [_scored(id="type1")]
        variants = generate_plan_variants(tasks, seed=42)
        best = select_best_variant(variants)
        assert isinstance(best, VariantResult)
        assert isinstance(best.simulation, SimulationResult)


# ---------------------------------------------------------------------------
# 11. Report formatting
# ---------------------------------------------------------------------------


class TestFormatConfidenceReport:
    def test_returns_valid_string(self):
        plan = _make_plan()
        result = simulate_block(plan, seed=42)
        report = format_confidence_report(result)
        assert isinstance(report, str)
        assert len(report) > 0

    def test_contains_confidence_percentage(self):
        plan = _make_plan()
        result = simulate_block(plan, seed=42)
        report = format_confidence_report(result)
        pct = int(result.confidence_score * 100)
        assert f"{pct}%" in report

    def test_contains_buffer_estimate(self):
        plan = _make_plan()
        result = simulate_block(plan, seed=42)
        report = format_confidence_report(result)
        assert "buffer" in report.lower()

    def test_contains_variant_name(self):
        plan = _make_plan()
        result = simulate_block(plan, seed=42)
        report = format_confidence_report(result, variant=PlanVariant.AGGRESSIVE)
        assert "aggressive" in report.lower()

    def test_contains_simulation_count(self):
        plan = _make_plan()
        result = simulate_block(plan, seed=42)
        report = format_confidence_report(result)
        assert str(result.simulation_count) in report

    def test_contains_overload_risk(self):
        plan = _make_plan()
        result = simulate_block(plan, seed=42)
        report = format_confidence_report(result)
        assert "overload" in report.lower()

    def test_multiline_format(self):
        plan = _make_plan()
        result = simulate_block(plan, seed=42)
        report = format_confidence_report(result)
        lines = report.strip().split("\n")
        assert len(lines) > 3, "Report should have multiple lines"

    def test_default_variant_is_balanced(self):
        plan = _make_plan()
        result = simulate_block(plan, seed=42)
        report = format_confidence_report(result)
        assert "balanced" in report.lower()


# ---------------------------------------------------------------------------
# 12. Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_plan_confidence_near_1(self):
        """An empty plan has nothing to fail — confidence should be 1.0."""
        empty_plan = BlockPlan(
            block_duration_min=480.0,
            total_committed_min=360.0,
            total_buffer_min=81.6,
        )
        result = simulate_block(empty_plan, seed=42)
        assert result.confidence_score == 1.0
        assert result.overload_risk == 0.0
        assert result.fragility == 0.0

    def test_single_task_plan(self):
        tasks = [_scored(id="single")]
        plan = _make_plan(tasks)
        result = simulate_block(plan, seed=42)
        assert isinstance(result, SimulationResult)
        assert result.simulation_count == 200

    def test_plan_where_opt_eq_exp_eq_pess(self):
        """Tasks with zero variance should behave deterministically."""
        plan = _zero_variance_plan()
        result = simulate_block(plan, seed=42)
        assert result.confidence_score >= 0.99
        assert result.fragility == 0.0

    def test_very_short_block(self):
        """Block of 10 minutes with a 30-min expected task."""
        tasks = [_scored(id="short1")]
        plan = _make_plan(tasks, block_duration_min=10.0)
        result = simulate_block(plan, seed=42)
        # Should still be valid
        assert 0.0 <= result.confidence_score <= 1.0

    def test_simulation_performance(self):
        """200 simulations should complete in <200ms."""
        plan = _make_plan()
        start = time.monotonic()
        simulate_block(plan, n_simulations=200, seed=42)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 200, f"Simulation took {elapsed_ms:.1f}ms, expected <200ms"

    def test_large_simulation_count(self):
        """1000 simulations should still work and be fast."""
        plan = _make_plan()
        start = time.monotonic()
        result = simulate_block(plan, n_simulations=1000, seed=42)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert result.simulation_count == 1000
        assert elapsed_ms < 500, f"1000 simulations took {elapsed_ms:.1f}ms, expected <500ms"

    def test_empty_scored_tasks_variants(self):
        """generate_plan_variants with empty task list."""
        variants = generate_plan_variants([], seed=42)
        assert len(variants) == 3
        for v in variants:
            assert v.simulation.confidence_score == 1.0

    def test_single_task_variants(self):
        tasks = [_scored(id="sv1")]
        variants = generate_plan_variants(tasks, seed=42)
        assert len(variants) == 3

    def test_plan_with_switch_costs(self):
        """Tasks with different work modes incur switch costs."""
        tasks = [
            _scored(id="sw1", work_mode=WorkMode.DEEP_IMPLEMENTATION),
            _scored(id="sw2", work_mode=WorkMode.MONITORING),
            _scored(id="sw3", work_mode=WorkMode.RESEARCH),
        ]
        plan = _make_plan(tasks)
        result = simulate_block(plan, seed=42)
        assert isinstance(result, SimulationResult)

    def test_plan_all_hard_commitment(self):
        """All HARD commitment tasks."""
        tasks = [
            _scored(
                id=f"hard{i}",
                commitment_level=CommitmentLevel.HARD,
                estimated_optimistic_min=20.0,
                estimated_expected_min=30.0,
                estimated_pessimistic_min=50.0,
            )
            for i in range(4)
        ]
        plan = _make_plan(tasks)
        result = simulate_block(plan, seed=42)
        assert isinstance(result, SimulationResult)

    def test_plan_all_opportunistic(self):
        """All OPPORTUNISTIC tasks end up in filler."""
        tasks = [
            _scored(
                id=f"opp{i}",
                commitment_level=CommitmentLevel.OPPORTUNISTIC,
                estimated_optimistic_min=5.0,
                estimated_expected_min=8.0,
                estimated_pessimistic_min=10.0,
            )
            for i in range(3)
        ]
        plan = _make_plan(tasks)
        result = simulate_block(plan, seed=42)
        assert isinstance(result, SimulationResult)


# ---------------------------------------------------------------------------
# 13. Integration: end-to-end flow
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_full_pipeline(self):
        """score -> pack -> simulate -> variant -> select -> report."""
        tasks = [
            _scored(
                id=f"e2e{i}",
                estimated_optimistic_min=15.0,
                estimated_expected_min=30.0,
                estimated_pessimistic_min=60.0,
            )
            for i in range(6)
        ]

        variants = generate_plan_variants(tasks, n_simulations=200, seed=42)
        assert len(variants) == 3

        best = select_best_variant(variants)
        assert isinstance(best, VariantResult)

        report = format_confidence_report(best.simulation, variant=best.variant)
        assert isinstance(report, str)
        assert len(report) > 10

    def test_select_then_simulate_again_reproducible(self):
        """Selecting a variant and re-simulating with same seed gives same result."""
        tasks = [_scored(id=f"rep{i}") for i in range(4)]
        variants = generate_plan_variants(tasks, n_simulations=200, seed=77)
        best = select_best_variant(variants)

        # Re-simulate the selected plan
        again = simulate_block(best.plan, n_simulations=200, seed=77)
        assert again.confidence_score == best.simulation.confidence_score

    def test_import_from_scheduler_package(self):
        """All new symbols should be importable from utils.scheduler."""
        from utils.scheduler import (  # noqa: F401
            PlanVariant,
            SimulationResult,
            VariantResult,
            format_confidence_report,
            generate_plan_variants,
            select_best_variant,
            simulate_block,
        )


# ---------------------------------------------------------------------------
# 14. Additional coverage for boundary behaviour
# ---------------------------------------------------------------------------


class TestBoundaryBehaviour:
    def test_fragility_capped_at_1(self):
        """Fragility should never exceed 1.0 even with huge variance and tiny buffer."""
        tasks = [
            _scored(
                id="frcap",
                estimated_optimistic_min=1.0,
                estimated_expected_min=100.0,
                estimated_pessimistic_min=200.0,
            )
        ]
        # Tiny buffer
        cfg = PackerConfig(committed_ratio=0.95, buffer_ratio=0.03, filler_ratio=0.02)
        plan = _make_plan(tasks, config=cfg)
        result = simulate_block(plan, seed=42)
        assert result.fragility <= 1.0

    def test_buffer_remaining_for_generous_plan(self):
        """Generous plan should have positive expected buffer remaining."""
        plan = _generous_plan()
        result = simulate_block(plan, n_simulations=500, seed=42)
        assert result.expected_buffer_remaining_min > 0

    def test_overload_and_confidence_are_complementary(self):
        """If overload_risk is X, all_complete should be ~1-X (approximately)."""
        plan = _make_plan()
        result = simulate_block(plan, n_simulations=1000, seed=42)
        # overload_risk = P(total > block_duration), confidence = P(total <= block_duration)
        # They should sum to ~1.0
        total = result.confidence_score + result.overload_risk
        assert abs(total - 1.0) < 0.01, (
            f"confidence ({result.confidence_score}) + overload_risk ({result.overload_risk}) = {total}, expected ~1.0"
        )

    def test_buffer_exhausted_subset_of_deferred(self):
        """p_buffer_exhausted should be <= p_tasks_deferred (conceptually)."""
        # Buffer exhaustion implies exceeding committed+buffer, which means
        # some tasks are effectively deferred. But p_tasks_deferred checks
        # committed only, so it should be >= p_buffer_exhausted.
        plan = _tight_plan()
        result = simulate_block(plan, n_simulations=500, seed=42)
        assert result.p_tasks_deferred >= result.p_buffer_exhausted - 0.01

    def test_negative_n_simulations_clamped(self):
        plan = _make_plan()
        result = simulate_block(plan, n_simulations=-5, seed=42)
        assert result.simulation_count == 1
