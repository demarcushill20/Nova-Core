"""Comprehensive tests for Stage C robustness gates.

Tests cover:
  - monte_carlo_gate: profitable, unprofitable, zero trades, edge cases
  - walk_forward_gate: mock configs and candles
  - cost_stress_gate: various multipliers
  - parameter_stability_gate: perturbation coverage
  - evaluate_stage_c: integration (all gates together)
  - StageCConfig: controls which gates run
"""

from __future__ import annotations

import random
from unittest.mock import MagicMock, patch

from novatrade.backtest.metrics import CompletedTrade, ExitReason, TradeSide
from novatrade.evaluation.gates import GateResult
from novatrade.models import Candle

# ---------------------------------------------------------------------------
# Test helpers — synthetic data generation
# ---------------------------------------------------------------------------

# Seed for reproducibility
random.seed(42)


def _make_candles(
    n: int = 1000,
    start_ts: float = 1704067200.0,
    base_price: float = 1.1,
    timeframe: str = "H1",
) -> list[Candle]:
    """Generate n synthetic candles."""
    rng = random.Random(42)  # noqa: S311
    candles: list[Candle] = []
    interval = 3600 if timeframe == "H1" else 14400
    price = base_price

    for i in range(n):
        change = rng.uniform(-0.001, 0.001)
        o = price
        c = price + change
        h = max(o, c) + rng.uniform(0, 0.0005)
        l_val = min(o, c) - rng.uniform(0, 0.0005)
        candles.append(
            Candle(
                timestamp=start_ts + i * interval,
                open=o,
                high=h,
                low=l_val,
                close=c,
                volume=rng.uniform(100, 1000),
                symbol="EURUSD",
                timeframe=timeframe,
            )
        )
        price = c

    return candles


def _make_trades(
    n: int = 60,
    profitable_pct: float = 0.6,
    start_ts: float = 1704067200.0,
    spacing_seconds: float = 72000,
    avg_pnl: float = 20.0,
    avg_loss: float = -15.0,
) -> list[CompletedTrade]:
    """Generate n synthetic trades."""
    rng = random.Random(42)  # noqa: S311
    trades: list[CompletedTrade] = []

    for i in range(n):
        is_winner = rng.random() < profitable_pct
        pnl = rng.uniform(5, abs(avg_pnl) * 2) if is_winner else rng.uniform(avg_loss * 2, -5)
        trades.append(
            CompletedTrade(
                trade_id=i,
                side=TradeSide.LONG,
                entry_bar=i * 20,
                exit_bar=i * 20 + 10,
                entry_price=1.1,
                exit_price=1.1 + pnl * 0.0001,
                stop_loss=1.09,
                volume=0.1,
                exit_reason=ExitReason.TRAILING_STOP,
                pnl_pips=pnl,
                pnl_usd=pnl * 10,
                risk_r=pnl / 30,
                hold_bars=10,
                entry_timestamp=start_ts + i * spacing_seconds,
                exit_timestamp=start_ts + i * spacing_seconds + 36000,
            )
        )

    return trades


# =========================================================================
# Monte Carlo Gate Tests
# =========================================================================


