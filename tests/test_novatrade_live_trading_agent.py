"""Tests for novatrade.execution.live_trading_agent — LiveSignal-to-TradingAgent bridge."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from novatrade.config import FtmoProfile, NovaTradeCfg
from novatrade.execution.live_trading_agent import LiveTradingAgent
from novatrade.execution.trading_agent import AgentResult, AgentState
from novatrade.strategy.live_engine import LiveSignal, SignalType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg() -> NovaTradeCfg:
    return NovaTradeCfg(
        symbols=["EURUSD"],
        ftmo=FtmoProfile(enabled=True, symbol_suffix=".ftmo"),
    )


def _mock_trading_agent(state: AgentState = AgentState.FLAT) -> MagicMock:
    agent = MagicMock()
    agent.state = state
    agent.pending_order_id = "ORD-123" if "PENDING" in state.value else None
    agent.process_alert = AsyncMock(
        return_value=AgentResult(success=True, state_after=state),
    )
    agent.notify_fill = MagicMock()
    agent.notify_broker_close = MagicMock()
    return agent


def _mock_engine() -> MagicMock:
    engine = MagicMock()
    engine.notify_fill = MagicMock()
    engine.notify_close = MagicMock()
    return engine


def _entry_signal(side: str = "LONG", **kw) -> LiveSignal:
    defaults = {
        "signal_type": SignalType.ENTRY,
        "side": side,
        "symbol": "EURUSD",
        "entry_price": 1.1050,
        "stop_loss": 1.1000,
        "volume": 0.10,
        "timestamp": 1700000000.0,
    }
    defaults.update(kw)
    return LiveSignal(**defaults)


def _exit_signal(side: str = "LONG", **kw) -> LiveSignal:
    defaults = {
        "signal_type": SignalType.EXIT,
        "side": side,
        "symbol": "EURUSD",
        "exit_reason": "TIME_STOP",
    }
    defaults.update(kw)
    return LiveSignal(**defaults)


def _modify_sl_signal(side: str = "LONG", **kw) -> LiveSignal:
    defaults = {
        "signal_type": SignalType.MODIFY_SL,
        "side": side,
        "symbol": "EURUSD",
        "new_stop": 1.1025,
        "metadata": {"old_stop": 1.1000, "atr": 0.0015},
    }
    defaults.update(kw)
    return LiveSignal(**defaults)


def _cancel_signal(side: str = "LONG", **kw) -> LiveSignal:
    defaults = {
        "signal_type": SignalType.CANCEL_PENDING,
        "side": side,
        "symbol": "EURUSD",
        "exit_reason": "PENDING_EXPIRED",
    }
    defaults.update(kw)
    return LiveSignal(**defaults)


def _pending_fill_signal(side: str = "LONG", **kw) -> LiveSignal:
    defaults = {
        "signal_type": SignalType.PENDING_FILL,
        "side": side,
        "symbol": "EURUSD",
        "entry_price": 1.1050,
        "stop_loss": 1.1000,
        "volume": 0.10,
    }
    defaults.update(kw)
    return LiveSignal(**defaults)


# ---------------------------------------------------------------------------
# ENTRY signal tests
# ---------------------------------------------------------------------------


class TestEntrySignal:
    def test_long_entry_produces_place_stop_order(self) -> None:
        agent = _mock_trading_agent(AgentState.FLAT)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        asyncio.run(lta.execute(_entry_signal("LONG")))

        payload = agent.process_alert.call_args[0][0]
        assert payload["action"] == "PLACE_STOP_ORDER"
        assert payload["strategy_name"] == "Rob Hoffman IRB"
        assert payload["strategy_version"] == "5.0.0"
        assert payload["signal_type"] == "signal_alert"
        assert payload["side"] == "BUY"
        assert payload["order_type"] == "BUY_STOP"
        assert payload["entry_price"] == 1.1050
        assert payload["stop_loss"] == 1.1000
        assert payload["volume"] == 0.10
        assert payload["campaign"] == "irb-live"

    def test_short_entry_produces_sell_stop(self) -> None:
        agent = _mock_trading_agent(AgentState.FLAT)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        asyncio.run(lta.execute(_entry_signal("SHORT")))

        payload = agent.process_alert.call_args[0][0]
        assert payload["side"] == "SELL"
        assert payload["order_type"] == "SELL_STOP"

    def test_entry_resolves_broker_symbol(self) -> None:
        agent = _mock_trading_agent(AgentState.FLAT)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        asyncio.run(lta.execute(_entry_signal()))

        payload = agent.process_alert.call_args[0][0]
        assert payload["broker_symbol"] == "EURUSD.ftmo"
        assert payload["symbol"] == "EURUSD"

    def test_entry_bar_close_time_from_timestamp(self) -> None:
        agent = _mock_trading_agent(AgentState.FLAT)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        sig = _entry_signal(timestamp=1700001234.567)
        asyncio.run(lta.execute(sig))

        payload = agent.process_alert.call_args[0][0]
        assert payload["bar_close_time"] == 1700001234


# ---------------------------------------------------------------------------
# EXIT signal tests
# ---------------------------------------------------------------------------


class TestExitSignal:
    def test_exit_produces_close_position(self) -> None:
        agent = _mock_trading_agent(AgentState.LONG)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        asyncio.run(lta.execute(_exit_signal("LONG")))

        payload = agent.process_alert.call_args[0][0]
        assert payload["action"] == "CLOSE_POSITION"
        assert payload["strategy_name"] == "Rob Hoffman IRB"
        assert payload["side"] == "BUY"
        assert payload["close_reason"] == "TIME_STOP"
        assert payload["campaign"] == "irb-live"

    def test_exit_default_reason(self) -> None:
        agent = _mock_trading_agent(AgentState.LONG)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        sig = _exit_signal(exit_reason="")
        asyncio.run(lta.execute(sig))

        payload = agent.process_alert.call_args[0][0]
        assert payload["close_reason"] == "STRATEGY_EXIT"


# ---------------------------------------------------------------------------
# MODIFY_SL signal tests
# ---------------------------------------------------------------------------


class TestModifySLSignal:
    def test_modify_sl_produces_correct_payload(self) -> None:
        agent = _mock_trading_agent(AgentState.LONG)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        asyncio.run(lta.execute(_modify_sl_signal()))

        payload = agent.process_alert.call_args[0][0]
        assert payload["action"] == "MODIFY_SL"
        assert payload["strategy_name"] == "Rob Hoffman IRB"
        assert payload["side"] == "BUY"
        assert payload["new_stop"] == 1.1025
        assert payload["old_stop"] == 1.1000
        assert payload["campaign"] == "irb-live"

    def test_modify_sl_short_side(self) -> None:
        agent = _mock_trading_agent(AgentState.SHORT)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        asyncio.run(lta.execute(_modify_sl_signal("SHORT")))

        payload = agent.process_alert.call_args[0][0]
        assert payload["side"] == "SELL"


# ---------------------------------------------------------------------------
# CANCEL_PENDING signal tests
# ---------------------------------------------------------------------------


class TestCancelPendingSignal:
    def test_cancel_produces_cancel_order(self) -> None:
        agent = _mock_trading_agent(AgentState.PENDING_LONG)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        asyncio.run(lta.execute(_cancel_signal()))

        payload = agent.process_alert.call_args[0][0]
        assert payload["action"] == "CANCEL_ORDER"
        assert payload["strategy_name"] == "Rob Hoffman IRB"
        assert payload["side"] == "BUY"
        assert payload["cancel_reason"] == "PENDING_EXPIRED"
        assert payload["campaign"] == "irb-live"

    def test_cancel_default_reason(self) -> None:
        agent = _mock_trading_agent(AgentState.PENDING_LONG)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        sig = _cancel_signal(exit_reason="")
        asyncio.run(lta.execute(sig))

        payload = agent.process_alert.call_args[0][0]
        assert payload["cancel_reason"] == "PENDING_EXPIRED"


# ---------------------------------------------------------------------------
# PENDING_FILL signal tests
# ---------------------------------------------------------------------------


class TestPendingFillSignal:
    def test_pending_fill_calls_notify_fill_on_both(self) -> None:
        agent = _mock_trading_agent(AgentState.PENDING_LONG)

        # Simulate state transition on notify_fill (real agent would do this)
        def transition_fill(*args, **kwargs):
            agent.state = AgentState.LONG

        agent.notify_fill = MagicMock(side_effect=transition_fill)
        engine = _mock_engine()
        lta = LiveTradingAgent(agent, engine, _cfg())

        result = asyncio.run(lta.execute(_pending_fill_signal()))

        assert result.success is True
        agent.notify_fill.assert_called_once_with(
            position_id="ORD-123",
            fill_price=1.1050,
            volume=0.10,
            stop_loss=1.1000,
        )
        engine.notify_fill.assert_called_once_with(1.1050, 0.10)

    def test_pending_fill_does_not_call_process_alert(self) -> None:
        agent = _mock_trading_agent(AgentState.PENDING_LONG)

        def transition_fill(*args, **kwargs):
            agent.state = AgentState.LONG

        agent.notify_fill = MagicMock(side_effect=transition_fill)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        asyncio.run(lta.execute(_pending_fill_signal()))

        agent.process_alert.assert_not_called()

    def test_pending_fill_empty_position_id_when_flat(self) -> None:
        agent = _mock_trading_agent(AgentState.FLAT)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        result = asyncio.run(lta.execute(_pending_fill_signal()))

        # Agent stays FLAT, so fill is ignored — success=False
        agent.notify_fill.assert_called_once()
        assert agent.notify_fill.call_args.kwargs["position_id"] == ""
        assert result.success is False
        assert "fill_ignored" in result.rejected_reason


# ---------------------------------------------------------------------------
# Side mapping tests
# ---------------------------------------------------------------------------


class TestSideMapping:
    def test_long_maps_to_buy(self) -> None:
        agent = _mock_trading_agent(AgentState.FLAT)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        asyncio.run(lta.execute(_entry_signal("LONG")))

        payload = agent.process_alert.call_args[0][0]
        assert payload["side"] == "BUY"

    def test_short_maps_to_sell(self) -> None:
        agent = _mock_trading_agent(AgentState.FLAT)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        asyncio.run(lta.execute(_entry_signal("SHORT")))

        payload = agent.process_alert.call_args[0][0]
        assert payload["side"] == "SELL"


# ---------------------------------------------------------------------------
# REPLACE: ENTRY when already PENDING same side
# ---------------------------------------------------------------------------


class TestReplaceOnPending:
    def test_entry_when_pending_long_produces_replace(self) -> None:
        agent = _mock_trading_agent(AgentState.PENDING_LONG)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        asyncio.run(lta.execute(_entry_signal("LONG")))

        payload = agent.process_alert.call_args[0][0]
        assert payload["action"] == "REPLACE_STOP_ORDER"

    def test_entry_when_pending_short_produces_replace(self) -> None:
        agent = _mock_trading_agent(AgentState.PENDING_SHORT)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        asyncio.run(lta.execute(_entry_signal("SHORT")))

        payload = agent.process_alert.call_args[0][0]
        assert payload["action"] == "REPLACE_STOP_ORDER"

    def test_entry_when_flat_produces_place(self) -> None:
        agent = _mock_trading_agent(AgentState.FLAT)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        asyncio.run(lta.execute(_entry_signal("LONG")))

        payload = agent.process_alert.call_args[0][0]
        assert payload["action"] == "PLACE_STOP_ORDER"

    def test_entry_opposite_side_pending_produces_place(self) -> None:
        agent = _mock_trading_agent(AgentState.PENDING_SHORT)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        # LONG entry while PENDING_SHORT -> PLACE (not REPLACE)
        asyncio.run(lta.execute(_entry_signal("LONG")))

        payload = agent.process_alert.call_args[0][0]
        assert payload["action"] == "PLACE_STOP_ORDER"


# ---------------------------------------------------------------------------
# Rejection handling
# ---------------------------------------------------------------------------


class TestRejectionHandling:
    def test_rejected_signal_does_not_crash(self) -> None:
        agent = _mock_trading_agent(AgentState.FLAT)
        agent.process_alert = AsyncMock(
            return_value=AgentResult(
                success=False,
                rejected_reason="risk_halt: daily_drawdown",
            ),
        )
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        result = asyncio.run(lta.execute(_entry_signal()))

        assert result.success is False
        assert result.rejected_reason == "risk_halt: daily_drawdown"


# ---------------------------------------------------------------------------
# on_fill callback tests
# ---------------------------------------------------------------------------


class TestOnFill:
    def test_on_fill_updates_both_agent_and_engine(self) -> None:
        agent = _mock_trading_agent(AgentState.PENDING_LONG)
        engine = _mock_engine()
        lta = LiveTradingAgent(agent, engine, _cfg())

        lta.on_fill("POS-456", 1.1050, 0.10, 1.1000)

        agent.notify_fill.assert_called_once_with("POS-456", 1.1050, 0.10, 1.1000)
        engine.notify_fill.assert_called_once_with(1.1050, 0.10)


# ---------------------------------------------------------------------------
# on_broker_close callback tests
# ---------------------------------------------------------------------------


class TestOnBrokerClose:
    def test_on_broker_close_updates_both_agent_and_engine(self) -> None:
        agent = _mock_trading_agent(AgentState.LONG)
        engine = _mock_engine()
        lta = LiveTradingAgent(agent, engine, _cfg())

        lta.on_broker_close("POS-456", -25.0, "SL_HIT")

        agent.notify_broker_close.assert_called_once_with("POS-456", "SL_HIT")
        engine.notify_close.assert_called_once_with(-25.0)

    def test_on_broker_close_default_reason(self) -> None:
        agent = _mock_trading_agent(AgentState.LONG)
        engine = _mock_engine()
        lta = LiveTradingAgent(agent, engine, _cfg())

        lta.on_broker_close("POS-789", 50.0)

        agent.notify_broker_close.assert_called_once_with("POS-789", "SL_HIT")


# ---------------------------------------------------------------------------
# Campaign field tests
# ---------------------------------------------------------------------------


class TestCampaignField:
    def test_custom_campaign_in_all_payloads(self) -> None:
        agent = _mock_trading_agent(AgentState.FLAT)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg(), campaign="ftmo-challenge")

        asyncio.run(lta.execute(_entry_signal()))
        payload = agent.process_alert.call_args[0][0]
        assert payload["campaign"] == "ftmo-challenge"

    def test_campaign_in_exit_payload(self) -> None:
        agent = _mock_trading_agent(AgentState.LONG)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg(), campaign="test-campaign")

        asyncio.run(lta.execute(_exit_signal()))
        payload = agent.process_alert.call_args[0][0]
        assert payload["campaign"] == "test-campaign"

    def test_campaign_in_modify_sl_payload(self) -> None:
        agent = _mock_trading_agent(AgentState.LONG)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg(), campaign="test-campaign")

        asyncio.run(lta.execute(_modify_sl_signal()))
        payload = agent.process_alert.call_args[0][0]
        assert payload["campaign"] == "test-campaign"

    def test_campaign_in_cancel_payload(self) -> None:
        agent = _mock_trading_agent(AgentState.PENDING_LONG)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg(), campaign="test-campaign")

        asyncio.run(lta.execute(_cancel_signal()))
        payload = agent.process_alert.call_args[0][0]
        assert payload["campaign"] == "test-campaign"


# ---------------------------------------------------------------------------
# CRITICAL #2: Cross-side ENTRY cancels opposite pending first
# ---------------------------------------------------------------------------


class TestCrossSideEntry:
    def test_pending_short_then_long_entry_cancels_then_places(self) -> None:
        """PENDING_SHORT + LONG ENTRY -> cancel SHORT pending, then PLACE_STOP_ORDER LONG."""
        agent = _mock_trading_agent(AgentState.PENDING_SHORT)

        # After cancel, agent transitions to FLAT, so second call sees FLAT
        cancel_result = AgentResult(success=True, state_after=AgentState.FLAT)
        place_result = AgentResult(success=True, state_after=AgentState.PENDING_LONG)

        call_count = 0

        async def mock_process_alert(payload: dict) -> AgentResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: CANCEL_ORDER
                agent.state = AgentState.FLAT  # simulate state transition
                return cancel_result
            else:
                # Second call: PLACE_STOP_ORDER
                return place_result

        agent.process_alert = AsyncMock(side_effect=mock_process_alert)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        result = asyncio.run(lta.execute(_entry_signal("LONG")))

        assert result.success is True
        assert agent.process_alert.call_count == 2

        # First call should be CANCEL_ORDER
        first_payload = agent.process_alert.call_args_list[0][0][0]
        assert first_payload["action"] == "CANCEL_ORDER"
        assert first_payload["side"] == "SELL"  # SHORT maps to SELL
        assert first_payload["cancel_reason"] == "CROSS_SIDE_FLIP"

        # Second call should be PLACE_STOP_ORDER for LONG
        second_payload = agent.process_alert.call_args_list[1][0][0]
        assert second_payload["action"] == "PLACE_STOP_ORDER"
        assert second_payload["side"] == "BUY"

    def test_pending_long_then_short_entry_cancels_then_places(self) -> None:
        """PENDING_LONG + SHORT ENTRY -> cancel LONG pending, then PLACE_STOP_ORDER SHORT."""
        agent = _mock_trading_agent(AgentState.PENDING_LONG)

        cancel_result = AgentResult(success=True, state_after=AgentState.FLAT)
        place_result = AgentResult(success=True, state_after=AgentState.PENDING_SHORT)

        call_count = 0

        async def mock_process_alert(payload: dict) -> AgentResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                agent.state = AgentState.FLAT
                return cancel_result
            else:
                return place_result

        agent.process_alert = AsyncMock(side_effect=mock_process_alert)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        result = asyncio.run(lta.execute(_entry_signal("SHORT")))

        assert result.success is True
        assert agent.process_alert.call_count == 2

        first_payload = agent.process_alert.call_args_list[0][0][0]
        assert first_payload["action"] == "CANCEL_ORDER"
        assert first_payload["side"] == "BUY"  # LONG maps to BUY

        second_payload = agent.process_alert.call_args_list[1][0][0]
        assert second_payload["action"] == "PLACE_STOP_ORDER"
        assert second_payload["side"] == "SELL"

    def test_cross_side_cancel_fails_skips_new_entry(self) -> None:
        """If cancel of opposite pending fails, new entry is skipped (conservative)."""
        agent = _mock_trading_agent(AgentState.PENDING_SHORT)
        cancel_result = AgentResult(
            success=False,
            rejected_reason="cancel_rejected: no_pending_order",
        )
        agent.process_alert = AsyncMock(return_value=cancel_result)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        result = asyncio.run(lta.execute(_entry_signal("LONG")))

        assert result.success is False
        assert "cross_side_cancel_failed" in result.rejected_reason
        # Only one call — the cancel attempt. No PLACE_STOP_ORDER.
        assert agent.process_alert.call_count == 1

    def test_same_side_pending_does_not_cancel(self) -> None:
        """Same-side pending should NOT trigger a cancel — only REPLACE."""
        agent = _mock_trading_agent(AgentState.PENDING_LONG)
        lta = LiveTradingAgent(agent, _mock_engine(), _cfg())

        asyncio.run(lta.execute(_entry_signal("LONG")))

        # Only one call: REPLACE_STOP_ORDER (no cancel)
        assert agent.process_alert.call_count == 1
        payload = agent.process_alert.call_args[0][0]
        assert payload["action"] == "REPLACE_STOP_ORDER"


# ---------------------------------------------------------------------------
# CRITICAL #3: PENDING_FILL when TradingAgent ignores fill
# ---------------------------------------------------------------------------


class TestPendingFillIgnored:
    def test_pending_fill_returns_false_when_agent_stays_flat(self) -> None:
        """If TradingAgent stays FLAT after notify_fill, result is success=False."""
        agent = _mock_trading_agent(AgentState.FLAT)
        # Agent stays FLAT (doesn't transition) — fill is ignored
        engine = _mock_engine()
        lta = LiveTradingAgent(agent, engine, _cfg())

        result = asyncio.run(lta.execute(_pending_fill_signal()))

        assert result.success is False
        assert "fill_ignored" in result.rejected_reason
        assert "FLAT" in result.rejected_reason

    def test_pending_fill_returns_false_when_agent_stays_pending(self) -> None:
        """If TradingAgent stays in PENDING state, fill was ignored."""
        agent = _mock_trading_agent(AgentState.PENDING_LONG)
        # Agent stays PENDING_LONG — notify_fill had no effect
        engine = _mock_engine()
        lta = LiveTradingAgent(agent, engine, _cfg())

        result = asyncio.run(lta.execute(_pending_fill_signal()))

        assert result.success is False
        assert "fill_ignored" in result.rejected_reason
        assert "PENDING_LONG" in result.rejected_reason

    def test_pending_fill_returns_true_when_agent_transitions_to_long(self) -> None:
        """If TradingAgent transitions to LONG, result is success=True."""
        agent = _mock_trading_agent(AgentState.PENDING_LONG)

        def transition_fill(*args, **kwargs):
            agent.state = AgentState.LONG

        agent.notify_fill = MagicMock(side_effect=transition_fill)
        engine = _mock_engine()
        lta = LiveTradingAgent(agent, engine, _cfg())

        result = asyncio.run(lta.execute(_pending_fill_signal()))

        assert result.success is True
        assert result.state_after == AgentState.LONG


# ---------------------------------------------------------------------------
# HIGH #4: on_fill / on_broker_close exception handling
# ---------------------------------------------------------------------------


class TestOnFillExceptionHandling:
    def test_on_fill_agent_exception_does_not_crash(self) -> None:
        """If TradingAgent.notify_fill raises, on_fill logs error but doesn't crash."""
        agent = _mock_trading_agent(AgentState.PENDING_LONG)
        agent.notify_fill = MagicMock(side_effect=RuntimeError("agent boom"))
        engine = _mock_engine()
        lta = LiveTradingAgent(agent, engine, _cfg())

        # Should not raise
        lta.on_fill("POS-1", 1.1050, 0.10, 1.1000)

        # Engine should NOT be called because agent failed
        engine.notify_fill.assert_not_called()

    def test_on_fill_engine_exception_does_not_crash(self) -> None:
        """If LiveStrategyEngine.notify_fill raises, on_fill logs error but doesn't crash."""
        agent = _mock_trading_agent(AgentState.PENDING_LONG)
        engine = _mock_engine()
        engine.notify_fill = MagicMock(side_effect=RuntimeError("engine boom"))
        lta = LiveTradingAgent(agent, engine, _cfg())

        # Should not raise
        lta.on_fill("POS-1", 1.1050, 0.10, 1.1000)

        # Agent was updated successfully
        agent.notify_fill.assert_called_once()
        # Engine was called but raised
        engine.notify_fill.assert_called_once()


