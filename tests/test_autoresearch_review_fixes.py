"""Comprehensive tests for NovaTrade AutoResearch modules.

Covers 5 previously untested modules:
  1. novatrade.evaluation.sacred_registry
  2. novatrade.evaluation.evaluation_ladder
  3. novatrade.optimization.campaign_budget
  4. novatrade.optimization.search_levels
  5. novatrade.evaluation.reproducibility_manifest
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from novatrade.evaluation.evaluation_ladder import (
    DashboardRow,
    EvaluationDashboard,
    LadderStage,
    SuccessCriteria,
    _compute_ranking_score,
    compute_dashboard_metrics,
)
from novatrade.evaluation.reproducibility_manifest import (
    ReproducibilityManifest,
    compare_manifests,
    create_manifest,
    load_manifest,
    save_manifest,
)
from novatrade.evaluation.sacred_registry import (
    SacredBundle,
    collect_sacred_hashes,
    verify_sacred_unchanged,
)
from novatrade.optimization.campaign_budget import (
    CampaignBudget,
    CampaignState,
    StopReason,
    create_campaign,
)
from novatrade.optimization.search_levels import (
    EXCLUDED_FROM_LEVEL1,
    Level2Toggle,
    SearchLevel,
    SearchLevelConfig,
    apply_level2_toggles,
    validate_level1_params,
    validate_level2_toggles,
)

# ---------------------------------------------------------------------------
# Module 1: sacred_registry
# ---------------------------------------------------------------------------


class TestSacredRegistry:
    """Tests for sacred_registry.py — sacred model version tracking."""

    def test_collect_sacred_hashes_returns_sacred_bundle(self):
        bundle = collect_sacred_hashes()
        assert isinstance(bundle, SacredBundle)

    def test_all_seven_model_hashes_are_non_empty(self):
        bundle = collect_sacred_hashes()
        assert bundle.fill_model_hash, "fill_model_hash must be non-empty"
        assert bundle.spread_model_hash, "spread_model_hash must be non-empty"
        assert bundle.slippage_model_hash, "slippage_model_hash must be non-empty"
        assert bundle.commission_model_hash, "commission_model_hash must be non-empty"
        assert bundle.session_calendar_hash, "session_calendar_hash must be non-empty"
        assert bundle.intrabar_policy_hash, "intrabar_policy_hash must be non-empty"
        assert bundle.metrics_hash, "metrics_hash must be non-empty"

    def test_combined_hash_is_non_empty(self):
        bundle = collect_sacred_hashes()
        assert bundle.combined_hash, "combined_hash must be non-empty"

    def test_combined_hash_is_deterministic(self):
        b1 = collect_sacred_hashes()
        b2 = collect_sacred_hashes()
        assert b1.combined_hash == b2.combined_hash

    def test_verify_sacred_unchanged_identical_bundle(self):
        bundle = collect_sacred_hashes()
        diffs = verify_sacred_unchanged(bundle)
        assert diffs == [], f"Expected no diffs but got: {diffs}"

    def test_verify_sacred_unchanged_modified_fill_model(self):
        bundle = collect_sacred_hashes()
        modified = replace(bundle, fill_model_hash="TAMPERED")
        diffs = verify_sacred_unchanged(modified)
        assert len(diffs) >= 1
        assert any("fill_model" in d for d in diffs)

    def test_verify_sacred_unchanged_modified_metrics(self):
        bundle = collect_sacred_hashes()
        modified = replace(bundle, metrics_hash="CHANGED")
        diffs = verify_sacred_unchanged(modified)
        assert any("metrics" in d for d in diffs)

    def test_verify_sacred_unchanged_multiple_changes(self):
        bundle = collect_sacred_hashes()
        modified = replace(
            bundle,
            spread_model_hash="X",
            slippage_model_hash="Y",
        )
        diffs = verify_sacred_unchanged(modified)
        assert len(diffs) >= 2
        names = " ".join(diffs)
        assert "spread_model" in names
        assert "slippage_model" in names

    def test_bundle_to_dict(self):
        bundle = collect_sacred_hashes()
        d = bundle.to_dict()
        assert isinstance(d, dict)
        assert "combined_hash" in d
        assert len(d) == 8  # 7 model hashes + combined

    def test_bundle_is_frozen(self):
        bundle = collect_sacred_hashes()
        with pytest.raises(AttributeError):
            bundle.fill_model_hash = "hack"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Module 2: evaluation_ladder
# ---------------------------------------------------------------------------


def _make_profitable_metrics(**overrides):
    """Build a SimpleNamespace mimicking a profitable BacktestMetrics."""
    defaults = dict(
        total_completed_trades=100,
        profit_factor=1.8,
        max_drawdown_pct=5.0,
        net_result_pips=350.0,
        total_bars=22 * 24 * 6,  # ~6 months
        expectancy_r=0.35,
        win_rate=0.55,
        max_drawdown_usd=500.0,
        average_winner_pips=25.0,
        average_loser_pips=-15.0,
        max_consecutive_losses=4,
        top_3_trades_pct_of_profit=20.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_poor_metrics(**overrides):
    """Build a SimpleNamespace with <30 trades (should get ranking_score 0)."""
    defaults = dict(
        total_completed_trades=20,
        profit_factor=0.8,
        max_drawdown_pct=25.0,
        net_result_pips=-50.0,
        total_bars=22 * 24,
        expectancy_r=-0.2,
        win_rate=0.35,
        max_drawdown_usd=2500.0,
        average_winner_pips=10.0,
        average_loser_pips=-20.0,
        max_consecutive_losses=8,
        top_3_trades_pct_of_profit=60.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestComputeDashboardMetrics:
    """Tests for compute_dashboard_metrics()."""

    def test_profitable_strategy_ranking_score_positive(self):
        m = _make_profitable_metrics()
        row = compute_dashboard_metrics(m, experiment_id="exp-001")
        assert row.ranking_score > 0

    def test_profitable_strategy_ladder_stage_is_ranked(self):
        m = _make_profitable_metrics()
        row = compute_dashboard_metrics(m, experiment_id="exp-002")
        assert row.ladder_stage == LadderStage.RANKED

    def test_low_trade_count_ranking_score_zero(self):
        m = _make_poor_metrics(total_completed_trades=20)
        row = compute_dashboard_metrics(m)
        assert row.ranking_score == 0.0

    def test_low_trade_count_ladder_stage_gated_b(self):
        m = _make_poor_metrics(total_completed_trades=20)
        row = compute_dashboard_metrics(m)
        assert row.ladder_stage == LadderStage.GATED_B

    def test_trade_count_extracted_correctly(self):
        m = _make_profitable_metrics(total_completed_trades=75)
        row = compute_dashboard_metrics(m)
        assert row.trade_count == 75

    def test_profit_factor_extracted(self):
        m = _make_profitable_metrics(profit_factor=2.1)
        row = compute_dashboard_metrics(m)
        assert row.profit_factor == pytest.approx(2.1)

    def test_max_drawdown_extracted(self):
        m = _make_profitable_metrics(max_drawdown_pct=8.5)
        row = compute_dashboard_metrics(m)
        assert row.max_drawdown_pct == pytest.approx(8.5)

    def test_expectancy_r_extracted(self):
        m = _make_profitable_metrics(expectancy_r=0.42)
        row = compute_dashboard_metrics(m)
        assert row.expectancy_r == pytest.approx(0.42)

    def test_win_rate_extracted(self):
        m = _make_profitable_metrics(win_rate=0.60)
        row = compute_dashboard_metrics(m)
        assert row.win_rate == pytest.approx(0.60)

    def test_stage_a_failures_propagated(self):
        gate_results = [
            SimpleNamespace(passed=True, gate="min_trades"),
            SimpleNamespace(passed=False, gate="max_drawdown"),
        ]
        m = _make_profitable_metrics()
        row = compute_dashboard_metrics(m, gate_a_results=gate_results)
        assert not row.stage_a_passed
        assert "max_drawdown" in row.stage_a_failures
        assert row.ladder_stage == LadderStage.GATED_A

    def test_stage_a_all_pass(self):
        gate_results = [
            SimpleNamespace(passed=True, gate="min_trades"),
            SimpleNamespace(passed=True, gate="max_drawdown"),
        ]
        m = _make_profitable_metrics()
        row = compute_dashboard_metrics(m, gate_a_results=gate_results)
        assert row.stage_a_passed

    def test_dashboard_row_to_dict(self):
        m = _make_profitable_metrics()
        row = compute_dashboard_metrics(m, experiment_id="exp-td")
        d = row.to_dict()
        assert d["experiment_id"] == "exp-td"
        assert "ranking_score" in d
        assert "ladder_stage" in d

    def test_avg_win_loss_ratio_computed(self):
        m = _make_profitable_metrics(average_winner_pips=30.0, average_loser_pips=-10.0)
        row = compute_dashboard_metrics(m)
        assert row.avg_win_loss_ratio == pytest.approx(3.0)

    def test_recovery_factor_computed(self):
        m = _make_profitable_metrics(net_result_pips=200.0, max_drawdown_usd=100.0)
        row = compute_dashboard_metrics(m)
        assert row.recovery_factor == pytest.approx(2.0)

    def test_missing_attribute_uses_default(self):
        """SimpleNamespace without some fields should not crash."""
        m = SimpleNamespace(total_completed_trades=50, total_bars=5280)
        row = compute_dashboard_metrics(m)
        assert row.profit_factor == 0.0
        assert row.trade_count == 50


class TestComputeRankingScore:
    """Tests for _compute_ranking_score() penalties and normalization."""

    def test_below_30_trades_returns_zero(self):
        row = DashboardRow(trade_count=29, profit_factor=2.0, calmar_ratio=3.0)
        assert _compute_ranking_score(row) == 0.0

    def test_exactly_30_trades_can_score(self):
        row = DashboardRow(
            trade_count=30,
            profit_factor=2.0,
            calmar_ratio=2.0,
            expectancy_r=0.5,
            recovery_factor=2.0,
            win_rate=0.55,
            trades_per_month=10.0,
            max_drawdown_pct=5.0,
        )
        assert _compute_ranking_score(row) > 0

    def test_high_drawdown_penalty_10pct(self):
        base = DashboardRow(
            trade_count=50,
            profit_factor=2.0,
            calmar_ratio=2.0,
            expectancy_r=0.5,
            recovery_factor=2.0,
            win_rate=0.55,
            trades_per_month=10.0,
            max_drawdown_pct=5.0,
        )
        penalized = DashboardRow(
            trade_count=50,
            profit_factor=2.0,
            calmar_ratio=2.0,
            expectancy_r=0.5,
            recovery_factor=2.0,
            win_rate=0.55,
            trades_per_month=10.0,
            max_drawdown_pct=15.0,
        )
        score_base = _compute_ranking_score(base)
        score_pen = _compute_ranking_score(penalized)
        assert score_pen < score_base
        assert score_base - score_pen == pytest.approx(0.10, abs=0.01)

    def test_high_drawdown_penalty_20pct_double(self):
        low_dd = DashboardRow(
            trade_count=50,
            profit_factor=2.0,
            calmar_ratio=2.0,
            expectancy_r=0.5,
            recovery_factor=2.0,
            win_rate=0.55,
            trades_per_month=10.0,
            max_drawdown_pct=5.0,
        )
        high_dd = DashboardRow(
            trade_count=50,
            profit_factor=2.0,
            calmar_ratio=2.0,
            expectancy_r=0.5,
            recovery_factor=2.0,
            win_rate=0.55,
            trades_per_month=10.0,
            max_drawdown_pct=25.0,
        )
        score_low = _compute_ranking_score(low_dd)
        score_high = _compute_ranking_score(high_dd)
        assert score_low - score_high == pytest.approx(0.20, abs=0.01)

    def test_ranking_score_capped_at_1(self):
        row = DashboardRow(
            trade_count=100,
            profit_factor=10.0,
            calmar_ratio=100.0,
            expectancy_r=5.0,
            recovery_factor=50.0,
            win_rate=1.0,
            trades_per_month=100.0,
            max_drawdown_pct=0.0,
        )
        assert _compute_ranking_score(row) <= 1.0

    def test_ranking_score_floored_at_0(self):
        row = DashboardRow(
            trade_count=50,
            profit_factor=0.0,
            calmar_ratio=0.0,
            expectancy_r=-1.0,
            recovery_factor=0.0,
            win_rate=0.0,
            trades_per_month=0.0,
            max_drawdown_pct=30.0,
        )
        assert _compute_ranking_score(row) >= 0.0


class TestEvaluationDashboard:
    """Tests for EvaluationDashboard operations."""

    def _make_row(self, exp_id, stage=LadderStage.RANKED, score=0.5):
        return DashboardRow(
            experiment_id=exp_id,
            ladder_stage=stage,
            ranking_score=score,
        )

    def test_add_and_count(self):
        db = EvaluationDashboard()
        db.add(self._make_row("a"))
        db.add(self._make_row("b"))
        assert len(db.rows) == 2

    def test_ranked_returns_only_eligible(self):
        db = EvaluationDashboard()
        db.add(self._make_row("ranked1", LadderStage.RANKED, 0.8))
        db.add(self._make_row("rejected1", LadderStage.REJECTED, 0.1))
        db.add(self._make_row("promoted1", LadderStage.PROMOTED, 0.9))
        ranked = db.ranked
        assert len(ranked) == 2
        assert ranked[0].experiment_id == "promoted1"  # highest score first
        assert ranked[1].experiment_id == "ranked1"

    def test_rejected_returns_failed(self):
        db = EvaluationDashboard()
        db.add(self._make_row("r1", LadderStage.RANKED, 0.7))
        db.add(self._make_row("f1", LadderStage.REJECTED, 0.0))
        db.add(self._make_row("f2", LadderStage.GATED_A, 0.0))
        db.add(self._make_row("f3", LadderStage.GATED_B, 0.0))
        rejected = db.rejected
        assert len(rejected) == 3

    def test_promote_sets_stage_and_score(self):
        db = EvaluationDashboard()
        db.add(self._make_row("exp-1", LadderStage.RANKED, 0.6))
        result = db.promote("exp-1", promotion_score=0.85)
        assert result is True
        assert db.rows[0].ladder_stage == LadderStage.PROMOTED
        assert db.rows[0].promotion_score == 0.85
        assert db.rows[0].stage_c_passed is True

    def test_promote_missing_experiment_returns_false(self):
        db = EvaluationDashboard()
        db.add(self._make_row("exp-1"))
        assert db.promote("nonexistent", 0.5) is False

    def test_export_tsv_has_header_and_data(self):
        db = EvaluationDashboard()
        db.add(self._make_row("exp-tsv", LadderStage.RANKED, 0.72))
        tsv = db.export_tsv()
        lines = tsv.strip().split("\n")
        assert len(lines) == 2  # header + 1 data row
        assert "experiment_id" in lines[0]
        assert "exp-tsv" in lines[1]

    def test_export_tsv_empty_dashboard(self):
        db = EvaluationDashboard()
        tsv = db.export_tsv()
        lines = tsv.strip().split("\n")
        assert len(lines) == 1  # header only


class TestSuccessCriteria:
    """Tests for SuccessCriteria.met property."""

    def test_all_true_met(self):
        sc = SuccessCriteria(
            reproducible_from_frozen=True,
            engine_under_target_runtime=True,
            all_gates_passed=True,
            promotion_passed=True,
        )
        assert sc.met is True

    def test_one_false_not_met(self):
        sc = SuccessCriteria(
            reproducible_from_frozen=True,
            engine_under_target_runtime=True,
            all_gates_passed=False,
            promotion_passed=True,
        )
        assert sc.met is False

    def test_all_false_not_met(self):
        sc = SuccessCriteria()
        assert sc.met is False


# ---------------------------------------------------------------------------
# Module 3: campaign_budget
# ---------------------------------------------------------------------------


class TestCampaignBudget:
    """Tests for CampaignBudget dataclass."""

    def test_default_values(self):
        b = CampaignBudget()
        assert b.max_experiments == 200
        assert b.max_wall_clock_seconds == 6 * 3600
        assert b.max_crashes == 20
        assert b.max_stagnant_experiments == 10
        assert b.checkpoint_interval == 25

    def test_custom_values(self):
        b = CampaignBudget(max_experiments=50, max_crashes=5)
        assert b.max_experiments == 50
        assert b.max_crashes == 5

    def test_to_dict(self):
        b = CampaignBudget()
        d = b.to_dict()
        assert isinstance(d, dict)
        assert d["max_experiments"] == 200
        assert "checkpoint_interval" in d


class TestCampaignState:
    """Tests for CampaignState and create_campaign()."""

    def test_create_campaign_returns_state(self):
        state = create_campaign("test-001")
        assert isinstance(state, CampaignState)
        assert state.campaign_id == "test-001"
        assert state.total_experiments == 0

    def test_create_campaign_custom_budget(self):
        budget = CampaignBudget(max_experiments=10)
        state = create_campaign("custom", budget=budget)
        assert state.budget.max_experiments == 10

    def test_record_successful_experiment(self):
        state = create_campaign("s1")
        state.record_experiment("e1", score=0.75)
        assert state.total_experiments == 1
        assert state.successful_experiments == 1
        assert state.crashed_experiments == 0
        assert state.best_score == 0.75
        assert state.best_experiment_id == "e1"

    def test_record_crashed_experiment(self):
        state = create_campaign("s2")
        state.record_experiment("e1", score=None, crashed=True)
        assert state.total_experiments == 1
        assert state.successful_experiments == 0
        assert state.crashed_experiments == 1

    def test_record_improvement_resets_stagnation(self):
        state = create_campaign("s3")
        state.record_experiment("e1", score=0.5)
        state.record_experiment("e2", score=0.4)  # no improvement
        assert state.experiments_since_improvement == 1
        state.record_experiment("e3", score=0.8)  # improvement
        assert state.experiments_since_improvement == 0
        assert state.best_experiment_id == "e3"

    def test_should_stop_running(self):
        state = create_campaign("run")
        assert state.should_stop() == StopReason.RUNNING

    def test_should_stop_experiment_limit(self):
        budget = CampaignBudget(max_experiments=3)
        state = create_campaign("el", budget=budget)
        for i in range(3):
            state.record_experiment(f"e{i}", score=0.1 * i)
        assert state.should_stop() == StopReason.EXPERIMENT_LIMIT

    def test_should_stop_wall_clock(self):
        budget = CampaignBudget(max_wall_clock_seconds=0.0)
        state = create_campaign("wc", budget=budget)
        # Wall clock is already >= 0.0 since started_at is in the past
        assert state.should_stop() == StopReason.WALL_CLOCK_LIMIT

    def test_should_stop_crash_limit(self):
        budget = CampaignBudget(max_crashes=2)
        state = create_campaign("cl", budget=budget)
        state.record_experiment("c1", score=None, crashed=True)
        state.record_experiment("c2", score=None, crashed=True)
        assert state.should_stop() == StopReason.CRASH_LIMIT

    def test_should_stop_stagnation(self):
        budget = CampaignBudget(max_stagnant_experiments=3)
        state = create_campaign("stag", budget=budget)
        state.record_experiment("e1", score=0.5)
        state.record_experiment("e2", score=0.3)
        state.record_experiment("e3", score=0.2)
        state.record_experiment("e4", score=0.1)
        assert state.should_stop() == StopReason.STAGNATION

    def test_needs_checkpoint_false_initially(self):
        state = create_campaign("cp")
        assert not state.needs_checkpoint()

    def test_needs_checkpoint_after_interval(self):
        budget = CampaignBudget(checkpoint_interval=3)
        state = create_campaign("cp2", budget=budget)
        for i in range(3):
            state.record_experiment(f"e{i}", score=0.1)
        assert state.needs_checkpoint()

    def test_mark_checkpoint_resets_counter(self):
        budget = CampaignBudget(checkpoint_interval=2)
        state = create_campaign("cp3", budget=budget)
        state.record_experiment("e1", score=0.1)
        state.record_experiment("e2", score=0.2)
        assert state.needs_checkpoint()
        state.mark_checkpoint()
        assert not state.needs_checkpoint()
        assert state.checkpoints_created == 1

    def test_summary_dict_structure(self):
        state = create_campaign("sum")
        state.record_experiment("e1", score=0.65)
        s = state.summary()
        assert isinstance(s, dict)
        assert s["campaign_id"] == "sum"
        assert s["total_experiments"] == 1
        assert s["successful"] == 1
        assert s["crashed"] == 0
        assert s["best_score"] == pytest.approx(0.65, abs=0.01)
        assert "elapsed_seconds" in s
        assert "started_at_utc" in s
        assert "remaining_experiments" in s
        assert "checkpoints_created" in s
        assert s["status"] == "running"

    def test_remaining_experiments(self):
        budget = CampaignBudget(max_experiments=10)
        state = create_campaign("rem", budget=budget)
        state.record_experiment("e1", score=0.1)
        assert state.remaining_experiments == 9

    def test_elapsed_seconds_positive(self):
        state = create_campaign("el2")
        assert state.elapsed_seconds >= 0

    def test_remaining_seconds(self):
        budget = CampaignBudget(max_wall_clock_seconds=100.0)
        state = create_campaign("rs", budget=budget)
        assert state.remaining_seconds <= 100.0
        assert state.remaining_seconds >= 0.0


# ---------------------------------------------------------------------------
# Module 4: search_levels
# ---------------------------------------------------------------------------


class TestValidateLevel1Params:
    """Tests for validate_level1_params()."""

    def test_valid_params_no_violations(self):
        candidate = {"irb_threshold": 0.45, "ema_period": 21}
        violations = validate_level1_params(candidate)
        assert violations == []

    def test_excluded_param_risk_fraction(self):
        candidate = {"risk_fraction": 0.02}
        violations = validate_level1_params(candidate)
        assert len(violations) == 1
        assert "risk_fraction" in violations[0]
        assert "sizing" in violations[0]

    def test_excluded_param_slippage(self):
        candidate = {"slippage_pips": 0.5}
        violations = validate_level1_params(candidate)
        assert len(violations) == 1
        assert "slippage_pips" in violations[0]

    def test_unrecognized_param(self):
        candidate = {"magic_indicator": 42}
        violations = validate_level1_params(candidate)
        assert len(violations) == 1
        assert "OPTIMIZABLE_PARAMS" in violations[0]

    def test_mixed_valid_and_invalid(self):
        candidate = {
            "irb_threshold": 0.45,
            "risk_fraction": 0.02,
            "unknown_param": 1,
        }
        violations = validate_level1_params(candidate)
        assert len(violations) == 2

    def test_all_excluded_params_rejected(self):
        for param in EXCLUDED_FROM_LEVEL1:
            violations = validate_level1_params({param: 1.0})
            assert len(violations) == 1, f"{param} should be excluded"


class TestValidateLevel2Toggles:
    """Tests for validate_level2_toggles()."""

    def test_valid_filter_toggle(self):
        toggles = [Level2Toggle(toggle_type="filter", name="trend_alignment")]
        assert validate_level2_toggles(toggles) == []

    def test_invalid_filter_name(self):
        toggles = [Level2Toggle(toggle_type="filter", name="nonexistent_filter")]
        violations = validate_level2_toggles(toggles)
        assert len(violations) == 1
        assert "nonexistent_filter" in violations[0]

    def test_valid_threshold_preset(self):
        toggles = [Level2Toggle(toggle_type="threshold_preset", name="preset", value="moderate")]
        assert validate_level2_toggles(toggles) == []

    def test_invalid_threshold_preset(self):
        toggles = [Level2Toggle(toggle_type="threshold_preset", name="preset", value="ultra")]
        violations = validate_level2_toggles(toggles)
        assert len(violations) == 1

    def test_valid_exit_variant(self):
        toggles = [Level2Toggle(toggle_type="exit_variant", name="trailing_tight")]
        assert validate_level2_toggles(toggles) == []

    def test_invalid_exit_variant(self):
        toggles = [Level2Toggle(toggle_type="exit_variant", name="scalp_exit")]
        violations = validate_level2_toggles(toggles)
        assert len(violations) == 1

    def test_valid_session_toggle(self):
        toggles = [Level2Toggle(toggle_type="session", name="s", value="london")]
        assert validate_level2_toggles(toggles) == []

    def test_invalid_session_toggle(self):
        toggles = [Level2Toggle(toggle_type="session", name="s", value="tokyo")]
        violations = validate_level2_toggles(toggles)
        assert len(violations) == 1

    def test_invalid_toggle_type_raises(self):
        with pytest.raises(ValueError, match="Unknown toggle_type"):
            Level2Toggle(toggle_type="bad_type", name="x")


class TestApplyLevel2Toggles:
    """Tests for apply_level2_toggles()."""

    def test_enable_filter(self):
        base = {"filters_enabled": []}
        toggles = [Level2Toggle(toggle_type="filter", name="trend_alignment", enabled=True)]
        result = apply_level2_toggles(base, toggles)
        assert "trend_alignment" in result["filters_enabled"]

    def test_disable_filter(self):
        base = {"filters_enabled": ["trend_alignment", "adx_strength"]}
        toggles = [Level2Toggle(toggle_type="filter", name="trend_alignment", enabled=False)]
        result = apply_level2_toggles(base, toggles)
        assert "trend_alignment" not in result["filters_enabled"]
        assert "adx_strength" in result["filters_enabled"]

    def test_apply_threshold_preset(self):
        base = {"irb_threshold": 0.50}
        toggles = [Level2Toggle(toggle_type="threshold_preset", name="p", value="aggressive")]
        result = apply_level2_toggles(base, toggles)
        assert result["irb_threshold"] == 0.35
        assert result["adx_threshold"] == 17.0

    def test_apply_session_filter(self):
        base = {"session_filter": None}
        toggles = [Level2Toggle(toggle_type="session", name="s", value="london")]
        result = apply_level2_toggles(base, toggles)
        assert result["session_filter"] == "london"

    def test_apply_exit_variant(self):
        base = {}
        toggles = [Level2Toggle(toggle_type="exit_variant", name="trailing_wide")]
        result = apply_level2_toggles(base, toggles)
        assert result["trail_atr_multiplier"] == 2.5
        assert result["time_stop_bars"] == 60
        assert "description" not in result

    def test_does_not_mutate_base(self):
        base = {"filters_enabled": ["trend_alignment"]}
        toggles = [Level2Toggle(toggle_type="filter", name="adx_strength", enabled=True)]
        result = apply_level2_toggles(base, toggles)
        assert "adx_strength" in result["filters_enabled"]
        assert "adx_strength" not in base["filters_enabled"]


class TestSearchLevelConfig:
    """Tests for SearchLevelConfig validation."""

    def test_level1_default_valid(self):
        cfg = SearchLevelConfig(level=SearchLevel.LEVEL_1)
        assert cfg.validate() == []

    def test_level2_valid_toggles(self):
        cfg = SearchLevelConfig(
            level=SearchLevel.LEVEL_2,
            level2_toggles=[
                Level2Toggle(toggle_type="filter", name="trend_alignment"),
            ],
        )
        assert cfg.validate() == []

    def test_level2_invalid_toggle_caught(self):
        cfg = SearchLevelConfig(
            level=SearchLevel.LEVEL_2,
            level2_toggles=[
                Level2Toggle(toggle_type="filter", name="fake_filter"),
            ],
        )
        violations = cfg.validate()
        assert len(violations) >= 1

    def test_level3_missing_template(self):
        cfg = SearchLevelConfig(level=SearchLevel.LEVEL_3, level3_template=None)
        violations = cfg.validate()
        assert any("template" in v.lower() for v in violations)


# ---------------------------------------------------------------------------
# Module 5: reproducibility_manifest
# ---------------------------------------------------------------------------


class TestCreateManifest:
    """Tests for create_manifest()."""

    def test_returns_valid_manifest(self):
        m = create_manifest(
            config_dict={"irb_threshold": 0.45, "ema_period": 21},
            strategy_name="IRB-v1",
            strategy_version="1.0.0",
            dataset_hash="abc123",
            dataset_symbol="EURUSD",
            dataset_date_range="2020-01-01..2025-12-31",
            doctrine_hash="doc_hash_1",
            campaign_id="camp-01",
        )
        assert isinstance(m, ReproducibilityManifest)
        assert m.strategy_name == "IRB-v1"
        assert m.dataset_symbol == "EURUSD"
        assert m.manifest_hash  # non-empty

    def test_manifest_hash_is_deterministic(self):
        kwargs = dict(
            config_dict={"irb_threshold": 0.45},
            strategy_name="test",
            strategy_version="1.0",
            dataset_hash="d1",
            dataset_symbol="EURUSD",
            dataset_date_range="2020..2025",
            doctrine_hash="doc1",
        )
        m1 = create_manifest(**kwargs)
        m2 = create_manifest(**kwargs)
        assert m1.manifest_hash == m2.manifest_hash

    def test_different_config_different_hash(self):
        common = dict(
            strategy_name="test",
            strategy_version="1.0",
            dataset_hash="d1",
            dataset_symbol="EURUSD",
            dataset_date_range="2020..2025",
            doctrine_hash="doc1",
        )
        m1 = create_manifest(config_dict={"irb_threshold": 0.45}, **common)
        m2 = create_manifest(config_dict={"irb_threshold": 0.50}, **common)
        assert m1.manifest_hash != m2.manifest_hash

    def test_no_doctrine_uses_fallback(self):
        m = create_manifest(
            config_dict={"a": 1},
            strategy_name="t",
            strategy_version="1",
            dataset_hash="d",
            dataset_symbol="X",
            dataset_date_range="r",
        )
        assert m.doctrine_hash == "no_doctrine"

    def test_manifest_to_dict(self):
        m = create_manifest(
            config_dict={"a": 1},
            strategy_name="t",
            strategy_version="1",
            dataset_hash="d",
            dataset_symbol="X",
            dataset_date_range="r",
            doctrine_hash="dh",
        )
        d = m.to_dict()
        assert isinstance(d, dict)
        assert "manifest_hash" in d
        assert "sacred_bundle" in d


class TestSaveLoadManifest:
    """Tests for save_manifest() and load_manifest() round-trip."""

    def test_round_trip(self, tmp_path):
        original = create_manifest(
            config_dict={"irb_threshold": 0.45, "ema_period": 21},
            strategy_name="IRB-v1",
            strategy_version="1.0.0",
            dataset_hash="abc123",
            dataset_symbol="EURUSD",
            dataset_date_range="2020-01-01..2025-12-31",
            doctrine_hash="doc_hash",
            campaign_id="camp-rt",
        )
        saved_path = save_manifest(original, tmp_path)
        assert saved_path.exists()
        assert saved_path.name.startswith("manifest_")

        loaded = load_manifest(saved_path)
        assert loaded.manifest_hash == original.manifest_hash
        assert loaded.strategy_name == original.strategy_name
        assert loaded.dataset_symbol == original.dataset_symbol
        assert loaded.campaign_id == original.campaign_id

    def test_save_creates_directory(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        m = create_manifest(
            config_dict={"a": 1},
            strategy_name="t",
            strategy_version="1",
            dataset_hash="d",
            dataset_symbol="X",
            dataset_date_range="r",
            doctrine_hash="dh",
        )
        path = save_manifest(m, nested)
        assert path.exists()
        assert nested.exists()

    def test_saved_file_is_valid_json(self, tmp_path):
        m = create_manifest(
            config_dict={"x": 1},
            strategy_name="j",
            strategy_version="1",
            dataset_hash="d",
            dataset_symbol="X",
            dataset_date_range="r",
            doctrine_hash="dh",
        )
        path = save_manifest(m, tmp_path)
        data = json.loads(path.read_text())
        assert "manifest_hash" in data
        assert data["strategy_name"] == "j"


class TestCompareManifests:
    """Tests for compare_manifests()."""

    def test_identical_manifests_empty_diff(self):
        m = create_manifest(
            config_dict={"a": 1},
            strategy_name="t",
            strategy_version="1",
            dataset_hash="d",
            dataset_symbol="X",
            dataset_date_range="r",
            doctrine_hash="dh",
        )
        diffs = compare_manifests(m, m)
        assert diffs == {}

    def test_different_config_hash_detected(self):
        common = dict(
            strategy_name="t",
            strategy_version="1",
            dataset_hash="d",
            dataset_symbol="X",
            dataset_date_range="r",
            doctrine_hash="dh",
        )
        m1 = create_manifest(config_dict={"a": 1}, **common)
        m2 = create_manifest(config_dict={"a": 2}, **common)
        diffs = compare_manifests(m1, m2)
        assert "strategy_config_hash" in diffs
        # manifest_hash should also differ
        assert "manifest_hash" in diffs

    def test_different_doctrine_hash_detected(self):
        common = dict(
            config_dict={"a": 1},
            strategy_name="t",
            strategy_version="1",
            dataset_hash="d",
            dataset_symbol="X",
            dataset_date_range="r",
        )
        m1 = create_manifest(doctrine_hash="d1", **common)
        m2 = create_manifest(doctrine_hash="d2", **common)
        diffs = compare_manifests(m1, m2)
        assert "doctrine_hash" in diffs

    def test_different_dataset_hash_detected(self):
        common = dict(
            config_dict={"a": 1},
            strategy_name="t",
            strategy_version="1",
            dataset_symbol="X",
            dataset_date_range="r",
            doctrine_hash="dh",
        )
        m1 = create_manifest(dataset_hash="d1", **common)
        m2 = create_manifest(dataset_hash="d2", **common)
        diffs = compare_manifests(m1, m2)
        assert "dataset_hash" in diffs

    def test_diff_returns_tuple_of_both_values(self):
        common = dict(
            config_dict={"a": 1},
            strategy_name="t",
            strategy_version="1",
            dataset_hash="d",
            dataset_symbol="X",
            dataset_date_range="r",
        )
        m1 = create_manifest(doctrine_hash="old", **common)
        m2 = create_manifest(doctrine_hash="new", **common)
        diffs = compare_manifests(m1, m2)
        assert "doctrine_hash" in diffs
        old_val, new_val = diffs["doctrine_hash"]
        assert old_val == m1.doctrine_hash
        assert new_val == m2.doctrine_hash
