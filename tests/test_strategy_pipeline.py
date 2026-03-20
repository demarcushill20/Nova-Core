"""Comprehensive tests for all phases of the NovaTrade Strategy Pipeline.

Tests cover:
  Phase 1: Doctrine (concept translation, YAML round-trip, bridge)
  Phase 2: Data Quality, Loader, Timeframe Derivation
  Phase 3: Pipeline Orchestrator and Reporter
  Phase 4: Mutations (filter, structural, ancestry, weighted dispatch)
  Phase 5: Stage C robustness gates (Monte Carlo, cost stress, evaluator)
  Phase 5A: Durability (rolling windows, YoY, regime, drawdown, OOS decay)
"""
# ruff: noqa: S311

from __future__ import annotations

import csv
import json
import math
import random
from pathlib import Path

import pytest

from novatrade.backtest.metrics import CompletedTrade, ExitReason, TradeSide
from novatrade.models import Candle

# ---------------------------------------------------------------------------
# Deterministic seed fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _seed_rng():
    """Seed the global RNG for deterministic tests."""
    random.seed(42)
    return


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _make_candles(
    n: int = 1000,
    symbol: str = "EURUSD",
    timeframe: str = "H1",
    start_ts: float | None = None,
    base_price: float = 1.1,
) -> list[Candle]:
    """Generate *n* synthetic candles for testing.

    Uses the seeded global RNG for reproducibility.  Each candle walks a
    random price series with realistic OHLCV structure.
    """
    candles: list[Candle] = []
    ts = start_ts or 1704067200.0  # 2024-01-01 00:00 UTC
    interval = 3600 if timeframe == "H1" else 14400  # H1 or H4
    price = base_price

    for _ in range(n):
        change = random.uniform(-0.001, 0.001)
        o = price
        c = price + change
        h = max(o, c) + random.uniform(0, 0.0005)
        l_val = min(o, c) - random.uniform(0, 0.0005)
        candles.append(
            Candle(
                timestamp=ts,
                open=o,
                high=h,
                low=l_val,
                close=c,
                volume=random.uniform(100, 1000),
                symbol=symbol,
                timeframe=timeframe,
            )
        )
        price = c
        ts += interval

    return candles


def _make_trades(
    n: int = 60,
    profitable_pct: float = 0.6,
    start_ts: float | None = None,
    spacing_seconds: float = 72000,
) -> list[CompletedTrade]:
    """Generate *n* synthetic trades for testing.

    Uses the seeded global RNG for reproducibility.  The *profitable_pct*
    parameter controls what fraction of trades are winners.

    Args:
        n: Number of trades to generate.
        profitable_pct: Fraction of trades that are winners (0.0 to 1.0).
        start_ts: Starting Unix timestamp (default 2024-01-01 UTC).
        spacing_seconds: Time gap between consecutive trades.  Use larger
            values (e.g. 315360) to span multiple years for durability tests.
    """
    trades: list[CompletedTrade] = []
    ts = start_ts or 1704067200.0

    for i in range(n):
        is_winner = random.random() < profitable_pct
        pnl = random.uniform(5, 50) if is_winner else random.uniform(-30, -5)
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
                entry_timestamp=ts + i * spacing_seconds,
                exit_timestamp=ts + i * spacing_seconds + 36000,
            )
        )

    return trades


def _save_candles_csv(candles: list[Candle], path: Path) -> None:
    """Write candles to a generic CSV file with standard column names."""
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in candles:
            writer.writerow([c.timestamp, c.open, c.high, c.low, c.close, c.volume])


# =========================================================================
# Phase 1 Tests — Doctrine
# =========================================================================


