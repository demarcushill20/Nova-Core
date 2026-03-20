"""Tests for Level 2/3 mutation generators and ancestry tracking.

Covers:
  - P4.1: generate_filter_mutation with and without doctrine
  - P4.2: generate_structural_mutation
  - P4.3: AncestryTracker (record, lineage, children, JSON round-trip)
  - P4.4: generate_mutated_candidate with different levels
  - compute_mutation_diff
  - select_mutation_level
  - MutationBudget validation
  - ExperimentDB mutation_type column and ancestry queries
  - Campaign engine integration with mutations (mock backtests)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# MutationBudget
# ---------------------------------------------------------------------------


class TestMutationBudget:
    """Verify MutationBudget constraints."""

    def test_default_budget_sums_to_one(self):
        from novatrade.optimization.mutations import MutationBudget

        b = MutationBudget()
        assert abs(b.level1_pct + b.level2_pct + b.level3_pct - 1.0) < 0.01

    def test_custom_budget_accepted(self):
        from novatrade.optimization.mutations import MutationBudget

        b = MutationBudget(level1_pct=0.50, level2_pct=0.30, level3_pct=0.20)
        assert b.level1_pct == 0.50
        assert b.level2_pct == 0.30
        assert b.level3_pct == 0.20

    def test_invalid_budget_rejected(self):
        from novatrade.optimization.mutations import MutationBudget

        with pytest.raises(ValueError, match="must sum to"):
            MutationBudget(level1_pct=0.50, level2_pct=0.10, level3_pct=0.10)

    def test_default_mutation_budget_constant(self):
        from novatrade.optimization.mutations import DEFAULT_MUTATION_BUDGET

        assert DEFAULT_MUTATION_BUDGET.level1_pct == 0.70
        assert DEFAULT_MUTATION_BUDGET.level2_pct == 0.20
        assert DEFAULT_MUTATION_BUDGET.level3_pct == 0.10


# ---------------------------------------------------------------------------
# P4.1: generate_filter_mutation
# ---------------------------------------------------------------------------


class TestGenerateFilterMutation:
    """Level 2 filter mutations — toggle filters, presets, exit variants."""

    def _make_doctrine(self, mandatory=None, optional=None, forbidden=None):
        from novatrade.doctrine.schema import DoctrineData, StrategyDoctrine

        return StrategyDoctrine(
            name="test_doctrine",
            concept="Test strategy for mutation testing",
            data=DoctrineData(pair="EURUSD"),
            filters_mandatory=mandatory or [],
            filters_optional=optional or [],
            filters_forbidden=forbidden or [],
        )

    def test_unconstrained_mutation_returns_dict_and_toggles(self):
        from novatrade.optimization.mutations import generate_filter_mutation

        base = {"irb_threshold": 0.45, "ema_period": 20}
        new_params, toggles = generate_filter_mutation(base, seed=42)
        assert isinstance(new_params, dict)
        assert isinstance(toggles, list)
        assert len(toggles) > 0

    def test_mutation_does_not_modify_base(self):
        from novatrade.optimization.mutations import generate_filter_mutation

        base = {"irb_threshold": 0.45, "filters_enabled": ["adx_strength"]}
        base_copy = dict(base)
        base_copy["filters_enabled"] = list(base["filters_enabled"])
        generate_filter_mutation(base, seed=42)
        assert base == base_copy

    def test_doctrine_mandatory_filters_always_enabled(self):
        from novatrade.optimization.mutations import generate_filter_mutation

        doctrine = self._make_doctrine(mandatory=["trend_alignment"])
        base = {"irb_threshold": 0.45, "filters_enabled": []}

        for seed in range(20):
            new_params, toggles = generate_filter_mutation(base, doctrine=doctrine, seed=seed)
            # Mandatory filter should always end up enabled
            mandatory_toggles = [t for t in toggles if t.name == "trend_alignment"]
            assert any(t.enabled for t in mandatory_toggles), (
                f"Mandatory filter 'trend_alignment' should be enabled (seed={seed})"
            )

    def test_doctrine_forbidden_filters_never_toggled(self):
        from novatrade.optimization.mutations import generate_filter_mutation

        doctrine = self._make_doctrine(forbidden=["news_blackout"])
        base = {"irb_threshold": 0.45, "filters_enabled": []}

        for seed in range(20):
            _new_params, toggles = generate_filter_mutation(base, doctrine=doctrine, seed=seed)
            # No toggle should enable a forbidden filter
            for t in toggles:
                if t.toggle_type == "filter" and t.name == "news_blackout":
                    assert not t.enabled, f"Forbidden filter should not be enabled (seed={seed})"

    def test_mutation_with_doctrine_optional_filters(self):
        from novatrade.optimization.mutations import generate_filter_mutation

        doctrine = self._make_doctrine(
            mandatory=["trend_alignment"],
            optional=["adx_strength", "overextension_guard"],
            forbidden=["news_blackout"],
        )
        base = {"irb_threshold": 0.45, "filters_enabled": ["trend_alignment"]}
        new_params, toggles = generate_filter_mutation(base, doctrine=doctrine, seed=42)
        assert isinstance(new_params, dict)
        assert len(toggles) > 0

    def test_reproducible_with_seed(self):
        from novatrade.optimization.mutations import generate_filter_mutation

        base = {"irb_threshold": 0.45, "ema_period": 20}
        r1, t1 = generate_filter_mutation(base, seed=123)
        r2, t2 = generate_filter_mutation(base, seed=123)
        assert r1 == r2
        assert len(t1) == len(t2)

    def test_threshold_preset_may_be_applied(self):
        """Over many seeds, at least one should apply a threshold preset."""
        from novatrade.optimization.mutations import generate_filter_mutation

        found_preset = False
        base = {"irb_threshold": 0.45, "ema_period": 20}
        for seed in range(100):
            _new, toggles = generate_filter_mutation(base, seed=seed)
            for t in toggles:
                if t.toggle_type == "threshold_preset":
                    found_preset = True
                    break
            if found_preset:
                break
        assert found_preset, "Expected at least one threshold preset toggle in 100 seeds"

    def test_exit_variant_may_be_applied(self):
        """Over many seeds, at least one should apply an exit variant."""
        from novatrade.optimization.mutations import generate_filter_mutation

        found_exit = False
        base = {"irb_threshold": 0.45}
        for seed in range(100):
            _new, toggles = generate_filter_mutation(base, seed=seed)
            for t in toggles:
                if t.toggle_type == "exit_variant":
                    found_exit = True
                    break
            if found_exit:
                break
        assert found_exit, "Expected at least one exit variant toggle in 100 seeds"

    def test_session_filter_may_be_applied(self):
        """Over many seeds, at least one should apply a session filter."""
        from novatrade.optimization.mutations import generate_filter_mutation

        found_session = False
        base = {"irb_threshold": 0.45}
        for seed in range(100):
            _new, toggles = generate_filter_mutation(base, seed=seed)
            for t in toggles:
                if t.toggle_type == "session":
                    found_session = True
                    break
            if found_session:
                break
        assert found_session, "Expected at least one session toggle in 100 seeds"


# ---------------------------------------------------------------------------
# P4.2: generate_structural_mutation
# ---------------------------------------------------------------------------


class TestGenerateStructuralMutation:
    """Level 3 structural template mutations."""

    def test_returns_params_and_template(self):
        from novatrade.optimization.mutations import generate_structural_mutation

        base = {"irb_threshold": 0.45, "ema_period": 20, "atr_period": 14}
        new_params, template = generate_structural_mutation(base, seed=42)
        assert isinstance(new_params, dict)
        assert hasattr(template, "composition")

    def test_template_is_registered(self):
        from novatrade.optimization.mutations import (
            STRUCTURAL_TEMPLATES,
            generate_structural_mutation,
        )

        base = {"irb_threshold": 0.45}
        for seed in range(20):
            _new, template = generate_structural_mutation(base, seed=seed)
            assert template.composition in STRUCTURAL_TEMPLATES

    def test_params_constrained_to_template(self):
        """Output params should only contain keys valid for the selected template."""
        from novatrade.optimization.mutations import generate_structural_mutation
        from novatrade.optimization.search_levels import get_active_params_for_template

        base = {"irb_threshold": 0.45, "ema_period": 20, "atr_period": 14}
        new_params, template = generate_structural_mutation(base, seed=42)
        active = set(get_active_params_for_template(template))
        # All returned params should be in the active set
        for key in new_params:
            assert key in active, f"Param '{key}' not in template '{template.composition}' active set"

    def test_reproducible_with_seed(self):
        from novatrade.optimization.mutations import generate_structural_mutation

        base = {"irb_threshold": 0.45, "ema_period": 20}
        r1, t1 = generate_structural_mutation(base, seed=99)
        r2, t2 = generate_structural_mutation(base, seed=99)
        assert r1 == r2
        assert t1.composition == t2.composition

    def test_different_seeds_produce_variety(self):
        from novatrade.optimization.mutations import generate_structural_mutation

        base = {"irb_threshold": 0.45}
        compositions = set()
        for seed in range(50):
            _new, template = generate_structural_mutation(base, seed=seed)
            compositions.add(template.composition)
        # With 50 seeds, we should see at least 2 different compositions
        assert len(compositions) >= 2


# ---------------------------------------------------------------------------
# P4.3: AncestryTracker
# ---------------------------------------------------------------------------


class TestAncestryTracker:
    """Ancestry tracking for mutation lineage."""

    def test_record_and_retrieve(self):
        from novatrade.optimization.mutations import AncestryTracker, MutationRecord

        tracker = AncestryTracker()
        rec = MutationRecord(
            experiment_id="exp-001",
            parent_id=None,
            mutation_type="level1_perturbation",
        )
        tracker.record(rec)
        assert len(tracker) == 1
        assert tracker.records[0].experiment_id == "exp-001"

    def test_lineage_single_record(self):
        from novatrade.optimization.mutations import AncestryTracker, MutationRecord

        tracker = AncestryTracker()
        tracker.record(
            MutationRecord(
                experiment_id="root",
                parent_id=None,
                mutation_type="level1_perturbation",
            )
        )
        chain = tracker.lineage("root")
        assert len(chain) == 1
        assert chain[0].experiment_id == "root"

    def test_lineage_chain(self):
        from novatrade.optimization.mutations import AncestryTracker, MutationRecord

        tracker = AncestryTracker()
        tracker.record(MutationRecord(experiment_id="root", parent_id=None, mutation_type="level1_perturbation"))
        tracker.record(MutationRecord(experiment_id="child1", parent_id="root", mutation_type="level2_filter"))
        tracker.record(MutationRecord(experiment_id="child2", parent_id="child1", mutation_type="level3_structural"))

        chain = tracker.lineage("child2")
        assert len(chain) == 3
        assert chain[0].experiment_id == "root"
        assert chain[1].experiment_id == "child1"
        assert chain[2].experiment_id == "child2"

    def test_lineage_unknown_id(self):
        from novatrade.optimization.mutations import AncestryTracker

        tracker = AncestryTracker()
        chain = tracker.lineage("nonexistent")
        assert chain == []

    def test_children(self):
        from novatrade.optimization.mutations import AncestryTracker, MutationRecord

        tracker = AncestryTracker()
        tracker.record(MutationRecord(experiment_id="root", parent_id=None, mutation_type="level1_perturbation"))
        tracker.record(MutationRecord(experiment_id="c1", parent_id="root", mutation_type="level2_filter"))
        tracker.record(MutationRecord(experiment_id="c2", parent_id="root", mutation_type="level3_structural"))
        tracker.record(MutationRecord(experiment_id="c3", parent_id="c1", mutation_type="level1_perturbation"))

        children = tracker.children("root")
        assert len(children) == 2
        assert {c.experiment_id for c in children} == {"c1", "c2"}

    def test_children_of_leaf(self):
        from novatrade.optimization.mutations import AncestryTracker, MutationRecord

        tracker = AncestryTracker()
        tracker.record(MutationRecord(experiment_id="leaf", parent_id="root", mutation_type="level1_perturbation"))
        assert tracker.children("leaf") == []

    def test_json_round_trip(self):
        from novatrade.optimization.mutations import AncestryTracker, MutationRecord

        tracker = AncestryTracker()
        tracker.record(
            MutationRecord(
                experiment_id="exp-A",
                parent_id=None,
                mutation_type="level1_perturbation",
                mutation_details={"diff": {"irb_threshold": {"before": 0.45, "after": 0.46}}},
                scout_score=0.75,
            )
        )
        tracker.record(
            MutationRecord(
                experiment_id="exp-B",
                parent_id="exp-A",
                mutation_type="level2_filter",
                mutation_details={"toggles": [{"type": "filter", "name": "adx_strength"}]},
            )
        )

        json_str = tracker.to_json()
        restored = AncestryTracker.from_json(json_str)

        assert len(restored) == 2
        assert restored.records[0].experiment_id == "exp-A"
        assert restored.records[1].experiment_id == "exp-B"
        assert restored.records[0].scout_score == 0.75

    def test_save_and_load(self):
        from novatrade.optimization.mutations import AncestryTracker, MutationRecord

        tracker = AncestryTracker()
        tracker.record(MutationRecord(experiment_id="exp-1", parent_id=None, mutation_type="level1_perturbation"))
        tracker.record(MutationRecord(experiment_id="exp-2", parent_id="exp-1", mutation_type="level2_filter"))

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ancestry.json"
            tracker.save(path)
            assert path.exists()

            loaded = AncestryTracker.load(path)
            assert len(loaded) == 2
            chain = loaded.lineage("exp-2")
            assert len(chain) == 2

    def test_lineage_handles_cycles(self):
        """Ensure lineage doesn't loop forever on circular references."""
        from novatrade.optimization.mutations import AncestryTracker, MutationRecord

        tracker = AncestryTracker()
        tracker.record(MutationRecord(experiment_id="a", parent_id="b", mutation_type="level1_perturbation"))
        tracker.record(MutationRecord(experiment_id="b", parent_id="a", mutation_type="level1_perturbation"))

        chain = tracker.lineage("a")
        # Should terminate, returning at most 2 records
        assert len(chain) <= 2

    def test_mutation_record_to_dict(self):
        from novatrade.optimization.mutations import MutationRecord

        rec = MutationRecord(
            experiment_id="exp-1",
            parent_id="exp-0",
            mutation_type="level2_filter",
            mutation_details={"toggles": []},
            scout_score=0.65,
        )
        d = rec.to_dict()
        assert d["experiment_id"] == "exp-1"
        assert d["parent_id"] == "exp-0"
        assert d["mutation_type"] == "level2_filter"

    def test_mutation_record_from_dict(self):
        from novatrade.optimization.mutations import MutationRecord

        d = {
            "experiment_id": "exp-1",
            "parent_id": None,
            "mutation_type": "level3_structural",
            "mutation_details": {"composition": "irb_minimal"},
            "scout_score": 0.80,
            "timestamp": 1234567890.0,
        }
        rec = MutationRecord.from_dict(d)
        assert rec.experiment_id == "exp-1"
        assert rec.mutation_type == "level3_structural"
        assert rec.scout_score == 0.80