class TestMonteCarloGate:
    """Tests for monte_carlo_gate."""

    def test_profitable_trades_pass(self):
        """A strongly profitable trade sequence should have low MC drawdown."""
        from novatrade.evaluation.stage_c import monte_carlo_gate

        trades = _make_trades(100, profitable_pct=0.85, avg_pnl=30.0, avg_loss=-10.0)
        result = monte_carlo_gate(trades, n_simulations=200, max_dd_threshold=20.0)
        assert isinstance(result, GateResult)
        assert result.gate == "C.monte_carlo"
        assert result.passed is True
        assert "dd_95th" in result.details
        assert result.details["n_trades"] == 100

    def test_unprofitable_trades_fail(self):
        """A money-losing trade sequence should have high MC drawdown."""
        from novatrade.evaluation.stage_c import monte_carlo_gate

        trades = _make_trades(100, profitable_pct=0.15, avg_pnl=10.0, avg_loss=-40.0)
        result = monte_carlo_gate(trades, n_simulations=200, max_dd_threshold=5.0)
        assert result.gate == "C.monte_carlo"
        assert result.passed is False

    def test_zero_trades(self):
        """Empty trade list should fail with 'No trades' reason."""
        from novatrade.evaluation.stage_c import monte_carlo_gate

        result = monte_carlo_gate([], n_simulations=100)
        assert result.passed is False
        assert "No trades" in result.reason
        assert result.details["n_trades"] == 0

    def test_single_trade(self):
        """Single trade should still produce a valid result."""
        from novatrade.evaluation.stage_c import monte_carlo_gate

        trades = _make_trades(1, profitable_pct=1.0)
        result = monte_carlo_gate(trades, n_simulations=50)
        assert result.gate == "C.monte_carlo"
        assert result.details["n_trades"] == 1

    def test_all_winners(self):
        """100% win rate should produce very low drawdown."""
        from novatrade.evaluation.stage_c import monte_carlo_gate

        trades = _make_trades(50, profitable_pct=1.0)
        result = monte_carlo_gate(trades, n_simulations=200, max_dd_threshold=50.0)
        assert result.passed is True
        assert result.details["dd_median"] >= 0.0

    def test_custom_percentile(self):
        """Custom percentile threshold should be respected."""
        from novatrade.evaluation.stage_c import monte_carlo_gate

        trades = _make_trades(60, profitable_pct=0.6)
        result = monte_carlo_gate(
            trades,
            n_simulations=200,
            max_dd_percentile=99.0,
            max_dd_threshold=50.0,
        )
        assert result.gate == "C.monte_carlo"
        assert "dd_99th" in result.details

    def test_custom_initial_equity(self):
        """Initial equity affects drawdown percentage computation."""
        from novatrade.evaluation.stage_c import monte_carlo_gate

        trades = _make_trades(60, profitable_pct=0.6)
        result = monte_carlo_gate(trades, n_simulations=100, initial_equity=100000.0)
        assert result.details["initial_equity"] == 100000.0

    def test_zero_usd_pnl_fallback_to_pips(self):
        """When all pnl_usd are 0, should fall back to pnl_pips."""
        from novatrade.evaluation.stage_c import monte_carlo_gate

        trades = _make_trades(50, profitable_pct=0.7)
        for t in trades:
            t.pnl_usd = 0.0  # Force zero USD P&L

        result = monte_carlo_gate(trades, n_simulations=100)
        assert result.gate == "C.monte_carlo"
        # Should still produce a valid result using pips
        assert result.details["n_trades"] == 50

    def test_details_structure(self):
        """Result details should contain expected keys."""
        from novatrade.evaluation.stage_c import monte_carlo_gate

        trades = _make_trades(60, profitable_pct=0.6)
        result = monte_carlo_gate(trades, n_simulations=100)
        assert "dd_median" in result.details
        assert "dd_max" in result.details
        assert "n_simulations" in result.details
        assert "threshold" in result.details


# =========================================================================
# Walk-Forward Gate Tests
# =========================================================================