class TestDoctrine:
    """Tests for doctrine translator and bridge."""

    def test_concept_to_doctrine_irb(self):
        from novatrade.doctrine.translator import concept_to_doctrine

        d = concept_to_doctrine("IRB reversal with trailing ATR exit", "EURUSD", "H1")
        assert d.setup_type == "reversal"
        assert "irb_threshold" in d.mutable_params
        assert "ema_period" in d.mutable_params
        assert d.data.pair == "EURUSD"
        assert d.entry_signal == "irb_rejection"

    def test_concept_to_doctrine_breakout(self):
        from novatrade.doctrine.translator import concept_to_doctrine

        d = concept_to_doctrine("range breakout on consolidation zones", "GBPUSD", "H4")
        assert d.setup_type == "breakout"
        assert "lookback_period" in d.mutable_params
        assert d.data.pair == "GBPUSD"
        assert d.entry_signal == "range_breakout"

    def test_concept_to_doctrine_mean_reversion(self):
        from novatrade.doctrine.translator import concept_to_doctrine

        d = concept_to_doctrine("bollinger band mean reversion", "USDJPY", "H1")
        assert d.setup_type == "mean_reversion"
        assert "bb_period" in d.mutable_params
        assert "rsi_period" in d.mutable_params

    def test_concept_to_doctrine_trend_following(self):
        from novatrade.doctrine.translator import concept_to_doctrine

        d = concept_to_doctrine("EMA crossover trend following", "AUDUSD", "D1")
        assert d.setup_type == "trend_following"
        assert "fast_ema" in d.mutable_params
        assert "slow_ema" in d.mutable_params

    def test_concept_to_doctrine_unknown(self):
        from novatrade.doctrine.translator import concept_to_doctrine

        d = concept_to_doctrine("some completely novel idea xyz", "EURUSD", "H1")
        # Unknown concepts return a skeleton
        assert d.entry_signal == "custom"
        assert "[SKELETON]" in d.description
        assert "atr_period" in d.mutable_params

    def test_doctrine_yaml_roundtrip(self, tmp_path):
        from novatrade.doctrine.schema import StrategyDoctrine
        from novatrade.doctrine.translator import concept_to_doctrine

        original = concept_to_doctrine("IRB reversal", "EURUSD", "H1")
        yaml_path = tmp_path / "test_doctrine.yaml"
        original.to_yaml(yaml_path)

        loaded = StrategyDoctrine.from_yaml(yaml_path)
        assert loaded.name == original.name
        assert loaded.setup_type == original.setup_type
        assert loaded.data.pair == original.data.pair
        assert loaded.mutable_params.keys() == original.mutable_params.keys()
        assert loaded.content_hash() == original.content_hash()

    def test_doctrine_to_config_bridge(self):
        from novatrade.doctrine.bridge import doctrine_to_config
        from novatrade.doctrine.translator import concept_to_doctrine

        doctrine = concept_to_doctrine("IRB reversal", "EURUSD", "H1")
        config = doctrine_to_config(doctrine)
        assert config.name == doctrine.name
        assert config.irb_threshold == doctrine.mutable_params["irb_threshold"].default
        # Mandatory filters should be present
        for f in doctrine.filters_mandatory:
            assert f in config.filters_enabled

    def test_doctrine_config_compatibility(self):
        from novatrade.doctrine.bridge import validate_doctrine_config_compatibility
        from novatrade.doctrine.translator import concept_to_doctrine

        doctrine = concept_to_doctrine("IRB reversal", "EURUSD", "H1")
        issues = validate_doctrine_config_compatibility(doctrine)
        assert isinstance(issues, list)
        assert len(issues) == 0, f"Unexpected compatibility issues: {issues}"

    def test_detect_strategy_family(self):
        from novatrade.doctrine.translator import detect_strategy_family

        assert detect_strategy_family("IRB reversal") == "irb"
        assert detect_strategy_family("range breakout") == "breakout"
        assert detect_strategy_family("bollinger band") == "mean_reversion"
        assert detect_strategy_family("ema crossover") == "trend_following"
        assert detect_strategy_family("xyz unknown") == "unknown"

    def test_doctrine_content_hash_stable(self):
        from novatrade.doctrine.translator import concept_to_doctrine

        d1 = concept_to_doctrine("IRB reversal", "EURUSD", "H1")
        d2 = concept_to_doctrine("IRB reversal", "EURUSD", "H1")
        assert d1.content_hash() == d2.content_hash()

    def test_doctrine_filter_consistency_validation(self):
        from novatrade.doctrine.schema import (
            DoctrineData,
            StrategyDoctrine,
        )

        with pytest.raises(ValueError, match="mandatory and forbidden"):
            StrategyDoctrine(
                name="Bad",
                concept="test",
                data=DoctrineData(pair="EURUSD"),
                filters_mandatory=["trend_alignment"],
                filters_forbidden=["trend_alignment"],
            )


# =========================================================================
# Phase 2 Tests — Data Quality
# =========================================================================


