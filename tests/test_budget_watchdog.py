"""Tests for utils.budget_watchdog — Budget Watchdog module."""

import time

import pytest

from utils.budget_watchdog import BudgetWatchdog


class TestBudgetWatchdogBasic:
    """Basic construction and defaults."""

    def test_defaults(self):
        bw = BudgetWatchdog()
        assert bw.max_steps == 50
        assert bw.max_wall_seconds == 300.0
        assert bw.max_tokens is None
        assert bw.max_turns is None
        assert bw.max_cost_usd is None
        assert bw.max_stall_count == 3

    def test_custom_limits(self):
        bw = BudgetWatchdog(max_steps=10, max_wall_seconds=60, max_tokens=1000)
        assert bw.max_steps == 10
        assert bw.max_wall_seconds == 60
        assert bw.max_tokens == 1000

    def test_initial_state(self):
        bw = BudgetWatchdog()
        assert bw.steps == 0
        assert bw.tokens_used == 0
        assert bw.turns == 0
        assert bw.cost_usd == 0.0

    def test_check_before_any_ticks(self):
        bw = BudgetWatchdog()
        allowed, reason = bw.check()
        assert allowed is True
        assert reason == "within budget"


class TestStepLimit:
    def test_step_limit_exact(self):
        bw = BudgetWatchdog(max_steps=3)
        for i in range(3):
            bw.tick(f"output {i}")
        allowed, reason = bw.check()
        assert allowed is False
        assert "step limit" in reason

    def test_step_limit_under(self):
        bw = BudgetWatchdog(max_steps=5)
        bw.tick("a")
        bw.tick("b")
        allowed, reason = bw.check()
        assert allowed is True

    def test_step_limit_disabled(self):
        bw = BudgetWatchdog(max_steps=None)
        for i in range(200):
            bw.tick(f"output {i}")
        allowed, _ = bw.check()
        assert allowed is True


class TestWallClock:
    def test_wall_clock_exceeded(self):
        bw = BudgetWatchdog(max_wall_seconds=0.01)
        bw.start()
        time.sleep(0.02)
        allowed, reason = bw.check()
        assert allowed is False
        assert "wall-clock" in reason

    def test_wall_clock_within(self):
        bw = BudgetWatchdog(max_wall_seconds=10.0)
        bw.start()
        allowed, _ = bw.check()
        assert allowed is True

    def test_wall_clock_not_started(self):
        bw = BudgetWatchdog(max_wall_seconds=0.001)
        # Timer not started — should pass (no start_time means 0 elapsed)
        allowed, _ = bw.check()
        assert allowed is True

    def test_elapsed_seconds(self):
        bw = BudgetWatchdog()
        assert bw.elapsed_seconds == 0.0
        bw.start()
        time.sleep(0.01)
        assert bw.elapsed_seconds > 0.0


class TestTokenLimit:
    def test_token_limit_exceeded(self):
        bw = BudgetWatchdog(max_tokens=100)
        bw.tick("output", tokens=60)
        bw.tick("output2", tokens=50)
        allowed, reason = bw.check()
        assert allowed is False
        assert "token limit" in reason

    def test_token_limit_within(self):
        bw = BudgetWatchdog(max_tokens=100)
        bw.tick("output", tokens=50)
        allowed, _ = bw.check()
        assert allowed is True


class TestTurnLimit:
    def test_turn_limit_exceeded(self):
        bw = BudgetWatchdog(max_turns=2)
        bw.tick("a", is_turn=True)
        bw.tick("b", is_turn=True)
        allowed, reason = bw.check()
        assert allowed is False
        assert "turn limit" in reason

    def test_non_turn_ticks_dont_count(self):
        bw = BudgetWatchdog(max_turns=2)
        bw.tick("a", is_turn=False)
        bw.tick("b", is_turn=False)
        bw.tick("c", is_turn=False)
        allowed, _ = bw.check()
        assert allowed is True


