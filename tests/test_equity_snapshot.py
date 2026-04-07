"""Regression tests for equity snapshot persistence and get_equity() wiring.

Covers the fix for stale equity_history.json when equity is unchanged
(the writer was silently skipping all snapshots when equity == last value).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Helper: build a minimal LiveLoop-like object with _persist_equity_snapshot
# ---------------------------------------------------------------------------


def _make_loop(base_path: str, adapter_equity: float | None = None, agent_equity: float | None = None):
    """Build a minimal LiveLoop with mocked adapter/agent for snapshot testing."""
    from novatrade.runtime.live_loop import LiveLoop

    # Minimal constructor bypass — LiveLoop needs many params; mock them
    loop = object.__new__(LiveLoop)

    # Adapter mock
    adapter = MagicMock()
    if adapter_equity is not None:
        adapter.get_equity = MagicMock(return_value=adapter_equity)
    else:
        # Remove the method so hasattr returns False
        del_spec = MagicMock(spec=[])
        loop.__dict__["_adapter"] = del_spec
        adapter = del_spec

    loop.__dict__["_adapter"] = adapter

    # Live agent mock
    agent = MagicMock()
    if agent_equity is not None:
        agent.last_equity = agent_equity
    else:
        # Make hasattr(agent, "last_equity") return False
        agent_spec = MagicMock(spec=[])
        loop.__dict__["_live_agent"] = agent_spec
        agent = agent_spec

    loop.__dict__["_live_agent"] = agent
    loop.__dict__["_base_path"] = None

    # Use a method that returns base_path
    loop._get_base_path = lambda: base_path  # type: ignore[method-assign]

    return loop


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEquitySnapshotStaleness:
    """The core bug: unchanged equity should still write after 1 hour."""

    def test_unchanged_equity_written_when_last_snapshot_old(self, tmp_path):
        """If equity hasn't changed but the last snapshot is >1h old, write anyway."""
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)

        # Existing snapshot from 2 hours ago
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        eq_data = {
            "snapshots": [
                {"equity": 100000.0, "timestamp": old_time, "source": "live"},
            ],
            "last_updated": old_time,
            "initial_equity": 100000.0,
        }
        (state_dir / "equity_history.json").write_text(json.dumps(eq_data))

        loop = _make_loop(str(tmp_path), adapter_equity=100000.0)
        loop._persist_equity_snapshot()

        # Should have appended a new snapshot
        result = json.loads((state_dir / "equity_history.json").read_text())
        assert len(result["snapshots"]) == 2, "Should write snapshot when last is >1h old even if equity unchanged"
        assert result["snapshots"][-1]["equity"] == 100000.0

    def test_unchanged_equity_skipped_when_last_snapshot_recent(self, tmp_path):
        """If equity hasn't changed and last snapshot is <1h old, skip."""
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)

        now = datetime.now(timezone.utc)
        # Stay within the same clock-hour to avoid flaky hour-boundary crossings
        recent_time = now.replace(minute=max(now.minute - 1, 0), second=0, microsecond=0).isoformat()
        eq_data = {
            "snapshots": [
                {"equity": 100000.0, "timestamp": recent_time, "source": "live"},
            ],
            "last_updated": recent_time,
            "initial_equity": 100000.0,
        }
        (state_dir / "equity_history.json").write_text(json.dumps(eq_data))

        loop = _make_loop(str(tmp_path), adapter_equity=100000.0)
        loop._persist_equity_snapshot()

        result = json.loads((state_dir / "equity_history.json").read_text())
        assert len(result["snapshots"]) == 1, "Should NOT write when recent and unchanged"

    def test_changed_equity_same_hour_skipped(self, tmp_path):
        """Changed equity within the same hour should be rate-limited."""
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)

        now = datetime.now(timezone.utc)
        # Stay within the same clock-hour to avoid flaky hour-boundary crossings
        recent_time = now.replace(minute=max(now.minute - 1, 0), second=0, microsecond=0).isoformat()
        eq_data = {
            "snapshots": [
                {"equity": 100000.0, "timestamp": recent_time, "source": "live"},
            ],
            "last_updated": recent_time,
            "initial_equity": 100000.0,
        }
        (state_dir / "equity_history.json").write_text(json.dumps(eq_data))

        loop = _make_loop(str(tmp_path), adapter_equity=100200.62)
        loop._persist_equity_snapshot()

        result = json.loads((state_dir / "equity_history.json").read_text())
        # Rate-limited: same hour → no new snapshot
        assert len(result["snapshots"]) == 1

    def test_changed_equity_new_hour_written(self, tmp_path):
        """Changed equity in a new hour should be written."""
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)

        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        eq_data = {
            "snapshots": [
                {"equity": 100000.0, "timestamp": old_time, "source": "live"},
            ],
            "last_updated": old_time,
            "initial_equity": 100000.0,
        }
        (state_dir / "equity_history.json").write_text(json.dumps(eq_data))

        loop = _make_loop(str(tmp_path), adapter_equity=100200.62)
        loop._persist_equity_snapshot()

        result = json.loads((state_dir / "equity_history.json").read_text())
        assert len(result["snapshots"]) == 2
        assert result["snapshots"][-1]["equity"] == 100200.62

    def test_no_equity_source_skips(self, tmp_path):
        """If neither adapter nor agent has equity, skip gracefully."""
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)

        eq_data = {"snapshots": [], "initial_equity": 100000.0}
        (state_dir / "equity_history.json").write_text(json.dumps(eq_data))

        loop = _make_loop(str(tmp_path))  # No equity source
        loop._persist_equity_snapshot()

        result = json.loads((state_dir / "equity_history.json").read_text())
        assert len(result["snapshots"]) == 0

    def test_anomaly_guard_rejects_large_jump(self, tmp_path):
        """Equity change >3% should be rejected."""
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)

        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        eq_data = {
            "snapshots": [
                {"equity": 100000.0, "timestamp": recent_time, "source": "live"},
            ],
            "initial_equity": 100000.0,
        }
        (state_dir / "equity_history.json").write_text(json.dumps(eq_data))

        # 5% jump — should be rejected
        loop = _make_loop(str(tmp_path), adapter_equity=105000.0)
        loop._persist_equity_snapshot()

        result = json.loads((state_dir / "equity_history.json").read_text())
        assert len(result["snapshots"]) == 1, "Anomalous jump should be rejected"

    def test_empty_file_creates_first_snapshot(self, tmp_path):
        """First snapshot should always be written."""
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)

        # Empty history
        eq_data = {"snapshots": [], "initial_equity": 100000.0}
        (state_dir / "equity_history.json").write_text(json.dumps(eq_data))

        loop = _make_loop(str(tmp_path), adapter_equity=100200.62)
        loop._persist_equity_snapshot()

        result = json.loads((state_dir / "equity_history.json").read_text())
        assert len(result["snapshots"]) == 1
        assert result["snapshots"][0]["equity"] == 100200.62
        assert result["snapshots"][0]["source"] == "live"

    def test_fallback_to_agent_equity(self, tmp_path):
        """If adapter has no get_equity, fall back to agent.last_equity."""
        state_dir = tmp_path / "STATE" / "novatrade"
        state_dir.mkdir(parents=True)

        eq_data = {"snapshots": [], "initial_equity": 100000.0}
        (state_dir / "equity_history.json").write_text(json.dumps(eq_data))

        loop = _make_loop(str(tmp_path), agent_equity=99500.0)
        loop._persist_equity_snapshot()

        result = json.loads((state_dir / "equity_history.json").read_text())
        assert len(result["snapshots"]) == 1
        assert result["snapshots"][0]["equity"] == 99500.0


