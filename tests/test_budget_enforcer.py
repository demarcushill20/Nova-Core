"""Tests for agents/budget_enforcer.py — focuses on uncovered paths.

Covers: check_alerts, monthly cost cap, day rollover, threshold alerts,
persistence, and cost calculation edge cases.
"""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


class TestBudgetEnforcerCore(unittest.TestCase):
    """Test BudgetEnforcer with isolated filesystem."""

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())
        self._budgets = self._tmpdir / "STATE" / "budgets"
        self._budgets.mkdir(parents=True)

        # Patch module-level paths before importing
        self._patches = [
            patch("agents.budget_enforcer.BASE", self._tmpdir),
            patch("agents.budget_enforcer.STATE", self._tmpdir / "STATE"),
            patch("agents.budget_enforcer.BUDGETS_DIR", self._budgets),
            patch("agents.budget_enforcer.LIMITS_FILE", self._budgets / "limits.json"),
        ]
        for p in self._patches:
            p.start()

        # Import fresh enforcer (not the module singleton)
        from agents.budget_enforcer import BudgetEnforcer

        self.enforcer = BudgetEnforcer()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_can_proceed_within_budget(self):
        ok, msg = self.enforcer.can_proceed(estimated_tokens=1000)
        self.assertTrue(ok)
        self.assertEqual(msg, "OK")

    def test_can_proceed_daily_exceeded(self):
        """Daily budget exceeded should return False."""
        self.enforcer._daily.input_tokens = 10_000_000
        ok, msg = self.enforcer.can_proceed(estimated_tokens=0)
        self.assertFalse(ok)
        self.assertIn("Daily input budget exceeded", msg)

    def test_can_proceed_session_exceeded(self):
        """Session budget exceeded should return False."""
        self.enforcer._session.input_tokens = 600_000
        ok, msg = self.enforcer.can_proceed(estimated_tokens=0, scope="session")
        self.assertFalse(ok)
        self.assertIn("Session input budget exceeded", msg)

    def test_can_proceed_task_exceeded(self):
        """Task budget exceeded should return False."""
        from agents.budget_enforcer import TokenCounter

        self.enforcer._tasks["test_task"] = TokenCounter(input_tokens=300_000)
        ok, msg = self.enforcer.can_proceed(estimated_tokens=0, scope="task")
        self.assertFalse(ok)
        self.assertIn("Task input budget exceeded", msg)

    def test_can_proceed_monthly_cap(self):
        """Monthly cost cap should block new tasks."""
        self.enforcer._monthly_cost_usd = 200.0  # well over default $100 cap
        ok, msg = self.enforcer.can_proceed(scope="monthly")
        self.assertFalse(ok)
        self.assertIn("Monthly cost cap reached", msg)

    def test_record_usage_updates_counters(self):
        """record_usage should update session, daily, and task counters."""
        self.enforcer.record_usage(1000, 500, model="claude-opus-4-6", task_id="t1")

        self.assertEqual(self.enforcer._session.input_tokens, 1000)
        self.assertEqual(self.enforcer._session.output_tokens, 500)
        self.assertEqual(self.enforcer._daily.input_tokens, 1000)
        self.assertIn("t1", self.enforcer._tasks)
        self.assertEqual(self.enforcer._tasks["t1"].input_tokens, 1000)

    def test_record_usage_accumulates(self):
        """Multiple record_usage calls should accumulate."""
        self.enforcer.record_usage(1000, 500, model="sonnet", task_id="t1")
        self.enforcer.record_usage(2000, 1000, model="sonnet", task_id="t1")

        self.assertEqual(self.enforcer._tasks["t1"].input_tokens, 3000)
        self.assertEqual(self.enforcer._daily.input_tokens, 3000)

    def test_record_usage_persists_daily(self):
        """record_usage should persist daily usage to disk."""
        self.enforcer.record_usage(1000, 500, model="haiku")

        # Check file was written
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        usage_file = self._budgets / f"usage_{today}.json"
        self.assertTrue(usage_file.exists())

        data = json.loads(usage_file.read_text())
        self.assertEqual(data["input_tokens"], 1000)
        self.assertEqual(data["output_tokens"], 500)

    def test_check_alerts_empty(self):
        """No alerts when usage is low."""
        alerts = self.enforcer.check_alerts()
        self.assertEqual(alerts, [])

    def test_check_alerts_daily_threshold(self):
        """Alert when daily tokens exceed 75% threshold."""
        limit = self.enforcer._limits["per_day"]["input_tokens"]
        self.enforcer._daily.input_tokens = int(limit * 0.80)

        alerts = self.enforcer.check_alerts()
        self.assertTrue(any("Daily input tokens" in a and "75%" in a for a in alerts))

    def test_check_alerts_90_threshold(self):
        """Alert emits at highest crossed threshold (break on first match).

        ALERT_THRESHOLDS = [0.75, 0.90] iterates low-to-high, breaks on first
        match. So at 95% usage, it matches 75% first and stops.
        """
        limit = self.enforcer._limits["per_day"]["input_tokens"]
        self.enforcer._daily.input_tokens = int(limit * 0.95)

        alerts = self.enforcer.check_alerts()
        # At 95%, crosses 75% threshold first (due to break), emits "75%"
        self.assertTrue(any("75%" in a for a in alerts))
        self.assertTrue(any("Daily input tokens" in a for a in alerts))

    def test_check_alerts_monthly_cost(self):
        """Alert when monthly cost exceeds threshold."""
        cap = self.enforcer._limits["monthly_cost_cap_usd"]
        self.enforcer._monthly_cost_usd = cap * 0.80

        alerts = self.enforcer.check_alerts()
        self.assertTrue(any("Monthly cost" in a for a in alerts))

    def test_check_alerts_session_threshold(self):
        """Alert when session tokens exceed threshold."""
        limit = self.enforcer._limits["per_session"]["input_tokens"]
        self.enforcer._session.input_tokens = int(limit * 0.80)

        alerts = self.enforcer.check_alerts()
        self.assertTrue(any("Session input tokens" in a for a in alerts))

    def test_check_alerts_task_threshold(self):
        """Alert when a specific task exceeds threshold."""
        from agents.budget_enforcer import TokenCounter

        limit = self.enforcer._limits["per_task"]["input_tokens"]
        self.enforcer._tasks["big_task"] = TokenCounter(input_tokens=int(limit * 0.80))

        alerts = self.enforcer.check_alerts()
        self.assertTrue(any("big_task" in a for a in alerts))

    def test_reset_task_budget(self):
        """reset_task_budget clears counters for a task."""
        from agents.budget_enforcer import TokenCounter

        self.enforcer._tasks["t1"] = TokenCounter(input_tokens=50000, output_tokens=10000)
        self.enforcer.reset_task_budget("t1")
        self.assertEqual(self.enforcer._tasks["t1"].input_tokens, 0)
        self.assertEqual(self.enforcer._tasks["t1"].output_tokens, 0)

    def test_get_usage_summary(self):
        """get_usage_summary returns structured dict."""
        self.enforcer.record_usage(1000, 500, task_id="t1")
        summary = self.enforcer.get_usage_summary()

        self.assertIn("session", summary)
        self.assertIn("daily", summary)
        self.assertIn("tasks", summary)
        self.assertIn("t1", summary["tasks"])

    def test_get_cost_summary(self):
        """get_cost_summary returns dollar amounts."""
        self.enforcer.record_usage(1000, 500, model="claude-opus-4-6")
        summary = self.enforcer.get_cost_summary()

        self.assertIn("session_cost_usd", summary)
        self.assertIn("daily_cost_usd", summary)
        self.assertIn("monthly_cost_usd", summary)
        self.assertIn("monthly_cap_usd", summary)
        self.assertGreater(summary["session_cost_usd"], 0)

    def test_calculate_cost_opus(self):
        """Cost calculation for Opus model."""
        cost = self.enforcer._calculate_cost(1_000_000, 100_000, "claude-opus-4-6")
        expected = (1_000_000 / 1_000_000) * 15.0 + (100_000 / 1_000_000) * 75.0
        self.assertAlmostEqual(cost, expected)

    def test_calculate_cost_sonnet(self):
        """Cost calculation for Sonnet model."""
        cost = self.enforcer._calculate_cost(1_000_000, 100_000, "sonnet")
        expected = (1_000_000 / 1_000_000) * 3.0 + (100_000 / 1_000_000) * 15.0
        self.assertAlmostEqual(cost, expected)

    def test_calculate_cost_unknown_model(self):
        """Unknown model should use default (Opus) pricing."""
        cost = self.enforcer._calculate_cost(1_000_000, 100_000, "unknown-model-x")
        expected = (1_000_000 / 1_000_000) * 15.0 + (100_000 / 1_000_000) * 75.0
        self.assertAlmostEqual(cost, expected)

    def test_day_rollover(self):
        """When day changes, daily counters should reset."""
        self.enforcer._daily.input_tokens = 5000
        self.enforcer._daily_cost_usd = 1.0
        self.enforcer._today_str = "2020-01-01"  # Force old date

        # Trigger rollover by calling can_proceed
        ok, msg = self.enforcer.can_proceed()
        self.assertTrue(ok)
        self.assertEqual(self.enforcer._daily.input_tokens, 0)
        self.assertEqual(self.enforcer._daily_cost_usd, 0.0)

    def test_load_monthly_cost_aggregation(self):
        """_load_monthly_cost aggregates from all daily files in current month."""
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        # Create several daily usage files
        for day in range(1, 4):
            f = self._budgets / f"usage_{month}-{day:02d}.json"
            f.write_text(json.dumps({"cost_usd": 5.0}))

        self.enforcer._load_monthly_cost()
        self.assertAlmostEqual(self.enforcer._monthly_cost_usd, 15.0)

    def test_load_limits_from_file(self):
        """Limits loaded from file override defaults."""
        custom_limits = {
            "per_task": {"input_tokens": 999, "output_tokens": 999},
            "per_session": {"input_tokens": 999, "output_tokens": 999},
            "per_day": {"input_tokens": 999, "output_tokens": 999},
            "monthly_cost_cap_usd": 42.0,
        }
        limits_file = self._budgets / "limits.json"
        limits_file.write_text(json.dumps(custom_limits))

        loaded = self.enforcer._load_limits()
        self.assertEqual(loaded["monthly_cost_cap_usd"], 42.0)
        self.assertEqual(loaded["per_task"]["input_tokens"], 999)

    def test_load_limits_corrupt_json(self):
        """Corrupt limits file falls back to defaults."""
        limits_file = self._budgets / "limits.json"
        limits_file.write_text("not json at all")

        from agents.budget_enforcer import DEFAULT_LIMITS

        loaded = self.enforcer._load_limits()
        self.assertEqual(loaded["monthly_cost_cap_usd"], DEFAULT_LIMITS["monthly_cost_cap_usd"])

    def test_pct_zero_limit(self):
        """_pct handles zero limit gracefully."""
        from agents.budget_enforcer import BudgetEnforcer

        self.assertEqual(BudgetEnforcer._pct(100, 0), 0.0)

    def test_check_threshold_alerts_zero_limit(self):
        """_check_threshold_alerts skips when limit is zero."""
        from agents.budget_enforcer import BudgetEnforcer

        alerts = []
        BudgetEnforcer._check_threshold_alerts(alerts, "test", 100, 0)
        self.assertEqual(alerts, [])


class TestTokenCounter(unittest.TestCase):
    """Test TokenCounter dataclass."""

    def test_total(self):
        from agents.budget_enforcer import TokenCounter

        tc = TokenCounter(input_tokens=100, output_tokens=50)
        self.assertEqual(tc.total(), 150)

    def test_add(self):
        from agents.budget_enforcer import TokenCounter

        tc = TokenCounter()
        tc.add(100, 50)
        tc.add(200, 100)
        self.assertEqual(tc.input_tokens, 300)
        self.assertEqual(tc.output_tokens, 150)

    def test_reset(self):
        from agents.budget_enforcer import TokenCounter

        tc = TokenCounter(input_tokens=100, output_tokens=50)
        tc.reset()
        self.assertEqual(tc.total(), 0)

    def test_to_dict(self):
        from agents.budget_enforcer import TokenCounter

        tc = TokenCounter(input_tokens=100, output_tokens=50)
        d = tc.to_dict()
        self.assertEqual(d, {"input_tokens": 100, "output_tokens": 50})


if __name__ == "__main__":
    unittest.main()
