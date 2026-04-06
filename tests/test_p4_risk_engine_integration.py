"""Integration tests for P4: Enhanced Session-Aware Risk Management

Tests the integration of session-aware filtering with the RiskEngine,
ensuring that the enhanced session checking works correctly within
the full risk management pipeline.

Test coverage:
- Risk engine session checking with enhanced filter
- Configuration-driven session filter creation
- Integration with pre-trade risk evaluation
- Session check results in risk decisions
- Backward compatibility with existing tests
"""

import datetime as dt
from unittest.mock import Mock, patch

import pytest

from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.models import AccountState, OrderRequest, OrderSide, OrderType, RiskVerdict
from novatrade.risk.risk_engine import RiskEngine


class TestRiskEngineSessionIntegration:
    """Test session-aware filtering integration with RiskEngine."""

    def create_test_risk_engine(self, session_config=None):
        """Create a test risk engine with session configuration."""
        cfg = NovaTradeCfg()

        # Apply session configuration if provided
        if session_config:
            for key, value in session_config.items():
                setattr(cfg.risk, key, value)

        return RiskEngine(cfg)

    def test_session_filter_creation_from_config(self):
        """Test that session filter is created based on configuration."""
        # Test default configuration
        engine = self.create_test_risk_engine()
        assert engine._session_filter is not None

        # Test overlap-only configuration
        engine_overlap = self.create_test_risk_engine(
            {"session_overlap_only": True, "session_minimum_quality": "premium"}
        )
        assert engine_overlap._session_filter is not None

        # Test with Asian session disabled
        engine_no_asian = self.create_test_risk_engine(
            {"session_allow_asian": False, "session_minimum_quality": "good"}
        )
        assert engine_no_asian._session_filter is not None

    def test_session_checking_enabled_by_default(self):
        """Test that session checking is enabled by default (P4 change)."""
        cfg = NovaTradeCfg()
        assert cfg.risk.check_forex_session is True  # Should be True after P4

    @patch("novatrade.risk.risk_engine._default_utc_now")
    def test_weekend_blocking_in_risk_engine(self, mock_clock):
        """Test that weekend times are properly blocked by risk engine."""
        # Saturday 12:00 UTC
        saturday = dt.datetime(2026, 4, 5, 12, 0, tzinfo=dt.timezone.utc)
        mock_clock.return_value = saturday

        engine = self.create_test_risk_engine({"check_forex_session": True})

        # Initialize engine
        account = AccountState(balance=100000, equity=100000, margin=0, free_margin=100000)
        engine.initialize(account)

        # Create a test order request
        request = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=1.0,
            order_type=OrderType.MARKET,
        )

        # Check pre-trade decision
        decision = engine.pre_trade_check(request, account, [])

        # Should be denied due to weekend
        assert decision.verdict == RiskVerdict.DENY
        assert decision.rule == "forex_session"
        assert "weekend" in decision.reason.lower() or "closed" in decision.reason.lower()

    @patch("novatrade.risk.risk_engine._default_utc_now")
    def test_overlap_session_allowed(self, mock_clock):
        """Test that London-NY overlap sessions are allowed."""
        # Wednesday 14:00 UTC - London-NY overlap
        overlap_time = dt.datetime(2026, 4, 2, 14, 0, tzinfo=dt.timezone.utc)
        mock_clock.return_value = overlap_time

        engine = self.create_test_risk_engine({"check_forex_session": True})

        # Initialize engine
        account = AccountState(balance=100000, equity=100000, margin=0, free_margin=100000)
        engine.initialize(account)

        # Mock the gate to always pass (focus on session checking)
        engine._gate.check = Mock(return_value=Mock(passed=True, detail="Mock pass"))

        # Create a test order request
        request = OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=1.0,
            order_type=OrderType.MARKET,
        )

        # Check pre-trade decision
        decision = engine.pre_trade_check(request, account, [])

        # Should be allowed (session check passed)
        # Note: May still fail on other checks, but session should pass
        session_check = next((check for check in decision.checks if check.name == "forex_session"), None)
        assert session_check is not None
        assert session_check.passed
        assert "premium" in session_check.detail.lower() or "overlap" in session_check.detail.lower()

    @patch("novatrade.risk.risk_engine._default_utc_now")
    def test_asian_session_behavior(self, mock_clock):
        """Test behavior during Asian session with different configurations."""
        # Wednesday 04:00 UTC - Asian session
        asian_time = dt.datetime(2026, 4, 2, 4, 0, tzinfo=dt.timezone.utc)
        mock_clock.return_value = asian_time

        # Test with Asian session allowed (default)
        engine_allow = self.create_test_risk_engine(
            {"check_forex_session": True, "session_allow_asian": True, "session_minimum_quality": "acceptable"}
        )

        account = AccountState(balance=100000, equity=100000, margin=0, free_margin=100000)
        engine_allow.initialize(account)

        # Mock the gate to always pass
        engine_allow._gate.check = Mock(return_value=Mock(passed=True, detail="Mock pass"))

        request = OrderRequest(symbol="EURUSD", side=OrderSide.BUY, volume=1.0, order_type=OrderType.MARKET)
        decision_allow = engine_allow.pre_trade_check(request, account, [])

        session_check_allow = next((c for c in decision_allow.checks if c.name == "forex_session"), None)
        assert session_check_allow is not None
        assert session_check_allow.passed  # Should pass with Asian allowed

        # Test with Asian session disabled
        engine_block = self.create_test_risk_engine(
            {
                "check_forex_session": True,
                "session_allow_asian": False,
            }
        )

        engine_block.initialize(account)
        decision_block = engine_block.pre_trade_check(request, account, [])

        assert decision_block.verdict == RiskVerdict.DENY
        assert decision_block.rule == "forex_session"

    @patch("novatrade.risk.risk_engine._default_utc_now")
    def test_overlap_only_configuration(self, mock_clock):
        """Test overlap-only configuration blocks non-overlap sessions."""
        # Wednesday 10:00 UTC - London session (not overlap)
        london_time = dt.datetime(2026, 4, 2, 10, 0, tzinfo=dt.timezone.utc)
        mock_clock.return_value = london_time

        engine = self.create_test_risk_engine(
            {
                "check_forex_session": True,
                "session_overlap_only": True,
            }
        )

        account = AccountState(balance=100000, equity=100000, margin=0, free_margin=100000)
        engine.initialize(account)

        request = OrderRequest(symbol="EURUSD", side=OrderSide.BUY, volume=1.0, order_type=OrderType.MARKET)
        decision = engine.pre_trade_check(request, account, [])

        # Should be denied (not in overlap)
        assert decision.verdict == RiskVerdict.DENY
        assert decision.rule == "forex_session"

    def test_session_checking_disabled(self):
        """Test that session checking can still be disabled."""
        engine = self.create_test_risk_engine({"check_forex_session": False})

        account = AccountState(balance=100000, equity=100000, margin=0, free_margin=100000)
        engine.initialize(account)

        # Mock the gate to always pass
        engine._gate.check = Mock(return_value=Mock(passed=True, detail="Mock pass"))

        # Even on a weekend, should not be blocked by session check when disabled
        with patch("novatrade.risk.risk_engine._default_utc_now") as mock_clock:
            saturday = dt.datetime(2026, 4, 5, 12, 0, tzinfo=dt.timezone.utc)
            mock_clock.return_value = saturday

            request = OrderRequest(symbol="EURUSD", side=OrderSide.BUY, volume=1.0, order_type=OrderType.MARKET)
            decision = engine.pre_trade_check(request, account, [])

            # Session check should not even be in the checks list when disabled
            session_checks = [c for c in decision.checks if c.name == "forex_session"]
            assert len(session_checks) == 0

    @pytest.mark.parametrize(
        "quality,expected_pass",
        [
            ("acceptable", True),
            ("good", False),  # Asian is acceptable, so should fail with good minimum
            ("premium", False),  # Asian is acceptable, so should fail with premium minimum
        ],
    )
    @patch("novatrade.risk.risk_engine._default_utc_now")
    def test_minimum_quality_thresholds(self, mock_clock, quality, expected_pass):
        """Test different minimum quality thresholds."""
        # Asian session time
        asian_time = dt.datetime(2026, 4, 2, 4, 0, tzinfo=dt.timezone.utc)
        mock_clock.return_value = asian_time

        engine = self.create_test_risk_engine(
            {
                "check_forex_session": True,
                "session_minimum_quality": quality,
                "session_allow_asian": True,
            }
        )

        account = AccountState(balance=100000, equity=100000, margin=0, free_margin=100000)
        engine.initialize(account)

        if expected_pass:
            # Mock gate to pass so we can test session logic
            engine._gate.check = Mock(return_value=Mock(passed=True, detail="Mock pass"))

        request = OrderRequest(symbol="EURUSD", side=OrderSide.BUY, volume=1.0, order_type=OrderType.MARKET)
        decision = engine.pre_trade_check(request, account, [])

        if expected_pass:
            session_check = next((c for c in decision.checks if c.name == "forex_session"), None)
            assert session_check is not None
            assert session_check.passed
        else:
            assert decision.verdict == RiskVerdict.DENY
            assert decision.rule == "forex_session"

    def test_config_quality_string_mapping(self):
        """Test that quality string values are properly mapped to enums."""

        # Test all valid quality strings
        quality_configs = ["minimum", "acceptable", "good", "premium"]

        for quality_str in quality_configs:
            engine = self.create_test_risk_engine({"session_minimum_quality": quality_str})

            # Verify the filter was created successfully
            assert engine._session_filter is not None

            # The actual mapping is tested in the filter creation method,
            # but we want to ensure no exceptions are thrown

    def test_backward_compatibility(self):
        """Test that existing functionality is not broken by P4 changes."""
        # Test with session checking disabled (old behavior)
        engine = self.create_test_risk_engine({"check_forex_session": False})

        account = AccountState(balance=100000, equity=100000, margin=0, free_margin=100000)
        engine.initialize(account)

        # Mock other checks to pass
        engine._gate.check = Mock(return_value=Mock(passed=True, detail="Mock pass"))

        request = OrderRequest(symbol="EURUSD", side=OrderSide.BUY, volume=1.0, order_type=OrderType.MARKET)

        # Should work the same as before (no session checking)
        decision = engine.pre_trade_check(request, account, [])

        # No forex_session checks should be present
        session_checks = [c for c in decision.checks if c.name == "forex_session"]
        assert len(session_checks) == 0


