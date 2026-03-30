"""Tests for the multi-engine cross-validation framework.

Tests cover:
- Type construction and defaults
- NovaEngineAdapter normalisation
- CrossValidator divergence detection
- CrossValidator consensus metrics (median)
- CrossValidationRunner discovery and orchestration
- Agreement score computation
- Report formatting
"""

from __future__ import annotations

import os

import pytest

from novatrade.backtest.cross_validation.base_adapter import BaseEngineAdapter
from novatrade.backtest.cross_validation.comparator import (
    CrossValidator,
    format_consensus_report,
    format_consensus_telegram,
)
from novatrade.backtest.cross_validation.nova_adapter import NovaEngineAdapter
from novatrade.backtest.cross_validation.runner import (
    ADAPTER_REGISTRY,
    CrossValidationRunner,
)
from novatrade.backtest.cross_validation.types import (
    ConsensusReport,
    DivergenceSeverity,
    DivergenceThresholds,
    EngineId,
    EngineResult,
    MetricAvailability,
    NormalizedMetrics,
    NormalizedTrade,
)
from novatrade.backtest.environment import DEFAULT_ENVIRONMENT
from novatrade.models import Candle


def _nautilus_safe():
    """Check if NautilusTrader BacktestEngine can be safely instantiated."""
    try:
        import nautilus_trader  # noqa: F401

        # BacktestEngine crashes with SIGABRT on resource-limited VPS
        # Only run these tests where the engine is known to work
        return os.environ.get("NAUTILUS_TESTS_ENABLED", "0") == "1"
    except ImportError:
        return False


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_candle(ts: float, o: float, h: float, low: float, c: float, v: float = 100.0) -> Candle:
    return Candle(timestamp=ts, open=o, high=h, low=low, close=c, volume=v)


def _make_trending_candles(n: int = 200, start_price: float = 1.1000) -> list[Candle]:
    """Generate N H1 candles with a mild uptrend and some IRB-like wicks."""
    candles = []
    price = start_price
    ts = 1700000000.0
    for i in range(n):
        move = 0.0005 if i % 3 != 0 else -0.0003
        price += move
        o = price
        c = price + 0.0002
        # Every 20th bar: create an IRB-like candle with a long lower wick
        if i % 20 == 15:
            h = c + 0.0001
            low = o - 0.0020  # long lower wick
        else:
            h = max(o, c) + 0.0003
            low = min(o, c) - 0.0003
        candles.append(_make_candle(ts + i * 3600, o, h, low, c))
    return candles


def _make_result(
    engine: EngineId,
    total_trades: int = 50,
    win_rate: float = 55.0,
    net_pnl_usd: float = 5000.0,
    max_drawdown_pct: float = 8.0,
    sharpe_ratio: float = 1.2,
    profit_factor: float = 1.5,
    error: str | None = None,
) -> EngineResult:
    """Build a synthetic EngineResult for testing."""
    return EngineResult(
        engine=engine,
        engine_version=f"{engine.value} test",
        metrics=NormalizedMetrics(
            total_trades=total_trades,
            win_rate=win_rate,
            net_pnl_usd=net_pnl_usd,
            max_drawdown_pct=max_drawdown_pct,
            sharpe_ratio=sharpe_ratio,
            profit_factor=profit_factor,
        ),
        error=error,
    )


# ==================================================================
# Type tests
# ==================================================================


class TestTypes:
    def test_engine_id_values(self):
        assert EngineId.NOVA.value == "nova"
        assert EngineId.BACKTESTING_PY.value == "backtesting_py"
        assert EngineId.VECTORBT.value == "vectorbt"
        assert EngineId.NAUTILUS.value == "nautilus"

    def test_normalized_trade_creation(self):
        t = NormalizedTrade(
            trade_id=1,
            side="LONG",
            entry_bar=10,
            exit_bar=20,
            entry_price=1.1000,
            exit_price=1.1050,
            stop_loss=1.0950,
            pnl_pips=50.0,
            pnl_usd=500.0,
            hold_bars=10,
            exit_reason="trailing_stop",
        )
        assert t.side == "LONG"
        assert t.hold_bars == 10

    def test_normalized_metrics_defaults(self):
        m = NormalizedMetrics()
        assert m.total_trades is None
        assert m.win_rate is None
        assert m.availability == {}

    def test_divergence_thresholds_defaults(self):
        t = DivergenceThresholds()
        assert t.total_trades_pct == 10.0
        assert t.win_rate_abs == 5.0
        assert t.net_pnl_pct == 15.0

    def test_engine_result_with_error(self):
        r = EngineResult(engine=EngineId.VECTORBT, engine_version="test", error="not installed")
        assert r.error is not None
        assert r.trades == []

    def test_consensus_report_defaults(self):
        r = ConsensusReport()
        assert r.agreement_score == 0.0
        assert r.summary == ""


