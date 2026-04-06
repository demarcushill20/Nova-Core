"""Tests for profit cushion protocol.

Comprehensive test coverage for the P2 priority implementation from
funded account survival research.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from novatrade.risk.profit_cushion_protocol import (
    ProfitCushionConfig,
    ProfitCushionProtocol,
    ProfitCushionState,
)


class TestProfitCushionConfig:
    """Test profit cushion configuration."""

    def test_default_config(self):
        """Test default configuration values."""
        config = ProfitCushionConfig()

        # Test default thresholds
        assert config.conservative_threshold == 3.0
        assert config.selective_threshold == 5.0
        assert config.turtle_threshold == 8.0
        assert config.scaling_threshold == 10.0

        # Test default multipliers
        assert config.normal_multiplier == 1.0
        assert config.conservative_multiplier == 0.75
        assert config.selective_multiplier == 0.5
        assert config.turtle_multiplier == 0.25
        assert config.scaling_lock_multiplier == 0.10

        # Test other defaults
        assert config.cycle_length_months == 4
        assert config.timezone == "Europe/Prague"
        assert config.auto_save is True

    def test_get_risk_multiplier_normal_tier(self):
        """Test risk multiplier for normal tier (0-3% profit)."""
        config = ProfitCushionConfig()

        # Test various profit levels in normal range
        assert config.get_risk_multiplier(0.0) == ("normal", 1.0)
        assert config.get_risk_multiplier(1.5) == ("normal", 1.0)
        assert config.get_risk_multiplier(2.9) == ("normal", 1.0)

    def test_get_risk_multiplier_conservative_tier(self):
        """Test risk multiplier for conservative tier (3-5% profit)."""
        config = ProfitCushionConfig()

        # Test various profit levels in conservative range
        assert config.get_risk_multiplier(3.0) == ("conservative", 0.75)
        assert config.get_risk_multiplier(4.0) == ("conservative", 0.75)
        assert config.get_risk_multiplier(4.9) == ("conservative", 0.75)

    def test_get_risk_multiplier_selective_tier(self):
        """Test risk multiplier for selective tier (5-8% profit)."""
        config = ProfitCushionConfig()

        # Test various profit levels in selective range
        assert config.get_risk_multiplier(5.0) == ("selective", 0.5)
        assert config.get_risk_multiplier(6.5) == ("selective", 0.5)
        assert config.get_risk_multiplier(7.9) == ("selective", 0.5)

    def test_get_risk_multiplier_turtle_tier(self):
        """Test risk multiplier for turtle tier (8-10% profit)."""
        config = ProfitCushionConfig()

        # Test various profit levels in turtle range
        assert config.get_risk_multiplier(8.0) == ("turtle", 0.25)
        assert config.get_risk_multiplier(9.0) == ("turtle", 0.25)
        assert config.get_risk_multiplier(9.9) == ("turtle", 0.25)

    def test_get_risk_multiplier_scaling_lock_tier(self):
        """Test risk multiplier for scaling lock tier (10%+ profit)."""
        config = ProfitCushionConfig()

        # Test various profit levels in scaling lock range
        assert config.get_risk_multiplier(10.0) == ("scaling_lock", 0.10)
        assert config.get_risk_multiplier(12.5) == ("scaling_lock", 0.10)
        assert config.get_risk_multiplier(20.0) == ("scaling_lock", 0.10)

    def test_custom_thresholds(self):
        """Test configuration with custom thresholds."""
        config = ProfitCushionConfig(
            conservative_threshold=2.5,
            selective_threshold=4.5,
            turtle_threshold=7.5,
            scaling_threshold=9.0,
        )

        assert config.get_risk_multiplier(2.4) == ("normal", 1.0)
        assert config.get_risk_multiplier(2.5) == ("conservative", 0.75)
        assert config.get_risk_multiplier(4.5) == ("selective", 0.5)
        assert config.get_risk_multiplier(7.5) == ("turtle", 0.25)
        assert config.get_risk_multiplier(9.0) == ("scaling_lock", 0.10)

    def test_custom_multipliers(self):
        """Test configuration with custom multipliers."""
        config = ProfitCushionConfig(
            conservative_multiplier=0.80,
            selective_multiplier=0.60,
            turtle_multiplier=0.30,
            scaling_lock_multiplier=0.05,
        )

        assert config.get_risk_multiplier(0.0) == ("normal", 1.0)
        assert config.get_risk_multiplier(3.5) == ("conservative", 0.80)
        assert config.get_risk_multiplier(6.0) == ("selective", 0.60)
        assert config.get_risk_multiplier(8.5) == ("turtle", 0.30)
        assert config.get_risk_multiplier(10.5) == ("scaling_lock", 0.05)


class TestProfitCushionState:
    """Test profit cushion state data structure."""

    def test_profit_calculations(self):
        """Test profit amount and percentage calculations."""
        tz = ZoneInfo("Europe/Prague")
        start_date = datetime(2026, 4, 1, tzinfo=tz)
        end_date = start_date + timedelta(days=120)

        state = ProfitCushionState(
            cycle_start_date=start_date,
            cycle_end_date=end_date,
            starting_equity=100000.0,
            current_equity=105000.0,
            cycle_profit_pct=5.0,
        )

        assert state.cycle_profit_amount == 5000.0
        assert state.starting_equity == 100000.0
        assert state.current_equity == 105000.0

    @patch("novatrade.risk.profit_cushion_protocol.datetime")
    def test_days_calculations(self, mock_datetime):
        """Test days in cycle and days remaining calculations."""
        tz = ZoneInfo("Europe/Prague")
        start_date = datetime(2026, 4, 1, tzinfo=tz)
        end_date = start_date + timedelta(days=120)
        current_date = datetime(2026, 4, 15, tzinfo=tz)  # 14 days later

        mock_datetime.now.return_value = current_date

        state = ProfitCushionState(
            cycle_start_date=start_date,
            cycle_end_date=end_date,
            starting_equity=100000.0,
            current_equity=105000.0,
        )

        assert state.days_in_cycle == 14
        assert state.days_remaining == 106

    def test_scaling_eligibility(self):
        """Test scaling eligibility detection."""
        tz = ZoneInfo("Europe/Prague")
        start_date = datetime(2026, 4, 1, tzinfo=tz)
        end_date = start_date + timedelta(days=120)

        # Not eligible (< 10%)
        state = ProfitCushionState(
            cycle_start_date=start_date,
            cycle_end_date=end_date,
            starting_equity=100000.0,
            current_equity=109500.0,
            cycle_profit_pct=9.5,
        )
        assert not state.is_scaling_eligible()

        # Eligible (>= 10%)
        state.cycle_profit_pct = 10.0
        assert state.is_scaling_eligible()

        state.cycle_profit_pct = 12.5
        assert state.is_scaling_eligible()


class TestProfitCushionProtocol:
    """Test profit cushion protocol implementation."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.temp_dir, "test_profit_cushion.json")

    def tearDown(self):
        """Clean up test fixtures."""
        if os.path.exists(self.state_file):
            os.unlink(self.state_file)
        os.rmdir(self.temp_dir)

    def test_initialization_new_cycle(self):
        """Test initialization of new profit cushion cycle."""
        self.setUp()
        try:
            tz = ZoneInfo("Europe/Prague")
            start_date = datetime(2026, 4, 1, tzinfo=tz)
            starting_equity = 100000.0

            config = ProfitCushionConfig(state_file_path=self.state_file)
            protocol = ProfitCushionProtocol(
                config=config,
                starting_equity=starting_equity,
                cycle_start_date=start_date,
            )

            # Test initial state
            assert protocol.get_tier() == "normal"
            assert protocol.get_risk_multiplier() == 1.0

            status = protocol.get_cycle_status()
            assert status["starting_equity"] == starting_equity
            assert status["current_equity"] == starting_equity
            assert status["cycle_profit_pct"] == 0.0
            assert not status["is_scaling_eligible"]

        finally:
            self.tearDown()

    def test_initialization_requires_parameters(self):
        """Test that initialization requires starting equity and cycle date for new cycles."""
        self.setUp()
        try:
            config = ProfitCushionConfig(state_file_path=self.state_file)

            # Should fail without parameters
            with pytest.raises(ValueError, match="Must provide starting_equity"):
                ProfitCushionProtocol(config=config)

            # Should fail with only starting equity
            with pytest.raises(ValueError, match="Must provide starting_equity"):
                ProfitCushionProtocol(config=config, starting_equity=100000.0)

            # Should fail with only cycle date
            with pytest.raises(ValueError, match="Must provide starting_equity"):
                ProfitCushionProtocol(config=config, cycle_start_date=datetime.now())

        finally:
            self.tearDown()

    def test_tier_progression_normal_to_conservative(self):
        """Test tier progression from normal to conservative."""
        self.setUp()
        try:
            tz = ZoneInfo("Europe/Prague")
            start_date = datetime(2026, 4, 1, tzinfo=tz)
            starting_equity = 100000.0

            config = ProfitCushionConfig(state_file_path=self.state_file)
            protocol = ProfitCushionProtocol(
                config=config,
                starting_equity=starting_equity,
                cycle_start_date=start_date,
            )

            # Start in normal tier
            assert protocol.get_tier() == "normal"
            assert protocol.get_risk_multiplier() == 1.0

            # Update to 2.5% profit (still normal)
            tier_changed = protocol.update_equity(102500.0)
            assert not tier_changed
            assert protocol.get_tier() == "normal"
            assert protocol.get_risk_multiplier() == 1.0

            # Update to 3.5% profit (conservative tier)
            tier_changed = protocol.update_equity(103500.0)
            assert tier_changed
            assert protocol.get_tier() == "conservative"
            assert protocol.get_risk_multiplier() == 0.75

        finally:
            self.tearDown()

    def test_full_tier_progression(self):
        """Test full tier progression through all levels."""
        self.setUp()
        try:
            tz = ZoneInfo("Europe/Prague")
            start_date = datetime(2026, 4, 1, tzinfo=tz)
            starting_equity = 100000.0

            config = ProfitCushionConfig(state_file_path=self.state_file)
            protocol = ProfitCushionProtocol(
                config=config,
                starting_equity=starting_equity,
                cycle_start_date=start_date,
            )

            # Test each tier transition
            test_cases = [
                (101000.0, "normal", 1.0, False),  # 1% profit
                (103500.0, "conservative", 0.75, True),  # 3.5% profit
                (106000.0, "selective", 0.5, True),  # 6% profit
                (108500.0, "turtle", 0.25, True),  # 8.5% profit
                (110500.0, "scaling_lock", 0.10, True),  # 10.5% profit
            ]

            for equity, expected_tier, expected_multiplier, expected_tier_change in test_cases:
                tier_changed = protocol.update_equity(equity)
                assert tier_changed == expected_tier_change
                assert protocol.get_tier() == expected_tier
                assert protocol.get_risk_multiplier() == expected_multiplier

                # Verify scaling eligibility for 10%+ profit
                status = protocol.get_cycle_status()
                if equity >= 110000.0:  # 10%+ profit
                    assert status["is_scaling_eligible"]
                else:
                    assert not status["is_scaling_eligible"]

        finally:
            self.tearDown()

    def test_equity_decrease_tier_adjustment(self):
        """Test tier adjustment when equity decreases."""
        self.setUp()
        try:
            tz = ZoneInfo("Europe/Prague")
            start_date = datetime(2026, 4, 1, tzinfo=tz)
            starting_equity = 100000.0

            config = ProfitCushionConfig(state_file_path=self.state_file)
            protocol = ProfitCushionProtocol(
                config=config,
                starting_equity=starting_equity,
                cycle_start_date=start_date,
            )

            # Move to selective tier (6% profit)
            protocol.update_equity(106000.0)
            assert protocol.get_tier() == "selective"
            assert protocol.get_risk_multiplier() == 0.5

            # Decrease to conservative tier (4% profit)
            tier_changed = protocol.update_equity(104000.0)
            assert tier_changed
            assert protocol.get_tier() == "conservative"
            assert protocol.get_risk_multiplier() == 0.75

            # Decrease to normal tier (2% profit)
            tier_changed = protocol.update_equity(102000.0)
            assert tier_changed
            assert protocol.get_tier() == "normal"
            assert protocol.get_risk_multiplier() == 1.0

        finally:
            self.tearDown()

    def test_state_persistence(self):
        """Test state persistence across protocol instances."""
        self.setUp()
        try:
            tz = ZoneInfo("Europe/Prague")
            start_date = datetime(2026, 4, 1, tzinfo=tz)
            starting_equity = 100000.0

            config = ProfitCushionConfig(state_file_path=self.state_file)

            # Create first instance and update equity
            protocol1 = ProfitCushionProtocol(
                config=config,
                starting_equity=starting_equity,
                cycle_start_date=start_date,
            )
            protocol1.update_equity(105500.0)  # 5.5% profit (selective tier)

            assert protocol1.get_tier() == "selective"
            assert protocol1.get_risk_multiplier() == 0.5

            # Create second instance (should load saved state)
            protocol2 = ProfitCushionProtocol(config=config)

            assert protocol2.get_tier() == "selective"
            assert protocol2.get_risk_multiplier() == 0.5

            status = protocol2.get_cycle_status()
            assert status["current_equity"] == 105500.0
            assert status["cycle_profit_pct"] == 5.5

        finally:
            self.tearDown()

    def test_cycle_expiration_detection(self):
        """Test detection of expired cycles."""
        self.setUp()
        try:
            tz = ZoneInfo("Europe/Prague")
            # Create cycle that ended yesterday
            start_date = datetime(2026, 1, 1, tzinfo=tz)
            end_date = datetime(2026, 4, 1, tzinfo=tz)  # Already past

            config = ProfitCushionConfig(state_file_path=self.state_file)

            # Save expired state manually
            expired_state = {
                "cycle_start_date": start_date.isoformat(),
                "cycle_end_date": end_date.isoformat(),
                "starting_equity": 100000.0,
                "current_equity": 105000.0,
                "cycle_profit_pct": 5.0,
                "tier": "selective",
                "risk_multiplier": 0.5,
                "tier_changed_at": None,
                "last_updated": datetime.now(tz).isoformat(),
            }

            with open(self.state_file, "w") as f:
                json.dump(expired_state, f)

            # Should not load expired state, will need new initialization
            with pytest.raises(ValueError, match="Must provide starting_equity"):
                ProfitCushionProtocol(config=config)

        finally:
            self.tearDown()

    def test_new_cycle_start(self):
        """Test starting a new cycle."""
        self.setUp()
        try:
            tz = ZoneInfo("Europe/Prague")
            config = ProfitCushionConfig(state_file_path=self.state_file)

            # Create initial protocol with old data
            old_start = datetime(2026, 1, 1, tzinfo=tz)
            protocol = ProfitCushionProtocol(
                config=config,
                starting_equity=100000.0,
                cycle_start_date=old_start,
            )
            protocol.update_equity(105000.0)  # 5% profit

            assert protocol.get_tier() == "selective"

            # Start new cycle
            new_start = datetime(2026, 5, 1, tzinfo=tz)
            protocol.start_new_cycle(starting_equity=125000.0, cycle_start_date=new_start)

            # Should reset to normal tier with new equity base
            assert protocol.get_tier() == "normal"
            assert protocol.get_risk_multiplier() == 1.0

            status = protocol.get_cycle_status()
            assert status["starting_equity"] == 125000.0
            assert status["current_equity"] == 125000.0
            assert status["cycle_profit_pct"] == 0.0

        finally:
            self.tearDown()

    def test_uninitalized_state_handling(self):
        """Test handling of uninitialized state."""
        # Create protocol with auto_save disabled and no state file
        config = ProfitCushionConfig(state_file_path="/nonexistent/path/test.json", auto_save=False)

        # Should not be able to create without parameters
        with pytest.raises(ValueError, match="Must provide starting_equity"):
            ProfitCushionProtocol(config=config)

    def test_custom_configuration_integration(self):
        """Test integration with custom configuration."""
        self.setUp()
        try:
            tz = ZoneInfo("Europe/Prague")
            start_date = datetime(2026, 4, 1, tzinfo=tz)

            # Custom config with different thresholds
            config = ProfitCushionConfig(
                state_file_path=self.state_file,
                conservative_threshold=2.0,  # Earlier threshold
                selective_threshold=4.0,
                turtle_threshold=6.0,
                scaling_threshold=8.0,
                conservative_multiplier=0.80,  # Different multiplier
            )

            protocol = ProfitCushionProtocol(
                config=config,
                starting_equity=100000.0,
                cycle_start_date=start_date,
            )

            # Test early conservative tier
            protocol.update_equity(102100.0)  # 2.1% profit
            assert protocol.get_tier() == "conservative"
            assert protocol.get_risk_multiplier() == 0.80  # Custom multiplier

            # Test early scaling eligibility
            protocol.update_equity(108200.0)  # 8.2% profit
            assert protocol.get_tier() == "scaling_lock"

            status = protocol.get_cycle_status()
            assert status["is_scaling_eligible"]  # 8.2% > 8.0% custom threshold

        finally:
            self.tearDown()

    def test_status_information_completeness(self):
        """Test that cycle status provides complete information."""
        self.setUp()
        try:
            tz = ZoneInfo("Europe/Prague")
            start_date = datetime(2026, 4, 1, tzinfo=tz)

            config = ProfitCushionConfig(state_file_path=self.state_file)
            protocol = ProfitCushionProtocol(
                config=config,
                starting_equity=100000.0,
                cycle_start_date=start_date,
            )

            protocol.update_equity(107500.0)  # 7.5% profit

            status = protocol.get_cycle_status()

            # Check all expected fields are present
            expected_fields = [
                "cycle_start",
                "cycle_end",
                "days_elapsed",
                "days_remaining",
                "starting_equity",
                "current_equity",
                "cycle_profit_amount",
                "cycle_profit_pct",
                "tier",
                "risk_multiplier",
                "is_scaling_eligible",
                "tier_changed_at",
                "last_updated",
            ]

            for field in expected_fields:
                assert field in status

            # Check specific values
            assert status["starting_equity"] == 100000.0
            assert status["current_equity"] == 107500.0
            assert status["cycle_profit_amount"] == 7500.0
            assert status["cycle_profit_pct"] == 7.5
            assert status["tier"] == "selective"
            assert status["risk_multiplier"] == 0.5

        finally:
            self.tearDown()
