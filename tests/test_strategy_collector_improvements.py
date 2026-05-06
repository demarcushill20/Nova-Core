"""Tests for strategy collector improvements (task 0816).

Covers:
- Phase 1: _is_shadow_symbol() override env fallback
- Phase 3: signal_generation_rate pipeline-alive floor
- Phase 4: backtest_live_alignment transition grace
"""

import json
import os
import time
from unittest.mock import patch

import pytest

from novatrade.autonomy.collectors.strategy import StrategyCollector


@pytest.fixture(autouse=True)
def _clear_env_cache():
    """Reset the override env cache between tests."""
    StrategyCollector._override_env_cache = None
    yield
    StrategyCollector._override_env_cache = None


class TestIsShadowSymbolOverrideFallback:
    """Phase 1: _is_shadow_symbol reads override env when os.environ lacks the var."""

    def test_sim_symbol_not_shadow_when_env_var_set(self):
        with patch.dict(os.environ, {"FTMO_SYMBOL_SUFFIX": ".sim"}):
            assert StrategyCollector._is_shadow_symbol("EURUSD.sim") is False

    def test_sim_symbol_is_shadow_when_no_env_and_no_override(self, tmp_path):
        with patch.dict(os.environ, {}, clear=True):
            StrategyCollector._override_env_cache = {}
            assert StrategyCollector._is_shadow_symbol("EURUSD.sim") is True

    def test_sim_symbol_not_shadow_via_override_env(self, tmp_path):
        override = {"FTMO_SYMBOL_SUFFIX": ".sim"}
        with patch.dict(os.environ, {}, clear=True):
            StrategyCollector._override_env_cache = override
            assert StrategyCollector._is_shadow_symbol("EURUSD.sim") is False

    def test_non_sim_symbol_never_shadow(self):
        assert StrategyCollector._is_shadow_symbol("EURUSD") is False


class TestSignalRatePipelineAliveFloor:
    """Phase 3: score floors at 40 when pipeline is alive but no signals."""

    def _make_collector(self, tmp_path):
        c = StrategyCollector(base_path=str(tmp_path))
        state = tmp_path / "STATE" / "novatrade"
        state.mkdir(parents=True, exist_ok=True)
        return c, state

    def test_floor_40_when_ticks_flowing(self, tmp_path):
        c, state = self._make_collector(tmp_path)
        (state / "live_metrics.json").write_text(json.dumps({"ticks": 500}))
        os.utime(state / "live_metrics.json", (time.time(), time.time()))

        with patch("novatrade.autonomy.collectors.strategy.datetime") as mock_dt:
            mock_now = type(
                "MockNow",
                (),
                {
                    "weekday": lambda self: 2,
                    "hour": 14,
                },
            )()
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: __import__("datetime").datetime(*a, **kw)

            score, rate = c._check_signal_rate()
        assert score >= 40.0

    def test_no_floor_when_no_ticks(self, tmp_path):
        c, state = self._make_collector(tmp_path)
        (state / "live_metrics.json").write_text(json.dumps({"ticks": 0}))
        os.utime(state / "live_metrics.json", (time.time(), time.time()))

        with patch("novatrade.autonomy.collectors.strategy.datetime") as mock_dt:
            mock_now = type(
                "MockNow",
                (),
                {
                    "weekday": lambda self: 2,
                    "hour": 14,
                },
            )()
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: __import__("datetime").datetime(*a, **kw)

            score, rate = c._check_signal_rate()
        assert score == 0.0


class TestBacktestAlignmentTransitionGrace:
    """Phase 4: 70-score grace period during first 24h after strategy transition."""

    def _make_collector(self, tmp_path):
        c = StrategyCollector(base_path=str(tmp_path))
        state = tmp_path / "STATE" / "novatrade"
        state.mkdir(parents=True, exist_ok=True)
        bt_dir = tmp_path / "OUTPUT" / "backtests"
        bt_dir.mkdir(parents=True, exist_ok=True)
        return c, state, bt_dir

    def test_grace_70_during_first_24h(self, tmp_path):
        c, state, bt_dir = self._make_collector(tmp_path)
        (bt_dir / "test.json").write_text(json.dumps({"sharpe": 0.15}))
        (state / "strategy_config.json").write_text(json.dumps({"started_at": time.time() - 3600}))

        with patch("novatrade.autonomy.collectors.strategy.datetime") as mock_dt:
            mock_now = type(
                "MockNow",
                (),
                {
                    "weekday": lambda self: 2,
                    "hour": 14,
                },
            )()
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: __import__("datetime").datetime(*a, **kw)

            score, delta = c._check_backtest_alignment()
        assert score == 70.0
        assert delta == -6.0

    def test_warmup_65_during_24_to_72h(self, tmp_path):
        """Phase 1 (0835): 24-72h warmup returns 65 — Sharpe unreliable."""
        c, state, bt_dir = self._make_collector(tmp_path)
        (bt_dir / "test.json").write_text(json.dumps({"sharpe": 0.15}))
        (state / "strategy_config.json").write_text(json.dumps({"started_at": time.time() - 48 * 3600}))

        with patch("novatrade.autonomy.collectors.strategy.datetime") as mock_dt:
            mock_now = type(
                "MockNow",
                (),
                {
                    "weekday": lambda self: 2,
                    "hour": 14,
                },
            )()
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: __import__("datetime").datetime(*a, **kw)

            score, delta = c._check_backtest_alignment()
        assert score == 65.0
        assert delta == -7.0

    def test_no_grace_after_72h(self, tmp_path):
        """After 72h warmup, normal scoring resumes."""
        c, state, bt_dir = self._make_collector(tmp_path)
        (bt_dir / "test.json").write_text(json.dumps({"sharpe": 0.15}))
        (state / "strategy_config.json").write_text(json.dumps({"started_at": time.time() - 96 * 3600}))

        with patch("novatrade.autonomy.collectors.strategy.datetime") as mock_dt:
            mock_now = type(
                "MockNow",
                (),
                {
                    "weekday": lambda self: 2,
                    "hour": 14,
                },
            )()
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: __import__("datetime").datetime(*a, **kw)

            score, delta = c._check_backtest_alignment()
        assert score == 60.0
        assert delta == -2.0
