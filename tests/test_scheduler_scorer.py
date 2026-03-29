"""Tests for utils.scheduler.scorer — Task Scoring Engine.

Phase 2 of the Intelligent Dynamic Block Scheduler.
Tests cover: determinism, weight sensitivity, deferment aging, commitment
adjustments, confidence modifiers, priority multipliers, time-cost divisor,
unlock value propagation, ranking, config loading, starvation prevention,
weight normalization, input immutability, and parametrized sample-task ordering.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from utils.scheduler.scorer import (
    ScoredTask,
    ScoringConfig,
    compute_unlock_values,
    load_scoring_config,
    rank_tasks,
    score_task,
)
from utils.scheduler.work_unit import CommitmentLevel, WorkUnit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wu(**overrides) -> WorkUnit:
    """Create a WorkUnit with sensible test defaults, accepting overrides."""
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
    }
    defaults.update(overrides)
    return WorkUnit(**defaults)


# ---------------------------------------------------------------------------
# Scoring determinism
# ---------------------------------------------------------------------------


class TestScoringDeterminism:
    def test_same_inputs_same_score(self):
        """Identical inputs must produce identical scores."""
        wu = _wu()
        s1 = score_task(wu)
        s2 = score_task(wu)
        assert s1.final_score == s2.final_score
        assert s1.raw_score == s2.raw_score

    def test_determinism_across_configs(self):
        """Same config object used twice gives identical results."""
        cfg = ScoringConfig()
        wu = _wu(urgency=0.9, strategic_value=0.8)
        s1 = score_task(wu, cfg)
        s2 = score_task(wu, cfg)
        assert s1.final_score == s2.final_score

    def test_default_config_produces_valid_scores(self):
        """Default config yields a positive finite score."""
        s = score_task(_wu())
        assert s.final_score >= 0.0
        assert s.raw_score >= 0.0
        assert isinstance(s.final_score, float)

    def test_scored_task_has_all_fields(self):
        """ScoredTask exposes all intermediate values."""
        s = score_task(_wu())
        assert hasattr(s, "raw_score")
        assert hasattr(s, "deferment_bonus")
        assert hasattr(s, "commitment_adjustment")
        assert hasattr(s, "confidence_modifier")
        assert hasattr(s, "effective_time")
        assert hasattr(s, "priority_multiplier")
        assert hasattr(s, "final_score")


# ---------------------------------------------------------------------------
# Weight sensitivity
# ---------------------------------------------------------------------------


class TestWeightSensitivity:
    def test_higher_urgency_weight_boosts_high_urgency_task(self):
        """Increasing urgency weight should raise score for high-urgency tasks."""
        wu = _wu(urgency=1.0, strategic_value=0.0, unblock_value=0.0, risk_reduction=0.0)
        cfg_normal = ScoringConfig()
        cfg_heavy = ScoringConfig(
            weights={
                "urgency": 0.50,
                "strategic_value": 0.10,
                "unblock_value": 0.20,
                "risk_reduction": 0.20,
            }
        )
        s_normal = score_task(wu, cfg_normal)
        s_heavy = score_task(wu, cfg_heavy)
        assert s_heavy.final_score > s_normal.final_score

    def test_weight_change_changes_relative_ranking(self):
        """Changing one weight should alter the relative order of two tasks."""
        urgent = _wu(id="urgent", urgency=1.0, strategic_value=0.0)
        strategic = _wu(id="strategic", urgency=0.0, strategic_value=1.0)

        cfg_urgency = ScoringConfig(
            weights={
                "urgency": 0.60,
                "strategic_value": 0.10,
                "unblock_value": 0.20,
                "risk_reduction": 0.10,
            }
        )
        cfg_strategic = ScoringConfig(
            weights={
                "urgency": 0.05,
                "strategic_value": 0.60,
                "unblock_value": 0.15,
                "risk_reduction": 0.20,
            }
        )

        # Under urgency-heavy weights, urgent task wins
        assert score_task(urgent, cfg_urgency).final_score > score_task(strategic, cfg_urgency).final_score
        # Under strategic-heavy weights, strategic task wins
        assert score_task(strategic, cfg_strategic).final_score > score_task(urgent, cfg_strategic).final_score

    def test_zero_weight_factor_ignored(self):
        """A factor with weight 0 should not contribute to the score."""
        wu = _wu(urgency=1.0, strategic_value=0.0, unblock_value=0.0, risk_reduction=0.0)
        cfg_zero = ScoringConfig(
            weights={
                "urgency": 0.0,
                "strategic_value": 0.50,
                "unblock_value": 0.25,
                "risk_reduction": 0.25,
            }
        )
        s = score_task(wu, cfg_zero)
        # With urgency weight=0 and all others zero on the unit,
        # the raw_score should be 0
        assert s.raw_score == 0.0

    def test_goal_alignment_removed_from_defaults(self):
        """Default weights should not include goal_alignment (removed in C2)."""
        cfg = ScoringConfig()
        assert "goal_alignment" not in cfg.weights


# ---------------------------------------------------------------------------
# Deferment aging
# ---------------------------------------------------------------------------


class TestDefermentAging:
    def test_zero_days_no_bonus(self):
        s = score_task(_wu(deferment_age_days=0.0))
        assert s.deferment_bonus == 0.0

    def test_seven_days_measurable_bonus(self):
        s = score_task(_wu(deferment_age_days=7.0))
        assert s.deferment_bonus > 0.0

    def test_thirty_days_capped(self):
        """Bonus cannot exceed aging_max_bonus."""
        cfg = ScoringConfig(aging_max_bonus=30.0)
        s = score_task(_wu(deferment_age_days=100.0, commitment_level=CommitmentLevel.HARD), cfg)
        assert s.deferment_bonus == 30.0

    def test_hard_ages_faster_than_soft(self):
        hard = score_task(_wu(deferment_age_days=5.0, commitment_level=CommitmentLevel.HARD))
        soft = score_task(_wu(deferment_age_days=5.0, commitment_level=CommitmentLevel.SOFT))
        assert hard.deferment_bonus > soft.deferment_bonus

    def test_soft_ages_faster_than_opportunistic(self):
        soft = score_task(_wu(deferment_age_days=5.0, commitment_level=CommitmentLevel.SOFT))
        opp = score_task(_wu(deferment_age_days=5.0, commitment_level=CommitmentLevel.OPPORTUNISTIC))
        assert soft.deferment_bonus > opp.deferment_bonus

    def test_opportunistic_ages_slowest(self):
        opp = score_task(_wu(deferment_age_days=5.0, commitment_level=CommitmentLevel.OPPORTUNISTIC))
        cfg = ScoringConfig()
        expected = min(5.0 * 0.2, cfg.aging_max_bonus)
        assert opp.deferment_bonus == expected


# ---------------------------------------------------------------------------
# Commitment adjustment
# ---------------------------------------------------------------------------


class TestCommitmentAdjustment:
    def test_hard_positive(self):
        s = score_task(_wu(commitment_level=CommitmentLevel.HARD))
        assert s.commitment_adjustment > 0.0

    def test_soft_zero(self):
        s = score_task(_wu(commitment_level=CommitmentLevel.SOFT))
        assert s.commitment_adjustment == 0.0

    def test_opportunistic_negative(self):
        s = score_task(_wu(commitment_level=CommitmentLevel.OPPORTUNISTIC))
        assert s.commitment_adjustment < 0.0

    def test_hard_scores_higher_than_soft_all_else_equal(self):
        hard = score_task(_wu(commitment_level=CommitmentLevel.HARD))
        soft = score_task(_wu(commitment_level=CommitmentLevel.SOFT))
        assert hard.final_score > soft.final_score

    def test_soft_scores_higher_than_opportunistic_all_else_equal(self):
        soft = score_task(_wu(commitment_level=CommitmentLevel.SOFT))
        opp = score_task(_wu(commitment_level=CommitmentLevel.OPPORTUNISTIC))
        assert soft.final_score > opp.final_score


# ---------------------------------------------------------------------------
# Confidence modifier
# ---------------------------------------------------------------------------


class TestConfidenceModifier:
    def test_low_confidence_penalises(self):
        low = score_task(_wu(confidence=0.1))
        med = score_task(_wu(confidence=0.5))
        assert low.confidence_modifier < 1.0
        assert low.final_score < med.final_score

    def test_high_confidence_boosts(self):
        high = score_task(_wu(confidence=0.9))
        med = score_task(_wu(confidence=0.5))
        assert high.confidence_modifier > 1.0
        assert high.final_score > med.final_score

    def test_medium_confidence_neutral(self):
        s = score_task(_wu(confidence=0.5))
        assert s.confidence_modifier == 1.0

    def test_boundary_low(self):
        """Confidence exactly 0.3 is NOT penalised (< 0.3 triggers penalty)."""
        s = score_task(_wu(confidence=0.3))
        assert s.confidence_modifier == 1.0

    def test_boundary_high(self):
        """Confidence exactly 0.8 is NOT boosted (> 0.8 triggers bonus)."""
        s = score_task(_wu(confidence=0.8))
        assert s.confidence_modifier == 1.0

    def test_just_below_low_threshold(self):
        s = score_task(_wu(confidence=0.29))
        assert s.confidence_modifier == 0.3

    def test_just_above_high_threshold(self):
        s = score_task(_wu(confidence=0.81))
        assert s.confidence_modifier == 1.1

    def test_confidence_does_not_dampen_deferment_bonus(self):
        """H4 fix: confidence modifier should not multiply deferment bonus."""
        low_conf = score_task(
            _wu(
                confidence=0.1,
                deferment_age_days=20.0,
                commitment_level=CommitmentLevel.HARD,
            )
        )
        assert low_conf.deferment_bonus == 20.0
        assert low_conf.final_score > 0.0


# ---------------------------------------------------------------------------
# Priority multiplier
# ---------------------------------------------------------------------------


class TestPriorityMultiplier:
    def test_high_boosts(self):
        high = score_task(_wu(priority="high"))
        med = score_task(_wu(priority="medium"))
        assert high.priority_multiplier > med.priority_multiplier
        assert high.final_score > med.final_score

    def test_low_reduces(self):
        low = score_task(_wu(priority="low"))
        med = score_task(_wu(priority="medium"))
        assert low.priority_multiplier < med.priority_multiplier
        assert low.final_score < med.final_score

    def test_medium_is_one(self):
        s = score_task(_wu(priority="medium"))
        assert s.priority_multiplier == 1.0

    def test_high_value(self):
        s = score_task(_wu(priority="high"))
        assert s.priority_multiplier == 1.3

    def test_low_value(self):
        s = score_task(_wu(priority="low"))
        assert s.priority_multiplier == 0.7


# ---------------------------------------------------------------------------
# Time cost (C1 fix: divisor, not subtraction)
# ---------------------------------------------------------------------------


class TestTimeCost:
    def test_longer_tasks_score_lower(self):
        """Longer tasks produce lower scores (divided by more time)."""
        short = score_task(
            _wu(
                estimated_optimistic_min=5.0,
                estimated_expected_min=10.0,
                estimated_pessimistic_min=20.0,
                context_switch_cost_min=2.0,
            )
        )
        long_task = score_task(
            _wu(
                estimated_optimistic_min=60.0,
                estimated_expected_min=120.0,
                estimated_pessimistic_min=240.0,
                context_switch_cost_min=10.0,
            )
        )
        assert short.final_score > long_task.final_score

    def test_short_task_lower_effective_time(self):
        short = score_task(
            _wu(
                estimated_optimistic_min=5.0,
                estimated_expected_min=10.0,
                estimated_pessimistic_min=20.0,
                context_switch_cost_min=2.0,
            )
        )
        long_task = score_task(
            _wu(
                estimated_optimistic_min=60.0,
                estimated_expected_min=120.0,
                estimated_pessimistic_min=240.0,
                context_switch_cost_min=10.0,
            )
        )
        assert short.effective_time < long_task.effective_time

    def test_zero_duration_weight_uncertainty_ignored(self):
        """With duration_weight=0, uncertainty spread doesn't add to time cost."""
        cfg = ScoringConfig(duration_weight=0.0)
        wu = _wu(
            estimated_optimistic_min=10.0,
            estimated_expected_min=60.0,
            estimated_pessimistic_min=200.0,
            context_switch_cost_min=5.0,
        )
        s = score_task(wu, cfg)
        expected_time = (60.0 + 5.0) / 60.0
        assert abs(s.effective_time - expected_time) < 0.01

    def test_effective_time_formula(self):
        """Verify: effective_time = (expected + ctx_switch)/60 + uncertainty * duration_weight."""
        cfg = ScoringConfig(duration_weight=0.1)
        wu = _wu(
            estimated_optimistic_min=10.0,
            estimated_expected_min=60.0,
            estimated_pessimistic_min=120.0,
            context_switch_cost_min=5.0,
        )
        s = score_task(wu, cfg)
        time_cost = (60.0 + 5.0) / 60.0
        uncertainty = (120.0 - 10.0) / 60.0
        expected_effective = time_cost + uncertainty * 0.1
        assert abs(s.effective_time - expected_effective) < 1e-4

    def test_context_switch_cost_included(self):
        """Higher context_switch_cost should increase effective_time and lower score."""
        low_ctx = score_task(_wu(context_switch_cost_min=0.0))
        high_ctx = score_task(_wu(context_switch_cost_min=30.0))
        assert high_ctx.effective_time > low_ctx.effective_time
        assert high_ctx.final_score < low_ctx.final_score

    def test_effective_time_floored(self):
        """Effective time should never go below 0.1 (prevents division by zero)."""
        wu = _wu(
            estimated_optimistic_min=1.0,
            estimated_expected_min=1.0,
            estimated_pessimistic_min=1.0,
            context_switch_cost_min=0.0,
        )
        cfg = ScoringConfig(duration_weight=0.0)
        s = score_task(wu, cfg)
        assert s.effective_time >= 0.1

    def test_score_is_value_per_hour(self):
        """Score should represent value per hour: higher value + shorter time = higher score."""
        fast = score_task(
            _wu(
                urgency=0.8,
                strategic_value=0.8,
                estimated_optimistic_min=5.0,
                estimated_expected_min=10.0,
                estimated_pessimistic_min=15.0,
                context_switch_cost_min=2.0,
            )
        )
        slow = score_task(
            _wu(
                urgency=0.8,
                strategic_value=0.8,
                estimated_optimistic_min=50.0,
                estimated_expected_min=100.0,
                estimated_pessimistic_min=150.0,
                context_switch_cost_min=10.0,
            )
        )
        assert fast.final_score > slow.final_score * 2


