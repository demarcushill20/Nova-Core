"""Tests for the IRB Trading Agent (Phase 5).

Tests cover:
  - Alert validation (all 4 types + rejection cases)
  - Idempotency key generation and duplicate suppression
  - FSM state transitions (valid and invalid)
  - Signal handling (PLACE/REPLACE)
  - Trail handling (MODIFY_SL)
  - Cancel handling (CANCEL_ORDER)
  - Close handling (CLOSE_POSITION)
  - External notifications (fill, broker close)
  - Risk gate integration
  - Symbol resolution
  - Evidence recording
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from novatrade.config import FtmoProfile, NovaTradeCfg, RiskConfig
from novatrade.execution.trading_agent import (
    AgentResult,
    AgentState,
    IntentType,
    OrderIntent,
    TradingAgent,
    make_idempotency_key,
    validate_alert,
)
from novatrade.models import (
    AccountMode,
    AccountState,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)
from novatrade.risk.risk_engine import RiskEngine
from novatrade.validation.evidence import EvidenceRecorder

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_cfg(**overrides) -> NovaTradeCfg:
    """Build a test NovaTradeCfg with FTMO symbol mapping."""
    ftmo = FtmoProfile(
        enabled=True,
        symbol_map={"EURUSD": "EURUSD.sim"},
        campaign_label="test-campaign",
    )
    risk = RiskConfig(max_positions=1)
    return NovaTradeCfg(
        mode=AccountMode.DEMO,
        ftmo=ftmo,
        risk=risk,
        dry_run=False,
        **overrides,
    )


def _make_account(balance: float = 100_000.0) -> AccountState:
    return AccountState(balance=balance, equity=balance, mode=AccountMode.DEMO)


def _make_adapter() -> MagicMock:
    """Create a mock MT5Adapter with standard async methods."""
    adapter = MagicMock()
    adapter.get_account = AsyncMock(return_value=_make_account())
    adapter.get_positions = AsyncMock(return_value=[])
    adapter.place_order = AsyncMock(return_value=OrderResult(ok=True, order_id="ORD-001", status=OrderStatus.PENDING))
    adapter.modify_order = AsyncMock(return_value=OrderResult(ok=True, order_id="POS-001", status=OrderStatus.FILLED))
    adapter.cancel_order = AsyncMock(
        return_value=OrderResult(ok=True, order_id="ORD-001", status=OrderStatus.CANCELLED)
    )
    adapter.close_position = AsyncMock(return_value=OrderResult(ok=True, order_id="POS-001", status=OrderStatus.FILLED))
    return adapter


def _make_risk_engine(cfg: NovaTradeCfg | None = None) -> RiskEngine:
    """Create a RiskEngine initialized with test account state."""
    c = cfg or _make_cfg()
    engine = RiskEngine(c)
    engine.initialize(_make_account())
    return engine


def _make_agent(
    cfg: NovaTradeCfg | None = None,
    adapter: MagicMock | None = None,
    risk_engine: RiskEngine | None = None,
    recorder: EvidenceRecorder | None = None,
) -> TradingAgent:
    """Create a TradingAgent with test dependencies."""
    c = cfg or _make_cfg()
    a = adapter or _make_adapter()
    r = risk_engine or _make_risk_engine(c)
    return TradingAgent(c, a, r, recorder)


def _signal_payload(**overrides) -> dict:
    """Build a valid signal_alert payload."""
    base = {
        "strategy_name": "Rob Hoffman IRB",
        "strategy_version": "2.0.0",
        "action": "PLACE_STOP_ORDER",
        "signal_type": "LONG_IRB",
        "irb_type": "UPTREND_IRB",
        "symbol": "EURUSD",
        "broker_symbol": "EURUSD.sim",
        "timeframe": "H1",
        "side": "BUY",
        "order_type": "BUY_STOP",
        "entry_price": 1.08765,
        "stop_loss": 1.08234,
        "stop_distance_pips": 53.1,
        "volume": 0.19,
        "risk_dollars": 1000.0,
        "bar_close_time": 1710000000000,
        "bar_ohlc_o": 1.08500,
        "bar_ohlc_h": 1.08800,
        "bar_ohlc_l": 1.08200,
        "bar_ohlc_c": 1.08700,
        "irb_range": 0.00600,
        "ema_20_h1": 1.08600,
        "ema_slope": 0.0015,
        "ema_20_h4": 1.08550,
        "ema_20_h4_dir": "RISING",
        "adx_14": 28.5,
        "atr_14": 0.00085,
        "overextension_ratio": 0.7,
        "trigger_window_bars": 20,
        "strategy_state": "PENDING_LONG",
        "campaign": "ftmo-free-trial-march-2026",
    }
    base.update(overrides)
    return base


def _trail_payload(**overrides) -> dict:
    """Build a valid trail_alert payload."""
    base = {
        "strategy_name": "Rob Hoffman IRB",
        "strategy_version": "2.0.0",
        "action": "MODIFY_SL",
        "symbol": "EURUSD",
        "side": "BUY",
        "old_stop": 1.08234,
        "new_stop": 1.08500,
        "best_close": 1.09100,
        "atr_14": 0.00085,
        "bars_since_entry": 5,
        "campaign": "ftmo-free-trial-march-2026",
    }
    base.update(overrides)
    return base


def _cancel_payload(**overrides) -> dict:
    """Build a valid cancel_alert payload."""
    base = {
        "strategy_name": "Rob Hoffman IRB",
        "strategy_version": "2.0.0",
        "action": "CANCEL_ORDER",
        "symbol": "EURUSD",
        "side": "BUY",
        "cancel_reason": "TRIGGER_WINDOW_EXPIRED",
        "bars_elapsed": 20,
        "campaign": "ftmo-free-trial-march-2026",
    }
    base.update(overrides)
    return base


def _close_payload(**overrides) -> dict:
    """Build a valid close_alert payload."""
    base = {
        "strategy_name": "Rob Hoffman IRB",
        "strategy_version": "2.0.0",
        "action": "CLOSE_POSITION",
        "symbol": "EURUSD",
        "side": "BUY",
        "close_reason": "TIME_STOP",
        "bars_held": 40,
        "close_price": 1.09200,
        "campaign": "ftmo-free-trial-march-2026",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Alert validation tests
# ---------------------------------------------------------------------------


class TestValidateAlert:
    def test_valid_signal_alert(self):
        err, action = validate_alert(_signal_payload())
        assert err is None
        assert action == "PLACE_STOP_ORDER"

    def test_valid_replace_signal(self):
        err, action = validate_alert(_signal_payload(action="REPLACE_STOP_ORDER"))
        assert err is None
        assert action == "REPLACE_STOP_ORDER"

    def test_valid_trail_alert(self):
        err, action = validate_alert(_trail_payload())
        assert err is None
        assert action == "MODIFY_SL"

    def test_valid_cancel_alert(self):
        err, action = validate_alert(_cancel_payload())
        assert err is None
        assert action == "CANCEL_ORDER"

    def test_valid_close_alert(self):
        err, action = validate_alert(_close_payload())
        assert err is None
        assert action == "CLOSE_POSITION"

    def test_missing_action(self):
        err, _ = validate_alert({"strategy_name": "Rob Hoffman IRB"})
        assert err is not None
        assert "action" in err

    def test_invalid_action(self):
        err, _ = validate_alert(_signal_payload(action="MARKET_BUY"))
        assert err is not None
        assert "MARKET_BUY" in err

    def test_wrong_strategy_name(self):
        err, _ = validate_alert(_signal_payload(strategy_name="EMA Crossover"))
        assert err is not None
        assert "unknown strategy" in err

    def test_wrong_version(self):
        err, _ = validate_alert(_signal_payload(strategy_version="1.0.0"))
        assert err is not None
        assert "version mismatch" in err

    def test_missing_required_field(self):
        p = _signal_payload()
        del p["entry_price"]
        err, _ = validate_alert(p)
        assert err is not None
        assert "missing" in err.lower()

    def test_invalid_side(self):
        err, _ = validate_alert(_signal_payload(side="LONG"))
        assert err is not None
        assert "side" in err.lower()

    def test_negative_entry_price(self):
        err, _ = validate_alert(_signal_payload(entry_price=-1.0))
        assert err is not None
        assert "entry_price" in err

    def test_zero_volume(self):
        err, _ = validate_alert(_signal_payload(volume=0))
        assert err is not None
        assert "volume" in err

    def test_invalid_order_type(self):
        err, _ = validate_alert(_signal_payload(order_type="MARKET"))
        assert err is not None
        assert "order_type" in err

    def test_negative_new_stop(self):
        err, _ = validate_alert(_trail_payload(new_stop=-1.0))
        assert err is not None
        assert "new_stop" in err


# ---------------------------------------------------------------------------
# Idempotency key tests
# ---------------------------------------------------------------------------


class TestIdempotencyKey:
    def test_signal_key_format(self):
        key = make_idempotency_key(_signal_payload())
        # MetaApi format: IRB_{action}{time6}_{side}
        assert key.startswith("IRB_PS")
        assert key.endswith("_B")
        assert "_" in key[4:]  # has second underscore
        assert len(key) <= 15

    def test_trail_key_uses_bar_close_time(self):
        p = _trail_payload()
        p["bar_close_time"] = 1710003600000
        key = make_idempotency_key(p)
        assert "MS" in key
        assert len(key) <= 15

    def test_cancel_key_uses_bars_elapsed(self):
        key = make_idempotency_key(_cancel_payload())
        assert "CX" in key
        assert len(key) <= 15

    def test_close_key_uses_bars_held(self):
        key = make_idempotency_key(_close_payload())
        assert "CP" in key
        assert len(key) <= 15

    def test_different_sides_different_keys(self):
        k1 = make_idempotency_key(_signal_payload(side="BUY"))
        k2 = make_idempotency_key(_signal_payload(side="SELL"))
        assert k1 != k2

    def test_key_matches_metaapi_pattern(self):
        """Keys must match MetaApi clientId pattern: strategyId_positionId_orderId."""
        import re

        pattern = re.compile(r"^[a-zA-Z0-9]+_[a-zA-Z0-9]+_[a-zA-Z0-9]+$")
        payloads = [
            _signal_payload(),
            _trail_payload(),
            _cancel_payload(),
            _close_payload(),
        ]
        for p in payloads:
            key = make_idempotency_key(p)
            assert len(key) <= 15, f"key too long: {key!r} ({len(key)} chars)"
            assert pattern.match(key), f"key doesn't match MetaApi pattern: {key!r}"


# ---------------------------------------------------------------------------
# FSM state transition tests
# ---------------------------------------------------------------------------


class TestFSMTransitions:
    @pytest.mark.asyncio
    async def test_flat_to_pending_long(self):
        agent = _make_agent()
        assert agent.state == AgentState.FLAT
        result = await agent.process_alert(_signal_payload())
        assert result.success
        assert agent.state == AgentState.PENDING_LONG
        assert agent.pending_order_id == "ORD-001"

    @pytest.mark.asyncio
    async def test_flat_to_pending_short(self):
        agent = _make_agent()
        result = await agent.process_alert(
            _signal_payload(side="SELL", order_type="SELL_STOP", signal_type="SHORT_IRB")
        )
        assert result.success
        assert agent.state == AgentState.PENDING_SHORT

    @pytest.mark.asyncio
    async def test_pending_long_replace(self):
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        assert agent.state == AgentState.PENDING_LONG

        result = await agent.process_alert(
            _signal_payload(
                action="REPLACE_STOP_ORDER",
                entry_price=1.08900,
                bar_close_time=1710003600000,
            )
        )
        assert result.success
        assert agent.state == AgentState.PENDING_LONG

    @pytest.mark.asyncio
    async def test_pending_to_flat_on_cancel(self):
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        assert agent.state == AgentState.PENDING_LONG

        result = await agent.process_alert(_cancel_payload())
        assert result.success
        assert agent.state == AgentState.FLAT
        assert agent.pending_order_id is None

    @pytest.mark.asyncio
    async def test_fill_transitions_to_long(self):
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        assert agent.state == AgentState.PENDING_LONG

        agent.notify_fill("POS-001", fill_price=1.08765, volume=0.19)
        assert agent.state == AgentState.LONG
        assert agent.position_id == "POS-001"
        assert agent.pending_order_id is None

    @pytest.mark.asyncio
    async def test_modify_sl_in_long(self):
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        agent.notify_fill("POS-001", fill_price=1.08765, volume=0.19)
        assert agent.state == AgentState.LONG

        result = await agent.process_alert(_trail_payload())
        assert result.success
        assert agent.state == AgentState.LONG  # no state change

    @pytest.mark.asyncio
    async def test_close_position_time_stop(self):
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        agent.notify_fill("POS-001", fill_price=1.08765, volume=0.19)
        assert agent.state == AgentState.LONG

        result = await agent.process_alert(_close_payload())
        assert result.success
        assert agent.state == AgentState.FLAT
        assert agent.position_id is None

    @pytest.mark.asyncio
    async def test_broker_close_transitions_to_flat(self):
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        agent.notify_fill("POS-001", fill_price=1.08765, volume=0.19)
        assert agent.state == AgentState.LONG

        agent.notify_broker_close("POS-001", exit_reason="TRAILING_STOP")
        assert agent.state == AgentState.FLAT


# ---------------------------------------------------------------------------
# Invalid transition tests
# ---------------------------------------------------------------------------


class TestInvalidTransitions:
    @pytest.mark.asyncio
    async def test_place_from_pending(self):
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        result = await agent.process_alert(_signal_payload(bar_close_time=1710003600000))
        assert not result.success
        assert "invalid" in result.rejected_reason.lower()

    @pytest.mark.asyncio
    async def test_place_from_long(self):
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        agent.notify_fill("POS-001", fill_price=1.08765, volume=0.19)
        result = await agent.process_alert(_signal_payload(bar_close_time=1710003600000))
        assert not result.success
        assert "PLACE_STOP_ORDER invalid" in result.rejected_reason

    @pytest.mark.asyncio
    async def test_modify_sl_from_flat(self):
        agent = _make_agent()
        result = await agent.process_alert(_trail_payload())
        assert not result.success
        assert "MODIFY_SL invalid" in result.rejected_reason

    @pytest.mark.asyncio
    async def test_modify_sl_from_pending(self):
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        result = await agent.process_alert(_trail_payload())
        assert not result.success
        assert "MODIFY_SL invalid" in result.rejected_reason

    @pytest.mark.asyncio
    async def test_cancel_from_flat(self):
        agent = _make_agent()
        result = await agent.process_alert(_cancel_payload())
        assert not result.success
        assert "CANCEL_ORDER invalid" in result.rejected_reason

    @pytest.mark.asyncio
    async def test_cancel_from_long(self):
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        agent.notify_fill("POS-001", fill_price=1.08765, volume=0.19)
        result = await agent.process_alert(_cancel_payload())
        assert not result.success
        assert "CANCEL_ORDER invalid" in result.rejected_reason

    @pytest.mark.asyncio
    async def test_close_from_flat(self):
        agent = _make_agent()
        result = await agent.process_alert(_close_payload())
        assert not result.success
        assert "CLOSE_POSITION invalid" in result.rejected_reason

    @pytest.mark.asyncio
    async def test_close_from_pending(self):
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        result = await agent.process_alert(_close_payload())
        assert not result.success
        assert "CLOSE_POSITION invalid" in result.rejected_reason


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_duplicate_suppressed(self):
        agent = _make_agent()
        r1 = await agent.process_alert(_signal_payload())
        assert r1.success

        # Reset state to FLAT so the duplicate isn't rejected by FSM
        agent._state = AgentState.FLAT
        agent._pending_order_id = None

        r2 = await agent.process_alert(_signal_payload())
        assert not r2.success
        assert "duplicate" in r2.rejected_reason

    @pytest.mark.asyncio
    async def test_different_bar_not_duplicate(self):
        agent = _make_agent()
        r1 = await agent.process_alert(_signal_payload())
        assert r1.success

        # Cancel first, then new signal with different bar time
        await agent.process_alert(_cancel_payload())
        assert agent.state == AgentState.FLAT

        r2 = await agent.process_alert(_signal_payload(bar_close_time=1710003600000))
        assert r2.success

    @pytest.mark.asyncio
    async def test_seen_keys_pruned(self):
        agent = _make_agent()
        agent._max_seen_keys = 5
        # Add many keys
        for i in range(10):
            agent._seen_keys.add(f"key_{i}")
        agent._prune_seen_keys()
        assert len(agent._seen_keys) <= 5


# ---------------------------------------------------------------------------
# Risk gate integration tests
# ---------------------------------------------------------------------------


class TestRiskGateIntegration:
    @pytest.mark.asyncio
    async def test_risk_deny_rejects_signal(self):
        cfg = _make_cfg()
        adapter = _make_adapter()
        engine = _make_risk_engine(cfg)
        engine._halted = True
        engine._halt_reason = "test halt"

        agent = _make_agent(cfg=cfg, adapter=adapter, risk_engine=engine)
        result = await agent.process_alert(_signal_payload())
        assert not result.success
        assert "risk_halt" in result.rejected_reason

        adapter.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_risk_allow_proceeds_to_execution(self):
        agent = _make_agent()
        result = await agent.process_alert(_signal_payload())
        assert result.success
        assert result.order_result is not None
        assert result.order_result.ok


# ---------------------------------------------------------------------------
# Symbol resolution tests
# ---------------------------------------------------------------------------


class TestSymbolResolution:
    @pytest.mark.asyncio
    async def test_resolves_via_ftmo_profile(self):
        agent = _make_agent()
        result = await agent.process_alert(_signal_payload())
        assert result.success
        assert result.intent is not None
        assert result.intent.broker_symbol == "EURUSD.sim"

    @pytest.mark.asyncio
    async def test_passthrough_when_no_mapping(self):
        cfg = _make_cfg()
        cfg.ftmo = FtmoProfile(enabled=True, symbol_map={})
        agent = _make_agent(cfg=cfg)
        result = await agent.process_alert(_signal_payload(broker_symbol="EURUSD"))
        assert result.success
        assert result.intent.broker_symbol == "EURUSD"


# ---------------------------------------------------------------------------
# Adapter error tests
# ---------------------------------------------------------------------------


class TestAdapterErrors:
    @pytest.mark.asyncio
    async def test_place_order_failure(self):
        adapter = _make_adapter()
        adapter.place_order = AsyncMock(return_value=OrderResult(ok=False, error="connection timeout"))
        agent = _make_agent(adapter=adapter)
        result = await agent.process_alert(_signal_payload())
        assert not result.success
        assert "connection timeout" in result.error
        assert agent.state == AgentState.FLAT  # no state change

    @pytest.mark.asyncio
    async def test_modify_sl_failure(self):
        adapter = _make_adapter()
        agent = _make_agent(adapter=adapter)
        await agent.process_alert(_signal_payload())
        agent.notify_fill("POS-001", fill_price=1.08765, volume=0.19)

        adapter.modify_order = AsyncMock(return_value=OrderResult(ok=False, error="invalid ticket"))
        result = await agent.process_alert(_trail_payload())
        assert not result.success
        assert "invalid ticket" in result.error
        assert agent.state == AgentState.LONG  # no state change

    @pytest.mark.asyncio
    async def test_cancel_failure_still_transitions_to_flat(self):
        adapter = _make_adapter()
        agent = _make_agent(adapter=adapter)
        await agent.process_alert(_signal_payload())
        assert agent.state == AgentState.PENDING_LONG

        adapter.cancel_order = AsyncMock(return_value=OrderResult(ok=False, error="order already expired"))
        result = await agent.process_alert(_cancel_payload())
        assert result.success  # cancel is still "successful" (fail-safe)
        assert agent.state == AgentState.FLAT

    @pytest.mark.asyncio
    async def test_close_failure_no_state_change(self):
        adapter = _make_adapter()
        agent = _make_agent(adapter=adapter)
        await agent.process_alert(_signal_payload())
        agent.notify_fill("POS-001", fill_price=1.08765, volume=0.19)

        adapter.close_position = AsyncMock(return_value=OrderResult(ok=False, error="position not found"))
        result = await agent.process_alert(_close_payload())
        assert not result.success
        assert agent.state == AgentState.LONG  # stays in position


# ---------------------------------------------------------------------------
# Evidence recording tests
# ---------------------------------------------------------------------------


class TestEvidenceRecording:
    @pytest.mark.asyncio
    async def test_records_on_success(self, tmp_path):
        evidence_path = tmp_path / "evidence.jsonl"
        recorder = EvidenceRecorder(path=evidence_path, campaign="test")
        agent = _make_agent(recorder=recorder)

        await agent.process_alert(_signal_payload())
        records = recorder.load()
        assert len(records) > 0
        assert any("ORDER_PLACED" in str(r.data) for r in records)

    @pytest.mark.asyncio
    async def test_records_on_validation_failure(self, tmp_path):
        evidence_path = tmp_path / "evidence.jsonl"
        recorder = EvidenceRecorder(path=evidence_path, campaign="test")
        agent = _make_agent(recorder=recorder)

        await agent.process_alert({"action": "INVALID"})
        records = recorder.load()
        assert len(records) > 0
        assert any(r.error for r in records)


# ---------------------------------------------------------------------------
# External notification tests
# ---------------------------------------------------------------------------


class TestExternalNotifications:
    def test_notify_fill_from_wrong_state(self):
        agent = _make_agent()
        agent.notify_fill("POS-001", 1.08765, 0.19)
        assert agent.state == AgentState.FLAT  # ignored

    def test_notify_broker_close_from_wrong_state(self):
        agent = _make_agent()
        agent.notify_broker_close("POS-001")
        assert agent.state == AgentState.FLAT  # ignored

    @pytest.mark.asyncio
    async def test_full_lifecycle(self):
        """Test complete lifecycle: FLAT -> PENDING -> LONG -> modify SL -> close."""
        agent = _make_agent()

        # 1. Place order
        r = await agent.process_alert(_signal_payload())
        assert r.success
        assert agent.state == AgentState.PENDING_LONG

        # 2. Fill
        agent.notify_fill("POS-001", 1.08765, 0.19, stop_loss=1.08234)
        assert agent.state == AgentState.LONG

        # 3. Modify SL (trailing stop)
        r = await agent.process_alert(_trail_payload())
        assert r.success
        assert agent.state == AgentState.LONG

        # 4. Close (time stop)
        r = await agent.process_alert(_close_payload())
        assert r.success
        assert agent.state == AgentState.FLAT

    @pytest.mark.asyncio
    async def test_pending_cancel_lifecycle(self):
        """FLAT -> PENDING -> cancel -> FLAT."""
        agent = _make_agent()

        r = await agent.process_alert(_signal_payload())
        assert agent.state == AgentState.PENDING_LONG

        r = await agent.process_alert(_cancel_payload())
        assert r.success
        assert agent.state == AgentState.FLAT

    @pytest.mark.asyncio
    async def test_pending_fill_broker_close_lifecycle(self):
        """FLAT -> PENDING -> fill -> broker SL close -> FLAT."""
        agent = _make_agent()

        await agent.process_alert(_signal_payload())
        agent.notify_fill("POS-001", 1.08765, 0.19)
        assert agent.state == AgentState.LONG

        agent.notify_broker_close("POS-001", "TRAILING_STOP")
        assert agent.state == AgentState.FLAT


# ---------------------------------------------------------------------------
# OrderIntent tests
# ---------------------------------------------------------------------------


class TestOrderIntent:
    def test_to_dict_place(self):
        intent = OrderIntent(
            intent_type=IntentType.PLACE_ORDER,
            idempotency_key="irb_PLACE_1710000000000_BUY",
            broker_symbol="EURUSD.sim",
            side=OrderSide.BUY,
            order_type=OrderType.STOP,
            entry_price=1.08765,
            stop_loss=1.08234,
            volume=0.19,
        )
        d = intent.to_dict()
        assert d["intent_type"] == "PLACE_ORDER"
        assert d["entry_price"] == 1.08765
        assert d["order_type"] == "STOP"

    def test_to_dict_modify(self):
        intent = OrderIntent(
            intent_type=IntentType.MODIFY_SL,
            idempotency_key="irb_MS_1710003600000_B",
            broker_symbol="EURUSD.sim",
            side=OrderSide.BUY,
            new_stop_loss=1.08500,
            old_stop_loss=1.08234,
        )
        d = intent.to_dict()
        assert d["intent_type"] == "MODIFY_SL"
        assert d["new_stop_loss"] == 1.08500

    def test_to_dict_cancel(self):
        intent = OrderIntent(
            intent_type=IntentType.CANCEL_ORDER,
            idempotency_key="irb_CX_20_B",
            broker_symbol="EURUSD.sim",
            side=OrderSide.BUY,
            cancel_reason="TRIGGER_WINDOW_EXPIRED",
        )
        d = intent.to_dict()
        assert d["cancel_reason"] == "TRIGGER_WINDOW_EXPIRED"

    def test_to_dict_close(self):
        intent = OrderIntent(
            intent_type=IntentType.CLOSE_POSITION,
            idempotency_key="irb_CP_40_B",
            broker_symbol="EURUSD.sim",
            side=OrderSide.BUY,
            close_reason="TIME_STOP",
        )
        d = intent.to_dict()
        assert d["close_reason"] == "TIME_STOP"


# ---------------------------------------------------------------------------
# AgentResult tests
# ---------------------------------------------------------------------------


class TestAgentResult:
    def test_rejected_property(self):
        r = AgentResult(success=False, rejected_reason="risk_denied: halt")
        assert r.rejected

    def test_not_rejected(self):
        r = AgentResult(success=True)
        assert not r.rejected

    def test_state_tracking(self):
        r = AgentResult(
            success=True,
            state_before=AgentState.FLAT,
            state_after=AgentState.PENDING_LONG,
        )
        assert r.state_before == AgentState.FLAT
        assert r.state_after == AgentState.PENDING_LONG


# ---------------------------------------------------------------------------
# Position tracking tests (symbol/volume carried through FSM)
# ---------------------------------------------------------------------------


class TestPositionTracking:
    """Verify symbol and volume are tracked across FSM states."""

    @pytest.mark.asyncio
    async def test_notify_fill_uses_tracked_symbol(self):
        """notify_fill passes the symbol from the pending order, not hardcoded EURUSD."""
        agent = _make_agent()
        # Place order — this sets _pending_symbol to the resolved broker symbol
        result = await agent.process_alert(_signal_payload())
        assert result.success
        assert agent.state == AgentState.PENDING_LONG

        # Fill — should track the correct symbol
        agent.notify_fill("POS-001", fill_price=1.08765, volume=0.19)
        assert agent.state == AgentState.LONG
        assert agent.position_symbol == "EURUSD.sim"
        assert agent.position_volume == 0.19

    @pytest.mark.asyncio
    async def test_notify_fill_short_tracks_symbol(self):
        """Short-side fill also tracks the correct symbol."""
        agent = _make_agent()
        payload = _signal_payload(
            side="SELL",
            order_type="SELL_STOP",
            action="PLACE_STOP_ORDER",
        )
        result = await agent.process_alert(payload)
        assert result.success

        agent.notify_fill("POS-002", fill_price=1.08765, volume=0.25)
        assert agent.state == AgentState.SHORT
        assert agent.position_symbol == "EURUSD.sim"
        assert agent.position_volume == 0.25

    @pytest.mark.asyncio
    async def test_close_passes_tracked_volume(self):
        """_handle_close passes the tracked volume to risk engine on_trade_close."""
        risk = _make_risk_engine()
        agent = _make_agent(risk_engine=risk)

        # Place + fill
        await agent.process_alert(_signal_payload())
        agent.notify_fill("POS-001", fill_price=1.08765, volume=0.19)
        assert agent.position_volume == 0.19

        # Close — check that the risk engine gets the real volume
        original_on_close = risk.on_trade_close
        close_calls = []

        def capture_close(**kwargs):
            close_calls.append(kwargs)
            return original_on_close(**kwargs)

        risk.on_trade_close = capture_close

        result = await agent.process_alert(_close_payload())
        assert result.success
        assert agent.state == AgentState.FLAT
        assert len(close_calls) == 1
        assert close_calls[0]["volume"] == 0.19  # not 0.0

    @pytest.mark.asyncio
    async def test_close_resets_tracking_fields(self):
        """After close, position_symbol and position_volume are cleared."""
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        agent.notify_fill("POS-001", fill_price=1.08765, volume=0.19)
        await agent.process_alert(_close_payload())

        assert agent.position_symbol is None
        assert agent.position_volume == 0.0

    @pytest.mark.asyncio
    async def test_cancel_resets_pending_symbol(self):
        """After cancel, pending_symbol is cleared."""
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        assert agent.state == AgentState.PENDING_LONG

        result = await agent.process_alert(_cancel_payload())
        assert result.success
        assert agent.state == AgentState.FLAT
        assert agent._pending_symbol is None

    @pytest.mark.asyncio
    async def test_force_flat_resets_all_tracking(self):
        """force_flat clears all symbol and volume tracking."""
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        agent.notify_fill("POS-001", fill_price=1.08765, volume=0.19)

        agent.force_flat("test reconciliation")
        assert agent.state == AgentState.FLAT
        assert agent.position_symbol is None
        assert agent.position_volume == 0.0
        assert agent._pending_symbol is None

    @pytest.mark.asyncio
    async def test_broker_close_resets_tracking(self):
        """notify_broker_close clears position tracking."""
        agent = _make_agent()
        await agent.process_alert(_signal_payload())
        agent.notify_fill("POS-001", fill_price=1.08765, volume=0.19)
        assert agent.position_symbol == "EURUSD.sim"

        agent.notify_broker_close("POS-001", exit_reason="SL_HIT")
        assert agent.state == AgentState.FLAT
        assert agent.position_symbol is None
        assert agent.position_volume == 0.0