# ---------------------------------------------------------------------------
# P4.4: generate_mutated_candidate
# ---------------------------------------------------------------------------


class TestGenerateMutatedCandidate:
    """Main entry point for the campaign engine."""

    def test_level1_returns_perturbation(self):
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import SearchLevel

        base = {"irb_threshold": 0.45, "ema_period": 20, "atr_period": 14}
        new_params, mtype, details = generate_mutated_candidate(
            base,
            SearchLevel.LEVEL_1,
            seed=42,
        )
        assert mtype == "level1_perturbation"
        assert "diff" in details
        assert isinstance(new_params, dict)

    def test_level2_returns_filter_mutation(self):
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import SearchLevel

        base = {"irb_threshold": 0.45, "ema_period": 20}
        new_params, mtype, details = generate_mutated_candidate(
            base,
            SearchLevel.LEVEL_2,
            seed=42,
        )
        assert mtype == "level2_filter"
        assert "toggles" in details

    def test_level3_returns_structural_mutation(self):
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import SearchLevel

        base = {"irb_threshold": 0.45, "ema_period": 20}
        new_params, mtype, details = generate_mutated_candidate(
            base,
            SearchLevel.LEVEL_3,
            seed=42,
        )
        assert mtype == "level3_structural"
        assert "composition" in details

    def test_level2_with_doctrine(self):
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
        base = {"irb_threshold": 0.45}
        new_params, mtype, details = generate_mutated_candidate(
            base,
            SearchLevel.LEVEL_2,
            doctrine=doctrine,
            seed=42,
        )
        assert mtype == "level2_filter"

    def test_level1_with_search_level_config_l3(self):
        """Level 1 mutation with a Level 3 config should constrain params."""
        from novatrade.optimization.mutations import generate_mutated_candidate
        from novatrade.optimization.search_levels import (
            Level3Template,
            SearchLevel,
            SearchLevelConfig,
        )

        slc = SearchLevelConfig(
            level=SearchLevel.LEVEL_3,
            level3_template=Level3Template(composition="irb_minimal"),
        )
        base = {"irb_threshold": 0.45, "ema_period": 20, "atr_period": 14}
        new_params, mtype, _details = generate_mutated_candidate(
            base,
            SearchLevel.LEVEL_1,
            search_level_config=slc,
            seed=42,
        )
        assert mtype == "level1_perturbation"
        # irb_minimal only has irb + atr_sizing params, so ema_period should not be in output
        assert "ema_period" not in new_params