class TestFirstTradeValidationWiring:
    """Test first trade validation is wired into risk engine on_trade_fill."""

    def create_engine(self):
        """Create a risk engine with session check disabled for simpler testing."""
        cfg = NovaTradeCfg()
        cfg.risk.check_forex_session = False
        engine = RiskEngine(cfg)
        account = AccountState(balance=100000, equity=100000, margin=0, free_margin=100000)
        engine.initialize(account)
        return engine

    def test_first_trade_validation_runs_on_fill(self):
        """First trade validation should run when on_trade_fill is called
        after prepare_first_trade_checks was called during pre-trade."""
        engine = self.create_engine()

        # Prepare expected values (as would happen during pre_trade_check)
        engine._gate._first_trade_validator.prepare_first_trade_checks(
            entry_price=1.10150,
            sl_price=1.10000,
            volume=0.05,
            typical_spread_pips=1.0,
        )

        # Simulate trade fill — should trigger first trade validation
        engine.on_trade_fill(
            position_id="test_pos_1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=0.05,
            fill_price=1.10150,
            stop_loss=1.10000,
        )

        # Validator should have marked first trade as completed
        assert not engine._gate._first_trade_validator.is_first_trade_pending

    def test_first_trade_validation_passes_with_good_fill(self):
        """Good fill should result in passed validation."""
        engine = self.create_engine()

        engine._gate._first_trade_validator.prepare_first_trade_checks(
            entry_price=1.10150,
            sl_price=1.10000,
            volume=0.05,
            typical_spread_pips=1.0,
        )

        # Fill with exact expected values — should pass
        engine.on_trade_fill(
            position_id="test_pos_1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=0.05,
            fill_price=1.10150,
            stop_loss=1.10000,
        )

        # Position should still be tracked (validation doesn't block)
        assert "test_pos_1" in engine._position_risks

    def test_first_trade_validation_logs_bad_fill(self):
        """Bad fill should log errors but not block the trade."""
        engine = self.create_engine()

        engine._gate._first_trade_validator.prepare_first_trade_checks(
            entry_price=1.10150,
            sl_price=1.10000,
            volume=0.05,
            typical_spread_pips=1.0,
        )

        # Fill with significantly deviated values (10 pip slippage, wrong volume)
        engine.on_trade_fill(
            position_id="test_pos_1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=0.10,  # 100% volume deviation
            fill_price=1.10250,  # 10 pip slippage
            stop_loss=1.10050,  # 5 pip SL deviation
        )

        # Position should still be tracked (validation is non-blocking)
        assert "test_pos_1" in engine._position_risks
        # Validator stays pending on failure — will re-validate next trade
        # until integration proves healthy
        assert engine._gate._first_trade_validator.is_first_trade_pending

    def test_second_trade_skips_validation(self):
        """Second trade should skip first trade validation entirely."""
        engine = self.create_engine()

        # First trade
        engine._gate._first_trade_validator.prepare_first_trade_checks(
            entry_price=1.10150,
            sl_price=1.10000,
            volume=0.05,
            typical_spread_pips=1.0,
        )
        engine.on_trade_fill(
            position_id="pos_1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=0.05,
            fill_price=1.10150,
            stop_loss=1.10000,
        )

        # Close first trade
        engine.on_trade_close(
            position_id="pos_1",
            symbol="EURUSD",
            side="BUY",
            volume=0.05,
            pnl_usd=50.0,
            pnl_pips=10.0,
            exit_reason="SL_HIT",
        )

        # Second trade — validation should be a no-op
        engine.on_trade_fill(
            position_id="pos_2",
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=0.05,
            fill_price=1.10500,
            stop_loss=1.10350,
        )

        # Both positions processed fine
        assert "pos_2" in engine._position_risks

    def test_first_trade_validation_resilient_to_errors(self):
        """Validation errors should not block trade processing."""
        engine = self.create_engine()

        # Don't prepare first trade checks — validator not initialized
        # This should not crash on_trade_fill
        engine.on_trade_fill(
            position_id="test_pos_1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=0.05,
            fill_price=1.10150,
            stop_loss=1.10000,
        )

        # Position should still be tracked
        assert "test_pos_1" in engine._position_risks


class TestP4ConfigDefaults:
    """Test that P4 configuration defaults are set correctly."""

    def test_session_filtering_enabled_by_default(self):
        """Test that session filtering is enabled by default in P4."""
        cfg = NovaTradeCfg()
        assert cfg.risk.check_forex_session is True

    def test_session_filter_defaults(self):
        """Test default values for new session filter configuration."""
        cfg = NovaTradeCfg()

        assert cfg.risk.session_overlap_only is False
        assert cfg.risk.session_allow_asian is True
        assert cfg.risk.session_minimum_quality == "acceptable"

    def test_risk_config_validation_with_new_fields(self):
        """Test that risk config validation works with new session fields."""
        risk_config = RiskConfig(
            check_forex_session=True,
            session_overlap_only=False,
            session_allow_asian=True,
            session_minimum_quality="good",
        )

        # Should not raise any validation errors
        errors = risk_config.validate()
        assert len(errors) == 0  # New fields don't add validation requirements
