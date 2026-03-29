"""Test HardRiskSupervisor integration with LiveLoop."""

import unittest.mock as mock
from unittest.mock import AsyncMock

import pytest

from novatrade.data.bar_aggregator import BarAggregator
from novatrade.data.price_feed import TickBatchPoller
from novatrade.execution.live_trading_agent import LiveTradingAgent
from novatrade.monitor.feed_health import FeedHealthSupervisor
from novatrade.risk.hard_risk_supervisor import HardRiskSupervisor, SupervisorAction
from novatrade.runtime.live_loop import LiveLoop
from novatrade.strategy.live_engine import LiveStrategyEngine


@pytest.mark.asyncio
class TestLiveLoopHardSupervisor:
    """Test HardRiskSupervisor integration with LiveLoop."""

    async def test_hard_supervisor_monitoring_called(self):
        """Test that hard supervisor monitoring is called when check method is invoked."""
        # Mock components
        poller = mock.create_autospec(TickBatchPoller)
        aggregator = mock.create_autospec(BarAggregator)
        supervisor = mock.create_autospec(FeedHealthSupervisor)
        strategy_engine = mock.create_autospec(LiveStrategyEngine)
        live_agent = mock.create_autospec(LiveTradingAgent)
        hard_supervisor = mock.create_autospec(HardRiskSupervisor)
        adapter = AsyncMock()

        # Set up adapter methods
        adapter.get_account = AsyncMock(return_value=mock.MagicMock(equity=10000.0))
        adapter.get_positions = AsyncMock(return_value=[])

        # Set up hard supervisor to return no actions
        hard_supervisor.check_account.return_value = []

        # Create LiveLoop with hard supervisor
        loop = LiveLoop(
            poller=poller,
            aggregator=aggregator,
            supervisor=supervisor,
            strategy_engine=strategy_engine,
            live_agent=live_agent,
            adapter=adapter,
            hard_risk_supervisor=hard_supervisor,
        )

        # Call the hard supervisor check directly
        await loop._check_hard_risk_supervisor()

        # Verify hard supervisor was called
        hard_supervisor.check_account.assert_called_once()
        adapter.get_account.assert_called()
        adapter.get_positions.assert_called()

    async def test_hard_supervisor_close_all_action(self):
        """Test that CLOSE_ALL supervisor action is executed correctly."""
        # Mock components
        poller = mock.create_autospec(TickBatchPoller)
        aggregator = mock.create_autospec(BarAggregator)
        supervisor = mock.create_autospec(FeedHealthSupervisor)
        strategy_engine = mock.create_autospec(LiveStrategyEngine)
        live_agent = mock.create_autospec(LiveTradingAgent)
        hard_supervisor = mock.create_autospec(HardRiskSupervisor)
        adapter = AsyncMock()

        # Set up adapter methods
        adapter.get_account = AsyncMock(return_value=mock.MagicMock(equity=8000.0))
        adapter.get_positions = AsyncMock(return_value=[])
        adapter.close_all_positions = AsyncMock()

        # Set up hard supervisor to return CLOSE_ALL action
        close_action = SupervisorAction(action="CLOSE_ALL", reason="equity below floor")
        hard_supervisor.check_account.return_value = [close_action]

        # Create LiveLoop
        loop = LiveLoop(
            poller=poller,
            aggregator=aggregator,
            supervisor=supervisor,
            strategy_engine=strategy_engine,
            live_agent=live_agent,
            adapter=adapter,
            hard_risk_supervisor=hard_supervisor,
        )

        # Call the hard supervisor check directly
        await loop._check_hard_risk_supervisor()

        # Verify CLOSE_ALL was executed
        adapter.close_all_positions.assert_called_once()

    async def test_hard_supervisor_halt_action(self):
        """Test that HALT supervisor action stops the loop."""
        # Mock components
        poller = mock.create_autospec(TickBatchPoller)
        aggregator = mock.create_autospec(BarAggregator)
        supervisor = mock.create_autospec(FeedHealthSupervisor)
        strategy_engine = mock.create_autospec(LiveStrategyEngine)
        live_agent = mock.create_autospec(LiveTradingAgent)
        hard_supervisor = mock.create_autospec(HardRiskSupervisor)
        adapter = AsyncMock()

        # Set up adapter methods
        adapter.get_account = AsyncMock(return_value=mock.MagicMock(equity=7000.0))
        adapter.get_positions = AsyncMock(return_value=[])

        # Set up hard supervisor to return HALT action
        halt_action = SupervisorAction(action="HALT", reason="critical risk threshold breached")
        hard_supervisor.check_account.return_value = [halt_action]

        # Create LiveLoop
        loop = LiveLoop(
            poller=poller,
            aggregator=aggregator,
            supervisor=supervisor,
            strategy_engine=strategy_engine,
            live_agent=live_agent,
            adapter=adapter,
            hard_risk_supervisor=hard_supervisor,
        )

        # Mock stop method
        loop.stop = mock.MagicMock()

        # Call the hard supervisor check directly
        await loop._check_hard_risk_supervisor()

        # Verify stop was called
        loop.stop.assert_called_once()

    async def test_hard_supervisor_none_skips_monitoring(self):
        """Test that monitoring is skipped when hard_supervisor is None."""
        # Mock components
        poller = mock.create_autospec(TickBatchPoller)
        aggregator = mock.create_autospec(BarAggregator)
        supervisor = mock.create_autospec(FeedHealthSupervisor)
        strategy_engine = mock.create_autospec(LiveStrategyEngine)
        live_agent = mock.create_autospec(LiveTradingAgent)
        adapter = AsyncMock()

        # Create LiveLoop without hard supervisor
        loop = LiveLoop(
            poller=poller,
            aggregator=aggregator,
            supervisor=supervisor,
            strategy_engine=strategy_engine,
            live_agent=live_agent,
            adapter=adapter,
            hard_risk_supervisor=None,  # No hard supervisor
        )

        # Call the hard supervisor check directly
        await loop._check_hard_risk_supervisor()

        # Should complete without error and not call adapter
        assert not adapter.get_account.called
        assert not adapter.get_positions.called

    async def test_hard_supervisor_error_handling(self):
        """Test that errors in hard supervisor monitoring are handled gracefully."""
        # Mock components
        poller = mock.create_autospec(TickBatchPoller)
        aggregator = mock.create_autospec(BarAggregator)
        supervisor = mock.create_autospec(FeedHealthSupervisor)
        strategy_engine = mock.create_autospec(LiveStrategyEngine)
        live_agent = mock.create_autospec(LiveTradingAgent)
        hard_supervisor = mock.create_autospec(HardRiskSupervisor)
        adapter = AsyncMock()

        # Set up adapter to raise exception
        adapter.get_account = AsyncMock(side_effect=Exception("MetaApi connection failed"))
        adapter.get_positions = AsyncMock(return_value=[])

        # Create LiveLoop
        loop = LiveLoop(
            poller=poller,
            aggregator=aggregator,
            supervisor=supervisor,
            strategy_engine=strategy_engine,
            live_agent=live_agent,
            adapter=adapter,
            hard_risk_supervisor=hard_supervisor,
        )

        # Call should not raise exception
        await loop._check_hard_risk_supervisor()

        # Hard supervisor check_account should not be called due to adapter failure
        assert not hard_supervisor.check_account.called
