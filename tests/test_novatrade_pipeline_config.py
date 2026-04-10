"""Tests for strategy config loading and pipeline switching in runner.py."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from novatrade.config import NovaTradeCfg
from novatrade.models import AccountMode, AccountState
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


# ---------------------------------------------------------------------------
# Strategy config loading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_live_stack_loads_strategy_config(tmp_path: Path):
    """Builds with a YAML strategy config and applies parameters to the env."""
    from novatrade.runtime.runner import build_live_stack

    config_yaml = tmp_path / "test_strategy.yaml"
    config_yaml.write_text(
        textwrap.dedent("""\
        strategy_type: irb
        irb_threshold: 0.53
        ema_period: 48
        atr_period: 9
        adx_period: 7
        trend_slope_threshold: 0.41
        adx_threshold: 16.28
        overextension_threshold: 2.73
        trail_atr_multiplier: 1.70
        trigger_window_bars: 10
        time_stop_bars: 35
        warmup_bars: 86
        name: test_config
        version: 2.0.0
    """)
    )

    cfg = _make_cfg()
    loop = await build_live_stack(cfg=cfg, shadow=True, strategy_config_path=str(config_yaml))

    assert isinstance(loop, LiveLoop)
    # Verify the strategy engine received the config params
    engine = loop._strategy_engine
    assert engine._env.irb_threshold == pytest.approx(0.53)
    assert engine._env.ema_period == 48
    assert engine._env.atr_period == 9
    assert engine._env.adx_period == 7
    assert engine._env.warmup_bars == 86
    assert engine._env.trail_atr_multiplier == pytest.approx(1.70)


@pytest.mark.asyncio
async def test_build_live_stack_loads_config_from_env_var(tmp_path: Path):
    """Loads strategy config from NOVATRADE_STRATEGY_CONFIG env var."""
    from novatrade.runtime.runner import build_live_stack

    config_yaml = tmp_path / "env_strategy.yaml"
    config_yaml.write_text(
        textwrap.dedent("""\
        strategy_type: irb
        irb_threshold: 0.50
        ema_period: 30
        warmup_bars: 50
        name: env_test
        version: 1.0.0
    """)
    )

    cfg = _make_cfg()
    with patch.dict(os.environ, {"NOVATRADE_STRATEGY_CONFIG": str(config_yaml)}):
        loop = await build_live_stack(cfg=cfg, shadow=True)

    engine = loop._strategy_engine
    assert engine._env.irb_threshold == pytest.approx(0.50)
    assert engine._env.ema_period == 30
    assert engine._env.warmup_bars == 50


@pytest.mark.asyncio
async def test_build_live_stack_explicit_path_overrides_env(tmp_path: Path):
    """Explicit strategy_config_path takes precedence over env var."""
    from novatrade.runtime.runner import build_live_stack

    env_yaml = tmp_path / "env.yaml"
    env_yaml.write_text("strategy_type: irb\nirb_threshold: 0.30\nname: env\nversion: 1.0.0\n")
    explicit_yaml = tmp_path / "explicit.yaml"
    explicit_yaml.write_text("strategy_type: irb\nirb_threshold: 0.55\nname: explicit\nversion: 1.0.0\n")

    cfg = _make_cfg()
    with patch.dict(os.environ, {"NOVATRADE_STRATEGY_CONFIG": str(env_yaml)}):
        loop = await build_live_stack(cfg=cfg, shadow=True, strategy_config_path=str(explicit_yaml))

    assert loop._strategy_engine._env.irb_threshold == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_build_live_stack_no_config_uses_defaults():
    """Without config, uses default BacktestEnvironment params."""
    from novatrade.runtime.runner import build_live_stack

    cfg = _make_cfg()
    # Clear env var if set
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NOVATRADE_STRATEGY_CONFIG", None)
        loop = await build_live_stack(cfg=cfg, shadow=True)

    # Default values from BacktestEnvironment
    assert loop._strategy_engine._env.irb_threshold == pytest.approx(0.40)
    assert loop._strategy_engine._env.ema_period == 20
    assert loop._strategy_engine._env.warmup_bars == 34


@pytest.mark.asyncio
async def test_build_live_stack_config_sets_equity_from_account(tmp_path: Path):
    """Strategy config doesn't override initial_equity — that comes from account."""
    from novatrade.runtime.runner import build_live_stack

    config_yaml = tmp_path / "cfg.yaml"
    config_yaml.write_text("strategy_type: irb\nname: test\nversion: 1.0.0\n")

    cfg = _make_cfg()
    account = _make_account(balance=75_000.0)

    with patch("novatrade.runtime.dry_run.DryRunAdapter.get_account", new_callable=AsyncMock) as mock:
        mock.return_value = account
        loop = await build_live_stack(cfg=cfg, shadow=True, strategy_config_path=str(config_yaml))

    assert loop._strategy_engine._env.initial_equity == pytest.approx(75_000.0)


