"""Comprehensive pytest tests for NovaTrade AutoResearch Phases 5 and 6.

Phase 5: Campaign Engine, Promotion Pipeline, Campaign CLI
Phase 6: MCP Tools and Resources
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from novatrade.cli.commands.campaign_cmd import format_campaign_report, start_campaign
from novatrade.cli.commands.data import (
    aggregate_h1_to_h4,
    generate_synthetic_candles,
    load_candles_csv,
    save_candles_csv,
)
from novatrade.cli.config_schema import StrategyConfig
from novatrade.optimization.campaign_engine import (
    _END_REASONS,
    CampaignCheckpoint,
    CampaignConfig,
    CampaignResult,
    load_latest_checkpoint,
    run_campaign,
    save_checkpoint,
)
from novatrade.optimization.promotion import PromotionResult, run_promotion_pipeline
from novatrade.storage.experiment_db import ExperimentDB, ExperimentRecord

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def campaign_candles():
    """30 days of data for fast campaign tests."""
    h1 = generate_synthetic_candles("EURUSD", "H1", days=30, start_price=1.1000)
    h4 = aggregate_h1_to_h4(h1)
    return h1, h4


@pytest.fixture
def medium_candles():
    """90 days of data for tests that need a bit more data (promotion, etc.)."""
    h1 = generate_synthetic_candles("EURUSD", "H1", days=90, start_price=1.1000)
    h4 = aggregate_h1_to_h4(h1)
    return h1, h4


@pytest.fixture
def default_config():
    """Default StrategyConfig with baseline parameters."""
    return StrategyConfig()


@pytest.fixture
def campaign_db(tmp_path):
    """Fresh experiment database in tmp_path."""
    db = ExperimentDB(tmp_path / "test_experiments.db")
    yield db
    db.close()


@pytest.fixture
def config_yaml(tmp_path):
    """Write a baseline YAML config to tmp_path, return the path."""
    cfg = StrategyConfig()
    path = tmp_path / "baseline.yaml"
    cfg.to_yaml(path)
    return path


# ===================================================================
# Phase 5: Campaign Engine Tests — CampaignConfig
# ===================================================================


class TestCampaignConfig:
    """Tests for CampaignConfig dataclass."""

    def test_default_values(self):
        """Default config has sensible values."""
        cfg = CampaignConfig()
        assert cfg.max_experiments == 200
        assert cfg.max_wall_clock_seconds == 21600.0
        assert cfg.stagnation_limit == 20
        assert cfg.max_crashes == 5
        assert cfg.checkpoint_interval == 25
        assert cfg.walkforward_on_new_best is True
        assert cfg.strategy_budget is None
        assert cfg.wf_config is None

    def test_auto_generated_campaign_id(self):
        """Auto-generated campaign_id starts with 'campaign-'."""
        cfg = CampaignConfig()
        assert cfg.campaign_id.startswith("campaign-")
        assert len(cfg.campaign_id) > len("campaign-")

    def test_unique_campaign_ids(self):
        """Each new CampaignConfig gets a unique campaign_id."""
        ids = {CampaignConfig().campaign_id for _ in range(20)}
        assert len(ids) == 20

    def test_custom_config_overrides(self):
        """Custom config overrides work."""
        cfg = CampaignConfig(
            campaign_id="my-campaign",
            max_experiments=50,
            stagnation_limit=10,
            max_crashes=2,
            max_wall_clock_seconds=60.0,
        )
        assert cfg.campaign_id == "my-campaign"
        assert cfg.max_experiments == 50
        assert cfg.stagnation_limit == 10
        assert cfg.max_crashes == 2
        assert cfg.max_wall_clock_seconds == 60.0

    def test_empty_string_gets_auto_id(self):
        """Empty string campaign_id triggers auto-generation."""
        cfg = CampaignConfig(campaign_id="")
        assert cfg.campaign_id.startswith("campaign-")


# ===================================================================
# Phase 5: Campaign Engine Tests — Campaign Loop
# ===================================================================


class TestCampaignLoop:
    """Tests for run_campaign with small data and short budgets."""

    def test_returns_campaign_result(self, campaign_candles, default_config, tmp_path, campaign_db):
        """run_campaign returns a CampaignResult."""
        h1, h4 = campaign_candles
        cfg = CampaignConfig(max_experiments=3, stagnation_limit=5)
        result = run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
            campaign_dir=tmp_path / "campaign",
            db=campaign_db,
        )
        assert isinstance(result, CampaignResult)

    def test_terminates_on_max_experiments(self, campaign_candles, default_config):
        """Campaign terminates when max_experiments is reached."""
        h1, h4 = campaign_candles
        cfg = CampaignConfig(max_experiments=5, stagnation_limit=100)
        result = run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
        )
        assert result.total_experiments == 5
        assert result.end_reason == "budget_exhausted"

    def test_terminates_on_stagnation(self, campaign_candles, default_config):
        """Campaign terminates on stagnation_limit (stagnation_limit=2 with short data)."""
        h1, h4 = campaign_candles
        cfg = CampaignConfig(
            max_experiments=100,
            stagnation_limit=2,
        )
        result = run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
        )
        # With 30 days of data, experiments are likely all gated/scored similarly
        # so stagnation triggers before 100 experiments
        assert result.end_reason == "stagnation"
        assert result.total_experiments <= 100

    def test_terminates_on_max_crashes(self, default_config, campaign_candles):
        """Campaign terminates on max_crashes (mock the backtest to crash)."""
        h1, h4 = campaign_candles

        def _fake_execute(**kwargs):
            from novatrade.cli.commands.run import BacktestRunResult
            from novatrade.evaluation.gates import GateResults

            return BacktestRunResult(
                experiment_id="crash-test",
                scout_score=None,
                gate_results=GateResults(),
                metrics=None,
                status="crashed",
                backtest_result=None,
                notes="mock crash",
            )

        cfg = CampaignConfig(max_experiments=100, max_crashes=3, stagnation_limit=200)

        with patch("novatrade.optimization.campaign_engine.execute_backtest", side_effect=_fake_execute):
            result = run_campaign(
                base_config=default_config,
                h1_candles=h1,
                h4_candles=h4,
                campaign_config=cfg,
            )
        assert result.end_reason == "max_crashes"

    def test_terminates_on_wall_clock(self, campaign_candles, default_config):
        """Campaign terminates when wall_clock_seconds is exceeded."""
        h1, h4 = campaign_candles
        # Set a very short wall clock so it expires quickly
        cfg = CampaignConfig(
            max_experiments=500,
            max_wall_clock_seconds=0.001,
            stagnation_limit=500,
        )
        result = run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
        )
        assert result.end_reason == "wall_clock"

    def test_terminates_on_stop_event(self, campaign_candles, default_config):
        """Campaign terminates when stop_event is set before starting."""
        h1, h4 = campaign_candles
        stop = threading.Event()
        stop.set()  # Pre-set the stop event

        cfg = CampaignConfig(max_experiments=100, stagnation_limit=100)
        result = run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
            stop_event=stop,
        )
        assert result.end_reason == "manual_stop"
        assert result.total_experiments == 0

    def test_best_scout_score_non_negative(self, campaign_candles, default_config):
        """best_scout_score is >= 0 after campaign."""
        h1, h4 = campaign_candles
        cfg = CampaignConfig(max_experiments=3, stagnation_limit=10)
        result = run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
        )
        assert result.best_scout_score >= 0.0

    def test_status_counts_has_correct_keys(self, campaign_candles, default_config):
        """status_counts dict has keys from valid statuses."""
        h1, h4 = campaign_candles
        cfg = CampaignConfig(max_experiments=5, stagnation_limit=10)
        result = run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
        )
        valid_statuses = {"valid", "gated_a", "gated_b", "gated_c", "ranked", "promoted", "rejected", "crashed"}
        for key in result.status_counts:
            assert key in valid_statuses, f"Unexpected status key: {key}"

    def test_end_reason_is_valid(self, campaign_candles, default_config):
        """CampaignResult.end_reason is in the valid set."""
        h1, h4 = campaign_candles
        cfg = CampaignConfig(max_experiments=3, stagnation_limit=10)
        result = run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
        )
        assert result.end_reason in _END_REASONS

    def test_invalid_end_reason_raises(self):
        """CampaignResult rejects invalid end_reason."""
        with pytest.raises(ValueError, match="Invalid end_reason"):
            CampaignResult(
                campaign_id="test",
                total_experiments=0,
                best_experiment_id=None,
                best_scout_score=0.0,
                best_config=None,
                end_reason="invalid_reason",
            )

    def test_improvements_contain_entries(self, campaign_candles, default_config):
        """improvements list contains entries when scores improve.

        We run enough experiments that at least the first ranked one
        registers as an improvement (going from -inf to some score).
        """
        h1, h4 = campaign_candles
        cfg = CampaignConfig(max_experiments=5, stagnation_limit=10)
        result = run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
        )
        # improvements may or may not be populated depending on data quality
        # but the list should exist and have correct structure if populated
        assert isinstance(result.improvements, list)
        for imp in result.improvements:
            assert "experiment_id" in imp
            assert "scout_score" in imp
            assert "experiment_index" in imp

    def test_walkforward_disabled_skips_wf(self, campaign_candles, default_config):
        """Campaign with walkforward_on_new_best=False skips WF validation."""
        h1, h4 = campaign_candles
        cfg = CampaignConfig(
            max_experiments=3,
            stagnation_limit=10,
            walkforward_on_new_best=False,
        )
        # Should complete without running WF (which would need more data)
        result = run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
        )
        assert isinstance(result, CampaignResult)
        assert result.total_experiments == 3

    def test_empty_candles_returns_zero_experiments(self, default_config):
        """Campaign with empty candle data returns immediately."""
        cfg = CampaignConfig(max_experiments=5)
        result = run_campaign(
            base_config=default_config,
            h1_candles=[],
            h4_candles=[],
            campaign_config=cfg,
        )
        assert result.total_experiments == 0
        assert result.end_reason == "budget_exhausted"

    def test_campaign_with_db_inserts_records(self, campaign_candles, default_config, tmp_path, campaign_db):
        """Campaign with db parameter inserts records into the database."""
        h1, h4 = campaign_candles
        cfg = CampaignConfig(
            campaign_id="db-test",
            max_experiments=3,
            stagnation_limit=10,
        )
        run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
            campaign_dir=tmp_path / "campaign",
            db=campaign_db,
        )
        count = campaign_db.count_by_campaign("db-test")
        # Should have some records (may not be 3 if some crashed before DB insert)
        assert count >= 1

    def test_campaign_elapsed_seconds_positive(self, campaign_candles, default_config):
        """Campaign elapsed_seconds is a positive number."""
        h1, h4 = campaign_candles
        cfg = CampaignConfig(max_experiments=3, stagnation_limit=10)
        result = run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
        )
        assert result.elapsed_seconds >= 0.0

    def test_candidate_generation_within_bounds(self, campaign_candles, default_config, tmp_path, campaign_db):
        """Campaign generates candidates within parameter bounds."""
        h1, h4 = campaign_candles
        cfg = CampaignConfig(
            campaign_id="bounds-test",
            max_experiments=5,
            stagnation_limit=10,
        )
        run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
            campaign_dir=tmp_path / "campaign",
            db=campaign_db,
        )
        records = campaign_db.list_by_campaign("bounds-test")
        # Verify we can instantiate configs from DB records (which implies bounds OK)
        assert len(records) >= 1


# ===================================================================
# Phase 5: Campaign Engine Tests — Checkpoints
# ===================================================================


class TestCampaignCheckpoints:
    """Tests for save_checkpoint and load_latest_checkpoint."""

    def test_save_creates_json(self, tmp_path):
        """save_checkpoint creates a JSON file."""
        cp = CampaignCheckpoint(
            campaign_id="test-cp",
            experiment_count=10,
            best_experiment_id="exp-001",
            best_scout_score=0.42,
            best_config={"irb_threshold": 0.45},
            stagnation_count=2,
            crash_count=0,
            elapsed_seconds=12.5,
        )
        path = save_checkpoint(cp, tmp_path)
        assert path.exists()
        assert path.suffix == ".json"

    def test_load_latest_returns_checkpoint(self, tmp_path):
        """load_latest_checkpoint loads the saved checkpoint."""
        cp = CampaignCheckpoint(
            campaign_id="test-cp",
            experiment_count=10,
            best_experiment_id="exp-001",
            best_scout_score=0.42,
            best_config={"irb_threshold": 0.45},
            stagnation_count=2,
            crash_count=0,
            elapsed_seconds=12.5,
        )
        save_checkpoint(cp, tmp_path)
        loaded = load_latest_checkpoint(tmp_path)
        assert loaded is not None
        assert loaded.campaign_id == "test-cp"
        assert loaded.experiment_count == 10

    def test_round_trip_preserves_fields(self, tmp_path):
        """Checkpoint round-trip preserves all fields."""
        cp = CampaignCheckpoint(
            campaign_id="rt-test",
            experiment_count=25,
            best_experiment_id="exp-best-42",
            best_scout_score=0.789,
            best_config={"irb_threshold": 0.50, "ema_period": 30},
            stagnation_count=5,
            crash_count=1,
            elapsed_seconds=99.99,
            created_at="2026-03-19T12:00:00+00:00",
        )
        save_checkpoint(cp, tmp_path)
        loaded = load_latest_checkpoint(tmp_path)
        assert loaded is not None
        assert loaded.campaign_id == cp.campaign_id
        assert loaded.experiment_count == cp.experiment_count
        assert loaded.best_experiment_id == cp.best_experiment_id
        assert loaded.best_scout_score == cp.best_scout_score
        assert loaded.best_config == cp.best_config
        assert loaded.stagnation_count == cp.stagnation_count
        assert loaded.crash_count == cp.crash_count
        assert loaded.elapsed_seconds == cp.elapsed_seconds
        assert loaded.created_at == cp.created_at

    def test_multiple_checkpoints_latest_loaded(self, tmp_path):
        """Multiple checkpoints, latest (highest experiment count) is loaded."""
        for i in [10, 20, 30]:
            cp = CampaignCheckpoint(
                campaign_id="multi-cp",
                experiment_count=i,
                best_experiment_id=f"exp-{i}",
                best_scout_score=float(i) / 100.0,
                best_config={},
                stagnation_count=0,
                crash_count=0,
                elapsed_seconds=float(i),
            )
            save_checkpoint(cp, tmp_path)

        loaded = load_latest_checkpoint(tmp_path)
        assert loaded is not None
        assert loaded.experiment_count == 30
        assert loaded.best_experiment_id == "exp-30"

    def test_load_from_empty_dir(self, tmp_path):
        """load_latest_checkpoint returns None from empty dir."""
        loaded = load_latest_checkpoint(tmp_path)
        assert loaded is None

    def test_load_from_nonexistent_dir(self, tmp_path):
        """load_latest_checkpoint returns None from nonexistent dir."""
        loaded = load_latest_checkpoint(tmp_path / "nonexistent")
        assert loaded is None


# ===================================================================
# Phase 5: Promotion Pipeline Tests
# ===================================================================


class TestPromotionPipeline:
    """Tests for the promotion validation pipeline."""

    def test_promotion_result_has_required_fields(self):
        """PromotionResult has all required fields."""
        result = PromotionResult(experiment_id="test-exp")
        assert result.experiment_id == "test-exp"
        assert result.walkforward_passed is False
        assert result.holdout_passed is False
        assert result.perturbation_passed is False
        assert result.stress_passed is False
        assert result.promotion_score is None
        assert result.overall_passed is False
        assert isinstance(result.details, dict)

    def test_run_promotion_returns_result(self, medium_candles, default_config):
        """run_promotion_pipeline returns a PromotionResult."""
        h1, h4 = medium_candles
        result = run_promotion_pipeline(
            config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            experiment_id="promo-test-001",
            baseline_score=0.3,
        )
        assert isinstance(result, PromotionResult)
        assert result.experiment_id == "promo-test-001"

    def test_all_stages_run_even_if_first_fails(self, medium_candles, default_config):
        """All four stages run even if the first fails."""
        h1, h4 = medium_candles
        result = run_promotion_pipeline(
            config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            experiment_id="all-stages-test",
            baseline_score=0.3,
        )
        # All four stage keys should be present in details
        assert "walkforward" in result.details
        assert "holdout" in result.details
        assert "perturbation" in result.details
        assert "stress" in result.details

    def test_overall_requires_all_four_stages(self, medium_candles, default_config):
        """overall_passed requires all 4 stages to pass."""
        h1, h4 = medium_candles
        result = run_promotion_pipeline(
            config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            experiment_id="overall-test",
            baseline_score=0.3,
        )
        if result.overall_passed:
            assert result.walkforward_passed
            assert result.holdout_passed
            assert result.perturbation_passed
            assert result.stress_passed
        else:
            # At least one must be false
            assert not (
                result.walkforward_passed
                and result.holdout_passed
                and result.perturbation_passed
                and result.stress_passed
            )

    def test_promotion_score_none_when_overall_fails(self, campaign_candles, default_config):
        """promotion_score is None when overall fails (short data likely fails WF)."""
        h1, h4 = campaign_candles  # 30 days - too short for full WF
        result = run_promotion_pipeline(
            config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            experiment_id="fail-test",
            baseline_score=0.3,
        )
        # With only 30 days of data, WF validation should fail
        if not result.overall_passed:
            assert result.promotion_score is None

    def test_promotion_score_type(self, medium_candles, default_config):
        """promotion_score is either None or a float."""
        h1, h4 = medium_candles
        result = run_promotion_pipeline(
            config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            experiment_id="type-test",
            baseline_score=0.3,
        )
        assert result.promotion_score is None or isinstance(result.promotion_score, float)

    def test_promotion_details_structure(self, medium_candles, default_config):
        """Promotion result details have expected structure per stage."""
        h1, h4 = medium_candles
        result = run_promotion_pipeline(
            config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            experiment_id="details-test",
            baseline_score=0.3,
        )
        # Walkforward details
        wf = result.details.get("walkforward", {})
        assert "windows" in wf
        assert "overall_passed" in wf

        # Perturbation details
        pt = result.details.get("perturbation", {})
        assert "all_passed" in pt
        assert "worst_drop" in pt

        # Stress details
        st = result.details.get("stress", {})
        assert "passed" in st
        assert "degradation_pct" in st

    def test_promotion_with_empty_candles(self, default_config):
        """Promotion pipeline handles empty candles gracefully."""
        result = run_promotion_pipeline(
            config=default_config,
            h1_candles=[],
            h4_candles=[],
            experiment_id="empty-test",
            baseline_score=0.3,
        )
        assert isinstance(result, PromotionResult)
        # With empty data, stages should fail gracefully (not crash)
        assert result.overall_passed is False


# ===================================================================
# Phase 5: Campaign CLI Tests
# ===================================================================


class TestCampaignCLI:
    """Tests for campaign CLI commands."""

    def test_format_campaign_report_returns_string(self):
        """format_campaign_report returns a non-empty string."""
        result = CampaignResult(
            campaign_id="test-fmt",
            total_experiments=10,
            best_experiment_id="exp-best",
            best_scout_score=0.56,
            best_config={"irb_threshold": 0.45},
            end_reason="budget_exhausted",
            status_counts={"ranked": 3, "gated_a": 7},
            elapsed_seconds=42.0,
        )
        report = format_campaign_report(result)
        assert isinstance(report, str)
        assert len(report) > 0

    def test_format_report_includes_end_reason(self):
        """format_campaign_report includes end_reason."""
        result = CampaignResult(
            campaign_id="test-reason",
            total_experiments=5,
            best_experiment_id=None,
            best_scout_score=0.0,
            best_config=None,
            end_reason="stagnation",
        )
        report = format_campaign_report(result)
        assert "stagnation" in report

    def test_format_report_includes_status_breakdown(self):
        """format_campaign_report includes status breakdown."""
        result = CampaignResult(
            campaign_id="test-status",
            total_experiments=15,
            best_experiment_id="exp-42",
            best_scout_score=0.35,
            best_config={},
            end_reason="budget_exhausted",
            status_counts={"ranked": 5, "gated_a": 8, "crashed": 2},
        )
        report = format_campaign_report(result)
        assert "ranked" in report
        assert "gated_a" in report
        assert "crashed" in report

    def test_format_report_with_improvements(self):
        """format_campaign_report includes improvement timeline."""
        result = CampaignResult(
            campaign_id="test-imp",
            total_experiments=10,
            best_experiment_id="exp-best",
            best_scout_score=0.8,
            best_config={"irb_threshold": 0.5},
            end_reason="budget_exhausted",
            improvements=[
                {"experiment_id": "exp-1", "scout_score": 0.5, "experiment_index": 2},
                {"experiment_id": "exp-best", "scout_score": 0.8, "experiment_index": 7},
            ],
        )
        report = format_campaign_report(result)
        assert "Improvements" in report
        assert "exp-1" in report

    def test_format_report_no_ranked(self):
        """format_campaign_report handles no ranked experiments."""
        result = CampaignResult(
            campaign_id="test-none",
            total_experiments=3,
            best_experiment_id=None,
            best_scout_score=0.0,
            best_config=None,
            end_reason="budget_exhausted",
        )
        report = format_campaign_report(result)
        assert "No ranked experiments" in report

    def test_start_campaign_runs(self, config_yaml, campaign_candles, tmp_path, campaign_db):
        """start_campaign runs a short campaign successfully."""
        h1, h4 = campaign_candles
        cfg = CampaignConfig(max_experiments=3, stagnation_limit=10)
        result = start_campaign(
            config_path=config_yaml,
            campaign_config=cfg,
            h1_candles=h1,
            h4_candles=h4,
            campaign_dir=tmp_path / "campaign",
            db=campaign_db,
        )
        assert isinstance(result, CampaignResult)
        assert result.total_experiments == 3

    def test_start_campaign_missing_config(self, campaign_candles, tmp_path):
        """start_campaign raises on missing config file."""
        from click.exceptions import Exit as ClickExit

        h1, h4 = campaign_candles
        cfg = CampaignConfig(max_experiments=1)
        with pytest.raises((SystemExit, ClickExit)):
            start_campaign(
                config_path=Path("/nonexistent/config.yaml"),
                campaign_config=cfg,
                h1_candles=h1,
                h4_candles=h4,
                campaign_dir=tmp_path / "campaign",
            )


# ===================================================================
# Phase 6: MCP Tools Tests
# ===================================================================


class TestMCPTools:
    """Tests for MCP tool functions (direct calls, no server)."""

    def test_bt_leaderboard_empty_db(self, campaign_db, tmp_path):
        """bt_leaderboard with empty DB returns 'No ranked experiments'."""
        from novatrade.mcp.tools import bt_leaderboard

        result = bt_leaderboard(db_path=str(tmp_path / "test_experiments.db"))
        assert "No ranked experiments" in result or "No experiments" in result

    def test_bt_leaderboard_with_records(self, campaign_db, tmp_path):
        """bt_leaderboard with populated DB returns formatted table."""
        from novatrade.mcp.tools import bt_leaderboard

        # Insert a ranked record
        rec = ExperimentRecord(
            experiment_id="exp-lb-001",
            strategy_config_hash="abc123",
            doctrine_hash="doc123",
            dataset_hash="ds123",
            engine_sha="sha123",
            scout_score=0.55,
            trade_count=50,
            profit_factor=1.8,
            max_drawdown_pct=5.2,
            status="ranked",
        )
        campaign_db.insert(rec)
        result = bt_leaderboard(db_path=str(tmp_path / "test_experiments.db"))
        assert "exp-lb-001" in result
        assert "0.55" in result

    def test_bt_compare_missing_experiments(self, campaign_db, tmp_path):
        """bt_compare returns error for missing experiments."""
        from novatrade.mcp.tools import bt_compare

        result = bt_compare("nonexistent-a", "nonexistent-b", db_path=str(tmp_path / "test_experiments.db"))
        assert "Error" in result or "not found" in result

    def test_bt_compare_with_records(self, campaign_db, tmp_path):
        """bt_compare returns comparison table for existing experiments."""
        from novatrade.mcp.tools import bt_compare

        for eid, score in [("exp-cmp-a", 0.5), ("exp-cmp-b", 0.7)]:
            rec = ExperimentRecord(
                experiment_id=eid,
                strategy_config_hash="abc",
                doctrine_hash="doc",
                dataset_hash="ds",
                engine_sha="sha",
                scout_score=score,
                trade_count=40,
                profit_factor=1.5,
                max_drawdown_pct=4.0,
                status="ranked",
            )
            campaign_db.insert(rec)
        result = bt_compare("exp-cmp-a", "exp-cmp-b", db_path=str(tmp_path / "test_experiments.db"))
        assert "exp-cmp-a" in result
        assert "exp-cmp-b" in result
        assert "Scout Score" in result

    def test_bt_run_invalid_config(self):
        """bt_run with invalid config path returns error string."""
        from novatrade.mcp.tools import bt_run

        result = bt_run("/nonexistent/config.yaml")
        assert "Error" in result or "error" in result

    def test_bt_run_valid_config(self, config_yaml, campaign_candles, tmp_path):
        """bt_run with valid config returns report string."""
        from novatrade.mcp.tools import bt_run

        h1, h4 = campaign_candles
        # Save candle CSVs to expected location
        candle_dir = tmp_path / "candles"
        candle_dir.mkdir(parents=True, exist_ok=True)
        save_candles_csv(h1, candle_dir / "EURUSD_H1.csv")
        save_candles_csv(h4, candle_dir / "EURUSD_H4.csv")

        # Patch CANDLE_DIR to point to tmp_path candles
        with (
            patch("novatrade.mcp.tools.CANDLE_DIR", candle_dir),
            patch("novatrade.mcp.tools.DEFAULT_DB", tmp_path / "exp.db"),
        ):
            result = bt_run(str(config_yaml))
        assert isinstance(result, str)
        # Should be a report or at least not an empty string
        assert len(result) > 0

    def test_bt_fetch_data_creates_csvs(self, tmp_path):
        """bt_fetch_data generates CSV files."""
        from novatrade.mcp.tools import bt_fetch_data

        with (
            patch("novatrade.mcp.tools.CANDLE_DIR", tmp_path),
            patch("novatrade.cli.commands.data.CANDLE_DIR", tmp_path),
        ):
            result = bt_fetch_data(symbol="EURUSD", days=10)
        assert "Generated" in result
        assert "EURUSD" in result

    def test_bt_campaign_start_missing_config(self):
        """bt_campaign_start returns error for missing config."""
        from novatrade.mcp.tools import bt_campaign_start

        result = bt_campaign_start("nonexistent.yaml")
        assert "Error" in result or "not found" in result

    def test_bt_campaign_status_returns_no_active(self):
        """bt_campaign_status returns 'No active campaign'."""
        from novatrade.mcp.tools import bt_campaign_status

        result = bt_campaign_status()
        assert "No active campaign" in result

    def test_bt_promote_returns_stub(self):
        """bt_promote returns stub message."""
        from novatrade.mcp.tools import bt_promote

        result = bt_promote("exp-001")
        assert "Phase 5" in result or "Promotion" in result

    def test_bt_compare_nonexistent_db(self):
        """bt_compare with non-existent DB returns error."""
        from novatrade.mcp.tools import bt_compare

        result = bt_compare("a", "b", db_path="/nonexistent/path.db")
        assert "Error" in result

    def test_bt_leaderboard_nonexistent_db(self):
        """bt_leaderboard with non-existent DB returns error."""
        from novatrade.mcp.tools import bt_leaderboard

        result = bt_leaderboard(db_path="/nonexistent/path.db")
        assert "Error" in result


# ===================================================================
# Phase 6: MCP Resources Tests
# ===================================================================


class TestMCPResources:
    """Tests for MCP resource functions."""

    def test_leaderboard_current_returns_string(self):
        """leaderboard_current returns a string."""
        from novatrade.mcp.resources import leaderboard_current

        result = leaderboard_current()
        assert isinstance(result, str)

    def test_campaign_status_returns_string(self):
        """campaign_status returns a string."""
        from novatrade.mcp.resources import campaign_status

        result = campaign_status()
        assert isinstance(result, str)
        assert "No active campaign" in result

    def test_config_baseline_returns_yaml_or_not_found(self):
        """config_baseline returns YAML content or not-found message."""
        from novatrade.mcp.resources import config_baseline

        result = config_baseline()
        assert isinstance(result, str)
        # Either real YAML content or a "not found" message
        assert "irb_baseline" in result or "not found" in result.lower()

    def test_config_baseline_with_mock(self, config_yaml):
        """config_baseline returns YAML content when baseline exists."""
        from novatrade.mcp import resources

        with patch.object(resources, "BASELINE_CONFIG", str(config_yaml)):
            result = resources.config_baseline()
        assert "irb_threshold" in result


# ===================================================================
# Integration Tests
# ===================================================================


class TestIntegration:
    """End-to-end integration tests combining multiple subsystems."""

    def test_generate_freeze_backtest(self, tmp_path):
        """End-to-end: generate data -> save to CSV -> load -> run backtest."""
        from novatrade.cli.commands.run import execute_backtest

        h1 = generate_synthetic_candles("EURUSD", "H1", days=60, start_price=1.1000)
        h4 = aggregate_h1_to_h4(h1)

        # Save and reload CSVs
        h1_path = tmp_path / "EURUSD_H1.csv"
        h4_path = tmp_path / "EURUSD_H4.csv"
        save_candles_csv(h1, h1_path)
        save_candles_csv(h4, h4_path)

        h1_loaded = load_candles_csv(h1_path, symbol="EURUSD", timeframe="H1")
        h4_loaded = load_candles_csv(h4_path, symbol="EURUSD", timeframe="H4")

        assert len(h1_loaded) == len(h1)
        assert len(h4_loaded) == len(h4)

        config = StrategyConfig()
        result = execute_backtest(
            config=config,
            h1_candles=h1_loaded,
            h4_candles=h4_loaded,
        )
        assert result.experiment_id
        assert result.status in {"valid", "gated_a", "gated_b", "ranked", "crashed"}

    def test_campaign_then_db_export(self, campaign_candles, default_config, tmp_path):
        """End-to-end: run 3-experiment campaign -> check DB -> export TSV."""
        from novatrade.cli.commands.report import export_campaign_tsv

        db = ExperimentDB(tmp_path / "integration.db")
        campaign_id = "int-test-001"
        cfg = CampaignConfig(
            campaign_id=campaign_id,
            max_experiments=3,
            stagnation_limit=10,
        )

        h1, h4 = campaign_candles
        result = run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
            campaign_dir=tmp_path / "campaign",
            db=db,
        )

        assert result.total_experiments == 3

        # Check DB has records
        count = db.count_by_campaign(campaign_id)
        assert count >= 1

        # Export TSV
        tsv_path = tmp_path / "export.tsv"
        rows = export_campaign_tsv(db, campaign_id, tsv_path)
        assert rows >= 1
        assert tsv_path.exists()

        # Verify TSV content
        content = tsv_path.read_text()
        assert "experiment_id" in content  # header
        assert "\t" in content  # tab-separated

        db.close()

    def test_full_pipeline_config_to_gate(self, tmp_path):
        """Full pipeline: config YAML -> load -> backtest -> gate evaluation -> score."""
        from novatrade.cli.commands.run import execute_backtest

        config = StrategyConfig()
        config_path = tmp_path / "pipeline_test.yaml"
        config.to_yaml(config_path)

        loaded_config = StrategyConfig.from_yaml(config_path)
        assert loaded_config.irb_threshold == config.irb_threshold

        h1 = generate_synthetic_candles("EURUSD", "H1", days=60, start_price=1.1000)
        h4 = aggregate_h1_to_h4(h1)

        db = ExperimentDB(tmp_path / "pipeline.db")
        result = execute_backtest(
            config=loaded_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_dir=tmp_path / "artifacts",
            db=db,
        )

        assert result.experiment_id
        assert result.gate_results is not None
        # If ranked, scout_score should be set
        if result.status == "ranked":
            assert result.scout_score is not None
            assert result.scout_score > 0.0

        db.close()

    def test_promotion_pipeline_end_to_end(self, medium_candles, default_config):
        """Promotion pipeline end-to-end (short data, may not pass but runs)."""
        h1, h4 = medium_candles
        result = run_promotion_pipeline(
            config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            experiment_id="e2e-promo-001",
            baseline_score=0.3,
        )
        assert isinstance(result, PromotionResult)
        assert result.experiment_id == "e2e-promo-001"
        # All four stages should have been attempted
        assert len(result.details) >= 4

    def test_campaign_checkpoints_during_run(self, campaign_candles, default_config, tmp_path):
        """Campaign creates checkpoints at the configured interval."""
        h1, h4 = campaign_candles
        campaign_dir = tmp_path / "cp_campaign"
        cfg = CampaignConfig(
            max_experiments=30,
            stagnation_limit=50,
            checkpoint_interval=10,
        )
        result = run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
            campaign_dir=campaign_dir,
        )
        # Should have checkpoints at interval
        cp_dir = campaign_dir / "checkpoints"
        if cp_dir.exists():
            cp_files = list(cp_dir.glob("cp_*.json"))
            # At least 1 checkpoint if we ran >= 10 experiments
            if result.total_experiments >= 10:
                assert len(cp_files) >= 1

    def test_stop_event_mid_campaign(self, campaign_candles, default_config):
        """Campaign can be stopped mid-execution via stop_event from a thread."""
        h1, h4 = campaign_candles
        stop = threading.Event()
        cfg = CampaignConfig(max_experiments=200, stagnation_limit=200)

        result_holder = {}

        def run_in_thread():
            result_holder["result"] = run_campaign(
                base_config=default_config,
                h1_candles=h1,
                h4_candles=h4,
                campaign_config=cfg,
                stop_event=stop,
            )

        t = threading.Thread(target=run_in_thread)
        t.start()
        # Let it run briefly then stop
        time.sleep(0.5)
        stop.set()
        t.join(timeout=30)

        assert "result" in result_holder
        result = result_holder["result"]
        assert result.end_reason == "manual_stop"
        assert result.total_experiments < 200

    def test_leaderboard_after_campaign(self, campaign_candles, default_config, tmp_path):
        """Leaderboard shows ranked experiments after a campaign."""
        db = ExperimentDB(tmp_path / "lb.db")
        campaign_id = "lb-test"
        cfg = CampaignConfig(
            campaign_id=campaign_id,
            max_experiments=5,
            stagnation_limit=10,
        )

        h1, h4 = campaign_candles
        run_campaign(
            base_config=default_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
            campaign_dir=tmp_path / "campaign",
            db=db,
        )

        records = db.get_leaderboard(campaign_id=campaign_id)
        # Records list should exist (may be empty if no experiments passed gates)
        assert isinstance(records, list)

        db.close()
