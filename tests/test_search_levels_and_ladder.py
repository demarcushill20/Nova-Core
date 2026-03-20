"""Tests for search-level DSL and gated evaluation ladder.

Covers:
  - Level 1 parameter validation (exclusions)
  - Level 2 rule toggle DSL (registered filters, presets, exit variants)
  - Level 3 structural templates (registered modules, compositions)
  - Evaluation ladder (dashboard metrics, ranking, promotion)
  - MCP thin-wrapper contract
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Level 1: Parameter constraints
# ---------------------------------------------------------------------------


class TestLevel1Validation:
    """Level 1 permits only OPTIMIZABLE_PARAMS, excludes risk_fraction et al."""

    def test_clean_level1_params_pass(self):
        from novatrade.optimization.search_levels import validate_level1_params

        candidate = {"irb_threshold": 0.50, "ema_period": 25, "atr_period": 14}
        violations = validate_level1_params(candidate)
        assert violations == []

    def test_risk_fraction_excluded(self):
        from novatrade.optimization.search_levels import validate_level1_params

        candidate = {"irb_threshold": 0.50, "risk_fraction": 0.02}
        violations = validate_level1_params(candidate)
        assert len(violations) == 1
        assert "risk_fraction" in violations[0]
        assert "sizing" in violations[0]

    def test_execution_params_excluded(self):
        from novatrade.optimization.search_levels import validate_level1_params

        candidate = {"commission_per_lot_usd": 7.0, "slippage_pips": 1.5}
        violations = validate_level1_params(candidate)
        assert len(violations) == 2
        for v in violations:
            assert "accounting/execution/alignment" in v

    def test_data_alignment_excluded(self):
        from novatrade.optimization.search_levels import validate_level1_params

        candidate = {"data_start_index": 100, "candle_alignment_mode": "utc"}
        violations = validate_level1_params(candidate)
        assert len(violations) == 2

    def test_unknown_param_rejected(self):
        from novatrade.optimization.search_levels import validate_level1_params

        candidate = {"irb_threshold": 0.50, "made_up_param": 42}
        violations = validate_level1_params(candidate)
        assert len(violations) == 1
        assert "made_up_param" in violations[0]

    def test_all_optimizable_params_accepted(self):
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.search_levels import validate_level1_params

        candidate = {p: 0.5 for p in StrategyConfig.OPTIMIZABLE_PARAMS}
        violations = validate_level1_params(candidate)
        assert violations == []

    def test_excluded_set_contents(self):
        from novatrade.optimization.search_levels import EXCLUDED_FROM_LEVEL1

        assert "risk_fraction" in EXCLUDED_FROM_LEVEL1
        assert "initial_equity" in EXCLUDED_FROM_LEVEL1
        assert "commission_per_lot_usd" in EXCLUDED_FROM_LEVEL1
        assert "slippage_pips" in EXCLUDED_FROM_LEVEL1
        assert "avg_spread_pips" in EXCLUDED_FROM_LEVEL1
        assert "stop_first_on_ambiguity" in EXCLUDED_FROM_LEVEL1
        # Signal params should NOT be excluded
        assert "irb_threshold" not in EXCLUDED_FROM_LEVEL1
        assert "ema_period" not in EXCLUDED_FROM_LEVEL1


# ---------------------------------------------------------------------------
# Level 2: Rule toggle DSL
# ---------------------------------------------------------------------------


class TestLevel2Toggles:
    """Level 2 permits only registered filters, presets, exit variants."""

    def test_valid_filter_toggle(self):
        from novatrade.optimization.search_levels import (
            Level2Toggle,
            validate_level2_toggles,
        )

        toggles = [
            Level2Toggle(toggle_type="filter", name="trend_alignment", enabled=True),
            Level2Toggle(toggle_type="filter", name="adx_strength", enabled=False),
        ]
        violations = validate_level2_toggles(toggles)
        assert violations == []

    def test_unregistered_filter_rejected(self):
        from novatrade.optimization.search_levels import (
            Level2Toggle,
            validate_level2_toggles,
        )

        toggles = [
            Level2Toggle(toggle_type="filter", name="magic_indicator"),
        ]
        violations = validate_level2_toggles(toggles)
        assert len(violations) == 1
        assert "magic_indicator" in violations[0]
        assert "not registered" in violations[0]

    def test_valid_threshold_preset(self):
        from novatrade.optimization.search_levels import (
            Level2Toggle,
            validate_level2_toggles,
        )

        toggles = [
            Level2Toggle(toggle_type="threshold_preset", name="preset", value="conservative"),
        ]
        violations = validate_level2_toggles(toggles)
        assert violations == []

    def test_invalid_threshold_preset(self):
        from novatrade.optimization.search_levels import (
            Level2Toggle,
            validate_level2_toggles,
        )

        toggles = [
            Level2Toggle(toggle_type="threshold_preset", name="preset", value="yolo"),
        ]
        violations = validate_level2_toggles(toggles)
        assert len(violations) == 1
        assert "yolo" in violations[0]

    def test_valid_exit_variant(self):
        from novatrade.optimization.search_levels import (
            Level2Toggle,
            validate_level2_toggles,
        )

        toggles = [
            Level2Toggle(toggle_type="exit_variant", name="trailing_tight"),
        ]
        violations = validate_level2_toggles(toggles)
        assert violations == []

    def test_invalid_exit_variant(self):
        from novatrade.optimization.search_levels import (
            Level2Toggle,
            validate_level2_toggles,
        )

        toggles = [
            Level2Toggle(toggle_type="exit_variant", name="rocket_exit"),
        ]
        violations = validate_level2_toggles(toggles)
        assert len(violations) == 1
        assert "rocket_exit" in violations[0]

    def test_valid_session_filter(self):
        from novatrade.optimization.search_levels import (
            Level2Toggle,
            validate_level2_toggles,
        )

        toggles = [
            Level2Toggle(toggle_type="session", name="session", value="london"),
        ]
        violations = validate_level2_toggles(toggles)
        assert violations == []

    def test_invalid_session_filter(self):
        from novatrade.optimization.search_levels import (
            Level2Toggle,
            validate_level2_toggles,
        )

        toggles = [
            Level2Toggle(toggle_type="session", name="session", value="mars_session"),
        ]
        violations = validate_level2_toggles(toggles)
        assert len(violations) == 1
        assert "mars_session" in violations[0]

    def test_invalid_toggle_type_raises(self):
        from novatrade.optimization.search_levels import Level2Toggle

        with pytest.raises(ValueError, match="Unknown toggle_type"):
            Level2Toggle(toggle_type="freeform", name="bad")

    def test_apply_filter_toggles(self):
        from novatrade.optimization.search_levels import (
            Level2Toggle,
            apply_level2_toggles,
        )

        base = {"irb_threshold": 0.45, "filters_enabled": ["adx_strength"]}
        toggles = [
            Level2Toggle(toggle_type="filter", name="trend_alignment", enabled=True),
            Level2Toggle(toggle_type="filter", name="adx_strength", enabled=False),
        ]
        result = apply_level2_toggles(base, toggles)
        assert "trend_alignment" in result["filters_enabled"]
        assert "adx_strength" not in result["filters_enabled"]
        # Original not mutated
        assert "adx_strength" in base["filters_enabled"]

    def test_apply_threshold_preset(self):
        from novatrade.optimization.search_levels import (
            Level2Toggle,
            apply_level2_toggles,
        )

        base = {"irb_threshold": 0.45, "adx_threshold": 20.0}
        toggles = [
            Level2Toggle(toggle_type="threshold_preset", name="p", value="conservative"),
        ]
        result = apply_level2_toggles(base, toggles)
        assert result["irb_threshold"] == 0.50
        assert result["adx_threshold"] == 25.0

    def test_apply_exit_variant(self):
        from novatrade.optimization.search_levels import (
            Level2Toggle,
            apply_level2_toggles,
        )

        base = {"trail_atr_multiplier": 1.5, "time_stop_bars": 40}
        toggles = [
            Level2Toggle(toggle_type="exit_variant", name="trailing_tight"),
        ]
        result = apply_level2_toggles(base, toggles)
        assert result["trail_atr_multiplier"] == 1.0
        assert result["time_stop_bars"] == 30

    def test_apply_session_filter(self):
        from novatrade.optimization.search_levels import (
            Level2Toggle,
            apply_level2_toggles,
        )

        base = {"session_filter": None}
        toggles = [
            Level2Toggle(toggle_type="session", name="s", value="london"),
        ]
        result = apply_level2_toggles(base, toggles)
        assert result["session_filter"] == "london"

    def test_registered_filters_complete(self):
        from novatrade.optimization.search_levels import REGISTERED_FILTERS

        assert len(REGISTERED_FILTERS) >= 10
        for _name, info in REGISTERED_FILTERS.items():
            assert "description" in info
            assert "category" in info
            assert "default_enabled" in info


# ---------------------------------------------------------------------------
# Level 3: Structural templates
# ---------------------------------------------------------------------------


class TestLevel3Templates:
    """Level 3 permits only registered modules and composition patterns."""

    def test_valid_template(self):
        from novatrade.optimization.search_levels import (
            Level3Template,
            validate_level3_template,
        )

        template = Level3Template(composition="irb_trend_adx")
        violations = validate_level3_template(template)
        assert violations == []

    def test_invalid_composition_rejected(self):
        from novatrade.optimization.search_levels import Level3Template

        with pytest.raises(ValueError, match="not registered"):
            Level3Template(composition="freeform_whatever")

    def test_invalid_indicator_override(self):
        from novatrade.optimization.search_levels import (
            Level3Template,
            validate_level3_template,
        )

        template = Level3Template(
            composition="irb_trend_adx",
            indicator_overrides={"quantum_indicator": True},
        )
        violations = validate_level3_template(template)
        assert len(violations) == 1
        assert "quantum_indicator" in violations[0]

    def test_get_active_params(self):
        from novatrade.optimization.search_levels import (
            Level3Template,
            get_active_params_for_template,
        )

        # irb_minimal only uses irb + atr_sizing
        template = Level3Template(composition="irb_minimal")
        params = get_active_params_for_template(template)
        assert "irb_threshold" in params
        assert "trigger_window_bars" in params
        assert "atr_period" in params
        assert "trail_atr_multiplier" in params
        # Should NOT have ema_period (not in minimal)
        assert "ema_period" not in params
        assert "adx_period" not in params

    def test_full_composition_has_all_params(self):
        from novatrade.optimization.search_levels import (
            Level3Template,
            get_active_params_for_template,
        )

        template = Level3Template(composition="irb_full")
        params = get_active_params_for_template(template)
        assert "irb_threshold" in params
        assert "ema_period" in params
        assert "adx_period" in params
        assert "atr_period" in params
        assert "mtf_lookback" in params

    def test_registered_compositions_exist(self):
        from novatrade.optimization.search_levels import REGISTERED_COMPOSITIONS

        assert "irb_trend_adx" in REGISTERED_COMPOSITIONS
        assert "irb_trend_mtf" in REGISTERED_COMPOSITIONS
        assert "irb_minimal" in REGISTERED_COMPOSITIONS
        assert "irb_full" in REGISTERED_COMPOSITIONS

    def test_registered_indicators_exist(self):
        from novatrade.optimization.search_levels import REGISTERED_INDICATORS

        assert "irb" in REGISTERED_INDICATORS
        assert "ema_trend" in REGISTERED_INDICATORS
        assert "adx_filter" in REGISTERED_INDICATORS
        assert "atr_sizing" in REGISTERED_INDICATORS
        assert "mtf_confluence" in REGISTERED_INDICATORS


# ---------------------------------------------------------------------------
# SearchLevelConfig
# ---------------------------------------------------------------------------


class TestSearchLevelConfig:
    """Unified search-level configuration validation."""

    def test_level1_validates_clean(self):
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        cfg = SearchLevelConfig(level=SearchLevel.LEVEL_1)
        assert cfg.validate() == []

    def test_level2_validates_with_valid_toggles(self):
        from novatrade.optimization.search_levels import (
            Level2Toggle,
            SearchLevel,
            SearchLevelConfig,
        )

        cfg = SearchLevelConfig(
            level=SearchLevel.LEVEL_2,
            level2_toggles=[
                Level2Toggle(toggle_type="filter", name="trend_alignment"),
            ],
        )
        assert cfg.validate() == []

    def test_level2_fails_with_bad_toggles(self):
        from novatrade.optimization.search_levels import (
            Level2Toggle,
            SearchLevel,
            SearchLevelConfig,
        )

        cfg = SearchLevelConfig(
            level=SearchLevel.LEVEL_2,
            level2_toggles=[
                Level2Toggle(toggle_type="filter", name="not_real"),
            ],
        )
        violations = cfg.validate()
        assert len(violations) == 1

    def test_level3_requires_template(self):
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        cfg = SearchLevelConfig(level=SearchLevel.LEVEL_3)
        violations = cfg.validate()
        assert len(violations) == 1
        assert "template" in violations[0].lower()

    def test_level3_with_valid_template(self):
        from novatrade.optimization.search_levels import (
            Level3Template,
            SearchLevel,
            SearchLevelConfig,
        )

        cfg = SearchLevelConfig(
            level=SearchLevel.LEVEL_3,
            level3_template=Level3Template(composition="irb_trend_adx"),
        )
        assert cfg.validate() == []

    def test_get_optimisable_params_level1(self):
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.search_levels import SearchLevel, SearchLevelConfig

        cfg = SearchLevelConfig(level=SearchLevel.LEVEL_1)
        params = cfg.get_optimisable_params()
        assert params == list(StrategyConfig.OPTIMIZABLE_PARAMS)

    def test_get_optimisable_params_level3(self):
        from novatrade.optimization.search_levels import (
            Level3Template,
            SearchLevel,
            SearchLevelConfig,
        )

        cfg = SearchLevelConfig(
            level=SearchLevel.LEVEL_3,
            level3_template=Level3Template(composition="irb_minimal"),
        )
        params = cfg.get_optimisable_params()
        assert "irb_threshold" in params
        assert "ema_period" not in params


# ---------------------------------------------------------------------------
# Evaluation Ladder
# ---------------------------------------------------------------------------


class TestEvaluationLadder:
    """Gated evaluation ladder tests."""

    def _make_metrics(self, **overrides):
        """Create a mock metrics object with sensible defaults."""

        class MockMetrics:
            total_completed_trades = 100
            profit_factor = 1.8
            max_drawdown_pct = 5.0
            max_drawdown_pips = 50.0
            max_drawdown_usd = 500.0
            net_result_pips = 200.0
            net_result_usd = 2000.0
            total_bars = 24 * 22 * 12  # ~12 months
            expectancy_r = 0.5
            win_rate = 0.55
            max_consecutive_losses = 5
            top_3_trades_pct_of_profit = 25.0
            average_winner_pips = 30.0
            average_loser_pips = -15.0

        m = MockMetrics()
        for k, v in overrides.items():
            setattr(m, k, v)
        return m

    def test_dashboard_row_ranked(self):
        from novatrade.evaluation.evaluation_ladder import (
            LadderStage,
            compute_dashboard_metrics,
        )

        metrics = self._make_metrics()
        row = compute_dashboard_metrics(metrics, experiment_id="exp-001")
        assert row.experiment_id == "exp-001"
        assert row.ladder_stage == LadderStage.RANKED
        assert row.ranking_score > 0
        assert row.profit_factor == 1.8
        assert row.trade_count == 100
        assert row.win_rate == 0.55

    def test_dashboard_row_gated_low_trades(self):
        from novatrade.evaluation.evaluation_ladder import (
            LadderStage,
            compute_dashboard_metrics,
        )

        metrics = self._make_metrics(total_completed_trades=10)
        row = compute_dashboard_metrics(metrics, experiment_id="exp-002")
        # Ranking score should be 0 with < 30 trades
        assert row.ranking_score == 0.0
        assert row.ladder_stage == LadderStage.GATED_B

    def test_dashboard_row_with_gate_a_failure(self):
        from novatrade.evaluation.evaluation_ladder import (
            LadderStage,
            compute_dashboard_metrics,
        )
        from novatrade.evaluation.gates import GateResult

        metrics = self._make_metrics()
        gate_results = [
            GateResult(gate="A.min_trades", passed=True, reason="ok"),
            GateResult(gate="A.no_crash", passed=False, reason="dd too high"),
        ]
        row = compute_dashboard_metrics(
            metrics,
            experiment_id="exp-003",
            gate_a_results=gate_results,
        )
        assert not row.stage_a_passed
        assert row.ladder_stage == LadderStage.GATED_A
        assert "A.no_crash" in row.stage_a_failures

    def test_multi_metric_columns_populated(self):
        from novatrade.evaluation.evaluation_ladder import compute_dashboard_metrics

        metrics = self._make_metrics()
        row = compute_dashboard_metrics(metrics, experiment_id="exp-004")
        assert row.sharpe_ratio > 0
        assert row.sortino_ratio > 0
        assert row.calmar_ratio > 0
        assert row.recovery_factor > 0
        assert row.trades_per_month > 0
        assert row.avg_win_loss_ratio > 0

    def test_ranking_score_not_optimisation_target(self):
        """Ranking score exists for sorting, verified by checking it's bounded."""
        from novatrade.evaluation.evaluation_ladder import compute_dashboard_metrics

        metrics = self._make_metrics(profit_factor=10.0, net_result_pips=10000.0)
        row = compute_dashboard_metrics(metrics, experiment_id="exp-005")
        # Score is capped at 1.0
        assert row.ranking_score <= 1.0
        assert row.ranking_score >= 0.0

    def test_to_dict_serialisation(self):
        from novatrade.evaluation.evaluation_ladder import compute_dashboard_metrics

        metrics = self._make_metrics()
        row = compute_dashboard_metrics(metrics, experiment_id="exp-006")
        d = row.to_dict()
        assert d["experiment_id"] == "exp-006"
        assert "ladder_stage" in d
        assert "sharpe_ratio" in d
        assert "ranking_score" in d
        assert isinstance(d["sharpe_ratio"], float)


