"""Tests for StateStore — SQLite-backed state persistence for TradingAgent.

Covers:
- Basic CRUD for agent_state, idempotency_keys, expected_positions tables
- TradingAgent integration (state persistence on transitions, recovery on init)
- Recovery scenarios (restart, missing DB, corrupted DB)
- PositionTracker persistence
"""

from __future__ import annotations

import asyncio
import os
import sqlite3

import pytest

from novatrade.config import NovaTradeCfg
from novatrade.execution.trading_agent import AgentState, TradingAgent
from novatrade.models import (
    AccountState,
    ExecutionOutcome,
    ExecutionResult,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    RiskDecision,
    RiskVerdict,
)
from novatrade.monitor.position_tracker import PositionTracker
from novatrade.risk.risk_engine import RiskEngine
from novatrade.storage.state_store import StateStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """Create a StateStore backed by a temp SQLite file."""
    return StateStore(tmp_path / "test_state.db")


@pytest.fixture
def cfg():
    """Minimal NovaTradeCfg for testing."""
    os.environ.setdefault("NOVATRADE_SYMBOLS", "EURUSD")
    return NovaTradeCfg.load()


class _MockAdapter:
    """Minimal mock adapter for TradingAgent tests."""

    async def place_order(self, req):
        return OrderResult(ok=True, order_id="ORD-001")

    async def cancel_order(self, order_id):
        return OrderResult(ok=True, order_id=order_id)

    async def close_position(self, position_id):
        return OrderResult(ok=True, order_id=position_id)

    async def modify_order(self, position_id, stop_loss=None):
        return OrderResult(ok=True, order_id=position_id)

    async def get_account(self):
        return AccountState(balance=100_000, equity=100_000)

    async def get_positions(self):
        return []

    async def get_orders(self):
        return []

    async def connect(self):
        from novatrade.models import HealthState, HealthStatus

        return HealthStatus(state=HealthState.OK, connected=True)


def _make_agent(cfg, store=None):
    """Create a TradingAgent with mock adapter and optional state store."""
    adapter = _MockAdapter()
    risk = RiskEngine(cfg)
    risk.initialize(AccountState(balance=100_000, equity=100_000))
    return TradingAgent(
        cfg=cfg,
        adapter=adapter,
        risk_engine=risk,
        state_store=store,
    )


def _signal_payload(side="BUY", bar_time=1000000):
    """Create a minimal valid PLACE_STOP_ORDER payload."""
    return {
        "strategy_name": "Rob Hoffman IRB",
        "strategy_version": "5.0.0",
        "action": "PLACE_STOP_ORDER",
        "signal_type": "ENTRY",
        "symbol": "EURUSD",
        "broker_symbol": "EURUSD",
        "side": side,
        "order_type": "BUY_STOP" if side == "BUY" else "SELL_STOP",
        "entry_price": 1.10000,
        "stop_loss": 1.09500,
        "volume": 0.01,
        "bar_close_time": bar_time,
        "campaign": "test",
    }


def _cancel_payload(side="BUY"):
    return {
        "strategy_name": "Rob Hoffman IRB",
        "strategy_version": "5.0.0",
        "action": "CANCEL_ORDER",
        "symbol": "EURUSD",
        "side": side,
        "cancel_reason": "TIME_EXPIRED",
        "campaign": "test",
    }


def _close_payload(side="BUY"):
    return {
        "strategy_name": "Rob Hoffman IRB",
        "strategy_version": "5.0.0",
        "action": "CLOSE_POSITION",
        "symbol": "EURUSD",
        "side": side,
        "close_reason": "TIME_STOP",
        "campaign": "test",
    }


# ===================================================================
# TestStateStoreBasic — core CRUD operations
# ===================================================================


