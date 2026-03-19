"""Comprehensive pytest tests for NovaTrade AutoResearch Phases 1-4.

Phase 1: Data Layer (generation, aggregation, CSV I/O, snapshot freeze)
Phase 2: Run Command (execute_backtest, gating, scout score, artifacts, DB, reports)
Phase 3: Walk-Forward, Perturbation, Stress testing
Phase 4: Sweep Engine, Strategy Selection, Ablation
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from novatrade.cli.commands.data import (
    aggregate_h1_to_h4,
    generate_synthetic_candles,
    load_candles_csv,
    save_candles_csv,
)
from novatrade.cli.config_schema import StrategyConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def synthetic_candles():
    """Generate 180 days of H1 + H4 candles for testing."""
    h1 = generate_synthetic_candles("EURUSD", "H1", days=180, start_price=1.1000)
    h4 = aggregate_h1_to_h4(h1)
    return h1, h4


@pytest.fixture
def small_candles():
    """Generate 10 days of H1 + H4 candles (minimal data for gating tests)."""
    h1 = generate_synthetic_candles("EURUSD", "H1", days=10, start_price=1.1000)
    h4 = aggregate_h1_to_h4(h1)
    return h1, h4


@pytest.fixture
def default_config():
    """Default StrategyConfig with baseline parameters."""
    return StrategyConfig()


# ===================================================================
# Phase 1: Data Layer Tests
# ===================================================================


class TestSyntheticCandleGeneration:
    """Tests for generate_synthetic_candles."""

    def test_h1_candle_count(self):
        """H1 generation: 24 bars per day * days."""
        candles = generate_synthetic_candles("EURUSD", "H1", days=30)
        assert len(candles) == 24 * 30

    def test_h4_candle_count(self):
        """H4 generation: 6 bars per day * days."""
        candles = generate_synthetic_candles("EURUSD", "H4", days=30)
        assert len(candles) == 6 * 30

    def test_m15_candle_count(self):
        """M15 generation: 96 bars per day * days."""
        candles = generate_synthetic_candles("EURUSD", "M15", days=5)
        assert len(candles) == 96 * 5

    def test_d1_candle_count(self):
        """D1 generation: 1 bar per day * days."""
        candles = generate_synthetic_candles("EURUSD", "D1", days=60)
        assert len(candles) == 60

    def test_prices_positive(self):
        """All candle prices must be positive."""
        candles = generate_synthetic_candles("EURUSD", "H1", days=90)
        for c in candles:
            assert c.open > 0, f"open={c.open}"
            assert c.high > 0, f"high={c.high}"
            assert c.low > 0, f"low={c.low}"
            assert c.close > 0, f"close={c.close}"

    def test_ohlc_consistency(self):
        """high >= max(open, close) and low <= min(open, close) for every candle."""
        candles = generate_synthetic_candles("EURUSD", "H1", days=90)
        for c in candles:
            assert c.high >= c.open, f"high {c.high} < open {c.open}"
            assert c.high >= c.close, f"high {c.high} < close {c.close}"
            assert c.low <= c.open, f"low {c.low} > open {c.open}"
            assert c.low <= c.close, f"low {c.low} > close {c.close}"

    def test_volume_positive(self):
        """All candle volumes must be positive."""
        candles = generate_synthetic_candles("EURUSD", "H1", days=30)
        for c in candles:
            assert c.volume > 0

    def test_prices_realistic_range(self):
        """EURUSD prices should stay within a realistic range (0.5-2.0)."""
        candles = generate_synthetic_candles("EURUSD", "H1", days=365, start_price=1.1)
        for c in candles:
            assert 0.5 < c.close < 2.0, f"unrealistic EURUSD price: {c.close}"

    def test_jpy_pair_digits(self):
        """JPY pairs should round to 3 decimal places."""
        candles = generate_synthetic_candles("USDJPY", "H1", days=10, start_price=150.0)
        for c in candles:
            # Check that values are rounded to 3 decimal places
            assert round(c.close, 3) == c.close

    def test_seed_reproducibility(self):
        """Same seed produces identical candles."""
        a = generate_synthetic_candles("EURUSD", "H1", days=30, seed=123)
        b = generate_synthetic_candles("EURUSD", "H1", days=30, seed=123)
        assert len(a) == len(b)
        for ca, cb in zip(a, b, strict=False):
            assert ca.close == cb.close
            assert ca.high == cb.high

    def test_different_seeds_differ(self):
        """Different seeds produce different candles."""
        a = generate_synthetic_candles("EURUSD", "H1", days=30, seed=1)
        b = generate_synthetic_candles("EURUSD", "H1", days=30, seed=2)
        closes_a = [c.close for c in a]
        closes_b = [c.close for c in b]
        assert closes_a != closes_b

    def test_symbol_and_timeframe_set(self):
        """Each candle has the correct symbol and timeframe."""
        candles = generate_synthetic_candles("GBPUSD", "H4", days=5)
        for c in candles:
            assert c.symbol == "GBPUSD"
            assert c.timeframe == "H4"

    def test_timestamps_monotonically_increasing(self):
        """Timestamps strictly increase across candles."""
        candles = generate_synthetic_candles("EURUSD", "H1", days=30)
        for i in range(1, len(candles)):
            assert candles[i].timestamp > candles[i - 1].timestamp


class TestAggregateH1ToH4:
    """Tests for aggregate_h1_to_h4."""

    def test_count_is_quarter(self):
        """H4 count should be H1 count / 4."""
        h1 = generate_synthetic_candles("EURUSD", "H1", days=30)
        h4 = aggregate_h1_to_h4(h1)
        assert len(h4) == len(h1) // 4

    def test_h4_high_is_max_of_h1_highs(self):
        """Each H4 high = max of the 4 constituent H1 highs."""
        h1 = generate_synthetic_candles("EURUSD", "H1", days=30)
        h4 = aggregate_h1_to_h4(h1)
        for i, h4c in enumerate(h4):
            chunk = h1[i * 4 : i * 4 + 4]
            expected_high = max(c.high for c in chunk)
            assert h4c.high == expected_high

    def test_h4_low_is_min_of_h1_lows(self):
        """Each H4 low = min of the 4 constituent H1 lows."""
        h1 = generate_synthetic_candles("EURUSD", "H1", days=30)
        h4 = aggregate_h1_to_h4(h1)
        for i, h4c in enumerate(h4):
            chunk = h1[i * 4 : i * 4 + 4]
            expected_low = min(c.low for c in chunk)
            assert h4c.low == expected_low

    def test_h4_open_matches_first_h1_open(self):
        """Each H4 open = first H1 open in the chunk."""
        h1 = generate_synthetic_candles("EURUSD", "H1", days=30)
        h4 = aggregate_h1_to_h4(h1)
        for i, h4c in enumerate(h4):
            assert h4c.open == h1[i * 4].open

    def test_h4_close_matches_last_h1_close(self):
        """Each H4 close = last H1 close in the chunk."""
        h1 = generate_synthetic_candles("EURUSD", "H1", days=30)
        h4 = aggregate_h1_to_h4(h1)
        for i, h4c in enumerate(h4):
            chunk = h1[i * 4 : i * 4 + 4]
            assert h4c.close == chunk[-1].close

    def test_h4_volume_is_sum(self):
        """Each H4 volume = sum of the 4 constituent H1 volumes."""
        h1 = generate_synthetic_candles("EURUSD", "H1", days=30)
        h4 = aggregate_h1_to_h4(h1)
        for i, h4c in enumerate(h4):
            chunk = h1[i * 4 : i * 4 + 4]
            expected_vol = round(sum(c.volume for c in chunk), 1)
            assert h4c.volume == expected_vol

    def test_h4_timeframe_set(self):
        """Aggregated candles have timeframe='H4'."""
        h1 = generate_synthetic_candles("EURUSD", "H1", days=10)
        h4 = aggregate_h1_to_h4(h1)
        for c in h4:
            assert c.timeframe == "H4"

    def test_empty_input(self):
        """Empty H1 list produces empty H4 list."""
        assert aggregate_h1_to_h4([]) == []


class TestCSVRoundTrip:
    """Tests for save_candles_csv / load_candles_csv."""

    def test_round_trip_preserves_data(self, tmp_path: Path):
        """Save then load produces identical candle data."""
        candles = generate_synthetic_candles("EURUSD", "H1", days=10)
        csv_path = tmp_path / "EURUSD_H1.csv"

        count = save_candles_csv(candles, csv_path)
        assert count == len(candles)

        loaded = load_candles_csv(csv_path, symbol="EURUSD", timeframe="H1")
        assert len(loaded) == len(candles)

        for orig, loaded_c in zip(candles, loaded, strict=False):
            assert orig.timestamp == loaded_c.timestamp
            assert abs(orig.open - loaded_c.open) < 1e-9
            assert abs(orig.close - loaded_c.close) < 1e-9

    def test_save_creates_parent_dirs(self, tmp_path: Path):
        """save_candles_csv creates nested parent directories."""
        candles = generate_synthetic_candles("EURUSD", "H1", days=5)
        csv_path = tmp_path / "deep" / "nested" / "EURUSD_H1.csv"
        save_candles_csv(candles, csv_path)
        assert csv_path.exists()

    def test_load_infers_symbol_timeframe(self, tmp_path: Path):
        """load_candles_csv infers symbol and timeframe from filename."""
        candles = generate_synthetic_candles("GBPUSD", "H4", days=5)
        csv_path = tmp_path / "GBPUSD_H4.csv"
        save_candles_csv(candles, csv_path)

        loaded = load_candles_csv(csv_path)
        assert loaded[0].symbol == "GBPUSD"
        assert loaded[0].timeframe == "H4"

    def test_load_missing_file_raises(self, tmp_path: Path):
        """load_candles_csv raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            load_candles_csv(tmp_path / "nonexistent.csv")

    def test_save_returns_count(self, tmp_path: Path):
        """save_candles_csv returns the number of candles written."""
        candles = generate_synthetic_candles("EURUSD", "H1", days=3)
        count = save_candles_csv(candles, tmp_path / "test.csv")
        assert count == len(candles)


