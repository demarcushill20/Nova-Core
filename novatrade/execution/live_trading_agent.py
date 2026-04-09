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

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Any, ClassVar

from novatrade.config import NovaTradeCfg
from novatrade.execution.trading_agent import AgentResult, AgentState, TradingAgent
from novatrade.monitor.post_trade_verifier import PostTradeVerifier, _VerificationContext
from novatrade.monitor.regime_detector import load_regime_safe
from novatrade.strategy.live_engine import LiveSignal, LiveStrategyEngine, SignalType

log = logging.getLogger("novatrade.execution.live_trading_agent")

# ---------------------------------------------------------------------------
# Price normalization fix for MetaAPI broker compatibility
# ---------------------------------------------------------------------------


class PriceNormalizer:
    """Price normalization for MetaAPI broker compatibility.

    Fixes TRADE_RETCODE_INVALID_PRICE errors by ensuring prices are
    formatted to the correct decimal precision for each symbol.
    """

    SYMBOL_PRECISION: ClassVar[dict[str, int]] = {
        # Major FX pairs (5 decimals)
        "EURUSD": 5,
        "GBPUSD": 5,
        "USDCHF": 5,
        "AUDUSD": 5,
        "NZDUSD": 5,
        "USDCAD": 5,
        "EURGBP": 5,
        "EURJPY": 3,
        # JPY pairs (3 decimals)
        "USDJPY": 3,
        "GBPJPY": 3,
        "AUDJPY": 3,
        "NZDJPY": 3,
        "CADJPY": 3,
        "CHFJPY": 3,
        # Default for unknown symbols
        "DEFAULT": 5,
    }

    @classmethod
    def normalize_price(cls, symbol: str, price: float) -> float:
        """Normalize price to broker-required precision."""
        precision = cls.SYMBOL_PRECISION.get(symbol, cls.SYMBOL_PRECISION["DEFAULT"])
        return round(price, precision)

    @classmethod
    def normalize_payload_prices(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalize all prices in a trading payload."""
        symbol = payload.get("symbol", "")

        # All price fields that need normalization
        price_fields = ["entry_price", "stop_loss", "take_profit", "old_stop", "new_stop"]

        for price_field in price_fields:
            if price_field in payload and isinstance(payload[price_field], (int, float)):
                original = payload[price_field]
                normalized = cls.normalize_price(symbol, original)
                if original != normalized:
                    log.debug("Normalized %s %s: %.8f -> %.8f", symbol, price_field, original, normalized)
                payload[price_field] = normalized

        return payload


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
_STRATEGY_VERSION = "5.0.0"

_VALIDATION_REPORTS_PATH = Path("OUTPUT/novatrade/validation_reports.jsonl")


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
        verifier: PostTradeVerifier | None = None,
    ) -> None:
        self._trading_agent = trading_agent
        self._strategy_engine = strategy_engine
        self._cfg = cfg
        self._campaign = campaign
        self._verifier = verifier
        self._pending_verification: dict[str, _VerificationContext] = {}

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

        # Anti-EA-detection: randomize order timing so entries don't land
        # at exact bar-close intervals (a strong EA fingerprint).
        if signal.signal_type == SignalType.ENTRY and self._cfg.risk.entry_jitter_enabled:
            jitter = random.uniform(  # noqa: S311 — non-crypto jitter
                self._cfg.risk.entry_jitter_min_seconds,
                self._cfg.risk.entry_jitter_max_seconds,
            )
            log.info("entry jitter: sleeping %.2fs before order placement", jitter)
            await asyncio.sleep(jitter)

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

        # --- Post-trade validation (best-effort, never crashes pipeline) ---
        agent = self._trading_agent
        symbol = agent._position_symbol or (agent._cfg.symbols[0] if agent._cfg.symbols else "")
        side = "BUY" if agent._state == AgentState.LONG else "SELL"
        self._run_post_trade_validation(
            symbol=symbol,
            side=side,
            entry_price=fill_price,
            stop_loss=stop_loss,
            volume=volume,
            fill_price=fill_price,
            order_id=position_id,
        )

        # --- Snapshot candle buffers for post-trade verification ---
        if self._verifier:
            try:
                # Resolve entry bar timestamp from the engine's last bar
                entry_bar_ts = 0.0
                h1_buf = self._strategy_engine._h1_candles
                if h1_buf:
                    entry_bar_ts = h1_buf[-1].timestamp

                trade_side = "LONG" if agent._state == AgentState.LONG else "SHORT"
                self._pending_verification[position_id] = _VerificationContext(
                    position_id=position_id,
                    symbol=symbol,
                    side=trade_side,
                    entry_price=fill_price,
                    stop_loss=stop_loss,
                    entry_bar_timestamp=entry_bar_ts,
                    h1_snapshot=list(self._strategy_engine._h1_candles),
                    h4_snapshot=list(self._strategy_engine._h4_candles),
                )
            except Exception:
                log.exception("Failed to snapshot candle buffers for verification (best-effort)")

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

        # --- Post-trade verification (best-effort, never crashes pipeline) ---
        ctx = self._pending_verification.pop(position_id, None)
        if ctx and self._verifier:
            try:
                self._verifier.verify_trade(
                    position_id=ctx.position_id,
                    symbol=ctx.symbol,
                    side=ctx.side,
                    entry_price=ctx.entry_price,
                    stop_loss=ctx.stop_loss,
                    entry_bar_timestamp=ctx.entry_bar_timestamp,
                    h1_candles=ctx.h1_snapshot,
                    h4_candles=ctx.h4_snapshot,
                )
                if self._verifier.should_alert():
                    summary = self._verifier.get_summary()
                    log.warning(
                        "POST-TRADE DRIFT ALERT: mismatch rate %.1f%% exceeds %.1f%% threshold "
                        "over last %d trades — review strategy alignment",
                        summary.mismatch_rate * 100,
                        self._verifier._alert_threshold * 100,
                        summary.total_verified,
                    )
            except Exception:
                log.exception("Post-trade verification failed for position=%s (best-effort)", position_id)

    # -- Post-trade validation hook ----------------------------------------

    def _run_post_trade_validation(
        self,
        *,
        symbol: str,
        side: str,
        entry_price: float,
        stop_loss: float,
        volume: float,
        fill_price: float | None = None,
        order_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Fire the post-trade validation hook (best-effort, never crashes the pipeline).

        Constructs a ``TradeRecord`` from fill data and delegates to
        ``post_trade_validation_hook`` which runs all validation rules,
        appends a report to ``OUTPUT/novatrade/validation_reports.jsonl``,
        and logs critical violations as warnings.
        """
        try:
            from novatrade.validation.trade_validator import (
                TradeRecord,
                post_trade_validation_hook,
            )

            record = TradeRecord(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                stop_loss=stop_loss,
                volume=volume,
                timestamp=time.time(),
                campaign=self._campaign,
                strategy_name=_STRATEGY_NAME,
                strategy_version=_STRATEGY_VERSION,
                outcome="FILLED",
                fill_price=fill_price,
                order_id=order_id,
                metadata=metadata or {},
            )

            post_trade_validation_hook(
                record,
                reports_path=_VALIDATION_REPORTS_PATH,
            )
        except Exception:
            log.warning(
                "Post-trade validation hook failed (non-fatal) — trading continues",
                exc_info=True,
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
        if signal.signal_type == SignalType.PARTIAL_EXIT:
            return self._build_partial_exit_payload(signal)
        return None

    def _build_partial_exit_payload(self, signal: LiveSignal) -> dict[str, Any] | None:
        """Build PARTIAL_CLOSE payload for v5 partial profit at 1R."""
        side_mapped = _SIDE_MAP.get(signal.side)
        if side_mapped is None:
            log.error("Unknown signal side %r — cannot build partial-exit payload", signal.side)
            return None
        if signal.volume <= 0:
            log.error("PARTIAL_EXIT signal has volume <= 0 — skipping")
            return None
        return {
            "strategy_name": _STRATEGY_NAME,
            "strategy_version": _STRATEGY_VERSION,
            "action": "PARTIAL_CLOSE",
            "symbol": signal.symbol,
            "side": side_mapped,
            "volume": signal.volume,
            "close_reason": signal.exit_reason or "partial_tp",
            "campaign": self._campaign,
        }

    def _build_entry_payload(self, signal: LiveSignal) -> dict[str, Any] | None:
        """Build PLACE_STOP_ORDER or REPLACE_STOP_ORDER payload.

        Returns None if signal.side is not a recognized value.
        """
        side_mapped = _SIDE_MAP.get(signal.side)
        order_type = _ORDER_TYPE_MAP.get(signal.side)
        if side_mapped is None or order_type is None:
            log.error(
                "Unknown signal side %r — cannot build entry payload (expected LONG/SHORT)",
                signal.side,
            )
            return None
        action = self._resolve_entry_action(signal.side)

        # --- ATR regime-based position sizing adjustment ---
        volume = self._apply_atr_regime_sizing(signal.volume, signal.symbol)

        payload = {
            "strategy_name": _STRATEGY_NAME,
            "strategy_version": _STRATEGY_VERSION,
            "action": action,
            "signal_type": "signal_alert",
            "symbol": signal.symbol,
            "broker_symbol": self._cfg.ftmo.resolve_symbol(signal.symbol),
            "side": side_mapped,
            "order_type": order_type,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "volume": volume,
            "bar_close_time": int(signal.timestamp),
            "campaign": self._campaign,
        }

        # Apply price normalization to fix MetaAPI precision issues
        return PriceNormalizer.normalize_payload_prices(payload)

    def _apply_atr_regime_sizing(self, base_volume: float, symbol: str) -> float:
        """Apply ATR regime risk factor to position volume.

        When NOVATRADE_ATR_REGIME_SIZING_ENABLED=true, reads the current
        regime from STATE/novatrade/regime.json and scales the base volume
        by the zone's risk factor (0.0–1.0).

        When the flag is off or the regime file is unavailable/stale/malformed,
        the base volume is returned unchanged.

        Returns:
            Adjusted volume (>= min_volume_per_trade), rounded to 2 decimals.
        """
        risk_cfg = self._cfg.risk
        enabled = risk_cfg.atr_regime_sizing_enabled

        if not enabled:
            log.debug(
                "ATR regime sizing DISABLED — using base volume %.2f for %s",
                base_volume,
                symbol,
            )
            return base_volume

        # Load regime with safety guards
        risk_factor, atr_zone, fallback_reason = load_regime_safe(
            staleness_seconds=risk_cfg.atr_regime_staleness_seconds,
        )

        if fallback_reason:
            log.warning(
                "ATR regime sizing FALLBACK for %s: %s — using base volume %.2f (factor=1.0)",
                symbol,
                fallback_reason,
                base_volume,
            )
            return base_volume

        # Apply risk factor
        adjusted_volume = round(base_volume * risk_factor, 2)
        adjusted_volume = max(risk_cfg.min_volume_per_trade, adjusted_volume)

        log.info(
            "ATR regime sizing APPLIED for %s: atr_zone=%s risk_factor=%.2f base_volume=%.2f adjusted_volume=%.2f",
            symbol,
            atr_zone,
            risk_factor,
            base_volume,
            adjusted_volume,
        )

        # If risk_factor is 0.0 (compression/extreme zones), volume floors
        # at min_volume_per_trade.  The ATR zone-based trade BLOCKING is a
        # separate feature (not yet enabled) — this only scales volume.
        if risk_factor == 0.0:
            log.warning(
                "ATR regime sizing: zone=%s has risk_factor=0.0 — "
                "volume floored to min %.2f.  Consider enabling ATR-based "
                "trade blocking as a separate feature for full protection.",
                atr_zone,
                adjusted_volume,
            )

        return adjusted_volume

    def _build_exit_payload(self, signal: LiveSignal) -> dict[str, Any] | None:
        """Build CLOSE_POSITION payload."""
        side_mapped = _SIDE_MAP.get(signal.side)
        if side_mapped is None:
            log.error("Unknown signal side %r — cannot build exit payload", signal.side)
            return None
        return {
            "strategy_name": _STRATEGY_NAME,
            "strategy_version": _STRATEGY_VERSION,
            "action": "CLOSE_POSITION",
            "symbol": signal.symbol,
            "side": side_mapped,
            "close_reason": signal.exit_reason or "STRATEGY_EXIT",
            "campaign": self._campaign,
        }

    def _build_modify_sl_payload(self, signal: LiveSignal) -> dict[str, Any] | None:
        """Build MODIFY_SL payload."""
        side_mapped = _SIDE_MAP.get(signal.side)
        if side_mapped is None:
            log.error("Unknown signal side %r — cannot build modify_sl payload", signal.side)
            return None
        payload = {
            "strategy_name": _STRATEGY_NAME,
            "strategy_version": _STRATEGY_VERSION,
            "action": "MODIFY_SL",
            "symbol": signal.symbol,
            "side": side_mapped,
            "old_stop": signal.metadata.get("old_stop", 0.0),
            "new_stop": signal.new_stop,
            "campaign": self._campaign,
        }

        # Apply price normalization to fix MetaAPI precision issues
        return PriceNormalizer.normalize_payload_prices(payload)

    def _build_cancel_payload(self, signal: LiveSignal) -> dict[str, Any] | None:
        """Build CANCEL_ORDER payload."""
        side_mapped = _SIDE_MAP.get(signal.side)
        if side_mapped is None:
            log.error("Unknown signal side %r — cannot build cancel payload", signal.side)
            return None
        return {
            "strategy_name": _STRATEGY_NAME,
            "strategy_version": _STRATEGY_VERSION,
            "action": "CANCEL_ORDER",
            "symbol": signal.symbol,
            "side": side_mapped,
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

        # --- Post-trade validation (best-effort, never crashes pipeline) ---
        self._run_post_trade_validation(
            symbol=signal.symbol,
            side=_SIDE_MAP.get(signal.side, signal.side),
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            volume=signal.volume,
            fill_price=signal.entry_price,
            order_id=position_id,
            metadata=signal.metadata,
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