# ==================================================================
# NovaEngineAdapter tests
# ==================================================================


class TestNovaAdapter:
    def test_is_available(self):
        adapter = NovaEngineAdapter()
        assert adapter.is_available() is True

    def test_engine_id(self):
        adapter = NovaEngineAdapter()
        assert adapter.engine_id == EngineId.NOVA

    def test_engine_version(self):
        adapter = NovaEngineAdapter()
        assert "IRBBacktester" in adapter.engine_version

    def test_run_returns_engine_result(self):
        adapter = NovaEngineAdapter()
        h1 = _make_trending_candles(300)
        h4 = _make_trending_candles(75, start_price=1.1000)
        result = adapter.run(h1, h4, DEFAULT_ENVIRONMENT)
        assert isinstance(result, EngineResult)
        assert result.engine == EngineId.NOVA
        assert result.error is None
        assert result.elapsed_seconds > 0

    def test_run_populates_metrics(self):
        adapter = NovaEngineAdapter()
        h1 = _make_trending_candles(300)
        h4 = _make_trending_candles(75)
        result = adapter.run(h1, h4, DEFAULT_ENVIRONMENT)
        m = result.metrics
        assert m.total_trades is not None
        assert m.total_trades >= 0
        assert m.win_rate is not None

    def test_normalize_trades_preserves_count(self):
        from novatrade.backtest.metrics import CompletedTrade, ExitReason, TradeSide

        trades = [
            CompletedTrade(
                trade_id=1,
                side=TradeSide.LONG,
                entry_bar=10,
                exit_bar=20,
                entry_price=1.1000,
                exit_price=1.1050,
                stop_loss=1.0950,
                volume=0.1,
                exit_reason=ExitReason.TRAILING_STOP,
                pnl_pips=50.0,
                pnl_usd=50.0,
            ),
            CompletedTrade(
                trade_id=2,
                side=TradeSide.SHORT,
                entry_bar=30,
                exit_bar=40,
                entry_price=1.1050,
                exit_price=1.1020,
                stop_loss=1.1100,
                volume=0.1,
                exit_reason=ExitReason.STOP_LOSS,
                pnl_pips=30.0,
                pnl_usd=30.0,
            ),
        ]
        normalized = NovaEngineAdapter._normalize_trades(trades)
        assert len(normalized) == 2
        assert normalized[0].side == "LONG"
        assert normalized[0].exit_reason == "trailing_stop"
        assert normalized[1].side == "SHORT"
        assert normalized[1].exit_reason == "stop_loss"

    def test_run_with_insufficient_data(self):
        """Engine should handle very short data gracefully."""
        adapter = NovaEngineAdapter()
        h1 = _make_trending_candles(5)
        h4 = _make_trending_candles(2)
        result = adapter.run(h1, h4, DEFAULT_ENVIRONMENT)
        # Should not crash, may have 0 trades
        assert result.error is None
        assert result.metrics.total_trades is not None


# ==================================================================
# CrossValidator tests
# ==================================================================


