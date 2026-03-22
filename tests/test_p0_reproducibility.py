"""Tests for P0 — Reproducibility & Evaluation Integrity.

Covers:
  - Sacred registry: hash collection, bundle immutability, drift detection
  - Reproducibility manifest: creation, serialization, comparison
  - Campaign budget: limits, stagnation, checkpointing
  - Metrics versioning
  - Experiment identity v2
"""

from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# Metrics versioning
# ---------------------------------------------------------------------------


class TestMetricsVersioning:
    def test_version_constant_exists(self):
        from novatrade.backtest.metrics import VERSION

        assert VERSION == "1.2.0"

    def test_version_hash_is_deterministic(self):
        from novatrade.backtest.metrics import metrics_version_hash

        h1 = metrics_version_hash()
        h2 = metrics_version_hash()
        assert h1 == h2
        assert len(h1) == 16

    def test_version_hash_is_hex(self):
        from novatrade.backtest.metrics import metrics_version_hash

        h = metrics_version_hash()
        int(h, 16)  # raises ValueError if not hex


# ---------------------------------------------------------------------------
# Sacred registry
# ---------------------------------------------------------------------------


class TestSacredRegistry:
    def test_collect_defaults_returns_bundle(self):
        from novatrade.evaluation.sacred_registry import SacredBundle, collect_sacred_hashes

        bundle = collect_sacred_hashes()
        assert isinstance(bundle, SacredBundle)
        assert len(bundle.combined_hash) == 16
        assert len(bundle.fill_model_hash) == 16
        assert len(bundle.spread_model_hash) == 16
        assert len(bundle.slippage_model_hash) == 16
        assert len(bundle.commission_model_hash) == 16
        assert len(bundle.session_calendar_hash) == 16
        assert len(bundle.intrabar_policy_hash) == 16
        assert len(bundle.metrics_hash) == 16

    def test_collect_is_deterministic(self):
        from novatrade.evaluation.sacred_registry import collect_sacred_hashes

        b1 = collect_sacred_hashes()
        b2 = collect_sacred_hashes()
        assert b1.combined_hash == b2.combined_hash
        assert b1 == b2

    def test_bundle_is_frozen(self):
        from novatrade.evaluation.sacred_registry import collect_sacred_hashes

        bundle = collect_sacred_hashes()
        with pytest.raises(AttributeError):
            bundle.combined_hash = "tampered"

    def test_to_dict(self):
        from novatrade.evaluation.sacred_registry import collect_sacred_hashes

        bundle = collect_sacred_hashes()
        d = bundle.to_dict()
        assert isinstance(d, dict)
        assert "combined_hash" in d
        assert "fill_model_hash" in d
        assert len(d) == 8  # 7 individual + combined

    def test_verify_unchanged_with_defaults(self):
        from novatrade.evaluation.sacred_registry import collect_sacred_hashes, verify_sacred_unchanged

        bundle = collect_sacred_hashes()
        diffs = verify_sacred_unchanged(bundle)
        assert diffs == []

    def test_verify_detects_drift(self):
        from novatrade.evaluation.sacred_registry import SacredBundle, verify_sacred_unchanged

        # Create a bundle with a tampered hash
        tampered = SacredBundle(
            fill_model_hash="aaaa" * 4,
            spread_model_hash="bbbb" * 4,
            slippage_model_hash="cccc" * 4,
            commission_model_hash="dddd" * 4,
            session_calendar_hash="eeee" * 4,
            intrabar_policy_hash="ffff" * 4,
            metrics_hash="0000" * 4,
            combined_hash="1111" * 4,
        )
        diffs = verify_sacred_unchanged(tampered)
        assert len(diffs) == 7  # all 7 should differ

    def test_custom_models_produce_different_hashes(self):
        from novatrade.evaluation.fill_model import FillModelConfig
        from novatrade.evaluation.sacred_registry import collect_sacred_hashes

        default = collect_sacred_hashes()
        custom_fill = FillModelConfig(stop_first_on_ambiguity=False)
        custom = collect_sacred_hashes(fill_model=custom_fill)
        assert custom.fill_model_hash != default.fill_model_hash
        assert custom.combined_hash != default.combined_hash
        # Other hashes should stay the same
        assert custom.spread_model_hash == default.spread_model_hash


# ---------------------------------------------------------------------------
# Reproducibility manifest
# ---------------------------------------------------------------------------