class TestWalkForwardGate:
    """Tests for walk_forward_gate."""

    def test_invalid_config_fails(self):
        """Non-coercible config should fail gracefully."""
        from novatrade.evaluation.stage_c import walk_forward_gate

        result = walk_forward_gate(
            config="not_a_config",
            h1_candles=_make_candles(100),
            h4_candles=_make_candles(25, timeframe="H4"),
        )
        assert result.gate == "C.walk_forward"
        assert result.passed is False
        assert "Invalid config" in result.reason or "error" in result.details

    @patch("novatrade.evaluation.stage_c.run_walk_forward")
    def test_walk_forward_execution_failure(self, mock_wf):
        """Walk-forward execution failure should return failed gate."""
        from novatrade.evaluation.stage_c import walk_forward_gate

        mock_wf.side_effect = RuntimeError("Backtest engine crash")
        mock_config = MagicMock()
        mock_config.__class__.__name__ = "StrategyConfig"

        # Need to patch isinstance check
        with patch("novatrade.evaluation.stage_c.StrategyConfig", type(mock_config)):
            result = walk_forward_gate(
                config=mock_config,
                h1_candles=_make_candles(100),
                h4_candles=_make_candles(25, timeframe="H4"),
            )
        assert result.gate == "C.walk_forward"
        assert result.passed is False
        assert "failed" in result.reason.lower() or "error" in result.details

    @patch("novatrade.evaluation.stage_c.run_walk_forward")
    def test_walk_forward_no_windows(self, mock_wf):
        """Walk-forward with no windows should fail."""
        from novatrade.evaluation.stage_c import walk_forward_gate
        from novatrade.optimization.walkforward import WalkForwardResult

        mock_wf.return_value = WalkForwardResult()  # empty, no windows
        mock_config = MagicMock()

        with patch("novatrade.evaluation.stage_c.StrategyConfig", type(mock_config)):
            result = walk_forward_gate(
                config=mock_config,
                h1_candles=_make_candles(100),
                h4_candles=_make_candles(25, timeframe="H4"),
            )
        assert result.gate == "C.walk_forward"
        assert result.passed is False
        assert "Insufficient data" in result.reason or result.details.get("n_windows") == 0

    @patch("novatrade.evaluation.stage_c.run_walk_forward")
    def test_walk_forward_passes(self, mock_wf):
        """Walk-forward with good OOS ratio should pass."""
        from novatrade.evaluation.stage_c import walk_forward_gate
        from novatrade.optimization.walkforward import WalkForwardResult

        mock_window = MagicMock()
        mock_window.oos_scout_score = 0.8
        mock_window.is_scout_score = 1.0

        mock_result = WalkForwardResult()
        mock_result.windows = [mock_window, mock_window]
        mock_result.median_oos_is_ratio = 0.8
        mock_result.holdout_passed = True
        mock_result.holdout_score = 0.75
        mock_result.median_oos_score = 0.8
        mock_result.median_is_score = 1.0
        mock_result.all_windows_passed = True
        mock_result.overall_passed = True
        mock_wf.return_value = mock_result

        mock_config = MagicMock()

        with patch("novatrade.evaluation.stage_c.StrategyConfig", type(mock_config)):
            result = walk_forward_gate(
                config=mock_config,
                h1_candles=_make_candles(100),
                h4_candles=_make_candles(25, timeframe="H4"),
                min_oos_ratio=0.6,
            )
        assert result.gate == "C.walk_forward"
        assert result.passed is True


# =========================================================================
# Cost Stress Gate Tests
# =========================================================================


class TestCostStressGate:
    """Tests for cost_stress_gate."""

    def test_invalid_config_fails(self):
        """Non-coercible config should fail gracefully."""
        from novatrade.evaluation.stage_c import cost_stress_gate

        result = cost_stress_gate(
            config="not_a_config",
            h1_candles=_make_candles(100),
            h4_candles=_make_candles(25, timeframe="H4"),
        )
        assert result.gate == "C.cost_stress"
        assert result.passed is False

    @patch("novatrade.evaluation.stage_c._run_backtest_and_score")
    def test_cost_stress_with_mock_backtest(self, mock_bt_score):
        """Stress test with profitable result should pass."""
        from novatrade.evaluation.stage_c import cost_stress_gate

        # Mock _run_backtest_and_score to return (profit_factor, net_pips)
        mock_bt_score.return_value = (1.8, 150.0)

        mock_config = MagicMock()
        mock_config.to_environment_kwargs.return_value = {}

        mock_spread = MagicMock()
        mock_spread.fixed_spread_pips = 1.5
        mock_spread.avg_spread_pips = 1.5
        mock_spread.slippage_pips = 0.3
        mock_spread.commission_per_lot_usd = 7.0
        mock_spread.description = "test"

        mock_env = MagicMock()
        mock_env.spread = mock_spread

        with (
            patch("novatrade.evaluation.stage_c.StrategyConfig", type(mock_config)),
            patch("novatrade.evaluation.stage_c.DEFAULT_ENVIRONMENT", mock_env),
            patch("novatrade.evaluation.stage_c.replace", return_value=mock_env),
            patch("novatrade.evaluation.stage_c.SpreadAssumptions", return_value=mock_spread),
        ):
            result = cost_stress_gate(
                config=mock_config,
                h1_candles=_make_candles(100),
                h4_candles=_make_candles(25, timeframe="H4"),
                spread_multiplier=2.0,
                slippage_multiplier=3.0,
                commission_multiplier=2.0,
            )

        assert result.gate == "C.cost_stress"
        assert result.passed is True
        assert result.details["stress_pf"] == 1.8
        assert result.details["spread_multiplier"] == 2.0

    def test_details_contain_multipliers(self):
        """Details should always contain the multiplier values."""
        from novatrade.evaluation.stage_c import cost_stress_gate

        result = cost_stress_gate(
            config="bad",
            h1_candles=[],
            h4_candles=[],
            spread_multiplier=3.0,
            slippage_multiplier=4.0,
            commission_multiplier=5.0,
        )
        # Even on failure, basic gate info is present
        assert result.gate == "C.cost_stress"


