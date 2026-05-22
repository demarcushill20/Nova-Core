"""Tests for LiveLoop._pre_rollover_sweep_if_due().

Background: the PreTradeGate's rollover dead zone (21:00-22:00 UTC by default)
blocks NEW order placement during the rollover window but does nothing about
STOP/LIMIT orders already resting at the broker. On 2026-05-19 a SELL STOP
placed at 19:10 UTC filled at 21:05:00 during a 9-pip spread spike and the
position was immediately stopped out at ~10 pips of slippage past the stated
SL — losing 2.7x the planned risk. This sweep cancels pending orders 5
minutes before the dead zone starts so they can't get caught.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from novatrade.execution.trading_agent import AgentState


@dataclass
class FakePendingOrder:
    order_id: str
    symbol: str = "EURUSD.sim"
    side: str = "SELL"


def _make_loop(
    *,
    rollover_enabled: bool = True,
    rollover_start_hour: int = 21,
    adapter_available: bool = True,
    pending_orders: list[FakePendingOrder] | None = None,
    cancel_raises_on: set[str] | None = None,
    risk_engine_available: bool = True,
    agent_state: AgentState = AgentState.FLAT,
):
    from novatrade.runtime.live_loop import LiveLoop

    poller = MagicMock()
    aggregator = MagicMock()
    supervisor = MagicMock()
    supervisor.check_staleness.return_value = {}

    strategy_engine = MagicMock()
    strategy_engine.cancel_pending = MagicMock()
    strategy_engine.snapshot.return_value = {}

    trading_agent = MagicMock()
    trading_agent.state = agent_state
    trading_agent.force_flat = MagicMock()

    live_agent = MagicMock()
    live_agent.trading_agent = trading_agent
    live_agent.strategy_engine = strategy_engine

    adapter = None
    if adapter_available:
        adapter = AsyncMock()
        adapter.get_orders = AsyncMock(return_value=pending_orders or [])

        async def _cancel(order_id):
            if cancel_raises_on and order_id in cancel_raises_on:
                raise ConnectionError(f"broker down for {order_id}")
            return MagicMock(ok=True)

        adapter.cancel_order = AsyncMock(side_effect=_cancel)

    risk_engine = None
    if risk_engine_available:
        risk_engine = MagicMock()
        risk_engine._risk = MagicMock()
        risk_engine._risk.rollover_dead_zone_enabled = rollover_enabled
        risk_engine._risk.rollover_start_hour_utc = rollover_start_hour

    loop = LiveLoop(
        poller=poller,
        aggregator=aggregator,
        supervisor=supervisor,
        strategy_engine=strategy_engine,
        live_agent=live_agent,
        adapter=adapter,
        risk_engine=risk_engine,
    )

    return loop, adapter, trading_agent, strategy_engine


def _freeze_clock(monkeypatch, when: datetime) -> None:
    """Patch the datetime used inside the live_loop module."""

    import novatrade.runtime.live_loop as live_loop_mod

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return when if tz is None else when.astimezone(tz)

    monkeypatch.setattr(live_loop_mod, "datetime", _FrozenDatetime)


class TestPreRolloverSweep:
    @pytest.mark.asyncio
    async def test_sweep_cancels_all_pending_at_T_minus_5(self, monkeypatch):
        """In-window: 20:55 UTC with start_hour=21 — cancel every pending order."""
        orders = [FakePendingOrder(order_id="ord_1"), FakePendingOrder(order_id="ord_2")]
        loop, adapter, _, _ = _make_loop(pending_orders=orders)
        _freeze_clock(monkeypatch, datetime(2026, 5, 19, 20, 55, 0, tzinfo=timezone.utc))

        await loop._pre_rollover_sweep_if_due()

        assert adapter.get_orders.await_count == 1
        cancelled_ids = sorted(call.args[0] for call in adapter.cancel_order.await_args_list)
        assert cancelled_ids == ["ord_1", "ord_2"]

    @pytest.mark.asyncio
    async def test_sweep_skipped_outside_window(self, monkeypatch):
        """20:30 UTC is before the T-5 window — no broker calls at all."""
        loop, adapter, _, _ = _make_loop(
            pending_orders=[FakePendingOrder(order_id="ord_1")],
        )
        _freeze_clock(monkeypatch, datetime(2026, 5, 19, 20, 30, 0, tzinfo=timezone.utc))

        await loop._pre_rollover_sweep_if_due()

        adapter.get_orders.assert_not_called()
        adapter.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_skipped_inside_dead_zone(self, monkeypatch):
        """21:05 UTC is inside the dead zone, after the sweep window — no sweep."""
        loop, adapter, _, _ = _make_loop(
            pending_orders=[FakePendingOrder(order_id="ord_1")],
        )
        _freeze_clock(monkeypatch, datetime(2026, 5, 19, 21, 5, 0, tzinfo=timezone.utc))

        await loop._pre_rollover_sweep_if_due()

        adapter.get_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_idempotent_within_window(self, monkeypatch):
        """Five health cycles inside the T-5 window → only one sweep."""
        orders = [FakePendingOrder(order_id="ord_1")]
        loop, adapter, _, _ = _make_loop(pending_orders=orders)
        _freeze_clock(monkeypatch, datetime(2026, 5, 19, 20, 56, 30, tzinfo=timezone.utc))

        for _ in range(5):
            await loop._pre_rollover_sweep_if_due()

        assert adapter.get_orders.await_count == 1
        assert adapter.cancel_order.await_count == 1

    @pytest.mark.asyncio
    async def test_sweep_runs_again_on_next_day(self, monkeypatch):
        """A new UTC date with the same rollover boundary triggers a fresh sweep."""
        orders = [FakePendingOrder(order_id="ord_1")]
        loop, adapter, _, _ = _make_loop(pending_orders=orders)

        _freeze_clock(monkeypatch, datetime(2026, 5, 19, 20, 55, 0, tzinfo=timezone.utc))
        await loop._pre_rollover_sweep_if_due()

        _freeze_clock(monkeypatch, datetime(2026, 5, 20, 20, 55, 0, tzinfo=timezone.utc))
        await loop._pre_rollover_sweep_if_due()

        assert adapter.get_orders.await_count == 2
        assert adapter.cancel_order.await_count == 2

    @pytest.mark.asyncio
    async def test_sweep_disabled_when_dead_zone_disabled(self, monkeypatch):
        loop, adapter, _, _ = _make_loop(
            rollover_enabled=False,
            pending_orders=[FakePendingOrder(order_id="ord_1")],
        )
        _freeze_clock(monkeypatch, datetime(2026, 5, 19, 20, 55, 0, tzinfo=timezone.utc))

        await loop._pre_rollover_sweep_if_due()

        adapter.get_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_no_op_without_adapter_or_risk_engine(self, monkeypatch):
        _freeze_clock(monkeypatch, datetime(2026, 5, 19, 20, 55, 0, tzinfo=timezone.utc))

        loop_no_adapter, _, _, _ = _make_loop(adapter_available=False)
        await loop_no_adapter._pre_rollover_sweep_if_due()  # must not raise

        loop_no_risk, adapter, _, _ = _make_loop(risk_engine_available=False)
        await loop_no_risk._pre_rollover_sweep_if_due()
        adapter.get_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_continues_when_one_cancel_fails(self, monkeypatch):
        """Broker rejecting one cancel must not stop the others — and must not
        keep retrying on subsequent health cycles within the same boundary."""
        orders = [
            FakePendingOrder(order_id="ord_1"),
            FakePendingOrder(order_id="ord_2"),
            FakePendingOrder(order_id="ord_3"),
        ]
        loop, adapter, _, _ = _make_loop(
            pending_orders=orders,
            cancel_raises_on={"ord_2"},
        )
        _freeze_clock(monkeypatch, datetime(2026, 5, 19, 20, 55, 0, tzinfo=timezone.utc))

        await loop._pre_rollover_sweep_if_due()

        assert adapter.cancel_order.await_count == 3  # all three attempted

    @pytest.mark.asyncio
    async def test_sweep_resets_agent_if_pending(self, monkeypatch):
        """If the agent is in a PENDING state, force it FLAT after cancelling so
        reconciliation doesn't keep waiting for a now-gone broker order."""
        orders = [FakePendingOrder(order_id="ord_1")]
        loop, _, trading_agent, strategy_engine = _make_loop(
            pending_orders=orders,
            agent_state=AgentState.PENDING_SHORT,
        )
        _freeze_clock(monkeypatch, datetime(2026, 5, 19, 20, 55, 0, tzinfo=timezone.utc))

        await loop._pre_rollover_sweep_if_due()

        trading_agent.force_flat.assert_called_once()
        strategy_engine.cancel_pending.assert_called_once()

    @pytest.mark.asyncio
    async def test_sweep_does_not_reset_agent_when_flat(self, monkeypatch):
        """When agent is FLAT we must not force_flat (no state change needed)."""
        orders = [FakePendingOrder(order_id="ord_1")]
        loop, _, trading_agent, _ = _make_loop(
            pending_orders=orders,
            agent_state=AgentState.FLAT,
        )
        _freeze_clock(monkeypatch, datetime(2026, 5, 19, 20, 55, 0, tzinfo=timezone.utc))

        await loop._pre_rollover_sweep_if_due()

        trading_agent.force_flat.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_handles_get_orders_failure(self, monkeypatch):
        """get_orders raising must not bubble — supervisor stays alive."""
        loop, adapter, _, _ = _make_loop(pending_orders=[])
        adapter.get_orders = AsyncMock(side_effect=ConnectionError("metaapi down"))
        _freeze_clock(monkeypatch, datetime(2026, 5, 19, 20, 55, 0, tzinfo=timezone.utc))

        await loop._pre_rollover_sweep_if_due()  # must not raise
        adapter.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_window_respects_configured_start_hour(self, monkeypatch):
        """Sweep window slides with rollover_start_hour_utc config."""
        orders = [FakePendingOrder(order_id="ord_1")]
        loop, adapter, _, _ = _make_loop(
            pending_orders=orders,
            rollover_start_hour=22,  # custom config: rollover at 22:00 UTC
        )
        _freeze_clock(monkeypatch, datetime(2026, 5, 19, 20, 55, 0, tzinfo=timezone.utc))

        await loop._pre_rollover_sweep_if_due()
        adapter.get_orders.assert_not_called()  # 20:55 is now too early

        _freeze_clock(monkeypatch, datetime(2026, 5, 19, 21, 55, 0, tzinfo=timezone.utc))
        await loop._pre_rollover_sweep_if_due()
        adapter.get_orders.assert_called_once()  # 21:55 is the new T-5