class TestReproducibilityManifest:
    @pytest.fixture
    def sample_config(self):
        return {
            "irb_threshold": 0.45,
            "ema_period": 20,
            "atr_period": 14,
        }

    def test_create_manifest(self, sample_config):
        from novatrade.evaluation.reproducibility_manifest import create_manifest

        m = create_manifest(
            config_dict=sample_config,
            strategy_name="IRB_v1",
            strategy_version="1.0.0",
            dataset_hash="abcdef1234567890",
            dataset_symbol="EURUSD",
            dataset_date_range="2020-01-01..2025-12-31",
            doctrine_hash="doctrine_hash_123",
        )
        assert len(m.manifest_hash) == 16
        assert m.strategy_name == "IRB_v1"
        assert m.dataset_symbol == "EURUSD"
        assert isinstance(m.sacred_bundle, dict)
        assert "combined_hash" in m.sacred_bundle

    def test_manifest_is_frozen(self, sample_config):
        from novatrade.evaluation.reproducibility_manifest import create_manifest

        m = create_manifest(
            config_dict=sample_config,
            strategy_name="test",
            strategy_version="1.0",
            dataset_hash="abc",
            dataset_symbol="EURUSD",
            dataset_date_range="2020..2025",
            doctrine_hash="doc",
        )
        with pytest.raises(AttributeError):
            m.manifest_hash = "tampered"

    def test_manifest_deterministic(self, sample_config):
        from novatrade.evaluation.reproducibility_manifest import create_manifest

        m1 = create_manifest(
            config_dict=sample_config,
            strategy_name="test",
            strategy_version="1.0",
            dataset_hash="abc",
            dataset_symbol="EURUSD",
            dataset_date_range="2020..2025",
            doctrine_hash="doc",
        )
        m2 = create_manifest(
            config_dict=sample_config,
            strategy_name="test",
            strategy_version="1.0",
            dataset_hash="abc",
            dataset_symbol="EURUSD",
            dataset_date_range="2020..2025",
            doctrine_hash="doc",
        )
        assert m1.manifest_hash == m2.manifest_hash

    def test_different_config_different_hash(self, sample_config):
        from novatrade.evaluation.reproducibility_manifest import create_manifest

        m1 = create_manifest(
            config_dict=sample_config,
            strategy_name="test",
            strategy_version="1.0",
            dataset_hash="abc",
            dataset_symbol="EURUSD",
            dataset_date_range="2020..2025",
            doctrine_hash="doc",
        )
        altered = {**sample_config, "irb_threshold": 0.50}
        m2 = create_manifest(
            config_dict=altered,
            strategy_name="test",
            strategy_version="1.0",
            dataset_hash="abc",
            dataset_symbol="EURUSD",
            dataset_date_range="2020..2025",
            doctrine_hash="doc",
        )
        assert m1.manifest_hash != m2.manifest_hash

    def test_save_and_load_manifest(self, sample_config, tmp_path):
        from novatrade.evaluation.reproducibility_manifest import (
            create_manifest,
            load_manifest,
            save_manifest,
        )

        m = create_manifest(
            config_dict=sample_config,
            strategy_name="test",
            strategy_version="1.0",
            dataset_hash="abc",
            dataset_symbol="EURUSD",
            dataset_date_range="2020..2025",
            doctrine_hash="doc",
        )
        path = save_manifest(m, tmp_path)
        assert path.exists()
        loaded = load_manifest(path)
        assert loaded.manifest_hash == m.manifest_hash
        assert loaded.strategy_name == m.strategy_name

    def test_compare_identical_manifests(self, sample_config):
        from novatrade.evaluation.reproducibility_manifest import (
            compare_manifests,
            create_manifest,
        )

        m1 = create_manifest(
            config_dict=sample_config,
            strategy_name="test",
            strategy_version="1.0",
            dataset_hash="abc",
            dataset_symbol="EURUSD",
            dataset_date_range="2020..2025",
            doctrine_hash="doc",
        )
        m2 = create_manifest(
            config_dict=sample_config,
            strategy_name="test",
            strategy_version="1.0",
            dataset_hash="abc",
            dataset_symbol="EURUSD",
            dataset_date_range="2020..2025",
            doctrine_hash="doc",
        )
        diffs = compare_manifests(m1, m2)
        assert diffs == {}

    def test_compare_different_manifests(self, sample_config):
        from novatrade.evaluation.reproducibility_manifest import (
            compare_manifests,
            create_manifest,
        )

        m1 = create_manifest(
            config_dict=sample_config,
            strategy_name="test",
            strategy_version="1.0",
            dataset_hash="abc",
            dataset_symbol="EURUSD",
            dataset_date_range="2020..2025",
            doctrine_hash="doc",
        )
        m2 = create_manifest(
            config_dict=sample_config,
            strategy_name="test",
            strategy_version="1.0",
            dataset_hash="xyz_different",
            dataset_symbol="EURUSD",
            dataset_date_range="2020..2025",
            doctrine_hash="doc",
        )
        diffs = compare_manifests(m1, m2)
        assert "dataset_hash" in diffs
        assert "manifest_hash" in diffs

    def test_to_dict(self, sample_config):
        from novatrade.evaluation.reproducibility_manifest import create_manifest

        m = create_manifest(
            config_dict=sample_config,
            strategy_name="test",
            strategy_version="1.0",
            dataset_hash="abc",
            dataset_symbol="EURUSD",
            dataset_date_range="2020..2025",
            doctrine_hash="doc",
        )
        d = m.to_dict()
        assert isinstance(d, dict)
        assert "manifest_hash" in d
        assert "sacred_bundle" in d
        # Ensure JSON-serializable
        json.dumps(d)


