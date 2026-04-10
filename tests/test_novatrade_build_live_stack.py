"""Tests for build_live_stack factory (Phase 5.2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from novatrade.config import NovaTradeCfg
from novatrade.models import AccountMode, AccountState, Candle
from novatrade.runtime.live_loop import LiveLoop


def _make_cfg() -> NovaTradeCfg:
    """Create a minimal NovaTradeCfg for testing."""
    cfg = NovaTradeCfg()
    cfg.symbols = ["EURUSD"]
    cfg.timeframes = ["H1"]
    return cfg


def _make_account(balance: float = 100_000.0) -> AccountState:
    return AccountState(
        balance=balance,
        equity=balance,
        margin=0.0,
        free_margin=balance,
        currency="USD",
        leverage=100,
        mode=AccountMode.DEMO,
        open_positions=0,
    )


def _make_candles(n: int = 10) -> list[Candle]:
    return [
        Candle(
            timestamp=1700000000.0 + i * 3600,
            open=1.08 + i * 0.0001,
            high=1.082 + i * 0.0001,
            low=1.078 + i * 0.0001,
            close=1.081 + i * 0.0001,
            volume=100.0,
            symbol="EURUSD",
            timeframe="H1",
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_build_live_stack_active_demo():
    """Builds successfully with DryRunAdapter and returns a LiveLoop."""
    from novatrade.runtime.runner import build_live_stack

    cfg = _make_cfg()
    loop = await build_live_stack(cfg=cfg, shadow=True)
    assert isinstance(loop, LiveLoop)


@pytest.mark.asyncio
async def test_build_live_stack_fetches_account_balance():
    """Fetches real account balance from adapter."""
    from novatrade.runtime.runner import build_live_stack

    cfg = _make_cfg()
    account = _make_account(balance=50_000.0)

    with patch("novatrade.runtime.dry_run.DryRunAdapter.get_account", new_callable=AsyncMock) as mock_account:
        mock_account.return_value = account
        loop = await build_live_stack(cfg=cfg, shadow=True)

    mock_account.assert_awaited_once()
    assert isinstance(loop, LiveLoop)


@pytest.mark.asyncio
async def test_build_live_stack_seeds_history():
    """Pre-seeds historical candles via adapter.get_candles."""
    from novatrade.runtime.runner import build_live_stack

    cfg = _make_cfg()
    candles = _make_candles(100)

    with patch("novatrade.runtime.dry_run.DryRunAdapter.get_candles", new_callable=AsyncMock) as mock_candles:
        mock_candles.return_value = candles
        loop = await build_live_stack(cfg=cfg, shadow=True)

    # get_candles called for H1 and H4
    assert mock_candles.await_count == 2
    assert isinstance(loop, LiveLoop)


@pytest.mark.asyncio
async def test_build_live_stack_fails_on_account_error():
    """Fails hard when adapter.get_account() raises."""
    from novatrade.runtime.runner import build_live_stack

    cfg = _make_cfg()

    with (
        patch(
            "novatrade.runtime.dry_run.DryRunAdapter.get_account",
            new_callable=AsyncMock,
            side_effect=ConnectionError("broker unreachable"),
        ),
        pytest.raises(RuntimeError, match="Failed to fetch account state"),
    ):
        await build_live_stack(cfg=cfg, shadow=True)


@pytest.mark.asyncio
async def test_build_live_stack_h4_auto_added():
    """H4 timeframe is auto-added if missing from cfg.timeframes."""
    from novatrade.runtime.runner import build_live_stack

    cfg = _make_cfg()
    cfg.timeframes = ["H1"]  # no H4

    loop = await build_live_stack(cfg=cfg, shadow=True)
    assert isinstance(loop, LiveLoop)
    # The aggregator should have H4 in its timeframes
    assert "H4" in loop._aggregator.timeframes


@pytest.mark.asyncio
async def test_build_live_stack_h4_not_duplicated():
    """H4 is not duplicated if already present in cfg.timeframes."""
    from novatrade.runtime.runner import build_live_stack

    cfg = _make_cfg()
    cfg.timeframes = ["H1", "H4"]

    loop = await build_live_stack(cfg=cfg, shadow=True)
    assert loop._aggregator.timeframes.count("H4") == 1


@pytest.mark.asyncio
async def test_build_live_stack_warmup_failure_continues():
    """Warmup failure (get_candles) logs warning but does not raise."""
    from novatrade.runtime.runner import build_live_stack

    cfg = _make_cfg()

    with patch(
        "novatrade.runtime.dry_run.DryRunAdapter.get_candles",
        new_callable=AsyncMock,
        side_effect=ConnectionError("candle fetch failed"),
    ):
        loop = await build_live_stack(cfg=cfg, shadow=True)

    # Should still return a LiveLoop despite warmup failure
    assert isinstance(loop, LiveLoop)