# ---------------------------------------------------------------------------
# compute_mutation_diff
# ---------------------------------------------------------------------------


class TestComputeMutationDiff:
    """Diff computation between parameter dicts."""

    def test_identical_dicts_no_diff(self):
        from novatrade.optimization.mutations import compute_mutation_diff

        a = {"x": 1, "y": 2}
        diff = compute_mutation_diff(a, a)
        assert diff == {}

    def test_single_change(self):
        from novatrade.optimization.mutations import compute_mutation_diff

        a = {"x": 1, "y": 2}
        b = {"x": 1, "y": 3}
        diff = compute_mutation_diff(a, b)
        assert diff == {"y": {"before": 2, "after": 3}}

    def test_added_key(self):
        from novatrade.optimization.mutations import compute_mutation_diff

        a = {"x": 1}
        b = {"x": 1, "y": 2}
        diff = compute_mutation_diff(a, b)
        assert "y" in diff
        assert diff["y"]["before"] is None
        assert diff["y"]["after"] == 2

    def test_removed_key(self):
        from novatrade.optimization.mutations import compute_mutation_diff

        a = {"x": 1, "y": 2}
        b = {"x": 1}
        diff = compute_mutation_diff(a, b)
        assert "y" in diff
        assert diff["y"]["before"] == 2
        assert diff["y"]["after"] is None

    def test_multiple_changes(self):
        from novatrade.optimization.mutations import compute_mutation_diff

        a = {"x": 1, "y": 2, "z": 3}
        b = {"x": 10, "y": 2, "z": 30}
        diff = compute_mutation_diff(a, b)
        assert "x" in diff
        assert "z" in diff
        assert "y" not in diff