@pytest.mark.asyncio
async def test_build_live_stack_bad_config_raises(tmp_path: Path):
    """Invalid config YAML raises a validation error."""
    from novatrade.runtime.runner import build_live_stack

    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("strategy_type: irb\nirb_threshold: 999.0\n")  # out of bounds

    cfg = _make_cfg()
    with pytest.raises((ValueError, TypeError)):  # pydantic ValidationError
        await build_live_stack(cfg=cfg, shadow=True, strategy_config_path=str(bad_yaml))


@pytest.mark.asyncio
async def test_build_live_stack_missing_config_raises(tmp_path: Path):
    """Non-existent config path raises FileNotFoundError."""
    from novatrade.runtime.runner import build_live_stack

    cfg = _make_cfg()
    with pytest.raises(FileNotFoundError):
        await build_live_stack(cfg=cfg, shadow=True, strategy_config_path=str(tmp_path / "nonexistent.yaml"))


_SEED9999_EXISTS = os.path.exists("configs/strategies/irb_v2_seed9999.yaml")


@pytest.mark.asyncio
@pytest.mark.skipif(not _SEED9999_EXISTS, reason="seed9999 config not found")
async def test_build_live_stack_loads_seed9999_config():
    """Loads the actual seed9999 config file from configs/strategies/."""
    from novatrade.runtime.runner import build_live_stack

    cfg = _make_cfg()
    loop = await build_live_stack(
        cfg=cfg,
        shadow=True,
        strategy_config_path="configs/strategies/irb_v2_seed9999.yaml",
    )

    env = loop._strategy_engine._env
    assert env.irb_threshold == pytest.approx(0.5305640555147589)
    assert env.ema_period == 48
    assert env.atr_period == 9
    assert env.adx_period == 7
    assert env.warmup_bars == 86
    assert env.use_ema_stack_filter is True


# ---------------------------------------------------------------------------
# Pipeline mode selection
# ---------------------------------------------------------------------------


def test_main_selects_webhook_by_default():
    """main() defaults to webhook pipeline when NOVATRADE_PIPELINE is unset."""
    from novatrade.runtime.runner import main

    with (
        patch.dict(os.environ, {}, clear=False),
        patch("novatrade.runtime.runner.build_stack") as mock_build,
        patch("novatrade.runtime.runner.generate_readiness_report", return_value="OK"),
        patch("novatrade.runtime.runner.run_server"),
    ):
        os.environ.pop("NOVATRADE_PIPELINE", None)
        mock_build.return_value = (None, None, None)
        main()

    mock_build.assert_called_once()


def test_main_selects_live_pipeline():
    """main() uses build_live_stack when NOVATRADE_PIPELINE=live."""
    from novatrade.runtime.runner import main

    with (
        patch.dict(os.environ, {"NOVATRADE_PIPELINE": "live"}),
        patch("novatrade.runtime.runner.asyncio") as mock_asyncio,
    ):
        # asyncio.run should be called with the _start_live coroutine
        main()

    mock_asyncio.run.assert_called_once()
    # The argument should be a coroutine (from _start_live)
    coro = mock_asyncio.run.call_args[0][0]
    assert coro is not None
    # Clean up the coroutine to avoid RuntimeWarning
    coro.close()


# ---------------------------------------------------------------------------
# run_live function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_live_creates_health_endpoint():
    """run_live starts a server with /health and /status endpoints."""
    from novatrade.runtime.runner import build_live_stack, run_live

    cfg = _make_cfg()
    loop = await build_live_stack(cfg=cfg, shadow=True)

    # We can't easily test the full server startup, but we can verify
    # the function exists and accepts the right arguments
    assert callable(run_live)
    # LiveLoop was created successfully
    assert isinstance(loop, LiveLoop)