# ---------------------------------------------------------------------------
# Campaign budget
# ---------------------------------------------------------------------------


class TestCampaignBudget:
    def test_default_budget(self):
        from novatrade.optimization.campaign_budget import CampaignBudget

        b = CampaignBudget()
        assert b.max_experiments == 200
        assert b.max_wall_clock_seconds == 6 * 3600
        assert b.max_crashes == 20
        assert b.max_stagnant_experiments == 10
        assert b.checkpoint_interval == 25

    def test_create_campaign(self):
        from novatrade.optimization.campaign_budget import (
            CampaignState,
            StopReason,
            create_campaign,
        )

        state = create_campaign("test-campaign-001")
        assert isinstance(state, CampaignState)
        assert state.campaign_id == "test-campaign-001"
        assert state.total_experiments == 0
        assert state.should_stop() == StopReason.RUNNING

    def test_experiment_limit(self):
        from novatrade.optimization.campaign_budget import (
            CampaignBudget,
            StopReason,
            create_campaign,
        )

        budget = CampaignBudget(max_experiments=3)
        state = create_campaign("test", budget)

        state.record_experiment("e1", 0.5)
        state.record_experiment("e2", 0.6)
        assert state.should_stop() == StopReason.RUNNING

        state.record_experiment("e3", 0.7)
        assert state.should_stop() == StopReason.EXPERIMENT_LIMIT

    def test_crash_limit(self):
        from novatrade.optimization.campaign_budget import (
            CampaignBudget,
            StopReason,
            create_campaign,
        )

        budget = CampaignBudget(max_crashes=2, max_experiments=100)
        state = create_campaign("test", budget)

        state.record_experiment("e1", None, crashed=True)
        assert state.should_stop() == StopReason.RUNNING

        state.record_experiment("e2", None, crashed=True)
        assert state.should_stop() == StopReason.CRASH_LIMIT

    def test_stagnation_detection(self):
        from novatrade.optimization.campaign_budget import (
            CampaignBudget,
            StopReason,
            create_campaign,
        )

        budget = CampaignBudget(max_stagnant_experiments=3, max_experiments=100)
        state = create_campaign("test", budget)

        # First experiment sets the baseline
        state.record_experiment("e1", 0.5)
        assert state.experiments_since_improvement == 0

        # Three more without improvement
        state.record_experiment("e2", 0.4)
        state.record_experiment("e3", 0.3)
        assert state.should_stop() == StopReason.RUNNING

        state.record_experiment("e4", 0.2)
        assert state.should_stop() == StopReason.STAGNATION

    def test_improvement_resets_stagnation(self):
        from novatrade.optimization.campaign_budget import (
            CampaignBudget,
            StopReason,
            create_campaign,
        )

        budget = CampaignBudget(max_stagnant_experiments=3, max_experiments=100)
        state = create_campaign("test", budget)

        state.record_experiment("e1", 0.5)
        state.record_experiment("e2", 0.4)  # no improvement
        state.record_experiment("e3", 0.3)  # no improvement
        assert state.experiments_since_improvement == 2

        state.record_experiment("e4", 0.6)  # improvement!
        assert state.experiments_since_improvement == 0
        assert state.best_score == 0.6
        assert state.best_experiment_id == "e4"
        assert state.should_stop() == StopReason.RUNNING

    def test_wall_clock_limit(self):
        from novatrade.optimization.campaign_budget import (
            CampaignBudget,
            StopReason,
            create_campaign,
        )

        budget = CampaignBudget(max_wall_clock_seconds=0.0)  # immediate timeout
        state = create_campaign("test", budget)
        assert state.should_stop() == StopReason.WALL_CLOCK_LIMIT

    def test_checkpoint_tracking(self):
        from novatrade.optimization.campaign_budget import (
            CampaignBudget,
            create_campaign,
        )

        budget = CampaignBudget(checkpoint_interval=3, max_experiments=100)
        state = create_campaign("test", budget)

        for i in range(2):
            state.record_experiment(f"e{i}", 0.5)
            assert not state.needs_checkpoint()

        state.record_experiment("e2", 0.5)
        assert state.needs_checkpoint()

        state.mark_checkpoint()
        assert not state.needs_checkpoint()
        assert state.checkpoints_created == 1

    def test_summary(self):
        from novatrade.optimization.campaign_budget import create_campaign

        state = create_campaign("test-001")
        state.record_experiment("e1", 0.7)
        summary = state.summary()

        assert summary["campaign_id"] == "test-001"
        assert summary["total_experiments"] == 1
        assert summary["successful"] == 1
        assert summary["crashed"] == 0
        assert summary["best_score"] == 0.7
        assert summary["best_experiment_id"] == "e1"
        assert isinstance(summary["elapsed_seconds"], float)

    def test_remaining_counters(self):
        from novatrade.optimization.campaign_budget import (
            CampaignBudget,
            create_campaign,
        )

        budget = CampaignBudget(max_experiments=10)
        state = create_campaign("test", budget)

        state.record_experiment("e1", 0.5)
        assert state.remaining_experiments == 9

    def test_budget_to_dict(self):
        from novatrade.optimization.campaign_budget import CampaignBudget

        b = CampaignBudget()
        d = b.to_dict()
        assert d["max_experiments"] == 200
        assert d["max_wall_clock_seconds"] == 6 * 3600

    def test_custom_budget(self):
        from novatrade.optimization.campaign_budget import (
            CampaignBudget,
            StopReason,
            create_campaign,
        )

        budget = CampaignBudget(
            max_experiments=50,
            max_wall_clock_seconds=3600,
            max_crashes=5,
            max_stagnant_experiments=15,
            checkpoint_interval=10,
        )
        state = create_campaign("custom", budget)
        assert state.budget.max_experiments == 50
        assert state.should_stop() == StopReason.RUNNING