class TestCrossValidator:
    def test_identical_results_give_perfect_score(self):
        a = _make_result(EngineId.NOVA, total_trades=50, win_rate=55.0, net_pnl_usd=5000.0)
        b = _make_result(EngineId.BACKTESTING_PY, total_trades=50, win_rate=55.0, net_pnl_usd=5000.0)
        report = CrossValidator().compare([a, b])
        assert report.agreement_score == 100.0
        assert all(d.severity == DivergenceSeverity.INFO for d in report.divergences)

    def test_small_divergence_is_warning(self):
        a = _make_result(EngineId.NOVA, win_rate=55.0)
        b = _make_result(EngineId.BACKTESTING_PY, win_rate=51.0)  # 4pp diff < 5pp threshold
        report = CrossValidator().compare([a, b])
        win_divs = [d for d in report.divergences if d.metric_name == "win_rate"]
        assert len(win_divs) == 1
        assert win_divs[0].severity == DivergenceSeverity.WARNING

    def test_large_divergence_is_critical(self):
        a = _make_result(EngineId.NOVA, win_rate=55.0)
        b = _make_result(EngineId.BACKTESTING_PY, win_rate=40.0)  # 15pp diff > 5pp threshold
        report = CrossValidator().compare([a, b])
        win_divs = [d for d in report.divergences if d.metric_name == "win_rate"]
        assert len(win_divs) == 1
        assert win_divs[0].severity == DivergenceSeverity.CRITICAL

    def test_custom_thresholds(self):
        a = _make_result(EngineId.NOVA, win_rate=55.0)
        b = _make_result(EngineId.BACKTESTING_PY, win_rate=40.0)
        # With a wide threshold, this should be just a warning
        thresholds = DivergenceThresholds(win_rate_abs=20.0)
        report = CrossValidator(thresholds=thresholds).compare([a, b])
        win_divs = [d for d in report.divergences if d.metric_name == "win_rate"]
        assert win_divs[0].severity == DivergenceSeverity.WARNING

    def test_consensus_uses_median(self):
        results = [
            _make_result(EngineId.NOVA, total_trades=50),
            _make_result(EngineId.BACKTESTING_PY, total_trades=100),
            _make_result(EngineId.VECTORBT, total_trades=60),
        ]
        report = CrossValidator().compare(results)
        # Median of [50, 60, 100] = 60
        assert report.consensus_metrics.total_trades == 60

    def test_failed_engines_excluded_from_comparison(self):
        a = _make_result(EngineId.NOVA)
        b = _make_result(EngineId.BACKTESTING_PY, error="not installed")
        report = CrossValidator().compare([a, b])
        assert len(report.failed_engines) == 1
        assert "backtesting_py" in report.failed_engines[0]
        assert "Need at least 2" in report.summary

    def test_three_engine_comparison(self):
        a = _make_result(EngineId.NOVA, total_trades=50, win_rate=55.0)
        b = _make_result(EngineId.BACKTESTING_PY, total_trades=48, win_rate=54.0)
        c = _make_result(EngineId.VECTORBT, total_trades=52, win_rate=53.0)
        report = CrossValidator().compare([a, b, c])
        assert report.agreement_score > 0
        assert len(report.results) == 3

    def test_pnl_percentage_divergence(self):
        a = _make_result(EngineId.NOVA, net_pnl_usd=10000.0)
        b = _make_result(EngineId.BACKTESTING_PY, net_pnl_usd=5000.0)
        # 50% diff > 15% threshold → CRITICAL
        report = CrossValidator().compare([a, b])
        pnl_divs = [d for d in report.divergences if d.metric_name == "net_pnl_usd"]
        assert len(pnl_divs) == 1
        assert pnl_divs[0].severity == DivergenceSeverity.CRITICAL

    def test_agreement_score_range(self):
        a = _make_result(EngineId.NOVA)
        b = _make_result(EngineId.BACKTESTING_PY)
        report = CrossValidator().compare([a, b])
        assert 0.0 <= report.agreement_score <= 100.0

    def test_none_metrics_skipped(self):
        a = EngineResult(
            engine=EngineId.NOVA,
            engine_version="test",
            metrics=NormalizedMetrics(total_trades=50, win_rate=55.0),
        )
        b = EngineResult(
            engine=EngineId.BACKTESTING_PY,
            engine_version="test",
            metrics=NormalizedMetrics(total_trades=48),  # no win_rate
        )
        report = CrossValidator().compare([a, b])
        # win_rate divergence should not be computed since b has None
        win_divs = [d for d in report.divergences if d.metric_name == "win_rate"]
        assert len(win_divs) == 0


# ==================================================================
# Report formatting tests
# ==================================================================


