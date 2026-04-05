"""Tests for stepped trailing stop with cooldown functionality."""

import pytest

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle
from novatrade.strategies.base import BaseStrategy
from novatrade.strategy.live_engine import LiveStrategyEngine, SignalType


class MockStrategy(BaseStrategy):
    """Mock strategy for testing."""

    def __init__(self, env):
        self.env = env

    def check_entry(self, i, h1_candles, indicators, h4_candles=None):
        return None

    def check_exit(self, i, h1_candles, indicators, position_dict):
        return None

    def compute_indicators(self, h1_candles):
        # Return mock ATR values
        return {"atr": [0.001] * len(h1_candles)}

    def generate_signals(self, h1_candles, h4_candles=None):
        """Required abstract method."""
        return []

    def get_default_doctrine(self):
        """Required abstract method."""
        return {}

    def get_parameter_bounds(self):
        """Required abstract method."""
        return {}


@pytest.fixture
def engine_with_stepped_trailing():
    """Create live engine with stepped trailing configuration."""
    env = BacktestEnvironment(
        trail_atr_multiplier=2.0,
        trail_step_pips=10.0,
        trail_cooldown_bars=4,
        pip_value=0.0001,
    )
    strategy = MockStrategy(env)
    engine = LiveStrategyEngine(strategy, env)

    # Seed with initial data
    candles = [
        Candle(timestamp=f"2024-01-01T{i:02d}:00:00", open=1.1000, high=1.1010, low=1.0990, close=1.1005, volume=100)
        for i in range(50)
    ]
    engine.seed_history(candles)

    return engine


def test_stepped_trailing_stop_threshold():
    """Test that trailing stop only updates when step threshold is exceeded."""
    env = BacktestEnvironment(
        trail_atr_multiplier=2.0,
        trail_step_pips=10.0,  # 10 pips = 0.001 for EURUSD
        trail_cooldown_bars=1,  # Minimal cooldown for this test
        pip_value=0.0001,
    )
    strategy = MockStrategy(env)
    engine = LiveStrategyEngine(strategy, env)

    # Seed with initial data
    candles = [
        Candle(timestamp=f"2024-01-01T{i:02d}:00:00", open=1.1000, high=1.1010, low=1.0990, close=1.1005, volume=100)
        for i in range(50)
    ]
    engine.seed_history(candles)

    # Simulate a position
    engine.recover_position_state("LONG", 1.1000, 1.0980, 0.10)  # LONG position with 20-pip stop

    # Test small movement (< 10 pips) - should NOT trigger update
    small_move_bar = Candle(
        timestamp="2024-01-01T10:00:00", open=1.1005, high=1.1008, low=1.1003, close=1.1007, volume=100
    )
    signals = engine.on_bar(small_move_bar, "H1")

    # Should have no MODIFY_SL signals
    modify_signals = [s for s in signals if s.signal_type == SignalType.MODIFY_SL]
    assert len(modify_signals) == 0, "Small movement should not trigger SL modification"

    # Test large movement (>= 10 pips) - should trigger update
    large_move_bar = Candle(
        timestamp="2024-01-01T11:00:00", open=1.1007, high=1.1020, low=1.1005, close=1.1018, volume=100
    )
    signals = engine.on_bar(large_move_bar, "H1")

    # Should have exactly 1 MODIFY_SL signal
    modify_signals = [s for s in signals if s.signal_type == SignalType.MODIFY_SL]
    assert len(modify_signals) == 1, "Large movement should trigger SL modification"
    assert modify_signals[0].metadata["step_pips"] >= 10.0, "Step size should be at least 10 pips"


