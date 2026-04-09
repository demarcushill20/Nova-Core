"""IRB Trading Agent — Phase 5 runtime for the NovaTrade Demo Test Run.

Receives validated IRB alert payloads from Pine/TradingView, transforms them
into governed execution intents, routes through the risk engine, and executes
via the broker adapter.

5-state FSM: FLAT -> PENDING_LONG/PENDING_SHORT -> LONG/SHORT -> FLAT

Alert types handled:
  signal_alert  -> PLACE_STOP_ORDER / REPLACE_STOP_ORDER
  trail_alert   -> MODIFY_SL
  cancel_alert  -> CANCEL_ORDER
  close_alert   -> CLOSE_POSITION

Design constraints (Phase 5 scope):
  - No retry logic (fail-fast, record evidence)
  - No polling loop (events are push-driven via webhooks)
  - No strategy redesign (consumes Pine alerts as-is)
  - No risk engine redesign (integrates with existing RiskEngine)
  - Provider-neutral (uses MT5Adapter ABC)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from novatrade.adapter.base import MT5Adapter
from novatrade.config import NovaTradeCfg
from novatrade.models import (
    EvidenceRecord,
    EvidenceType,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
    RiskVerdict,
)
from novatrade.risk.hard_risk_supervisor import HardRiskSupervisor
from novatrade.risk.risk_engine import RiskEngine
from novatrade.storage.state_store import StateStore
from novatrade.validation.evidence import EvidenceRecorder

log = logging.getLogger("novatrade.execution.trading_agent")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AgentState(Enum):
    """Trading Agent FSM state — mirrors strategy.pine 5-state model."""

    FLAT = "FLAT"
    PENDING_LONG = "PENDING_LONG"
    PENDING_SHORT = "PENDING_SHORT"
    LONG = "LONG"
    SHORT = "SHORT"


class IntentType(Enum):
    """Order intent types derived from alert actions."""

    PLACE_ORDER = "PLACE_ORDER"
    REPLACE_ORDER = "REPLACE_ORDER"
    MODIFY_SL = "MODIFY_SL"
    CANCEL_ORDER = "CANCEL_ORDER"
    CLOSE_POSITION = "CLOSE_POSITION"


class AlertAction(Enum):
    """Actions from alert payloads (matches alerts_schema.json)."""

    PLACE_STOP_ORDER = "PLACE_STOP_ORDER"
    REPLACE_STOP_ORDER = "REPLACE_STOP_ORDER"
    MODIFY_SL = "MODIFY_SL"
    CANCEL_ORDER = "CANCEL_ORDER"
    CLOSE_POSITION = "CLOSE_POSITION"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"  # v5: close a fraction of open volume


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class OrderIntent:
    """Normalized order intent created from a validated alert.

    This is the governed intermediate representation between raw alert
    ingestion and broker execution.  Every intent has an idempotency key,
    broker-resolved symbol, and full traceability metadata.
    """

    intent_type: IntentType
    idempotency_key: str
    broker_symbol: str
    side: OrderSide
    strategy_id: str = "Rob Hoffman IRB"
    strategy_version: str = "5.0.0"
    campaign: str = ""
    source_bar_time: int = 0
    created_at: float = field(default_factory=time.time)

    # PLACE/REPLACE fields
    order_type: OrderType | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    volume: float | None = None

    # MODIFY_SL fields
    new_stop_loss: float | None = None
    old_stop_loss: float | None = None

    # CANCEL fields
    cancel_reason: str = ""

    # CLOSE fields
    close_reason: str = ""

    def to_dict(self) -> dict:
        d: dict = {
            "intent_type": self.intent_type.value,
            "idempotency_key": self.idempotency_key,
            "broker_symbol": self.broker_symbol,
            "side": self.side.value,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "campaign": self.campaign,
            "source_bar_time": self.source_bar_time,
            "created_at": self.created_at,
        }
        if self.intent_type in (IntentType.PLACE_ORDER, IntentType.REPLACE_ORDER):
            d["order_type"] = self.order_type.value if self.order_type else None
            d["entry_price"] = self.entry_price
            d["stop_loss"] = self.stop_loss
            d["volume"] = self.volume
        elif self.intent_type == IntentType.MODIFY_SL:
            d["new_stop_loss"] = self.new_stop_loss
            d["old_stop_loss"] = self.old_stop_loss
        elif self.intent_type == IntentType.CANCEL_ORDER:
            d["cancel_reason"] = self.cancel_reason
        elif self.intent_type == IntentType.CLOSE_POSITION:
            d["close_reason"] = self.close_reason
        return d


@dataclass
class AgentResult:
    """Result of processing a single alert through the Trading Agent."""

    success: bool
    intent: OrderIntent | None = None
    state_before: AgentState = AgentState.FLAT
    state_after: AgentState = AgentState.FLAT
    order_result: OrderResult | None = None
    error: str = ""
    rejected_reason: str = ""
    elapsed_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)

    @property
    def rejected(self) -> bool:
        return bool(self.rejected_reason)


# ---------------------------------------------------------------------------
# Alert validation
# ---------------------------------------------------------------------------

_SIGNAL_REQUIRED = frozenset(
    {
        "strategy_name",
        "strategy_version",
        "action",
        "signal_type",
        "symbol",
        "broker_symbol",
        "side",
        "order_type",
        "entry_price",
        "stop_loss",
        "volume",
        "bar_close_time",
        "campaign",
    }
)

_TRAIL_REQUIRED = frozenset(
    {
        "strategy_name",
        "strategy_version",
        "action",
        "symbol",
        "side",
        "old_stop",
        "new_stop",
        "campaign",
    }
)

_CANCEL_REQUIRED = frozenset(
    {
        "strategy_name",
        "strategy_version",
        "action",
        "symbol",
        "side",
        "cancel_reason",
        "campaign",
    }
)

_CLOSE_REQUIRED = frozenset(
    {
        "strategy_name",
        "strategy_version",
        "action",
        "symbol",
        "side",
        "close_reason",
        "campaign",
    }
)

_PARTIAL_CLOSE_REQUIRED = frozenset(
    {
        "strategy_name",
        "strategy_version",
        "action",
        "symbol",
        "side",
        "volume",
        "close_reason",
        "campaign",
    }
)

_VALID_ACTIONS = frozenset(
    {
        "PLACE_STOP_ORDER",
        "REPLACE_STOP_ORDER",
        "MODIFY_SL",
        "CANCEL_ORDER",
        "CLOSE_POSITION",
        "PARTIAL_CLOSE",
    }
)


def validate_alert(payload: dict) -> tuple[str | None, str]:
    """Validate an alert payload against the alerts_schema.json contract.

    Returns ``(error, action)``.  If *error* is ``None`` the alert is valid.
    """
    action = payload.get("action")
    if not action or action not in _VALID_ACTIONS:
        return f"invalid or missing action: {action!r}", ""

    if payload.get("strategy_name") != "Rob Hoffman IRB":
        return f"unknown strategy: {payload.get('strategy_name')!r}", ""

    # Enhanced version validation with debugging info
    expected_version = "5.0.0"
    actual_version = payload.get("strategy_version")
    if actual_version != expected_version:
        return (
            f"version mismatch: got {actual_version!r} (type {type(actual_version).__name__}), "
            f"expected {expected_version!r} (type {type(expected_version).__name__})"
        ), ""

    # Required-field check per action type
    if action in ("PLACE_STOP_ORDER", "REPLACE_STOP_ORDER"):
        required = _SIGNAL_REQUIRED
    elif action == "MODIFY_SL":
        required = _TRAIL_REQUIRED
    elif action == "CANCEL_ORDER":
        required = _CANCEL_REQUIRED
    elif action == "CLOSE_POSITION":
        required = _CLOSE_REQUIRED
    elif action == "PARTIAL_CLOSE":
        required = _PARTIAL_CLOSE_REQUIRED
    else:
        return f"unhandled action: {action}", ""

    missing = required - set(payload.keys())
    if missing:
        return f"missing fields for {action}: {sorted(missing)}", ""

    # Side validation
    side = payload.get("side")
    if side not in ("BUY", "SELL"):
        return f"invalid side: {side!r}", ""

    # Type-specific value validation
    if action in ("PLACE_STOP_ORDER", "REPLACE_STOP_ORDER"):
        if not isinstance(payload.get("entry_price"), (int, float)) or payload["entry_price"] <= 0:
            return "entry_price must be a positive number", ""
        if not isinstance(payload.get("stop_loss"), (int, float)) or payload["stop_loss"] <= 0:
            return "stop_loss must be a positive number", ""
        if "volume" in payload and not isinstance(payload["volume"], (int, float)):
            return "volume must be a number", ""
        # volume <= 0 or missing is allowed — triggers auto-sizing in PreTradeGate
        ot = payload.get("order_type")
        if ot not in ("BUY_STOP", "SELL_STOP"):
            return f"invalid order_type: {ot!r}", ""
    elif action == "MODIFY_SL":
        if not isinstance(payload.get("new_stop"), (int, float)) or payload["new_stop"] <= 0:
            return "new_stop must be a positive number", ""
    elif action == "PARTIAL_CLOSE":
        vol = payload.get("volume")
        if not isinstance(vol, (int, float)) or vol <= 0:
            return "PARTIAL_CLOSE volume must be a positive number", ""

    return None, action


_ACTION_ABBREV = {
    "PLACE_STOP_ORDER": "PS",
    "REPLACE_STOP_ORDER": "RS",
    "MODIFY_SL": "MS",
    "CANCEL_ORDER": "CX",
    "CLOSE_POSITION": "CP",
    "PARTIAL_CLOSE": "PC",
}
_SIDE_ABBREV = {"BUY": "B", "SELL": "S"}


def make_idempotency_key(payload: dict) -> str:
    """Deterministic idempotency key from alert payload.

    Broker clientId format: ``{strategyId}_{positionId}_{orderId}``
    with underscore separators. Max 15 chars to stay within broker limits.

    Format: ``IRB_{abbrev}{time6}_{side}`` (max 14 chars).

    Ensures one intent per (action, bar, direction) combination.  For alerts
    that lack ``bar_close_time`` (cancel/close), ``bars_elapsed``/``bars_held``
    is used as the temporal discriminator.
    """
    action = payload.get("action", "UNKNOWN")
    action_short = _ACTION_ABBREV.get(action, action[:2])
    bar_time = payload.get(
        "bar_close_time",
        payload.get("bars_elapsed", payload.get("bars_held", int(time.time()))),
    )
    # Compact the temporal part: use last 6 digits for uniqueness
    bar_str = str(bar_time)
    digits = "".join(c for c in bar_str if c.isdigit())
    time_compact = digits[-6:] if len(digits) >= 6 else digits.ljust(6, "0")
    side = payload.get("side", "UNKNOWN")
    side_short = _SIDE_ABBREV.get(side, side[:1])
    # Broker pattern: {strategyId}_{positionId}_{orderId}
    return f"IRB_{action_short}{time_compact}_{side_short}"


# ---------------------------------------------------------------------------
# Trading Agent
# ---------------------------------------------------------------------------


class TradingAgent:
    """IRB Trading Agent runtime.

    Processes alert payloads through the full governed pipeline:

    1. **Validate** — verify alert contract compliance
    2. **Idempotency** — reject duplicate submissions
    3. **Intent** — normalize into :class:`OrderIntent`
    4. **State check** — verify FSM transition is legal
    5. **Symbol normalize** — resolve broker symbol via FtmoProfile
    6. **Risk check** — delegate to :class:`RiskEngine`
    7. **Execute** — route to adapter
    8. **State transition** — update FSM
    9. **Evidence** — record every action

    Args:
        cfg: NovaTrade configuration (symbol mapping, risk params).
        adapter: Broker adapter implementing :class:`MT5Adapter`.
        risk_engine: Initialized :class:`RiskEngine` instance.
        recorder: Optional evidence recorder for audit trail.
    """

    def __init__(
        self,
        cfg: NovaTradeCfg,
        adapter: MT5Adapter,
        risk_engine: RiskEngine,
        recorder: EvidenceRecorder | None = None,
        supervisor: HardRiskSupervisor | None = None,
        state_store: StateStore | None = None,
    ) -> None:
        self._cfg = cfg
        self._adapter = adapter
        self._risk = risk_engine
        self._recorder = recorder
        self._supervisor = supervisor
        self._state_store = state_store

        # FSM state
        self._state = AgentState.FLAT
        self._pending_order_id: str | None = None
        self._position_id: str | None = None
        self._pending_side: OrderSide | None = None
        self._position_side: OrderSide | None = None
        self._pending_symbol: str | None = None
        self._position_symbol: str | None = None
        self._position_volume: float = 0.0

        # Idempotency — bounded set of seen keys
        self._seen_keys: set[str] = set()
        self._max_seen_keys = 1000

        # Restore persisted state if available
        self._load_persisted_state()

    # -- Properties --------------------------------------------------------

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def pending_order_id(self) -> str | None:
        return self._pending_order_id

    @property
    def position_id(self) -> str | None:
        return self._position_id

    @property
    def position_symbol(self) -> str | None:
        return self._position_symbol

    @property
    def position_volume(self) -> float:
        return self._position_volume

    # -- Main entry point --------------------------------------------------

    async def process_alert(self, payload: dict) -> AgentResult:
        """Process a single alert payload end-to-end.

        Takes a raw alert dict (parsed from the webhook JSON), validates it,
        creates an order intent, checks risk, executes, and updates state.
        """
        t0 = time.monotonic()
        state_before = self._state

        try:
            result = await self._process_inner(payload, t0)
            result.state_before = state_before
            return result
        except Exception as exc:
            elapsed = (time.monotonic() - t0) * 1000
            error_msg = f"Unhandled exception: {exc}"
            log.error("trading_agent exception: %s", error_msg)
            self._record_error(error_msg, {"payload": _safe_payload(payload)})
            return AgentResult(
                success=False,
                state_before=state_before,
                state_after=self._state,
                error=error_msg,
                elapsed_ms=elapsed,
            )

    async def _process_inner(self, payload: dict, t0: float) -> AgentResult:
        # 0. Fast-reject if risk engine is halted
        if self._risk.halted:
            elapsed = (time.monotonic() - t0) * 1000
            log.info("alert rejected — risk engine halted: %s", self._risk.halt_reason)
            self._record_event(
                "RISK_HALT",
                {
                    "reason": self._risk.halt_reason,
                    "payload_action": payload.get("action", ""),
                },
            )
            return AgentResult(
                success=False,
                state_after=self._state,
                rejected_reason=f"risk_halt: {self._risk.halt_reason}",
                elapsed_ms=elapsed,
            )

        # 1. Validate
        error, action = validate_alert(payload)
        if error:
            elapsed = (time.monotonic() - t0) * 1000

            # Enhanced logging for validation failures
            log.warning("alert validation failed: %s", error)
            log.warning("validation failure details - payload: %s", _safe_payload(payload))
            log.warning(
                "validation failure details - expected strategy_name: 'Rob Hoffman IRB', got: %r",
                payload.get("strategy_name"),
            )
            log.warning(
                "validation failure details - expected strategy_version: '5.0.0', got: %r (type: %s)",
                payload.get("strategy_version"),
                type(payload.get("strategy_version")).__name__,
            )
            log.warning(
                "validation failure details - action: %r, expected one of: %s",
                payload.get("action"),
                sorted(_VALID_ACTIONS),
            )

            # Check for common validation failure patterns
            if "version mismatch" in error:
                actual_version = payload.get("strategy_version")
                log.warning(
                    "VERSION MISMATCH DEBUG - actual: %r, expected: '5.0.0', equal: %s, repr equal: %s",
                    actual_version,
                    actual_version == "5.0.0",
                    repr(actual_version) == repr("5.0.0"),
                )

            self._record_error(
                f"validation: {error}",
                {
                    "payload": _safe_payload(payload),
                    "debug_info": {
                        "strategy_name_match": payload.get("strategy_name") == "Rob Hoffman IRB",
                        "strategy_version_match": payload.get("strategy_version") == "5.0.0",
                        "strategy_version_type": type(payload.get("strategy_version")).__name__,
                        "action_valid": payload.get("action") in _VALID_ACTIONS,
                        "payload_keys": list(payload.keys()) if isinstance(payload, dict) else "not_dict",
                    },
                },
            )
            return AgentResult(
                success=False,
                state_after=self._state,
                rejected_reason=f"validation_failed: {error}",
                elapsed_ms=elapsed,
            )

        # 2. Idempotency
        idem_key = make_idempotency_key(payload)
        if idem_key in self._seen_keys:
            elapsed = (time.monotonic() - t0) * 1000
            log.info("duplicate alert suppressed: %s", idem_key)
            return AgentResult(
                success=False,
                state_after=self._state,
                rejected_reason=f"duplicate: {idem_key}",
                elapsed_ms=elapsed,
            )

        # 3. Route by action
        action_enum = AlertAction(action)

        if action_enum in (AlertAction.PLACE_STOP_ORDER, AlertAction.REPLACE_STOP_ORDER):
            result = await self._handle_signal(payload, action_enum, idem_key, t0)
        elif action_enum == AlertAction.MODIFY_SL:
            result = await self._handle_trail(payload, idem_key, t0)
        elif action_enum == AlertAction.CANCEL_ORDER:
            result = await self._handle_cancel(payload, idem_key, t0)
        elif action_enum == AlertAction.CLOSE_POSITION:
            result = await self._handle_close(payload, idem_key, t0)
        elif action_enum == AlertAction.PARTIAL_CLOSE:
            result = await self._handle_partial_close(payload, idem_key, t0)
        else:
            elapsed = (time.monotonic() - t0) * 1000
            return AgentResult(
                success=False,
                state_after=self._state,
                rejected_reason=f"unknown_action: {action}",
                elapsed_ms=elapsed,
            )

        # 4. Mark key as seen on success
        if result.success:
            self._seen_keys.add(idem_key)
            self._prune_seen_keys()
            if self._state_store is not None:
                try:
                    self._state_store.add_idempotency_key(idem_key)
                    self._state_store.prune_idempotency_keys(self._max_seen_keys)
                except Exception:
                    log.exception("state_store: failed to persist idempotency key")

        return result

    # -- Signal handler (PLACE / REPLACE) ----------------------------------

    async def _handle_signal(
        self,
        payload: dict,
        action: AlertAction,
        idem_key: str,
        t0: float,
    ) -> AgentResult:
        side = OrderSide(payload["side"])
        broker_symbol = self._resolve_symbol(payload)

        # State validation
        if action == AlertAction.PLACE_STOP_ORDER:
            if self._state != AgentState.FLAT:
                elapsed = (time.monotonic() - t0) * 1000
                return AgentResult(
                    success=False,
                    state_after=self._state,
                    rejected_reason=(f"PLACE_STOP_ORDER invalid from {self._state.value}"),
                    elapsed_ms=elapsed,
                )
        elif action == AlertAction.REPLACE_STOP_ORDER:
            expected = AgentState.PENDING_LONG if side == OrderSide.BUY else AgentState.PENDING_SHORT
            if self._state != expected:
                elapsed = (time.monotonic() - t0) * 1000
                return AgentResult(
                    success=False,
                    state_after=self._state,
                    rejected_reason=(
                        f"REPLACE_STOP_ORDER invalid from {self._state.value} (expected {expected.value})"
                    ),
                    elapsed_ms=elapsed,
                )

        # Build intent
        intent = OrderIntent(
            intent_type=(
                IntentType.PLACE_ORDER if action == AlertAction.PLACE_STOP_ORDER else IntentType.REPLACE_ORDER
            ),
            idempotency_key=idem_key,
            broker_symbol=broker_symbol,
            side=side,
            campaign=payload.get("campaign", ""),
            source_bar_time=payload.get("bar_close_time", 0),
            order_type=OrderType.STOP,
            entry_price=payload["entry_price"],
            stop_loss=payload["stop_loss"],
            volume=payload["volume"],
        )

        # Cancel existing pending order on REPLACE
        if action == AlertAction.REPLACE_STOP_ORDER and self._pending_order_id:
            try:
                await self._adapter.cancel_order(self._pending_order_id)
                self._risk.record_server_request("cancel_for_replace")
                log.info(
                    "cancelled pending order %s for replacement",
                    self._pending_order_id,
                )
            except Exception as exc:
                log.warning(
                    "failed to cancel pending order %s: %s — continuing",
                    self._pending_order_id,
                    exc,
                )

        # Build OrderRequest — auto-size if volume is missing or zero
        from novatrade.models import VOLUME_AUTO_SIZE

        raw_volume = payload.get("volume", 0)
        order_volume = VOLUME_AUTO_SIZE if (not raw_volume or raw_volume <= 0) else raw_volume

        order_req = OrderRequest(
            symbol=broker_symbol,
            side=side,
            order_type=OrderType.STOP,
            volume=order_volume,
            price=payload["entry_price"],
            stop_loss=payload["stop_loss"],
            idempotency_key=idem_key,
            strategy_id="Rob Hoffman IRB",
            strategy_version="5.0.0",
        )

        # --- Hard Risk Supervisor veto (BEFORE RiskEngine) ---
        account = await self._adapter.get_account()
        positions = await self._adapter.get_positions()

        if self._supervisor is not None:
            pos_dicts = [
                {
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "volume": p.volume,
                    "position_id": p.position_id,
                }
                for p in positions
            ]
            sv = self._supervisor.veto(
                symbol=broker_symbol,
                side=payload["side"],
                volume=payload["volume"],
                entry_price=payload["entry_price"],
                stop_loss=payload.get("stop_loss"),
                account_equity=account.equity,
                open_positions=pos_dicts,
            )
            if sv.vetoed:
                elapsed = (time.monotonic() - t0) * 1000
                log.info(
                    "SUPERVISOR VETO: %s — %s",
                    sv.rule,
                    sv.detail,
                )
                self._record_event(
                    "SUPERVISOR_VETO",
                    {
                        "intent": intent.to_dict(),
                        "rule": sv.rule,
                        "detail": sv.detail,
                        "verdict": sv.verdict.value,
                    },
                )
                return AgentResult(
                    success=False,
                    intent=intent,
                    state_after=self._state,
                    rejected_reason=f"supervisor_{sv.verdict.value.lower()}: {sv.rule}",
                    elapsed_ms=elapsed,
                )

            # Apply supervisor-adjusted volume (risk gate scaled down to fit cap)
            if sv.adjusted_volume is not None and sv.adjusted_volume != order_req.volume:
                log.info(
                    "Supervisor adjusted volume %.2f → %.2f",
                    order_req.volume,
                    sv.adjusted_volume,
                )
                order_req = OrderRequest(
                    symbol=order_req.symbol,
                    side=order_req.side,
                    order_type=order_req.order_type,
                    volume=sv.adjusted_volume,
                    price=order_req.price,
                    stop_loss=order_req.stop_loss,
                    take_profit=order_req.take_profit,
                    comment=order_req.comment,
                    idempotency_key=order_req.idempotency_key,
                    strategy_id=order_req.strategy_id,
                    strategy_version=order_req.strategy_version,
                    max_slippage_pips=order_req.max_slippage_pips,
                )

        # Risk check
        decision = self._risk.pre_trade_check(order_req, account, positions)

        if decision.denied:
            elapsed = (time.monotonic() - t0) * 1000
            is_halt = decision.verdict == RiskVerdict.HALT
            event_name = "RISK_HALT" if is_halt else "RISK_DENIED"
            log.info(
                "risk %s: %s — %s",
                "HALT" if is_halt else "DENIED",
                decision.rule,
                decision.reason,
            )
            self._record_event(
                event_name,
                {
                    "intent": intent.to_dict(),
                    "verdict": decision.verdict.value,
                    "rule": decision.rule,
                    "reason": decision.reason,
                    "policy_layer": decision.policy_layer,
                },
            )
            return AgentResult(
                success=False,
                intent=intent,
                state_after=self._state,
                rejected_reason=f"risk_{decision.verdict.value.lower()}: {decision.rule}",
                elapsed_ms=elapsed,
            )

        # Dynamic spread adjustment: widen SL based on live spread if it
        # exceeds the static buffer baked into the signal by the strategy.
        # The strategy uses sl_spread_buffer_pips (default 1.0 pip) which is
        # static; live spread may be wider (e.g. news, Asian session, exotic
        # pairs). We query the current bid/ask and apply the difference.
        order_req = await self._adjust_sl_for_live_spread(order_req, side, broker_symbol)

        # Execute
        order_result = await self._adapter.place_order(order_req)
        elapsed = (time.monotonic() - t0) * 1000

        if not order_result.ok:
            log.warning("adapter error placing order: %s", order_result.error)
            self._record_event(
                "ADAPTER_ERROR",
                {
                    "intent": intent.to_dict(),
                    "error": order_result.error,
                },
            )
            return AgentResult(
                success=False,
                intent=intent,
                state_after=self._state,
                order_result=order_result,
                error=order_result.error,
                elapsed_ms=elapsed,
            )

        # Notify supervisor of trade opened
        if self._supervisor is not None:
            self._supervisor.on_trade_opened(payload["volume"])

        # State transition
        new_state = AgentState.PENDING_LONG if side == OrderSide.BUY else AgentState.PENDING_SHORT
        self._state = new_state
        self._pending_order_id = order_result.order_id
        self._pending_side = side
        self._pending_symbol = broker_symbol
        self._persist()

        log.info(
            "%s -> %s order_id=%s entry=%.5f sl=%.5f vol=%.2f",
            intent.intent_type.value,
            new_state.value,
            order_result.order_id,
            payload["entry_price"],
            payload["stop_loss"],
            payload["volume"],
        )
        self._record_event(
            "ORDER_PLACED",
            {
                "intent": intent.to_dict(),
                "order_id": order_result.order_id,
                "state": new_state.value,
            },
        )

        return AgentResult(
            success=True,
            intent=intent,
            state_after=new_state,
            order_result=order_result,
            elapsed_ms=elapsed,
        )

    # -- Dynamic spread SL adjustment --------------------------------------

    async def _adjust_sl_for_live_spread(
        self,
        order_req: OrderRequest,
        side: OrderSide,
        broker_symbol: str,
    ) -> OrderRequest:
        """Widen stop-loss if live spread exceeds the static buffer.

        The strategy bakes in a static sl_spread_buffer_pips (default 1.0).
        If the live bid/ask spread is wider, we add the difference so the SL
        has room to breathe past the current spread. Fail-open: if we can't
        fetch the price, return the order unchanged.
        """
        if order_req.stop_loss is None:
            return order_req
        try:
            price = await self._adapter.get_symbol_price(broker_symbol)
        except Exception:
            log.debug("spread adjust: could not fetch price for %s — skipping", broker_symbol)
            return order_req

        if price is None or price.spread <= 0:
            return order_req

        pip_value = 0.0001  # standard 4-digit FX pip
        live_spread_pips = price.spread / pip_value
        static_buffer = self._cfg.risk.sl_spread_buffer_pips
        extra_pips = live_spread_pips - static_buffer
        if extra_pips <= 0:
            return order_req

        # Cap the extra adjustment
        max_extra = self._cfg.risk.sl_spread_max_extra_pips
        extra_pips = min(extra_pips, max_extra)

        original_sl = order_req.stop_loss
        if side == OrderSide.BUY:
            order_req.stop_loss = original_sl - (extra_pips * pip_value)
        else:
            order_req.stop_loss = original_sl + (extra_pips * pip_value)

        log.info(
            "spread adjust: live=%.1f pips, static_buf=%.1f, extra=%.1f → SL %.5f→%.5f",
            live_spread_pips,
            static_buffer,
            extra_pips,
            original_sl,
            order_req.stop_loss,
        )
        return order_req

    # -- Trail handler (MODIFY_SL) -----------------------------------------

    async def _handle_trail(
        self,
        payload: dict,
        idem_key: str,
        t0: float,
    ) -> AgentResult:
        if self._state not in (AgentState.LONG, AgentState.SHORT):
            elapsed = (time.monotonic() - t0) * 1000
            return AgentResult(
                success=False,
                state_after=self._state,
                rejected_reason=f"MODIFY_SL invalid from {self._state.value}",
                elapsed_ms=elapsed,
            )

        if not self._position_id:
            elapsed = (time.monotonic() - t0) * 1000
            return AgentResult(
                success=False,
                state_after=self._state,
                rejected_reason="MODIFY_SL but no tracked position_id",
                elapsed_ms=elapsed,
            )

        new_stop = payload["new_stop"]
        side = OrderSide(payload["side"])
        broker_symbol = self._resolve_symbol(payload)

        intent = OrderIntent(
            intent_type=IntentType.MODIFY_SL,
            idempotency_key=idem_key,
            broker_symbol=broker_symbol,
            side=side,
            campaign=payload.get("campaign", ""),
            new_stop_loss=new_stop,
            old_stop_loss=payload.get("old_stop"),
        )

        order_result = await self._adapter.modify_order(
            self._position_id,
            stop_loss=new_stop,
        )
        elapsed = (time.monotonic() - t0) * 1000

        if not order_result.ok:
            log.warning("adapter error modifying SL: %s", order_result.error)
            self._record_event(
                "MODIFY_SL_ERROR",
                {
                    "intent": intent.to_dict(),
                    "position_id": self._position_id,
                    "error": order_result.error,
                },
            )
            return AgentResult(
                success=False,
                intent=intent,
                state_after=self._state,
                order_result=order_result,
                error=order_result.error,
                elapsed_ms=elapsed,
            )

        # Track FTMO server request
        self._risk.record_server_request("modify_sl")

        log.info(
            "SL modified: position=%s old=%.5f new=%.5f",
            self._position_id,
            payload.get("old_stop", 0),
            new_stop,
        )
        self._record_event(
            "SL_MODIFIED",
            {
                "intent": intent.to_dict(),
                "position_id": self._position_id,
            },
        )

        return AgentResult(
            success=True,
            intent=intent,
            state_after=self._state,
            order_result=order_result,
            elapsed_ms=elapsed,
        )

    # -- Cancel handler (CANCEL_ORDER) -------------------------------------

    async def _handle_cancel(
        self,
        payload: dict,
        idem_key: str,
        t0: float,
    ) -> AgentResult:
        if self._state not in (AgentState.PENDING_LONG, AgentState.PENDING_SHORT):
            elapsed = (time.monotonic() - t0) * 1000
            return AgentResult(
                success=False,
                state_after=self._state,
                rejected_reason=f"CANCEL_ORDER invalid from {self._state.value}",
                elapsed_ms=elapsed,
            )

        if not self._pending_order_id:
            elapsed = (time.monotonic() - t0) * 1000
            return AgentResult(
                success=False,
                state_after=self._state,
                rejected_reason="CANCEL_ORDER but no tracked pending_order_id",
                elapsed_ms=elapsed,
            )

        side = OrderSide(payload["side"])
        broker_symbol = self._resolve_symbol(payload)

        intent = OrderIntent(
            intent_type=IntentType.CANCEL_ORDER,
            idempotency_key=idem_key,
            broker_symbol=broker_symbol,
            side=side,
            campaign=payload.get("campaign", ""),
            cancel_reason=payload.get("cancel_reason", ""),
        )

        old_order_id = self._pending_order_id
        order_result = await self._adapter.cancel_order(old_order_id)
        elapsed = (time.monotonic() - t0) * 1000

        if not order_result.ok:
            log.warning("adapter error cancelling order %s: %s", old_order_id, order_result.error)
            self._record_event(
                "CANCEL_ERROR",
                {
                    "intent": intent.to_dict(),
                    "order_id": old_order_id,
                    "error": order_result.error,
                },
            )
            # Transition to FLAT regardless — the pending order may have
            # already expired or filled on the broker side.

        # Track FTMO server request
        self._risk.record_server_request("cancel_order")

        # State transition: PENDING_X -> FLAT
        self._state = AgentState.FLAT
        self._pending_order_id = None
        self._pending_side = None
        self._pending_symbol = None
        self._persist()

        log.info(
            "order cancelled: %s reason=%s -> FLAT",
            old_order_id,
            payload.get("cancel_reason", ""),
        )
        self._record_event(
            "ORDER_CANCELLED",
            {
                "intent": intent.to_dict(),
                "order_id": old_order_id,
            },
        )

        return AgentResult(
            success=True,
            intent=intent,
            state_after=AgentState.FLAT,
            order_result=order_result if order_result.ok else None,
            elapsed_ms=elapsed,
        )

    # -- Close handler (CLOSE_POSITION — time stop) ------------------------

    async def _handle_close(
        self,
        payload: dict,
        idem_key: str,
        t0: float,
    ) -> AgentResult:
        if self._state not in (AgentState.LONG, AgentState.SHORT):
            elapsed = (time.monotonic() - t0) * 1000
            return AgentResult(
                success=False,
                state_after=self._state,
                rejected_reason=f"CLOSE_POSITION invalid from {self._state.value}",
                elapsed_ms=elapsed,
            )

        if not self._position_id:
            elapsed = (time.monotonic() - t0) * 1000
            return AgentResult(
                success=False,
                state_after=self._state,
                rejected_reason="CLOSE_POSITION but no tracked position_id",
                elapsed_ms=elapsed,
            )

        side = OrderSide(payload["side"])
        broker_symbol = self._resolve_symbol(payload)

        intent = OrderIntent(
            intent_type=IntentType.CLOSE_POSITION,
            idempotency_key=idem_key,
            broker_symbol=broker_symbol,
            side=side,
            campaign=payload.get("campaign", ""),
            close_reason=payload.get("close_reason", ""),
        )

        # Get current positions to extract P&L before closing
        pnl_usd = 0.0
        try:
            positions = await self._adapter.get_positions()
            for pos in positions:
                if pos.position_id == self._position_id:
                    pnl_usd = getattr(pos, "unrealized_pnl", 0.0)
                    break
        except Exception as exc:
            log.warning("Failed to get position P&L before close: %s", exc)

        old_position = self._position_id
        order_result = await self._adapter.close_position(old_position)
        elapsed = (time.monotonic() - t0) * 1000

        if not order_result.ok:
            log.warning("adapter error closing position %s: %s", old_position, order_result.error)
            self._record_event(
                "CLOSE_ERROR",
                {
                    "intent": intent.to_dict(),
                    "position_id": old_position,
                    "error": order_result.error,
                },
            )
            return AgentResult(
                success=False,
                intent=intent,
                state_after=self._state,
                order_result=order_result,
                error=order_result.error,
                elapsed_ms=elapsed,
            )

        # Track FTMO server request + notify risk engine
        self._risk.record_server_request("close_position")
        self._risk.on_trade_close(
            position_id=old_position,
            symbol=broker_symbol,
            side=side.value,
            volume=self._position_volume,
            pnl_usd=pnl_usd,  # actual P&L from position before close
            pnl_pips=0.0,
            exit_reason=payload.get("close_reason", "TIME_STOP"),
        )

        # Notify supervisor with actual P&L
        if self._supervisor is not None:
            self._supervisor.on_trade_closed(pnl_usd)

        # State transition: LONG/SHORT -> FLAT
        self._state = AgentState.FLAT
        self._position_id = None
        self._position_side = None
        self._position_symbol = None
        self._position_volume = 0.0
        self._persist()

        log.info(
            "position closed: %s reason=%s -> FLAT",
            old_position,
            payload.get("close_reason", ""),
        )
        self._record_event(
            "POSITION_CLOSED",
            {
                "intent": intent.to_dict(),
                "position_id": old_position,
            },
        )

        return AgentResult(
            success=True,
            intent=intent,
            state_after=AgentState.FLAT,
            order_result=order_result,
            elapsed_ms=elapsed,
        )

    # -- Partial close handler (PARTIAL_CLOSE — v5 partial profit) ----------

    async def _handle_partial_close(
        self,
        payload: dict,
        idem_key: str,
        t0: float,
    ) -> AgentResult:
        """Close a fraction of the open position without leaving LONG/SHORT state.

        Used by the v5 lifecycle to take partial profit at 1R. The remainder
        (the runner) stays open for the breakeven move and ATR trail.
        """
        if self._state not in (AgentState.LONG, AgentState.SHORT):
            elapsed = (time.monotonic() - t0) * 1000
            return AgentResult(
                success=False,
                state_after=self._state,
                rejected_reason=f"PARTIAL_CLOSE invalid from {self._state.value}",
                elapsed_ms=elapsed,
            )

        if not self._position_id:
            elapsed = (time.monotonic() - t0) * 1000
            return AgentResult(
                success=False,
                state_after=self._state,
                rejected_reason="PARTIAL_CLOSE but no tracked position_id",
                elapsed_ms=elapsed,
            )

        partial_volume = float(payload.get("volume", 0.0))
        if partial_volume <= 0:
            elapsed = (time.monotonic() - t0) * 1000
            return AgentResult(
                success=False,
                state_after=self._state,
                rejected_reason="PARTIAL_CLOSE volume must be > 0",
                elapsed_ms=elapsed,
            )

        if partial_volume >= self._position_volume:
            # Partial >= remaining => fall through to a full close instead.
            log.info(
                "PARTIAL_CLOSE volume %.2f >= position volume %.2f — routing to full close",
                partial_volume,
                self._position_volume,
            )
            return await self._handle_close(payload, idem_key, t0)

        side = OrderSide(payload["side"])
        broker_symbol = self._resolve_symbol(payload)

        intent = OrderIntent(
            intent_type=IntentType.CLOSE_POSITION,
            idempotency_key=idem_key,
            broker_symbol=broker_symbol,
            side=side,
            volume=partial_volume,
            campaign=payload.get("campaign", ""),
            close_reason=payload.get("close_reason", "partial_tp"),
        )

        order_result = await self._adapter.close_position(self._position_id, volume=partial_volume)
        elapsed = (time.monotonic() - t0) * 1000

        if not order_result.ok:
            log.warning(
                "adapter error on partial close %s vol=%.2f: %s",
                self._position_id,
                partial_volume,
                order_result.error,
            )
            self._record_event(
                "PARTIAL_CLOSE_ERROR",
                {
                    "intent": intent.to_dict(),
                    "position_id": self._position_id,
                    "partial_volume": partial_volume,
                    "error": order_result.error,
                },
            )
            return AgentResult(
                success=False,
                intent=intent,
                state_after=self._state,
                order_result=order_result,
                error=order_result.error,
                elapsed_ms=elapsed,
            )

        # Track FTMO server request and update tracked volume.
        self._risk.record_server_request("partial_close")
        old_volume = self._position_volume
        self._position_volume = max(0.0, old_volume - partial_volume)
        self._persist()

        log.info(
            "PARTIAL_CLOSE: %s vol %.2f -> remaining %.2f (state stays %s)",
            self._position_id,
            partial_volume,
            self._position_volume,
            self._state.value,
        )
        self._record_event(
            "PARTIAL_CLOSED",
            {
                "intent": intent.to_dict(),
                "position_id": self._position_id,
                "partial_volume": partial_volume,
                "remaining_volume": self._position_volume,
            },
        )

        return AgentResult(
            success=True,
            intent=intent,
            state_after=self._state,  # stays LONG or SHORT — runner is still open
            order_result=order_result,
            elapsed_ms=elapsed,
        )

    # -- External notifications --------------------------------------------

    def notify_fill(
        self,
        position_id: str,
        fill_price: float,
        volume: float,
        stop_loss: float = 0.0,
    ) -> None:
        """Notify the agent that a pending stop order has been filled.

        Called by the monitoring layer when broker fills the pending order.
        Transitions PENDING_LONG -> LONG or PENDING_SHORT -> SHORT.
        """
        if self._state == AgentState.PENDING_LONG:
            self._state = AgentState.LONG
            self._position_id = position_id
            self._position_side = OrderSide.BUY
        elif self._state == AgentState.PENDING_SHORT:
            self._state = AgentState.SHORT
            self._position_id = position_id
            self._position_side = OrderSide.SELL
        else:
            log.warning("notify_fill in state %s — ignoring", self._state.value)
            return

        # Carry symbol from pending state; clear pending trackers
        self._position_symbol = self._pending_symbol
        self._position_volume = volume
        self._pending_order_id = None
        self._pending_side = None
        self._pending_symbol = None
        self._persist()

        # Inform risk engine — use tracked symbol, not hardcoded
        symbol = self._position_symbol or self._cfg.symbols[0]
        resolved = self._resolve_symbol({"symbol": symbol})
        if self._position_side:
            self._risk.on_trade_fill(
                position_id=position_id,
                symbol=resolved,
                side=self._position_side,
                volume=volume,
                fill_price=fill_price,
                stop_loss=stop_loss,
            )

        log.info(
            "fill: PENDING -> %s position=%s at %.5f vol=%.2f",
            self._state.value,
            position_id,
            fill_price,
            volume,
        )
        self._record_event(
            "ORDER_FILLED",
            {
                "position_id": position_id,
                "fill_price": fill_price,
                "volume": volume,
                "state": self._state.value,
            },
        )

    def notify_broker_close(
        self,
        position_id: str,
        exit_reason: str = "SL_HIT",
    ) -> None:
        """Notify the agent that the broker closed a position.

        Called by the monitoring layer when broker-side SL or trailing stop
        triggers.  Pine does NOT emit alerts for SL exits.
        """
        if self._state not in (AgentState.LONG, AgentState.SHORT):
            log.warning(
                "notify_broker_close in state %s — ignoring",
                self._state.value,
            )
            return

        old_state = self._state
        self._state = AgentState.FLAT
        self._position_id = None
        self._position_side = None
        self._position_symbol = None
        self._position_volume = 0.0
        self._persist()

        log.info("broker close: %s -> FLAT reason=%s", old_state.value, exit_reason)
        self._record_event(
            "BROKER_CLOSE",
            {
                "position_id": position_id,
                "exit_reason": exit_reason,
                "state_before": old_state.value,
            },
        )

    def force_flat(self, reason: str = "reconciliation") -> None:
        """Force-reset the agent to FLAT state.

        Called by the monitoring layer when reconciliation detects a stale
        pending order (broker has no matching order or position).  This is
        the only non-alert-driven state transition and must always be
        accompanied by evidence recording.
        """
        old_state = self._state
        self._state = AgentState.FLAT
        self._pending_order_id = None
        self._pending_side = None
        self._pending_symbol = None
        self._position_id = None
        self._position_side = None
        self._position_symbol = None
        self._position_volume = 0.0
        self._persist()

        log.info("force_flat: %s -> FLAT reason=%s", old_state.value, reason)
        self._record_event(
            "FORCE_FLAT",
            {
                "reason": reason,
                "state_before": old_state.value,
            },
        )

    def recover_position(
        self,
        position_id: str,
        side: OrderSide,
        symbol: str,
        volume: float,
        fill_price: float,
        stop_loss: float = 0.0,
    ) -> None:
        """Adopt a broker position discovered on startup.

        Transitions FLAT → LONG/SHORT to reconcile with broker reality.
        Called during startup when the broker has an open position but the
        agent (freshly initialized) is FLAT.  Unlike ``notify_fill``, this
        does NOT require a prior PENDING state.
        """
        if self._state != AgentState.FLAT:
            log.warning(
                "recover_position called in state %s — expected FLAT, ignoring",
                self._state.value,
            )
            return

        self._state = AgentState.LONG if side == OrderSide.BUY else AgentState.SHORT
        self._position_id = position_id
        self._position_side = side
        self._position_symbol = symbol
        self._position_volume = volume
        self._pending_order_id = None
        self._pending_side = None
        self._pending_symbol = None
        self._persist()

        # Inform risk engine about the adopted position
        resolved = self._resolve_symbol({"symbol": symbol})
        self._risk.on_trade_fill(
            position_id=position_id,
            symbol=resolved,
            side=side,
            volume=volume,
            fill_price=fill_price,
            stop_loss=stop_loss,
        )

        log.info(
            "recover_position: FLAT -> %s position=%s %s vol=%.2f at %.5f",
            self._state.value,
            position_id,
            symbol,
            volume,
            fill_price,
        )
        self._record_event(
            "POSITION_RECOVERED",
            {
                "position_id": position_id,
                "side": side.value,
                "symbol": symbol,
                "volume": volume,
                "fill_price": fill_price,
                "stop_loss": stop_loss,
            },
        )

    # -- Internal helpers --------------------------------------------------

    def _resolve_symbol(self, payload: dict) -> str:
        """Resolve broker symbol via FtmoProfile, validating against alert."""
        display = payload.get("symbol", "EURUSD")
        alert_broker = payload.get("broker_symbol", "")
        resolved = self._cfg.ftmo.resolve_symbol(display)

        if alert_broker and alert_broker != resolved:
            log.warning(
                "symbol mismatch: alert=%s resolved=%s — using resolved",
                alert_broker,
                resolved,
            )
        return resolved

    def _record_event(self, event: str, data: dict) -> None:
        """Record a Trading Agent event to the evidence trail."""
        if not self._recorder:
            return
        try:
            self._recorder._append(
                EvidenceRecord(
                    event_type=EvidenceType.EXECUTION,
                    data={"trading_agent_event": event, **data},
                )
            )
        except Exception as exc:
            log.warning("evidence recording failed: %s", exc)

    def _record_error(self, error: str, context: dict) -> None:
        """Record an error to the evidence trail."""
        if not self._recorder:
            return
        try:
            self._recorder.record_error(error, context)
        except Exception as exc:
            log.warning("error recording failed: %s", exc)

    def _prune_seen_keys(self) -> None:
        """Prevent unbounded growth of the idempotency set."""
        if len(self._seen_keys) > self._max_seen_keys:
            excess = len(self._seen_keys) - (self._max_seen_keys // 2)
            keys = list(self._seen_keys)
            self._seen_keys = set(keys[excess:])

    # -- State persistence -------------------------------------------------

    def _load_persisted_state(self) -> None:
        """Restore FSM state and idempotency keys from persistent storage.

        Called once during __init__.  If no store is configured or the DB is
        empty/corrupted, the agent starts fresh in FLAT state (backward compat).
        """
        if self._state_store is None:
            return
        try:
            row = self._state_store.load_agent_state()
            if row is not None:
                self._state = AgentState(row["state"])
                self._pending_order_id = row.get("pending_order_id")
                self._pending_side = OrderSide(row["pending_side"]) if row.get("pending_side") else None
                self._pending_symbol = row.get("pending_symbol")
                self._position_id = row.get("position_id")
                self._position_side = OrderSide(row["position_side"]) if row.get("position_side") else None
                self._position_symbol = row.get("position_symbol")
                self._position_volume = row.get("position_volume", 0.0)
                log.info(
                    "state_store: restored state=%s pending=%s position=%s",
                    self._state.value,
                    self._pending_order_id,
                    self._position_id,
                )

            keys = self._state_store.load_idempotency_keys(self._max_seen_keys)
            if keys:
                self._seen_keys = keys
                log.info("state_store: restored %d idempotency keys", len(keys))
        except Exception:
            log.exception("state_store: failed to load persisted state — starting fresh")
            self._state = AgentState.FLAT

    def _persist(self) -> None:
        """Save current FSM state and any new idempotency keys to the store.

        Called after every state transition.  Wrapped in try/except so a
        persistence failure never blocks trading.
        """
        if self._state_store is None:
            return
        try:
            self._state_store.save_agent_state(
                state=self._state.value,
                pending_order_id=self._pending_order_id,
                pending_side=self._pending_side.value if self._pending_side else None,
                pending_symbol=self._pending_symbol,
                position_id=self._position_id,
                position_side=self._position_side.value if self._position_side else None,
                position_symbol=self._position_symbol,
                position_volume=self._position_volume,
            )
        except Exception:
            log.exception("state_store: failed to persist agent state")


def _safe_payload(payload: dict) -> dict:
    """Create a logging-safe copy of an alert payload."""
    safe: dict = {}
    for k, v in payload.items():
        if isinstance(v, str) and len(v) > 200:
            safe[k] = v[:200] + "..."
        else:
            safe[k] = v
    return safe