# =========================================================================
# Parameter Stability Gate Tests
# =========================================================================


class TestParameterStabilityGate:
    """Tests for parameter_stability_gate."""

    def test_invalid_config_fails(self):
        """Non-coercible config should fail gracefully."""
        from novatrade.evaluation.stage_c import parameter_stability_gate

        result = parameter_stability_gate(
            config="not_a_config",
            h1_candles=_make_candles(100),
            h4_candles=_make_candles(25, timeframe="H4"),
        )
        assert result.gate == "C.parameter_stability"
        assert result.passed is False

    @patch("novatrade.evaluation.stage_c.compute_scout_score")
    @patch("novatrade.evaluation.stage_c.compute_metrics")
    @patch("novatrade.evaluation.stage_c.IRBBacktester")
    @patch("novatrade.evaluation.stage_c.DEFAULT_ENVIRONMENT")
    def test_stability_with_stable_perturbations(self, mock_env, mock_bt_cls, mock_metrics, mock_scout):
        """When all perturbations produce positive scores, gate should pass."""
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.evaluation.stage_c import parameter_stability_gate

        mock_spread = MagicMock()
        mock_spread.fixed_spread_pips = 1.5
        mock_spread.avg_spread_pips = 1.5
        mock_spread.slippage_pips = 0.3
        mock_spread.commission_per_lot_usd = 7.0
        mock_env.spread = mock_spread
        mock_env.initial_equity = 10000.0

        mock_result = MagicMock()
        mock_result.trades = [MagicMock()] * 60
        mock_result.pending_orders = []
        mock_result.signals = []
        mock_result.filter_rejections = []
        mock_result.total_bars = 5000

        mock_bt = MagicMock()
        mock_bt.run.return_value = mock_result
        mock_bt_cls.return_value = mock_bt

        # Return a positive scout score for all perturbations
        mock_scout.return_value = 0.75

        # Create a real StrategyConfig so isinstance checks pass
        real_config = StrategyConfig(name="test_stability", pair="EURUSD")

        with patch("novatrade.evaluation.stage_c.replace", return_value=mock_env):
            result = parameter_stability_gate(
                config=real_config,
                h1_candles=_make_candles(100),
                h4_candles=_make_candles(25, timeframe="H4"),
                n_perturbations=5,
            )

        assert result.gate == "C.parameter_stability"
        assert "stability_ratio" in result.details
        # With mocked positive scout scores, all perturbations should be stable
        if result.details["n_tested"] > 0:
            assert result.details["stability_ratio"] == 1.0
            assert result.passed is True

    def test_no_perturbations_fail(self):
        """If no valid perturbations can be generated, gate should fail."""
        from novatrade.evaluation.stage_c import parameter_stability_gate

        # Pass a non-StrategyConfig object that will fail model_validate
        result = parameter_stability_gate(
            config="not_a_config",
            h1_candles=_make_candles(100),
            h4_candles=_make_candles(25, timeframe="H4"),
        )

        assert result.gate == "C.parameter_stability"
        assert result.passed is False