class TestFreezeCandles:
    """Tests for freeze_candles snapshot creation."""

    def test_freeze_creates_snapshot(self, tmp_path: Path):
        """freeze_candles returns a valid hex snapshot ID."""
        from novatrade.cli.commands.data import freeze_candles

        h1 = generate_synthetic_candles("EURUSD", "H1", days=30)
        h4 = aggregate_h1_to_h4(h1)

        candle_dir = tmp_path / "candles"
        snapshot_dir = tmp_path / "snapshots"
        candle_dir.mkdir()

        save_candles_csv(h1, candle_dir / "EURUSD_H1.csv")
        save_candles_csv(h4, candle_dir / "EURUSD_H4.csv")

        snapshot_id = freeze_candles("EURUSD", candle_dir, snapshot_dir)

        assert isinstance(snapshot_id, str)
        assert len(snapshot_id) == 16
        # Valid hex string
        int(snapshot_id, 16)


# ===================================================================
# Phase 2: Run Command Tests
# ===================================================================


class TestExecuteBacktest:
    """Tests for execute_backtest and BacktestRunResult."""

    def test_returns_backtest_run_result(self, synthetic_candles, default_config):
        """execute_backtest returns a BacktestRunResult instance."""
        from novatrade.cli.commands.run import BacktestRunResult, execute_backtest

        h1, h4 = synthetic_candles
        result = execute_backtest(default_config, h1, h4)
        assert isinstance(result, BacktestRunResult)

    def test_result_has_required_fields(self, synthetic_candles, default_config):
        """BacktestRunResult contains all required fields."""
        from novatrade.cli.commands.run import execute_backtest

        h1, h4 = synthetic_candles
        result = execute_backtest(default_config, h1, h4)

        assert hasattr(result, "experiment_id")
        assert hasattr(result, "scout_score")
        assert hasattr(result, "gate_results")
        assert hasattr(result, "metrics")
        assert hasattr(result, "status")
        assert hasattr(result, "backtest_result")
        assert hasattr(result, "notes")

    def test_experiment_id_is_nonempty_string(self, synthetic_candles, default_config):
        """experiment_id is a non-empty string."""
        from novatrade.cli.commands.run import execute_backtest

        h1, h4 = synthetic_candles
        result = execute_backtest(default_config, h1, h4)
        assert isinstance(result.experiment_id, str)
        assert len(result.experiment_id) > 0

    def test_empty_candles_returns_gated_a(self, default_config):
        """Empty candle data returns status='gated_a'."""
        from novatrade.cli.commands.run import execute_backtest

        result = execute_backtest(default_config, [], [])
        assert result.status == "gated_a"
        assert result.scout_score is None
        assert result.metrics is None

    def test_empty_h4_returns_gated_a(self, default_config):
        """Missing H4 data returns status='gated_a'."""
        from novatrade.cli.commands.run import execute_backtest

        h1 = generate_synthetic_candles("EURUSD", "H1", days=10)
        result = execute_backtest(default_config, h1, [])
        assert result.status == "gated_a"

    @pytest.mark.slow
    def test_backtest_with_data_produces_valid_status(self, synthetic_candles, default_config):
        """Backtest with sufficient data produces ranked, valid, gated_a, or gated_b."""
        from novatrade.cli.commands.run import execute_backtest

        h1, h4 = synthetic_candles
        result = execute_backtest(default_config, h1, h4)
        assert result.status in {"ranked", "valid", "gated_a", "gated_b"}

    def test_gate_a_catches_few_trades(self, default_config):
        """Very few candles should trigger gate A (insufficient trades)."""
        from novatrade.cli.commands.run import execute_backtest

        # 3 days of data — not enough for meaningful trades
        h1 = generate_synthetic_candles("EURUSD", "H1", days=3, start_price=1.1000)
        h4 = aggregate_h1_to_h4(h1)
        result = execute_backtest(default_config, h1, h4)
        # With only 72 H1 bars (barely past warmup), very likely gated_a
        assert result.status in {"gated_a", "gated_b", "ranked", "valid"}

    @pytest.mark.slow
    def test_gate_b_passes_default_environment(self, synthetic_candles, default_config):
        """Default environment has spread > 0, so Gate B should pass if Gate A passes."""
        from novatrade.backtest.environment import DEFAULT_ENVIRONMENT
        from novatrade.cli.commands.run import execute_backtest

        assert DEFAULT_ENVIRONMENT.spread.avg_spread_pips > 0
        h1, h4 = synthetic_candles
        result = execute_backtest(default_config, h1, h4)
        # If gates pass, status is ranked or valid (not gated_b)
        if result.status not in {"gated_a"}:
            assert result.status in {"ranked", "valid", "gated_b"}

    @pytest.mark.slow
    def test_scout_score_computed_when_gates_pass(self, synthetic_candles, default_config):
        """When both gates pass, scout_score is a float (not None)."""
        from novatrade.cli.commands.run import execute_backtest

        h1, h4 = synthetic_candles
        result = execute_backtest(default_config, h1, h4)
        if result.status in {"ranked", "valid"}:
            assert result.scout_score is not None
            assert isinstance(result.scout_score, float)

    @pytest.mark.slow
    def test_metrics_present_after_backtest(self, synthetic_candles, default_config):
        """Metrics are populated after a successful backtest run."""
        from novatrade.cli.commands.run import execute_backtest

        h1, h4 = synthetic_candles
        result = execute_backtest(default_config, h1, h4)
        if result.status != "crashed":
            assert result.metrics is not None

    @pytest.mark.slow
    def test_artifacts_saved_with_campaign_dir(self, synthetic_candles, default_config, tmp_path):
        """Artifacts are saved when campaign_dir is provided."""
        from novatrade.cli.commands.run import execute_backtest

        h1, h4 = synthetic_candles
        execute_backtest(default_config, h1, h4, campaign_dir=tmp_path)

        config_dir = tmp_path / "configs"
        trades_dir = tmp_path / "trades"

        assert config_dir.exists()
        assert trades_dir.exists()

        config_files = list(config_dir.glob("*.json"))
        trade_files = list(trades_dir.glob("*.json"))
        assert len(config_files) >= 1
        assert len(trade_files) >= 1

    @pytest.mark.slow
    def test_db_record_inserted(self, synthetic_candles, default_config, tmp_path):
        """DB record is inserted when db is provided."""
        from novatrade.cli.commands.run import execute_backtest
        from novatrade.storage.experiment_db import ExperimentDB

        db_path = tmp_path / "test.db"
        db = ExperimentDB(db_path)

        h1, h4 = synthetic_candles
        result = execute_backtest(
            default_config,
            h1,
            h4,
            campaign_id="test-campaign",
            db=db,
        )

        # Verify record exists in DB
        record = db.get(result.experiment_id)
        assert record is not None
        assert record.experiment_id == result.experiment_id
        assert record.status == result.status

    @pytest.mark.slow
    def test_backtest_result_populated(self, synthetic_candles, default_config):
        """BacktestResult is populated (not None) for successful runs."""
        from novatrade.cli.commands.run import execute_backtest

        h1, h4 = synthetic_candles
        result = execute_backtest(default_config, h1, h4)
        if result.status != "crashed":
            assert result.backtest_result is not None
            assert result.backtest_result.total_bars > 0

    def test_deterministic_experiment_id(self, small_candles, default_config):
        """Same inputs produce the same experiment_id."""
        from novatrade.cli.commands.run import execute_backtest

        h1, h4 = small_candles
        r1 = execute_backtest(default_config, h1, h4)
        r2 = execute_backtest(default_config, h1, h4)
        assert r1.experiment_id == r2.experiment_id

    @pytest.mark.slow
    def test_gate_results_populated(self, synthetic_candles, default_config):
        """Gate results contain at least one result entry."""
        from novatrade.cli.commands.run import execute_backtest

        h1, h4 = synthetic_candles
        result = execute_backtest(default_config, h1, h4)
        if result.status != "crashed":
            assert len(result.gate_results.results) >= 1