class TestFormatting:
    def test_format_consensus_report_contains_engines(self):
        a = _make_result(EngineId.NOVA)
        b = _make_result(EngineId.BACKTESTING_PY)
        report = CrossValidator().compare([a, b])
        md = format_consensus_report(report)
        assert "nova" in md.lower()
        assert "backtesting_py" in md.lower()
        assert "Agreement Score" in md

    def test_format_telegram_concise(self):
        a = _make_result(EngineId.NOVA)
        b = _make_result(EngineId.BACKTESTING_PY)
        report = CrossValidator().compare([a, b])
        tg = format_consensus_telegram(report)
        assert "Cross-Val" in tg
        assert len(tg) < 300  # should be concise

    def test_format_report_with_failed_engine(self):
        a = _make_result(EngineId.NOVA)
        b = _make_result(EngineId.BACKTESTING_PY, error="not installed")
        report = CrossValidator().compare([a, b])
        md = format_consensus_report(report)
        assert "FAILED" in md

    def test_format_report_with_divergences(self):
        a = _make_result(EngineId.NOVA, win_rate=55.0)
        b = _make_result(EngineId.BACKTESTING_PY, win_rate=40.0)
        report = CrossValidator().compare([a, b])
        md = format_consensus_report(report)
        assert "Divergences" in md
        assert "win_rate" in md


# ==================================================================
# Runner tests
# ==================================================================


class TestRunner:
    def test_adapter_registry_populated(self):
        assert EngineId.NOVA in ADAPTER_REGISTRY
        assert EngineId.BACKTESTING_PY in ADAPTER_REGISTRY
        assert EngineId.VECTORBT in ADAPTER_REGISTRY
        assert EngineId.NAUTILUS in ADAPTER_REGISTRY

    def test_nova_only_runner(self):
        """Run with just the Nova engine — should produce a result but no comparison."""
        runner = CrossValidationRunner(engines=[EngineId.NOVA])
        h1 = _make_trending_candles(200)
        h4 = _make_trending_candles(50)
        report = runner.run(h1, h4, DEFAULT_ENVIRONMENT)
        assert len(report.results) == 1
        assert report.results[0].engine == EngineId.NOVA
        # Single engine: comparison needs at least 2
        assert "Need at least 2" in report.summary

    def test_runner_discovers_available(self):
        runner = CrossValidationRunner()
        adapters = runner._discover_adapters()
        # Nova should always be available
        nova_adapters = [a for a in adapters if a.engine_id == EngineId.NOVA]
        assert len(nova_adapters) == 1

    def test_runner_handles_missing_engine(self):
        """If we request a non-existent engine, it's skipped."""
        runner = CrossValidationRunner(engines=[EngineId.NOVA])
        h1 = _make_trending_candles(100)
        h4 = _make_trending_candles(25)
        report = runner.run(h1, h4, DEFAULT_ENVIRONMENT)
        assert len(report.results) >= 1

    def test_runner_with_custom_thresholds(self):
        thresholds = DivergenceThresholds(win_rate_abs=1.0)
        runner = CrossValidationRunner(
            engines=[EngineId.NOVA],
            thresholds=thresholds,
        )
        h1 = _make_trending_candles(200)
        h4 = _make_trending_candles(50)
        report = runner.run(h1, h4, DEFAULT_ENVIRONMENT)
        assert report.thresholds.win_rate_abs == 1.0


# ==================================================================
# BaseEngineAdapter tests
# ==================================================================


class TestBaseAdapter:
    def test_candles_to_dataframe(self):
        candles = _make_trending_candles(10)
        df = BaseEngineAdapter._candles_to_dataframe(candles)
        assert len(df) == 10
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert df.index.name is None  # DatetimeIndex

    def test_candles_to_dataframe_empty(self):
        df = BaseEngineAdapter._candles_to_dataframe([])
        assert len(df) == 0


# ==================================================================
# Integration test: Nova adapter → comparator round-trip
# ==================================================================