# ---------------------------------------------------------------------------
# Dashboard operations
# ---------------------------------------------------------------------------


class TestEvaluationDashboard:
    """Dashboard leaderboard tests."""

    def _make_row(self, exp_id: str, score: float, stage: str = "ranked"):
        from novatrade.evaluation.evaluation_ladder import DashboardRow, LadderStage

        stage_map = {
            "ranked": LadderStage.RANKED,
            "gated_a": LadderStage.GATED_A,
            "promoted": LadderStage.PROMOTED,
        }
        return DashboardRow(
            experiment_id=exp_id,
            ranking_score=score,
            ladder_stage=stage_map[stage],
        )

    def test_dashboard_ranked_sorted(self):
        from novatrade.evaluation.evaluation_ladder import EvaluationDashboard

        db = EvaluationDashboard()
        db.add(self._make_row("a", 0.5))
        db.add(self._make_row("b", 0.8))
        db.add(self._make_row("c", 0.3))
        ranked = db.ranked
        assert len(ranked) == 3
        assert ranked[0].experiment_id == "b"
        assert ranked[2].experiment_id == "c"

    def test_dashboard_rejected_separated(self):
        from novatrade.evaluation.evaluation_ladder import EvaluationDashboard

        db = EvaluationDashboard()
        db.add(self._make_row("good", 0.8))
        db.add(self._make_row("bad", 0.0, stage="gated_a"))
        assert len(db.ranked) == 1
        assert len(db.rejected) == 1

    def test_dashboard_promote(self):
        from novatrade.evaluation.evaluation_ladder import (
            EvaluationDashboard,
            LadderStage,
        )

        db = EvaluationDashboard()
        db.add(self._make_row("exp-1", 0.7))
        result = db.promote("exp-1", promotion_score=0.65)
        assert result is True
        assert db.rows[0].ladder_stage == LadderStage.PROMOTED
        assert db.rows[0].promotion_score == 0.65

    def test_dashboard_promote_not_found(self):
        from novatrade.evaluation.evaluation_ladder import EvaluationDashboard

        db = EvaluationDashboard()
        result = db.promote("nonexistent", 0.5)
        assert result is False

    def test_export_tsv_format(self):
        from novatrade.evaluation.evaluation_ladder import EvaluationDashboard

        db = EvaluationDashboard()
        db.add(self._make_row("exp-1", 0.7))
        db.add(self._make_row("exp-2", 0.5))
        tsv = db.export_tsv()
        lines = tsv.split("\n")
        assert len(lines) == 3  # header + 2 rows
        assert "experiment_id" in lines[0]
        assert "exp-1" in lines[1]  # higher score first


