"""Tests for TradingView data fetcher (Python wrapper)."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from novatrade.data.tradingview_fetcher import (
    TradingViewFetcher,
    TVBacktestReport,
    TVPerformance,
    TVTrade,
)
from novatrade.models import Candle

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MOCK_CANDLES_RESPONSE = {
    "ok": True,
    "symbol": "OANDA:EURUSD",
    "timeframe": "60",
    "candles": [
        {"timestamp": 1700000000, "open": 1.1000, "high": 1.1050, "low": 1.0990, "close": 1.1030, "volume": 1000},
        {"timestamp": 1700003600, "open": 1.1030, "high": 1.1060, "low": 1.1010, "close": 1.1045, "volume": 1200},
        {"timestamp": 1700007200, "open": 1.1045, "high": 1.1080, "low": 1.1020, "close": 1.1070, "volume": 800},
    ],
}

MOCK_BACKTEST_RESPONSE = {
    "ok": True,
    "symbol": "OANDA:EURUSD",
    "timeframe": "60",
    "strategy": "PUB;test123",
    "inputs": {"Length": 14, "ATR_Mult": 2.0},
    "trade_count": 2,
    "trades": [
        {
            "entry_name": "Long",
            "entry_type": "long",
            "entry_price": 1.1000,
            "entry_time": 1700000000,
            "exit_name": "Trail Stop",
            "exit_price": 1.1050,
            "exit_time": 1700010000,
            "quantity": 100000,
            "profit": {"rel": 0.0045, "abs": 500.0},
            "cumulative": {"rel": 0.0045, "abs": 500.0},
            "runup": {"rel": 0.008, "abs": 800.0},
            "drawdown": {"rel": -0.001, "abs": -100.0},
        },
        {
            "entry_name": "Short",
            "entry_type": "short",
            "entry_price": 1.1100,
            "entry_time": 1700020000,
            "exit_name": "Trail Stop",
            "exit_price": 1.1060,
            "exit_time": 1700030000,
            "quantity": 100000,
            "profit": {"rel": 0.0036, "abs": 400.0},
            "cumulative": {"rel": 0.0081, "abs": 900.0},
            "runup": {"rel": 0.005, "abs": 500.0},
            "drawdown": {"rel": -0.002, "abs": -200.0},
        },
    ],
    "performance": {
        "all": {
            "netProfit": 900.0,
            "netProfitPercent": 9.0,
            "totalTrades": 2,
            "percentProfitable": 100.0,
            "profitFactor": 9.0,
            "sharpeRatio": 2.5,
            "sortinoRatio": 3.0,
            "avgTrade": 450.0,
            "avgWinTrade": 450.0,
            "avgLosTrade": 0.0,
            "largestWinTrade": 500.0,
            "largestLosTrade": 0.0,
            "maxDrawDown": -200.0,
            "commissionPaid": 28.0,
        },
    },
    "equity_curve": [
        {"equity": 10000, "equityPercent": 0, "drawDown": 0},
        {"equity": 10500, "equityPercent": 5.0, "drawDown": 0},
        {"equity": 10900, "equityPercent": 9.0, "drawDown": 0},
    ],
    "settings": {"dateRange": "2020-01-01 to 2026-01-01"},
}

MOCK_INDICATOR_RESPONSE = {
    "ok": True,
    "symbol": "OANDA:EURUSD",
    "indicator": "STD;EMA",
    "inputs": {"Length": 50},
    "values": [
        {"timestamp": 1700000000, "EMA": 1.1020},
        {"timestamp": 1700003600, "EMA": 1.1025},
    ],
}


@pytest.fixture
def fetcher():
    return TradingViewFetcher(session="test_session", signature="test_sig")


# ---------------------------------------------------------------------------
# Tests — fetch_candles
# ---------------------------------------------------------------------------


class TestFetchCandles:
    def test_returns_candle_objects(self, fetcher):
        with patch.object(fetcher, "_run_cli", return_value=MOCK_CANDLES_RESPONSE):
            candles = fetcher.fetch_candles("OANDA:EURUSD", "60", bars=500)

        assert len(candles) == 3
        assert all(isinstance(c, Candle) for c in candles)

    def test_candle_values_correct(self, fetcher):
        with patch.object(fetcher, "_run_cli", return_value=MOCK_CANDLES_RESPONSE):
            candles = fetcher.fetch_candles("OANDA:EURUSD", "60")

        c = candles[0]
        assert c.timestamp == 1700000000
        assert c.open == 1.1000
        assert c.high == 1.1050
        assert c.low == 1.0990
        assert c.close == 1.1030
        assert c.volume == 1000

    def test_symbol_stripped(self, fetcher):
        with patch.object(fetcher, "_run_cli", return_value=MOCK_CANDLES_RESPONSE):
            candles = fetcher.fetch_candles("OANDA:EURUSD", "60")

        assert candles[0].symbol == "EURUSD"

    def test_timeframe_mapped(self, fetcher):
        with patch.object(fetcher, "_run_cli", return_value=MOCK_CANDLES_RESPONSE):
            candles = fetcher.fetch_candles("OANDA:EURUSD", "60")

        assert candles[0].timeframe == "H1"

    def test_sorted_ascending(self, fetcher):
        response = {
            **MOCK_CANDLES_RESPONSE,
            "candles": list(reversed(MOCK_CANDLES_RESPONSE["candles"])),
        }
        with patch.object(fetcher, "_run_cli", return_value=response):
            candles = fetcher.fetch_candles("OANDA:EURUSD", "60")

        timestamps = [c.timestamp for c in candles]
        assert timestamps == sorted(timestamps)

    def test_cli_called_with_correct_args(self, fetcher):
        with patch.object(fetcher, "_run_cli", return_value=MOCK_CANDLES_RESPONSE) as mock:
            fetcher.fetch_candles("BINANCE:BTCUSDT", "240", bars=1000)

        mock.assert_called_once_with(
            "fetch-candles",
            symbol="BINANCE:BTCUSDT",
            timeframe="240",
            bars=1000,
        )

    def test_h4_timeframe_mapping(self, fetcher):
        response = {**MOCK_CANDLES_RESPONSE, "timeframe": "240"}
        with patch.object(fetcher, "_run_cli", return_value=response):
            candles = fetcher.fetch_candles("OANDA:EURUSD", "240")

        assert candles[0].timeframe == "H4"


# ---------------------------------------------------------------------------
# Tests — run_backtest
# ---------------------------------------------------------------------------


class TestRunBacktest:
    def test_returns_report(self, fetcher):
        with patch.object(fetcher, "_run_cli", return_value=MOCK_BACKTEST_RESPONSE):
            report = fetcher.run_backtest(
                strategy_id="PUB;test123",
                overrides={"Length": 14},
            )

        assert isinstance(report, TVBacktestReport)
        assert report.trade_count == 2
        assert report.strategy_id == "PUB;test123"

    def test_trades_parsed(self, fetcher):
        with patch.object(fetcher, "_run_cli", return_value=MOCK_BACKTEST_RESPONSE):
            report = fetcher.run_backtest(strategy_id="PUB;test123")

        assert len(report.trades) == 2
        t0 = report.trades[0]
        assert isinstance(t0, TVTrade)
        assert t0.entry_type == "long"
        assert t0.entry_price == 1.1000
        assert t0.profit == {"rel": 0.0045, "abs": 500.0}

    def test_performance_parsed(self, fetcher):
        with patch.object(fetcher, "_run_cli", return_value=MOCK_BACKTEST_RESPONSE):
            report = fetcher.run_backtest(strategy_id="PUB;test123")

        p = report.performance
        assert isinstance(p, TVPerformance)
        assert p.net_profit == 900.0
        assert p.total_trades == 2
        assert p.win_rate == 100.0
        assert p.sharpe_ratio == 2.5
        assert p.commission_paid == 28.0

    def test_equity_curve_present(self, fetcher):
        with patch.object(fetcher, "_run_cli", return_value=MOCK_BACKTEST_RESPONSE):
            report = fetcher.run_backtest(strategy_id="PUB;test123")

        assert report.equity_curve is not None
        assert len(report.equity_curve) == 3

    def test_inputs_preserved(self, fetcher):
        with patch.object(fetcher, "_run_cli", return_value=MOCK_BACKTEST_RESPONSE):
            report = fetcher.run_backtest(
                strategy_id="PUB;test123",
                overrides={"Length": 14, "ATR_Mult": 2.0},
            )

        assert report.inputs == {"Length": 14, "ATR_Mult": 2.0}

    def test_strategy_id_required(self, fetcher):
        with pytest.raises(ValueError, match="strategy_id is required"):
            fetcher.run_backtest(strategy_id="")

    def test_zero_trade_report(self, fetcher):
        response = {
            **MOCK_BACKTEST_RESPONSE,
            "trade_count": 0,
            "trades": [],
            "performance": None,
        }
        with patch.object(fetcher, "_run_cli", return_value=response):
            report = fetcher.run_backtest(strategy_id="PUB;test123")

        assert report.trade_count == 0
        assert report.trades == []
        assert report.performance.net_profit == 0


# ---------------------------------------------------------------------------
# Tests — fetch_indicator
# ---------------------------------------------------------------------------


class TestFetchIndicator:
    def test_returns_values(self, fetcher):
        with patch.object(fetcher, "_run_cli", return_value=MOCK_INDICATOR_RESPONSE):
            values = fetcher.fetch_indicator(indicator="STD;EMA", options={"Length": 50})

        assert len(values) == 2
        assert values[0]["EMA"] == 1.1020

    def test_cli_called_with_options(self, fetcher):
        with patch.object(fetcher, "_run_cli", return_value=MOCK_INDICATOR_RESPONSE) as mock:
            fetcher.fetch_indicator(
                symbol="OANDA:EURUSD",
                timeframe="60",
                indicator="STD;EMA",
                options={"Length": 50},
                bars=300,
            )

        mock.assert_called_once_with(
            "fetch-indicator",
            symbol="OANDA:EURUSD",
            timeframe="60",
            indicator="STD;EMA",
            option={"Length": 50},
            bars=300,
        )


# ---------------------------------------------------------------------------
# Tests — sweep_parameters
# ---------------------------------------------------------------------------


class TestSweepParameters:
    def test_generates_combinations(self, fetcher):
        with patch.object(fetcher, "run_backtest", return_value=MagicMock(spec=TVBacktestReport)) as mock:
            results = fetcher.sweep_parameters(
                symbol="OANDA:EURUSD",
                timeframe="60",
                strategy_id="PUB;test123",
                param_grid={"Length": [10, 20], "Mult": [1.5, 2.0]},
            )

        assert len(results) == 4  # 2 x 2 = 4 combinations
        assert mock.call_count == 4

    def test_continues_on_failure(self, fetcher):
        call_count = 0

        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Connection failed")
            return MagicMock(spec=TVBacktestReport)

        with patch.object(fetcher, "run_backtest", side_effect=side_effect):
            results = fetcher.sweep_parameters(
                symbol="OANDA:EURUSD",
                timeframe="60",
                strategy_id="PUB;test123",
                param_grid={"Length": [10, 20, 30]},
            )

        assert len(results) == 2  # 3 total, 1 failed


# ---------------------------------------------------------------------------
# Tests — _run_cli
# ---------------------------------------------------------------------------


class TestRunCli:
    def test_timeout_raises(self, fetcher):
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="test", timeout=120)),
            pytest.raises(TimeoutError, match="timed out"),
        ):
            fetcher._run_cli("fetch-candles")

    def test_empty_output_raises(self, fetcher):
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = "some error"
        mock_result.returncode = 1
        with (
            patch("subprocess.run", return_value=mock_result),
            pytest.raises(RuntimeError, match="returned no output"),
        ):
            fetcher._run_cli("fetch-candles")

    def test_invalid_json_raises(self, fetcher):
        mock_result = MagicMock()
        mock_result.stdout = "not json"
        mock_result.returncode = 0
        with (
            patch("subprocess.run", return_value=mock_result),
            pytest.raises(RuntimeError, match="invalid JSON"),
        ):
            fetcher._run_cli("fetch-candles")

    def test_error_response_raises(self, fetcher):
        mock_result = MagicMock()
        mock_result.stdout = json.dumps({"error": "auth failed"})
        mock_result.returncode = 1
        with (
            patch("subprocess.run", return_value=mock_result),
            pytest.raises(RuntimeError, match="auth failed"),
        ):
            fetcher._run_cli("fetch-candles")


# ---------------------------------------------------------------------------
# Tests — TVPerformance
# ---------------------------------------------------------------------------


class TestTVPerformance:
    def test_from_none(self):
        p = TVPerformance.from_dict(None)
        assert p.net_profit == 0
        assert p.total_trades == 0

    def test_from_nested_all(self):
        p = TVPerformance.from_dict(MOCK_BACKTEST_RESPONSE["performance"])
        assert p.net_profit == 900.0
        assert p.sharpe_ratio == 2.5

    def test_from_flat_dict(self):
        flat = {"netProfit": 100, "totalTrades": 5, "percentProfitable": 60}
        p = TVPerformance.from_dict(flat)
        assert p.net_profit == 100
        assert p.total_trades == 5
