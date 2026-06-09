"""Tests for utils/cost_router.py — Phase 4.4 Cost-Aware Scheduling."""

import json
from datetime import datetime, timezone
from unittest import mock

import pytest

from utils.cost_router import (
    DAILY_COST_ALERT_USD,
    MODEL_TIERS,
    HeartbeatConfig,
    RoutingDecision,
    TaskComplexity,
    check_budget_alerts,
    compute_heartbeat_config,
    get_langfuse_daily_cost,
    get_rolling_average_cost,
    read_heartbeat_config,
    route_task,
    score_complexity,
    write_heartbeat_config,
)

# ---------------------------------------------------------------------------
# Complexity scoring tests
# ---------------------------------------------------------------------------


class TestScoreComplexity:
    """Test task complexity scoring across different inputs."""

    def test_simple_class_returns_trivial(self):
        result = score_complexity("Fix the typo in the README", task_class="simple")
        assert result == TaskComplexity.TRIVIAL

    def test_code_impl_class_returns_complex(self):
        result = score_complexity("Implement the feature", task_class="code_impl")
        assert result == TaskComplexity.COMPLEX

    def test_research_class_returns_moderate(self):
        result = score_complexity("Research async patterns", task_class="research")
        assert result == TaskComplexity.MODERATE

    def test_unknown_class_returns_moderate(self):
        result = score_complexity("Do something", task_class="unknown")
        assert result == TaskComplexity.MODERATE

    def test_multiple_simple_signals_force_trivial(self):
        text = "Quick format fix: rename the typo variable"
        result = score_complexity(text, task_class="code_impl")
        assert result == TaskComplexity.TRIVIAL

    def test_multiple_complex_signals_force_complex(self):
        text = "Comprehensive multi-phase refactor of the pipeline architecture"
        result = score_complexity(text, task_class="research")
        assert result == TaskComplexity.COMPLEX

    def test_long_text_with_complex_signal(self):
        text = "Implement a comprehensive multi-step pipeline refactor. " + "word " * 1000
        result = score_complexity(text, task_class="code_review")
        assert result == TaskComplexity.COMPLEX

    def test_critical_priority_never_below_moderate(self):
        result = score_complexity("Fix typo", task_class="simple", priority="critical")
        assert result >= TaskComplexity.MODERATE

    def test_short_text_with_simple_signal_caps_at_simple(self):
        text = "Quick fix the name"
        result = score_complexity(text, task_class="code_impl")
        assert result <= TaskComplexity.SIMPLE

    def test_retry_signal_detected(self):
        text = "---\npriority: normal\n---\n# Retry: __retry1 contract repair"
        result = score_complexity(text, task_class="simple")
        assert result == TaskComplexity.TRIVIAL

    def test_empty_text_returns_class_default(self):
        result = score_complexity("", task_class="code_review")
        assert result == TaskComplexity.MODERATE

    def test_system_class_returns_complex(self):
        result = score_complexity("Deploy the service", task_class="system")
        assert result == TaskComplexity.COMPLEX


# ---------------------------------------------------------------------------
# Model routing tests
# ---------------------------------------------------------------------------


