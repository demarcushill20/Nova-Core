"""Live Trading Agent — bridge between LiveStrategyEngine and TradingAgent FSM.

Translates ``LiveSignal`` objects (Python-native strategy output) into
TradingAgent-compatible webhook payloads, preserving all execution
guarantees: idempotency, evidence recording, fill/close lifecycle,
and state synchronization between the engine and agent.

Key incompatibility handled here:
  - LiveSignal uses side="LONG"/"SHORT"; TradingAgent requires "BUY"/"SELL"
  - LiveSignal uses action="ENTRY"; TradingAgent requires "PLACE_STOP_ORDER"
  - TradingAgent requires fields (strategy_name, strategy_version, broker_symbol,
    bar_close_time, campaign, signal_type, order_type) absent from LiveSignal

This adapter builds correct payloads from scratch rather than using
``LiveSignal.to_alert_payload()``.
"""

from __future__ import annotations

import logging
from typing import Any

from novatrade.config import NovaTradeCfg
from novatrade.execution.trading_agent import AgentResult, AgentState, TradingAgent
from novatrade.strategy.live_engine import LiveSignal, LiveStrategyEngine, SignalType

log = logging.getLogger("novatrade.execution.live_trading_agent")

# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

_SIDE_MAP: dict[str, str] = {
    "LONG": "BUY",
    "SHORT": "SELL",
}

_ORDER_TYPE_MAP: dict[str, str] = {
    "LONG": "BUY_STOP",
    "SHORT": "SELL_STOP",
}

_STRATEGY_NAME = "Rob Hoffman IRB"
_STRATEGY_VERSION = "2.0.0"


# ---------------------------------------------------------------------------
# LiveTradingAgent
# ---------------------------------------------------------------------------


