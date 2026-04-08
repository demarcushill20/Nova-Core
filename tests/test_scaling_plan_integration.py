"""Tests for scaling plan calendar integration with PreTradeGate.

Integration tests to ensure the scaling plan calendar works correctly
within the live trading risk management stack alongside profit cushion.
"""

import os
import tempfile
from unittest.mock import patch

from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.models import AccountMode, AccountState, OrderRequest, OrderSide, OrderType
from novatrade.risk.pre_trade_gate import PreTradeGate


def create_test_config(**overrides):
    """Create a test configuration with safe defaults."""
    risk = RiskConfig(
        max_volume_per_trade=10.0,
        max_total_drawdown_pct=10.0,
        max_daily_drawdown_pct=5.0,
        require_stop_loss=True,
        max_slippage_pips=3.0,
    )

    defaults = {
        "dry_run": False,
        "symbols": ["EURUSD", "GBPUSD"],
        "risk": risk,
    }
    defaults.update(overrides)
    return NovaTradeCfg(**defaults)  # type: ignore[arg-type]


class TestScalingPlanIntegration:
    """Integration tests for scaling plan calendar within PreTradeGate."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.temp_dir, "test_scaling_calendar.json")
        self.profit_state_file = os.path.join(self.temp_dir, "test_profit_cushion.json")

        self.cfg = create_test_config()

    def tearDown(self):
        """Clean up test fixtures."""
        for file_path in [self.state_file, self.profit_state_file]:
            if os.path.exists(file_path):
                os.unlink(file_path)
        if os.path.exists(self.temp_dir):
            os.rmdir(self.temp_dir)

    def test_scaling_calendar_initialization_with_pretrade_gate(self):
        """Test scaling calendar initialization during PreTradeGate setup."""
        self.setUp()
        try:
            # Mock the scaling calendar to use our test state file
            with patch("novatrade.risk.scaling_plan_calendar.ScalingPlanConfig") as mock_config_class:
                mock_config = mock_config_class.return_value
                mock_config.state_file_path = self.state_file
                mock_config.auto_save = True
                mock_config.cycle_length_months = 4
                mock_config.target_profit_pct = 10.0
                mock_config.timezone = "Europe/Prague"

                gate = PreTradeGate(self.cfg)

                # Verify scaling calendar was initialized
                assert gate._scaling_calendar is not None
                assert gate.scaling_calendar_status["status"] == "no_active_cycle"

        finally:
            self.tearDown()

    def test_scaling_calendar_cycle_initialization_on_first_trade(self):
        """Test scaling calendar cycle initialization during first trade evaluation."""
        self.setUp()
        try:
            # Import here to ensure proper patching

            with (
                patch("novatrade.risk.scaling_plan_calendar.ScalingPlanConfig") as mock_scaling_config,
                patch("novatrade.risk.profit_cushion_protocol.ProfitCushionConfig") as mock_profit_config,
            ):
                # Configure scaling calendar mock
                scaling_config = mock_scaling_config.return_value
                scaling_config.state_file_path = self.state_file
                scaling_config.auto_save = True
                scaling_config.cycle_length_months = 4
                scaling_config.target_profit_pct = 10.0
                scaling_config.timezone = "Europe/Prague"
                scaling_config.notification_cooldown_seconds = 3600
                scaling_config.equity_change_threshold = 10.0
                scaling_config.enable_calendar_events = True
                scaling_config.reminder_days_before = [30, 14, 7, 3, 1]
                scaling_config.auto_submit_scaling = False
                scaling_config.notification_enabled = True

                # Configure profit cushion mock
                profit_config = mock_profit_config.return_value
                profit_config.state_file_path = self.profit_state_file
                profit_config.auto_save = True
                profit_config.conservative_threshold = 3.0
                profit_config.selective_threshold = 5.0
                profit_config.turtle_threshold = 8.0
                profit_config.scaling_threshold = 10.0
                profit_config.normal_multiplier = 1.0
                profit_config.conservative_multiplier = 0.75
                profit_config.selective_multiplier = 0.5
                profit_config.turtle_multiplier = 0.25
                profit_config.scaling_lock_multiplier = 0.10
                profit_config.timezone = "Europe/Prague"
                profit_config.cycle_length_months = 4
                profit_config.get_risk_multiplier = lambda profit_pct: (
                    ("scaling_lock", 0.10)
                    if profit_pct >= 10.0
                    else ("turtle", 0.25)
                    if profit_pct >= 8.0
                    else ("selective", 0.5)
                    if profit_pct >= 5.0
                    else ("conservative", 0.75)
                    if profit_pct >= 3.0
                    else ("normal", 1.0)
                )

                gate = PreTradeGate(self.cfg)

                # Create test order and account
                order = OrderRequest(
                    symbol="EURUSD",
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    volume=1.0,
                    price=1.1000,  # Add price for volume sizing calculations
                    stop_loss=1.0950,
                    take_profit=1.1100,
                )

                account = AccountState(
                    equity=100000.0,
                    balance=100000.0,
                    mode=AccountMode.DEMO,
                    free_margin=100000.0,
                    margin=0.0,
                    server="TestServer",
                )

                # Initialize profit cycle explicitly (this should init both profit cushion and scaling calendar)
                gate.start_new_profit_cycle(starting_equity=100000.0)

                # First evaluation should use the initialized systems
                gate.evaluate(order, account, [])

                # Verify scaling calendar was initialized
                assert gate._scaling_calendar.get_current_cycle() is not None

                # Get scaling calendar status
                status = gate.scaling_calendar_status
                assert status["starting_equity"] == 100000.0
                assert status["current_equity"] == 100000.0
                assert status["cycle_status"] == "active"
                assert status["is_scaling_eligible"] is False  # 0% profit initially

        finally:
            self.tearDown()

    def test_scaling_eligibility_detection_during_trading(self):
        """Test scaling eligibility detection as equity increases during trading."""
        self.setUp()
        try:
            with (
                patch("novatrade.risk.scaling_plan_calendar.ScalingPlanConfig") as mock_scaling_config,
                patch("novatrade.risk.profit_cushion_protocol.ProfitCushionConfig") as mock_profit_config,
            ):
                # Configure mocks
                scaling_config = mock_scaling_config.return_value
                scaling_config.state_file_path = self.state_file
                scaling_config.auto_save = True
                scaling_config.cycle_length_months = 4
                scaling_config.target_profit_pct = 10.0
                scaling_config.timezone = "Europe/Prague"
                scaling_config.notification_cooldown_seconds = 3600
                scaling_config.equity_change_threshold = 10.0

                profit_config = mock_profit_config.return_value
                profit_config.state_file_path = self.profit_state_file
                profit_config.auto_save = True
                profit_config.conservative_threshold = 3.0
                profit_config.selective_threshold = 5.0
                profit_config.turtle_threshold = 8.0
                profit_config.scaling_threshold = 10.0
                profit_config.normal_multiplier = 1.0
                profit_config.conservative_multiplier = 0.75
                profit_config.selective_multiplier = 0.5
                profit_config.turtle_multiplier = 0.25
                profit_config.scaling_lock_multiplier = 0.10
                profit_config.timezone = "Europe/Prague"
                profit_config.cycle_length_months = 4
                profit_config.get_risk_multiplier = lambda profit_pct: (
                    ("scaling_lock", 0.10)
                    if profit_pct >= 10.0
                    else ("turtle", 0.25)
                    if profit_pct >= 8.0
                    else ("selective", 0.5)
                    if profit_pct >= 5.0
                    else ("conservative", 0.75)
                    if profit_pct >= 3.0
                    else ("normal", 1.0)
                )

                gate = PreTradeGate(self.cfg)

                # Initialize cycles with starting equity
                gate.start_new_profit_cycle(starting_equity=99500.0)

                # Test order
                order = OrderRequest(
                    symbol="EURUSD",
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    volume=1.0,
                    price=1.1000,  # Add price for volume sizing calculations
                    stop_loss=1.0950,
                    take_profit=1.1100,
                )

                # Test progression through profit levels (starting from 99500.0)
                test_cases = [
                    (100000.0, False, "active"),  # 0.5% profit
                    (102000.0, False, "active"),  # 2.5% profit
                    (105000.0, False, "active"),  # 5.5% profit
                    (108000.0, False, "active"),  # 8.5% profit
                    (109450.0, True, "scaling_eligible"),  # 10% profit - scaling eligible!
                    (111000.0, True, "scaling_eligible"),  # 11.6% profit - still eligible
                ]

                for equity, expected_eligible, expected_status in test_cases:
                    account = AccountState(
                        equity=equity,
                        balance=equity,
                        mode=AccountMode.DEMO,
                        free_margin=equity,
                        margin=0.0,
                        server="TestServer",
                    )

                    # Evaluate order to trigger updates
                    gate.evaluate(order, account, [])

                    # Check scaling eligibility
                    assert gate.is_scaling_eligible == expected_eligible

                    # Check scaling calendar status
                    status = gate.scaling_calendar_status
                    assert status["current_equity"] == equity
                    assert status["cycle_status"] == expected_status
                    assert status["is_scaling_eligible"] == expected_eligible

        finally:
            self.tearDown()

    def test_integrated_risk_management_with_scaling_calendar(self):
        """Test integration of profit cushion + scaling calendar risk management."""
        self.setUp()
        try:
            with (
                patch("novatrade.risk.scaling_plan_calendar.ScalingPlanConfig") as mock_scaling_config,
                patch("novatrade.risk.profit_cushion_protocol.ProfitCushionConfig") as mock_profit_config,
            ):
                # Configure mocks
                scaling_config = mock_scaling_config.return_value
                scaling_config.state_file_path = self.state_file
                scaling_config.auto_save = True
                scaling_config.cycle_length_months = 1  # Shorter cycle for testing
                scaling_config.target_profit_pct = 10.0
                scaling_config.timezone = "Europe/Prague"
                scaling_config.enable_calendar_events = True
                scaling_config.reminder_days_before = [14, 7, 3, 1]  # Shorter reminders
                scaling_config.auto_submit_scaling = False
                scaling_config.notification_enabled = True

                profit_config = mock_profit_config.return_value
                profit_config.state_file_path = self.profit_state_file
                profit_config.auto_save = True
                profit_config.conservative_threshold = 3.0
                profit_config.selective_threshold = 5.0
                profit_config.turtle_threshold = 8.0
                profit_config.scaling_threshold = 10.0
                profit_config.normal_multiplier = 1.0
                profit_config.conservative_multiplier = 0.75
                profit_config.selective_multiplier = 0.5
                profit_config.turtle_multiplier = 0.25
                profit_config.scaling_lock_multiplier = 0.10
                profit_config.timezone = "Europe/Prague"
                profit_config.cycle_length_months = 4
                profit_config.get_risk_multiplier = lambda profit_pct: (
                    ("scaling_lock", 0.10)
                    if profit_pct >= 10.0
                    else ("turtle", 0.25)
                    if profit_pct >= 8.0
                    else ("selective", 0.5)
                    if profit_pct >= 5.0
                    else ("conservative", 0.75)
                    if profit_pct >= 3.0
                    else ("normal", 1.0)
                )

                gate = PreTradeGate(self.cfg)
                gate.start_new_profit_cycle(starting_equity=100000.0)

                order = OrderRequest(
                    symbol="EURUSD",
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    volume=0.15,  # Reduced volume to pass scaling lock risk checks
                    price=1.1000,  # Add price for volume sizing calculations
                    stop_loss=1.0950,
                    take_profit=1.1100,
                )

                # Test at 10% profit (scaling eligible + scaling lock tier)
                account = AccountState(
                    equity=110000.0,  # 10% profit
                    balance=110000.0,
                    mode=AccountMode.DEMO,
                    free_margin=110000.0,
                    margin=0.0,
                    server="TestServer",
                )

                gate.evaluate(order, account, [])

                # Verify integrated status
                assert gate.is_scaling_eligible is True  # Scaling calendar
                assert gate.profit_cushion_tier == "scaling_lock"  # Profit cushion
                assert gate.profit_cushion_multiplier == 0.10  # 90% risk reduction

                # Verify scaling calendar status includes profit information
                scaling_status = gate.scaling_calendar_status
                assert scaling_status["current_profit_pct"] == 10.0
                assert scaling_status["is_scaling_eligible"] is True
                assert scaling_status["cycle_status"] == "scaling_eligible"

                # Verify upcoming events include scaling deadline
                upcoming_events = gate.upcoming_scaling_events
                assert len(upcoming_events) > 0

                # Should have submission deadline in upcoming events
                deadline_events = [e for e in upcoming_events if e["type"] == "submission_deadline"]
                assert len(deadline_events) > 0

        finally:
            self.tearDown()

    def test_scaling_calendar_properties_and_accessors(self):
        """Test PreTradeGate property accessors for scaling calendar."""
        self.setUp()
        try:
            with (
                patch("novatrade.risk.scaling_plan_calendar.ScalingPlanConfig") as mock_scaling_config,
                patch("novatrade.risk.profit_cushion_protocol.ProfitCushionConfig") as mock_profit_config,
            ):
                # Configure mocks
                scaling_config = mock_scaling_config.return_value
                scaling_config.state_file_path = self.state_file
                scaling_config.auto_save = True
                scaling_config.cycle_length_months = 4
                scaling_config.target_profit_pct = 10.0
                scaling_config.timezone = "Europe/Prague"
                scaling_config.notification_cooldown_seconds = 3600
                scaling_config.equity_change_threshold = 10.0

                profit_config = mock_profit_config.return_value
                profit_config.state_file_path = self.profit_state_file
                profit_config.auto_save = True
                profit_config.conservative_threshold = 3.0
                profit_config.timezone = "Europe/Prague"
                profit_config.cycle_length_months = 4
                profit_config.get_risk_multiplier = lambda profit_pct: (
                    ("scaling_lock", 0.10)
                    if profit_pct >= 10.0
                    else ("turtle", 0.25)
                    if profit_pct >= 8.0
                    else ("selective", 0.5)
                    if profit_pct >= 5.0
                    else ("conservative", 0.75)
                    if profit_pct >= 3.0
                    else ("normal", 1.0)
                )

                gate = PreTradeGate(self.cfg)
                gate.start_new_profit_cycle(starting_equity=100000.0)

                # Test property accessors before scaling eligibility
                assert gate.is_scaling_eligible is False
                assert gate.scaling_deadline is not None
                assert isinstance(gate.scaling_deadline, str)  # ISO format
                assert gate.upcoming_scaling_events == []  # No immediate events

                # Update to scaling eligible and test again
                order = OrderRequest(
                    symbol="EURUSD",
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    volume=1.0,
                    price=1.1000,  # Add price for volume sizing calculations
                    stop_loss=1.0950,  # Add stop_loss for volume sizing
                )

                account = AccountState(
                    equity=110000.0,
                    balance=110000.0,
                    mode=AccountMode.DEMO,
                    free_margin=110000.0,
                    margin=0.0,
                    server="TestServer",
                )

                gate.evaluate(order, account, [])

                # Test properties after scaling eligibility
                assert gate.is_scaling_eligible is True

                status = gate.scaling_calendar_status
                assert status["cycle_status"] == "scaling_eligible"
                assert status["is_scaling_eligible"] is True
                assert status["current_profit_pct"] == 10.0

        finally:
            self.tearDown()

    def test_scaling_calendar_error_handling(self):
        """Test error handling when scaling calendar fails to initialize."""
        self.setUp()
        try:
            # Force scaling calendar initialization failure
            with patch(
                "novatrade.risk.scaling_plan_calendar.ScalingPlanCalendar",
                side_effect=Exception("Calendar init failed"),
            ):
                gate = PreTradeGate(self.cfg)

                # Should handle initialization failure gracefully
                assert gate._scaling_calendar is None

                # Properties should handle missing calendar gracefully
                assert gate.scaling_calendar_status["error"] == "Scaling calendar not initialized"
                assert gate.is_scaling_eligible is False
                assert gate.scaling_deadline is None
                assert gate.upcoming_scaling_events == []

        finally:
            self.tearDown()

    def test_scaling_calendar_state_persistence(self):
        """Test scaling calendar state persistence across PreTradeGate instances."""
        self.setUp()
        try:
            with (
                patch("novatrade.risk.scaling_plan_calendar.ScalingPlanConfig") as mock_scaling_config,
                patch("novatrade.risk.profit_cushion_protocol.ProfitCushionConfig") as mock_profit_config,
            ):
                # Configure mocks
                scaling_config = mock_scaling_config.return_value
                scaling_config.state_file_path = self.state_file
                scaling_config.auto_save = True
                scaling_config.cycle_length_months = 4
                scaling_config.target_profit_pct = 10.0
                scaling_config.timezone = "Europe/Prague"
                scaling_config.notification_cooldown_seconds = 3600
                scaling_config.equity_change_threshold = 10.0

                profit_config = mock_profit_config.return_value
                profit_config.state_file_path = self.profit_state_file
                profit_config.auto_save = True
                profit_config.timezone = "Europe/Prague"
                profit_config.cycle_length_months = 4
                profit_config.get_risk_multiplier = lambda profit_pct: (
                    ("scaling_lock", 0.10)
                    if profit_pct >= 10.0
                    else ("turtle", 0.25)
                    if profit_pct >= 8.0
                    else ("selective", 0.5)
                    if profit_pct >= 5.0
                    else ("conservative", 0.75)
                    if profit_pct >= 3.0
                    else ("normal", 1.0)
                )

                # Create first gate and establish scaling state
                gate1 = PreTradeGate(self.cfg)
                gate1.start_new_profit_cycle(starting_equity=100000.0)

                # Update to scaling eligible
                order = OrderRequest(
                    symbol="EURUSD",
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    volume=1.0,
                    price=1.1000,  # Add price for volume sizing calculations
                    stop_loss=1.0950,  # Add stop_loss for volume sizing
                )

                account = AccountState(
                    equity=110000.0,
                    balance=110000.0,
                    mode=AccountMode.DEMO,
                    free_margin=110000.0,
                    margin=0.0,
                    server="TestServer",
                )

                gate1.evaluate(order, account, [])

                # Verify scaling eligibility
                assert gate1.is_scaling_eligible is True

                # Create second gate instance (should load existing state)
                gate2 = PreTradeGate(self.cfg)

                # Verify state was persisted and loaded
                assert gate2._scaling_calendar.get_current_cycle() is not None
                status = gate2.scaling_calendar_status
                assert status["current_equity"] == 110000.0
                assert status["is_scaling_eligible"] is True
                assert status["cycle_status"] == "scaling_eligible"

        finally:
            self.tearDown()