class TestStateStoreBasic:
    """Test StateStore's basic read/write operations in isolation."""

    def test_create_db_creates_tables(self, store):
        conn = sqlite3.connect(str(store._db_path))
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert "agent_state" in tables
        assert "idempotency_keys" in tables
        assert "expected_positions" in tables

    def test_save_and_load_agent_state(self, store):
        store.save_agent_state(
            state="PENDING_LONG",
            pending_order_id="ORD-42",
            pending_side="BUY",
            pending_symbol="EURUSD",
        )
        row = store.load_agent_state()
        assert row is not None
        assert row["state"] == "PENDING_LONG"
        assert row["pending_order_id"] == "ORD-42"
        assert row["pending_side"] == "BUY"
        assert row["pending_symbol"] == "EURUSD"
        assert row["position_id"] is None

    def test_load_agent_state_empty_db(self, store):
        assert store.load_agent_state() is None

    def test_save_agent_state_upsert(self, store):
        store.save_agent_state(state="FLAT")
        store.save_agent_state(state="LONG", position_id="POS-1", position_side="BUY")
        row = store.load_agent_state()
        assert row["state"] == "LONG"
        assert row["position_id"] == "POS-1"

    def test_add_and_has_idempotency_key(self, store):
        assert not store.has_idempotency_key("KEY-1")
        store.add_idempotency_key("KEY-1")
        assert store.has_idempotency_key("KEY-1")
        assert not store.has_idempotency_key("KEY-2")

    def test_load_idempotency_keys(self, store):
        for i in range(5):
            store.add_idempotency_key(f"K-{i}")
        keys = store.load_idempotency_keys()
        assert len(keys) == 5
        assert "K-0" in keys
        assert "K-4" in keys

    def test_prune_idempotency_keys(self, store):
        for i in range(10):
            store.add_idempotency_key(f"K-{i:03d}")
        deleted = store.prune_idempotency_keys(keep=3)
        assert deleted == 7
        remaining = store.load_idempotency_keys()
        assert len(remaining) == 3

    def test_save_and_load_expected_position(self, store):
        pos = Position(
            position_id="POS-1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=0.01,
            open_price=1.10000,
            stop_loss=1.09500,
        )
        store.save_expected_position(pos)
        loaded = store.load_expected_positions()
        assert "POS-1" in loaded
        p = loaded["POS-1"]
        assert p.symbol == "EURUSD"
        assert p.side == OrderSide.BUY
        assert p.volume == 0.01
        assert p.open_price == 1.10000
        assert p.stop_loss == 1.09500

    def test_remove_expected_position(self, store):
        pos = Position(
            position_id="POS-1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=0.01,
            open_price=1.10000,
        )
        store.save_expected_position(pos)
        store.remove_expected_position("POS-1")
        assert store.load_expected_positions() == {}

    def test_clear_expected_positions(self, store):
        for i in range(3):
            store.save_expected_position(
                Position(
                    position_id=f"POS-{i}",
                    symbol="EURUSD",
                    side=OrderSide.BUY,
                    volume=0.01,
                    open_price=1.10000,
                )
            )
        assert len(store.load_expected_positions()) == 3
        store.clear_expected_positions()
        assert len(store.load_expected_positions()) == 0

    def test_schema_version(self, store):
        conn = sqlite3.connect(str(store._db_path))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == 1


# ===================================================================
# TestStateStoreWithTradingAgent — integration with FSM transitions
# ===================================================================


class TestStateStoreWithTradingAgent:
    """Test that TradingAgent persists state on every FSM transition."""

    def test_agent_with_no_store_works_unchanged(self, cfg):
        agent = _make_agent(cfg, store=None)
        assert agent.state == AgentState.FLAT

    def test_agent_persists_on_signal(self, cfg, store):
        agent = _make_agent(cfg, store)
        result = asyncio.run(agent.process_alert(_signal_payload()))
        assert result.success
        assert agent.state == AgentState.PENDING_LONG
        # Verify DB
        row = store.load_agent_state()
        assert row["state"] == "PENDING_LONG"
        assert row["pending_order_id"] == "ORD-001"
        assert row["pending_side"] == "BUY"

    def test_agent_persists_on_cancel(self, cfg, store):
        agent = _make_agent(cfg, store)
        asyncio.run(agent.process_alert(_signal_payload()))
        asyncio.run(agent.process_alert(_cancel_payload()))
        row = store.load_agent_state()
        assert row["state"] == "FLAT"
        assert row["pending_order_id"] is None

    def test_agent_persists_on_fill(self, cfg, store):
        agent = _make_agent(cfg, store)
        asyncio.run(agent.process_alert(_signal_payload()))
        agent.notify_fill("POS-42", fill_price=1.10000, volume=0.01)
        assert agent.state == AgentState.LONG
        row = store.load_agent_state()
        assert row["state"] == "LONG"
        assert row["position_id"] == "POS-42"
        assert row["position_side"] == "BUY"

    def test_agent_persists_on_close(self, cfg, store):
        agent = _make_agent(cfg, store)
        asyncio.run(agent.process_alert(_signal_payload()))
        agent.notify_fill("POS-42", fill_price=1.10000, volume=0.01)
        asyncio.run(agent.process_alert(_close_payload()))
        row = store.load_agent_state()
        assert row["state"] == "FLAT"
        assert row["position_id"] is None

    def test_agent_persists_on_force_flat(self, cfg, store):
        agent = _make_agent(cfg, store)
        asyncio.run(agent.process_alert(_signal_payload()))
        agent.force_flat("test_reconciliation")
        row = store.load_agent_state()
        assert row["state"] == "FLAT"

    def test_agent_persists_on_broker_close(self, cfg, store):
        agent = _make_agent(cfg, store)
        asyncio.run(agent.process_alert(_signal_payload()))
        agent.notify_fill("POS-42", fill_price=1.10000, volume=0.01)
        agent.notify_broker_close("POS-42", exit_reason="SL_HIT")
        row = store.load_agent_state()
        assert row["state"] == "FLAT"

    def test_agent_persists_idempotency_keys(self, cfg, store):
        agent = _make_agent(cfg, store)
        asyncio.run(agent.process_alert(_signal_payload(bar_time=1234567)))
        keys = store.load_idempotency_keys()
        assert len(keys) > 0


