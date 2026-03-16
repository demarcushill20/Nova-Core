"""Tests for novatrade.monitor.health — health snapshot capture."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from novatrade.models import (
    AccountState,
    HealthSnapshot,
    HealthState,
    HealthStatus,
    OrderSide,
    Position,
)
from novatrade.monitor.health import take_health_snapshot

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _healthy_status() -> HealthStatus:
    return HealthStatus(state=HealthState.OK, connected=True, latency_ms=12.5)


def _degraded_status() -> HealthStatus:
    return HealthStatus(state=HealthState.DEGRADED, connected=True, message="high latency")


def _down_status() -> HealthStatus:
    return HealthStatus(state=HealthState.DOWN, connected=False, message="connection lost")


def _account() -> AccountState:
    return AccountState(balance=10_000.0, equity=10_200.0, free_margin=9_500.0)


def _positions() -> list[Position]:
    return [
        Position(
            position_id="100",
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=0.10,
            open_price=1.08000,
            unrealized_pnl=50.0,
        ),
        Position(
            position_id="101",
            symbol="GBPUSD",
            side=OrderSide.SELL,
            volume=0.05,
            open_price=1.26000,
            unrealized_pnl=-20.0,
        ),
    ]


def _make_adapter(
    health: HealthStatus | Exception,
    account: AccountState | Exception | None = None,
    positions: list[Position] | Exception | None = None,
) -> MagicMock:
    adapter = MagicMock()

    if isinstance(health, Exception):
        adapter.health_check = AsyncMock(side_effect=health)
    else:
        adapter.health_check = AsyncMock(return_value=health)

    if isinstance(account, Exception):
        adapter.get_account = AsyncMock(side_effect=account)
    elif account is not None:
        adapter.get_account = AsyncMock(return_value=account)

    if isinstance(positions, Exception):
        adapter.get_positions = AsyncMock(side_effect=positions)
    elif positions is not None:
        adapter.get_positions = AsyncMock(return_value=positions)

    return adapter


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHealthySnapshot:
    @pytest.mark.asyncio
    async def test_ok_snapshot(self):
        adapter = _make_adapter(_healthy_status(), _account(), _positions())
        snap = await take_health_snapshot(adapter)

        assert snap.ok is True
        assert snap.adapter_health.state == HealthState.OK
        assert snap.account is not None
        assert snap.account.balance == 10_000.0
        assert snap.position_count == 2
        assert snap.total_unrealized_pnl == pytest.approx(30.0)
        assert snap.error == ""

    @pytest.mark.asyncio
    async def test_no_positions(self):
        adapter = _make_adapter(_healthy_status(), _account(), [])
        snap = await take_health_snapshot(adapter)

        assert snap.ok is True
        assert snap.position_count == 0
        assert snap.total_unrealized_pnl == 0.0

    @pytest.mark.asyncio
    async def test_degraded_still_ok(self):
        adapter = _make_adapter(_degraded_status(), _account(), _positions())
        snap = await take_health_snapshot(adapter)

        assert snap.ok is True
        assert snap.adapter_health.state == HealthState.DEGRADED


# ---------------------------------------------------------------------------
# Adapter health failure
# ---------------------------------------------------------------------------


class TestHealthCheckFailure:
    @pytest.mark.asyncio
    async def test_health_check_exception(self):
        adapter = _make_adapter(RuntimeError("connection refused"))
        snap = await take_health_snapshot(adapter)

        assert snap.ok is False
        assert snap.adapter_health.state == HealthState.DOWN
        assert "connection refused" in snap.error
        assert snap.account is None

    @pytest.mark.asyncio
    async def test_health_check_returns_down(self):
        adapter = _make_adapter(_down_status(), _account(), _positions())
        snap = await take_health_snapshot(adapter)

        assert snap.ok is False
        assert snap.adapter_health.state == HealthState.DOWN
        assert "connection lost" in snap.error
        assert snap.account is None  # early return before get_account


# ---------------------------------------------------------------------------
# Downstream failures
# ---------------------------------------------------------------------------


class TestDownstreamFailures:
    @pytest.mark.asyncio
    async def test_get_account_failure(self):
        adapter = _make_adapter(
            _healthy_status(),
            RuntimeError("account fetch failed"),
            _positions(),
        )
        snap = await take_health_snapshot(adapter)

        assert snap.ok is False
        assert "get_account failed" in snap.error
        assert snap.adapter_health.state == HealthState.OK
        assert snap.account is None

    @pytest.mark.asyncio
    async def test_get_positions_failure(self):
        adapter = _make_adapter(
            _healthy_status(),
            _account(),
            RuntimeError("positions unavailable"),
        )
        snap = await take_health_snapshot(adapter)

        assert snap.ok is False
        assert "get_positions failed" in snap.error
        assert snap.account is not None
        assert snap.position_count == 0


# ---------------------------------------------------------------------------
# Model properties
# ---------------------------------------------------------------------------


class TestHealthSnapshotModel:
    def test_ok_when_healthy_no_error(self):
        snap = HealthSnapshot(adapter_health=_healthy_status())
        assert snap.ok is True

    def test_not_ok_when_down(self):
        snap = HealthSnapshot(adapter_health=_down_status())
        assert snap.ok is False

    def test_not_ok_when_error_present(self):
        snap = HealthSnapshot(adapter_health=_healthy_status(), error="something wrong")
        assert snap.ok is False

    def test_timestamp_auto_set(self):
        snap = HealthSnapshot(adapter_health=_healthy_status())
        assert snap.timestamp > 0