class TestRouteTask:
    """Test model routing decisions under various conditions."""

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=0.0)
    @mock.patch("utils.cost_router.get_langfuse_daily_cost", return_value=None)
    def test_trivial_task_routes_to_sonnet(self, _lf, _budget):
        result = route_task("Fix typo", task_class="simple", priority="normal")
        assert result.model == MODEL_TIERS["sonnet"]
        assert result.complexity == TaskComplexity.TRIVIAL
        assert not result.downgraded

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=0.0)
    @mock.patch("utils.cost_router.get_langfuse_daily_cost", return_value=None)
    def test_complex_task_routes_to_opus(self, _lf, _budget):
        result = route_task(
            "Implement comprehensive multi-phase architecture refactor",
            task_class="code_impl",
            priority="normal",
        )
        assert result.model == MODEL_TIERS["opus"]
        assert result.complexity == TaskComplexity.COMPLEX

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=96.0)
    @mock.patch("utils.cost_router.get_langfuse_daily_cost", return_value=None)
    def test_budget_downgrade_at_95pct(self, _lf, _budget):
        """At 96% budget, normal-priority opus tasks downgrade to sonnet."""
        result = route_task(
            "Research the topic thoroughly",
            task_class="research",
            priority="normal",
        )
        assert result.model == MODEL_TIERS["sonnet"]
        assert result.downgraded

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=96.0)
    @mock.patch("utils.cost_router.get_langfuse_daily_cost", return_value=None)
    def test_high_priority_not_downgraded_at_95pct(self, _lf, _budget):
        """High priority tasks are NOT downgraded at 95% budget."""
        result = route_task(
            "Research the topic thoroughly",
            task_class="research",
            priority="high",
        )
        assert result.model == MODEL_TIERS["opus"]
        assert not result.downgraded

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=99.0)
    @mock.patch("utils.cost_router.get_langfuse_daily_cost", return_value=None)
    def test_budget_critical_at_98pct(self, _lf, _budget):
        """At 99% budget, non-critical tasks downgrade to haiku."""
        result = route_task(
            "Research the topic",
            task_class="research",
            priority="high",
        )
        assert result.model == MODEL_TIERS["haiku"]
        assert result.downgraded

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=99.0)
    @mock.patch("utils.cost_router.get_langfuse_daily_cost", return_value=None)
    def test_critical_priority_overrides_budget_limit(self, _lf, _budget):
        """CRITICAL priority always gets opus, even at 99% budget."""
        result = route_task(
            "Research the topic",
            task_class="research",
            priority="critical",
        )
        assert result.model == MODEL_TIERS["opus"]
        assert not result.downgraded

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=0.0)
    @mock.patch("utils.cost_router.get_langfuse_daily_cost", return_value=None)
    def test_retry_task_downgraded_to_sonnet(self, _lf, _budget):
        """Retry/contract repair tasks prefer sonnet over opus."""
        result = route_task(
            "# Retry: __retry1 contract repair for 0042_test. Implement the full integration pipeline end-to-end.",
            task_class="code_impl",
            priority="normal",
        )
        assert result.model == MODEL_TIERS["sonnet"]
        assert result.downgraded

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=0.0)
    @mock.patch("utils.cost_router.get_langfuse_daily_cost", return_value=None)
    def test_routing_reasons_are_populated(self, _lf, _budget):
        result = route_task("Do something", task_class="unknown", priority="normal")
        assert len(result.reasons) > 0
        # Should contain complexity, budget, langfuse status, base_model
        reason_text = " ".join(result.reasons)
        assert "complexity=" in reason_text
        assert "budget_utilization=" in reason_text

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=0.0)
    @mock.patch("utils.cost_router.get_langfuse_daily_cost", return_value=None)
    def test_estimated_tokens_populated(self, _lf, _budget):
        text = "x" * 400
        result = route_task(text, task_class="unknown")
        assert result.estimated_tokens == 100  # 400 chars / 4

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=90.0)
    @mock.patch("utils.cost_router.get_langfuse_daily_cost", return_value=None)
    def test_budget_warn_logged_but_no_downgrade(self, _lf, _budget):
        """At 90% budget (above 85% warn), warn is logged but no model change."""
        result = route_task(
            "Research the topic thoroughly",
            task_class="research",
            priority="normal",
        )
        # Should still be opus for moderate complexity
        assert result.model == MODEL_TIERS["opus"]
        assert not result.downgraded
        reason_text = " ".join(result.reasons)
        assert "BUDGET_WARN" in reason_text

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=0.0)
    @mock.patch("utils.cost_router.get_langfuse_daily_cost", return_value=None)
    def test_cache_eligible_noted_in_reasons(self, _lf, _budget):
        result = route_task("Do something", cache_eligible=True)
        reason_text = " ".join(result.reasons)
        assert "cache_eligible=true" in reason_text


