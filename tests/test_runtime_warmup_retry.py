"""Tests for enhanced warmup retry logic in runner.py"""

from unittest.mock import MagicMock, patch

import pytest

from novatrade.models import AccountMode, AccountState, SymbolPrice
from novatrade.runtime.runner import build_live_stack


class MockAdapter:
    """Mock adapter for testing warmup retry logic"""

    def __init__(self, fail_count: int = 0):
        self.fail_count = fail_count
        self.call_count = 0

    async def connect(self):
        return MagicMock(connected=True)

    async def get_account(self):
        return AccountState(balance=100000.0, equity=100000.0, margin=0.0, free_margin=100000.0, mode=AccountMode.DEMO)

    async def get_candles(self, symbol: str, timeframe: str, count: int):
        """Simulate candle fetch with configurable failures"""
        self.call_count += 1
        if self.call_count <= self.fail_count:
            if "500" in str(count):  # Primary candles
                raise ConnectionError(
                    f"SDK settings you use does not match the account region (attempt {self.call_count})"
                )
            else:  # Higher candles
                raise ConnectionError(f"Connection timeout (attempt {self.call_count})")

        # Success case - return mock candles
        candles = []
        for i in range(count):
            candles.append(
                MagicMock(
                    timestamp=1700000000 + i * 3600,
                    open=1.1000 + i * 0.0001,
                    high=1.1010 + i * 0.0001,
                    low=1.0990 + i * 0.0001,
                    close=1.1005 + i * 0.0001,
                    volume=100,
                )
            )
        return candles

    async def get_current_price(self, symbol: str):
        return SymbolPrice(symbol=symbol, bid=1.1000, ask=1.1002, spread=0.2)

    async def subscribe_to_market_data(self, symbols):
        pass

    async def get_positions(self):
        return []

    async def disconnect(self):
        pass


@pytest.mark.asyncio
async def test_warmup_success_first_attempt():
    """Test successful warmup on first attempt"""
    mock_adapter = MockAdapter(fail_count=0)

    with (
        patch("novatrade.adapter.metaapi_provider.MetaApiAdapter", return_value=mock_adapter),
        patch("novatrade.runtime.runner.NovaTradeCfg.load") as mock_load,
        patch("novatrade.runtime.runner.LiveEngine") as mock_engine,
        patch("novatrade.runtime.runner.StrategyAgent"),
        patch("novatrade.runtime.runner.MessageBus"),
        patch("novatrade.runtime.runner.DataPoller"),
        patch("novatrade.runtime.runner.BarAggregator"),
        patch("novatrade.runtime.runner.LiveLoop"),
    ):
        # Setup mocks
        mock_cfg = MagicMock()
        mock_cfg.symbols = ["EURUSD"]
        mock_cfg.metaapi.validate.return_value = []
        mock_cfg.ftmo.resolve_symbol.return_value = "EURUSD.sim"
        mock_load.return_value = mock_cfg

        mock_strategy_engine = MagicMock()
        mock_engine.return_value = mock_strategy_engine

        # Test warmup
        await build_live_stack(cfg=mock_cfg, dry_run=True)

        # Verify warmup was attempted only once
        assert mock_adapter.call_count == 2  # Primary + higher candles

        # Verify strategy engine was seeded
        mock_strategy_engine.seed_history.assert_called_once()


@pytest.mark.asyncio
async def test_warmup_retry_success_second_attempt():
    """Test successful warmup after one retry"""
    mock_adapter = MockAdapter(fail_count=1)  # Fail once, then succeed

    with (
        patch("novatrade.adapter.metaapi_provider.MetaApiAdapter", return_value=mock_adapter),
        patch("novatrade.runtime.runner.NovaTradeCfg.load") as mock_load,
        patch("novatrade.runtime.runner.LiveEngine") as mock_engine,
        patch("novatrade.runtime.runner.StrategyAgent"),
        patch("novatrade.runtime.runner.MessageBus"),
        patch("novatrade.runtime.runner.DataPoller"),
        patch("novatrade.runtime.runner.BarAggregator"),
        patch("novatrade.runtime.runner.LiveLoop"),
        patch("asyncio.sleep") as mock_sleep,
    ):  # Mock sleep to speed up test
        # Setup mocks
        mock_cfg = MagicMock()
        mock_cfg.symbols = ["EURUSD"]
        mock_cfg.metaapi.validate.return_value = []
        mock_cfg.ftmo.resolve_symbol.return_value = "EURUSD.sim"
        mock_load.return_value = mock_cfg

        mock_strategy_engine = MagicMock()
        mock_engine.return_value = mock_strategy_engine

        # Test warmup
        await build_live_stack(cfg=mock_cfg, dry_run=True)

        # Verify retry logic was used
        assert mock_adapter.call_count == 4  # 2 failed attempts + 2 successful attempts
        mock_sleep.assert_called_once_with(5)  # First retry delay

        # Verify strategy engine was eventually seeded
        mock_strategy_engine.seed_history.assert_called_once()