# ---------------------------------------------------------------------------
# Success criteria
# ---------------------------------------------------------------------------


class TestSuccessCriteria:
    """Revised success criteria per doctrine redlines."""

    def test_all_met(self):
        from novatrade.evaluation.evaluation_ladder import SuccessCriteria

        sc = SuccessCriteria(
            reproducible_from_frozen=True,
            engine_under_target_runtime=True,
            all_gates_passed=True,
            promotion_passed=True,
        )
        assert sc.met is True

    def test_not_met_missing_reproducibility(self):
        from novatrade.evaluation.evaluation_ladder import SuccessCriteria

        sc = SuccessCriteria(
            reproducible_from_frozen=False,
            engine_under_target_runtime=True,
            all_gates_passed=True,
            promotion_passed=True,
        )
        assert sc.met is False

    def test_not_met_missing_gates(self):
        from novatrade.evaluation.evaluation_ladder import SuccessCriteria

        sc = SuccessCriteria(
            reproducible_from_frozen=True,
            engine_under_target_runtime=True,
            all_gates_passed=False,
            promotion_passed=True,
        )
        assert sc.met is False


# ---------------------------------------------------------------------------
# MCP thin-wrapper contract
# ---------------------------------------------------------------------------


class TestMCPThinWrapper:
    """MCP tools must be thin wrappers — no direct storage mutation."""

    def test_mcp_tools_module_imports(self):
        from novatrade.mcp import tools

        assert hasattr(tools, "bt_run")
        assert hasattr(tools, "bt_compare")
        assert hasattr(tools, "bt_leaderboard")
        assert hasattr(tools, "bt_fetch_data")
        assert hasattr(tools, "bt_campaign_start")
        assert hasattr(tools, "bt_campaign_status")
        assert hasattr(tools, "bt_campaign_stop")
        assert hasattr(tools, "bt_walkforward")
        assert hasattr(tools, "bt_promote")

    def test_bt_promote_refuses_mutation(self):
        from novatrade.mcp.tools import bt_promote

        result = bt_promote("exp-123")
        assert "does not mutate" in result.lower() or "CLI" in result

    def test_bt_campaign_status_no_campaigns(self):
        from novatrade.mcp.tools import bt_campaign_status

        result = bt_campaign_status()
        assert "no" in result.lower() or "campaign" in result.lower()


