"""Tests for SearchLevelConfig integration with campaign mutations.

Covers:
  - SearchLevelConfig properly constrains mutation selection
  - Level 1 only restricts to parameter perturbation
  - Level 2 enables filter toggles
  - Level 3 enables structural templates
  - Budget allocation respects level constraints
  - End-to-end mutation generation at each level
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# SearchLevelConfig constrains mutation selection
# ---------------------------------------------------------------------------


class TestSearchLevelConstraints:
    """Verify that SearchLevelConfig properly constrains which mutations can run."""

    def test_level1_config_only_permits_level1(self):
        from novatrade.optimization.mutations import select_mutation_level
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        slc = SearchLevelConfig(level=SearchLevel.LEVEL_1)
        for i in range(100):
            level = select_mutation_level(i, 100, search_level_config=slc)
            assert level == SearchLevel.LEVEL_1, f"Level 1 config should only permit Level 1, got {level} at index {i}"

    def test_level2_config_permits_level1_and_level2(self):
        from novatrade.optimization.mutations import select_mutation_level
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        slc = SearchLevelConfig(level=SearchLevel.LEVEL_2)
        levels_seen = set()
        for i in range(100):
            level = select_mutation_level(i, 100, search_level_config=slc)
            assert level in (SearchLevel.LEVEL_1, SearchLevel.LEVEL_2), (
                f"Level 2 config should not permit Level 3, got {level} at index {i}"
            )
            levels_seen.add(level)
        # Should see both L1 and L2
        assert SearchLevel.LEVEL_1 in levels_seen
        assert SearchLevel.LEVEL_2 in levels_seen

    def test_level3_config_permits_all(self):
        from novatrade.optimization.mutations import select_mutation_level
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        slc = SearchLevelConfig(level=SearchLevel.LEVEL_3)
        levels_seen = set()
        for i in range(100):
            level = select_mutation_level(i, 100, search_level_config=slc)
            levels_seen.add(level)
        assert SearchLevel.LEVEL_1 in levels_seen
        assert SearchLevel.LEVEL_2 in levels_seen
        assert SearchLevel.LEVEL_3 in levels_seen


# ---------------------------------------------------------------------------
# Level 1 restricts to parameter perturbation
# ---------------------------------------------------------------------------


class TestLevel1Restriction:
    """When SearchLevel is LEVEL_1, only parameter perturbation should occur."""

    def test_generate_mutated_candidate_level1_type(self):
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import SearchLevel

        base = {"irb_threshold": 0.45, "ema_period": 20}
        for seed in range(10):
            _params, mtype, _details = generate_mutated_candidate(
                base,
                SearchLevel.LEVEL_1,
                seed=seed,
            )
            assert mtype == "level1_perturbation"

    def test_level1_candidate_modifies_params(self):
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import SearchLevel

        base = {"irb_threshold": 0.45, "ema_period": 20}
        params, _mtype, details = generate_mutated_candidate(
            base,
            SearchLevel.LEVEL_1,
            seed=42,
        )
        # Should return valid params (numeric values)
        assert isinstance(params, dict)
        assert all(isinstance(v, (int, float)) for v in params.values())

    def test_level1_does_not_include_filter_toggles(self):
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import SearchLevel

        base = {"irb_threshold": 0.45}
        _params, _mtype, details = generate_mutated_candidate(
            base,
            SearchLevel.LEVEL_1,
            seed=42,
        )
        assert "toggles" not in details
        assert "composition" not in details

    def test_level1_within_parameter_bounds(self):
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import SearchLevel

        base = {"irb_threshold": 0.45, "ema_period": 20}
        params, _mtype, _details = generate_mutated_candidate(
            base,
            SearchLevel.LEVEL_1,
            seed=42,
        )
        bounds = StrategyConfig.PARAMETER_BOUNDS
        for key, val in params.items():
            if key in bounds:
                b = bounds[key]
                assert b.min_val <= val <= b.max_val, f"Param {key}={val} out of bounds [{b.min_val}, {b.max_val}]"


# ---------------------------------------------------------------------------
# Level 2 enables filter toggles
# ---------------------------------------------------------------------------


class TestLevel2EnablesFilters:
    """When SearchLevel is LEVEL_2, filter mutations should be applied."""

    def test_level2_mutation_type_is_filter(self):
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import SearchLevel

        base = {"irb_threshold": 0.45}
        _params, mtype, _details = generate_mutated_candidate(
            base,
            SearchLevel.LEVEL_2,
            seed=42,
        )
        assert mtype == "level2_filter"

    def test_level2_includes_toggle_details(self):
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import SearchLevel

        base = {"irb_threshold": 0.45}
        _params, _mtype, details = generate_mutated_candidate(
            base,
            SearchLevel.LEVEL_2,
            seed=42,
        )
        assert "toggles" in details
        assert isinstance(details["toggles"], list)

    def test_level2_toggles_reference_registered_filters(self):
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import (
            EXIT_RULE_VARIANTS,
            REGISTERED_FILTERS,
            SESSION_FILTER_CHOICES,
            THRESHOLD_PRESETS,
            SearchLevel,
        )

        base = {"irb_threshold": 0.45}
        for seed in range(20):
            _params, _mtype, details = generate_mutated_candidate(
                base,
                SearchLevel.LEVEL_2,
                seed=seed,
            )
            for toggle in details["toggles"]:
                if toggle["type"] == "filter":
                    assert toggle["name"] in REGISTERED_FILTERS
                elif toggle["type"] == "threshold_preset":
                    assert toggle["value"] in THRESHOLD_PRESETS
                elif toggle["type"] == "exit_variant":
                    assert toggle["name"] in EXIT_RULE_VARIANTS
                elif toggle["type"] == "session":
                    assert toggle["value"] in SESSION_FILTER_CHOICES

    def test_level2_with_doctrine_respects_mandatory(self):
        from novatrade.doctrine.schema import DoctrineData, StrategyDoctrine
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import SearchLevel

        doctrine = StrategyDoctrine(
            name="test",
            concept="Test strategy",
            data=DoctrineData(pair="EURUSD"),
            filters_mandatory=["trend_alignment"],
            filters_forbidden=["news_blackout"],
        )
        base = {"irb_threshold": 0.45, "filters_enabled": []}

        for seed in range(20):
            _params, _mtype, details = generate_mutated_candidate(
                base,
                SearchLevel.LEVEL_2,
                doctrine=doctrine,
                seed=seed,
            )
            # Mandatory filter should appear as enabled in toggles
            mandatory_toggles = [
                t for t in details["toggles"] if t["type"] == "filter" and t["name"] == "trend_alignment"
            ]
            assert any(t["enabled"] for t in mandatory_toggles), f"Mandatory filter should be enabled (seed={seed})"


# ---------------------------------------------------------------------------
# Level 3 enables structural templates
# ---------------------------------------------------------------------------


class TestLevel3EnablesTemplates:
    """When SearchLevel is LEVEL_3, structural template swaps should occur."""

    def test_level3_mutation_type_is_structural(self):
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import SearchLevel

        base = {"irb_threshold": 0.45}
        _params, mtype, _details = generate_mutated_candidate(
            base,
            SearchLevel.LEVEL_3,
            seed=42,
        )
        assert mtype == "level3_structural"

    def test_level3_includes_composition(self):
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import SearchLevel

        base = {"irb_threshold": 0.45}
        _params, _mtype, details = generate_mutated_candidate(
            base,
            SearchLevel.LEVEL_3,
            seed=42,
        )
        assert "composition" in details
        assert isinstance(details["composition"], str)

    def test_level3_composition_is_registered(self):
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import REGISTERED_COMPOSITIONS, SearchLevel

        base = {"irb_threshold": 0.45}
        for seed in range(20):
            _params, _mtype, details = generate_mutated_candidate(
                base,
                SearchLevel.LEVEL_3,
                seed=seed,
            )
            assert details["composition"] in REGISTERED_COMPOSITIONS

    def test_level3_params_match_template(self):
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import (
            Level3Template,
            SearchLevel,
            get_active_params_for_template,
        )

        base = {"irb_threshold": 0.45, "ema_period": 20, "atr_period": 14}
        params, _mtype, details = generate_mutated_candidate(
            base,
            SearchLevel.LEVEL_3,
            seed=42,
        )
        template = Level3Template(composition=details["composition"])
        active = set(get_active_params_for_template(template))
        for key in params:
            assert key in active, f"Param '{key}' not in template '{details['composition']}' active params"


# ---------------------------------------------------------------------------
# Budget allocation respects level constraints
# ---------------------------------------------------------------------------


class TestBudgetWithLevelConstraints:
    """Verify that mutation budget + level config interact correctly."""

    def test_budget_allocation_with_level1_cap(self):
        from novatrade.optimization.mutations import MutationBudget, select_mutation_level
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        # Even with a budget that allocates to L2/L3, L1 cap should override
        budget = MutationBudget(level1_pct=0.10, level2_pct=0.40, level3_pct=0.50)
        slc = SearchLevelConfig(level=SearchLevel.LEVEL_1)

        for i in range(100):
            level = select_mutation_level(i, 100, budget=budget, search_level_config=slc)
            assert level == SearchLevel.LEVEL_1

    def test_budget_allocation_with_level2_cap(self):
        from novatrade.optimization.mutations import MutationBudget, select_mutation_level
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        budget = MutationBudget(level1_pct=0.10, level2_pct=0.40, level3_pct=0.50)
        slc = SearchLevelConfig(level=SearchLevel.LEVEL_2)

        levels = set()
        for i in range(100):
            level = select_mutation_level(i, 100, budget=budget, search_level_config=slc)
            levels.add(level)
            assert level != SearchLevel.LEVEL_3

        assert SearchLevel.LEVEL_1 in levels
        assert SearchLevel.LEVEL_2 in levels

    def test_no_config_allows_all_levels(self):
        from novatrade.optimization.mutations import select_mutation_level

        levels = set()
        for i in range(100):
            level = select_mutation_level(i, 100)
            levels.add(level)
        assert len(levels) == 3


# ---------------------------------------------------------------------------
# End-to-end campaign mutation flow
# ---------------------------------------------------------------------------


class TestEndToEndMutationFlow:
    """Integration tests for the full mutation pipeline."""

    def test_full_mutation_cycle(self):
        """Generate mutations at all three levels and verify they produce valid output."""
        from novatrade.optimization.mutations import (
            AncestryTracker,
            MutationRecord,
            generate_mutated_candidate,
        )
        from novatrade.optimization.search_levels import SearchLevel

        base = {
            "irb_threshold": 0.45,
            "ema_period": 20,
            "atr_period": 14,
            "adx_period": 14,
            "trend_slope_threshold": 0.4,
            "adx_threshold": 20.0,
            "overextension_threshold": 2.0,
            "trail_atr_multiplier": 1.5,
            "trigger_window_bars": 20,
            "time_stop_bars": 40,
            "mtf_lookback": 5,
            "warmup_bars": 34,
        }

        tracker = AncestryTracker()

        # Level 1
        p1, mt1, d1 = generate_mutated_candidate(base, SearchLevel.LEVEL_1, seed=1)
        tracker.record(
            MutationRecord(
                experiment_id="exp-L1",
                parent_id=None,
                mutation_type=mt1,
                mutation_details=d1,
            )
        )

        # Level 2
        p2, mt2, d2 = generate_mutated_candidate(base, SearchLevel.LEVEL_2, seed=2)
        tracker.record(
            MutationRecord(
                experiment_id="exp-L2",
                parent_id="exp-L1",
                mutation_type=mt2,
                mutation_details=d2,
            )
        )

        # Level 3
        p3, mt3, d3 = generate_mutated_candidate(base, SearchLevel.LEVEL_3, seed=3)
        tracker.record(
            MutationRecord(
                experiment_id="exp-L3",
                parent_id="exp-L2",
                mutation_type=mt3,
                mutation_details=d3,
            )
        )

        assert len(tracker) == 3
        assert mt1 == "level1_perturbation"
        assert mt2 == "level2_filter"
        assert mt3 == "level3_structural"

        # Verify lineage
        chain = tracker.lineage("exp-L3")
        assert len(chain) == 3
        assert chain[0].experiment_id == "exp-L1"
        assert chain[2].experiment_id == "exp-L3"

    def test_mutation_candidates_differ_from_base(self):
        """Mutations should actually change something vs the base config."""
        from novatrade.optimization.mutations import (
            compute_mutation_diff,
            generate_mutated_candidate,
        )
        from novatrade.optimization.search_levels import SearchLevel

        base = {
            "irb_threshold": 0.45,
            "ema_period": 20,
            "atr_period": 14,
        }

        # Level 1 should perturb at least one param
        p1, _, _ = generate_mutated_candidate(base, SearchLevel.LEVEL_1, seed=42)
        diff1 = compute_mutation_diff(base, p1)
        assert len(diff1) > 0, "Level 1 mutation should change at least one parameter"

    @patch("novatrade.optimization.campaign_engine.execute_backtest")
    def test_campaign_mutation_type_tracking(self, mock_bt):
        """Campaign engine should track mutation types in improvements."""
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.campaign_engine import CampaignConfig, run_campaign
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        call_idx = [0]

        def make_result(*args, **kwargs):
            call_idx[0] += 1
            mock = MagicMock()
            mock.experiment_id = f"exp-{call_idx[0]:03d}"
            mock.scout_score = 0.1 * call_idx[0]  # increasing scores
            mock.status = "ranked"
            return mock

        mock_bt.side_effect = make_result

        cfg = CampaignConfig(
            max_experiments=5,
            stagnation_limit=100,
            enable_mutations=True,
            search_level_config=SearchLevelConfig(level=SearchLevel.LEVEL_3),
            walkforward_on_new_best=False,
        )

        result = run_campaign(StrategyConfig(), [MagicMock()] * 100, [MagicMock()] * 100, campaign_config=cfg)
        assert result.total_experiments == 5
        # All improvements should have mutation_type
        for imp in result.improvements:
            assert "mutation_type" in imp
            assert imp["mutation_type"] in (
                "level1_perturbation",
                "level2_filter",
                "level3_structural",
            )