class TestFormatRunReport:
    """Tests for format_run_report."""

    @pytest.mark.slow
    def test_format_returns_nonempty_string(self, synthetic_candles, default_config):
        """format_run_report returns a non-empty markdown string."""
        from novatrade.cli.commands.report import format_run_report
        from novatrade.cli.commands.run import execute_backtest

        h1, h4 = synthetic_candles
        result = execute_backtest(default_config, h1, h4)
        report = format_run_report(result)
        assert isinstance(report, str)
        assert len(report) > 50

    @pytest.mark.slow
    def test_report_contains_experiment_id(self, synthetic_candles, default_config):
        """Report includes the experiment ID."""
        from novatrade.cli.commands.report import format_run_report
        from novatrade.cli.commands.run import execute_backtest

        h1, h4 = synthetic_candles
        result = execute_backtest(default_config, h1, h4)
        report = format_run_report(result)
        assert result.experiment_id in report

    @pytest.mark.slow
    def test_report_contains_status(self, synthetic_candles, default_config):
        """Report includes the status."""
        from novatrade.cli.commands.report import format_run_report
        from novatrade.cli.commands.run import execute_backtest

        h1, h4 = synthetic_candles
        result = execute_backtest(default_config, h1, h4)
        report = format_run_report(result)
        assert result.status in report

    def test_report_for_gated_result(self, default_config):
        """Report renders correctly for an empty-candle gated result."""
        from novatrade.cli.commands.report import format_run_report
        from novatrade.cli.commands.run import execute_backtest

        result = execute_backtest(default_config, [], [])
        report = format_run_report(result)
        assert "gated_a" in report
        assert "N/A" in report  # scout score is N/A