# ---------------------------------------------------------------------------
# Doctrine v2 integrity
# ---------------------------------------------------------------------------


class TestDoctrineV2:
    """Verify doctrine file reflects all redlines."""

    def test_doctrine_file_exists(self):
        from pathlib import Path

        doctrine_path = Path("/home/nova/nova-core/configs/strategies/doctrine.md")
        assert doctrine_path.exists()
        content = doctrine_path.read_text()

        # Key redlines present
        assert "risk_fraction" in content
        assert "separate sizing" in content.lower()
        assert "gated" in content.lower()
        assert "bounded campaign" in content.lower() or "bounded campaigns" in content.lower()
        assert "TSV" in content
        assert "export" in content.lower()
        assert "MCP" in content
        assert "thin wrapper" in content.lower()
        assert "registered" in content.lower()
        assert "structural" in content.lower() or "template" in content.lower()
        assert "reproducib" in content.lower()

    def test_doctrine_excludes_risk_fraction(self):
        from pathlib import Path

        doctrine_path = Path("/home/nova/nova-core/configs/strategies/doctrine.md")
        content = doctrine_path.read_text()
        # Must explicitly exclude risk_fraction from Level 1
        assert "EXCLUDED" in content or "excluded" in content
        assert "risk_fraction" in content

    def test_doctrine_bounded_campaigns(self):
        from pathlib import Path

        doctrine_path = Path("/home/nova/nova-core/configs/strategies/doctrine.md")
        content = doctrine_path.read_text()
        assert "NOT" in content and ("Never Stop" in content or "never stop" in content.lower())

    def test_doctrine_tsv_as_export(self):
        from pathlib import Path

        doctrine_path = Path("/home/nova/nova-core/configs/strategies/doctrine.md")
        content = doctrine_path.read_text()
        assert "TSV is an EXPORT" in content or "TSV is an export" in content


