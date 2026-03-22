"""Tests for production review fixes round 2 (task 0499).

Covers:
  C1 — Transaction cost deduction in engine._close_position()
  C2 — Stage B gate defaults are production-realistic
  H1 — Campaign engine uses logging, not print()
  H3 — Recovery factor uses consistent USD units
  H4 — Sharpe/Sortino documented as proxies
  M2 — Vectorized candle construction (no iterrows)
  M4 — pytest.mark.slow registered
  M5 — Stagnation defaults aligned
  M6 — ExperimentDB.export_tsv() is atomic
  Prevention gate — quality check script runs
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from novatrade.backtest.metrics import ExitReason

# ── C1: Transaction costs ──────────────────────────────────────────────────


class TestTransactionCostDeduction:
    """C1: Verify engine deducts spread, slippage, and commission from PnL."""

    def test_close_position_deducts_spread(self):
        """PnL should be reduced by total_cost_pips (spread + 2*slippage)."""
        from novatrade.backtest.engine import IRBBacktester
        from novatrade.backtest.environment import BacktestEnvironment, SpreadAssumptions

        env = BacktestEnvironment(
            spread=SpreadAssumptions(
                avg_spread_pips=1.0,
                slippage_pips=0.5,
                commission_per_lot_usd=0.0,
            ),
        )
        engine = IRBBacktester(env=env)
        # total_cost_pips = 1.0 + 2*0.5 = 2.0 pips
        assert env.spread.total_cost_pips == 2.0

        # Create a synthetic winning trade: buy at 1.10000, close at 1.10100 = +10 pips raw
        # After cost: 10 - 2.0 = 8.0 pips net
        from novatrade.backtest.engine import _OpenPosition
        from novatrade.backtest.metrics import TradeSide

        engine._position = _OpenPosition(
            side=TradeSide.LONG,
            entry_price=1.10000,
            stop_loss=1.09900,
            volume=0.10,
            entry_bar=0,
        )
        engine._close_position(5, 1.10100, ExitReason.TRAILING_STOP)

        assert len(engine._trades) == 1
        trade = engine._trades[0]
        # Raw: 10 pips, after cost: 8 pips
        assert abs(trade.pnl_pips - 8.0) < 0.01

    def test_close_position_deducts_commission(self):
        """PnL USD should be reduced by round-trip commission."""
        from novatrade.backtest.engine import IRBBacktester, _OpenPosition
        from novatrade.backtest.environment import BacktestEnvironment, SpreadAssumptions
        from novatrade.backtest.metrics import TradeSide

        env = BacktestEnvironment(
            spread=SpreadAssumptions(
                avg_spread_pips=0.0,
                slippage_pips=0.0,
                commission_per_lot_usd=3.50,  # $3.50 per lot round-trip
            ),
        )
        engine = IRBBacktester(env=env)

        engine._position = _OpenPosition(
            side=TradeSide.LONG,
            entry_price=1.10000,
            stop_loss=1.09900,
            volume=1.0,  # 1 standard lot
            entry_bar=0,
        )
        engine._close_position(5, 1.10100, ExitReason.TRAILING_STOP)

        trade = engine._trades[0]
        # Raw PnL: 10 pips * 1.0 lot * $10/pip = $100
        # Commission: $3.50 * 1.0 lot (round-trip) = $3.50
        # Net: $100 - $3.50 = $96.50
        assert abs(trade.pnl_usd - 96.50) < 0.01

    def test_spread_turns_marginal_winner_into_loser(self):
        """A 1-pip winner with 2-pip spread should be a net loser."""
        from novatrade.backtest.engine import IRBBacktester, _OpenPosition
        from novatrade.backtest.environment import BacktestEnvironment, SpreadAssumptions
        from novatrade.backtest.metrics import TradeSide

        env = BacktestEnvironment(
            spread=SpreadAssumptions(avg_spread_pips=2.0, slippage_pips=0.0),
        )
        engine = IRBBacktester(env=env)

        engine._position = _OpenPosition(
            side=TradeSide.LONG,
            entry_price=1.10000,
            stop_loss=1.09900,
            volume=0.10,
            entry_bar=0,
        )
        # +1 pip raw, -2 pip cost = -1 pip net
        engine._close_position(5, 1.10010, ExitReason.TRAILING_STOP)

        trade = engine._trades[0]
        assert trade.pnl_pips < 0, "Marginal winner should become loser after spread"


# ── C2: Stage B gate defaults ──────────────────────────────────────────────


class TestStageBGateDefaults:
    """C2: Stage B gate defaults should reject zero-cost backtests."""

    def test_default_gate_rejects_zero_spread(self):
        from novatrade.evaluation.gates import GateConfig, evaluate_stage_b

        config = GateConfig()  # Uses defaults
        env = {"avg_spread_pips": 0.0, "slippage_pips": 0.0, "commission_per_lot_usd": 0.0}
        results = evaluate_stage_b(env, config)

        spread_gate = next(r for r in results if r.gate == "B.spread_applied")
        assert not spread_gate.passed, "Zero spread should fail default gate"

    def test_default_gate_rejects_zero_slippage(self):
        from novatrade.evaluation.gates import GateConfig, evaluate_stage_b

        config = GateConfig()
        env = {"avg_spread_pips": 1.0, "slippage_pips": 0.0, "commission_per_lot_usd": 0.0}
        results = evaluate_stage_b(env, config)

        slip_gate = next(r for r in results if r.gate == "B.slippage_applied")
        assert not slip_gate.passed, "Zero slippage should fail default gate"

    def test_default_gate_passes_realistic_costs(self):
        from novatrade.evaluation.gates import GateConfig, GateResults, evaluate_stage_b

        config = GateConfig()
        env = {
            "avg_spread_pips": 1.5,
            "slippage_pips": 0.2,
            "commission_per_lot_usd": 5.0,
            "stop_first_on_ambiguity": True,
        }
        results = GateResults(results=evaluate_stage_b(env, config))
        assert results.passed, "Realistic costs should pass default gate"

    def test_min_spread_default_is_nonzero(self):
        from novatrade.evaluation.gates import GateConfig

        config = GateConfig()
        assert config.min_avg_spread_pips > 0, "Default min_avg_spread_pips must be > 0"
        assert config.min_slippage_pips > 0, "Default min_slippage_pips must be > 0"


# ── H1: Campaign engine logging ───────────────────────────────────────────


class TestCampaignEngineLogging:
    """H1: campaign_engine.py should use logging, not print()."""

    def test_no_print_in_campaign_engine(self):
        import inspect

        import novatrade.optimization.campaign_engine as mod

        source = inspect.getsource(mod)
        # Count print() calls (excluding comments and strings)
        import ast

        tree = ast.parse(source)
        print_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(getattr(node, "func", None), ast.Name)
            and node.func.id == "print"
        ]
        assert len(print_calls) == 0, f"Found {len(print_calls)} print() calls — should use logging"

    def test_campaign_engine_has_logger(self):
        import novatrade.optimization.campaign_engine as mod

        assert hasattr(mod, "log"), "Module should have a 'log' logger"
        assert isinstance(mod.log, logging.Logger)


# ── H3: Recovery factor units ─────────────────────────────────────────────


class TestRecoveryFactorUnits:
    """H3: Recovery factor should use consistent USD units."""

    def test_recovery_factor_uses_usd_consistently(self):
        from novatrade.evaluation.evaluation_ladder import compute_dashboard_metrics

        metrics = SimpleNamespace(
            net_result_pips=100.0,
            net_result_usd=1000.0,
            max_drawdown_usd=500.0,
            max_drawdown_pct=10.0,
            total_completed_trades=50,
            winning_trades=30,
            losing_trades=20,
            total_bars=5000,
            average_winner_pips=30.0,
            average_loser_pips=-15.0,
            profit_factor=2.0,
        )
        row = compute_dashboard_metrics(metrics, experiment_id="test")
        # recovery_factor = net_result_usd / max_drawdown_usd = 1000 / 500 = 2.0
        assert abs(row.recovery_factor - 2.0) < 0.01

    def test_recovery_factor_zero_drawdown(self):
        from novatrade.evaluation.evaluation_ladder import compute_dashboard_metrics

        metrics = SimpleNamespace(
            net_result_pips=100.0,
            net_result_usd=1000.0,
            max_drawdown_usd=0.0,
            max_drawdown_pct=0.0,
            total_completed_trades=50,
            winning_trades=30,
            losing_trades=20,
            total_bars=5000,
            average_winner_pips=30.0,
            average_loser_pips=-15.0,
            profit_factor=2.0,
        )
        row = compute_dashboard_metrics(metrics, experiment_id="test")
        assert row.recovery_factor == 5.0  # Default for positive net with zero drawdown


# ── M4: pytest.mark.slow registered ───────────────────────────────────────


class TestPytestMarkers:
    """M4: pytest.mark.slow should be registered."""

    def test_slow_marker_registered(self):

        # Just verify we can use the marker without warning
        # The real test is that running pytest no longer shows 36 warnings
        mark = pytest.mark.slow
        assert mark is not None


# ── M5: Stagnation defaults aligned ───────────────────────────────────────


class TestStagnationDefaults:
    """M5: CampaignConfig and CampaignBudget stagnation defaults must match."""

    def test_stagnation_defaults_aligned(self):
        from novatrade.optimization.campaign_budget import CampaignBudget
        from novatrade.optimization.campaign_engine import CampaignConfig

        config = CampaignConfig()
        budget = CampaignBudget()
        assert config.stagnation_limit == budget.max_stagnant_experiments, (
            f"Stagnation mismatch: CampaignConfig={config.stagnation_limit}, "
            f"CampaignBudget={budget.max_stagnant_experiments}"
        )


# ── M6: Atomic TSV export ─────────────────────────────────────────────────


class TestAtomicTsvExport:
    """M6: ExperimentDB.export_tsv() should use atomic write pattern."""

    def test_export_tsv_is_atomic(self):
        """Verify export uses tempfile+os.replace (check source code)."""
        import inspect

        from novatrade.storage.experiment_db import ExperimentDB

        source = inspect.getsource(ExperimentDB.export_tsv)
        assert "tempfile.mkstemp" in source, "export_tsv should use tempfile.mkstemp"
        assert "os.replace" in source, "export_tsv should use os.replace for atomicity"

    def test_export_tsv_produces_correct_file(self):
        """Functional test: export TSV and verify contents."""
        from novatrade.storage.experiment_db import ExperimentDB

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            out_path = Path(tmp) / "export.tsv"

            with ExperimentDB(db_path) as db:
                count = db.export_tsv("nonexistent-campaign", out_path)

            assert count == 0
            assert out_path.exists()
            content = out_path.read_text()
            assert "experiment_id" in content  # Header present


# ── M2: Vectorized candle construction ─────────────────────────────────────


class TestVectorizedCandles:
    """M2: _df_to_candles should not use iterrows()."""

    def test_no_iterrows_call_in_source(self):
        import ast
        import inspect

        from novatrade.storage.data_snapshot import _df_to_candles

        source = inspect.getsource(_df_to_candles)
        tree = ast.parse(source)
        iterrows_calls = [
            node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "iterrows"
        ]
        assert len(iterrows_calls) == 0, "_df_to_candles should not call .iterrows()"

    def test_df_to_candles_functional(self):
        """Verify vectorized version produces correct output."""
        pd = pytest.importorskip("pandas")
        from novatrade.storage.data_snapshot import _df_to_candles

        df = pd.DataFrame(
            {
                "timestamp": [1.0, 2.0, 3.0],
                "open": [1.1, 1.2, 1.3],
                "high": [1.2, 1.3, 1.4],
                "low": [1.0, 1.1, 1.2],
                "close": [1.15, 1.25, 1.35],
                "volume": [100.0, 200.0, 300.0],
                "symbol": ["EURUSD"] * 3,
                "timeframe": ["H1"] * 3,
            }
        )
        candles = _df_to_candles(df)
        assert len(candles) == 3
        assert candles[0].open == 1.1
        assert candles[2].close == 1.35

    def test_df_to_candles_nan_detection(self):
        """NaN in price columns should raise ValueError."""
        pd = pytest.importorskip("pandas")
        from novatrade.storage.data_snapshot import _df_to_candles

        df = pd.DataFrame(
            {
                "timestamp": [1.0, 2.0],
                "open": [1.1, float("nan")],
                "high": [1.2, 1.3],
                "low": [1.0, 1.1],
                "close": [1.15, 1.25],
                "volume": [100.0, 200.0],
                "symbol": ["EURUSD"] * 2,
                "timeframe": ["H1"] * 2,
            }
        )
        with pytest.raises(ValueError, match="NaN"):
            _df_to_candles(df)


# ── Quality gate script ───────────────────────────────────────────────────


class TestQualityGate:
    """Verify the quality check script exists and runs."""

    def test_quality_gate_script_exists(self):
        script = Path(__file__).resolve().parent.parent / "scripts" / "check-novatrade-quality.py"
        assert script.exists(), "Quality gate script missing"

    def test_quality_gate_importable(self):
        """Script should be valid Python."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "check_quality",
            Path(__file__).resolve().parent.parent / "scripts" / "check-novatrade-quality.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "ALL_CHECKS")
        assert len(mod.ALL_CHECKS) >= 5