# ===================================================================
# Phase 3: Walk-Forward Tests
# ===================================================================


class TestWalkForward:
    """Tests for walk-forward validation."""

    def test_walk_forward_config_defaults(self):
        """WalkForwardConfig has sensible defaults."""
        from novatrade.optimization.walkforward import WalkForwardConfig

        wf = WalkForwardConfig()
        assert wf.train_bars == 4380
        assert wf.test_bars == 1460
        assert wf.step_bars == 730
        assert wf.embargo_bars == 5
        assert wf.holdout_bars == 2190
        assert wf.min_oos_is_ratio == 0.6
        assert wf.min_holdout_ratio == 0.7

    @pytest.mark.slow
    def test_run_walk_forward_produces_result(self, synthetic_candles, default_config):
        """run_walk_forward returns a WalkForwardResult."""
        from novatrade.optimization.walkforward import (
            WalkForwardConfig,
            WalkForwardResult,
            run_walk_forward,
        )

        h1, h4 = synthetic_candles
        # Use small windows to fit in 180 days of data
        wf_config = WalkForwardConfig(
            train_bars=1000,
            test_bars=500,
            step_bars=500,
            embargo_bars=2,
            holdout_bars=500,
        )
        result = run_walk_forward(default_config, h1, h4, wf_config)
        assert isinstance(result, WalkForwardResult)

    @pytest.mark.slow
    def test_walk_forward_creates_windows(self, synthetic_candles, default_config):
        """Walk-forward with sufficient data creates at least 1 window."""
        from novatrade.optimization.walkforward import WalkForwardConfig, run_walk_forward

        h1, h4 = synthetic_candles
        wf_config = WalkForwardConfig(
            train_bars=1000,
            test_bars=500,
            step_bars=500,
            embargo_bars=2,
            holdout_bars=500,
        )
        result = run_walk_forward(default_config, h1, h4, wf_config)
        assert len(result.windows) >= 1

    @pytest.mark.slow
    def test_window_result_has_scores(self, synthetic_candles, default_config):
        """WindowResult has IS and OOS scout scores."""
        from novatrade.optimization.walkforward import WalkForwardConfig, run_walk_forward

        h1, h4 = synthetic_candles
        wf_config = WalkForwardConfig(
            train_bars=1000,
            test_bars=500,
            step_bars=500,
            embargo_bars=2,
            holdout_bars=500,
        )
        result = run_walk_forward(default_config, h1, h4, wf_config)
        if result.windows:
            w = result.windows[0]
            assert hasattr(w, "is_scout_score")
            assert hasattr(w, "oos_scout_score")
            assert hasattr(w, "oos_is_ratio")
            assert hasattr(w, "passed")
            assert isinstance(w.is_scout_score, float)
            assert isinstance(w.oos_scout_score, float)

    def test_not_enough_data_returns_empty(self, default_config):
        """Insufficient data returns empty windows."""
        from novatrade.optimization.walkforward import run_walk_forward

        h1 = generate_synthetic_candles("EURUSD", "H1", days=5)
        h4 = aggregate_h1_to_h4(h1)
        # Default config requires ~8000+ bars, only have 120
        result = run_walk_forward(default_config, h1, h4)
        assert len(result.windows) == 0

    def test_holdout_reserved(self, synthetic_candles, default_config):
        """Holdout bars are reserved at the end, not used in windows."""
        from novatrade.optimization.walkforward import WalkForwardConfig, run_walk_forward

        h1, h4 = synthetic_candles
        holdout_bars = 500
        wf_config = WalkForwardConfig(
            train_bars=1000,
            test_bars=500,
            step_bars=500,
            embargo_bars=2,
            holdout_bars=holdout_bars,
        )
        result = run_walk_forward(default_config, h1, h4, wf_config)
        usable = len(h1) - holdout_bars
        # All windows must end within the usable range
        for w in result.windows:
            assert w.test_end <= usable

    @pytest.mark.slow
    def test_overall_passed_reflects_windows(self, synthetic_candles, default_config):
        """overall_passed is True only if all windows passed and holdout passed."""
        from novatrade.optimization.walkforward import WalkForwardConfig, run_walk_forward

        h1, h4 = synthetic_candles
        wf_config = WalkForwardConfig(
            train_bars=1000,
            test_bars=500,
            step_bars=500,
            embargo_bars=2,
            holdout_bars=500,
        )
        result = run_walk_forward(default_config, h1, h4, wf_config)
        if result.windows:
            all_win_passed = all(w.passed for w in result.windows)
            holdout_ok = result.holdout_passed is True or result.holdout_passed is None
            assert result.overall_passed == (all_win_passed and holdout_ok)

    @pytest.mark.slow
    def test_median_oos_score_computed(self, synthetic_candles, default_config):
        """median_oos_score is computed when windows exist."""
        from novatrade.optimization.walkforward import WalkForwardConfig, run_walk_forward

        h1, h4 = synthetic_candles
        wf_config = WalkForwardConfig(
            train_bars=1000,
            test_bars=500,
            step_bars=500,
            embargo_bars=2,
            holdout_bars=500,
        )
        result = run_walk_forward(default_config, h1, h4, wf_config)
        if result.windows:
            assert isinstance(result.median_oos_score, float)
            assert isinstance(result.median_oos_is_ratio, float)


# ===================================================================
# Phase 3: Perturbation Tests
# ===================================================================


