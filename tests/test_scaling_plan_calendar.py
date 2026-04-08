"""Tests for scaling plan calendar automation.

Tests the FTMO scaling plan calendar system including cycle management,
event generation, scaling eligibility tracking, and calendar automation.
"""

import os
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from novatrade.risk.scaling_plan_calendar import ScalingCycle, ScalingEvent, ScalingPlanCalendar, ScalingPlanConfig


class TestScalingEvent:
    """Tests for ScalingEvent dataclass."""

    def test_scaling_event_creation(self):
        """Test basic scaling event creation."""
        due_date = datetime(2026, 4, 15, 12, 0, tzinfo=ZoneInfo("Europe/Prague"))

        event = ScalingEvent(
            event_id="test_event_001",
            event_type="cycle_start",
            title="Test Scaling Event",
            description="Test event for scaling calendar",
            due_date=due_date,
            status="pending",
            metadata={"test_key": "test_value"},
        )

        assert event.event_id == "test_event_001"
        assert event.event_type == "cycle_start"
        assert event.title == "Test Scaling Event"
        assert event.description == "Test event for scaling calendar"
        assert event.due_date == due_date
        assert event.status == "pending"
        assert event.metadata["test_key"] == "test_value"
        assert event.created_at is not None


class TestScalingCycle:
    """Tests for ScalingCycle dataclass and properties."""

    def test_scaling_cycle_creation(self):
        """Test basic scaling cycle creation."""
        start_date = datetime(2026, 4, 1, tzinfo=ZoneInfo("Europe/Prague"))
        end_date = datetime(2026, 8, 1, tzinfo=ZoneInfo("Europe/Prague"))

        cycle = ScalingCycle(
            cycle_id="test_cycle_20260401",
            start_date=start_date,
            end_date=end_date,
            starting_equity=100000.0,
            target_profit_amount=10000.0,
            current_equity=100000.0,
        )

        assert cycle.cycle_id == "test_cycle_20260401"
        assert cycle.start_date == start_date
        assert cycle.end_date == end_date
        assert cycle.starting_equity == 100000.0
        assert cycle.target_profit_amount == 10000.0
        assert cycle.current_equity == 100000.0
        assert cycle.cycle_status == "active"

    def test_profit_calculations(self):
        """Test cycle profit calculation properties."""
        start_date = datetime(2026, 4, 1, tzinfo=ZoneInfo("Europe/Prague"))
        end_date = datetime(2026, 8, 1, tzinfo=ZoneInfo("Europe/Prague"))

        cycle = ScalingCycle(
            cycle_id="test_cycle",
            start_date=start_date,
            end_date=end_date,
            starting_equity=100000.0,
            target_profit_amount=10000.0,
            current_equity=105000.0,  # 5% profit
        )

        # Test profit calculations
        assert cycle.current_profit_amount == 5000.0
        assert cycle.current_profit_pct == 5.0

        # Test scaling eligibility (should be False at 5%)
        assert not cycle.is_scaling_eligible

        # Update to 10% profit
        cycle.current_equity = 110000.0
        assert cycle.current_profit_amount == 10000.0
        assert cycle.current_profit_pct == 10.0
        assert cycle.is_scaling_eligible

    def test_days_remaining_calculation(self):
        """Test days remaining calculation."""
        # Create cycle that started 30 days ago and ends in 90 days
        now = datetime.now(ZoneInfo("Europe/Prague"))
        start_date = now - timedelta(days=30)
        end_date = now + timedelta(days=90)

        cycle = ScalingCycle(
            cycle_id="test_cycle",
            start_date=start_date,
            end_date=end_date,
            starting_equity=100000.0,
            target_profit_amount=10000.0,
        )

        # Should have approximately 90 days remaining
        assert 89 <= cycle.days_remaining <= 91

    def test_scaling_deadline_calculation(self):
        """Test scaling deadline calculation (7 days before end)."""
        start_date = datetime(2026, 4, 1, tzinfo=ZoneInfo("Europe/Prague"))
        end_date = datetime(2026, 8, 1, tzinfo=ZoneInfo("Europe/Prague"))
        expected_deadline = datetime(2026, 7, 25, tzinfo=ZoneInfo("Europe/Prague"))

        cycle = ScalingCycle(
            cycle_id="test_cycle",
            start_date=start_date,
            end_date=end_date,
            starting_equity=100000.0,
            target_profit_amount=10000.0,
        )

        assert cycle.scaling_deadline == expected_deadline


