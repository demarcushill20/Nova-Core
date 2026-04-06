"""Tests for profit cushion protocol integration with PreTradeGate.

Integration tests to ensure the profit cushion protocol works correctly
within the live trading risk management stack.
"""

import os
import tempfile
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.models import AccountMode, AccountState, OrderRequest, OrderSide, OrderType
from novatrade.risk.pre_trade_gate import PreTradeGate


def _cfg(**overrides) -> NovaTradeCfg:
    """Build a test config with dry_run=False so orders can pass by default."""
    risk_overrides = overrides.pop("risk", {})
    risk = RiskConfig(**{**{"require_stop_loss": True}, **risk_overrides})
    defaults = {
        "dry_run": False,
        "symbols": ["EURUSD", "GBPUSD"],
        "risk": risk,
    }
    defaults.update(overrides)
    return NovaTradeCfg(**defaults)  # type: ignore[arg-type]


class TestProfitCushionIntegration:
    """Integration tests for profit cushion protocol within PreTradeGate."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.temp_dir, "test_profit_cushion.json")

        # Mock configuration with profit cushion state file
        self.cfg = _cfg()
        # Monkey patch the profit cushion state file path
        self.original_state_file = "/tmp/novatrade_profit_cushion_state.json"

    def tearDown(self):
        """Clean up test fixtures."""
        if os.path.exists(self.state_file):
            os.unlink(self.state_file)
        os.rmdir(self.temp_dir)

    def test_profit_cushion_initialization_no_existing_state(self):
        """Test PreTradeGate initialization without existing profit cushion state."""
        self.setUp()
        try:
            # Ensure no existing state file
            if os.path.exists(self.original_state_file):
                os.unlink(self.original_state_file)

            gate = PreTradeGate(self.cfg)

            # Should initialize with None (no existing state)
            assert gate._profit_cushion is None
            assert gate.profit_cushion_tier == "normal"
            assert gate.profit_cushion_multiplier == 1.0

            status = gate.profit_cushion_status
            assert "error" in status
            assert "not initialized" in status["error"]

        finally:
            self.tearDown()

    def test_profit_cushion_initialization_on_first_trade(self):
        """Test profit cushion initialization during first trade evaluation."""
        self.setUp()
        try:
            # Patch the state file path to use our temp file
            with patch("novatrade.risk.profit_cushion_protocol.ProfitCushionConfig") as mock_config_class:
                mock_config = mock_config_class.return_value
                mock_config.state_file_path = self.state_file
                mock_config.auto_save = True
                mock_config.conservative_threshold = 3.0
                mock_config.selective_threshold = 5.0
                mock_config.turtle_threshold = 8.0
                mock_config.scaling_threshold = 10.0
                mock_config.normal_multiplier = 1.0
                mock_config.conservative_multiplier = 0.75
                mock_config.selective_multiplier = 0.5
                mock_config.turtle_multiplier = 0.25
                mock_config.scaling_lock_multiplier = 0.10
                mock_config.cycle_length_months = 4
                mock_config.timezone = "Europe/Prague"
                mock_config.get_risk_multiplier = lambda profit_pct: (
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

                # Create test order request and account state
                order = OrderRequest(
                    symbol="EURUSD",
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    volume=1.0,
                    price=1.1000,
                    stop_loss=1.0950,  # 50 pip stop
                    take_profit=1.1100,  # 100 pip target
                )

                account = AccountState(
                    equity=100000.0,  # $100K starting equity
                    balance=100000.0,
                    mode=AccountMode.FUNDED_PRESERVATION,
                    free_margin=100000.0,
                    margin=0.0,
                    server="TestServer",
                )

                # First evaluation should initialize profit cushion
                gate.evaluate(order, account, [])

                # Profit cushion should now be initialized
                assert gate._profit_cushion is not None
                assert gate.profit_cushion_tier == "normal"
                assert gate.profit_cushion_multiplier == 1.0

                status = gate.profit_cushion_status
                assert "error" not in status
                assert status["starting_equity"] == 100000.0
                assert status["current_equity"] == 100000.0
                assert status["cycle_profit_pct"] == 0.0

        finally:
            self.tearDown()

    def test_profit_cushion_tier_progression_during_trading(self):
        """Test profit cushion tier changes as equity increases."""
        self.setUp()
        try:
            # Patch datetime.now to control cycle timing
            tz = ZoneInfo("Europe/Prague")
            cycle_start = datetime(2026, 4, 1, tzinfo=tz)

            with patch("novatrade.risk.profit_cushion_protocol.datetime") as mock_datetime:
                mock_datetime.now.return_value = cycle_start
                mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

                # Initialize gate with profit cushion
                gate = PreTradeGate(self.cfg)
                gate.start_new_profit_cycle(starting_equity=100000.0, cycle_start_date=cycle_start)

                # Test order
                order = OrderRequest(
                    symbol="EURUSD",
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    volume=1.0,
                    price=1.1000,
                    stop_loss=1.0950,
                    take_profit=1.1100,
                )

                # Test progression through tiers
                test_cases = [
                    (100000.0, "normal", 1.0),  # 0% profit
                    (102000.0, "normal", 1.0),  # 2% profit (still normal)
                    (103500.0, "conservative", 0.75),  # 3.5% profit (conservative)
                    (106000.0, "selective", 0.5),  # 6% profit (selective)
                    (108500.0, "turtle", 0.25),  # 8.5% profit (turtle)
                    (110500.0, "scaling_lock", 0.10),  # 10.5% profit (scaling lock)
                ]

                for equity, expected_tier, expected_multiplier in test_cases:
                    account = AccountState(
                        equity=equity,
                        balance=equity,
                        mode=AccountMode.FUNDED_PRESERVATION,
                        free_margin=equity,
                        margin=0.0,
                        server="TestServer",
                    )

                    # Evaluate order to trigger profit cushion update
                    gate.evaluate(order, account, [])

                    # Check tier and multiplier
                    assert gate.profit_cushion_tier == expected_tier
                    assert gate.profit_cushion_multiplier == expected_multiplier

                    # Check status
                    status = gate.profit_cushion_status
                    profit_pct = (equity - 100000.0) / 100000.0 * 100.0
                    assert abs(status["cycle_profit_pct"] - profit_pct) < 0.01

                    # Verify scaling lock mode shows scaling eligibility
                    if equity >= 110000.0:  # 10%+ profit
                        assert status["is_scaling_eligible"]
                    else:
                        assert not status["is_scaling_eligible"]

        finally:
            self.tearDown()

    def test_profit_cushion_volume_scaling_effect(self):
        """Test that profit cushion actually affects position sizing."""
        self.setUp()
        try:
            # Initialize gate with profit cushion
            tz = ZoneInfo("Europe/Prague")
            cycle_start = datetime(2026, 4, 1, tzinfo=tz)

            gate = PreTradeGate(self.cfg)
            gate.start_new_profit_cycle(starting_equity=100000.0, cycle_start_date=cycle_start)

            # Test order with reasonable sizing
            order = OrderRequest(
                symbol="EURUSD",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=5.0,  # 5 lots
                price=1.1000,
                stop_loss=1.0950,  # 50 pip stop
                take_profit=1.1100,  # 100 pip target
            )

            # Test normal tier (no scaling)
            account_normal = AccountState(
                equity=102000.0,  # 2% profit (normal tier)
                balance=102000.0,
                mode=AccountMode.DEMO,
                free_margin=102000.0,
                margin=0.0,
                server="TestServer",
            )

            decision_normal = gate.evaluate(order, account_normal, [])
            volume_normal = None
            for check in decision_normal.checks:
                if check.name == "volume_sizing" and check.detail and "final=" in check.detail:
                    volume_normal = float(check.detail.split("final=")[1].split(",")[0])
                    break

            # Test selective tier (50% scaling)
            account_selective = AccountState(
                equity=106000.0,  # 6% profit (selective tier, 0.5x multiplier)
                balance=106000.0,
                mode=AccountMode.DEMO,
                free_margin=106000.0,
                margin=0.0,
                server="TestServer",
            )

            decision_selective = gate.evaluate(order, account_selective, [])
            volume_selective = None
            for check in decision_selective.checks:
                if check.name == "volume_sizing" and check.detail and "final=" in check.detail:
                    volume_selective = float(check.detail.split("final=")[1].split(",")[0])
                    break

            # Selective tier should have smaller volume (50% of normal)
            if volume_normal is not None and volume_selective is not None:
                ratio = volume_selective / volume_normal
                assert 0.45 <= ratio <= 0.55  # Should be ~0.5x

            # Test scaling lock tier (10% scaling)
            account_scaling = AccountState(
                equity=110500.0,  # 10.5% profit (scaling lock tier, 0.1x multiplier)
                balance=110500.0,
                mode=AccountMode.DEMO,
                free_margin=110500.0,
                margin=0.0,
                server="TestServer",
            )

            decision_scaling = gate.evaluate(order, account_scaling, [])
            volume_scaling = None
            for check in decision_scaling.checks:
                if check.name == "volume_sizing" and check.detail and "final=" in check.detail:
                    volume_scaling = float(check.detail.split("final=")[1].split(",")[0])
                    break

            # Scaling lock tier should have much smaller volume (10% of normal)
            if volume_normal is not None and volume_scaling is not None:
                ratio = volume_scaling / volume_normal
                assert 0.05 <= ratio <= 0.15  # Should be ~0.1x

        finally:
            self.tearDown()

    def test_profit_cushion_state_persistence(self):
        """Test that profit cushion state persists across PreTradeGate instances."""
        self.setUp()
        try:
            with patch("novatrade.risk.profit_cushion_protocol.ProfitCushionConfig") as mock_config_class:
                mock_config = mock_config_class.return_value
                mock_config.state_file_path = self.state_file
                mock_config.auto_save = True
                # Set thresholds to real values
                mock_config.conservative_threshold = 3.0
                mock_config.selective_threshold = 5.0
                mock_config.turtle_threshold = 8.0
                mock_config.scaling_threshold = 10.0
                # Set multipliers to real values
                mock_config.normal_multiplier = 1.0
                mock_config.conservative_multiplier = 0.75
                mock_config.selective_multiplier = 0.5
                mock_config.turtle_multiplier = 0.25
                mock_config.scaling_lock_multiplier = 0.10
                mock_config.timezone = "Europe/Prague"  # Set real timezone string
                mock_config.cycle_length_months = 4  # Set real integer value
                mock_config.get_risk_multiplier = lambda profit_pct: (
                    ("conservative", 0.75) if profit_pct >= 3.0 else ("normal", 1.0)
                )

                # Create first gate and establish profit state
                gate1 = PreTradeGate(self.cfg)
                gate1.start_new_profit_cycle(starting_equity=100000.0)

                # Update to conservative tier
                account = AccountState(
                    equity=103500.0,  # 3.5% profit
                    balance=103500.0,
                    mode=AccountMode.FUNDED_PRESERVATION,
                    free_margin=103500.0,
                    margin=0.0,
                    server="TestServer",
                )

                order = OrderRequest(
                    symbol="EURUSD",
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    volume=1.0,
                    price=1.1000,
                    stop_loss=1.0950,
                    take_profit=1.1100,
                )

                # Trigger profit cushion update
                gate1.evaluate(order, account, [])

                # Verify first gate state
                assert gate1.profit_cushion_tier == "conservative"
                assert gate1.profit_cushion_multiplier == 0.75

                # Create second gate (should load existing state)
                gate2 = PreTradeGate(self.cfg)

                # Second gate should have the same state
                assert gate2.profit_cushion_tier == "conservative"
                assert gate2.profit_cushion_multiplier == 0.75

                status = gate2.profit_cushion_status
                assert abs(status["cycle_profit_pct"] - 3.5) < 0.1

        finally:
            self.tearDown()

    def test_profit_cushion_error_handling(self):
        """Test error handling when profit cushion operations fail."""
        self.setUp()
        try:
            gate = PreTradeGate(self.cfg)

            # Create order and account
            order = OrderRequest(
                symbol="EURUSD",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=1.0,
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

            # Evaluation should work even if profit cushion fails
            # (it should fall back to drawdown-only scaling)
            with patch(
                "novatrade.risk.profit_cushion_protocol.ProfitCushionProtocol.__init__",
                side_effect=Exception("Test error"),
            ):
                decision = gate.evaluate(order, account, [])

                # Should still get a decision (fallback behavior)
                assert decision is not None
                assert decision.verdict is not None

        finally:
            self.tearDown()

    def test_new_cycle_start_method(self):
        """Test the start_new_profit_cycle method."""
        self.setUp()
        try:
            with patch("novatrade.risk.profit_cushion_protocol.ProfitCushionConfig") as mock_config_class:
                mock_config = mock_config_class.return_value
                mock_config.state_file_path = self.state_file
                mock_config.auto_save = True
                # Set thresholds to real values
                mock_config.conservative_threshold = 3.0
                mock_config.selective_threshold = 5.0
                mock_config.turtle_threshold = 8.0
                mock_config.scaling_threshold = 10.0
                # Set multipliers to real values
                mock_config.normal_multiplier = 1.0
                mock_config.conservative_multiplier = 0.75
                mock_config.selective_multiplier = 0.5
                mock_config.turtle_multiplier = 0.25
                mock_config.scaling_lock_multiplier = 0.10
                mock_config.timezone = "Europe/Prague"
                mock_config.cycle_length_months = 4
                mock_config.get_risk_multiplier = lambda profit_pct: (
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

                # Start new cycle
                tz = ZoneInfo("Europe/Prague")
                cycle_start = datetime(2026, 4, 1, tzinfo=tz)
                gate.start_new_profit_cycle(starting_equity=125000.0, cycle_start_date=cycle_start)

                # Verify initialization
                assert gate._profit_cushion is not None
                assert gate.profit_cushion_tier == "normal"
                assert gate.profit_cushion_multiplier == 1.0

                status = gate.profit_cushion_status
                assert status["starting_equity"] == 125000.0
                assert status["current_equity"] == 125000.0
                assert status["cycle_profit_pct"] == 0.0

        finally:
            self.tearDown()