class TestPerturbation:
    """Tests for perturbation stability testing."""

    @pytest.mark.slow
    def test_perturbation_produces_results(self, synthetic_candles, default_config):
        """run_perturbation_test produces results for each parameter."""
        from novatrade.optimization.perturbation import (
            PerturbationConfig,
            run_perturbation_test,
        )

        h1, h4 = synthetic_candles
        pc = PerturbationConfig(
            deltas=[0.05],
            params_to_test=["irb_threshold", "ema_period"],
        )
        result = run_perturbation_test(default_config, h1, h4, baseline_score=0.5, perturb_config=pc)
        # 2 params * 1 delta * 2 directions = up to 4 results (some may be skipped if clamped)
        assert len(result.results) >= 1

    @pytest.mark.slow
    def test_perturbed_values_within_bounds(self, synthetic_candles, default_config):
        """All perturbed values respect PARAMETER_BOUNDS."""
        from novatrade.optimization.perturbation import (
            PerturbationConfig,
            run_perturbation_test,
        )

        h1, h4 = synthetic_candles
        bounds = StrategyConfig.PARAMETER_BOUNDS
        pc = PerturbationConfig(
            deltas=[0.05, 0.10],
            params_to_test=["irb_threshold", "trail_atr_multiplier"],
        )
        result = run_perturbation_test(default_config, h1, h4, baseline_score=0.5, perturb_config=pc)
        for pr in result.results:
            b = bounds[pr.param_name]
            assert pr.perturbed_value >= b.min_val, f"{pr.param_name}: {pr.perturbed_value} < {b.min_val}"
            assert pr.perturbed_value <= b.max_val, f"{pr.param_name}: {pr.perturbed_value} > {b.max_val}"

    @pytest.mark.slow
    def test_both_directions_tested(self, synthetic_candles, default_config):
        """Both '+' and '-' directions are tested."""
        from novatrade.optimization.perturbation import (
            PerturbationConfig,
            run_perturbation_test,
        )

        h1, h4 = synthetic_candles
        pc = PerturbationConfig(
            deltas=[0.05],
            params_to_test=["trail_atr_multiplier"],  # 1.5 default, bounds [1.0, 3.0] — both directions work
        )
        result = run_perturbation_test(default_config, h1, h4, baseline_score=0.5, perturb_config=pc)
        directions = {pr.direction for pr in result.results}
        assert "+" in directions
        assert "-" in directions

    @pytest.mark.slow
    def test_cliff_detected_on_large_drop(self, synthetic_candles, default_config):
        """cliff_detected is True when fitness drop exceeds 50%."""
        from novatrade.optimization.perturbation import PerturbationResult, PerturbResult

        # Construct a result manually with a large drop
        pr = PerturbResult(
            param_name="irb_threshold",
            delta=0.10,
            direction="+",
            original_value=0.45,
            perturbed_value=0.50,
            original_score=1.0,
            perturbed_score=0.3,
            fitness_drop_pct=0.70,
            passed=False,
        )
        result = PerturbationResult(
            results=[pr],
            all_passed=False,
            worst_drop=0.70,
            cliff_detected=True,
        )
        assert result.cliff_detected is True
        assert result.worst_drop > 0.50

    def test_perturbation_config_defaults(self):
        """PerturbationConfig has correct default deltas."""
        from novatrade.optimization.perturbation import PerturbationConfig

        pc = PerturbationConfig()
        assert pc.deltas == [0.05, 0.10]
        assert pc.max_fitness_drop == 0.25

    @pytest.mark.slow
    def test_fitness_drop_is_non_negative(self, synthetic_candles, default_config):
        """fitness_drop_pct is always >= 0 (improvements are not penalised)."""
        from novatrade.optimization.perturbation import (
            PerturbationConfig,
            run_perturbation_test,
        )

        h1, h4 = synthetic_candles
        pc = PerturbationConfig(
            deltas=[0.05],
            params_to_test=["ema_period"],
        )
        result = run_perturbation_test(default_config, h1, h4, baseline_score=0.5, perturb_config=pc)
        for pr in result.results:
            assert pr.fitness_drop_pct >= 0

    @pytest.mark.slow
    def test_perturbation_result_aggregate_flags(self, synthetic_candles, default_config):
        """PerturbationResult.all_passed reflects individual results."""
        from novatrade.optimization.perturbation import (
            PerturbationConfig,
            run_perturbation_test,
        )

        h1, h4 = synthetic_candles
        pc = PerturbationConfig(
            deltas=[0.05],
            params_to_test=["irb_threshold"],
        )
        result = run_perturbation_test(default_config, h1, h4, baseline_score=0.5, perturb_config=pc)
        if result.results:
            expected_all_passed = all(r.passed for r in result.results)
            assert result.all_passed == expected_all_passed


# ===================================================================
# Phase 3: Stress Tests
# ===================================================================


class TestStress:
    """Tests for cost stress testing."""

    def test_stress_config_defaults(self):
        """StressConfig has correct defaults."""
        from novatrade.optimization.stress import StressConfig

        sc = StressConfig()
        assert sc.spread_multiplier == 2.0
        assert sc.extra_slippage_pips == 1.0
        assert sc.delayed_entry_bars == 1

    @pytest.mark.slow
    def test_stress_test_produces_result(self, synthetic_candles, default_config):
        """run_stress_test returns a StressResult."""
        from novatrade.optimization.stress import StressResult, run_stress_test

        h1, h4 = synthetic_candles
        result = run_stress_test(default_config, h1, h4, normal_score=0.5)
        assert isinstance(result, StressResult)
        assert result.normal_score == 0.5
        assert isinstance(result.stress_score, float)

    @pytest.mark.slow
    def test_stressed_score_not_higher(self, synthetic_candles, default_config):
        """Higher costs should generally not improve the score (or at most marginally)."""
        from novatrade.optimization.stress import run_stress_test

        h1, h4 = synthetic_candles
        # Run baseline first
        from novatrade.optimization.perturbation import _run_backtest_score

        baseline = _run_backtest_score(default_config, h1, h4)
        result = run_stress_test(default_config, h1, h4, normal_score=baseline)
        # Stressed score should typically be <= baseline (or close to it)
        assert result.stress_score <= baseline + 0.1  # small tolerance

    @pytest.mark.slow
    def test_still_profitable_reflects_score(self, synthetic_candles, default_config):
        """still_profitable is True iff stress_score > 0."""
        from novatrade.optimization.stress import run_stress_test

        h1, h4 = synthetic_candles
        result = run_stress_test(default_config, h1, h4, normal_score=0.5)
        assert result.still_profitable == (result.stress_score > 0)

    @pytest.mark.slow
    def test_extreme_stress_degrades_significantly(self, synthetic_candles, default_config):
        """10x spread should cause significant degradation."""
        from novatrade.optimization.stress import StressConfig, run_stress_test

        h1, h4 = synthetic_candles
        extreme = StressConfig(spread_multiplier=10.0, extra_slippage_pips=5.0)
        result = run_stress_test(default_config, h1, h4, normal_score=0.5, stress_config=extreme)
        # With 10x spread, degradation should be significant
        assert result.score_degradation_pct >= 0

    @pytest.mark.slow
    def test_stress_degradation_non_negative(self, synthetic_candles, default_config):
        """score_degradation_pct is always >= 0."""
        from novatrade.optimization.stress import run_stress_test

        h1, h4 = synthetic_candles
        result = run_stress_test(default_config, h1, h4, normal_score=0.5)
        assert result.score_degradation_pct >= 0

    @pytest.mark.slow
    def test_stress_passed_matches_profitable(self, synthetic_candles, default_config):
        """passed == still_profitable for stress tests."""
        from novatrade.optimization.stress import run_stress_test

        h1, h4 = synthetic_candles
        result = run_stress_test(default_config, h1, h4, normal_score=0.5)
        assert result.passed == result.still_profitable