def test_cooldown_period():
    """Test that cooldown period prevents rapid SL modifications."""
    env = BacktestEnvironment(
        trail_atr_multiplier=2.0,
        trail_step_pips=5.0,  # Small step to ensure threshold is met
        trail_cooldown_bars=4,  # 4-bar cooldown
        pip_value=0.0001,
    )
    strategy = MockStrategy(env)
    engine = LiveStrategyEngine(strategy, env)

    # Seed with initial data
    candles = [
        Candle(timestamp=f"2024-01-01T{i:02d}:00:00", open=1.1000, high=1.1010, low=1.0990, close=1.1005, volume=100)
        for i in range(50)
    ]
    engine.seed_history(candles)

    # Simulate a position
    engine.recover_position_state("LONG", 1.1000, 1.0980, 0.10)  # LONG position with 20-pip stop

    # First large movement - should trigger update
    bar1 = Candle(timestamp="2024-01-01T10:00:00", open=1.1005, high=1.1020, low=1.1003, close=1.1018, volume=100)
    signals1 = engine.on_bar(bar1, "H1")
    modify_signals1 = [s for s in signals1 if s.signal_type == SignalType.MODIFY_SL]
    assert len(modify_signals1) == 1, "First large movement should trigger SL modification"

    # Additional movements within cooldown period - should NOT trigger updates
    for i in range(3):  # 3 bars within 4-bar cooldown
        bar = Candle(
            timestamp=f"2024-01-01T{11 + i}:00:00",
            open=1.1018,
            high=1.1030,
            low=1.1015,
            close=1.1025 + i * 0.001,
            volume=100,
        )
        signals = engine.on_bar(bar, "H1")
        modify_signals = [s for s in signals if s.signal_type == SignalType.MODIFY_SL]
        assert len(modify_signals) == 0, f"Movement within cooldown (bar {i + 1}) should not trigger SL modification"

    # Movement after cooldown period - should trigger update
    bar_after_cooldown = Candle(
        timestamp="2024-01-01T14:00:00", open=1.1028, high=1.1040, low=1.1025, close=1.1035, volume=100
    )
    signals_after = engine.on_bar(bar_after_cooldown, "H1")
    modify_signals_after = [s for s in signals_after if s.signal_type == SignalType.MODIFY_SL]
    assert len(modify_signals_after) == 1, "Movement after cooldown should trigger SL modification"


def test_jitter_metadata_included():
    """Test that timing jitter is included in signal metadata."""
    env = BacktestEnvironment(
        trail_atr_multiplier=2.0,
        trail_step_pips=5.0,
        trail_cooldown_bars=1,
        pip_value=0.0001,
    )
    strategy = MockStrategy(env)
    engine = LiveStrategyEngine(strategy, env)

    # Seed with initial data
    candles = [
        Candle(timestamp=f"2024-01-01T{i:02d}:00:00", open=1.1000, high=1.1010, low=1.0990, close=1.1005, volume=100)
        for i in range(50)
    ]
    engine.seed_history(candles)

    # Simulate a position
    engine.recover_position_state("LONG", 1.1000, 1.0980, 0.10)  # LONG position with 20-pip stop

    # Trigger a trailing stop update
    bar = Candle(timestamp="2024-01-01T10:00:00", open=1.1005, high=1.1020, low=1.1003, close=1.1018, volume=100)
    signals = engine.on_bar(bar, "H1")

    modify_signals = [s for s in signals if s.signal_type == SignalType.MODIFY_SL]
    assert len(modify_signals) == 1, "Should trigger SL modification"

    signal = modify_signals[0]
    metadata = signal.metadata

    # Check required metadata fields
    assert "jitter_seconds" in metadata, "Jitter should be included in metadata"
    assert "step_pips" in metadata, "Step size should be included in metadata"
    assert "cooldown_bars" in metadata, "Cooldown period should be included in metadata"

    # Check jitter is within expected range
    jitter = metadata["jitter_seconds"]
    assert 5.0 <= jitter <= 15.0, f"Jitter {jitter} should be between 5-15 seconds"