# ---------------------------------------------------------------------------
# Experiment identity v2
# ---------------------------------------------------------------------------


class TestExperimentIdentityV2:
    def test_v2_deterministic(self):
        from novatrade.storage.experiment_identity import compute_experiment_id_v2

        id1 = compute_experiment_id_v2("cfg", "data", "sacred", "engine", "doctrine")
        id2 = compute_experiment_id_v2("cfg", "data", "sacred", "engine", "doctrine")
        assert id1 == id2
        assert len(id1) == 16

    def test_v2_differs_from_v1(self):
        from novatrade.storage.experiment_identity import (
            compute_experiment_id,
            compute_experiment_id_v2,
        )

        # v1 and v2 with same inputs should produce different IDs because
        # v2 uses sacred_combined_hash which covers more models than fill_model_hash
        # (unless sacred_combined_hash == fill_model_hash, which it won't normally)
        v1 = compute_experiment_id("cfg", "data", "fill_only", "engine", "doctrine")
        v2 = compute_experiment_id_v2("cfg", "data", "all_sacred", "engine", "doctrine")
        assert v1 != v2

    def test_v2_different_sacred_different_id(self):
        from novatrade.storage.experiment_identity import compute_experiment_id_v2

        id1 = compute_experiment_id_v2("cfg", "data", "sacred_v1", "engine", "doctrine")
        id2 = compute_experiment_id_v2("cfg", "data", "sacred_v2", "engine", "doctrine")
        assert id1 != id2

    def test_v1_backward_compat(self):
        """Ensure v1 still works unchanged."""
        from novatrade.storage.experiment_identity import compute_experiment_id

        eid = compute_experiment_id("a", "b", "c", "d", "e")
        assert len(eid) == 16
        assert eid == compute_experiment_id("a", "b", "c", "d", "e")