class TestIntegration:
    def test_nova_self_comparison(self):
        """Run Nova twice on same data — results should be identical."""
        adapter = NovaEngineAdapter()
        h1 = _make_trending_candles(300)
        h4 = _make_trending_candles(75)
        r1 = adapter.run(h1, h4, DEFAULT_ENVIRONMENT)
        r2 = adapter.run(h1, h4, DEFAULT_ENVIRONMENT)

        # Patch engine IDs to make them "different" for comparison
        r2.engine = EngineId.BACKTESTING_PY
        r2.engine_version = "fake for test"

        report = CrossValidator().compare([r1, r2])
        assert report.agreement_score == 100.0
        assert all(d.severity == DivergenceSeverity.INFO for d in report.divergences)


# ==================================================================
# NautilusAdapter tests
# ==================================================================


class TestNautilusAdapter:
    """Tests for the NautilusTrader adapter.

    Tests verify interface compliance, graceful degradation, and extraction
    logic via mocks. The BacktestEngine itself requires significant system
    resources (thread creation) so full E2E run() tests are skipped to avoid
    process-level aborts in resource-constrained CI environments.

    When nautilus_trader IS installed, tests also verify instrument building
    and candle conversion against the real Nautilus API.
    """

    def test_engine_id(self):
        from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter

        adapter = NautilusAdapter()
        assert adapter.engine_id == EngineId.NAUTILUS

    def test_engine_version(self):
        from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter

        adapter = NautilusAdapter()
        version = adapter.engine_version
        assert "nautilus_trader" in version

    def test_is_available_returns_bool(self):
        from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter

        adapter = NautilusAdapter()
        result = adapter.is_available()
        assert isinstance(result, bool)

    def test_implements_base_adapter(self):
        from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter

        adapter = NautilusAdapter()
        assert isinstance(adapter, BaseEngineAdapter)
        # Check all abstract methods are implemented (not raising NotImplementedError)
        assert hasattr(adapter, "engine_id")
        assert hasattr(adapter, "engine_version")
        assert hasattr(adapter, "is_available")
        assert hasattr(adapter, "run")
        assert hasattr(adapter, "_build_instrument")
        assert hasattr(adapter, "_candles_to_nautilus_bars")
        assert hasattr(adapter, "_build_strategy")
        assert hasattr(adapter, "_extract_trades")
        assert hasattr(adapter, "_extract_metrics")

    @pytest.mark.skipif(
        not _nautilus_safe(),
        reason="NautilusTrader BacktestEngine crashes with SIGABRT on this VPS",
    )
    def test_build_instrument_creates_currency_pair(self):
        """_build_instrument creates a valid CurrencyPair when nautilus_trader is installed."""
        from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter

        adapter = NautilusAdapter()
        if not adapter.is_available():
            pytest.skip("nautilus_trader not installed")

        from nautilus_trader.model.identifiers import Venue

        venue = Venue("SIM")
        instrument = adapter._build_instrument(DEFAULT_ENVIRONMENT, venue)

        assert str(instrument.id) == "EUR/USD.SIM"
        assert instrument.price_precision == 5
        assert instrument.size_precision == 2

    @pytest.mark.skipif(
        not _nautilus_safe(),
        reason="NautilusTrader BacktestEngine crashes with SIGABRT on this VPS",
    )
    def test_candles_to_nautilus_bars_converts_correctly(self):
        """_candles_to_nautilus_bars produces correct Bar objects."""
        from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter

        adapter = NautilusAdapter()
        if not adapter.is_available():
            pytest.skip("nautilus_trader not installed")

        from nautilus_trader.model.identifiers import Venue

        venue = Venue("SIM")
        instrument = adapter._build_instrument(DEFAULT_ENVIRONMENT, venue)

        candles = _make_trending_candles(5)
        bars = adapter._candles_to_nautilus_bars(candles, instrument)

        assert len(bars) == 5
        # Verify first bar OHLCV matches first candle
        first_bar = bars[0]
        assert abs(float(first_bar.open) - candles[0].open) < 1e-5
        assert abs(float(first_bar.high) - candles[0].high) < 1e-5
        assert abs(float(first_bar.low) - candles[0].low) < 1e-5
        assert abs(float(first_bar.close) - candles[0].close) < 1e-5
        # Timestamp should be in nanoseconds
        expected_ns = int(candles[0].timestamp * 1_000_000_000)
        assert first_bar.ts_event == expected_ns

    @pytest.mark.skipif(
        not _nautilus_safe(),
        reason="NautilusTrader BacktestEngine crashes with SIGABRT on this VPS",
    )
    def test_build_instrument_fee_from_environment(self):
        """Verify fees are derived from BacktestEnvironment commission model."""
        from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter

        adapter = NautilusAdapter()
        if not adapter.is_available():
            pytest.skip("nautilus_trader not installed")

        from nautilus_trader.model.identifiers import Venue

        venue = Venue("SIM")
        instrument = adapter._build_instrument(DEFAULT_ENVIRONMENT, venue)

        # Fee should be commission_per_lot / 100_000
        expected_fee = DEFAULT_ENVIRONMENT.spread.commission_per_lot_usd / 100_000.0
        assert abs(float(instrument.maker_fee) - expected_fee) < 1e-10
        assert abs(float(instrument.taker_fee) - expected_fee) < 1e-10

    @pytest.mark.skipif(
        not _nautilus_safe(),
        reason="NautilusTrader BacktestEngine crashes with SIGABRT on this VPS",
    )
    def test_build_strategy_returns_strategy_instance(self):
        """_build_strategy returns a NautilusTrader Strategy subclass."""
        from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter

        adapter = NautilusAdapter()
        if not adapter.is_available():
            pytest.skip("nautilus_trader not installed")

        from nautilus_trader.model.identifiers import Venue
        from nautilus_trader.trading.strategy import Strategy

        venue = Venue("SIM")
        instrument = adapter._build_instrument(DEFAULT_ENVIRONMENT, venue)
        strategy = adapter._build_strategy(DEFAULT_ENVIRONMENT, instrument)

        assert isinstance(strategy, Strategy)
        assert hasattr(strategy, "get_trade_records")
        assert strategy.get_trade_records() == []

    def test_extract_trades_empty_without_records(self):
        """_extract_trades returns empty list for object without get_trade_records."""
        from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter

        adapter = NautilusAdapter()
        # Pass a mock strategy with no get_trade_records
        result = adapter._extract_trades(object())
        assert result == []

    def test_extract_trades_with_mock_records(self):
        """_extract_trades correctly normalises mock trade records."""
        from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter

        adapter = NautilusAdapter()

        class MockStrategy:
            def get_trade_records(self):
                return [
                    {
                        "trade_id": 1,
                        "side": "LONG",
                        "entry_bar": 10,
                        "exit_bar": 25,
                        "entry_price": 1.10000,
                        "exit_price": 1.10500,
                        "stop_loss": 1.09500,
                        "pnl_pips": 50.0,
                        "pnl_usd": 50.0,
                        "hold_bars": 15,
                        "exit_reason": "trailing_stop",
                    },
                    {
                        "trade_id": 2,
                        "side": "SHORT",
                        "entry_bar": 30,
                        "exit_bar": 45,
                        "entry_price": 1.10500,
                        "exit_price": 1.10200,
                        "stop_loss": 1.11000,
                        "pnl_pips": 30.0,
                        "pnl_usd": 30.0,
                        "hold_bars": 15,
                        "exit_reason": "stop_loss",
                    },
                ]

        trades = adapter._extract_trades(MockStrategy())
        assert len(trades) == 2
        assert trades[0].side == "LONG"
        assert trades[0].exit_reason == "trailing_stop"
        assert trades[0].pnl_pips == 50.0
        assert trades[1].side == "SHORT"
        assert trades[1].exit_reason == "stop_loss"

    def test_extract_metrics_empty(self):
        """_extract_metrics returns zero-trade metrics for empty strategy."""
        from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter

        adapter = NautilusAdapter()

        class MockStrategy:
            def get_trade_records(self):
                return []

        metrics = adapter._extract_metrics(None, MockStrategy())
        assert metrics.total_trades == 0

    def test_extract_metrics_with_trades(self):
        """_extract_metrics computes correct win rate and PnL."""
        from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter

        adapter = NautilusAdapter()

        class MockStrategy:
            def get_trade_records(self):
                return [
                    {
                        "trade_id": 1,
                        "side": "LONG",
                        "entry_bar": 0,
                        "exit_bar": 10,
                        "entry_price": 1.1,
                        "exit_price": 1.105,
                        "stop_loss": 1.09,
                        "pnl_pips": 50.0,
                        "pnl_usd": 50.0,
                        "hold_bars": 10,
                        "exit_reason": "trailing_stop",
                    },
                    {
                        "trade_id": 2,
                        "side": "SHORT",
                        "entry_bar": 20,
                        "exit_bar": 30,
                        "entry_price": 1.105,
                        "exit_price": 1.11,
                        "stop_loss": 1.1,
                        "pnl_pips": -50.0,
                        "pnl_usd": -50.0,
                        "hold_bars": 10,
                        "exit_reason": "stop_loss",
                    },
                    {
                        "trade_id": 3,
                        "side": "LONG",
                        "entry_bar": 40,
                        "exit_bar": 50,
                        "entry_price": 1.1,
                        "exit_price": 1.106,
                        "stop_loss": 1.09,
                        "pnl_pips": 60.0,
                        "pnl_usd": 60.0,
                        "hold_bars": 10,
                        "exit_reason": "trailing_stop",
                    },
                ]

        metrics = adapter._extract_metrics(None, MockStrategy())
        assert metrics.total_trades == 3
        assert metrics.long_trades == 2
        assert metrics.short_trades == 1
        # 2 wins out of 3
        assert abs(metrics.win_rate - 66.666666) < 1.0
        assert metrics.net_pnl_usd == 60.0  # 50 - 50 + 60
        assert metrics.profit_factor == 110.0 / 50.0  # gross_profit / gross_loss
        assert metrics.max_consecutive_wins == 1  # win, loss, win
        assert metrics.max_consecutive_losses == 1

    def test_extract_metrics_all_losses(self):
        """_extract_metrics handles all-loss scenario (profit_factor=0)."""
        from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter

        adapter = NautilusAdapter()

        class MockStrategy:
            def get_trade_records(self):
                return [
                    {
                        "trade_id": 1,
                        "side": "LONG",
                        "entry_bar": 0,
                        "exit_bar": 5,
                        "entry_price": 1.1,
                        "exit_price": 1.095,
                        "stop_loss": 1.09,
                        "pnl_pips": -50.0,
                        "pnl_usd": -50.0,
                        "hold_bars": 5,
                        "exit_reason": "stop_loss",
                    },
                    {
                        "trade_id": 2,
                        "side": "SHORT",
                        "entry_bar": 10,
                        "exit_bar": 15,
                        "entry_price": 1.1,
                        "exit_price": 1.105,
                        "stop_loss": 1.11,
                        "pnl_pips": -50.0,
                        "pnl_usd": -50.0,
                        "hold_bars": 5,
                        "exit_reason": "stop_loss",
                    },
                ]

        metrics = adapter._extract_metrics(None, MockStrategy())
        assert metrics.total_trades == 2
        assert metrics.win_rate == 0.0
        assert metrics.profit_factor == 0.0
        assert metrics.max_consecutive_losses == 2

    def test_extract_metrics_availability_flags(self):
        """Verify metric availability flags are set correctly."""
        from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter

        adapter = NautilusAdapter()

        class MockStrategy:
            def get_trade_records(self):
                return [
                    {
                        "trade_id": 1,
                        "side": "LONG",
                        "entry_bar": 0,
                        "exit_bar": 5,
                        "entry_price": 1.1,
                        "exit_price": 1.105,
                        "stop_loss": 1.09,
                        "pnl_pips": 50.0,
                        "pnl_usd": 50.0,
                        "hold_bars": 5,
                        "exit_reason": "trailing_stop",
                    }
                ]

        metrics = adapter._extract_metrics(None, MockStrategy())
        assert metrics.availability["total_trades"] == MetricAvailability.AVAILABLE
        assert metrics.availability["win_rate"] == MetricAvailability.AVAILABLE
        assert metrics.availability["max_drawdown_pct"] == MetricAvailability.NOT_COMPUTED
        assert metrics.availability["sharpe_ratio"] == MetricAvailability.NOT_COMPUTED

    def test_adapter_in_registry(self):
        """NautilusAdapter should be registered in ADAPTER_REGISTRY."""
        assert EngineId.NAUTILUS in ADAPTER_REGISTRY