@pytest.mark.asyncio
async def test_warmup_failure_after_max_retries():
    """Test warmup failure after all retries exhausted"""
    mock_adapter = MockAdapter(fail_count=999)  # Always fail

    with (
        patch("novatrade.adapter.metaapi_provider.MetaApiAdapter", return_value=mock_adapter),
        patch("novatrade.runtime.runner.NovaTradeCfg.load") as mock_load,
        patch("novatrade.runtime.runner.LiveEngine") as mock_engine,
        patch("novatrade.runtime.runner.StrategyAgent"),
        patch("novatrade.runtime.runner.MessageBus"),
        patch("novatrade.runtime.runner.DataPoller"),
        patch("novatrade.runtime.runner.BarAggregator"),
        patch("novatrade.runtime.runner.LiveLoop"),
        patch("asyncio.sleep") as mock_sleep,
    ):  # Mock sleep to speed up test
        # Setup mocks
        mock_cfg = MagicMock()
        mock_cfg.symbols = ["EURUSD"]
        mock_cfg.metaapi.validate.return_value = []
        mock_cfg.ftmo.resolve_symbol.return_value = "EURUSD.sim"
        mock_load.return_value = mock_cfg

        mock_strategy_engine = MagicMock()
        mock_engine.return_value = mock_strategy_engine

        # Test warmup
        await build_live_stack(cfg=mock_cfg, dry_run=True)

        # Verify all retry attempts were made
        assert mock_adapter.call_count == 6  # 3 attempts × 2 candle calls each
        assert mock_sleep.call_count == 2  # 2 retry delays

        # Verify strategy engine was NOT seeded (warmup failed)
        mock_strategy_engine.seed_history.assert_not_called()


@pytest.mark.asyncio
async def test_warmup_insufficient_data_validation():
    """Test that insufficient candle data triggers retry"""

    class MockAdapterInsufficientData:
        def __init__(self):
            self.call_count = 0

        async def connect(self):
            return MagicMock(connected=True)

        async def get_account(self):
            return AccountState(
                balance=100000.0, equity=100000.0, margin=0.0, free_margin=100000.0, mode=AccountMode.DEMO
            )

        async def get_candles(self, symbol: str, timeframe: str, count: int):
            self.call_count += 1
            if self.call_count <= 2:  # First attempt - insufficient data
                return [MagicMock() for _ in range(10)]  # Only 10 candles (< 100 required)
            else:  # Second attempt - sufficient data
                return [MagicMock() for _ in range(count)]

        async def subscribe_to_market_data(self, symbols):
            pass

        async def get_positions(self):
            return []

    mock_adapter = MockAdapterInsufficientData()

    with (
        patch("novatrade.adapter.metaapi_provider.MetaApiAdapter", return_value=mock_adapter),
        patch("novatrade.runtime.runner.NovaTradeCfg.load") as mock_load,
        patch("novatrade.runtime.runner.LiveEngine") as mock_engine,
        patch("novatrade.runtime.runner.StrategyAgent"),
        patch("novatrade.runtime.runner.MessageBus"),
        patch("novatrade.runtime.runner.DataPoller"),
        patch("novatrade.runtime.runner.BarAggregator"),
        patch("novatrade.runtime.runner.LiveLoop"),
        patch("asyncio.sleep") as mock_sleep,
    ):
        # Setup mocks
        mock_cfg = MagicMock()
        mock_cfg.symbols = ["EURUSD"]
        mock_cfg.metaapi.validate.return_value = []
        mock_cfg.ftmo.resolve_symbol.return_value = "EURUSD.sim"
        mock_load.return_value = mock_cfg

        mock_strategy_engine = MagicMock()
        mock_engine.return_value = mock_strategy_engine

        # Test warmup
        await build_live_stack(cfg=mock_cfg, dry_run=True)

        # Verify retry was triggered by insufficient data validation
        assert mock_adapter.call_count == 4  # 2 failed + 2 successful calls
        mock_sleep.assert_called_once_with(5)  # Retry delay

        # Verify strategy engine was eventually seeded
        mock_strategy_engine.seed_history.assert_called_once()