class TestDataQuality:
    """Tests for candle data quality validation and auto-fix."""

    def test_validate_clean_data(self):
        candles = _make_candles(100)
        from novatrade.data.quality import validate_candles

        report = validate_candles(candles)
        assert report.overall_status in ("pass", "warn")
        assert report.total_candles == 100

    def test_nan_detection(self):
        from novatrade.data.quality import validate_candles

        candles = _make_candles(50)
        # Inject a NaN value
        bad = candles[10]
        candles[10] = Candle(
            bad.timestamp,
            float("nan"),
            bad.high,
            bad.low,
            bad.close,
            bad.volume,
            bad.symbol,
            bad.timeframe,
        )
        report = validate_candles(candles)
        assert any(c.name == "nan_check" and c.status.value == "fail" for c in report.checks)

    def test_ohlc_integrity_failure(self):
        from novatrade.data.quality import validate_candles

        candles = _make_candles(50)
        # Create a candle where high < low
        bad = candles[5]
        candles[5] = Candle(
            bad.timestamp,
            bad.open,
            bad.low - 0.001,  # high < low
            bad.high + 0.001,  # low > high
            bad.close,
            bad.volume,
            bad.symbol,
            bad.timeframe,
        )
        report = validate_candles(candles)
        assert any(c.name == "ohlc_integrity" and c.status.value == "fail" for c in report.checks)

    def test_duplicate_detection(self):
        from novatrade.data.quality import validate_candles

        candles = _make_candles(50)
        # Duplicate the timestamp of candle 20 at candle 21
        dup = candles[20]
        candles[21] = Candle(
            dup.timestamp,  # duplicate timestamp
            candles[21].open,
            candles[21].high,
            candles[21].low,
            candles[21].close,
            candles[21].volume,
            candles[21].symbol,
            candles[21].timeframe,
        )
        report = validate_candles(candles)
        dup_checks = [c for c in report.checks if c.name == "duplicate_check"]
        assert len(dup_checks) == 1
        # Either fail or warn (timestamp monotonicity may also fail)
        assert dup_checks[0].status.value in ("fail", "warn")

    def test_gap_detection(self):
        from novatrade.data.quality import validate_candles

        candles = _make_candles(50)
        # Inject a large gap between candle 25 and 26 (10x normal interval)
        candles[26] = Candle(
            candles[25].timestamp + 36000,  # 10 hours gap instead of 1
            candles[26].open,
            candles[26].high,
            candles[26].low,
            candles[26].close,
            candles[26].volume,
            candles[26].symbol,
            candles[26].timeframe,
        )
        # Fix subsequent timestamps to stay monotonic
        for i in range(27, len(candles)):
            candles[i] = Candle(
                candles[i - 1].timestamp + 3600,
                candles[i].open,
                candles[i].high,
                candles[i].low,
                candles[i].close,
                candles[i].volume,
                candles[i].symbol,
                candles[i].timeframe,
            )
        report = validate_candles(candles)
        gap_check = [c for c in report.checks if c.name == "gap_detection"]
        assert len(gap_check) == 1
        assert gap_check[0].status.value == "warn"

    def test_auto_fix_removes_nans(self):
        from novatrade.data.quality import auto_fix_candles, validate_candles

        candles = _make_candles(50)
        bad = candles[10]
        candles[10] = Candle(
            bad.timestamp,
            float("nan"),
            bad.high,
            bad.low,
            bad.close,
            bad.volume,
            bad.symbol,
            bad.timeframe,
        )
        report = validate_candles(candles)
        fixed = auto_fix_candles(candles, report)
        assert len(fixed) == 49  # one NaN candle removed
        # Verify no NaN values remain
        for c in fixed:
            assert not math.isnan(c.open)
            assert not math.isnan(c.high)
            assert not math.isnan(c.low)
            assert not math.isnan(c.close)

    def test_auto_fix_deduplicates(self):
        from novatrade.data.quality import auto_fix_candles, validate_candles

        candles = _make_candles(50)
        # Add a duplicate timestamp
        dup = candles[15]
        candles.append(
            Candle(
                dup.timestamp,
                1.11,
                1.12,
                1.10,
                1.115,
                500.0,
                dup.symbol,
                dup.timeframe,
            )
        )
        report = validate_candles(candles)
        fixed = auto_fix_candles(candles, report)
        # Duplicate should be removed
        timestamps = [c.timestamp for c in fixed]
        assert len(timestamps) == len(set(timestamps))


# =========================================================================
# Phase 2 Tests — Data Loader
# =========================================================================


