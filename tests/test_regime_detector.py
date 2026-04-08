"""Tests for ATR regime detector (v87 P2.1) with 5-zone percentile system."""

import json
from dataclasses import dataclass

import pytest

from novatrade.monitor.regime_detector import (
    ATR_ZONE_RISK_FACTOR,
    RegimeSnapshot,
    atr_zone_risk_factor,
    classify_atr_zone,
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

    def test_trading_allowed_normal(self):
        snap = RegimeSnapshot(regime="ranging", atr_zone="normal", atr_zone_risk_factor=1.0)
        assert snap.trading_allowed is True

    def test_trading_blocked_compression(self):
        snap = RegimeSnapshot(regime="quiet", atr_zone="compression", atr_zone_risk_factor=0.0)
        assert snap.trading_allowed is False

    def test_trading_blocked_extreme(self):
        snap = RegimeSnapshot(regime="volatile", atr_zone="extreme", atr_zone_risk_factor=0.0)
        assert snap.trading_allowed is False

    def test_defaults_include_atr_fields(self):
        snap = RegimeSnapshot(regime="ranging")
        assert snap.atr_percentile == 50.0
        assert snap.atr_zone == "normal"
        assert snap.atr_zone_risk_factor == 1.0


class TestAtrZoneClassification:
    """Tests for the 5-zone ATR percentile system."""

    @pytest.mark.parametrize(
        "percentile,expected_zone",
        [
            (0.0, "compression"),
            (5.0, "compression"),
            (9.9, "compression"),
            (10.0, "low_vol"),
            (20.0, "low_vol"),
            (29.9, "low_vol"),
            (30.0, "normal"),
            (50.0, "normal"),
            (69.9, "normal"),
            (70.0, "elevated"),
            (80.0, "elevated"),
            (89.9, "elevated"),
            (90.0, "extreme"),
            (95.0, "extreme"),
            (100.0, "extreme"),
        ],
    )
    def test_zone_boundaries(self, percentile, expected_zone):
        assert classify_atr_zone(percentile) == expected_zone

    def test_risk_factors(self):
        assert atr_zone_risk_factor("compression") == 0.0
        assert atr_zone_risk_factor("low_vol") == 0.5
        assert atr_zone_risk_factor("normal") == 1.0
        assert atr_zone_risk_factor("elevated") == 0.75
        assert atr_zone_risk_factor("extreme") == 0.0

    def test_unknown_zone_defaults_to_full_risk(self):
        assert atr_zone_risk_factor("unknown") == 1.0

    def test_all_zones_have_risk_factors(self):
        from novatrade.monitor.regime_detector import ATR_ZONE_THRESHOLDS

        for zone in ATR_ZONE_THRESHOLDS:
            assert zone in ATR_ZONE_RISK_FACTOR


class TestLiveRegimeAtrPercentile:
    """Tests that classify_live_regime computes ATR percentile and zone."""

    def test_normal_candles_have_atr_percentile(self):
        candles = _make_candles(50)
        snap = classify_live_regime(candles)
        assert 0.0 <= snap.atr_percentile <= 100.0
        assert snap.atr_zone in ("compression", "low_vol", "normal", "elevated", "extreme")
        assert snap.atr_zone_risk_factor >= 0.0

    def test_uniform_candles_high_percentile(self):
        """With uniform spread, current ATR equals all ATRs → high percentile."""
        candles = _make_candles(50, spread=0.003)
        snap = classify_live_regime(candles)
        # All ATRs are identical, so percentile should be 100%
        assert snap.atr_percentile == 100.0

    def test_volatile_spike_at_end(self):
        """A big volatility spike at the end should produce high percentile."""
        candles = _make_candles(50, spread=0.001)
        # Make the last few candles very volatile
        for c in candles[-5:]:
            c.high = c.open + 0.02
            c.low = c.open - 0.02
        snap = classify_live_regime(candles)
        assert snap.atr_percentile > 80.0

    def test_calm_end_after_volatile_start(self):
        """Calm candles at the end after volatile start → low percentile."""
        candles = _make_candles(50, spread=0.01)
        # Make the last 20 candles very calm
        for c in candles[-20:]:
            c.high = c.open + 0.0001
            c.low = c.open - 0.0001
        snap = classify_live_regime(candles)
        assert snap.atr_percentile < 30.0

    def test_insufficient_candles_get_defaults(self):
        candles = _make_candles(5)
        snap = classify_live_regime(candles)
        assert snap.atr_percentile == 50.0
        assert snap.atr_zone == "normal"
        assert snap.atr_zone_risk_factor == 1.0

    def test_persisted_snapshot_includes_atr_fields(self, tmp_path):
        candles = _make_candles(50)
        classify_live_regime(candles)
        data = json.loads((tmp_path / "regime.json").read_text())
        assert "atr_percentile" in data
        assert "atr_zone" in data
        assert "atr_zone_risk_factor" in data
