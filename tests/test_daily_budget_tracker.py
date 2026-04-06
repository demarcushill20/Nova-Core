"""Tests for Daily Risk Budget Tracker (P1 priority from funded account survival research).

Tests the core functionality of tracking and allocating daily risk budget
across multiple trades instead of just checking FTMO compliance.
"""

import json
import time
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from novatrade.risk.daily_budget_tracker import DailyRiskBudgetTracker, TradeRiskAllocation


@pytest.fixture
def budget_tracker():
    """Standard budget tracker for testing."""
    tracker = DailyRiskBudgetTracker(
        daily_loss_limit_pct=5.0,
        max_risk_per_trade_pct=20.0,
        min_risk_per_trade_pct=5.0,
        max_trades_per_day=10,
        allocation_strategy="adaptive",
    )
    tracker.initialize(100000.0)  # $100K account
    return tracker


@pytest.fixture
def ftmo_tz():
    """FTMO timezone for testing."""
    return ZoneInfo("Europe/Prague")


class TestBudgetTrackerInitialization:
    """Test budget tracker initialization and configuration."""

    def test_initialize_with_account_equity(self):
        """Test proper initialization with account equity."""
        tracker = DailyRiskBudgetTracker()
        tracker.initialize(100000.0)

        assert tracker.daily_budget_usd == 5000.0  # 5% of 100K
        assert tracker.allocated_today_usd == 0.0
        assert tracker.remaining_budget_usd == 5000.0
        assert tracker.budget_utilization_pct == 0.0
        assert tracker.trades_today == 0

    def test_custom_configuration(self):
        """Test initialization with custom parameters."""
        tracker = DailyRiskBudgetTracker(
            daily_loss_limit_pct=3.0,  # Conservative 3%
            max_risk_per_trade_pct=15.0,  # Max 15% per trade
            min_risk_per_trade_pct=3.0,  # Min 3% per trade
            max_trades_per_day=8,
            allocation_strategy="equal",
        )
        tracker.initialize(50000.0)  # $50K account

        assert tracker.daily_budget_usd == 1500.0  # 3% of 50K
        assert tracker.max_risk_per_trade_pct == 15.0
        assert tracker.allocation_strategy == "equal"


class TestRiskCalculation:
    """Test risk allocation calculation strategies."""

    def test_equal_allocation_strategy(self):
        """Test equal allocation across expected trades."""
        tracker = DailyRiskBudgetTracker(
            daily_loss_limit_pct=5.0,
            max_trades_per_day=10,
            allocation_strategy="equal",
        )
        tracker.initialize(100000.0)

        # First trade: 5000 / 10 = 500
        risk, reasoning = tracker.calculate_trade_risk("EURUSD", 1.1000, 1.0950)
        assert risk == 500.0
        assert "equal allocation" in reasoning

    def test_adaptive_allocation_strategy(self, budget_tracker):
        """Test adaptive allocation based on budget utilization."""
        # Initial allocation should be full percentage
        risk1, _ = budget_tracker.calculate_trade_risk("EURUSD", 1.1000, 1.0950)
        expected_max = 5000.0 * 0.20  # 20% of daily budget
        assert risk1 == expected_max

        # Simulate some budget usage
        budget_tracker._allocated_today = [
            TradeRiskAllocation(time.time(), "EURUSD", 800.0, 1.1000, 1.0950, 0.8, "test1")
        ]

        # Second allocation should be reduced due to utilization
        risk2, _ = budget_tracker.calculate_trade_risk("GBPUSD", 1.2500, 1.2450)
        assert risk2 < expected_max  # Should be reduced

    @patch("time.time")
    def test_time_weighted_allocation_strategy(self, mock_time):
        """Test time-weighted allocation during high-probability windows."""
        # Mock time to London morning (9 AM Prague = 8 AM UTC in winter)
        london_morning = datetime(2026, 1, 15, 8, 0, 0, tzinfo=timezone.utc)
        mock_time.return_value = london_morning.timestamp()

        tracker = DailyRiskBudgetTracker(
            allocation_strategy="time_weighted",
            max_trades_per_day=10,
        )
        tracker.initialize(100000.0)

        risk, reasoning = tracker.calculate_trade_risk("EURUSD", 1.1000, 1.0950, london_morning.timestamp())

        # Should get 1.2x weight during London morning
        base_allocation = 5000.0 / 10  # 500 base
        expected = base_allocation * 1.2  # 600 with weight
        assert abs(risk - expected) < 10  # Allow some tolerance
        assert "time_weighted allocation" in reasoning

    def test_risk_bounds_enforcement(self, budget_tracker):
        """Test that risk allocation respects min/max bounds."""
        # Test max bound
        risk, _ = budget_tracker.calculate_trade_risk("EURUSD", 1.1000, 1.0950)
        max_allowed = 5000.0 * 0.20  # 20%
        assert risk <= max_allowed

        # Test min bound by using most of the budget
        for i in range(8):
            budget_tracker._allocated_today.append(
                TradeRiskAllocation(time.time(), f"PAIR{i}", 500.0, 1.1000, 1.0950, 0.5, f"test{i}")
            )

        # Should still get minimum allocation
        risk, _ = budget_tracker.calculate_trade_risk("EURJPY", 160.00, 159.50)
        min_allowed = 5000.0 * 0.05  # 5%
        assert risk >= min_allowed

    def test_budget_exhaustion(self, budget_tracker):
        """Test behavior when budget is exhausted."""
        # Exhaust the budget
        budget_tracker._allocated_today = [
            TradeRiskAllocation(time.time(), "EURUSD", 5000.0, 1.1000, 1.0950, 5.0, "test1")
        ]

        risk, reasoning = budget_tracker.calculate_trade_risk("GBPUSD", 1.2500, 1.2450)
        assert risk == 0.0
        assert "budget exhausted" in reasoning