class TestScalingPlanConfig:
    """Tests for ScalingPlanConfig."""

    def test_default_configuration(self):
        """Test default scaling plan configuration."""
        config = ScalingPlanConfig()

        assert config.cycle_length_months == 4
        assert config.target_profit_pct == 10.0
        assert config.scaling_deadline_days == 7
        assert config.timezone == "Europe/Prague"
        assert config.enable_calendar_events is True
        assert config.reminder_days_before == [30, 14, 7, 3, 1]
        assert config.auto_submit_scaling is False
        assert config.notification_enabled is True

    def test_custom_configuration(self):
        """Test custom scaling plan configuration."""
        config = ScalingPlanConfig(
            cycle_length_months=6,
            target_profit_pct=15.0,
            scaling_deadline_days=14,
            timezone="America/New_York",
            reminder_days_before=[7, 3, 1],
        )

        assert config.cycle_length_months == 6
        assert config.target_profit_pct == 15.0
        assert config.scaling_deadline_days == 14
        assert config.timezone == "America/New_York"
        assert config.reminder_days_before == [7, 3, 1]


class TestScalingPlanCalendar:
    """Tests for ScalingPlanCalendar main class."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.temp_dir, "test_scaling_calendar.json")

        self.config = ScalingPlanConfig(state_file_path=self.state_file, auto_save=True)

    def tearDown(self):
        """Clean up test fixtures."""
        if os.path.exists(self.state_file):
            os.unlink(self.state_file)
        if os.path.exists(self.temp_dir):
            os.rmdir(self.temp_dir)

    def test_calendar_initialization(self):
        """Test scaling plan calendar initialization."""
        self.setUp()
        try:
            calendar = ScalingPlanCalendar(self.config)

            # Should not have a current cycle initially
            assert calendar.get_current_cycle() is None

            # Status should indicate no active cycle
            status = calendar.get_cycle_status()
            assert status["status"] == "no_active_cycle"
        finally:
            self.tearDown()

    def test_cycle_initialization(self):
        """Test FTMO cycle initialization."""
        self.setUp()
        try:
            calendar = ScalingPlanCalendar(self.config)

            # Initialize new cycle
            start_date = datetime(2026, 4, 1, tzinfo=ZoneInfo("Europe/Prague"))
            cycle = calendar.initialize_cycle(starting_equity=100000.0, cycle_start_date=start_date)

            # Verify cycle properties
            assert cycle.cycle_id == "ftmo_cycle_20260401"
            assert cycle.start_date == start_date
            assert cycle.starting_equity == 100000.0
            assert cycle.target_profit_amount == 10000.0  # 10% of 100K
            assert cycle.current_equity == 100000.0
            assert cycle.cycle_status == "active"

            # Should be set as current cycle
            current_cycle = calendar.get_current_cycle()
            assert current_cycle is not None
            assert current_cycle.cycle_id == "ftmo_cycle_20260401"
        finally:
            self.tearDown()

    def test_event_generation(self):
        """Test calendar event generation for new cycle."""
        self.setUp()
        try:
            calendar = ScalingPlanCalendar(self.config)

            start_date = datetime(2026, 4, 1, tzinfo=ZoneInfo("Europe/Prague"))
            cycle = calendar.initialize_cycle(starting_equity=100000.0, cycle_start_date=start_date)

            # Should have generated multiple events
            assert len(cycle.events) >= 5  # start, reminders, deadline, end

            # Check for specific event types
            event_types = [e.event_type for e in cycle.events]
            assert "cycle_start" in event_types
            assert "submission_deadline" in event_types
            assert "cycle_end" in event_types
            assert "submission_reminder" in event_types

            # Verify cycle start event
            start_event = next(e for e in cycle.events if e.event_type == "cycle_start")
            assert start_event.due_date == start_date
            assert start_event.status == "completed"
        finally:
            self.tearDown()

    def test_equity_updates_and_scaling_eligibility(self):
        """Test equity updates and scaling eligibility detection."""
        self.setUp()
        try:
            calendar = ScalingPlanCalendar(self.config)

            start_date = datetime(2026, 4, 1, tzinfo=ZoneInfo("Europe/Prague"))
            calendar.initialize_cycle(starting_equity=100000.0, cycle_start_date=start_date)

            # Initially not scaling eligible
            cycle = calendar.get_current_cycle()
            assert not cycle.is_scaling_eligible
            assert cycle.cycle_status == "active"
            assert cycle.scaling_eligible_date is None

            # Update to 5% profit (not scaling eligible yet)
            calendar.update_equity(105000.0)
            cycle = calendar.get_current_cycle()
            assert cycle.current_equity == 105000.0
            assert cycle.current_profit_pct == 5.0
            assert not cycle.is_scaling_eligible
            assert cycle.cycle_status == "active"

            # Update to 10% profit (scaling eligible!)
            calendar.update_equity(110000.0)
            cycle = calendar.get_current_cycle()
            assert cycle.current_equity == 110000.0
            assert cycle.current_profit_pct == 10.0
            assert cycle.is_scaling_eligible
            assert cycle.cycle_status == "scaling_eligible"
            assert cycle.scaling_eligible_date is not None

            # Should have created scaling eligible event
            eligible_events = [e for e in cycle.events if e.event_type == "scaling_eligible"]
            assert len(eligible_events) == 1
            assert eligible_events[0].status == "completed"
        finally:
            self.tearDown()

    def test_cycle_status_reporting(self):
        """Test comprehensive cycle status reporting."""
        self.setUp()
        try:
            calendar = ScalingPlanCalendar(self.config)

            start_date = datetime(2026, 4, 1, tzinfo=ZoneInfo("Europe/Prague"))
            calendar.initialize_cycle(starting_equity=100000.0, cycle_start_date=start_date)

            # Update equity to scaling eligible
            calendar.update_equity(110000.0)

            # Get comprehensive status
            status = calendar.get_cycle_status()

            # Verify status fields
            assert status["cycle_id"] == "ftmo_cycle_20260401"
            assert status["cycle_status"] == "scaling_eligible"
            assert status["starting_equity"] == 100000.0
            assert status["current_equity"] == 110000.0
            assert status["current_profit_amount"] == 10000.0
            assert status["current_profit_pct"] == 10.0
            assert status["target_profit_amount"] == 10000.0
            assert status["target_profit_pct"] == 10.0
            assert status["is_scaling_eligible"] is True
            assert status["scaling_eligible_date"] is not None
            assert "scaling_deadline" in status
            assert "days_remaining" in status
            assert status["pending_events"] > 0
        finally:
            self.tearDown()

    def test_upcoming_events_filtering(self):
        """Test filtering of upcoming calendar events."""
        self.setUp()
        try:
            calendar = ScalingPlanCalendar(self.config)

            # Create cycle with start date in the past so we have upcoming events
            now = datetime.now(ZoneInfo("Europe/Prague"))
            start_date = now - timedelta(days=10)
            calendar.initialize_cycle(starting_equity=100000.0, cycle_start_date=start_date)

            # Get upcoming events for next 120 days (4 months)
            upcoming = calendar.get_upcoming_events(days_ahead=120)

            # Should have some upcoming events (reminders, deadline, cycle end)
            assert len(upcoming) > 0

            # All events should be pending and in the future
            now = datetime.now(ZoneInfo("Europe/Prague"))
            for event in upcoming:
                assert event.status == "pending"
                assert event.due_date >= now
                assert event.due_date <= now + timedelta(days=120)

            # Events should be sorted by due date
            for i in range(1, len(upcoming)):
                assert upcoming[i - 1].due_date <= upcoming[i].due_date
        finally:
            self.tearDown()

    def test_scaling_submission_process(self):
        """Test scaling application submission process."""
        self.setUp()
        try:
            self.config.auto_submit_scaling = True
            calendar = ScalingPlanCalendar(self.config)

            start_date = datetime(2026, 4, 1, tzinfo=ZoneInfo("Europe/Prague"))
            calendar.initialize_cycle(starting_equity=100000.0, cycle_start_date=start_date)

            # Cannot submit when not scaling eligible
            assert not calendar.submit_scaling_application()

            # Update to scaling eligible
            calendar.update_equity(110000.0)
            cycle = calendar.get_current_cycle()
            assert cycle.is_scaling_eligible

            # Now submission should work
            assert calendar.submit_scaling_application()

            # Verify submission was recorded
            cycle = calendar.get_current_cycle()
            assert cycle.cycle_status == "scaling_submitted"
            assert cycle.scaling_submitted_date is not None

            # Should have created submission event
            submission_events = [e for e in cycle.events if e.event_type == "scaling_submitted"]
            assert len(submission_events) == 1
            assert submission_events[0].status == "completed"
        finally:
            self.tearDown()

    def test_state_persistence(self):
        """Test state persistence across calendar instances."""
        self.setUp()
        try:
            # Create calendar and initialize cycle
            calendar1 = ScalingPlanCalendar(self.config)
            start_date = datetime(2026, 4, 1, tzinfo=ZoneInfo("Europe/Prague"))
            cycle = calendar1.initialize_cycle(starting_equity=100000.0, cycle_start_date=start_date)

            # Update equity and trigger scaling eligibility
            calendar1.update_equity(110000.0)

            # Verify state file was created
            assert os.path.exists(self.state_file)

            # Create new calendar instance (should load existing state)
            calendar2 = ScalingPlanCalendar(self.config)
            loaded_cycle = calendar2.get_current_cycle()

            # Verify state was loaded correctly
            assert loaded_cycle is not None
            assert loaded_cycle.cycle_id == cycle.cycle_id
            assert loaded_cycle.starting_equity == 100000.0
            assert loaded_cycle.current_equity == 110000.0
            assert loaded_cycle.is_scaling_eligible
            assert loaded_cycle.cycle_status == "scaling_eligible"
            assert len(loaded_cycle.events) == len(cycle.events)
        finally:
            self.tearDown()

    def test_state_file_corruption_handling(self):
        """Test handling of corrupted state files."""
        self.setUp()
        try:
            # Create invalid state file
            with open(self.state_file, "w") as f:
                f.write("invalid json content")

            # Should handle corruption gracefully
            calendar = ScalingPlanCalendar(self.config)

            # Should start with no active cycle
            assert calendar.get_current_cycle() is None

            # Should be able to initialize new cycle
            start_date = datetime(2026, 4, 1, tzinfo=ZoneInfo("Europe/Prague"))
            cycle = calendar.initialize_cycle(starting_equity=100000.0, cycle_start_date=start_date)
            assert cycle is not None
        finally:
            self.tearDown()

    def test_multiple_equity_updates(self):
        """Test multiple equity updates and peak tracking."""
        self.setUp()
        try:
            calendar = ScalingPlanCalendar(self.config)

            start_date = datetime(2026, 4, 1, tzinfo=ZoneInfo("Europe/Prague"))
            calendar.initialize_cycle(starting_equity=100000.0, cycle_start_date=start_date)

            # Track equity progression (values differ by > $10 threshold)
            equity_updates = [102000, 105000, 108000, 106000, 111000, 109000]

            for equity in equity_updates:
                calendar.update_equity(equity)
                cycle = calendar.get_current_cycle()
                assert cycle.current_equity == equity
                assert cycle.peak_equity >= equity  # Peak should be monotonically increasing

            # Final state checks
            cycle = calendar.get_current_cycle()
            assert cycle.peak_equity == 111000.0  # Highest value seen
            assert cycle.current_equity == 109000.0  # Current value (9% profit)
            # Note: scaling eligibility is based on current equity, not peak
            assert not cycle.is_scaling_eligible  # 109K = 9% profit, below 10% threshold
            assert cycle.current_profit_pct == 9.0  # 9% profit at current equity
        finally:
            self.tearDown()

    def test_equity_change_threshold_skips_small_changes(self):
        """Test that equity updates below the threshold are skipped."""
        self.setUp()
        try:
            self.config.equity_change_threshold = 50.0  # $50 threshold
            calendar = ScalingPlanCalendar(self.config)

            start_date = datetime(2026, 4, 1, tzinfo=ZoneInfo("Europe/Prague"))
            calendar.initialize_cycle(starting_equity=100000.0, cycle_start_date=start_date)

            # First update: 102000 — processes (no previous processed equity)
            calendar.update_equity(102000.0)
            cycle = calendar.get_current_cycle()
            assert cycle.current_equity == 102000.0

            # Second update: 102010 — skipped (only $10 change, below $50 threshold)
            calendar.update_equity(102010.0)
            cycle = calendar.get_current_cycle()
            assert cycle.current_equity == 102000.0  # Still at previous value

            # Third update: 102100 — processes ($100 change from last processed)
            calendar.update_equity(102100.0)
            cycle = calendar.get_current_cycle()
            assert cycle.current_equity == 102100.0
        finally:
            self.tearDown()

    def test_notification_rate_limiting(self):
        """Test that notification rate-limiter suppresses duplicate sends."""
        from unittest.mock import patch

        from novatrade.risk.scaling_plan_calendar import _notification_rate_limiter, _send_scaling_notification

        # Clear rate limiter state
        _notification_rate_limiter.clear()

        with (
            patch("novatrade.risk.scaling_plan_calendar.os.environ.get", return_value="test"),
            patch("novatrade.risk.scaling_plan_calendar.urllib.request.urlopen") as mock_urlopen,
        ):
            # First call should go through
            _send_scaling_notification("Test message for dedup", cooldown_seconds=3600)
            assert mock_urlopen.call_count == 1

            # Second identical call should be suppressed
            _send_scaling_notification("Test message for dedup", cooldown_seconds=3600)
            assert mock_urlopen.call_count == 1  # Still 1, not 2

        # Clean up
        _notification_rate_limiter.clear()