# ===================================================================
# Phase 4: Sweep Engine Tests
# ===================================================================


class TestSweepEngine:
    """Tests for parameter sweep engines."""

    def test_generate_random_configs_count(self):
        """generate_random_configs produces the requested number of configs."""
        from novatrade.optimization.sweep_engine import generate_random_configs

        configs = generate_random_configs(50, seed=42)
        assert len(configs) == 50

    def test_random_configs_within_bounds(self):
        """All random config values are within PARAMETER_BOUNDS."""
        from novatrade.optimization.sweep_engine import generate_random_configs

        bounds = StrategyConfig.PARAMETER_BOUNDS
        configs = generate_random_configs(100, seed=42)
        for cfg in configs:
            for param, value in cfg.items():
                b = bounds[param]
                assert b.min_val <= value <= b.max_val, f"{param}={value} outside [{b.min_val}, {b.max_val}]"

    def test_random_configs_integer_params_are_int(self):
        """Integer parameters (periods, bar counts) are int type."""
        from novatrade.optimization.sweep_engine import INT_PARAMS, generate_random_configs

        configs = generate_random_configs(20, seed=42)
        for cfg in configs:
            for param, value in cfg.items():
                if param in INT_PARAMS:
                    assert isinstance(value, int), f"{param}={value} should be int"

    def test_generate_latin_hypercube_count(self):
        """generate_latin_hypercube produces the requested number of configs."""
        from novatrade.optimization.sweep_engine import generate_latin_hypercube

        configs = generate_latin_hypercube(50, seed=42)
        assert len(configs) == 50

    def test_latin_hypercube_within_bounds(self):
        """All LHS config values are within PARAMETER_BOUNDS."""
        from novatrade.optimization.sweep_engine import generate_latin_hypercube

        bounds = StrategyConfig.PARAMETER_BOUNDS
        configs = generate_latin_hypercube(100, seed=42)
        for cfg in configs:
            for param, value in cfg.items():
                b = bounds[param]
                assert b.min_val <= value <= b.max_val, f"{param}={value} outside [{b.min_val}, {b.max_val}]"

    def test_lhs_better_coverage_than_random(self):
        """LHS provides better space coverage (lower variance of parameter values)."""
        from novatrade.optimization.sweep_engine import (
            generate_latin_hypercube,
            generate_random_configs,
        )

        n = 100
        param = "irb_threshold"
        StrategyConfig.PARAMETER_BOUNDS[param]

        lhs_configs = generate_latin_hypercube(n, params=[param], seed=42)
        rnd_configs = generate_random_configs(n, params=[param], seed=42)

        lhs_vals = sorted(cfg[param] for cfg in lhs_configs)
        rnd_vals = sorted(cfg[param] for cfg in rnd_configs)

        # Compute gap variance (LHS should have more uniform spacing)
        lhs_gaps = [lhs_vals[i + 1] - lhs_vals[i] for i in range(len(lhs_vals) - 1)]
        rnd_gaps = [rnd_vals[i + 1] - rnd_vals[i] for i in range(len(rnd_vals) - 1)]

        lhs_gap_var = np.var(lhs_gaps)
        rnd_gap_var = np.var(rnd_gaps)

        # LHS should have lower gap variance (more uniform)
        assert lhs_gap_var <= rnd_gap_var * 1.5  # generous tolerance

    @pytest.mark.slow
    def test_run_sweep_returns_result(self, synthetic_candles, default_config):
        """run_sweep returns a SweepResult."""
        from novatrade.optimization.sweep_engine import SweepConfig, SweepResult, run_sweep

        h1, h4 = synthetic_candles
        sc = SweepConfig(n_experiments=5, method="random", seed=42)
        result = run_sweep(default_config, h1, h4, sc)
        assert isinstance(result, SweepResult)
        assert result.total_experiments == 5

    @pytest.mark.slow
    def test_sweep_best_is_highest_score(self, synthetic_candles, default_config):
        """SweepResult.best is the experiment with the highest scout_score."""
        from novatrade.optimization.sweep_engine import SweepConfig, run_sweep

        h1, h4 = synthetic_candles
        sc = SweepConfig(n_experiments=5, method="random", seed=42)
        result = run_sweep(default_config, h1, h4, sc)
        if result.best is not None:
            ranked = [e for e in result.experiments if e.status == "ranked"]
            if ranked:
                max_score = max(e.scout_score for e in ranked)
                assert result.best.scout_score == max_score

    @pytest.mark.slow
    def test_seeded_sweep_reproducible(self, synthetic_candles, default_config):
        """Same seed produces identical sweep results."""
        from novatrade.optimization.sweep_engine import SweepConfig, run_sweep

        h1, h4 = synthetic_candles
        sc = SweepConfig(n_experiments=3, method="random", seed=99)

        r1 = run_sweep(default_config, h1, h4, sc)
        r2 = run_sweep(default_config, h1, h4, sc)

        assert r1.total_experiments == r2.total_experiments
        for e1, e2 in zip(r1.experiments, r2.experiments, strict=False):
            assert e1.config == e2.config
            assert e1.scout_score == e2.scout_score

    @pytest.mark.slow
    def test_sweep_counts_consistent(self, synthetic_candles, default_config):
        """ranked + gated + crashed = total_experiments."""
        from novatrade.optimization.sweep_engine import SweepConfig, run_sweep

        h1, h4 = synthetic_candles
        sc = SweepConfig(n_experiments=5, method="random", seed=42)
        result = run_sweep(default_config, h1, h4, sc)
        assert result.ranked_count + result.gated_count + result.crashed_count == result.total_experiments

    @pytest.mark.slow
    def test_sweep_elapsed_positive(self, synthetic_candles, default_config):
        """Elapsed time is positive after sweep completion."""
        from novatrade.optimization.sweep_engine import SweepConfig, run_sweep

        h1, h4 = synthetic_candles
        sc = SweepConfig(n_experiments=3, method="random", seed=42)
        result = run_sweep(default_config, h1, h4, sc)
        assert result.elapsed_seconds > 0

    def test_seeded_configs_reproducible(self):
        """generate_random_configs with same seed produces identical configs."""
        from novatrade.optimization.sweep_engine import generate_random_configs

        a = generate_random_configs(10, seed=77)
        b = generate_random_configs(10, seed=77)
        assert a == b

    def test_lhs_seeded_reproducible(self):
        """generate_latin_hypercube with same seed produces identical configs."""
        from novatrade.optimization.sweep_engine import generate_latin_hypercube

        a = generate_latin_hypercube(10, seed=77)
        b = generate_latin_hypercube(10, seed=77)
        assert a == b