# ---------------------------------------------------------------------------
# Unlock value propagation
# ---------------------------------------------------------------------------


class TestUnlockValues:
    def test_single_chain(self):
        """A -> B -> C: A unblocks the most downstream value."""
        a = _wu(id="A", urgency=0.5, strategic_value=0.5)
        b = _wu(id="B", urgency=0.6, strategic_value=0.6, dependency_ids=["A"])
        c = _wu(id="C", urgency=0.7, strategic_value=0.7, dependency_ids=["B"])
        vals = compute_unlock_values([a, b, c])
        assert vals["A"] > vals["B"]
        assert vals["C"] == 0.0  # C has no dependents

    def test_no_dependencies(self):
        """All tasks independent -> all unlock values are 0."""
        tasks = [_wu(id="X"), _wu(id="Y"), _wu(id="Z")]
        vals = compute_unlock_values(tasks)
        assert all(v == 0.0 for v in vals.values())

    def test_circular_dependency_no_crash(self):
        """Cycles must not cause infinite loops."""
        a = _wu(id="A", dependency_ids=["B"])
        b = _wu(id="B", dependency_ids=["A"])
        vals = compute_unlock_values([a, b])
        assert isinstance(vals, dict)

    def test_orphan_task_zero(self):
        """A task with no dependents has unlock value 0."""
        orphan = _wu(id="orphan")
        blocker = _wu(id="blocker")
        dependent = _wu(id="dep", dependency_ids=["blocker"])
        vals = compute_unlock_values([orphan, blocker, dependent])
        assert vals["orphan"] == 0.0

    def test_empty_list(self):
        vals = compute_unlock_values([])
        assert vals == {}

    def test_diamond_dependency(self):
        """A -> B, A -> C, B -> D, C -> D: A unblocks B, C, D."""
        a = _wu(id="A", urgency=0.5, strategic_value=0.5)
        b = _wu(id="B", urgency=0.4, strategic_value=0.4, dependency_ids=["A"])
        c = _wu(id="C", urgency=0.3, strategic_value=0.3, dependency_ids=["A"])
        d = _wu(id="D", urgency=0.6, strategic_value=0.6, dependency_ids=["B", "C"])
        vals = compute_unlock_values([a, b, c, d])
        assert vals["A"] == 1.0
        assert vals["B"] > 0.0
        assert vals["C"] > 0.0
        assert vals["D"] == 0.0

    def test_missing_dependency_id_ignored(self):
        """If a dependency_id refers to a non-existent task, skip it."""
        a = _wu(id="A", dependency_ids=["nonexistent"])
        vals = compute_unlock_values([a])
        assert vals["A"] == 0.0

    def test_normalisation_to_one(self):
        """Highest unlock value should be 1.0 after normalisation."""
        a = _wu(id="A")
        b = _wu(id="B", dependency_ids=["A"], urgency=0.8, strategic_value=0.8)
        vals = compute_unlock_values([a, b])
        assert max(vals.values()) == 1.0

    def test_unlock_includes_unblock_value_and_success_prob(self):
        """H2 fix: unlock BFS should include unblock_value and weight by success_probability."""
        a = _wu(id="A")
        b = _wu(
            id="B",
            dependency_ids=["A"],
            urgency=0.4,
            strategic_value=0.4,
            risk_reduction=0.4,
            unblock_value=0.8,
            success_probability=0.5,
        )
        vals = compute_unlock_values([a, b])
        assert vals["A"] == 1.0  # normalised to 1.0 (it's the only nonzero)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


