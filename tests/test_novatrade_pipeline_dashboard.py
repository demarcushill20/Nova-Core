"""Tests for NovaTrade pipeline health dashboard.

Validates real-time monitoring, health assessment, and status reporting
for the live trading pipeline.
"""

import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from novatrade.config import NovaTradeCfg
from novatrade.models import AccountMode, AccountState
from novatrade.monitor.pipeline_dashboard import PipelineDashboard, PipelineHealth

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir():
    """Temporary data directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "data"
        data_dir.mkdir()
        yield data_dir


@pytest.fixture
def mock_cfg(temp_data_dir):
    """Mock NovaTrade configuration."""
    cfg = Mock(spec=NovaTradeCfg)
    cfg.data_dir = temp_data_dir
    cfg.symbols = ["EURUSD", "GBPUSD"]
    return cfg


@pytest.fixture
def mock_adapter():
    """Mock MT5 adapter."""
    adapter = AsyncMock()
    adapter.__class__.__name__ = "MetaApiAdapter"
    adapter.is_connected.return_value = True
    return adapter


@pytest.fixture
def mock_agent():
    """Mock trading agent."""
    agent = AsyncMock()
    agent.get_metrics.return_value = {
        "orders_pending": 2,
        "orders_filled_1h": 5,
        "orders_rejected_1h": 1,
    }
    return agent


@pytest.fixture
def mock_risk_engine():
    """Mock risk engine."""
    risk_engine = AsyncMock()
    account = AccountState(
        balance=10000.0,
        equity=9800.0,
        mode=AccountMode.DEMO,
    )
    risk_engine.get_account_state.return_value = account
    return risk_engine


@pytest.fixture
def mock_feed_supervisor():
    """Mock feed health supervisor."""
    supervisor = AsyncMock()

    # Mock feed states
    healthy_state = Mock()
    healthy_state.is_stale = False
    healthy_state.staleness_seconds = 5.0
    healthy_state.last_tick_time = time.time() - 5

    stale_state = Mock()
    stale_state.is_stale = True
    stale_state.staleness_seconds = 300.0  # 5 minutes
    stale_state.last_tick_time = time.time() - 300

    supervisor.get_feed_state.side_effect = lambda symbol: healthy_state if symbol == "EURUSD" else stale_state

    return supervisor


# ---------------------------------------------------------------------------
# PipelineHealth Tests
# ---------------------------------------------------------------------------


def test_pipeline_health_creation():
    """Test PipelineHealth dataclass creation."""
    health = PipelineHealth()

    assert health.overall_status == "UNKNOWN"
    assert health.feed_symbols_active == 0
    assert health.feed_symbols_stale == 0
    assert health.timestamp > 0


def test_pipeline_health_to_dict():
    """Test PipelineHealth serialization."""
    health = PipelineHealth(
        overall_status="HEALTHY",
        feed_symbols_active=2,
        feed_symbols_stale=0,
        signal_rate_per_hour=1.5,
    )

    data = health.to_dict()

    assert data["overall_status"] == "HEALTHY"
    assert data["feed"]["symbols_active"] == 2
    assert data["feed"]["symbols_stale"] == 0
    assert data["strategy"]["rate_per_hour"] == 1.5
    assert "timestamp_utc" in data
    assert isinstance(data["timestamp_utc"], str)


# ---------------------------------------------------------------------------
# PipelineDashboard Tests
# ---------------------------------------------------------------------------


def test_dashboard_creation(mock_cfg):
    """Test dashboard creation."""
    dashboard = PipelineDashboard(mock_cfg)

    assert dashboard.cfg == mock_cfg
    assert dashboard.health_history == []
    assert not dashboard._running


@pytest.mark.asyncio
async def test_dashboard_start_stop(mock_cfg):
    """Test dashboard start/stop lifecycle."""
    dashboard = PipelineDashboard(mock_cfg)

    # Start
    await dashboard.start(interval_seconds=0.1)
    assert dashboard._running
    assert dashboard._task is not None

    # Stop
    await dashboard.stop()
    assert not dashboard._running
    assert dashboard._task is None


@pytest.mark.asyncio
async def test_collect_health_snapshot_basic(mock_cfg):
    """Test basic health snapshot collection."""
    dashboard = PipelineDashboard(mock_cfg)

    health = await dashboard.collect_health_snapshot()

    assert isinstance(health, PipelineHealth)
    assert health.timestamp > 0
    assert health.overall_status in ["HEALTHY", "DEGRADED", "UNHEALTHY"]


@pytest.mark.asyncio
async def test_collect_feed_health(mock_cfg, mock_feed_supervisor):
    """Test feed health collection."""
    dashboard = PipelineDashboard(mock_cfg, feed_supervisor=mock_feed_supervisor)

    health = PipelineHealth()
    await dashboard._collect_feed_health(health)

    assert health.feed_symbols_active == 1  # EURUSD healthy
    assert health.feed_symbols_stale == 1  # GBPUSD stale
    assert health.feed_staleness_max_minutes == 5.0
    assert health.feed_last_tick_utc != ""


@pytest.mark.asyncio
async def test_collect_execution_health(mock_cfg, mock_agent):
    """Test execution health collection."""
    dashboard = PipelineDashboard(mock_cfg, agent=mock_agent)

    health = PipelineHealth()
    await dashboard._collect_execution_health(health)

    assert health.orders_pending == 2
    assert health.orders_filled_1h == 5
    assert health.orders_rejected_1h == 1
    assert health.execution_success_rate == 5 / 6  # 5 filled out of 6 total


@pytest.mark.asyncio
async def test_collect_risk_health(mock_cfg, mock_risk_engine):
    """Test risk engine health collection."""
    dashboard = PipelineDashboard(mock_cfg, risk_engine=mock_risk_engine)

    health = PipelineHealth()
    await dashboard._collect_risk_health(health)

    # Account: balance=10000, equity=9800, so drawdown = 2%
    assert health.equity_drawdown_pct == 2.0
    assert not health.kill_switch_active  # No kill switch file


@pytest.mark.asyncio
async def test_collect_risk_health_with_kill_switch(mock_cfg, mock_risk_engine, temp_data_dir):
    """Test risk health collection with kill switch active."""
    # Create kill switch file
    state_dir = temp_data_dir.parent / "STATE"
    state_dir.mkdir(exist_ok=True)
    kill_switch_path = state_dir / "kill_switch.flag"
    kill_switch_path.touch()

    dashboard = PipelineDashboard(mock_cfg, risk_engine=mock_risk_engine)

    health = PipelineHealth()
    await dashboard._collect_risk_health(health)

    assert health.kill_switch_active


@pytest.mark.asyncio
async def test_collect_adapter_health(mock_cfg, mock_adapter):
    """Test adapter health collection."""
    dashboard = PipelineDashboard(mock_cfg, adapter=mock_adapter)

    health = PipelineHealth()
    await dashboard._collect_adapter_health(health)

    assert health.adapter_type == "MetaApiAdapter"
    assert health.adapter_connected


def test_determine_overall_status_healthy(mock_cfg):
    """Test healthy status determination."""
    dashboard = PipelineDashboard(mock_cfg)

    health = PipelineHealth(
        adapter_connected=True,
        feed_symbols_stale=0,
        feed_staleness_max_minutes=1.0,
        errors_1h=0,
        kill_switch_active=False,
        execution_success_rate=1.0,
    )

    status = dashboard._determine_overall_status(health)
    assert status == "HEALTHY"


def test_determine_overall_status_degraded(mock_cfg):
    """Test degraded status determination."""
    dashboard = PipelineDashboard(mock_cfg)

    health = PipelineHealth(
        adapter_connected=True,
        feed_symbols_stale=1,  # One stale feed
        feed_staleness_max_minutes=3.0,
        errors_1h=2,
        kill_switch_active=False,
    )

    status = dashboard._determine_overall_status(health)
    assert status == "DEGRADED"


def test_determine_overall_status_unhealthy_kill_switch(mock_cfg):
    """Test unhealthy status from kill switch."""
    dashboard = PipelineDashboard(mock_cfg)

    health = PipelineHealth(
        adapter_connected=True,
        kill_switch_active=True,  # Kill switch active
    )

    status = dashboard._determine_overall_status(health)
    assert status == "UNHEALTHY"


def test_determine_overall_status_unhealthy_disconnected(mock_cfg):
    """Test unhealthy status from disconnected adapter."""
    dashboard = PipelineDashboard(mock_cfg)

    health = PipelineHealth(
        adapter_connected=False,  # Adapter disconnected
        kill_switch_active=False,
    )

    status = dashboard._determine_overall_status(health)
    assert status == "UNHEALTHY"


def test_determine_overall_status_unhealthy_errors(mock_cfg):
    """Test unhealthy status from high error count."""
    dashboard = PipelineDashboard(mock_cfg)

    health = PipelineHealth(
        adapter_connected=True,
        kill_switch_active=False,
        errors_1h=15,  # High error count
    )

    status = dashboard._determine_overall_status(health)
    assert status == "UNHEALTHY"


def test_add_to_history_basic(mock_cfg):
    """Test adding health snapshots to history."""
    dashboard = PipelineDashboard(mock_cfg)

    health1 = PipelineHealth(overall_status="HEALTHY")
    health2 = PipelineHealth(overall_status="DEGRADED")

    dashboard._add_to_history(health1)
    dashboard._add_to_history(health2)

    assert len(dashboard.health_history) == 2
    assert dashboard.health_history[0].overall_status == "HEALTHY"
    assert dashboard.health_history[1].overall_status == "DEGRADED"


def test_add_to_history_trimming(mock_cfg):
    """Test history trimming to max size."""
    dashboard = PipelineDashboard(mock_cfg)
    dashboard.max_history_size = 3

    for i in range(5):
        health = PipelineHealth(timestamp=float(i))
        dashboard._add_to_history(health)

    # Should keep only last 3
    assert len(dashboard.health_history) == 3
    assert dashboard.health_history[0].timestamp == 2.0
    assert dashboard.health_history[-1].timestamp == 4.0


@pytest.mark.asyncio
async def test_persist_health(mock_cfg, temp_data_dir):
    """Test health persistence to disk."""
    dashboard = PipelineDashboard(mock_cfg)

    health = PipelineHealth(overall_status="HEALTHY")
    await dashboard._persist_health(health)

    # Check current.json exists
    current_path = temp_data_dir / "pipeline_health" / "current.json"
    assert current_path.exists()

    with current_path.open() as f:
        data = json.load(f)
        assert data["overall_status"] == "HEALTHY"

    # Check JSONL log exists
    today_log = temp_data_dir / "pipeline_health" / f"health_{time.strftime('%Y%m%d')}.jsonl"
    assert today_log.exists()


def test_get_status_summary_no_data(mock_cfg):
    """Test status summary with no data."""
    dashboard = PipelineDashboard(mock_cfg)

    summary = dashboard.get_status_summary()
    assert "No health data available" in summary


def test_get_status_summary_with_data(mock_cfg):
    """Test status summary with health data."""
    dashboard = PipelineDashboard(mock_cfg)

    health = PipelineHealth(
        overall_status="HEALTHY",
        feed_symbols_active=2,
        feed_symbols_stale=0,
        orders_filled_1h=5,
        orders_rejected_1h=1,
        adapter_type="MetaApiAdapter",
        adapter_connected=True,
    )

    dashboard.health_history.append(health)

    summary = dashboard.get_status_summary()

    assert "Pipeline Status: HEALTHY" in summary
    assert "Data Feeds: 2 active, 0 stale" in summary
    assert "Execution: 5 filled, 1 rejected (1h)" in summary
    assert "Adapter: MetaApiAdapter connected" in summary


def test_get_status_summary_with_warnings(mock_cfg):
    """Test status summary with warning conditions."""
    dashboard = PipelineDashboard(mock_cfg)

    health = PipelineHealth(
        overall_status="DEGRADED",
        feed_staleness_max_minutes=7.5,
        kill_switch_active=True,
    )

    dashboard.health_history.append(health)

    summary = dashboard.get_status_summary()

    assert "Pipeline Status: DEGRADED" in summary
    assert "Max Feed Staleness: 7.5 minutes" in summary
    assert "⚠️  KILL SWITCH ACTIVE" in summary


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_health_collection_integration(
    mock_cfg, mock_adapter, mock_agent, mock_risk_engine, mock_feed_supervisor
):
    """Test full health collection with all components."""
    dashboard = PipelineDashboard(
        mock_cfg,
        adapter=mock_adapter,
        agent=mock_agent,
        risk_engine=mock_risk_engine,
        feed_supervisor=mock_feed_supervisor,
    )

    health = await dashboard.collect_health_snapshot()

    # Should have data from all components
    assert health.adapter_connected
    assert health.adapter_type == "MetaApiAdapter"
    assert health.orders_filled_1h == 5
    assert health.equity_drawdown_pct == 2.0
    assert health.feed_symbols_active == 1
    assert health.feed_symbols_stale == 1


@pytest.mark.asyncio
async def test_dashboard_monitoring_loop_error_handling(mock_cfg):
    """Test dashboard handles monitoring errors gracefully."""
    dashboard = PipelineDashboard(mock_cfg)

    # Mock collect_health_snapshot to raise an exception
    with patch.object(dashboard, "collect_health_snapshot", side_effect=Exception("Mock error")):
        # Start dashboard for short duration
        await dashboard.start(interval_seconds=0.01)
        await asyncio.sleep(0.05)  # Let it run a few iterations
        await dashboard.stop()

        # Should not crash and should have empty history due to errors
        assert len(dashboard.health_history) == 0


# ---------------------------------------------------------------------------
# Performance Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_collection_performance(
    mock_cfg, mock_adapter, mock_agent, mock_risk_engine, mock_feed_supervisor
):
    """Test health collection performance."""
    dashboard = PipelineDashboard(
        mock_cfg,
        adapter=mock_adapter,
        agent=mock_agent,
        risk_engine=mock_risk_engine,
        feed_supervisor=mock_feed_supervisor,
    )

    start_time = time.time()

    # Collect health 10 times
    for _ in range(10):
        await dashboard.collect_health_snapshot()

    elapsed = time.time() - start_time

    # Should be fast (< 100ms per collection)
    assert elapsed < 1.0, f"Health collection too slow: {elapsed:.3f}s for 10 collections"


@pytest.mark.asyncio
async def test_history_management_performance(mock_cfg):
    """Test history management doesn't become a bottleneck."""
    dashboard = PipelineDashboard(mock_cfg)
    dashboard.max_history_size = 1000

    start_time = time.time()

    # Add 2000 entries (should trigger trimming)
    for i in range(2000):
        health = PipelineHealth(timestamp=float(i))
        dashboard._add_to_history(health)

    elapsed = time.time() - start_time

    # Should maintain max size
    assert len(dashboard.health_history) == 1000

    # Should be reasonably fast
    assert elapsed < 1.0, f"History management too slow: {elapsed:.3f}s for 2000 entries"