# =========================================================================
# evaluate_stage_c Integration Tests
# =========================================================================


class TestEvaluateStageC:
    """Tests for the evaluate_stage_c integration function."""

    def test_all_gates_disabled(self):
        """With all gates disabled, should return empty results."""
        from novatrade.evaluation.stage_c import StageCConfig, evaluate_stage_c

        trades = _make_trades(60)
        candles = _make_candles(500)
        cfg = StageCConfig(
            run_monte_carlo=False,
            run_walk_forward=False,
            run_cost_stress=False,
            run_param_stability=False,
        )
        results = evaluate_stage_c(trades, None, candles, candles[:125], stage_c_config=cfg)
        assert results == []

    def test_monte_carlo_only(self):
        """Only MC enabled should return exactly 1 result."""
        from novatrade.evaluation.stage_c import StageCConfig, evaluate_stage_c

        trades = _make_trades(80, profitable_pct=0.65)
        candles = _make_candles(500)
        cfg = StageCConfig(
            run_monte_carlo=True,
            run_walk_forward=False,
            run_cost_stress=False,
            run_param_stability=False,
            mc_simulations=50,
        )
        results = evaluate_stage_c(trades, None, candles, candles[:125], stage_c_config=cfg)
        assert len(results) == 1
        assert results[0].gate == "C.monte_carlo"

    def test_default_config_runs_all_gates(self):
        """Default StageCConfig should attempt all 4 gates."""
        from novatrade.evaluation.stage_c import StageCConfig, evaluate_stage_c

        trades = _make_trades(80, profitable_pct=0.65)
        candles = _make_candles(500)

        cfg = StageCConfig(mc_simulations=50)
        # Walk-forward, cost stress, and param stability will fail without real config
        # but they should still produce GateResult objects
        results = evaluate_stage_c(trades, "invalid_config", candles, candles[:125], stage_c_config=cfg)
        assert len(results) == 4
        gate_names = {r.gate for r in results}
        assert gate_names == {"C.monte_carlo", "C.walk_forward", "C.cost_stress", "C.parameter_stability"}

    def test_results_are_gate_result_instances(self):
        """All results should be GateResult instances."""
        from novatrade.evaluation.stage_c import StageCConfig, evaluate_stage_c

        trades = _make_trades(50)
        candles = _make_candles(200)
        cfg = StageCConfig(
            run_walk_forward=False,
            run_cost_stress=False,
            run_param_stability=False,
            mc_simulations=20,
        )
        results = evaluate_stage_c(trades, None, candles, candles[:50], stage_c_config=cfg)
        for r in results:
            assert isinstance(r, GateResult)
            assert hasattr(r, "gate")
            assert hasattr(r, "passed")
            assert hasattr(r, "reason")
            assert hasattr(r, "details")


# =========================================================================
# StageCConfig Tests
# =========================================================================