class TestBudgetAllocation:
    """Test trade allocation and budget tracking."""

    def test_allocate_trade_risk(self, budget_tracker):
        """Test successful trade allocation."""
        result = budget_tracker.allocate_trade_risk(
            symbol="EURUSD",
            entry_price=1.1000,
            stop_loss=1.0950,
            volume=1.0,
            trade_id="test_001",
        )

        assert result.passed
        assert "risk allocated" in result.detail
        assert budget_tracker.trades_today == 1
        assert budget_tracker.allocated_today_usd > 0
        assert budget_tracker.remaining_budget_usd < 5000.0

    def test_multiple_trade_allocations(self, budget_tracker):
        """Test multiple trades throughout the day."""
        trades = [
            ("EURUSD", 1.1000, 1.0950, 1.0),
            ("GBPUSD", 1.2500, 1.2450, 0.8),
            ("USDJPY", 150.00, 149.50, 1.2),
        ]

        initial_budget = budget_tracker.remaining_budget_usd

        for i, (symbol, entry, stop, volume) in enumerate(trades):
            result = budget_tracker.allocate_trade_risk(symbol, entry, stop, volume, f"trade_{i}")
            assert result.passed

        assert budget_tracker.trades_today == 3
        assert budget_tracker.remaining_budget_usd < initial_budget
        assert budget_tracker.budget_utilization_pct > 0

    def test_trade_allocation_above_budget(self, budget_tracker):
        """Test allocation when trade would exceed remaining budget."""
        # Use most of the budget
        budget_tracker._allocated_today = [
            TradeRiskAllocation(time.time(), "EURUSD", 4800.0, 1.1000, 1.0950, 4.8, "test1")
        ]

        # Try to allocate a large trade
        result = budget_tracker.allocate_trade_risk(
            symbol="GBPUSD",
            entry_price=1.2500,
            stop_loss=1.2000,  # Large stop = high risk
            volume=2.0,  # Large volume
        )

        # Should still pass with reduced allocation to fit remaining budget
        assert result.passed


class TestBudgetChecking:
    """Test budget checking for pre-trade gate integration."""

    def test_check_budget_availability_sufficient(self, budget_tracker):
        """Test budget check with sufficient funds."""
        result = budget_tracker.check_budget_availability(800.0)

        assert result.passed
        assert "budget available" in result.detail
        assert "800" in result.detail

    def test_check_budget_availability_insufficient(self, budget_tracker):
        """Test budget check with insufficient funds."""
        # Exhaust most of the budget
        budget_tracker._allocated_today = [
            TradeRiskAllocation(time.time(), "EURUSD", 4500.0, 1.1000, 1.0950, 4.5, "test1")
        ]

        # Try to check for more than remaining
        result = budget_tracker.check_budget_availability(1000.0)

        assert not result.passed
        assert "insufficient budget" in result.detail

    def test_check_budget_exhausted(self, budget_tracker):
        """Test budget check when completely exhausted."""
        # Exhaust the budget completely
        budget_tracker._allocated_today = [
            TradeRiskAllocation(time.time(), "EURUSD", 5000.0, 1.1000, 1.0950, 5.0, "test1")
        ]

        result = budget_tracker.check_budget_availability(100.0)

        assert not result.passed
        assert "budget exhausted" in result.detail

    def test_check_single_trade_limit(self, budget_tracker):
        """Test check against max single trade limit."""
        # Try to allocate more than max per trade (20% of 5000 = 1000, so 1500 exceeds it)
        result = budget_tracker.check_budget_availability(1500.0)

        assert not result.passed
        assert "exceeds max per trade" in result.detail