# ---------------------------------------------------------------------------
# Integration: config_schema already excludes risk_fraction
# ---------------------------------------------------------------------------


class TestConfigSchemaExclusion:
    """Verify StrategyConfig does not include risk_fraction."""

    def test_risk_fraction_not_in_optimizable(self):
        from novatrade.cli.config_schema import StrategyConfig

        assert "risk_fraction" not in StrategyConfig.OPTIMIZABLE_PARAMS

    def test_risk_fraction_not_in_bounds(self):
        from novatrade.cli.config_schema import StrategyConfig

        assert "risk_fraction" not in StrategyConfig.PARAMETER_BOUNDS

    def test_risk_fraction_not_in_env_kwargs(self):
        from novatrade.cli.config_schema import StrategyConfig

        config = StrategyConfig()
        kwargs = config.to_environment_kwargs()
        assert "risk_fraction" not in kwargs

    def test_config_schema_docstring_mentions_separation(self):
        from novatrade.cli.config_schema import StrategyConfig

        assert (
            "risk_fraction" in (StrategyConfig.__doc__ or "").lower()
            or "sizing" in (StrategyConfig.__doc__ or "").lower()
        )


# ---------------------------------------------------------------------------
# H2: Overfit detection fires when IS >> OOS
# ---------------------------------------------------------------------------