def test_short_position_stepped_trailing():
    """Test stepped trailing stop for SHORT positions."""
    env = BacktestEnvironment(
        trail_atr_multiplier=2.0,
        trail_step_pips=10.0,
        trail_cooldown_bars=2,
        pip_value=0.0001,
    )
    strategy = MockStrategy(env)
    engine = LiveStrategyEngine(strategy, env)

    # Seed with initial data
    candles = [
        Candle(timestamp=f"2024-01-01T{i:02d}:00:00", open=1.1000, high=1.1010, low=1.0990, close=1.1005, volume=100)
        for i in range(50)
    ]
    engine.seed_history(candles)

    # Simulate a SHORT position
    engine.recover_position_state("SHORT", 1.1000, 1.1020, 0.10)  # SHORT position with 20-pip stop

    # Test small downward movement (< 10 pips) - should NOT trigger update
    small_move_bar = Candle(
        timestamp="2024-01-01T10:00:00", open=1.1000, high=1.0998, low=1.0993, close=1.0995, volume=100
    )
    signals = engine.on_bar(small_move_bar, "H1")

    # Should have no MODIFY_SL signals
    modify_signals = [s for s in signals if s.signal_type == SignalType.MODIFY_SL]
    assert len(modify_signals) == 0, "Small movement should not trigger SL modification"

    # Test large downward movement (>= 10 pips) - should trigger update
    large_move_bar = Candle(
        timestamp="2024-01-01T11:00:00", open=1.0995, high=1.0992, low=1.0982, close=1.0985, volume=100
    )
    signals = engine.on_bar(large_move_bar, "H1")

    # Should have exactly 1 MODIFY_SL signal
    modify_signals = [s for s in signals if s.signal_type == SignalType.MODIFY_SL]
    assert len(modify_signals) == 1, "Large downward movement should trigger SL modification"
    assert modify_signals[0].metadata["step_pips"] >= 10.0, "Step size should be at least 10 pips"


def test_configuration_parameter_validation():
    """Test that new configuration parameters are properly validated."""
    # Test valid configuration
    env = BacktestEnvironment(
        trail_step_pips=10.0,
        trail_cooldown_bars=4,
    )
    assert env.trail_step_pips == 10.0
    assert env.trail_cooldown_bars == 4

    # Test default values
    env_default = BacktestEnvironment()
    assert env_default.trail_step_pips == 10.0
    assert env_default.trail_cooldown_bars == 4


def test_modification_frequency_reduction():
    """Test that the stepped trailing reduces modification frequency."""
    # Test traditional trailing (step = 0.1 pip, cooldown = 1)
    env_traditional = BacktestEnvironment(
        trail_atr_multiplier=2.0,
        trail_step_pips=0.1,  # Very small step
        trail_cooldown_bars=1,  # Minimal cooldown
        pip_value=0.0001,
    )
    strategy_traditional = MockStrategy(env_traditional)
    engine_traditional = LiveStrategyEngine(strategy_traditional, env_traditional)

    # Test stepped trailing (step = 10 pips, cooldown = 4)
    env_stepped = BacktestEnvironment(
        trail_atr_multiplier=2.0,
        trail_step_pips=10.0,  # Large step
        trail_cooldown_bars=4,  # Longer cooldown
        pip_value=0.0001,
    )
    strategy_stepped = MockStrategy(env_stepped)
    engine_stepped = LiveStrategyEngine(strategy_stepped, env_stepped)

    # Seed both engines
    candles = [
        Candle(timestamp=f"2024-01-01T{i:02d}:00:00", open=1.1000, high=1.1010, low=1.0990, close=1.1005, volume=100)
        for i in range(50)
    ]
    engine_traditional.seed_history(candles)
    engine_stepped.seed_history(candles)

    # Simulate positions
    engine_traditional.recover_position_state("LONG", 1.1000, 1.0980, 0.10)
    engine_stepped.recover_position_state("LONG", 1.1000, 1.0980, 0.10)

    traditional_modifications = 0
    stepped_modifications = 0

    # Simulate gradual price movement over 20 bars
    for i in range(20):
        price = 1.1005 + i * 0.0002  # Gradual increase, 2 pips per bar
        bar = Candle(
            timestamp=f"2024-01-01T{10 + i}:00:00",
            open=price,
            high=price + 0.0002,
            low=price - 0.0001,
            close=price + 0.0001,
            volume=100,
        )

        # Count modifications for traditional approach
        signals_traditional = engine_traditional.on_bar(bar, "H1")
        traditional_modifications += len([s for s in signals_traditional if s.signal_type == SignalType.MODIFY_SL])

        # Count modifications for stepped approach
        signals_stepped = engine_stepped.on_bar(bar, "H1")
        stepped_modifications += len([s for s in signals_stepped if s.signal_type == SignalType.MODIFY_SL])

    # Stepped approach should significantly reduce modification frequency
    assert stepped_modifications < traditional_modifications, "Stepped trailing should reduce modification frequency"
    assert stepped_modifications <= 5, "Stepped trailing should keep modifications to minimum"  # Reasonable upper bound