class TestGetEquity:
    """Tests for the get_equity() method on adapters."""

    def test_base_adapter_returns_none(self):
        """MT5Adapter.get_equity() default returns None."""
        from novatrade.adapter.base import MT5Adapter

        # Can't instantiate ABC directly, check via subclass
        class StubAdapter(MT5Adapter):
            async def connect(self): ...
            async def disconnect(self): ...
            async def health_check(self): ...
            async def get_account(self): ...
            async def get_positions(self): ...
            async def get_symbol_price(self, s): ...
            async def get_candles(self, s, t, c=100): ...
            async def place_order(self, r): ...
            async def modify_order(self, o, **kw): ...
            async def close_position(self, p, v=None): ...

        adapter = StubAdapter()
        assert adapter.get_equity() is None

    def test_dry_run_adapter_returns_default(self):
        """DryRunAdapter.get_equity() returns 100k when no inner adapter."""
        from novatrade.runtime.dry_run import DryRunAdapter

        adapter = DryRunAdapter(inner=None)
        assert adapter.get_equity() == 100_000.0

    def test_dry_run_adapter_delegates_to_inner(self):
        """DryRunAdapter.get_equity() delegates to inner when present."""
        inner = MagicMock()
        inner.get_equity.return_value = 99800.50

        from novatrade.runtime.dry_run import DryRunAdapter

        adapter = DryRunAdapter(inner=inner)
        assert adapter.get_equity() == 99800.50

    def test_metaapi_adapter_caches_equity(self):
        """MetaApiAdapter._cached_equity starts None, updated on get_equity()."""
        from novatrade.adapter.metaapi_provider import MetaApiAdapter, MetaApiConfig

        config = MetaApiConfig(
            token="test",
            account_id="test",
        )
        adapter = MetaApiAdapter(config)
        assert adapter.get_equity() is None  # before any account info fetch
        adapter._cached_equity = 100200.62
        assert adapter.get_equity() == 100200.62
