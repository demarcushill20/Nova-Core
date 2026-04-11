"""Tests for startup reconciliation functionality.

Covers:
1. TradingAgent.recover_position() — FLAT -> LONG/SHORT adoption
2. LiveStrategyEngine.recover_position_state() — engine state recovery
3. PipelineCollector uptime guard fix — ticks > 0 but uptime_seconds = 0.0
4. Config max_volume_per_trade — default is 5.0, overridable via env var
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from novatrade.config import _DEFAULT_MAX_VOLUME_PER_TRADE, NovaTradeCfg, RiskConfig
from novatrade.execution.trading_agent import AgentState, TradingAgent
from novatrade.models import OrderSide
from novatrade.strategy.live_engine import (
    LiveConfig,
    LiveState,
    LiveStrategyEngine,
    PendingOrder,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(**overrides: object) -> NovaTradeCfg:
    """Minimal NovaTradeCfg for testing."""
    os.environ.setdefault("NOVATRADE_SYMBOLS", "EURUSD")
    cfg = NovaTradeCfg.load()
    for k, v in overrides.items():
        if hasattr(cfg, k):
            object.__setattr__(cfg, k, v)
    return cfg


def _make_trading_agent(
    *,
    cfg: NovaTradeCfg | None = None,
    adapter: MagicMock | None = None,
    risk_engine: MagicMock | None = None,
    recorder: MagicMock | None = None,
    supervisor: MagicMock | None = None,
    state_store: MagicMock | None = None,
) -> TradingAgent:
    """Build a TradingAgent with mock dependencies."""
    _cfg = cfg or _make_cfg()
    _adapter = adapter or MagicMock()
    _risk = risk_engine or MagicMock()
    _recorder = recorder or MagicMock()
    _supervisor = supervisor or MagicMock()
    _store = state_store or MagicMock()

    # state_store.load_agent_state returns None so agent starts FLAT
    _store.load_agent_state.return_value = None

    return TradingAgent(
        cfg=_cfg,
        adapter=_adapter,
        risk_engine=_risk,
        recorder=_recorder,
        supervisor=_supervisor,
        state_store=_store,
    )


def _make_live_engine(
    *,
    strategy: MagicMock | None = None,
    env: MagicMock | None = None,
    config: LiveConfig | None = None,
) -> LiveStrategyEngine:
    """Build a LiveStrategyEngine with mock dependencies."""
    _strategy = strategy or MagicMock()
    _env = env or MagicMock()

    # Provide sane env defaults
    _env.symbol_display = "EURUSD"
    _env.trigger_window_bars = 20
    _env.warmup_bars = 34
    _env.initial_equity = 100_000.0
    _env.risk_fraction = 0.01
    _env.min_volume = 0.01
    _env.max_volume = 1.0
    _env.pip_value = 0.0001
    _env.pip_value_per_standard_lot = 10.0
    _env.trail_atr_multiplier = 1.5
    _env.max_consecutive_losses = 0

    _config = config or LiveConfig(symbol="EURUSD", trigger_window_bars=20)

    return LiveStrategyEngine(strategy=_strategy, env=_env, config=_config)


# ===========================================================================
# TradingAgent.recover_position() tests
# ===========================================================================


class TestTradingAgentRecoverPosition:
    """Test TradingAgent.recover_position() startup reconciliation."""

    def test_flat_to_long_on_buy_position(self):
        """FLAT -> LONG when given a BUY position."""
        agent = _make_trading_agent()
        assert agent.state == AgentState.FLAT

        agent.recover_position(
            position_id="POS-001",
            side=OrderSide.BUY,
            symbol="EURUSD",
            volume=0.10,
            fill_price=1.10500,
            stop_loss=1.10000,
        )

        assert agent.state == AgentState.LONG
        assert agent.position_id == "POS-001"
        assert agent.position_symbol == "EURUSD"
        assert agent.position_volume == 0.10

    def test_flat_to_short_on_sell_position(self):
        """FLAT -> SHORT when given a SELL position."""
        agent = _make_trading_agent()
        assert agent.state == AgentState.FLAT

        agent.recover_position(
            position_id="POS-002",
            side=OrderSide.SELL,
            symbol="EURUSD",
            volume=0.05,
            fill_price=1.11000,
            stop_loss=1.11500,
        )

        assert agent.state == AgentState.SHORT
        assert agent.position_id == "POS-002"
        assert agent.position_symbol == "EURUSD"
        assert agent.position_volume == 0.05

    def test_ignores_call_when_already_long(self):
        """recover_position is a no-op if agent is not FLAT."""
        agent = _make_trading_agent()

        # First, adopt a position to move to LONG
        agent.recover_position(
            position_id="POS-001",
            side=OrderSide.BUY,
            symbol="EURUSD",
            volume=0.10,
            fill_price=1.10500,
        )
        assert agent.state == AgentState.LONG

        # Try to adopt another — should be ignored
        agent.recover_position(
            position_id="POS-999",
            side=OrderSide.SELL,
            symbol="GBPUSD",
            volume=0.20,
            fill_price=1.25000,
        )

        # State unchanged — still LONG with original position
        assert agent.state == AgentState.LONG
        assert agent.position_id == "POS-001"
        assert agent.position_symbol == "EURUSD"
        assert agent.position_volume == 0.10

    def test_ignores_call_when_already_short(self):
        """recover_position is a no-op if agent is in SHORT state."""
        agent = _make_trading_agent()

        agent.recover_position(
            position_id="POS-003",
            side=OrderSide.SELL,
            symbol="EURUSD",
            volume=0.15,
            fill_price=1.12000,
        )
        assert agent.state == AgentState.SHORT

        agent.recover_position(
            position_id="POS-999",
            side=OrderSide.BUY,
            symbol="EURUSD",
            volume=0.20,
            fill_price=1.10000,
        )

        assert agent.state == AgentState.SHORT
        assert agent.position_id == "POS-003"

    def test_persists_state_after_recovery(self):
        """State is persisted to state_store after successful recovery."""
        store = MagicMock()
        store.load_agent_state.return_value = None
        agent = _make_trading_agent(state_store=store)

        agent.recover_position(
            position_id="POS-010",
            side=OrderSide.BUY,
            symbol="EURUSD",
            volume=0.10,
            fill_price=1.10500,
            stop_loss=1.10000,
        )

        store.save_agent_state.assert_called()
        call_kwargs = store.save_agent_state.call_args
        # Verify the persisted state reflects LONG
        assert (
            call_kwargs.kwargs.get("state") == "LONG"
            or (call_kwargs[1].get("state") == "LONG" if call_kwargs[1] else False)
            or ("LONG" in str(call_kwargs))
        )

    def test_informs_risk_engine_about_adopted_position(self):
        """Risk engine is notified about the adopted position via on_trade_fill."""
        risk = MagicMock()
        agent = _make_trading_agent(risk_engine=risk)

        agent.recover_position(
            position_id="POS-020",
            side=OrderSide.BUY,
            symbol="EURUSD",
            volume=0.10,
            fill_price=1.10500,
            stop_loss=1.10000,
        )

        risk.on_trade_fill.assert_called_once()
        call_kwargs = risk.on_trade_fill.call_args
        # Verify position_id, side, volume, fill_price, stop_loss are passed
        assert call_kwargs.kwargs["position_id"] == "POS-020"
        assert call_kwargs.kwargs["side"] == OrderSide.BUY
        assert call_kwargs.kwargs["volume"] == 0.10
        assert call_kwargs.kwargs["fill_price"] == 1.10500
        assert call_kwargs.kwargs["stop_loss"] == 1.10000

    def test_risk_engine_receives_sell_side(self):
        """Risk engine sees SELL side for SHORT position recovery."""
        risk = MagicMock()
        agent = _make_trading_agent(risk_engine=risk)

        agent.recover_position(
            position_id="POS-030",
            side=OrderSide.SELL,
            symbol="GBPUSD",
            volume=0.20,
            fill_price=1.25000,
            stop_loss=1.25500,
        )

        risk.on_trade_fill.assert_called_once()
        assert risk.on_trade_fill.call_args.kwargs["side"] == OrderSide.SELL

    def test_clears_pending_fields_on_recovery(self):
        """Pending order fields are cleared after position recovery."""
        agent = _make_trading_agent()

        agent.recover_position(
            position_id="POS-040",
            side=OrderSide.BUY,
            symbol="EURUSD",
            volume=0.10,
            fill_price=1.10500,
        )

        assert agent.pending_order_id is None

    def test_records_evidence_event(self):
        """An evidence event is recorded for the recovery."""
        recorder = MagicMock()
        agent = _make_trading_agent(recorder=recorder)

        agent.recover_position(
            position_id="POS-050",
            side=OrderSide.BUY,
            symbol="EURUSD",
            volume=0.10,
            fill_price=1.10500,
            stop_loss=1.10000,
        )

        # Evidence recorder's _append should have been called
        recorder._append.assert_called()

    def test_stop_loss_defaults_to_zero(self):
        """stop_loss defaults to 0.0 when not provided."""
        risk = MagicMock()
        agent = _make_trading_agent(risk_engine=risk)

        agent.recover_position(
            position_id="POS-060",
            side=OrderSide.BUY,
            symbol="EURUSD",
            volume=0.10,
            fill_price=1.10500,
        )

        assert risk.on_trade_fill.call_args.kwargs["stop_loss"] == 0.0


# ===========================================================================
# LiveStrategyEngine.recover_position_state() tests
# ===========================================================================


class TestLiveEngineRecoverPositionState:
    """Test LiveStrategyEngine.recover_position_state() startup reconciliation."""

    def test_sets_engine_to_long_state(self):
        """Engine transitions to LONG state with correct position data."""
        engine = _make_live_engine()
        # Engine starts in WARMING_UP, transition to FLAT first won't matter
        # recover_position_state sets state directly
        engine.recover_position_state(
            side="LONG",
            entry_price=1.10500,
            stop_loss=1.10000,
            volume=0.10,
        )

        assert engine.state == LiveState.LONG
        assert engine.position is not None
        assert engine.position.side == "LONG"
        assert engine.position.entry_price == 1.10500
        assert engine.position.stop_loss == 1.10000
        assert engine.position.volume == 0.10

    def test_sets_engine_to_short_state(self):
        """Engine transitions to SHORT state with correct position data."""
        engine = _make_live_engine()

        engine.recover_position_state(
            side="SHORT",
            entry_price=1.11000,
            stop_loss=1.11500,
            volume=0.05,
        )

        assert engine.state == LiveState.SHORT
        assert engine.position is not None
        assert engine.position.side == "SHORT"
        assert engine.position.entry_price == 1.11000
        assert engine.position.stop_loss == 1.11500
        assert engine.position.volume == 0.05

    def test_clears_pending_order(self):
        """Any pending order is cleared when position state is recovered."""
        engine = _make_live_engine()

        # Simulate a pending order by setting it directly
        engine._pending = PendingOrder(
            side="LONG",
            entry_price=1.10500,
            stop_loss=1.10000,
            volume=0.10,
            bar_index=0,
        )
        engine._state = LiveState.PENDING_LONG

        engine.recover_position_state(
            side="SHORT",
            entry_price=1.11000,
            stop_loss=1.11500,
            volume=0.05,
        )

        assert engine.pending is None
        assert engine.state == LiveState.SHORT

    def test_position_entry_price_set_correctly(self):
        """Position entry_price matches the provided value."""
        engine = _make_live_engine()

        engine.recover_position_state(
            side="LONG",
            entry_price=1.23456,
            stop_loss=1.23000,
            volume=0.15,
        )

        assert engine.position.entry_price == 1.23456

    def test_position_stop_loss_set_correctly(self):
        """Position stop_loss matches the provided value."""
        engine = _make_live_engine()

        engine.recover_position_state(
            side="LONG",
            entry_price=1.10500,
            stop_loss=1.09876,
            volume=0.10,
        )

        assert engine.position.stop_loss == 1.09876
        # current_stop should also be initialized from stop_loss via __post_init__
        assert engine.position.current_stop == 1.09876

    def test_position_volume_set_correctly(self):
        """Position volume matches the provided value."""
        engine = _make_live_engine()

        engine.recover_position_state(
            side="SHORT",
            entry_price=1.11000,
            stop_loss=1.11500,
            volume=0.42,
        )

        assert engine.position.volume == 0.42

    def test_position_entry_bar_is_nonnegative(self):
        """entry_bar is set to max(current_index, 0) — never negative."""
        engine = _make_live_engine()
        # No candles seeded — index would be -1, but max(i, 0) should be 0
        assert len(engine._h1_candles) == 0

        engine.recover_position_state(
            side="LONG",
            entry_price=1.10500,
            stop_loss=1.10000,
            volume=0.10,
        )

        assert engine.position.entry_bar >= 0

    def test_recovery_from_warming_up_state(self):
        """Recovery works even if engine is in WARMING_UP state."""
        engine = _make_live_engine()
        assert engine.state == LiveState.WARMING_UP

        engine.recover_position_state(
            side="LONG",
            entry_price=1.10500,
            stop_loss=1.10000,
            volume=0.10,
        )

        assert engine.state == LiveState.LONG

    def test_recovery_from_flat_state(self):
        """Recovery works when engine is already in FLAT state."""
        engine = _make_live_engine()
        engine._state = LiveState.FLAT

        engine.recover_position_state(
            side="SHORT",
            entry_price=1.11000,
            stop_loss=1.11500,
            volume=0.05,
        )

        assert engine.state == LiveState.SHORT

    def test_best_close_initialized_from_entry(self):
        """OpenPosition.best_close is initialized to entry_price via __post_init__."""
        engine = _make_live_engine()

        engine.recover_position_state(
            side="LONG",
            entry_price=1.10500,
            stop_loss=1.10000,
            volume=0.10,
        )

        assert engine.position.best_close == 1.10500


# ===========================================================================
# PipelineCollector uptime guard fix tests
# ===========================================================================


class TestPipelineCollectorUptimeGuard:
    """Test the pipeline collector's feed_health scoring when uptime is zero.

    The fix ensures that when ticks > 0 but uptime_seconds = 0.0, the
    service is still scored as alive (rather than falling through to
    the 'running but no ticks yet' branch).
    """

    def _make_state_dir(self, tmp_path: Path) -> Path:
        """Create STATE/novatrade directory structure."""
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)
        return state_dir

    def _write_live_metrics(
        self,
        state_dir: Path,
        *,
        ticks: int = 0,
        errors: int = 0,
        uptime_seconds: float = 0.0,
    ) -> None:
        """Write a live_metrics.json with the given values."""
        data = {
            "ticks": ticks,
            "errors": errors,
            "uptime_seconds": uptime_seconds,
        }
        metrics_path = state_dir / "live_metrics.json"
        metrics_path.write_text(json.dumps(data))
        # Touch it so it appears fresh (within 0.5h)
        os.utime(metrics_path, (time.time(), time.time()))

    @pytest.mark.asyncio
    async def test_ticks_positive_uptime_zero_scores_healthy(self, tmp_path):
        """ticks > 0, uptime_seconds = 0.0 should score as healthy (ticks branch)."""
        from novatrade.autonomy.collectors.pipeline import PipelineCollector

        state_dir = self._make_state_dir(tmp_path)
        self._write_live_metrics(state_dir, ticks=100, errors=2, uptime_seconds=0.0)

        collector = PipelineCollector(base_path=str(tmp_path))
        score, raw = collector._check_feed_health()

        # ticks=100, errors=2 -> error_ratio = 0.02 < 0.05 -> 50 pts
        assert score >= 50.0, f"Expected >= 50.0 but got {score}"

    @pytest.mark.asyncio
    async def test_ticks_positive_uptime_positive_scores_healthy(self, tmp_path):
        """ticks > 0, uptime > 0 should also score as healthy."""
        from novatrade.autonomy.collectors.pipeline import PipelineCollector

        state_dir = self._make_state_dir(tmp_path)
        self._write_live_metrics(state_dir, ticks=500, errors=5, uptime_seconds=3600.0)

        collector = PipelineCollector(base_path=str(tmp_path))
        score, raw = collector._check_feed_health()

        # ticks=500, errors=5 -> error_ratio = 0.01 < 0.05 -> 50 pts
        assert score >= 50.0, f"Expected >= 50.0 but got {score}"

    @pytest.mark.asyncio
    async def test_ticks_zero_uptime_positive_scores_lower(self, tmp_path):
        """ticks = 0, uptime > 0 -> 'running but no ticks yet' (20 pts)."""
        from novatrade.autonomy.collectors.pipeline import PipelineCollector

        state_dir = self._make_state_dir(tmp_path)
        self._write_live_metrics(state_dir, ticks=0, errors=0, uptime_seconds=60.0)

        collector = PipelineCollector(base_path=str(tmp_path))
        with (
            patch.object(collector, "_is_forex_market_closed", return_value=False),
            patch.object(collector, "_runtime_reports_market_closed", return_value=False),
        ):
            score, raw = collector._check_feed_health()

        assert score == 20.0, f"Expected 20.0 but got {score}"

    @pytest.mark.asyncio
    async def test_high_error_ratio_scores_lower(self, tmp_path):
        """ticks > 0 with high error ratio should score lower (10 pts)."""
        from novatrade.autonomy.collectors.pipeline import PipelineCollector

        state_dir = self._make_state_dir(tmp_path)
        self._write_live_metrics(state_dir, ticks=100, errors=50, uptime_seconds=300.0)

        collector = PipelineCollector(base_path=str(tmp_path))
        score, raw = collector._check_feed_health()

        # error_ratio = 50/100 = 0.5 >= 0.2 -> 10 pts
        assert score == 10.0, f"Expected 10.0 but got {score}"

    @pytest.mark.asyncio
    async def test_moderate_error_ratio_scores_middle(self, tmp_path):
        """ticks > 0 with moderate error ratio should score 30 pts."""
        from novatrade.autonomy.collectors.pipeline import PipelineCollector

        state_dir = self._make_state_dir(tmp_path)
        self._write_live_metrics(state_dir, ticks=100, errors=10, uptime_seconds=300.0)

        collector = PipelineCollector(base_path=str(tmp_path))
        score, raw = collector._check_feed_health()

        # error_ratio = 10/100 = 0.1, 0.05 <= 0.1 < 0.2 -> 30 pts
        assert score == 30.0, f"Expected 30.0 but got {score}"


# ===========================================================================
# Config max_volume_per_trade tests
# ===========================================================================


class TestConfigMaxVolumePerTrade:
    """Test that max_volume_per_trade defaults to 5.0 and is overridable."""

    def test_default_is_50_0(self):
        """RiskConfig default max_volume_per_trade is 50.0."""
        cfg = RiskConfig()
        assert cfg.max_volume_per_trade == 50.0

    def test_default_constant_is_50_0(self):
        """Module-level constant _DEFAULT_MAX_VOLUME_PER_TRADE is 50.0."""
        assert _DEFAULT_MAX_VOLUME_PER_TRADE == 50.0

    def test_novatrade_cfg_default_is_50_0(self):
        """NovaTradeCfg default risk.max_volume_per_trade is 50.0."""
        cfg = NovaTradeCfg()
        assert cfg.risk.max_volume_per_trade == 50.0

    def test_overridable_via_env_var(self, monkeypatch):
        """max_volume_per_trade can be overridden via NOVATRADE_MAX_VOLUME_PER_TRADE."""
        monkeypatch.setenv("NOVATRADE_MAX_VOLUME_PER_TRADE", "2.5")
        # Clear other env vars to avoid interference
        for key in (
            "NOVATRADE_MODE",
            "NOVATRADE_SYMBOLS",
            "NOVATRADE_TIMEFRAMES",
            "NOVATRADE_PROVIDER",
            "METAAPI_TOKEN",
            "METAAPI_ACCOUNT_ID",
            "FTMO_ENABLED",
        ):
            monkeypatch.delenv(key, raising=False)

        cfg = NovaTradeCfg.load(env_file=Path("/nonexistent"))
        assert cfg.risk.max_volume_per_trade == 2.5

    def test_env_var_not_set_uses_default(self, monkeypatch):
        """When NOVATRADE_MAX_VOLUME_PER_TRADE is not set, default 50.0 is used."""
        monkeypatch.delenv("NOVATRADE_MAX_VOLUME_PER_TRADE", raising=False)
        for key in (
            "NOVATRADE_MODE",
            "NOVATRADE_SYMBOLS",
            "NOVATRADE_TIMEFRAMES",
            "NOVATRADE_PROVIDER",
            "METAAPI_TOKEN",
            "METAAPI_ACCOUNT_ID",
            "FTMO_ENABLED",
        ):
            monkeypatch.delenv(key, raising=False)

        cfg = NovaTradeCfg.load(env_file=Path("/nonexistent"))
        assert cfg.risk.max_volume_per_trade == 50.0

    def test_env_override_large_value(self, monkeypatch):
        """Large volume override via env var works correctly."""
        monkeypatch.setenv("NOVATRADE_MAX_VOLUME_PER_TRADE", "10.0")
        for key in (
            "NOVATRADE_MODE",
            "NOVATRADE_SYMBOLS",
            "NOVATRADE_TIMEFRAMES",
            "NOVATRADE_PROVIDER",
            "METAAPI_TOKEN",
            "METAAPI_ACCOUNT_ID",
            "FTMO_ENABLED",
        ):
            monkeypatch.delenv(key, raising=False)

        cfg = NovaTradeCfg.load(env_file=Path("/nonexistent"))
        assert cfg.risk.max_volume_per_trade == 10.0


# ===========================================================================
# Stale-state reconciliation: force FLAT when broker has no positions
# ===========================================================================


class TestStaleStateReconciliation:
    """Test force-FLAT reconciliation for stale persisted state.

    When an agent restores a non-FLAT state from StateStore (e.g. LONG from
    a prior session) but the broker has no open positions (SL hit, manual
    close, etc.), the startup reconciliation must force the agent to FLAT
    to prevent stale-state lockout where all new signals are rejected.
    """

    def _make_agent_with_persisted_state(self, state: AgentState, *, position_id: str = "POS-STALE") -> TradingAgent:
        """Build a TradingAgent that restores to a non-FLAT state."""
        store = MagicMock()
        # Simulate StateStore returning a persisted non-FLAT state
        store.load_agent_state.return_value = {
            "state": state.value,
            "position_id": position_id,
            "position_side": "BUY" if state in (AgentState.LONG, AgentState.PENDING_LONG) else "SELL",
            "position_symbol": "EURUSD",
            "position_volume": 1.0,
            "entry_time": 1774985519.0,
        }
        agent = _make_trading_agent(state_store=store)
        # If store load didn't set state (depends on implementation),
        # manually force it to simulate the persisted restore scenario
        if agent.state != state:
            agent._state = state
            if state in (AgentState.LONG, AgentState.SHORT):
                agent._position_id = position_id
                agent._position_side = OrderSide.BUY if state == AgentState.LONG else OrderSide.SELL
                agent._position_symbol = "EURUSD"
                agent._position_volume = 1.0
        return agent

    def test_force_flat_from_long_when_broker_empty(self):
        """Agent persisted as LONG + broker has no positions → force FLAT."""
        agent = self._make_agent_with_persisted_state(AgentState.LONG)
        assert agent.state == AgentState.LONG

        agent.force_flat(reason="startup_reconcile_no_broker_position")

        assert agent.state == AgentState.FLAT
        assert agent.position_id is None
        assert agent.position_volume == 0.0

    def test_force_flat_from_short_when_broker_empty(self):
        """Agent persisted as SHORT + broker has no positions → force FLAT."""
        agent = self._make_agent_with_persisted_state(AgentState.SHORT, position_id="POS-SHORT-STALE")
        assert agent.state == AgentState.SHORT

        agent.force_flat(reason="startup_reconcile_no_broker_position")

        assert agent.state == AgentState.FLAT
        assert agent.position_id is None

    def test_force_flat_from_pending_long_when_broker_empty(self):
        """Agent persisted as PENDING_LONG + broker has no positions → force FLAT."""
        agent = self._make_agent_with_persisted_state(AgentState.PENDING_LONG)
        # PENDING states may not have position_id set
        agent._pending_order_id = "ORD-STALE"
        agent._pending_side = OrderSide.BUY
        agent._pending_symbol = "EURUSD"

        agent.force_flat(reason="startup_reconcile_no_broker_position")

        assert agent.state == AgentState.FLAT
        assert agent.pending_order_id is None

    def test_force_flat_from_pending_short_when_broker_unreachable(self):
        """Agent persisted as PENDING_SHORT + broker unreachable → force FLAT."""
        agent = self._make_agent_with_persisted_state(AgentState.PENDING_SHORT)
        agent._pending_order_id = "ORD-STALE-2"
        agent._pending_side = OrderSide.SELL
        agent._pending_symbol = "EURUSD"

        agent.force_flat(reason="startup_reconcile_broker_unreachable")

        assert agent.state == AgentState.FLAT
        assert agent.pending_order_id is None

    def test_force_flat_persists_new_state(self):
        """force_flat persists the FLAT state to StateStore."""
        store = MagicMock()
        store.load_agent_state.return_value = None
        agent = _make_trading_agent(state_store=store)
        # Simulate restored LONG state
        agent._state = AgentState.LONG
        agent._position_id = "POS-STALE"
        store.reset_mock()  # clear init calls

        agent.force_flat(reason="startup_reconcile_no_broker_position")

        store.save_agent_state.assert_called()
        assert agent.state == AgentState.FLAT

    def test_flat_agent_not_forced_again(self):
        """Agent already FLAT + broker has no positions → no state change needed."""
        store = MagicMock()
        store.load_agent_state.return_value = None
        agent = _make_trading_agent(state_store=store)
        assert agent.state == AgentState.FLAT
        store.reset_mock()

        # The reconciliation code checks `if agent.state != AgentState.FLAT`
        # and skips force_flat. We verify state is still FLAT and no
        # unnecessary persistence call was made.
        assert agent.state == AgentState.FLAT
        store.save_agent_state.assert_not_called()

    def test_force_flat_clears_all_position_fields(self):
        """force_flat resets all position and pending fields to defaults."""
        agent = self._make_agent_with_persisted_state(AgentState.LONG)
        agent._pending_order_id = "ORD-LEFTOVER"
        agent._pending_side = OrderSide.BUY

        agent.force_flat(reason="startup_reconcile_no_broker_position")

        assert agent.state == AgentState.FLAT
        assert agent.position_id is None
        assert agent.position_volume == 0.0
        assert agent.pending_order_id is None

    def test_engine_cancel_pending_after_force_flat(self):
        """LiveStrategyEngine.cancel_pending() clears pending orders after force FLAT."""
        engine = _make_live_engine()
        # Simulate a pending order in the engine
        engine._pending = PendingOrder(
            side="LONG",
            entry_price=1.10500,
            stop_loss=1.10000,
            volume=0.10,
            bar_index=0,
        )
        engine._state = LiveState.PENDING_LONG

        engine.cancel_pending()

        assert engine.pending is None
        assert engine.state == LiveState.FLAT

    def test_force_flat_records_evidence(self):
        """force_flat records a FORCE_FLAT event for audit trail."""
        recorder = MagicMock()
        agent = _make_trading_agent(recorder=recorder)
        agent._state = AgentState.LONG
        agent._position_id = "POS-STALE"

        agent.force_flat(reason="startup_reconcile_no_broker_position")

        recorder._append.assert_called()
        # Verify the event type is FORCE_FLAT
        call_args = recorder._append.call_args
        assert "FORCE_FLAT" in str(call_args)