# ---------------------------------------------------------------------------
# select_mutation_level
# ---------------------------------------------------------------------------


class TestSelectMutationLevel:
    """Determine which mutation level for a given experiment index."""

    def test_early_experiments_are_level1(self):
        from novatrade.optimization.mutations import select_mutation_level
        from novatrade.optimization.search_levels import SearchLevel

        level = select_mutation_level(0, 100)
        assert level == SearchLevel.LEVEL_1

    def test_late_experiments_may_be_level3(self):
        from novatrade.optimization.mutations import select_mutation_level
        from novatrade.optimization.search_levels import SearchLevel

        # Default budget: 70% L1, 20% L2, 10% L3
        # For 100 experiments: 0-69 = L1, 70-89 = L2, 90-99 = L3
        level = select_mutation_level(95, 100)
        assert level == SearchLevel.LEVEL_3

    def test_mid_experiments_level2(self):
        from novatrade.optimization.mutations import select_mutation_level
        from novatrade.optimization.search_levels import SearchLevel

        level = select_mutation_level(75, 100)
        assert level == SearchLevel.LEVEL_2

    def test_clamped_by_search_level_config(self):
        from novatrade.optimization.mutations import select_mutation_level
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        # Config says max Level 1
        slc = SearchLevelConfig(level=SearchLevel.LEVEL_1)
        level = select_mutation_level(95, 100, search_level_config=slc)
        assert level == SearchLevel.LEVEL_1

    def test_clamped_to_level2(self):
        from novatrade.optimization.mutations import select_mutation_level
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        slc = SearchLevelConfig(level=SearchLevel.LEVEL_2)
        # Index 95 would normally be L3, but clamped to L2
        level = select_mutation_level(95, 100, search_level_config=slc)
        assert level == SearchLevel.LEVEL_2

    def test_custom_budget(self):
        from novatrade.optimization.mutations import MutationBudget, select_mutation_level
        from novatrade.optimization.search_levels import SearchLevel

        # All level3
        budget = MutationBudget(level1_pct=0.0, level2_pct=0.0, level3_pct=1.0)
        level = select_mutation_level(0, 100, budget=budget)
        assert level == SearchLevel.LEVEL_3

    def test_distribution_across_campaign(self):
        from novatrade.optimization.mutations import select_mutation_level
        from novatrade.optimization.search_levels import SearchLevel

        counts = {SearchLevel.LEVEL_1: 0, SearchLevel.LEVEL_2: 0, SearchLevel.LEVEL_3: 0}
        for i in range(100):
            level = select_mutation_level(i, 100)
            counts[level] += 1

        assert counts[SearchLevel.LEVEL_1] == 70
        assert counts[SearchLevel.LEVEL_2] == 20
        assert counts[SearchLevel.LEVEL_3] == 10