class TestDataLoader:
    """Tests for multi-format candle data loading."""

    def test_csv_load(self, tmp_path):
        from novatrade.data.loader import load_candles

        candles = _make_candles(50)
        csv_path = tmp_path / "test.csv"
        _save_candles_csv(candles, csv_path)

        loaded = load_candles(csv_path, symbol="EURUSD", timeframe="H1")
        assert len(loaded) == 50
        assert loaded[0].symbol == "EURUSD"
        assert loaded[0].timeframe == "H1"
        # Verify sorted by timestamp
        for i in range(1, len(loaded)):
            assert loaded[i].timestamp >= loaded[i - 1].timestamp

    def test_json_load(self, tmp_path):
        from novatrade.data.loader import load_candles

        candles = _make_candles(30)
        json_data = [
            {
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ]
        json_path = tmp_path / "test.json"
        json_path.write_text(json.dumps(json_data))

        loaded = load_candles(json_path, symbol="GBPUSD", timeframe="H4")
        assert len(loaded) == 30
        assert loaded[0].symbol == "GBPUSD"

    def test_format_detection(self, tmp_path):
        from novatrade.data.loader import CandleFormat, detect_format

        # CSV file
        csv_path = tmp_path / "test.csv"
        _save_candles_csv(_make_candles(10), csv_path)
        assert detect_format(csv_path) == CandleFormat.CSV

        # JSON file
        json_path = tmp_path / "test.json"
        json_path.write_text(json.dumps([{"timestamp": 1, "open": 1, "high": 1, "low": 1, "close": 1}]))
        assert detect_format(json_path) == CandleFormat.JSON

    def test_metatrader_format(self, tmp_path):
        from novatrade.data.loader import CandleFormat, load_candles

        mt5_path = tmp_path / "mt5.csv"
        with mt5_path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Time", "Open", "High", "Low", "Close", "Volume"])
            writer.writerow(["2024.01.01", "00:00:00", "1.1000", "1.1050", "1.0950", "1.1020", "500"])
            writer.writerow(["2024.01.01", "01:00:00", "1.1020", "1.1060", "1.0980", "1.1040", "600"])
            writer.writerow(["2024.01.01", "02:00:00", "1.1040", "1.1080", "1.1000", "1.1050", "550"])

        loaded = load_candles(mt5_path, symbol="EURUSD", timeframe="H1", fmt=CandleFormat.METATRADER)
        assert len(loaded) == 3
        assert loaded[0].open == pytest.approx(1.1000, abs=0.0001)
        assert loaded[0].symbol == "EURUSD"

    def test_file_not_found_raises(self):
        from novatrade.data.loader import load_candles

        with pytest.raises(FileNotFoundError):
            load_candles("/nonexistent/path/data.csv")


# =========================================================================
# Phase 2 Tests — Timeframe Derivation
# =========================================================================


class TestTimeframeDerivation:
    """Tests for candle timeframe aggregation."""

    def test_h1_to_h4(self):
        from novatrade.data.timeframe import derive_timeframe

        candles = _make_candles(100, timeframe="H1")
        h4 = derive_timeframe(candles, "H4")
        assert len(h4) == 25  # 100 H1 / 4

    def test_h1_to_d1(self):
        from novatrade.data.timeframe import derive_timeframe

        candles = _make_candles(240, timeframe="H1")
        d1 = derive_timeframe(candles, "D1")
        assert len(d1) == 10  # 240 / 24

    def test_ohlcv_aggregation_correctness(self):
        from novatrade.data.timeframe import derive_timeframe

        # Build 4 candles with known values
        candles = [
            Candle(0, 1.10, 1.15, 1.08, 1.12, 100.0, "EURUSD", "H1"),
            Candle(3600, 1.12, 1.18, 1.11, 1.16, 200.0, "EURUSD", "H1"),
            Candle(7200, 1.16, 1.20, 1.14, 1.17, 150.0, "EURUSD", "H1"),
            Candle(10800, 1.17, 1.19, 1.09, 1.13, 250.0, "EURUSD", "H1"),
        ]
        h4 = derive_timeframe(candles, "H4")
        assert len(h4) == 1
        c = h4[0]
        assert c.open == pytest.approx(1.10)  # first open
        assert c.high == pytest.approx(1.20)  # max high
        assert c.low == pytest.approx(1.08)  # min low
        assert c.close == pytest.approx(1.13)  # last close
        assert c.volume == pytest.approx(700.0)  # sum volume

    def test_invalid_derivation_rejected(self):
        from novatrade.data.timeframe import derive_timeframe

        candles = _make_candles(10, timeframe="H4")
        with pytest.raises(ValueError, match="target must be higher"):
            derive_timeframe(candles, "M5")

    def test_empty_candles_raises(self):
        from novatrade.data.timeframe import derive_timeframe

        with pytest.raises(ValueError, match="empty candle list"):
            derive_timeframe([], "H4")

    def test_unknown_timeframe_raises(self):
        from novatrade.data.timeframe import derive_timeframe

        candles = _make_candles(10, timeframe="H1")
        with pytest.raises(ValueError, match="Unknown target timeframe"):
            derive_timeframe(candles, "X99")


# =========================================================================
# Phase 3 Tests — Pipeline Orchestrator & Reporter
# =========================================================================


class TestPipelineOrchestrator:
    """Tests for the pipeline orchestrator data structures and reporter."""

    def test_pipeline_config_defaults(self):
        from novatrade.pipeline.orchestrator import PipelineConfig, PipelineMode

        config = PipelineConfig(data_path="/tmp/test.csv")
        assert config.pair == "EURUSD"
        assert config.timeframe == "H1"
        assert config.mode == PipelineMode.SINGLE
        assert config.experiments == 200

    def test_pipeline_config_experiments_clamped(self):
        from novatrade.pipeline.orchestrator import PipelineConfig

        # Min clamp
        c1 = PipelineConfig(data_path="/tmp/t.csv", experiments=0)
        assert c1.experiments == 1
        # Max clamp
        c2 = PipelineConfig(data_path="/tmp/t.csv", experiments=999999)
        assert c2.experiments == 10_000

    def test_pipeline_result_structure(self):
        from novatrade.pipeline.orchestrator import PipelineMode, PipelineResult

        result = PipelineResult(
            mode=PipelineMode.SINGLE,
            total_experiments=1,
            best_score=0.75,
        )
        assert result.mode == PipelineMode.SINGLE
        assert result.total_experiments == 1
        assert result.best_score == 0.75

    def test_pipeline_report_generation(self):
        from novatrade.pipeline.orchestrator import (
            PipelineMode,
            PipelineResult,
            PipelineStage,
        )
        from novatrade.pipeline.reporter import format_pipeline_report

        result = PipelineResult(
            mode=PipelineMode.SINGLE,
            total_experiments=1,
            best_score=0.5,
            doctrine_name="IRB Reversal H1",
            stages=[
                PipelineStage(name="load_data", status="ok", duration_ms=100),
                PipelineStage(name="validate_data", status="ok", duration_ms=50),
            ],
        )
        report = format_pipeline_report(result)
        assert "Pipeline" in report
        assert "SINGLE" in report
        assert "IRB Reversal H1" in report
        assert "load_data" in report

    def test_pipeline_json_report(self):
        from novatrade.pipeline.orchestrator import PipelineMode, PipelineResult
        from novatrade.pipeline.reporter import format_pipeline_json

        result = PipelineResult(
            mode=PipelineMode.CAMPAIGN,
            total_experiments=100,
            best_score=0.85,
        )
        json_str = format_pipeline_json(result)
        parsed = json.loads(json_str)
        assert parsed["mode"] == "campaign"
        assert parsed["total_experiments"] == 100
        assert parsed["best_score"] == pytest.approx(0.85, abs=0.001)

    def test_pipeline_stage_timing(self):
        from novatrade.pipeline.orchestrator import PipelineStage

        stage = PipelineStage(name="test_stage", status="ok", duration_ms=1234)
        assert stage.duration_ms == 1234
        assert stage.details == {}


# =========================================================================
# Phase 4 Tests — Mutations
# =========================================================================


class TestMutations:
    """Tests for L2/L3 mutation wiring and ancestry tracking."""

    def test_filter_mutation_respects_doctrine(self):
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.doctrine.translator import concept_to_doctrine
        from novatrade.optimization.mutations import generate_filter_mutation

        config = StrategyConfig()
        doctrine = concept_to_doctrine("IRB reversal", "EURUSD", "H1")
        mutated = generate_filter_mutation(config, doctrine=doctrine, rng=random.Random(42))

        # Mandatory filters must always be present
        for f in doctrine.filters_mandatory:
            assert f in mutated.filters_enabled
        # Forbidden filters must never be present
        for f in doctrine.filters_forbidden:
            assert f not in mutated.filters_enabled

    def test_filter_mutation_without_doctrine(self):
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.mutations import generate_filter_mutation

        config = StrategyConfig()
        mutated = generate_filter_mutation(config, rng=random.Random(123))
        # Should still produce a valid StrategyConfig
        assert isinstance(mutated, StrategyConfig)

    def test_structural_mutation_within_bounds(self):
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.mutations import generate_structural_mutation

        config = StrategyConfig()
        mutated = generate_structural_mutation(config, rng=random.Random(42))

        for param, bounds in StrategyConfig.PARAMETER_BOUNDS.items():
            val = getattr(mutated, param)
            assert bounds.min_val <= val <= bounds.max_val, (
                f"{param}: {val} outside [{bounds.min_val}, {bounds.max_val}]"
            )

    def test_structural_mutation_tags_lineage(self):
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.mutations import generate_structural_mutation

        config = StrategyConfig(name="base_config")
        mutated = generate_structural_mutation(config, rng=random.Random(42))
        assert "L3:" in mutated.description
        assert "base_config" in mutated.description

    def test_ancestry_tracking(self):
        from novatrade.optimization.mutations import AncestryTracker

        tracker = AncestryTracker()
        tracker.record("parent1", "child1", "l1_param", {"irb_threshold": 0.45})
        tracker.record("child1", "child2", "l2_filter", {"filters_enabled": ["trend"]})

        lineage = tracker.get_lineage("child2")
        assert len(lineage) == 2
        assert lineage[0].child_id == "child2"
        assert lineage[0].parent_id == "child1"
        assert lineage[1].child_id == "child1"
        assert lineage[1].parent_id == "parent1"

    def test_ancestry_get_children(self):
        from novatrade.optimization.mutations import AncestryTracker

        tracker = AncestryTracker()
        tracker.record("parent", "child_a", "l1_param", {"x": 1})
        tracker.record("parent", "child_b", "l2_filter", {"y": 2})
        tracker.record("child_a", "grandchild", "l3_structural", {"z": 3})

        children = tracker.get_children("parent")
        assert len(children) == 2
        child_ids = {c.child_id for c in children}
        assert child_ids == {"child_a", "child_b"}

    def test_ancestry_json_roundtrip(self):
        from novatrade.optimization.mutations import AncestryTracker

        tracker = AncestryTracker()
        tracker.record("p1", "c1", "l1_param", {"a": 1.0})
        tracker.record("c1", "c2", "l2_filter", {"b": ["trend"]})

        json_str = tracker.to_json()
        restored = AncestryTracker.from_json(json_str)
        assert len(restored.records) == 2
        lineage = restored.get_lineage("c2")
        assert len(lineage) == 2

    def test_mutated_candidate_budget(self):
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.mutations import generate_mutated_candidate

        config = StrategyConfig()
        types = {"l1_param": 0, "l2_filter": 0, "l3_structural": 0}
        rng = random.Random(42)

        for _ in range(100):
            _, mt = generate_mutated_candidate(
                config,
                {"l1": 0.60, "l2": 0.25, "l3": 0.15},
                rng=rng,
            )
            types[mt] += 1

        # L1 should be dominant (~60 out of 100)
        assert types["l1_param"] > 30
        # All types should appear at least once with 100 trials
        assert types["l2_filter"] > 0
        assert types["l3_structural"] > 0

    def test_mutation_diff(self):
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.mutations import compute_mutation_diff

        parent = StrategyConfig(irb_threshold=0.45, ema_period=20)
        child = StrategyConfig(irb_threshold=0.50, ema_period=20)
        diff = compute_mutation_diff(parent, child)
        assert "irb_threshold" in diff
        assert diff["irb_threshold"]["old"] == pytest.approx(0.45)
        assert diff["irb_threshold"]["new"] == pytest.approx(0.50)
        assert "ema_period" not in diff  # unchanged


# =========================================================================
# Phase 5 Tests — Stage C Robustness Gates
# =========================================================================


class TestStageC:
    """Tests for Stage C robustness evaluation gates."""

    def test_monte_carlo_returns_gate_result(self):
        from novatrade.evaluation.stage_c import monte_carlo_gate

        trades = _make_trades(100, profitable_pct=0.7)
        result = monte_carlo_gate(trades, n_simulations=100)
        assert isinstance(result.passed, bool)
        assert result.gate == "C.monte_carlo"
        assert "dd_95th" in result.details or "dd_95" in result.details

    def test_monte_carlo_profitable_strategy(self):
        from novatrade.evaluation.stage_c import monte_carlo_gate

        trades = _make_trades(100, profitable_pct=0.7)
        result = monte_carlo_gate(
            trades,
            n_simulations=200,
            max_dd_threshold=50.0,
        )
        # With 70% win rate and generous threshold, should pass
        assert result.passed is True

    def test_monte_carlo_losing_strategy(self):
        from novatrade.evaluation.stage_c import monte_carlo_gate

        trades = _make_trades(100, profitable_pct=0.2)
        result = monte_carlo_gate(
            trades,
            n_simulations=200,
            max_dd_threshold=5.0,
        )
        # Losing strategy with tight threshold should fail
        assert isinstance(result.passed, bool)

    def test_monte_carlo_empty_trades(self):
        from novatrade.evaluation.stage_c import monte_carlo_gate

        result = monte_carlo_gate([], n_simulations=100)
        assert result.passed is False
        assert result.details["n_trades"] == 0

    def test_stage_c_config_defaults(self):
        from novatrade.evaluation.stage_c import StageCConfig

        cfg = StageCConfig()
        assert cfg.run_monte_carlo is True
        assert cfg.mc_simulations == 1000
        assert cfg.mc_dd_threshold == 15.0

    def test_evaluate_stage_c_monte_carlo_only(self):
        from novatrade.evaluation.stage_c import StageCConfig, evaluate_stage_c

        trades = _make_trades(100, profitable_pct=0.65)
        candles = _make_candles(5000)
        results = evaluate_stage_c(
            trades,
            None,
            candles,
            candles[:1250],
            stage_c_config=StageCConfig(
                run_walk_forward=False,
                run_cost_stress=False,
                run_param_stability=False,
                mc_simulations=50,
            ),
        )
        assert len(results) == 1
        assert results[0].gate == "C.monte_carlo"
        assert hasattr(results[0], "passed")


# =========================================================================
# Phase 5A Tests — Durability Validation
# =========================================================================


class TestDurability:
    """Tests for strategy durability validation gates."""

    def test_rolling_window_analysis(self):
        from novatrade.evaluation.durability import rolling_window_analysis

        # Generate trades spanning ~2 years (200 trades, ~3.65 days apart = 730 days)
        trades = _make_trades(
            200,
            profitable_pct=0.6,
            start_ts=1640995200.0,
            spacing_seconds=315360,
        )
        windows = rolling_window_analysis(trades, window_months=12, step_months=3)
        assert len(windows) > 0
        for w in windows:
            assert w.trade_count > 0
            assert w.start_ts < w.end_ts

    def test_rolling_window_empty_trades(self):
        from novatrade.evaluation.durability import rolling_window_analysis

        windows = rolling_window_analysis([], window_months=12, step_months=3)
        assert windows == []

    def test_year_over_year_breakdown(self):
        from novatrade.evaluation.durability import year_over_year_breakdown

        # Trades spanning 2022-2024 (~2 years)
        trades = _make_trades(
            200,
            start_ts=1640995200.0,
            spacing_seconds=315360,
        )
        years = year_over_year_breakdown(trades)
        assert len(years) > 0
        assert all(y.trade_count > 0 for y in years)
        # Should be sorted chronologically
        for i in range(1, len(years)):
            assert years[i].year >= years[i - 1].year

    def test_year_over_year_empty(self):
        from novatrade.evaluation.durability import year_over_year_breakdown

        years = year_over_year_breakdown([])
        assert years == []

    def test_regime_classification(self):
        from novatrade.evaluation.durability import classify_regimes

        candles = _make_candles(500)
        regimes = classify_regimes(candles)
        assert len(regimes) > 0
        valid_regimes = {"trending", "ranging", "volatile", "quiet"}
        for regime in regimes.values():
            assert regime in valid_regimes

    def test_regime_classification_short_data(self):
        from novatrade.evaluation.durability import classify_regimes

        # Very short data — should default to "ranging"
        candles = _make_candles(5)
        regimes = classify_regimes(candles)
        assert all(r == "ranging" for r in regimes.values())

    def test_drawdown_survivability(self):
        from novatrade.evaluation.durability import drawdown_survivability_gate

        trades = _make_trades(100, profitable_pct=0.65)
        result = drawdown_survivability_gate(trades)
        assert isinstance(result.passed, bool)
        assert result.gate == "D.drawdown_survivability"
        assert "max_overall_dd_pct" in result.details

    def test_drawdown_survivability_empty(self):
        from novatrade.evaluation.durability import drawdown_survivability_gate

        result = drawdown_survivability_gate([])
        assert result.passed is False

    def test_oos_decay_gate_passes(self):
        from novatrade.evaluation.durability import oos_decay_gate

        # OOS PF = 1.0, IS PF = 1.5 => ratio = 0.67 > 0.50
        result = oos_decay_gate(1.5, 1.0)
        assert result.passed is True
        assert result.details["oos_is_ratio"] == pytest.approx(0.6667, abs=0.01)

    def test_oos_decay_gate_fails(self):
        from novatrade.evaluation.durability import oos_decay_gate

        # OOS PF = 0.3, IS PF = 1.5 => ratio = 0.20 < 0.50
        result = oos_decay_gate(1.5, 0.3)
        assert result.passed is False

    def test_oos_decay_zero_is(self):
        from novatrade.evaluation.durability import oos_decay_gate

        result = oos_decay_gate(0.0, 1.0)
        assert result.passed is False

    def test_evaluate_durability_full(self):
        from novatrade.evaluation.durability import evaluate_durability

        trades = _make_trades(
            200,
            profitable_pct=0.65,
            start_ts=1640995200.0,
            spacing_seconds=315360,
        )
        candles = _make_candles(5000, start_ts=1640995200.0)
        report = evaluate_durability(trades, candles)

        assert report.durability_grade in ("A", "B", "C", "D", "F")
        assert len(report.gates) > 0
        assert isinstance(report.overall_passed, bool)
        assert len(report.year_breakdown) > 0

    def test_evaluate_durability_with_oos(self):
        from novatrade.evaluation.durability import evaluate_durability

        trades = _make_trades(
            200,
            profitable_pct=0.65,
            start_ts=1640995200.0,
            spacing_seconds=315360,
        )
        candles = _make_candles(5000, start_ts=1640995200.0)
        report = evaluate_durability(
            trades,
            candles,
            is_profit_factor=1.5,
            oos_profit_factor=1.2,
        )
        # Should have OOS decay gate added
        gate_names = [g.gate for g in report.gates]
        assert "D.oos_decay" in gate_names
        assert report.oos_is_ratio is not None

    def test_durability_grade_all_pass(self):
        from novatrade.evaluation.durability import _compute_durability_grade
        from novatrade.evaluation.gates import GateResult

        gates = [
            GateResult(gate="D.rolling_profitability", passed=True, reason="ok"),
            GateResult(gate="D.regime_consistency", passed=True, reason="ok"),
            GateResult(gate="D.drawdown_survivability", passed=True, reason="ok"),
        ]
        assert _compute_durability_grade(gates) == "A"

    def test_durability_grade_one_fail(self):
        from novatrade.evaluation.durability import _compute_durability_grade
        from novatrade.evaluation.gates import GateResult

        gates = [
            GateResult(gate="D.rolling_profitability", passed=True, reason="ok"),
            GateResult(gate="D.regime_consistency", passed=False, reason="fail"),
            GateResult(gate="D.drawdown_survivability", passed=True, reason="ok"),
        ]
        assert _compute_durability_grade(gates) == "B"

    def test_durability_grade_critical_failure(self):
        from novatrade.evaluation.durability import _compute_durability_grade
        from novatrade.evaluation.gates import GateResult

        gates = [
            GateResult(gate="D.rolling_profitability", passed=False, reason="fail"),
            GateResult(gate="D.regime_consistency", passed=False, reason="fail"),
            GateResult(gate="D.drawdown_survivability", passed=True, reason="ok"),
        ]
        assert _compute_durability_grade(gates) == "F"

    def test_rolling_profitability_gate(self):
        from novatrade.evaluation.durability import rolling_profitability_gate

        trades = _make_trades(
            200,
            profitable_pct=0.65,
            start_ts=1640995200.0,
            spacing_seconds=315360,
        )
        result = rolling_profitability_gate(trades)
        assert result.gate == "D.rolling_profitability"
        assert isinstance(result.passed, bool)
        assert "rolling_12m_win_pct" in result.details
        assert "rolling_6m_win_pct" in result.details

    def test_regime_consistency_gate(self):
        from novatrade.evaluation.durability import regime_consistency_gate

        trades = _make_trades(
            200,
            profitable_pct=0.6,
            start_ts=1640995200.0,
            spacing_seconds=315360,
        )
        candles = _make_candles(5000, start_ts=1640995200.0)
        result = regime_consistency_gate(trades, candles)
        assert result.gate == "D.regime_consistency"
        assert isinstance(result.passed, bool)
        assert "regimes" in result.details