class LiveTradingAgent:
    """Bridge between LiveSignal objects and TradingAgent FSM.

    Translates LiveSignal -> TradingAgent-compatible alert payload,
    preserving all guarantees: idempotency, evidence recording,
    fill/close lifecycle, retry+backoff.
    """

    def __init__(
        self,
        trading_agent: TradingAgent,
        strategy_engine: LiveStrategyEngine,
        cfg: NovaTradeCfg,
        campaign: str = "irb-live",
    ) -> None:
        self._trading_agent = trading_agent
        self._strategy_engine = strategy_engine
        self._cfg = cfg
        self._campaign = campaign

    # -- Properties --------------------------------------------------------

    @property
    def trading_agent(self) -> TradingAgent:
        return self._trading_agent

    @property
    def strategy_engine(self) -> LiveStrategyEngine:
        return self._strategy_engine

    @property
    def campaign(self) -> str:
        return self._campaign

    # -- Main entry point --------------------------------------------------

    async def execute(self, signal: LiveSignal) -> AgentResult:
        """Execute a LiveSignal through the TradingAgent pipeline.

        Translates the signal into a webhook-compatible payload,
        then delegates to TradingAgent.process_alert().

        For PENDING_FILL signals, calls notify_fill() directly on both
        the TradingAgent and LiveStrategyEngine instead of process_alert().

        For ENTRY signals when the TradingAgent is in an opposite-side
        PENDING state (strategy flip), first cancels the existing pending
        order, then places the new entry.
        """
        if signal.signal_type == SignalType.PENDING_FILL:
            return self._handle_pending_fill(signal)

        # --- Cross-side ENTRY: cancel opposite pending before placing new ---
        if signal.signal_type == SignalType.ENTRY and self._is_opposite_pending(signal.side):
            cancel_result = await self._cancel_opposite_pending(signal)
            if not cancel_result.success:
                log.warning(
                    "Cannot flip: cancel of opposite pending failed — skipping new %s entry: %s",
                    signal.side,
                    cancel_result.rejected_reason or cancel_result.error,
                )
                return AgentResult(
                    success=False,
                    state_after=self._trading_agent.state,
                    rejected_reason=f"cross_side_cancel_failed: {cancel_result.rejected_reason or cancel_result.error}",
                )

        payload = self._build_payload(signal)
        if payload is None:
            log.warning(
                "Unsupported signal type %s — skipping",
                signal.signal_type.value,
            )
            return AgentResult(
                success=False,
                state_after=self._trading_agent.state,
                rejected_reason=f"unsupported_signal_type: {signal.signal_type.value}",
            )

        log.info(
            "Executing %s signal: %s %s",
            signal.signal_type.value,
            signal.symbol,
            signal.side,
        )
        log.debug("Payload: %s", payload)

        result = await self._trading_agent.process_alert(payload)

        if result.rejected:
            log.warning(
                "Signal rejected by TradingAgent: %s",
                result.rejected_reason,
            )

        return result

    # -- Fill / close callbacks --------------------------------------------

    def on_fill(
        self,
        position_id: str,
        fill_price: float,
        volume: float,
        stop_loss: float,
    ) -> None:
        """Handle fill notification — update both TradingAgent and LiveStrategyEngine.

        Called by the monitoring layer when the broker fills a pending stop order.
        TradingAgent is updated first (closer to broker truth). If the strategy
        engine update fails, we log at ERROR but do not crash — the TradingAgent
        has the authoritative state.
        """
        log.info(
            "Fill notification: position=%s price=%.5f vol=%.2f sl=%.5f",
            position_id,
            fill_price,
            volume,
            stop_loss,
        )
        try:
            self._trading_agent.notify_fill(position_id, fill_price, volume, stop_loss)
        except Exception:
            log.exception(
                "DESYNC: TradingAgent.notify_fill raised for position=%s — agent state may be inconsistent",
                position_id,
            )
            return  # Don't update engine if agent failed

        try:
            self._strategy_engine.notify_fill(fill_price, volume)
        except Exception:
            log.exception(
                "DESYNC: LiveStrategyEngine.notify_fill raised for position=%s — "
                "TradingAgent updated but engine did not. TradingAgent has authoritative state.",
                position_id,
            )

    def on_broker_close(
        self,
        position_id: str,
        pnl: float,
        exit_reason: str = "SL_HIT",
    ) -> None:
        """Handle broker close — update both TradingAgent and LiveStrategyEngine.

        Called by the monitoring layer when the broker closes a position
        (e.g. stop-loss hit, trailing stop triggered).
        TradingAgent is updated first (closer to broker truth). If the strategy
        engine update fails, we log at ERROR but do not crash.
        """
        log.info(
            "Broker close: position=%s pnl=%.2f reason=%s",
            position_id,
            pnl,
            exit_reason,
        )
        try:
            self._trading_agent.notify_broker_close(position_id, exit_reason)
        except Exception:
            log.exception(
                "DESYNC: TradingAgent.notify_broker_close raised for position=%s — agent state may be inconsistent",
                position_id,
            )
            return  # Don't update engine if agent failed

        try:
            self._strategy_engine.notify_close(pnl)
        except Exception:
            log.exception(
                "DESYNC: LiveStrategyEngine.notify_close raised for position=%s — "
                "TradingAgent updated but engine did not. TradingAgent has authoritative state.",
                position_id,
            )

    # -- Internal: payload construction ------------------------------------

    def _build_payload(self, signal: LiveSignal) -> dict[str, Any] | None:
        """Build a TradingAgent-compatible payload from a LiveSignal.

        Returns None for unsupported signal types.
        """
        if signal.signal_type == SignalType.ENTRY:
            return self._build_entry_payload(signal)
        if signal.signal_type == SignalType.EXIT:
            return self._build_exit_payload(signal)
        if signal.signal_type == SignalType.MODIFY_SL:
            return self._build_modify_sl_payload(signal)
        if signal.signal_type == SignalType.CANCEL_PENDING:
            return self._build_cancel_payload(signal)
        return None

    def _build_entry_payload(self, signal: LiveSignal) -> dict[str, Any]:
        """Build PLACE_STOP_ORDER or REPLACE_STOP_ORDER payload."""
        side_mapped = _SIDE_MAP[signal.side]
        action = self._resolve_entry_action(signal.side)

        return {
            "strategy_name": _STRATEGY_NAME,
            "strategy_version": _STRATEGY_VERSION,
            "action": action,
            "signal_type": "signal_alert",
            "symbol": signal.symbol,
            "broker_symbol": self._cfg.ftmo.resolve_symbol(signal.symbol),
            "side": side_mapped,
            "order_type": _ORDER_TYPE_MAP[signal.side],
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "volume": signal.volume,
            "bar_close_time": int(signal.timestamp),
            "campaign": self._campaign,
        }

    def _build_exit_payload(self, signal: LiveSignal) -> dict[str, Any]:
        """Build CLOSE_POSITION payload."""
        return {
            "strategy_name": _STRATEGY_NAME,
            "strategy_version": _STRATEGY_VERSION,
            "action": "CLOSE_POSITION",
            "symbol": signal.symbol,
            "side": _SIDE_MAP[signal.side],
            "close_reason": signal.exit_reason or "STRATEGY_EXIT",
            "campaign": self._campaign,
        }

    def _build_modify_sl_payload(self, signal: LiveSignal) -> dict[str, Any]:
        """Build MODIFY_SL payload."""
        return {
            "strategy_name": _STRATEGY_NAME,
            "strategy_version": _STRATEGY_VERSION,
            "action": "MODIFY_SL",
            "symbol": signal.symbol,
            "side": _SIDE_MAP[signal.side],
            "old_stop": signal.metadata.get("old_stop", 0.0),
            "new_stop": signal.new_stop,
            "campaign": self._campaign,
        }

    def _build_cancel_payload(self, signal: LiveSignal) -> dict[str, Any]:
        """Build CANCEL_ORDER payload."""
        return {
            "strategy_name": _STRATEGY_NAME,
            "strategy_version": _STRATEGY_VERSION,
            "action": "CANCEL_ORDER",
            "symbol": signal.symbol,
            "side": _SIDE_MAP[signal.side],
            "cancel_reason": signal.exit_reason or "PENDING_EXPIRED",
            "campaign": self._campaign,
        }

    # -- Internal: PENDING_FILL handling -----------------------------------

    def _handle_pending_fill(self, signal: LiveSignal) -> AgentResult:
        """Handle PENDING_FILL by calling notify_fill on both components.

        PENDING_FILL means the LiveStrategyEngine detected a pending order
        filled from bar data.  We notify TradingAgent directly (not via
        process_alert) and sync the engine state.

        Returns success=False if the TradingAgent did not transition to
        LONG/SHORT (e.g. it was already FLAT when fill arrived).
        """
        state_before = self._trading_agent.state
        position_id = self._trading_agent.pending_order_id or ""

        log.info(
            "PENDING_FILL: %s %s at %.5f vol=%.2f (position_id=%s)",
            signal.symbol,
            signal.side,
            signal.entry_price,
            signal.volume,
            position_id,
        )

        self._trading_agent.notify_fill(
            position_id=position_id,
            fill_price=signal.entry_price,
            volume=signal.volume,
            stop_loss=signal.stop_loss,
        )

        # Engine already transitioned in _check_pending_fill, but sync fill info
        self._strategy_engine.notify_fill(signal.entry_price, signal.volume)

        state_after = self._trading_agent.state

        # Verify the agent actually transitioned to a position state.
        # If it stayed in FLAT (e.g. monitoring layer already cancelled),
        # the fill was silently dropped — report failure.
        if state_after not in (AgentState.LONG, AgentState.SHORT):
            log.warning(
                "PENDING_FILL ignored: agent state stayed %s (expected LONG/SHORT)",
                state_after.value,
            )
            return AgentResult(
                success=False,
                state_before=state_before,
                state_after=state_after,
                rejected_reason=f"fill_ignored: agent in {state_after.value}",
            )

        return AgentResult(
            success=True,
            state_before=state_before,
            state_after=state_after,
        )

    # -- Internal: cross-side pending detection/cancellation ----------------

    def _is_opposite_pending(self, signal_side: str) -> bool:
        """Check if the TradingAgent is in a PENDING state opposite to signal_side."""
        agent_state = self._trading_agent.state
        if signal_side == "LONG" and agent_state == AgentState.PENDING_SHORT:
            return True
        return signal_side == "SHORT" and agent_state == AgentState.PENDING_LONG

    async def _cancel_opposite_pending(self, signal: LiveSignal) -> AgentResult:
        """Cancel the existing opposite-side pending order before flipping.

        Builds a CANCEL_ORDER payload for the *current* pending side and
        sends it through TradingAgent.process_alert().
        """
        agent_state = self._trading_agent.state
        cancel_side = "SHORT" if agent_state == AgentState.PENDING_SHORT else "LONG"
        log.info(
            "Cross-side flip: cancelling %s pending before placing %s entry",
            cancel_side,
            signal.side,
        )

        cancel_payload = {
            "strategy_name": _STRATEGY_NAME,
            "strategy_version": _STRATEGY_VERSION,
            "action": "CANCEL_ORDER",
            "symbol": signal.symbol,
            "side": _SIDE_MAP[cancel_side],
            "cancel_reason": "CROSS_SIDE_FLIP",
            "campaign": self._campaign,
        }

        return await self._trading_agent.process_alert(cancel_payload)

    # -- Internal: entry action resolution ---------------------------------

    def _resolve_entry_action(self, signal_side: str) -> str:
        """Determine PLACE_STOP_ORDER vs REPLACE_STOP_ORDER.

        If TradingAgent is in a PENDING state matching the same side,
        use REPLACE_STOP_ORDER to update the existing pending order.
        Otherwise use PLACE_STOP_ORDER for a fresh entry.
        """
        agent_state = self._trading_agent.state

        if signal_side == "LONG" and agent_state == AgentState.PENDING_LONG:
            log.debug("Same-side PENDING_LONG detected — using REPLACE_STOP_ORDER")
            return "REPLACE_STOP_ORDER"
        if signal_side == "SHORT" and agent_state == AgentState.PENDING_SHORT:
            log.debug("Same-side PENDING_SHORT detected — using REPLACE_STOP_ORDER")
            return "REPLACE_STOP_ORDER"

        return "PLACE_STOP_ORDER"