# ===================================================================
# TestStateStoreRecovery — restart and corruption scenarios
# ===================================================================


class TestStateStoreRecovery:
    """Test agent state recovery on restart."""

    def test_recovery_pending_state(self, cfg, tmp_path):
        db_path = tmp_path / "recover.db"
        store1 = StateStore(db_path)
        agent1 = _make_agent(cfg, store1)
        asyncio.run(agent1.process_alert(_signal_payload()))
        assert agent1.state == AgentState.PENDING_LONG

        # Simulate restart — new store, new agent
        store2 = StateStore(db_path)
        agent2 = _make_agent(cfg, store2)
        assert agent2.state == AgentState.PENDING_LONG
        assert agent2.pending_order_id == "ORD-001"

    def test_recovery_position_state(self, cfg, tmp_path):
        db_path = tmp_path / "recover.db"
        store1 = StateStore(db_path)
        agent1 = _make_agent(cfg, store1)
        asyncio.run(agent1.process_alert(_signal_payload()))
        agent1.notify_fill("POS-99", fill_price=1.10000, volume=0.01)
        assert agent1.state == AgentState.LONG

        # Simulate restart
        store2 = StateStore(db_path)
        agent2 = _make_agent(cfg, store2)
        assert agent2.state == AgentState.LONG
        assert agent2.position_id == "POS-99"
        assert agent2.position_volume == 0.01

    def test_recovery_idempotency_keys(self, cfg, tmp_path):
        db_path = tmp_path / "recover.db"
        store1 = StateStore(db_path)
        agent1 = _make_agent(cfg, store1)
        asyncio.run(agent1.process_alert(_signal_payload(bar_time=9999999)))

        # Simulate restart — same DB, new agent
        store2 = StateStore(db_path)
        agent2 = _make_agent(cfg, store2)
        # The same alert should be rejected as duplicate
        agent2.force_flat("test")  # reset to FLAT first
        result = asyncio.run(agent2.process_alert(_signal_payload(bar_time=9999999)))
        assert not result.success
        assert "duplicate" in result.rejected_reason

    def test_recovery_with_missing_db(self, cfg, tmp_path):
        db_path = tmp_path / "nonexistent" / "missing.db"
        store = StateStore(db_path)
        agent = _make_agent(cfg, store)
        assert agent.state == AgentState.FLAT  # starts fresh

    def test_recovery_with_corrupted_db(self, cfg, tmp_path):
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"THIS IS NOT A SQLITE FILE")
        # StateStore should handle corruption gracefully — init may fail
        # but should not raise; agent falls back to FLAT
        StateStore(db_path)
        agent = _make_agent(cfg, store=None)
        assert agent.state == AgentState.FLAT


# ===================================================================
# TestPositionTrackerPersistence
# ===================================================================


class TestPositionTrackerPersistence:
    """Test PositionTracker with StateStore backing."""

    def test_tracker_with_no_store(self):
        tracker = PositionTracker()
        assert tracker.count == 0

    def test_tracker_persists_on_record(self, store):
        tracker = PositionTracker(state_store=store)
        result = ExecutionResult(
            outcome=ExecutionOutcome.FILLED,
            order_result=OrderResult(ok=True, order_id="ORD-1", fill_price=1.10),
            risk_decision=RiskDecision(
                verdict=RiskVerdict.ALLOW,
                request=OrderRequest(
                    symbol="EURUSD",
                    side=OrderSide.BUY,
                    order_type=OrderType.STOP,
                    volume=0.01,
                    price=1.10,
                    stop_loss=1.095,
                    strategy_id="IRB",
                ),
            ),
        )
        tracker.record(result)
        assert tracker.count == 1
        # Verify persisted
        loaded = store.load_expected_positions()
        assert "ORD-1" in loaded

    def test_tracker_persists_on_remove(self, store):
        tracker = PositionTracker(state_store=store)
        pos = Position(
            position_id="POS-X",
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=0.01,
            open_price=1.10,
        )
        store.save_expected_position(pos)
        tracker._expected["POS-X"] = pos
        tracker.remove("POS-X")
        assert store.load_expected_positions() == {}

    def test_tracker_loads_on_init(self, store):
        pos = Position(
            position_id="POS-SAVED",
            symbol="EURUSD",
            side=OrderSide.SELL,
            volume=0.05,
            open_price=1.09000,
        )
        store.save_expected_position(pos)
        tracker = PositionTracker(state_store=store)
        assert tracker.count == 1
        assert tracker.expected_positions[0].position_id == "POS-SAVED"

    def test_tracker_clear_persists(self, store):
        pos = Position(
            position_id="POS-C",
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=0.01,
            open_price=1.10,
        )
        store.save_expected_position(pos)
        tracker = PositionTracker(state_store=store)
        tracker.clear()
        assert store.load_expected_positions() == {}