# ---------------------------------------------------------------------------
# Budget alert tests
# ---------------------------------------------------------------------------


class TestBudgetAlerts:
    """Test budget tracking and alerts."""

    @mock.patch("utils.cost_router.get_rolling_average_cost", return_value=5.0)
    def test_alerts_on_high_daily_cost(self, _avg):
        mock_budget = mock.MagicMock()
        mock_budget.check_alerts.return_value = []
        mock_budget.get_cost_summary.return_value = {
            "daily_cost_usd": DAILY_COST_ALERT_USD + 5.0,
            "monthly_cost_usd": 50.0,
            "monthly_pct": 50.0,
        }

        with (
            mock.patch("utils.cost_router.budget", mock_budget, create=True),
            mock.patch.dict(
                "sys.modules",
                {"agents.budget_enforcer": mock.MagicMock(budget=mock_budget)},
            ),
        ):
            alerts = check_budget_alerts()
            assert any("daily cost" in a.lower() for a in alerts)

    @mock.patch("utils.cost_router.get_rolling_average_cost", return_value=5.0)
    def test_alerts_on_2x_average(self, _avg):
        mock_budget = mock.MagicMock()
        mock_budget.check_alerts.return_value = []
        mock_budget.get_cost_summary.return_value = {
            "daily_cost_usd": 15.0,  # 3x the average of 5.0
            "monthly_cost_usd": 50.0,
            "monthly_pct": 50.0,
        }

        with mock.patch.dict(
            "sys.modules",
            {"agents.budget_enforcer": mock.MagicMock(budget=mock_budget)},
        ):
            alerts = check_budget_alerts()
            assert any("7-day average" in a for a in alerts)

    def test_no_alerts_when_budget_unavailable(self):
        """Should return empty list when budget_enforcer import fails."""
        with mock.patch.dict("sys.modules", {"agents.budget_enforcer": None}):
            alerts = check_budget_alerts()
            assert alerts == []


# ---------------------------------------------------------------------------
# Rolling average cost tests
# ---------------------------------------------------------------------------


class TestRollingAverageCost:
    """Test rolling average calculation from budget files."""

    def test_rolling_average_from_files(self, tmp_path):
        """Test that rolling average reads from usage files."""
        # Create fake usage files
        today = datetime.now(timezone.utc).date()
        for i in range(3):
            from datetime import timedelta

            day = today - timedelta(days=i)
            usage = {"cost_usd": 10.0 * (i + 1)}
            (tmp_path / f"usage_{day.isoformat()}.json").write_text(json.dumps(usage))

        with mock.patch("utils.cost_router.BUDGETS_DIR", tmp_path):
            avg = get_rolling_average_cost(days=7)
            # 3 days of data: 10+20+30 = 60, avg = 20
            assert avg == pytest.approx(20.0)

    def test_rolling_average_no_data(self, tmp_path):
        with mock.patch("utils.cost_router.BUDGETS_DIR", tmp_path):
            avg = get_rolling_average_cost(days=7)
            assert avg == 0.0

    def test_rolling_average_corrupt_file(self, tmp_path):
        today = datetime.now(timezone.utc).date()
        (tmp_path / f"usage_{today.isoformat()}.json").write_text("not json")

        with mock.patch("utils.cost_router.BUDGETS_DIR", tmp_path):
            avg = get_rolling_average_cost(days=7)
            assert avg == 0.0


# ---------------------------------------------------------------------------
# Langfuse degradation tests
# ---------------------------------------------------------------------------