# ---------------------------------------------------------------------------
# ExperimentDB — mutation_type column and ancestry queries
# ---------------------------------------------------------------------------


class TestExperimentDBMutationType:
    """Verify mutation_type column and ancestry queries in ExperimentDB."""

    def _make_db(self, tmp_path):
        from novatrade.storage.experiment_db import ExperimentDB

        return ExperimentDB(tmp_path / "test.db")

    def test_insert_with_mutation_type(self, tmp_path):
        from novatrade.storage.experiment_db import ExperimentRecord

        db = self._make_db(tmp_path)
        record = ExperimentRecord(
            experiment_id="exp-001",
            strategy_config_hash="abc",
            doctrine_hash="def",
            dataset_hash="ghi",
            engine_sha="jkl",
            mutation_type="level2_filter",
        )
        db.insert(record)
        fetched = db.get("exp-001")
        assert fetched is not None
        assert fetched.mutation_type == "level2_filter"
        db.close()

    def test_insert_without_mutation_type_defaults_empty(self, tmp_path):
        from novatrade.storage.experiment_db import ExperimentRecord

        db = self._make_db(tmp_path)
        record = ExperimentRecord(
            experiment_id="exp-002",
            strategy_config_hash="abc",
            doctrine_hash="def",
            dataset_hash="ghi",
            engine_sha="jkl",
        )
        db.insert(record)
        fetched = db.get("exp-002")
        assert fetched is not None
        assert fetched.mutation_type == ""
        db.close()

    def test_count_by_mutation_type(self, tmp_path):
        from novatrade.storage.experiment_db import ExperimentRecord

        db = self._make_db(tmp_path)
        for i, mt in enumerate(["level1_perturbation", "level1_perturbation", "level2_filter", "level3_structural"]):
            db.insert(
                ExperimentRecord(
                    experiment_id=f"exp-{i:03d}",
                    campaign_id="camp-1",
                    strategy_config_hash="h",
                    doctrine_hash="d",
                    dataset_hash="s",
                    engine_sha="e",
                    mutation_type=mt,
                )
            )
        counts = db.count_by_mutation_type("camp-1")
        assert counts["level1_perturbation"] == 2
        assert counts["level2_filter"] == 1
        assert counts["level3_structural"] == 1
        db.close()

    def test_list_by_mutation_type(self, tmp_path):
        from novatrade.storage.experiment_db import ExperimentRecord

        db = self._make_db(tmp_path)
        for i, mt in enumerate(["level1_perturbation", "level2_filter", "level2_filter"]):
            db.insert(
                ExperimentRecord(
                    experiment_id=f"exp-{i:03d}",
                    campaign_id="camp-1",
                    strategy_config_hash="h",
                    doctrine_hash="d",
                    dataset_hash="s",
                    engine_sha="e",
                    mutation_type=mt,
                    scout_score=float(i),
                )
            )
        l2_records = db.list_by_mutation_type("camp-1", "level2_filter")
        assert len(l2_records) == 2
        assert all(r.mutation_type == "level2_filter" for r in l2_records)
        db.close()

    def test_get_children(self, tmp_path):
        from novatrade.storage.experiment_db import ExperimentRecord

        db = self._make_db(tmp_path)
        db.insert(
            ExperimentRecord(
                experiment_id="parent",
                strategy_config_hash="h",
                doctrine_hash="d",
                dataset_hash="s",
                engine_sha="e",
            )
        )
        db.insert(
            ExperimentRecord(
                experiment_id="child1",
                parent_experiment_id="parent",
                strategy_config_hash="h",
                doctrine_hash="d",
                dataset_hash="s",
                engine_sha="e",
            )
        )
        db.insert(
            ExperimentRecord(
                experiment_id="child2",
                parent_experiment_id="parent",
                strategy_config_hash="h",
                doctrine_hash="d",
                dataset_hash="s",
                engine_sha="e",
            )
        )
        children = db.get_children("parent")
        assert len(children) == 2
        assert {c.experiment_id for c in children} == {"child1", "child2"}
        db.close()

    def test_get_lineage(self, tmp_path):
        from novatrade.storage.experiment_db import ExperimentRecord

        db = self._make_db(tmp_path)
        db.insert(
            ExperimentRecord(
                experiment_id="root",
                strategy_config_hash="h",
                doctrine_hash="d",
                dataset_hash="s",
                engine_sha="e",
            )
        )
        db.insert(
            ExperimentRecord(
                experiment_id="gen1",
                parent_experiment_id="root",
                strategy_config_hash="h",
                doctrine_hash="d",
                dataset_hash="s",
                engine_sha="e",
            )
        )
        db.insert(
            ExperimentRecord(
                experiment_id="gen2",
                parent_experiment_id="gen1",
                strategy_config_hash="h",
                doctrine_hash="d",
                dataset_hash="s",
                engine_sha="e",
            )
        )
        lineage = db.get_lineage("gen2")
        assert len(lineage) == 3
        assert lineage[0].experiment_id == "root"
        assert lineage[1].experiment_id == "gen1"
        assert lineage[2].experiment_id == "gen2"
        db.close()

    def test_migration_adds_column_to_existing_db(self, tmp_path):
        """Simulate opening a DB that was created without mutation_type."""
        import sqlite3

        db_path = tmp_path / "old.db"
        # Create DB without mutation_type column
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE experiments (
                experiment_id TEXT PRIMARY KEY,
                parent_experiment_id TEXT,
                campaign_id TEXT,
                strategy_config_hash TEXT NOT NULL,
                doctrine_hash TEXT NOT NULL,
                dataset_hash TEXT NOT NULL,
                engine_sha TEXT NOT NULL,
                gate_a_passed INTEGER,
                gate_b_passed INTEGER,
                gate_c_passed INTEGER,
                gate_results_json TEXT NOT NULL DEFAULT '{}',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                scout_score REAL,
                promotion_score REAL,
                trade_count INTEGER NOT NULL DEFAULT 0,
                profit_factor REAL,
                max_drawdown_pct REAL,
                status TEXT NOT NULL DEFAULT 'valid',
                created_at TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute(
            "INSERT INTO experiments (experiment_id, strategy_config_hash, doctrine_hash, "
            "dataset_hash, engine_sha, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("old-exp", "h", "d", "s", "e", "2026-01-01T00:00:00"),
        )
        conn.commit()
        conn.close()

        # Now open with ExperimentDB — migration should add column
        from novatrade.storage.experiment_db import ExperimentDB

        db = ExperimentDB(db_path)
        record = db.get("old-exp")
        assert record is not None
        assert record.mutation_type == ""  # default from migration
        db.close()


# ---------------------------------------------------------------------------
# Campaign engine integration with mutations
# ---------------------------------------------------------------------------


class TestCampaignEngineMutationIntegration:
    """Test that campaign engine properly uses mutations when enabled."""

    def test_campaign_config_has_mutation_fields(self):
        from novatrade.optimization.campaign_engine import CampaignConfig

        cfg = CampaignConfig()
        assert hasattr(cfg, "enable_mutations")
        assert hasattr(cfg, "search_level_config")
        assert hasattr(cfg, "mutation_budget")
        assert hasattr(cfg, "doctrine")
        assert cfg.enable_mutations is False

    def test_campaign_config_with_mutations_enabled(self):
        from novatrade.optimization.campaign_engine import CampaignConfig
        from novatrade.optimization.mutations import MutationBudget
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        cfg = CampaignConfig(
            enable_mutations=True,
            search_level_config=SearchLevelConfig(level=SearchLevel.LEVEL_2),
            mutation_budget=MutationBudget(),
        )
        assert cfg.enable_mutations is True
        assert cfg.search_level_config.level == SearchLevel.LEVEL_2

    @patch("novatrade.optimization.campaign_engine.execute_backtest")
    def test_campaign_with_mutations_runs(self, mock_bt):
        """Campaign with mutations enabled should call generate_mutated_candidate."""
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.campaign_engine import CampaignConfig, run_campaign
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        # Mock backtest result
        mock_result = MagicMock()
        mock_result.experiment_id = "exp-001"
        mock_result.scout_score = 0.5
        mock_result.status = "ranked"
        mock_bt.return_value = mock_result

        base_config = StrategyConfig()
        h1 = [MagicMock() for _ in range(100)]
        h4 = [MagicMock() for _ in range(100)]

        cfg = CampaignConfig(
            max_experiments=3,
            stagnation_limit=100,
            enable_mutations=True,
            search_level_config=SearchLevelConfig(level=SearchLevel.LEVEL_2),
            walkforward_on_new_best=False,
        )

        result = run_campaign(base_config, h1, h4, campaign_config=cfg)
        assert result.total_experiments == 3
        assert mock_bt.call_count == 3

    @patch("novatrade.optimization.campaign_engine.execute_backtest")
    def test_campaign_without_mutations_uses_strategy(self, mock_bt):
        """Campaign without mutations should use the legacy strategy selector."""
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.campaign_engine import CampaignConfig, run_campaign

        mock_result = MagicMock()
        mock_result.experiment_id = "exp-001"
        mock_result.scout_score = 0.5
        mock_result.status = "ranked"
        mock_bt.return_value = mock_result

        base_config = StrategyConfig()
        h1 = [MagicMock() for _ in range(100)]
        h4 = [MagicMock() for _ in range(100)]

        cfg = CampaignConfig(
            max_experiments=3,
            stagnation_limit=100,
            enable_mutations=False,
            walkforward_on_new_best=False,
        )

        result = run_campaign(base_config, h1, h4, campaign_config=cfg)
        assert result.total_experiments == 3

    @patch("novatrade.optimization.campaign_engine.execute_backtest")
    def test_campaign_saves_ancestry_json(self, mock_bt):
        """When campaign_dir is set and mutations enabled, ancestry.json should be saved."""
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.campaign_engine import CampaignConfig, run_campaign
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        mock_result = MagicMock()
        mock_result.experiment_id = "exp-001"
        mock_result.scout_score = 0.5
        mock_result.status = "ranked"
        mock_bt.return_value = mock_result

        base_config = StrategyConfig()
        h1 = [MagicMock() for _ in range(100)]
        h4 = [MagicMock() for _ in range(100)]

        with tempfile.TemporaryDirectory() as tmpdir:
            campaign_dir = Path(tmpdir)
            cfg = CampaignConfig(
                max_experiments=5,
                stagnation_limit=100,
                enable_mutations=True,
                search_level_config=SearchLevelConfig(level=SearchLevel.LEVEL_2),
                walkforward_on_new_best=False,
            )

            run_campaign(base_config, h1, h4, campaign_config=cfg, campaign_dir=campaign_dir)

            ancestry_path = campaign_dir / "ancestry.json"
            assert ancestry_path.exists()
            data = json.loads(ancestry_path.read_text())
            assert len(data) == 5  # one record per experiment

    @patch("novatrade.optimization.campaign_engine.execute_backtest")
    def test_campaign_improvements_include_mutation_type(self, mock_bt):
        """Improvements list should include mutation_type when mutations are enabled."""
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.campaign_engine import CampaignConfig, run_campaign
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        # First call returns low score, second returns high score
        call_count = [0]

        def make_result(*args, **kwargs):
            call_count[0] += 1
            mock = MagicMock()
            mock.experiment_id = f"exp-{call_count[0]:03d}"
            mock.scout_score = 0.1 * call_count[0]
            mock.status = "ranked"
            return mock

        mock_bt.side_effect = make_result

        base_config = StrategyConfig()
        h1 = [MagicMock() for _ in range(100)]
        h4 = [MagicMock() for _ in range(100)]

        cfg = CampaignConfig(
            max_experiments=3,
            stagnation_limit=100,
            enable_mutations=True,
            search_level_config=SearchLevelConfig(level=SearchLevel.LEVEL_2),
            walkforward_on_new_best=False,
        )

        result = run_campaign(base_config, h1, h4, campaign_config=cfg)
        assert len(result.improvements) >= 1
        for imp in result.improvements:
            assert "mutation_type" in imp


# ---------------------------------------------------------------------------
# STRUCTURAL_TEMPLATES constant
# ---------------------------------------------------------------------------


class TestStructuralTemplatesConstant:
    """Verify STRUCTURAL_TEMPLATES is populated from registered compositions."""

    def test_templates_non_empty(self):
        from novatrade.optimization.mutations import STRUCTURAL_TEMPLATES

        assert len(STRUCTURAL_TEMPLATES) >= 4

    def test_templates_match_registered(self):
        from novatrade.optimization.mutations import STRUCTURAL_TEMPLATES
        from novatrade.optimization.search_levels import REGISTERED_COMPOSITIONS

        assert set(STRUCTURAL_TEMPLATES) == set(REGISTERED_COMPOSITIONS.keys())