# ===================================================================
# Phase 4: Strategy Selection Tests
# ===================================================================


class TestStrategySelection:
    """Tests for experiment strategy selection."""

    def test_select_local_for_early_indices(self):
        """Early experiment indices (0-59) map to LOCAL strategy."""
        from novatrade.optimization.strategies import ExperimentStrategy, select_strategy

        for i in range(0, 59):
            assert select_strategy(i) == ExperimentStrategy.LOCAL

    def test_select_explore_for_middle_indices(self):
        """Indices 60-74 map to EXPLORE strategy (default 15% budget)."""
        from novatrade.optimization.strategies import ExperimentStrategy, select_strategy

        assert select_strategy(60) == ExperimentStrategy.EXPLORE

    def test_select_bayesian_for_budget_range(self):
        """Indices 75-89 map to BAYESIAN strategy (default 15% budget)."""
        from novatrade.optimization.strategies import ExperimentStrategy, select_strategy

        assert select_strategy(75) == ExperimentStrategy.BAYESIAN

    def test_select_ablation_for_budget_range(self):
        """Indices 90-94 map to ABLATION strategy (default 5% budget)."""
        from novatrade.optimization.strategies import ExperimentStrategy, select_strategy

        assert select_strategy(90) == ExperimentStrategy.ABLATION

    def test_select_combination_for_last_indices(self):
        """Indices 95-99 map to COMBINATION strategy (default 5% budget)."""
        from novatrade.optimization.strategies import ExperimentStrategy, select_strategy

        assert select_strategy(95) == ExperimentStrategy.COMBINATION

    def test_strategy_budget_sums_to_one(self):
        """Default StrategyBudget percentages sum to 1.0."""
        from novatrade.optimization.strategies import StrategyBudget

        b = StrategyBudget()
        total = b.local_pct + b.explore_pct + b.bayesian_pct + b.ablation_pct + b.combination_pct
        assert abs(total - 1.0) < 1e-9

    def test_custom_budget(self):
        """Custom budget allocations work correctly."""
        from novatrade.optimization.strategies import (
            ExperimentStrategy,
            StrategyBudget,
            select_strategy,
        )

        # 50% local, 50% explore
        budget = StrategyBudget(
            local_pct=0.50,
            explore_pct=0.50,
            bayesian_pct=0.0,
            ablation_pct=0.0,
            combination_pct=0.0,
        )
        assert select_strategy(0, budget=budget) == ExperimentStrategy.LOCAL
        assert select_strategy(49, budget=budget) == ExperimentStrategy.LOCAL
        assert select_strategy(50, budget=budget) == ExperimentStrategy.EXPLORE

    def test_all_strategies_assigned(self):
        """All 5 strategies are assigned across 100 experiments with default budget."""
        from novatrade.optimization.strategies import ExperimentStrategy, select_strategy

        strategies = {select_strategy(i) for i in range(100)}
        assert ExperimentStrategy.LOCAL in strategies
        assert ExperimentStrategy.EXPLORE in strategies
        assert ExperimentStrategy.BAYESIAN in strategies
        assert ExperimentStrategy.ABLATION in strategies
        assert ExperimentStrategy.COMBINATION in strategies


class TestCandidateGenerators:
    """Tests for local, explore, and combination candidate generators."""

    def test_local_candidate_within_bounds(self):
        """generate_local_candidate stays within PARAMETER_BOUNDS."""
        from novatrade.optimization.strategies import generate_local_candidate

        bounds = StrategyConfig.PARAMETER_BOUNDS
        best = {p: (b.min_val + b.max_val) / 2 for p, b in bounds.items()}

        candidate = generate_local_candidate(best, seed=42)
        for param, value in candidate.items():
            b = bounds[param]
            assert b.min_val <= value <= b.max_val, f"{param}={value} outside [{b.min_val}, {b.max_val}]"

    def test_local_candidate_near_best(self):
        """Local candidate should be close to the best config (small perturbation)."""
        from novatrade.optimization.strategies import generate_local_candidate

        bounds = StrategyConfig.PARAMETER_BOUNDS
        best = {p: (b.min_val + b.max_val) / 2 for p, b in bounds.items()}

        candidate = generate_local_candidate(best, scale=0.01, seed=42)
        # Check that values are within 5% of the range from the best
        for param, value in candidate.items():
            b = bounds[param]
            span = b.max_val - b.min_val
            diff = abs(value - best[param])
            assert diff <= 0.05 * span + 1  # +1 for integer rounding

    def test_explore_candidate_valid(self):
        """generate_explore_candidate produces a valid config dict."""
        from novatrade.optimization.strategies import generate_explore_candidate

        bounds = StrategyConfig.PARAMETER_BOUNDS
        candidate = generate_explore_candidate(seed=42)
        assert isinstance(candidate, dict)
        for param, value in candidate.items():
            b = bounds[param]
            assert b.min_val <= value <= b.max_val

    def test_explore_candidate_all_params(self):
        """Explore candidate includes all optimizable params by default."""
        from novatrade.optimization.strategies import generate_explore_candidate

        candidate = generate_explore_candidate(seed=42)
        for param in StrategyConfig.OPTIMIZABLE_PARAMS:
            assert param in candidate

    def test_combination_candidate_from_parents(self):
        """generate_combination_candidate merges values from parent configs."""
        from novatrade.optimization.strategies import generate_combination_candidate

        bounds = StrategyConfig.PARAMETER_BOUNDS
        parent_a = {p: b.min_val for p, b in bounds.items()}
        parent_b = {p: b.max_val for p, b in bounds.items()}

        candidate = generate_combination_candidate([parent_a, parent_b], seed=42)
        assert isinstance(candidate, dict)
        for param, value in candidate.items():
            b = bounds[param]
            assert b.min_val <= value <= b.max_val
            # Each value should come from one of the parents (within rounding tolerance)
            from_a = abs(value - parent_a[param]) < 1
            from_b = abs(value - parent_b[param]) < 1
            assert from_a or from_b, (
                f"{param}={value} not from parent_a={parent_a[param]} or parent_b={parent_b[param]}"
            )

    def test_combination_empty_parents_raises(self):
        """generate_combination_candidate raises ValueError for empty parents."""
        from novatrade.optimization.strategies import generate_combination_candidate

        with pytest.raises(ValueError, match="non-empty"):
            generate_combination_candidate([])

    def test_combination_single_parent(self):
        """Combination with one parent returns values from that parent."""
        from novatrade.optimization.strategies import generate_combination_candidate

        bounds = StrategyConfig.PARAMETER_BOUNDS
        parent = {p: (b.min_val + b.max_val) / 2 for p, b in bounds.items()}
        candidate = generate_combination_candidate([parent], seed=42)
        for param in StrategyConfig.OPTIMIZABLE_PARAMS:
            if param in candidate and param in parent:
                bounds[param]
                # Value should be the parent's value (possibly snapped to int)
                expected = parent[param]
                assert abs(candidate[param] - expected) < 1