# ---------------------------------------------------------------------------
# Integration: end-to-end reproducibility chain
# ---------------------------------------------------------------------------


class TestReproducibilityChain:
    """Verify the full chain: sacred_registry -> manifest -> experiment_id_v2."""

    def test_full_chain(self):
        from novatrade.evaluation.reproducibility_manifest import create_manifest
        from novatrade.evaluation.sacred_registry import collect_sacred_hashes
        from novatrade.storage.experiment_identity import compute_experiment_id_v2

        # Collect all sacred hashes
        bundle = collect_sacred_hashes()

        # Create manifest
        config = {"irb_threshold": 0.45, "ema_period": 20}
        manifest = create_manifest(
            config_dict=config,
            strategy_name="IRB",
            strategy_version="1.0",
            dataset_hash="dataset_abc",
            dataset_symbol="EURUSD",
            dataset_date_range="2020..2025",
            doctrine_hash="doctrine_xyz",
            sacred_bundle=bundle,
        )

        # Compute v2 experiment ID using sacred combined hash
        eid = compute_experiment_id_v2(
            config_hash=manifest.strategy_config_hash,
            dataset_hash=manifest.dataset_hash,
            sacred_combined_hash=bundle.combined_hash,
            engine_sha=manifest.engine_sha,
            doctrine_hash=manifest.doctrine_hash,
        )

        # Verify determinism
        assert len(eid) == 16
        assert len(manifest.manifest_hash) == 16

        # Re-run should produce same IDs
        bundle2 = collect_sacred_hashes()
        assert bundle2.combined_hash == bundle.combined_hash

    def test_drift_detection(self):
        """Modifying a sacred model should be detectable."""
        from novatrade.evaluation.fill_model import FillModelConfig
        from novatrade.evaluation.sacred_registry import collect_sacred_hashes, verify_sacred_unchanged

        # Snapshot defaults
        baseline = collect_sacred_hashes()

        # Modify fill model
        modified_fill = FillModelConfig(stop_first_on_ambiguity=False)
        modified = collect_sacred_hashes(fill_model=modified_fill)

        # Combined hash changed
        assert modified.combined_hash != baseline.combined_hash

        # verify_sacred_unchanged detects the drift vs current defaults
        diffs = verify_sacred_unchanged(modified)
        assert any("fill_model" in d for d in diffs)


# ---------------------------------------------------------------------------
# Campaign budget edge cases
# ---------------------------------------------------------------------------


class TestCampaignBudgetEdgeCases:
    def test_all_crashed_experiments(self):
        from novatrade.optimization.campaign_budget import (
            CampaignBudget,
            StopReason,
            create_campaign,
        )

        budget = CampaignBudget(max_crashes=3, max_experiments=100)
        state = create_campaign("test", budget)

        for i in range(3):
            state.record_experiment(f"e{i}", None, crashed=True)

        assert state.should_stop() == StopReason.CRASH_LIMIT
        assert state.successful_experiments == 0
        assert state.crashed_experiments == 3

    def test_mixed_experiments(self):
        from novatrade.optimization.campaign_budget import (
            CampaignBudget,
            StopReason,
            create_campaign,
        )

        budget = CampaignBudget(max_experiments=10, max_crashes=5)
        state = create_campaign("test", budget)

        state.record_experiment("e1", 0.5)
        state.record_experiment("e2", None, crashed=True)
        state.record_experiment("e3", 0.7)
        state.record_experiment("e4", None, crashed=True)

        assert state.total_experiments == 4
        assert state.successful_experiments == 2
        assert state.crashed_experiments == 2
        assert state.best_score == 0.7
        assert state.should_stop() == StopReason.RUNNING

    def test_zero_budget_experiments(self):
        from novatrade.optimization.campaign_budget import (
            CampaignBudget,
            StopReason,
            create_campaign,
        )

        budget = CampaignBudget(max_experiments=0)
        state = create_campaign("test", budget)
        assert state.should_stop() == StopReason.EXPERIMENT_LIMIT

    def test_best_score_starts_negative(self):
        from novatrade.optimization.campaign_budget import create_campaign

        state = create_campaign("test")
        assert state.best_score == -1.0
        assert state.best_experiment_id == ""

        # First positive score always counts as improvement
        state.record_experiment("e1", 0.0)
        assert state.best_score == 0.0
        assert state.experiments_since_improvement == 0