class TestOverfitDetection:
    """Bug H2: compute_promotion_score overfit penalty must fire when IS >> OOS.

    The is_median parameter must come from true IS scores, not OOS scores.
    When IS >> OOS (overfitting), the oos_is_ratio (OOS/IS) should be low,
    triggering the penalty.
    """

    def test_overfit_penalty_fires_when_is_much_greater_than_oos(self):
        """When IS median is 0.8 and OOS scores are ~0.2, ratio = 0.25 < 0.5 => severe penalty."""
        from novatrade.evaluation.fitness import compute_promotion_score

        oos_scores = [0.20, 0.22, 0.18, 0.21]
        holdout_score = 0.19
        is_median = 0.80  # IS >> OOS => overfitting
        complexity = 5

        score_overfit = compute_promotion_score(
            oos_scores=oos_scores,
            holdout_score=holdout_score,
            is_median=is_median,
            complexity=complexity,
        )

        # Same OOS but with IS close to OOS (no overfitting)
        score_healthy = compute_promotion_score(
            oos_scores=oos_scores,
            holdout_score=holdout_score,
            is_median=0.22,  # IS ~ OOS
            complexity=complexity,
        )

        # Overfit score should be penalized (lower) vs healthy
        assert score_overfit < score_healthy

    def test_overfit_severe_penalty_threshold(self):
        """oos_is_ratio < 0.5 should apply 0.15 penalty (severe degradation)."""
        from novatrade.evaluation.fitness import compute_promotion_score

        oos_scores = [0.30, 0.30, 0.30]
        holdout_score = 0.30
        is_median = 0.80  # ratio = 0.30/0.80 = 0.375 < 0.5
        complexity = 5

        # Without penalty (is_median = 0, bypasses check)
        score_no_check = compute_promotion_score(
            oos_scores=oos_scores,
            holdout_score=holdout_score,
            is_median=0.0,
            complexity=complexity,
        )

        score_with_penalty = compute_promotion_score(
            oos_scores=oos_scores,
            holdout_score=holdout_score,
            is_median=is_median,
            complexity=complexity,
        )

        # Penalty should be 0.15
        assert score_no_check - score_with_penalty == pytest.approx(0.15, abs=0.01)

    def test_overfit_moderate_penalty_threshold(self):
        """oos_is_ratio between 0.5 and 0.7 should apply 0.08 penalty."""
        from novatrade.evaluation.fitness import compute_promotion_score

        oos_scores = [0.50, 0.50, 0.50]
        holdout_score = 0.50
        is_median = 0.80  # ratio = 0.50/0.80 = 0.625, between 0.5 and 0.7
        complexity = 5

        score_no_check = compute_promotion_score(
            oos_scores=oos_scores,
            holdout_score=holdout_score,
            is_median=0.0,
            complexity=complexity,
        )

        score_with_penalty = compute_promotion_score(
            oos_scores=oos_scores,
            holdout_score=holdout_score,
            is_median=is_median,
            complexity=complexity,
        )

        # Penalty should be 0.08
        assert score_no_check - score_with_penalty == pytest.approx(0.08, abs=0.01)

    def test_no_penalty_when_oos_close_to_is(self):
        """oos_is_ratio >= 0.7 should have no penalty."""
        from novatrade.evaluation.fitness import compute_promotion_score

        oos_scores = [0.60, 0.60, 0.60]
        holdout_score = 0.60
        is_median = 0.70  # ratio = 0.60/0.70 = 0.857 >= 0.7
        complexity = 5

        score_no_check = compute_promotion_score(
            oos_scores=oos_scores,
            holdout_score=holdout_score,
            is_median=0.0,
            complexity=complexity,
        )

        score_with_is = compute_promotion_score(
            oos_scores=oos_scores,
            holdout_score=holdout_score,
            is_median=is_median,
            complexity=complexity,
        )

        # No overfit penalty
        assert score_no_check == pytest.approx(score_with_is, abs=0.01)

    def test_promotion_pipeline_uses_true_is_median(self):
        """Verify that promotion.py passes wf_result.median_is_score
        (not median_oos_score) to compute_promotion_score."""
        import inspect

        from novatrade.optimization.promotion import run_promotion_pipeline

        source = inspect.getsource(run_promotion_pipeline)
        # Must use median_is_score, not median_oos_score as IS proxy
        assert "median_is_score" in source
        # Should NOT use median_oos_score as IS
        assert "median_oos_score" not in source.split("is_median =")[1].split("\n")[0]