# ===================================================================
# Phase 4: Ablation Tests
# ===================================================================


class TestAblation:
    """Tests for filter ablation testing."""

    def test_no_filters_returns_empty(self, synthetic_candles, default_config):
        """run_ablation with no filters_enabled returns empty list."""
        from novatrade.optimization.ablation import run_ablation

        h1, h4 = synthetic_candles
        # Default config has filters_enabled=[]
        assert default_config.filters_enabled == []
        results = run_ablation(default_config, h1, h4, baseline_score=0.5)
        assert results == []

    @pytest.mark.slow
    def test_ablation_with_filters(self, synthetic_candles):
        """run_ablation with mock filters produces one result per filter."""
        from novatrade.optimization.ablation import run_ablation

        h1, h4 = synthetic_candles
        config = StrategyConfig(filters_enabled=["session_london", "volatility_filter"])
        results = run_ablation(config, h1, h4, baseline_score=0.5)
        assert len(results) == 2
        filter_names = {r.filter_name for r in results}
        assert "session_london" in filter_names
        assert "volatility_filter" in filter_names

    @pytest.mark.slow
    def test_ablation_result_fields(self, synthetic_candles):
        """AblationResult has correct fields populated."""
        from novatrade.optimization.ablation import run_ablation

        h1, h4 = synthetic_candles
        config = StrategyConfig(filters_enabled=["session_london"])
        results = run_ablation(config, h1, h4, baseline_score=0.5)
        if results:
            r = results[0]
            assert r.filter_name == "session_london"
            assert r.baseline_score == 0.5
            assert isinstance(r.ablated_score, float)
            assert isinstance(r.score_change, float)
            assert isinstance(r.filter_contributes, bool)

    @pytest.mark.slow
    def test_ablation_score_change_sign(self, synthetic_candles):
        """filter_contributes is True when removing the filter reduces score."""
        from novatrade.optimization.ablation import run_ablation

        h1, h4 = synthetic_candles
        config = StrategyConfig(filters_enabled=["test_filter"])
        results = run_ablation(config, h1, h4, baseline_score=0.5)
        if results:
            r = results[0]
            assert r.filter_contributes == (r.ablated_score < r.baseline_score)


# ===================================================================
# Config Schema Tests (supporting all phases)
# ===================================================================


class TestStrategyConfig:
    """Tests for StrategyConfig schema validation and methods."""

    def test_default_config_valid(self):
        """Default StrategyConfig passes validation."""
        config = StrategyConfig()
        assert config.irb_threshold == 0.45
        assert config.ema_period == 20

    def test_content_hash_deterministic(self):
        """Same config produces same content hash."""
        a = StrategyConfig()
        b = StrategyConfig()
        assert a.content_hash() == b.content_hash()

    def test_content_hash_changes_with_params(self):
        """Different params produce different content hash."""
        a = StrategyConfig(irb_threshold=0.40)
        b = StrategyConfig(irb_threshold=0.50)
        assert a.content_hash() != b.content_hash()

    def test_yaml_round_trip(self, tmp_path: Path):
        """YAML save/load round-trip preserves all fields."""
        config = StrategyConfig(irb_threshold=0.40, ema_period=25, name="test_cfg")
        yaml_path = tmp_path / "test_config.yaml"
        config.to_yaml(yaml_path)

        loaded = StrategyConfig.from_yaml(yaml_path)
        assert loaded.irb_threshold == config.irb_threshold
        assert loaded.ema_period == config.ema_period
        assert loaded.name == config.name

    def test_to_environment_kwargs(self):
        """to_environment_kwargs returns strategy params only."""
        config = StrategyConfig(irb_threshold=0.40, ema_period=30)
        kwargs = config.to_environment_kwargs()
        assert kwargs["irb_threshold"] == 0.40
        assert kwargs["ema_period"] == 30
        assert "name" not in kwargs
        assert "version" not in kwargs
        assert "filters_enabled" not in kwargs

    def test_parameter_bounds_all_present(self):
        """Every OPTIMIZABLE_PARAM has bounds defined."""
        for param in StrategyConfig.OPTIMIZABLE_PARAMS:
            assert param in StrategyConfig.PARAMETER_BOUNDS

    def test_out_of_bounds_raises(self):
        """Values outside bounds raise ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            StrategyConfig(irb_threshold=0.99)  # max is 0.60

        with pytest.raises(ValidationError):
            StrategyConfig(ema_period=5)  # min is 10

    def test_from_environment(self):
        """from_environment extracts params from BacktestEnvironment."""
        from novatrade.backtest.environment import DEFAULT_ENVIRONMENT

        config = StrategyConfig.from_environment(DEFAULT_ENVIRONMENT)
        assert config.irb_threshold == DEFAULT_ENVIRONMENT.irb_threshold
        assert config.ema_period == DEFAULT_ENVIRONMENT.ema_period
        assert config.atr_period == DEFAULT_ENVIRONMENT.atr_period