class TestCostLimit:
    def test_cost_limit_exceeded(self):
        bw = BudgetWatchdog(max_cost_usd=0.10)
        bw.tick("a", cost_usd=0.06)
        bw.tick("b", cost_usd=0.05)
        allowed, reason = bw.check()
        assert allowed is False
        assert "cost limit" in reason

    def test_cost_within(self):
        bw = BudgetWatchdog(max_cost_usd=1.00)
        bw.tick("a", cost_usd=0.50)
        allowed, _ = bw.check()
        assert allowed is True


class TestStallDetection:
    def test_stall_detected(self):
        bw = BudgetWatchdog(max_stall_count=3, max_steps=100)
        for _ in range(4):
            bw.tick("identical output")
        allowed, reason = bw.check()
        assert allowed is False
        assert "stall" in reason

    def test_no_stall_with_varied_output(self):
        bw = BudgetWatchdog(max_stall_count=3, max_steps=100)
        for i in range(10):
            bw.tick(f"different output {i}")
        allowed, _ = bw.check()
        assert allowed is True

    def test_stall_resets_on_different_output(self):
        bw = BudgetWatchdog(max_stall_count=3, max_steps=100)
        bw.tick("same")
        bw.tick("same")
        bw.tick("different!")  # resets stall counter
        bw.tick("same again")
        bw.tick("same again")
        allowed, _ = bw.check()
        assert allowed is True


class TestContextManager:
    def test_context_manager_starts_timer(self):
        bw = BudgetWatchdog()
        assert bw._start_time is None
        with bw:
            assert bw._start_time is not None

    def test_context_manager_doesnt_suppress_exceptions(self):
        bw = BudgetWatchdog()
        with pytest.raises(ValueError, match="test error"), bw:
            raise ValueError("test error")


class TestExhaustedStickiness:
    def test_once_exhausted_stays_exhausted(self):
        bw = BudgetWatchdog(max_steps=1)
        bw.tick("a")
        allowed1, reason1 = bw.check()
        assert allowed1 is False

        # Subsequent checks return same result
        allowed2, reason2 = bw.check()
        assert allowed2 is False
        assert reason2 == reason1


class TestReset:
    def test_reset_clears_all_state(self):
        bw = BudgetWatchdog(max_steps=2)
        bw.start()
        bw.tick("a", tokens=10, cost_usd=0.01, is_turn=True)
        bw.tick("b", tokens=10, cost_usd=0.01, is_turn=True)
        bw.check()  # should exhaust

        bw.reset()
        assert bw.steps == 0
        assert bw.tokens_used == 0
        assert bw.turns == 0
        assert bw.cost_usd == 0.0
        assert bw._start_time is None
        assert bw._exhausted_reason is None

        allowed, reason = bw.check()
        assert allowed is True
        assert reason == "within budget"


class TestSummary:
    def test_summary_contains_all_fields(self):
        bw = BudgetWatchdog(max_steps=10, max_tokens=500)
        bw.start()
        bw.tick("hello", tokens=100, cost_usd=0.01, is_turn=True)

        s = bw.summary()
        assert s["steps"] == 1
        assert s["max_steps"] == 10
        assert s["tokens_used"] == 100
        assert s["max_tokens"] == 500
        assert s["turns"] == 1
        assert s["cost_usd"] == 0.01
        assert s["stall_count"] == 0
        assert s["exhausted_reason"] is None
        assert s["elapsed_seconds"] >= 0  # may be 0.0 on fast machines
        assert "elapsed_seconds" in s


class TestMultipleLimitsFirstHit:
    """When multiple limits are close, the first one hit should be reported."""

    def test_steps_hit_before_tokens(self):
        bw = BudgetWatchdog(max_steps=2, max_tokens=1000)
        bw.tick("a", tokens=100)
        bw.tick("b", tokens=100)
        allowed, reason = bw.check()
        assert allowed is False
        assert "step limit" in reason

    def test_tokens_hit_before_steps(self):
        bw = BudgetWatchdog(max_steps=100, max_tokens=50)
        bw.tick("a", tokens=60)
        allowed, reason = bw.check()
        assert allowed is False
        assert "token limit" in reason