class TestOnBrokerCloseExceptionHandling:
    def test_on_broker_close_agent_exception_does_not_crash(self) -> None:
        """If TradingAgent.notify_broker_close raises, on_broker_close doesn't crash."""
        agent = _mock_trading_agent(AgentState.LONG)
        agent.notify_broker_close = MagicMock(side_effect=RuntimeError("agent boom"))
        engine = _mock_engine()
        lta = LiveTradingAgent(agent, engine, _cfg())

        # Should not raise
        lta.on_broker_close("POS-1", -25.0, "SL_HIT")

        # Engine should NOT be called because agent failed
        engine.notify_close.assert_not_called()

    def test_on_broker_close_engine_exception_does_not_crash(self) -> None:
        """If LiveStrategyEngine.notify_close raises, on_broker_close doesn't crash."""
        agent = _mock_trading_agent(AgentState.LONG)
        engine = _mock_engine()
        engine.notify_close = MagicMock(side_effect=RuntimeError("engine boom"))
        lta = LiveTradingAgent(agent, engine, _cfg())

        # Should not raise
        lta.on_broker_close("POS-1", -25.0, "SL_HIT")

        # Agent was updated successfully
        agent.notify_broker_close.assert_called_once()
        # Engine was called but raised
        engine.notify_close.assert_called_once()