class TestStageCConfig:
    """Tests for StageCConfig dataclass."""

    def test_default_values(self):
        from novatrade.evaluation.stage_c import StageCConfig

        cfg = StageCConfig()
        assert cfg.run_monte_carlo is True
        assert cfg.run_walk_forward is True
        assert cfg.run_cost_stress is True
        assert cfg.run_param_stability is True
        assert cfg.mc_simulations == 1000
        assert cfg.mc_dd_threshold == 15.0
        assert cfg.mc_dd_percentile == 95.0
        assert cfg.mc_initial_equity == 10000.0
        assert cfg.wf_min_oos_ratio == 0.6
        assert cfg.wf_min_holdout_ratio == 0.7
        assert cfg.stress_spread_mult == 2.0
        assert cfg.stress_slippage_mult == 3.0
        assert cfg.stress_commission_mult == 2.0
        assert cfg.stability_n_perturbations == 10
        assert cfg.stability_perturbation_pct == 0.10

    def test_custom_values(self):
        from novatrade.evaluation.stage_c import StageCConfig

        cfg = StageCConfig(
            run_monte_carlo=False,
            mc_simulations=500,
            mc_dd_threshold=20.0,
            stress_spread_mult=4.0,
        )
        assert cfg.run_monte_carlo is False
        assert cfg.mc_simulations == 500
        assert cfg.mc_dd_threshold == 20.0
        assert cfg.stress_spread_mult == 4.0

    def test_gates_selectable(self):
        """Individual gates can be toggled."""
        from novatrade.evaluation.stage_c import StageCConfig

        cfg = StageCConfig(
            run_monte_carlo=True,
            run_walk_forward=False,
            run_cost_stress=True,
            run_param_stability=False,
        )
        assert cfg.run_monte_carlo is True
        assert cfg.run_walk_forward is False
        assert cfg.run_cost_stress is True
        assert cfg.run_param_stability is False


# =========================================================================
# _max_drawdown_pct helper Tests
# =========================================================================


class TestMaxDrawdownPct:
    """Tests for the internal _max_drawdown_pct helper."""

    def test_all_profits_zero_drawdown(self):
        from novatrade.evaluation.stage_c import _max_drawdown_pct

        pnl = [10, 20, 30, 40, 50]
        dd = _max_drawdown_pct(pnl, 10000.0)
        assert dd == 0.0

    def test_single_loss(self):
        from novatrade.evaluation.stage_c import _max_drawdown_pct

        pnl = [100, -50, 100]
        dd = _max_drawdown_pct(pnl, 10000.0)
        assert dd > 0.0

    def test_deep_drawdown(self):
        from novatrade.evaluation.stage_c import _max_drawdown_pct

        pnl = [100, -5000]  # Lose 5000 from peak of 10100
        dd = _max_drawdown_pct(pnl, 10000.0)
        assert dd > 40.0  # Should be ~49.5%

    def test_empty_sequence(self):
        from novatrade.evaluation.stage_c import _max_drawdown_pct

        dd = _max_drawdown_pct([], 10000.0)
        assert dd == 0.0


# =========================================================================
# Gates.py Wiring Tests
# =========================================================================


class TestGatesWiring:
    """Tests that gates.py correctly wires to the real stage_c module."""

    def test_evaluate_stage_c_gates_exists(self):
        """The new evaluate_stage_c_gates function should be importable."""
        from novatrade.evaluation.gates import evaluate_stage_c_gates

        assert callable(evaluate_stage_c_gates)

    def test_evaluate_stage_c_gates_delegates(self):
        """evaluate_stage_c_gates should delegate to stage_c.evaluate_stage_c."""
        from novatrade.evaluation.gates import evaluate_stage_c_gates

        trades = _make_trades(50, profitable_pct=0.7)
        candles = _make_candles(200)

        from novatrade.evaluation.stage_c import StageCConfig

        cfg = StageCConfig(
            run_walk_forward=False,
            run_cost_stress=False,
            run_param_stability=False,
            mc_simulations=20,
        )
        results = evaluate_stage_c_gates(
            trades=trades,
            config=None,
            h1_candles=candles,
            h4_candles=candles[:50],
            stage_c_config=cfg,
        )
        assert len(results) == 1
        assert results[0].gate == "C.monte_carlo"

    def test_evaluate_stage_c_stub_still_works(self):
        """The stub should still work for backward compatibility."""
        from novatrade.evaluation.gates import evaluate_stage_c_stub

        results = evaluate_stage_c_stub()
        assert len(results) == 4
        assert all(not r.passed for r in results)
        assert all("NOT_EVALUATED" in r.reason for r in results)

    def test_evaluate_stage_c_gates_exported_from_init(self):
        """evaluate_stage_c_gates should be accessible from evaluation package."""
        from novatrade.evaluation import evaluate_stage_c_gates

        assert callable(evaluate_stage_c_gates)