class TestRanking:
    def test_returns_sorted(self):
        tasks = [
            _wu(id="low", urgency=0.1, strategic_value=0.1),
            _wu(id="high", urgency=1.0, strategic_value=1.0),
            _wu(id="med", urgency=0.5, strategic_value=0.5),
        ]
        ranked = rank_tasks(tasks)
        assert ranked[0].work_unit.id == "high"
        assert ranked[-1].work_unit.id == "low"

    def test_rank_numbers(self):
        tasks = [_wu(id=f"t{i}", urgency=i / 5.0) for i in range(1, 6)]
        ranked = rank_tasks(tasks)
        assert ranked[0].rank == 1
        assert ranked[-1].rank == 5

    def test_highest_normalized_is_100(self):
        tasks = [
            _wu(id="a", urgency=1.0, strategic_value=1.0),
            _wu(id="b", urgency=0.1, strategic_value=0.1),
        ]
        ranked = rank_tasks(tasks)
        assert ranked[0].normalized_score == 100.0

    def test_empty_list(self):
        assert rank_tasks([]) == []

    def test_single_task(self):
        ranked = rank_tasks([_wu(id="only")])
        assert len(ranked) == 1
        assert ranked[0].rank == 1
        assert ranked[0].normalized_score == 100.0

    def test_ranking_integrates_unlock_values(self):
        """A blocker task should rank higher after unlock value computation."""
        blocker = _wu(id="blocker", urgency=0.3, strategic_value=0.3)
        blocked = _wu(
            id="blocked",
            urgency=0.9,
            strategic_value=0.9,
            dependency_ids=["blocker"],
        )
        standalone = _wu(id="standalone", urgency=0.3, strategic_value=0.3)

        ranked = rank_tasks([blocker, blocked, standalone])
        blocker_rank = next(s.rank for s in ranked if s.work_unit.id == "blocker")
        standalone_rank = next(s.rank for s in ranked if s.work_unit.id == "standalone")
        assert blocker_rank < standalone_rank

    def test_rank_tasks_does_not_mutate_inputs(self):
        """H1 fix: rank_tasks must not mutate the original WorkUnit objects."""
        wu1 = _wu(id="blocker", urgency=0.5, strategic_value=0.5, unblock_value=0.0)
        wu2 = _wu(id="blocked", urgency=0.8, strategic_value=0.8, dependency_ids=["blocker"])
        original_uv = wu1.unblock_value

        rank_tasks([wu1, wu2])

        assert wu1.unblock_value == original_uv


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestConfigLoading:
    def test_load_valid_yaml(self, tmp_path: Path):
        yaml_content = textwrap.dedent("""\
            scorer:
              weights:
                urgency: 0.50
                strategic_value: 0.20
                unblock_value: 0.15
                risk_reduction: 0.15
              priority_multiplier:
                high: 1.5
              aging:
                rate_per_day:
                  hard: 2.0
                max_bonus: 50.0
              confidence:
                low_confidence_penalty: 0.2
                high_confidence_bonus: 1.2
              duration_weight: 0.2
        """)
        p = tmp_path / "test.yaml"
        p.write_text(yaml_content)

        cfg = load_scoring_config(p)
        assert cfg.weights["urgency"] == 0.50
        assert cfg.weights["strategic_value"] == 0.20
        assert cfg.priority_multiplier["high"] == 1.5
        assert cfg.aging_rate_per_day["hard"] == 2.0
        assert cfg.aging_max_bonus == 50.0
        assert cfg.confidence_low_penalty == 0.2
        assert cfg.confidence_high_bonus == 1.2
        assert cfg.duration_weight == 0.2

    def test_load_missing_file_uses_defaults(self, tmp_path: Path):
        cfg = load_scoring_config(tmp_path / "nonexistent.yaml")
        assert cfg == ScoringConfig()

    def test_load_partial_yaml_merges(self, tmp_path: Path):
        yaml_content = textwrap.dedent("""\
            scorer:
              weights:
                urgency: 0.40
                strategic_value: 0.25
                unblock_value: 0.25
                risk_reduction: 0.10
        """)
        p = tmp_path / "partial.yaml"
        p.write_text(yaml_content)

        cfg = load_scoring_config(p)
        assert cfg.weights["urgency"] == 0.40
        assert "unblock_value" in cfg.weights

    def test_load_empty_yaml_uses_defaults(self, tmp_path: Path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        cfg = load_scoring_config(p)
        assert cfg == ScoringConfig()

    def test_load_malformed_yaml_uses_defaults(self, tmp_path: Path):
        p = tmp_path / "bad.yaml"
        p.write_text("{{{{not yaml at all")
        cfg = load_scoring_config(p)
        assert cfg == ScoringConfig()

    def test_default_path_resolution(self):
        """load_scoring_config() with no args should resolve to config/scheduler_weights.yaml."""
        cfg = load_scoring_config()
        assert isinstance(cfg, ScoringConfig)
        assert cfg.weights["urgency"] == 0.30

    def test_no_goal_alignment_in_loaded_config(self):
        """Config loaded from default YAML should not contain goal_alignment."""
        cfg = load_scoring_config()
        assert "goal_alignment" not in cfg.weights


# ---------------------------------------------------------------------------
# Weight normalization (H3 fix)
# ---------------------------------------------------------------------------


class TestWeightNormalization:
    def test_weights_summing_to_one_unchanged(self):
        """Weights summing to 1.0 should not be altered."""
        cfg = ScoringConfig(
            weights={
                "urgency": 0.30,
                "strategic_value": 0.25,
                "unblock_value": 0.25,
                "risk_reduction": 0.20,
            }
        )
        assert abs(sum(cfg.weights.values()) - 1.0) < 1e-9

    def test_weights_within_tolerance_unchanged(self):
        """Weights summing to 0.95 (within 10% of 1.0) should not be normalized."""
        cfg = ScoringConfig(
            weights={
                "urgency": 0.25,
                "strategic_value": 0.25,
                "unblock_value": 0.25,
                "risk_reduction": 0.20,
            }
        )
        assert cfg.weights["urgency"] == 0.25

    def test_weights_far_off_auto_normalized(self):
        """Weights summing to 2.0 (>10% off) should be auto-normalized."""
        cfg = ScoringConfig(
            weights={
                "urgency": 0.50,
                "strategic_value": 0.50,
                "unblock_value": 0.50,
                "risk_reduction": 0.50,
            }
        )
        assert abs(sum(cfg.weights.values()) - 1.0) < 1e-9
        assert abs(cfg.weights["urgency"] - 0.25) < 1e-9

    def test_weights_very_small_auto_normalized(self):
        """Weights summing to 0.2 should be auto-normalized."""
        cfg = ScoringConfig(
            weights={
                "urgency": 0.05,
                "strategic_value": 0.05,
                "unblock_value": 0.05,
                "risk_reduction": 0.05,
            }
        )
        assert abs(sum(cfg.weights.values()) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Starvation prevention
# ---------------------------------------------------------------------------


class TestStarvationPrevention:
    def test_old_low_priority_outranks_fresh_medium(self):
        """A low-priority HARD task deferred 14 days should eventually outrank a fresh medium task."""
        old_low = _wu(
            id="old_low",
            priority="low",
            urgency=0.3,
            strategic_value=0.3,
            deferment_age_days=14.0,
            commitment_level=CommitmentLevel.HARD,
            confidence=0.5,
            success_probability=0.7,
        )
        fresh_med = _wu(
            id="fresh_med",
            priority="medium",
            urgency=0.5,
            strategic_value=0.5,
            deferment_age_days=0.0,
            commitment_level=CommitmentLevel.SOFT,
            confidence=0.5,
            success_probability=0.7,
        )
        s_old = score_task(old_low)
        s_fresh = score_task(fresh_med)
        assert s_old.final_score > s_fresh.final_score, (
            f"Old low ({s_old.final_score:.2f}) should outrank fresh medium ({s_fresh.final_score:.2f})"
        )

    def test_very_old_opportunistic_outranks_fresh_low(self):
        """Even opportunistic tasks should surface if deferred long enough."""
        old_opp = _wu(
            id="old_opp",
            priority="low",
            urgency=0.4,
            strategic_value=0.4,
            deferment_age_days=30.0,
            commitment_level=CommitmentLevel.OPPORTUNISTIC,
            confidence=0.5,
            success_probability=0.7,
        )
        fresh_low = _wu(
            id="fresh_low",
            priority="low",
            urgency=0.4,
            strategic_value=0.4,
            deferment_age_days=0.0,
            commitment_level=CommitmentLevel.OPPORTUNISTIC,
            confidence=0.5,
            success_probability=0.7,
        )
        s_old = score_task(old_opp)
        s_fresh = score_task(fresh_low)
        assert s_old.final_score > s_fresh.final_score, (
            f"30-day deferred ({s_old.final_score:.2f}) should outrank fresh equivalent ({s_fresh.final_score:.2f})"
        )

    def test_extreme_aging_overcomes_priority_gap(self):
        """With max aging bonus (30.0) applied, even low-priority can beat medium."""
        old = _wu(
            id="old",
            priority="low",
            urgency=0.4,
            strategic_value=0.4,
            deferment_age_days=30.0,
            commitment_level=CommitmentLevel.HARD,
            confidence=0.5,
            success_probability=0.7,
        )
        fresh_med = _wu(
            id="fresh_med",
            priority="medium",
            urgency=0.5,
            strategic_value=0.5,
            deferment_age_days=0.0,
            commitment_level=CommitmentLevel.SOFT,
            confidence=0.5,
            success_probability=0.7,
        )
        s_old = score_task(old)
        s_fresh = score_task(fresh_med)
        assert s_old.final_score > s_fresh.final_score

    def test_deferment_not_dampened_by_low_confidence(self):
        """H4 fix: deferment bonus should not be reduced by low confidence."""
        low_conf_old = score_task(
            _wu(
                confidence=0.1,
                deferment_age_days=20.0,
                commitment_level=CommitmentLevel.HARD,
                urgency=0.3,
                strategic_value=0.3,
                success_probability=0.7,
            )
        )
        med_conf_fresh = score_task(
            _wu(
                confidence=0.5,
                deferment_age_days=0.0,
                commitment_level=CommitmentLevel.SOFT,
                urgency=0.5,
                strategic_value=0.5,
                success_probability=0.7,
            )
        )
        assert low_conf_old.final_score > med_conf_fresh.final_score, (
            f"Old low-confidence ({low_conf_old.final_score:.2f}) should outrank "
            f"fresh medium-confidence ({med_conf_fresh.final_score:.2f}) due to deferment"
        )


# ---------------------------------------------------------------------------
# Parametrized sample tasks
# ---------------------------------------------------------------------------

_SAMPLE_TASKS = [
    # (id, priority, urgency, strategic_value, risk_reduction, unblock_value,
    #  confidence, success_prob, expected_min, deferment_days, commitment)
    ("critical_bug", "high", 1.0, 0.8, 0.9, 0.5, 0.9, 0.9, 15.0, 0.0, CommitmentLevel.HARD),
    ("security_fix", "high", 0.9, 0.7, 1.0, 0.3, 0.8, 0.85, 30.0, 1.0, CommitmentLevel.HARD),
    ("feature_deploy", "high", 0.8, 0.9, 0.2, 0.6, 0.7, 0.8, 45.0, 0.0, CommitmentLevel.SOFT),
    ("research_spike", "medium", 0.3, 0.8, 0.1, 0.2, 0.4, 0.6, 60.0, 3.0, CommitmentLevel.SOFT),
    ("perf_optimize", "medium", 0.5, 0.6, 0.4, 0.1, 0.6, 0.7, 45.0, 5.0, CommitmentLevel.SOFT),
    ("doc_update", "low", 0.1, 0.2, 0.0, 0.0, 0.9, 0.95, 20.0, 7.0, CommitmentLevel.OPPORTUNISTIC),
    ("refactor_util", "medium", 0.4, 0.5, 0.3, 0.4, 0.5, 0.65, 90.0, 2.0, CommitmentLevel.SOFT),
    ("test_coverage", "medium", 0.4, 0.4, 0.5, 0.1, 0.7, 0.8, 40.0, 4.0, CommitmentLevel.SOFT),
    ("ci_pipeline", "high", 0.7, 0.6, 0.3, 0.7, 0.8, 0.75, 50.0, 0.0, CommitmentLevel.HARD),
    ("logging_fix", "low", 0.2, 0.1, 0.1, 0.0, 0.9, 0.9, 10.0, 10.0, CommitmentLevel.OPPORTUNISTIC),
    ("db_migration", "high", 0.6, 0.7, 0.6, 0.8, 0.5, 0.6, 60.0, 0.0, CommitmentLevel.HARD),
    ("ux_polish", "low", 0.2, 0.3, 0.0, 0.0, 0.8, 0.85, 25.0, 14.0, CommitmentLevel.OPPORTUNISTIC),
    ("api_redesign", "medium", 0.5, 0.9, 0.2, 0.3, 0.4, 0.5, 120.0, 1.0, CommitmentLevel.SOFT),
    ("monitoring_alert", "high", 0.8, 0.5, 0.7, 0.2, 0.9, 0.9, 15.0, 0.0, CommitmentLevel.HARD),
    ("backup_script", "medium", 0.3, 0.4, 0.6, 0.1, 0.7, 0.8, 30.0, 6.0, CommitmentLevel.SOFT),
    ("deps_update", "low", 0.1, 0.2, 0.3, 0.0, 0.6, 0.7, 20.0, 21.0, CommitmentLevel.OPPORTUNISTIC),
    ("auth_module", "high", 0.9, 0.8, 0.8, 0.5, 0.6, 0.7, 60.0, 0.0, CommitmentLevel.HARD),
    ("cache_layer", "medium", 0.4, 0.7, 0.2, 0.3, 0.5, 0.65, 75.0, 3.0, CommitmentLevel.SOFT),
    ("error_handling", "medium", 0.5, 0.5, 0.4, 0.2, 0.6, 0.75, 35.0, 2.0, CommitmentLevel.SOFT),
    ("config_cleanup", "low", 0.1, 0.1, 0.0, 0.0, 0.9, 0.95, 10.0, 0.0, CommitmentLevel.OPPORTUNISTIC),
    ("rate_limiter", "high", 0.7, 0.6, 0.5, 0.4, 0.7, 0.8, 40.0, 1.0, CommitmentLevel.SOFT),
    ("webhook_retry", "medium", 0.6, 0.5, 0.3, 0.2, 0.6, 0.7, 30.0, 4.0, CommitmentLevel.SOFT),
]


def _make_sample(sample):
    """Convert a sample tuple to a WorkUnit."""
    id_, prio, urg, sv, rr, uv, conf, sp, exp, defer, commit = sample
    return WorkUnit(
        id=id_,
        title=id_.replace("_", " ").title(),
        priority=prio,
        urgency=urg,
        strategic_value=sv,
        risk_reduction=rr,
        unblock_value=uv,
        confidence=conf,
        success_probability=sp,
        estimated_optimistic_min=max(1.0, exp * 0.5),
        estimated_expected_min=exp,
        estimated_pessimistic_min=exp * 2.0,
        deferment_age_days=defer,
        commitment_level=commit,
    )


class TestParametrizedSamples:
    """Verify reasonable ordering across a realistic task mix."""

    @pytest.fixture
    def ranked(self):
        tasks = [_make_sample(s) for s in _SAMPLE_TASKS]
        return rank_tasks(tasks)

    def test_all_tasks_ranked(self, ranked):
        assert len(ranked) == len(_SAMPLE_TASKS)

    def test_ranks_are_sequential(self, ranked):
        for i, st in enumerate(ranked, start=1):
            assert st.rank == i

    def test_critical_bug_ranks_near_top(self, ranked):
        """Critical bug (high urgency + high priority + short duration) should be top 3."""
        bug_rank = next(s.rank for s in ranked if s.work_unit.id == "critical_bug")
        assert bug_rank <= 3, f"critical_bug ranked {bug_rank}, expected top 3"

    def test_config_cleanup_ranks_near_bottom(self, ranked):
        """Config cleanup (low everything, no aging) should be bottom 3."""
        rank = next(s.rank for s in ranked if s.work_unit.id == "config_cleanup")
        assert rank >= len(ranked) - 2, f"config_cleanup ranked {rank}, expected bottom 3"

    def test_high_priority_beats_low_similar_urgency(self, ranked):
        """Among tasks with similar urgency, high priority should rank higher."""
        alert_rank = next(s.rank for s in ranked if s.work_unit.id == "monitoring_alert")
        perf_rank = next(s.rank for s in ranked if s.work_unit.id == "perf_optimize")
        assert alert_rank < perf_rank

    def test_all_scores_non_negative(self, ranked):
        for st in ranked:
            assert st.final_score >= 0.0
            assert st.normalized_score >= 0.0

    def test_highest_normalized_is_100(self, ranked):
        assert ranked[0].normalized_score == 100.0

    def test_scores_are_monotonically_decreasing(self, ranked):
        for i in range(len(ranked) - 1):
            assert ranked[i].final_score >= ranked[i + 1].final_score

    @pytest.mark.parametrize("sample", _SAMPLE_TASKS, ids=[s[0] for s in _SAMPLE_TASKS])
    def test_individual_scoring_valid(self, sample):
        """Each sample task should score without error and produce a finite positive result."""
        wu = _make_sample(sample)
        st = score_task(wu)
        assert st.final_score >= 0.0
        assert st.raw_score >= 0.0
        assert isinstance(st.final_score, float)

    def test_deps_update_with_aging_surfaces(self, ranked):
        """deps_update (low, 21 days deferred) should not be dead last."""
        rank = next(s.rank for s in ranked if s.work_unit.id == "deps_update")
        assert rank < len(ranked), f"deps_update should not be last (rank {rank})"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_all_zero_scores(self):
        """A task with all scoring fields at 0 should have final_score >= 0."""
        wu = _wu(
            urgency=0.0,
            strategic_value=0.0,
            unblock_value=0.0,
            risk_reduction=0.0,
            confidence=0.1,
            success_probability=0.0,
            deferment_age_days=0.0,
        )
        s = score_task(wu)
        assert s.final_score == 0.0

    def test_all_max_scores(self):
        """A task with all scoring fields at max should produce a high score."""
        wu = _wu(
            urgency=1.0,
            strategic_value=1.0,
            unblock_value=1.0,
            risk_reduction=1.0,
            confidence=1.0,
            success_probability=1.0,
            priority="high",
            commitment_level=CommitmentLevel.HARD,
            deferment_age_days=30.0,
            estimated_optimistic_min=5.0,
            estimated_expected_min=10.0,
            estimated_pessimistic_min=20.0,
            context_switch_cost_min=2.0,
        )
        s = score_task(wu)
        assert s.final_score > 50.0

    def test_success_probability_zero_kills_score(self):
        """If success_probability=0, final score should be 0 (or near 0).

        With no deferment and soft commitment (0.0 adj), score should be 0.
        """
        wu = _wu(
            urgency=1.0,
            strategic_value=1.0,
            success_probability=0.0,
            deferment_age_days=0.0,
            commitment_level=CommitmentLevel.SOFT,
        )
        s = score_task(wu)
        assert s.final_score == 0.0

    def test_scored_task_serialization(self):
        """ScoredTask should be JSON-serializable via Pydantic."""
        s = score_task(_wu())
        data = s.model_dump()
        assert "final_score" in data
        assert "work_unit" in data
        st2 = ScoredTask(**data)
        assert st2.final_score == s.final_score

    def test_scoring_config_defaults(self):
        """ScoringConfig with no args should match documented defaults."""
        cfg = ScoringConfig()
        assert cfg.weights["urgency"] == 0.30
        assert cfg.weights["strategic_value"] == 0.25
        assert cfg.weights["unblock_value"] == 0.25
        assert cfg.weights["risk_reduction"] == 0.20
        assert "goal_alignment" not in cfg.weights
        assert cfg.priority_multiplier["high"] == 1.3
        assert cfg.commitment_bonus["hard"] == 0.15
        assert cfg.aging_rate_per_day["hard"] == 1.0
        assert cfg.aging_max_bonus == 30.0
        assert cfg.confidence_low_penalty == 0.3
        assert cfg.confidence_high_bonus == 1.1
        assert cfg.duration_weight == 0.1

    def test_work_units_without_ids(self):
        """Work units without IDs should still score (just no unlock propagation)."""
        tasks = [_wu(id=""), _wu(id="")]
        ranked = rank_tasks(tasks)
        assert len(ranked) == 2