class TestLangfuseDegradation:
    """Test that Langfuse integration degrades safely."""

    def test_langfuse_returns_none_when_unavailable(self):
        """get_langfuse_daily_cost degrades safely when langfuse is missing."""
        with mock.patch.dict("sys.modules", {"utils.langfuse_tracing": None}):
            result = get_langfuse_daily_cost()
            assert result is None

    def test_langfuse_returns_none_when_not_configured(self):
        """get_langfuse_daily_cost returns None (v1 always returns None)."""
        result = get_langfuse_daily_cost()
        assert result is None

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=0.0)
    @mock.patch("utils.cost_router.get_langfuse_daily_cost", return_value=None)
    def test_routing_works_without_langfuse(self, _lf, _budget):
        """Routing should work normally when Langfuse is unavailable."""
        result = route_task("Fix the bug", task_class="code_impl")
        assert result.model is not None
        reason_text = " ".join(result.reasons)
        assert "langfuse=unavailable" in reason_text


# ---------------------------------------------------------------------------
# Adaptive heartbeat interval tests
# ---------------------------------------------------------------------------


class TestAdaptiveHeartbeat:
    """Test adaptive heartbeat interval computation."""

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=0.0)
    @mock.patch(
        "utils.cost_router.get_cost_summary",
        return_value={
            "daily_cost_usd": 0,
            "monthly_cost_usd": 0,
            "monthly_pct": 0,
            "monthly_cap_usd": 100,
        },
    )
    def test_idle_mode_when_no_tasks(self, _cost, _budget, tmp_path):
        """Empty TASKS/ → idle mode with 30-min interval."""
        tasks = tmp_path / "TASKS"
        tasks.mkdir()
        config = compute_heartbeat_config(tasks_dir=tasks)
        assert config.mode == "idle"
        assert config.interval_minutes == 30
        assert not config.skip_research
        assert not config.skip_planning

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=10.0)
    @mock.patch(
        "utils.cost_router.get_cost_summary",
        return_value={
            "daily_cost_usd": 5,
            "monthly_cost_usd": 20,
            "monthly_pct": 20,
            "monthly_cap_usd": 100,
        },
    )
    def test_active_mode_with_pending_tasks(self, _cost, _budget, tmp_path):
        """Pending task files → active mode with 5-min interval."""
        tasks = tmp_path / "TASKS"
        tasks.mkdir()
        (tasks / "0001_test.md").write_text("test task")
        config = compute_heartbeat_config(tasks_dir=tasks)
        assert config.mode == "active"
        assert config.interval_minutes == 5
        assert not config.skip_research

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=10.0)
    @mock.patch(
        "utils.cost_router.get_cost_summary",
        return_value={
            "daily_cost_usd": 5,
            "monthly_cost_usd": 20,
            "monthly_pct": 20,
            "monthly_cap_usd": 100,
        },
    )
    def test_active_mode_with_inprogress_tasks(self, _cost, _budget, tmp_path):
        """In-progress task files → active mode."""
        tasks = tmp_path / "TASKS"
        tasks.mkdir()
        (tasks / "0001_test.md.inprogress").write_text("running")
        config = compute_heartbeat_config(tasks_dir=tasks)
        assert config.mode == "active"
        assert config.interval_minutes == 5

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=96.0)
    @mock.patch(
        "utils.cost_router.get_cost_summary",
        return_value={
            "daily_cost_usd": 15,
            "monthly_cost_usd": 96,
            "monthly_pct": 96,
            "monthly_cap_usd": 100,
        },
    )
    def test_budget_constrained_skips_cycles(self, _cost, _budget, tmp_path):
        """Monthly budget > 95% → budget_constrained, skip research/planning."""
        tasks = tmp_path / "TASKS"
        tasks.mkdir()
        (tasks / "0001_test.md").write_text("test task")
        config = compute_heartbeat_config(tasks_dir=tasks)
        assert config.mode == "budget_constrained"
        assert config.skip_research
        assert config.skip_planning
        assert config.interval_minutes == 30

    @mock.patch("utils.cost_router.get_budget_utilization", return_value=96.0)
    @mock.patch(
        "utils.cost_router.get_cost_summary",
        return_value={
            "daily_cost_usd": 5,
            "monthly_cost_usd": 30,
            "monthly_pct": 30,
            "monthly_cap_usd": 100,
        },
    )
    def test_daily_budget_constraint_triggers(self, _cost, _budget, tmp_path):
        """Daily budget > 95% (but monthly ok) → budget_constrained."""
        tasks = tmp_path / "TASKS"
        tasks.mkdir()
        config = compute_heartbeat_config(tasks_dir=tasks)
        assert config.mode == "budget_constrained"
        assert config.skip_research

    def test_nonexistent_tasks_dir_is_idle(self, tmp_path):
        """Non-existent TASKS/ dir → idle mode."""
        with (
            mock.patch("utils.cost_router.get_budget_utilization", return_value=0.0),
            mock.patch(
                "utils.cost_router.get_cost_summary",
                return_value={
                    "daily_cost_usd": 0,
                    "monthly_cost_usd": 0,
                    "monthly_pct": 0,
                    "monthly_cap_usd": 100,
                },
            ),
        ):
            config = compute_heartbeat_config(tasks_dir=tmp_path / "nonexistent")
            assert config.mode == "idle"