class TestDayBoundaryHandling:
    """Test day boundary management and reset logic."""

    def test_day_rollover_reset(self, budget_tracker):
        """Test budget reset at day boundary."""
        # Allocate some trades
        budget_tracker.allocate_trade_risk("EURUSD", 1.1000, 1.0950, 1.0)
        initial_trades = budget_tracker.trades_today
        initial_allocated = budget_tracker.allocated_today_usd

        assert initial_trades == 1
        assert initial_allocated > 0

        # Manually set a different day key to simulate day rollover
        budget_tracker._day_key = "2026-04-04"  # Set to yesterday

        # Trigger day check - should detect rollover and reset
        budget_tracker._maybe_new_day()

        # Should be reset
        assert budget_tracker.trades_today == 0
        assert budget_tracker.allocated_today_usd == 0.0
        assert budget_tracker.remaining_budget_usd == budget_tracker.daily_budget_usd
        assert budget_tracker._day_key != "2026-04-04"  # Should be updated from yesterday


class TestStatePersistence:
    """Test state saving and loading functionality."""

    def test_save_and_load_state(self, budget_tracker, tmp_path):
        """Test saving and loading budget state."""
        # Mock state directory
        with patch("novatrade.risk.daily_budget_tracker._STATE_DIR", tmp_path):
            # Add some trades
            budget_tracker.allocate_trade_risk("EURUSD", 1.1000, 1.0950, 1.0, "test1")
            budget_tracker.allocate_trade_risk("GBPUSD", 1.2500, 1.2450, 0.8, "test2")

            original_trades = budget_tracker.trades_today
            original_allocated = budget_tracker.allocated_today_usd

            # Save state
            budget_tracker.save_state()

            # Create new tracker and load state
            new_tracker = DailyRiskBudgetTracker()
            new_tracker.initialize(100000.0)
            new_tracker._day_key = budget_tracker._day_key  # Same day
            new_tracker.load_state()

            # Should match original state
            assert new_tracker.trades_today == original_trades
            assert abs(new_tracker.allocated_today_usd - original_allocated) < 0.01

    def test_state_file_creation(self, budget_tracker, tmp_path):
        """Test that state file is created correctly."""
        with patch("novatrade.risk.daily_budget_tracker._STATE_DIR", tmp_path):
            budget_tracker.allocate_trade_risk("EURUSD", 1.1000, 1.0950, 1.0)
            budget_tracker.save_state()

            # Check file exists and has correct structure
            state_files = list(tmp_path.glob("daily_budget_*.json"))
            assert len(state_files) == 1

            with state_files[0].open() as f:
                state_data = json.load(f)

            assert "day_key" in state_data
            assert "daily_budget_usd" in state_data
            assert "allocations" in state_data
            assert len(state_data["allocations"]) == 1


