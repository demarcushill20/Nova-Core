"""Tests for ATR regime detector (v87 P2.1)."""

import json
from dataclasses import dataclass

import pytest

from novatrade.monitor.regime_detector import (
    RegimeSnapshot,
    classify_live_regime,
    load_regime,
)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Redirect regime state to tmp."""
    monkeypatch.setattr("novatrade.monitor.regime_detector.STATE_DIR", tmp_path)
    monkeypatch.setattr("novatrade.monitor.regime_detector.REGIME_FILE", tmp_path / "regime.json")


@dataclass
class FakeCandle:
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def _make_candles(n: int, base: float = 1.0850, spread: float = 0.003) -> list[FakeCandle]:
    """Generate n candles with configurable volatility."""
    candles = []
    for i in range(n):
        c = FakeCandle(
            timestamp=float(1000 + i),
            open=base,
            high=base + spread,
            low=base - spread,
            close=base + (spread * 0.3 * ((-1) ** i)),
        )
        candles.append(c)
    return candles


class TestRegimeClassification:
    def test_insufficient_candles_returns_ranging(self):
        candles = _make_candles(5)
        snap = classify_live_regime(candles)
        assert snap.regime == "ranging"
        assert snap.candle_count == 5

    def test_normal_candles_classify(self):
        candles = _make_candles(30)
        snap = classify_live_regime(candles)
        assert snap.regime in ("quiet", "ranging", "trending", "volatile")
        assert snap.candle_count == 30
        assert snap.classified_at != ""

    def test_persists_to_disk(self, tmp_path):
        candles = _make_candles(30)
        classify_live_regime(candles, symbol="GBPUSD")
        regime_file = tmp_path / "regime.json"
        assert regime_file.exists()
        data = json.loads(regime_file.read_text())
        assert data["symbol"] == "GBPUSD"

    def test_load_regime(self, tmp_path):
        candles = _make_candles(30)
        classify_live_regime(candles, symbol="EURUSD", timeframe="M5")
        loaded = load_regime()
        assert loaded is not None
        assert loaded.symbol == "EURUSD"
        assert loaded.timeframe == "M5"

    def test_load_regime_returns_none_when_no_file(self):
        loaded = load_regime()
        assert loaded is None


class TestRegimeSnapshot:
    def test_is_low_vol(self):
        snap = RegimeSnapshot(regime="quiet")
        assert snap.is_low_vol is True
        assert snap.is_high_vol is False

    def test_is_high_vol(self):
        snap = RegimeSnapshot(regime="volatile")
        assert snap.is_high_vol is True
        assert snap.is_low_vol is False

    def test_normal_regime_neither(self):
        snap = RegimeSnapshot(regime="ranging")
        assert snap.is_low_vol is False
        assert snap.is_high_vol is False