# ---------------------------------------------------------------------------
# Heartbeat config persistence tests
# ---------------------------------------------------------------------------


class TestHeartbeatConfigPersistence:
    """Test write/read of heartbeat config file."""

    def test_write_and_read_config(self, tmp_path):
        config_file = tmp_path / "heartbeat_config.json"
        config = HeartbeatConfig(
            mode="active",
            interval_minutes=5,
            skip_research=False,
            skip_planning=False,
            reason="tasks active (budget=10.0%)",
        )

        with mock.patch("utils.cost_router.HEARTBEAT_CONFIG_FILE", config_file):
            write_heartbeat_config(config)
            loaded = read_heartbeat_config()

        assert loaded is not None
        assert loaded.mode == "active"
        assert loaded.interval_minutes == 5
        assert not loaded.skip_research
        assert loaded.reason == "tasks active (budget=10.0%)"

    def test_read_missing_config_returns_none(self, tmp_path):
        config_file = tmp_path / "nonexistent.json"
        with mock.patch("utils.cost_router.HEARTBEAT_CONFIG_FILE", config_file):
            loaded = read_heartbeat_config()
        assert loaded is None

    def test_read_corrupt_config_returns_none(self, tmp_path):
        config_file = tmp_path / "heartbeat_config.json"
        config_file.write_text("not valid json")
        with mock.patch("utils.cost_router.HEARTBEAT_CONFIG_FILE", config_file):
            loaded = read_heartbeat_config()
        assert loaded is None

    def test_config_file_contains_timestamp(self, tmp_path):
        config_file = tmp_path / "heartbeat_config.json"
        config = HeartbeatConfig(
            mode="idle",
            interval_minutes=30,
            skip_research=False,
            skip_planning=False,
            reason="test",
        )
        with mock.patch("utils.cost_router.HEARTBEAT_CONFIG_FILE", config_file):
            write_heartbeat_config(config)
            data = json.loads(config_file.read_text())
        assert "updated_at" in data
        assert "mode" in data


# ---------------------------------------------------------------------------
# RoutingDecision dataclass tests
# ---------------------------------------------------------------------------


class TestRoutingDecision:
    """Test the RoutingDecision dataclass."""

    def test_default_values(self):
        rd = RoutingDecision(
            model="claude-opus-4-8",
            complexity=TaskComplexity.MODERATE,
        )
        assert rd.reasons == []
        assert rd.estimated_tokens == 0
        assert rd.budget_utilization_pct == 0.0
        assert not rd.downgraded

    def test_all_fields_populated(self):
        rd = RoutingDecision(
            model="claude-sonnet-4-20250514",
            complexity=TaskComplexity.SIMPLE,
            reasons=["reason1", "reason2"],
            estimated_tokens=500,
            budget_utilization_pct=45.0,
            downgraded=True,
        )
        assert rd.model == "claude-sonnet-4-20250514"
        assert rd.complexity == TaskComplexity.SIMPLE
        assert len(rd.reasons) == 2
        assert rd.estimated_tokens == 500
        assert rd.budget_utilization_pct == 45.0
        assert rd.downgraded
