"""Integration tests for HardRiskSupervisor with TradingAgent.

Tests that the supervisor is properly wired into the trading flow
and provides the expected safety controls.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from novatrade.config import NovaTradeCfg
from novatrade.execution.trading_agent import AgentState, TradingAgent
from novatrade.models import AccountState, OrderResult, OrderSide, Position
from novatrade.risk.hard_risk_supervisor import HardLimits, HardRiskSupervisor
from novatrade.risk.risk_engine import RiskEngine


@pytest.fixture
def cfg():
    """Test configuration."""
    from novatrade.config import AccountMode, FtmoProfile, MetaApiConfig, RiskConfig

    return NovaTradeCfg(
        symbols=["EURUSD"],
        mode=AccountMode.DEMO,
        provider="metaapi",
        timeframes=["H1"],
        metaapi=MetaApiConfig(),
        risk=RiskConfig(),
        ftmo=FtmoProfile(symbol_map={"EURUSD": "EURUSD"}),
        dry_run=True,
    )


@pytest.fixture
def account():
    """Test account state."""
    return AccountState(balance=10000.0, equity=10000.0)


@pytest.fixture
def mock_adapter():
    """Mock adapter."""
    adapter = AsyncMock()
    adapter.get_account.return_value = AccountState(balance=10000.0, equity=10000.0)
    adapter.get_positions.return_value = []
    adapter.place_order.return_value = OrderResult(ok=True, order_id="test_order_123")
    adapter.close_position.return_value = OrderResult(ok=True, order_id="close_order_123")
    return adapter


@pytest.fixture
def supervisor():
    """Test supervisor with conservative limits."""
    import uuid

    # Use unique directories for each test to avoid state persistence issues
    test_id = uuid.uuid4().hex[:8]
    supervisor = HardRiskSupervisor(
        limits=HardLimits(
            equity_floor_usd=8000.0,
            max_daily_loss_usd=500.0,
            max_lot_size=1.0,
            max_concurrent_positions=3,
        ),
        state_dir=f"/tmp/test_supervisor_{test_id}",
        kill_switch_dir=f"/tmp/test_kill_{test_id}",
    )
    # Ensure supervisor starts in non-halted state
    supervisor.resume()
    return supervisor


@pytest.fixture
def trading_agent(cfg, mock_adapter, supervisor):
    """Test trading agent with supervisor."""
    # Match runner.py behavior: set dry_run=False to allow orders through
    cfg.dry_run = False

    risk_engine = RiskEngine(cfg)
    risk_engine.initialize(AccountState(balance=10000.0, equity=10000.0))

    recorder = MagicMock()

    agent = TradingAgent(
        cfg=cfg,
        adapter=mock_adapter,
        risk_engine=risk_engine,
        recorder=recorder,
        supervisor=supervisor,
    )

    supervisor.initialize(initial_equity=10000.0)
    return agent


class TestHardRiskSupervisorIntegration:
    """Integration tests for HardRiskSupervisor."""

    @pytest.mark.asyncio
    async def test_supervisor_instantiated_correctly(self, trading_agent):
        """Test that supervisor is properly wired into trading agent."""
        assert trading_agent._supervisor is not None
        assert isinstance(trading_agent._supervisor, HardRiskSupervisor)
        assert not trading_agent._supervisor.halted

    @pytest.mark.asyncio
    async def test_supervisor_veto_oversized_order(self, trading_agent, mock_adapter):
        """Test that supervisor vetos orders that exceed max lot size."""
        payload = {
            "strategy_name": "Rob Hoffman IRB",
            "strategy_version": "5.0.0",
            "action": "PLACE_STOP_ORDER",
            "signal_type": "LONG_ENTRY",
            "symbol": "EURUSD",
            "broker_symbol": "EURUSD",
            "side": "BUY",
            "order_type": "BUY_STOP",
            "entry_price": 1.1000,
            "stop_loss": 1.0950,
            "volume": 5.0,  # Exceeds max_lot_size of 1.0
            "bar_close_time": 1640995200,
            "campaign": "test",
        }

        result = await trading_agent.process_alert(payload)

        assert not result.success
        assert result.rejected
        assert "supervisor_veto" in result.rejected_reason
        assert "max_lot_size" in result.rejected_reason
        assert mock_adapter.place_order.call_count == 0  # Order never reached adapter

    @pytest.mark.asyncio
    async def test_supervisor_allows_valid_order(self, trading_agent, mock_adapter):
        """Test that supervisor allows valid orders to proceed."""
        payload = {
            "strategy_name": "Rob Hoffman IRB",
            "strategy_version": "5.0.0",
            "action": "PLACE_STOP_ORDER",
            "signal_type": "LONG_ENTRY",
            "symbol": "EURUSD",
            "broker_symbol": "EURUSD",
            "side": "BUY",
            "order_type": "BUY_STOP",
            "entry_price": 1.1000,
            "stop_loss": 1.0985,  # Smaller risk: 15 pips × 0.3 lots × $10 = $45 (within $150 limit)
            "volume": 0.3,  # Smaller volume to stay within risk limits
            "bar_close_time": 1640995200,
            "campaign": "test",
        }

        result = await trading_agent.process_alert(payload)

        assert result.success
        assert not result.rejected
        assert mock_adapter.place_order.call_count == 1  # Order reached adapter

    @pytest.mark.asyncio
    async def test_supervisor_trade_counting(self, trading_agent, mock_adapter):
        """Test that supervisor tracks trades opened."""
        payload = {
            "strategy_name": "Rob Hoffman IRB",
            "strategy_version": "5.0.0",
            "action": "PLACE_STOP_ORDER",
            "signal_type": "LONG_ENTRY",
            "symbol": "EURUSD",
            "broker_symbol": "EURUSD",
            "side": "BUY",
            "order_type": "BUY_STOP",
            "entry_price": 1.1000,
            "stop_loss": 1.0985,  # Low risk: 15 pips × 0.3 lots = $45
            "volume": 0.3,
            "bar_close_time": 1640995200,
            "campaign": "test",
        }

        # Initial trade count
        initial_trades = trading_agent._supervisor.trades_today

        result = await trading_agent.process_alert(payload)

        assert result.success
        # Supervisor should count the opened trade
        assert trading_agent._supervisor.trades_today == initial_trades + 1

    @pytest.mark.asyncio
    async def test_supervisor_pnl_tracking_on_close(self, trading_agent, mock_adapter):
        """Test that supervisor receives actual P&L when positions are closed."""
        # Set up agent in LONG state with a position
        trading_agent._state = AgentState.LONG
        trading_agent._position_id = "pos_123"
        trading_agent._position_volume = 0.5
        trading_agent._position_symbol = "EURUSD"
        trading_agent._position_side = OrderSide.BUY

        # Mock position with P&L
        position = Position(
            position_id="pos_123",
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=0.5,
            open_price=1.1000,
            current_price=1.1050,
            stop_loss=1.0950,
            unrealized_pnl=25.0,  # $25 profit
        )
        mock_adapter.get_positions.return_value = [position]

        # Close position payload
        payload = {
            "strategy_name": "Rob Hoffman IRB",
            "strategy_version": "5.0.0",
            "action": "CLOSE_POSITION",
            "symbol": "EURUSD",
            "side": "BUY",
            "close_reason": "TIME_STOP",
            "campaign": "test",
        }

        # Track initial P&L
        initial_pnl = trading_agent._supervisor.total_pnl_usd

        result = await trading_agent.process_alert(payload)

        assert result.success
        # Supervisor should have received the P&L update
        assert trading_agent._supervisor.total_pnl_usd == initial_pnl + 25.0

    @pytest.mark.asyncio
    async def test_supervisor_emergency_halt_blocks_orders(self, trading_agent, mock_adapter):
        """Test that emergency halt blocks all subsequent orders."""
        # Trigger emergency halt
        trading_agent._supervisor.emergency_halt("Test halt")

        payload = {
            "strategy_name": "Rob Hoffman IRB",
            "strategy_version": "5.0.0",
            "action": "PLACE_STOP_ORDER",
            "signal_type": "LONG_ENTRY",
            "symbol": "EURUSD",
            "broker_symbol": "EURUSD",
            "side": "BUY",
            "order_type": "BUY_STOP",
            "entry_price": 1.1000,
            "stop_loss": 1.0950,
            "volume": 0.5,
            "bar_close_time": 1640995200,
            "campaign": "test",
        }

        result = await trading_agent.process_alert(payload)

        assert not result.success
        assert result.rejected
        assert "supervisor_emergency_halt" in result.rejected_reason
        assert "halted" in result.rejected_reason
        assert mock_adapter.place_order.call_count == 0

    @pytest.mark.asyncio
    async def test_supervisor_concurrent_position_limit(self, trading_agent, mock_adapter):
        """Test that supervisor enforces concurrent position limits."""
        # Mock 3 existing positions (at the limit)
        positions = [
            Position(position_id=f"pos_{i}", symbol="EURUSD", side=OrderSide.BUY, volume=0.1, open_price=1.1000)
            for i in range(3)
        ]
        mock_adapter.get_positions.return_value = positions

        payload = {
            "strategy_name": "Rob Hoffman IRB",
            "strategy_version": "5.0.0",
            "action": "PLACE_STOP_ORDER",
            "signal_type": "LONG_ENTRY",
            "symbol": "EURUSD",
            "broker_symbol": "EURUSD",
            "side": "BUY",
            "order_type": "BUY_STOP",
            "entry_price": 1.1000,
            "stop_loss": 1.0950,
            "volume": 0.5,
            "bar_close_time": 1640995200,
            "campaign": "test",
        }

        result = await trading_agent.process_alert(payload)

        assert not result.success
        assert result.rejected
        assert "supervisor_veto" in result.rejected_reason
        assert "max_concurrent_positions" in result.rejected_reason

    @pytest.mark.asyncio
    async def test_runtime_integration_supervisor_wiring(self, cfg):
        """Test that build_stack properly wires the supervisor."""
        from novatrade.runtime.launch_gate import LaunchMode
        from novatrade.runtime.runner import build_stack

        # This tests that our changes to build_stack work correctly
        ws, loop, readiness = build_stack(cfg, mode=LaunchMode.DRY_RUN)

        # Verify supervisor is wired
        assert ws.agent._supervisor is not None
        assert isinstance(ws.agent._supervisor, HardRiskSupervisor)
        assert not ws.agent._supervisor.halted
        assert ws.agent._supervisor.limits.equity_floor_usd == 8500.0  # Default limit

        # Clean up
        loop.stop()
