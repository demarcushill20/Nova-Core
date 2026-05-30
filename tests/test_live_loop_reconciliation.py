"""Tests for LiveLoop._reconcile_broker_state() — broker ↔ agent state sync.

Covers the critical bug where agent gets stuck in LONG/SHORT after
a broker-side SL hit because the live pipeline had no reconciliation.

Scenarios:
    1. Agent LONG, broker no position → calls on_broker_close (SL hit)
    2. Agent SHORT, broker no position → calls on_broker_close
    3. Agent PENDING_LONG, broker has position → calls on_fill
    4. Agent PENDING_SHORT, broker has position → calls on_fill
    5. Agent PENDING, no order, no position → force_flat (stale pending)
    6. Agent FLAT → no reconciliation action (early return)
    7. Adapter unavailable → graceful degradation
    8. get_positions raises → graceful degradation
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from novatrade.execution.trading_agent import AgentState

# ---------------------------------------------------------------------------
# Minimal stubs to construct a LiveLoop without the full stack
# ---------------------------------------------------------------------------


@dataclass
class FakePosition:
    position_id: str = "pos_123"
    open_price: float = 1.10050
    volume: float = 0.10
    stop_loss: float = 1.09900
    symbol: str = "EURUSD"
    side: str = "BUY"


@dataclass
class FakeOrder:
    order_id: str = "ord_456"
    symbol: str = "EURUSD"
    side: str = "BUY"


def _make_live_loop(
    agent_state: AgentState = AgentState.FLAT,
    *,
    positions: list | None = None,
    orders: list | None = None,
    adapter_available: bool = True,
    get_positions_raises: bool = False,
):
    """Build a LiveLoop with mocked internals for reconciliation testing."""
    from novatrade.runtime.live_loop import LiveLoop

    # Mock poller, aggregator, supervisor, strategy engine
    poller = MagicMock()
    aggregator = MagicMock()
    supervisor = MagicMock()
    supervisor.check_staleness.return_value = {}

    strategy_engine = MagicMock()
    strategy_engine.cancel_pending = MagicMock()
    strategy_engine.snapshot.return_value = {}

    # Mock TradingAgent
    trading_agent = MagicMock()
    trading_agent.state = agent_state
    trading_agent.position_id = "pos_123" if agent_state in (AgentState.LONG, AgentState.SHORT) else None
    trading_agent.pending_order_id = (
        "ord_456" if agent_state in (AgentState.PENDING_LONG, AgentState.PENDING_SHORT) else None
    )
    trading_agent.force_flat = MagicMock()

    # Mock LiveTradingAgent
    live_agent = MagicMock()
    live_agent.trading_agent = trading_agent
    live_agent.strategy_engine = strategy_engine
    live_agent.on_fill = MagicMock()
    live_agent.on_broker_close = MagicMock()

    # Mock adapter
    adapter = None
    if adapter_available:
        adapter = AsyncMock()
        if get_positions_raises:
            adapter.get_positions = AsyncMock(side_effect=ConnectionError("broker down"))
        else:
            adapter.get_positions = AsyncMock(return_value=positions or [])
        adapter.get_orders = AsyncMock(return_value=orders or [])

    loop = LiveLoop(
        poller=poller,
        aggregator=aggregator,
        supervisor=supervisor,
        strategy_engine=strategy_engine,
        live_agent=live_agent,
        adapter=adapter,
    )

    return loop, live_agent, trading_agent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReconcileBrokerState:
    """Tests for LiveLoop._reconcile_broker_state()."""

    @pytest.mark.asyncio
    async def test_agent_long_broker_no_position_calls_broker_close(self):
        """Case 1: Agent stuck in LONG but broker has no position → SL hit."""
        loop, live_agent, _ = _make_live_loop(
            AgentState.LONG,
            positions=[],
        )
        await loop._reconcile_broker_state()
        live_agent.on_broker_close.assert_called_once_with(
            position_id="pos_123",
            pnl=0.0,
            exit_reason="BROKER_CLOSE_RECONCILED",
        )

    @pytest.mark.asyncio
    async def test_agent_short_broker_no_position_calls_broker_close(self):
        """Case 2: Agent stuck in SHORT but broker has no position."""
        loop, live_agent, _ = _make_live_loop(
            AgentState.SHORT,
            positions=[],
        )
        await loop._reconcile_broker_state()
        live_agent.on_broker_close.assert_called_once_with(
            position_id="pos_123",
            pnl=0.0,
            exit_reason="BROKER_CLOSE_RECONCILED",
        )

    @pytest.mark.asyncio
    async def test_agent_pending_long_broker_has_position_calls_fill(self):
        """Case 3: Agent PENDING_LONG but broker already has a position."""
        pos = FakePosition(position_id="fill_999", open_price=1.10100, volume=0.10, stop_loss=1.09800)
        loop, live_agent, _ = _make_live_loop(
            AgentState.PENDING_LONG,
            positions=[pos],
        )
        await loop._reconcile_broker_state()
        live_agent.on_fill.assert_called_once_with(
            position_id="fill_999",
            fill_price=1.10100,
            volume=0.10,
            stop_loss=1.09800,
        )

    @pytest.mark.asyncio
    async def test_agent_pending_short_broker_has_position_calls_fill(self):
        """Case 4: Agent PENDING_SHORT but broker already has a position."""
        pos = FakePosition(position_id="fill_888")
        loop, live_agent, _ = _make_live_loop(
            AgentState.PENDING_SHORT,
            positions=[pos],
        )
        await loop._reconcile_broker_state()
        live_agent.on_fill.assert_called_once()

    @pytest.mark.asyncio
    async def test_agent_pending_stale_forces_flat(self):
        """Case 5: Agent PENDING but broker has no order and no position."""
        loop, live_agent, trading_agent = _make_live_loop(
            AgentState.PENDING_LONG,
            positions=[],
            orders=[],
        )
        await loop._reconcile_broker_state()
        trading_agent.force_flat.assert_called_once()
        assert "reconcile_stale_pending" in trading_agent.force_flat.call_args[1]["reason"]

    @pytest.mark.asyncio
    async def test_agent_pending_with_order_no_action(self):
        """Agent PENDING and broker has a pending order — no reconciliation needed."""
        order = FakeOrder()
        loop, live_agent, trading_agent = _make_live_loop(
            AgentState.PENDING_LONG,
            positions=[],
            orders=[order],
        )
        await loop._reconcile_broker_state()
        live_agent.on_broker_close.assert_not_called()
        live_agent.on_fill.assert_not_called()
        trading_agent.force_flat.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_flat_skips_reconciliation(self):
        """Case 6: Agent FLAT → no action needed."""
        loop, live_agent, trading_agent = _make_live_loop(
            AgentState.FLAT,
            positions=[],
        )
        await loop._reconcile_broker_state()
        live_agent.on_broker_close.assert_not_called()
        live_agent.on_fill.assert_not_called()
        trading_agent.force_flat.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_adapter_skips(self):
        """Case 7: No adapter → graceful no-op."""
        loop, live_agent, _ = _make_live_loop(
            AgentState.LONG,
            adapter_available=False,
        )
        await loop._reconcile_broker_state()
        live_agent.on_broker_close.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_positions_raises_skips(self):
        """Case 8: get_positions raises → graceful degradation."""
        loop, live_agent, _ = _make_live_loop(
            AgentState.LONG,
            get_positions_raises=True,
        )
        await loop._reconcile_broker_state()
        live_agent.on_broker_close.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_long_with_position_no_action(self):
        """Agent LONG and broker has position — consistent, no action."""
        pos = FakePosition()
        loop, live_agent, trading_agent = _make_live_loop(
            AgentState.LONG,
            positions=[pos],
        )
        await loop._reconcile_broker_state()
        live_agent.on_broker_close.assert_not_called()
        live_agent.on_fill.assert_not_called()
        trading_agent.force_flat.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_pending_resets_strategy_engine(self):
        """Stale pending also resets strategy engine to prevent phantom signals."""
        loop, live_agent, trading_agent = _make_live_loop(
            AgentState.PENDING_LONG,
            positions=[],
            orders=[],
        )
        await loop._reconcile_broker_state()
        live_agent.strategy_engine.cancel_pending.assert_called_once()

    # -- Case 4: orphan adoption (restart while holding a position) ------------

    @pytest.mark.asyncio
    async def test_orphan_adoption_preserves_short_side(self, monkeypatch):
        """A SHORT orphan must be adopted as SHORT, not LONG. Regression: the
        adoption read getattr(pos, 'type') which never exists on Position, so
        every orphan defaulted to LONG (a short would get a stop on the wrong
        side)."""
        from novatrade.models import OrderSide

        monkeypatch.delenv("NOVATRADE_MAX_ADOPT_VOLUME", raising=False)
        pos = FakePosition(position_id="orph_short", volume=2.0, side="SELL")
        loop, live_agent, trading_agent = _make_live_loop(AgentState.FLAT, positions=[pos])

        await loop._reconcile_broker_state()

        trading_agent.recover_position.assert_called_once()
        assert trading_agent.recover_position.call_args.kwargs["side"] == OrderSide.SELL
        assert live_agent.strategy_engine.recover_position_state.call_args.kwargs["side"] == "SHORT"

    @pytest.mark.asyncio
    async def test_orphan_adoption_allows_risk_sized_volume(self, monkeypatch):
        """A risk-sized ~15-lot orphan must be adopted, not refused. The old
        5-lot cap left real positions unmanaged after a restart."""
        monkeypatch.delenv("NOVATRADE_MAX_ADOPT_VOLUME", raising=False)
        pos = FakePosition(position_id="orph_big", volume=15.0, side="BUY")
        loop, live_agent, trading_agent = _make_live_loop(AgentState.FLAT, positions=[pos])

        await loop._reconcile_broker_state()

        trading_agent.recover_position.assert_called_once()

    @pytest.mark.asyncio
    async def test_orphan_adoption_refuses_absurd_volume(self, monkeypatch):
        """An orphan above the cap is still refused (manual review)."""
        monkeypatch.setenv("NOVATRADE_MAX_ADOPT_VOLUME", "50.0")
        pos = FakePosition(position_id="orph_huge", volume=200.0, side="BUY")
        loop, live_agent, trading_agent = _make_live_loop(AgentState.FLAT, positions=[pos])

        await loop._reconcile_broker_state()

        trading_agent.recover_position.assert_not_called()