class TestStatusAndMonitoring:
    """Test status reporting and monitoring functionality."""

    def test_status_summary(self, budget_tracker):
        """Test status summary for monitoring."""
        # Add some trades
        budget_tracker.allocate_trade_risk("EURUSD", 1.1000, 1.0950, 1.0)
        budget_tracker.allocate_trade_risk("GBPUSD", 1.2500, 1.2450, 0.8)

        status = budget_tracker.get_status_summary()

        required_fields = [
            "day_key",
            "budget_usd",
            "allocated_usd",
            "remaining_usd",
            "utilization_pct",
            "trades_today",
            "max_per_trade_usd",
            "allocation_strategy",
        ]

        for field in required_fields:
            assert field in status

        assert status["budget_usd"] == 5000.0
        assert status["trades_today"] == 2
        assert 0 <= status["utilization_pct"] <= 100
        assert status["allocation_strategy"] == "adaptive"

    def test_properties(self, budget_tracker):
        """Test public properties."""
        # Initial state
        assert budget_tracker.daily_budget_usd == 5000.0
        assert budget_tracker.allocated_today_usd == 0.0
        assert budget_tracker.remaining_budget_usd == 5000.0
        assert budget_tracker.budget_utilization_pct == 0.0
        assert budget_tracker.trades_today == 0

        # After allocation
        budget_tracker.allocate_trade_risk("EURUSD", 1.1000, 1.0950, 1.0)

        assert budget_tracker.allocated_today_usd > 0
        assert budget_tracker.remaining_budget_usd < 5000.0
        assert budget_tracker.budget_utilization_pct > 0
        assert budget_tracker.trades_today == 1


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_uninitialized_tracker(self):
        """Test behavior when tracker is not initialized."""
        tracker = DailyRiskBudgetTracker()

        risk, reasoning = tracker.calculate_trade_risk("EURUSD", 1.1000, 1.0950)
        assert risk == 0.0
        assert "not initialized" in reasoning

        result = tracker.check_budget_availability(100.0)
        assert result.passed
        assert "not initialized" in result.detail

    def test_zero_equity_initialization(self):
        """Test initialization with zero equity."""
        tracker = DailyRiskBudgetTracker()
        tracker.initialize(0.0)

        assert tracker.daily_budget_usd == 0.0
        assert tracker.remaining_budget_usd == 0.0

    def test_very_small_account(self):
        """Test with very small account size."""
        tracker = DailyRiskBudgetTracker()
        tracker.initialize(1000.0)  # $1K account

        assert tracker.daily_budget_usd == 50.0  # 5% of 1K

        risk, _ = tracker.calculate_trade_risk("EURUSD", 1.1000, 1.0950)
        min_expected = 50.0 * 0.05  # 5% min
        max_expected = 50.0 * 0.20  # 20% max

        assert min_expected <= risk <= max_expected

    def test_pip_value_calculation(self, budget_tracker):
        """Test pip value calculation for different symbols."""
        # These test the internal _get_pip_value method indirectly

        # Major forex pair
        result = budget_tracker.allocate_trade_risk("EURUSD", 1.1000, 1.0950, 1.0)
        assert result.passed

        # JPY pair
        result = budget_tracker.allocate_trade_risk("USDJPY", 150.00, 149.50, 1.0)
        assert result.passed

        # Gold
        result = budget_tracker.allocate_trade_risk("XAUUSD", 2000.0, 1995.0, 1.0)
        assert result.passed


class TestIntegrationScenarios:
    """Test realistic trading scenarios."""

    def test_full_day_trading_scenario(self, budget_tracker):
        """Test a full day of trading with various trades."""
        # Morning trades (conservative)
        trades = [
            ("EURUSD", 1.1000, 1.0980, 0.5, "09:00"),
            ("GBPUSD", 1.2500, 1.2480, 0.3, "09:30"),
            ("USDJPY", 150.00, 149.80, 0.4, "10:15"),
        ]

        for symbol, entry, stop, volume, time_label in trades:
            result = budget_tracker.allocate_trade_risk(symbol, entry, stop, volume, time_label)
            assert result.passed

        # Check mid-day status
        assert budget_tracker.trades_today == 3
        assert budget_tracker.budget_utilization_pct < 70  # Should be well within limits

        # Afternoon trades (slightly larger)
        afternoon_trades = [
            ("EURJPY", 160.00, 159.50, 0.8, "14:00"),
            ("GBPJPY", 190.00, 189.00, 0.6, "15:30"),
        ]

        for symbol, entry, stop, volume, time_label in afternoon_trades:
            result = budget_tracker.allocate_trade_risk(symbol, entry, stop, volume, time_label)
            assert result.passed

        # Final status
        assert budget_tracker.trades_today == 5
        assert budget_tracker.remaining_budget_usd > 0  # Should still have budget
        assert budget_tracker.budget_utilization_pct < 95  # Conservative usage

    def test_high_volatility_day_scenario(self, budget_tracker):
        """Test behavior during high volatility with larger stops."""
        # Simulate news day with wider stops
        volatile_trades = [
            ("EURUSD", 1.1000, 1.0900, 1.0, "news_trade_1"),  # 100 pip stop
            ("GBPUSD", 1.2500, 1.2350, 0.8, "news_trade_2"),  # 150 pip stop
        ]

        for symbol, entry, stop, volume, trade_id in volatile_trades:
            result = budget_tracker.allocate_trade_risk(symbol, entry, stop, volume, trade_id)
            # Should still work but use more budget per trade
            assert result.passed

        # Should have used significant portion of budget due to large stops
        assert budget_tracker.budget_utilization_pct > 30

    def test_budget_conservation_mode(self, budget_tracker):
        """Test conservative allocation when budget gets low."""
        # Use 80% of budget
        large_allocation = 4000.0  # 80% of 5000
        budget_tracker._allocated_today = [
            TradeRiskAllocation(time.time(), "EURUSD", large_allocation, 1.1000, 1.0950, 4.0, "big_trade")
        ]

        # Remaining trades should be very conservative
        result = budget_tracker.allocate_trade_risk("GBPUSD", 1.2500, 1.2480, 0.2)
        assert result.passed

        # Should not exceed remaining budget
        total_after = budget_tracker.allocated_today_usd
        assert total_after <= budget_tracker.daily_budget_usd
