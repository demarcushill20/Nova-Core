"""Tests for ATR regime-based live position sizing.

Covers:
  - load_regime_safe(): staleness, missing file, malformed JSON, missing fields
  - ATR zone risk factor application: each zone scales volume correctly
  - Feature flag OFF: behavior identical to baseline (no adjustment)
  - Feature flag ON: volume scaled by regime risk factor
  - Fallback: stale/missing/malformed regime → base volume unchanged
  - Edge cases: risk_factor=0.0 floors to min_volume
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from novatrade.monitor.regime_detector import (
    atr_zone_risk_factor,
    classify_atr_zone,
    load_regime_safe,
)

# ---------------------------------------------------------------------------
# Helper: write a regime.json for testing
# ---------------------------------------------------------------------------


def _write_regime(
    tmp_path: Path,
    *,
    atr_zone: str = "normal",
    atr_zone_risk_factor_val: float = 1.0,
    classified_at: str | None = None,
    extra_fields: dict | None = None,
    raw_content: str | None = None,
) -> Path:
    """Write a regime.json file and return its path."""
    regime_file = tmp_path / "regime.json"
    if raw_content is not None:
        regime_file.write_text(raw_content)
        return regime_file

    if classified_at is None:
        classified_at = datetime.now(timezone.utc).isoformat()

    data = {
        "regime": "quiet",
        "atr_current": 0.000300,
        "atr_mean": 0.000500,
        "atr_percentile": 50.0,
        "atr_zone": atr_zone,
        "atr_zone_risk_factor": atr_zone_risk_factor_val,
        "symbol": "EURUSD",
        "timeframe": "M5",
        "candle_count": 500,
        "classified_at": classified_at,
    }
    if extra_fields:
        data.update(extra_fields)
    regime_file.write_text(json.dumps(data, indent=2))
    return regime_file


# ---------------------------------------------------------------------------
# Tests: classify_atr_zone and atr_zone_risk_factor (existing functions)
# ---------------------------------------------------------------------------


class TestATRZoneClassification:
    """Verify zone classification and risk factor lookup for all 5 zones."""

    @pytest.mark.parametrize(
        "percentile,expected_zone,expected_factor",
        [
            (0.0, "compression", 0.0),
            (5.0, "compression", 0.0),
            (9.9, "compression", 0.0),
            (10.0, "low_vol", 0.5),
            (20.0, "low_vol", 0.5),
            (29.9, "low_vol", 0.5),
            (30.0, "normal", 1.0),
            (50.0, "normal", 1.0),
            (69.9, "normal", 1.0),
            (70.0, "elevated", 0.75),
            (80.0, "elevated", 0.75),
            (89.9, "elevated", 0.75),
            (90.0, "extreme", 0.0),
            (95.0, "extreme", 0.0),
            (100.0, "extreme", 0.0),
        ],
    )
    def test_zone_and_factor(self, percentile, expected_zone, expected_factor):
        zone = classify_atr_zone(percentile)
        factor = atr_zone_risk_factor(zone)
        assert zone == expected_zone, f"percentile={percentile} → zone={zone}, expected {expected_zone}"
        assert factor == expected_factor, f"zone={zone} → factor={factor}, expected {expected_factor}"

    def test_unknown_zone_defaults_to_1(self):
        """Unknown zone string should return factor 1.0 (safe fallback)."""
        assert atr_zone_risk_factor("nonexistent_zone") == 1.0


# ---------------------------------------------------------------------------
# Tests: load_regime_safe()
# ---------------------------------------------------------------------------


class TestLoadRegimeSafe:
    """Tests for the safe regime loader with staleness/fallback handling."""

    def test_missing_file_returns_fallback(self, tmp_path):
        """Missing regime.json → factor=1.0, zone=unknown, reason set."""
        fake_path = tmp_path / "nonexistent.json"
        with patch("novatrade.monitor.regime_detector.REGIME_FILE", fake_path):
            factor, zone, reason = load_regime_safe(staleness_seconds=900)
        assert factor == 1.0
        assert zone == "unknown"
        assert "missing" in reason

    def test_malformed_json_returns_fallback(self, tmp_path):
        """Invalid JSON → factor=1.0, zone=unknown, reason set."""
        regime_file = _write_regime(tmp_path, raw_content="not valid json {{{}}")
        with patch("novatrade.monitor.regime_detector.REGIME_FILE", regime_file):
            factor, zone, reason = load_regime_safe(staleness_seconds=900)
        assert factor == 1.0
        assert zone == "unknown"
        assert "malformed" in reason or "unreadable" in reason

    def test_missing_atr_zone_field_returns_fallback(self, tmp_path):
        """JSON valid but missing atr_zone → fallback."""
        regime_file = tmp_path / "regime.json"
        regime_file.write_text(
            json.dumps(
                {
                    "regime": "quiet",
                    "classified_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
        with patch("novatrade.monitor.regime_detector.REGIME_FILE", regime_file):
            factor, zone, reason = load_regime_safe(staleness_seconds=900)
        assert factor == 1.0
        assert zone == "unknown"
        assert "missing required fields" in reason

    def test_missing_risk_factor_field_returns_fallback(self, tmp_path):
        """JSON valid but missing atr_zone_risk_factor → fallback."""
        regime_file = tmp_path / "regime.json"
        regime_file.write_text(
            json.dumps(
                {
                    "regime": "quiet",
                    "atr_zone": "normal",
                    "classified_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
        with patch("novatrade.monitor.regime_detector.REGIME_FILE", regime_file):
            factor, zone, reason = load_regime_safe(staleness_seconds=900)
        assert factor == 1.0
        assert zone == "unknown"
        assert "missing required fields" in reason

    def test_stale_regime_returns_fallback(self, tmp_path):
        """Regime classified > staleness_seconds ago → fallback."""
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=1200)).isoformat()
        regime_file = _write_regime(tmp_path, atr_zone="normal", atr_zone_risk_factor_val=1.0, classified_at=old_ts)
        with patch("novatrade.monitor.regime_detector.REGIME_FILE", regime_file):
            factor, zone, reason = load_regime_safe(staleness_seconds=900)
        assert factor == 1.0
        assert zone == "unknown"
        assert "stale" in reason

    def test_fresh_regime_returns_values(self, tmp_path):
        """Fresh, valid regime.json → returns actual values."""
        regime_file = _write_regime(tmp_path, atr_zone="elevated", atr_zone_risk_factor_val=0.75)
        with patch("novatrade.monitor.regime_detector.REGIME_FILE", regime_file):
            factor, zone, reason = load_regime_safe(staleness_seconds=900)
        assert factor == 0.75
        assert zone == "elevated"
        assert reason == ""

    def test_compression_zone_returns_zero(self, tmp_path):
        """Compression zone → factor=0.0 (no adjustment by loader, sizing handles it)."""
        regime_file = _write_regime(tmp_path, atr_zone="compression", atr_zone_risk_factor_val=0.0)
        with patch("novatrade.monitor.regime_detector.REGIME_FILE", regime_file):
            factor, zone, reason = load_regime_safe(staleness_seconds=900)
        assert factor == 0.0
        assert zone == "compression"
        assert reason == ""

    def test_no_classified_at_returns_fallback(self, tmp_path):
        """Missing classified_at → fallback (can't determine freshness)."""
        regime_file = tmp_path / "regime.json"
        regime_file.write_text(
            json.dumps(
                {
                    "atr_zone": "normal",
                    "atr_zone_risk_factor": 1.0,
                }
            )
        )
        with patch("novatrade.monitor.regime_detector.REGIME_FILE", regime_file):
            factor, zone, reason = load_regime_safe(staleness_seconds=900)
        assert factor == 1.0
        assert "no classified_at" in reason

    def test_unparseable_classified_at_returns_fallback(self, tmp_path):
        """Garbage classified_at → fallback."""
        regime_file = tmp_path / "regime.json"
        regime_file.write_text(
            json.dumps(
                {
                    "atr_zone": "normal",
                    "atr_zone_risk_factor": 1.0,
                    "classified_at": "not-a-date",
                }
            )
        )
        with patch("novatrade.monitor.regime_detector.REGIME_FILE", regime_file):
            factor, zone, reason = load_regime_safe(staleness_seconds=900)
        assert factor == 1.0
        assert "unparseable" in reason

    def test_non_numeric_risk_factor_returns_fallback(self, tmp_path):
        """Non-numeric risk_factor → fallback."""
        regime_file = tmp_path / "regime.json"
        regime_file.write_text(
            json.dumps(
                {
                    "atr_zone": "normal",
                    "atr_zone_risk_factor": "not_a_number",
                    "classified_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
        with patch("novatrade.monitor.regime_detector.REGIME_FILE", regime_file):
            factor, zone, reason = load_regime_safe(staleness_seconds=900)
        assert factor == 1.0
        assert "not numeric" in reason

    @pytest.mark.parametrize(
        "zone,expected_factor",
        [
            ("compression", 0.0),
            ("low_vol", 0.5),
            ("normal", 1.0),
            ("elevated", 0.75),
            ("extreme", 0.0),
        ],
    )
    def test_all_zones_load_correctly(self, tmp_path, zone, expected_factor):
        """Each ATR zone loads with the correct risk factor."""
        regime_file = _write_regime(tmp_path, atr_zone=zone, atr_zone_risk_factor_val=expected_factor)
        with patch("novatrade.monitor.regime_detector.REGIME_FILE", regime_file):
            factor, loaded_zone, reason = load_regime_safe(staleness_seconds=900)
        assert factor == expected_factor
        assert loaded_zone == zone
        assert reason == ""


# ---------------------------------------------------------------------------
# Tests: _apply_atr_regime_sizing (via LiveTradingAgent)
# ---------------------------------------------------------------------------


class TestApplyATRRegimeSizing:
    """Test ATR regime sizing integration in LiveTradingAgent.

    Uses _apply_atr_regime_sizing directly by constructing a minimal
    LiveTradingAgent with mocked dependencies.
    """

    def _make_agent(self, *, atr_enabled: bool = False, staleness: int = 900):
        """Create a LiveTradingAgent with mocked dependencies for sizing tests."""
        from novatrade.config import NovaTradeCfg, RiskConfig
        from novatrade.execution.live_trading_agent import LiveTradingAgent

        cfg = NovaTradeCfg(
            risk=RiskConfig(
                atr_regime_sizing_enabled=atr_enabled,
                atr_regime_staleness_seconds=staleness,
                min_volume_per_trade=0.01,
            ),
        )
        agent = LiveTradingAgent.__new__(LiveTradingAgent)
        agent._cfg = cfg
        return agent

    def test_flag_off_returns_base_volume(self):
        """Feature flag OFF → base volume returned unchanged."""
        agent = self._make_agent(atr_enabled=False)
        result = agent._apply_atr_regime_sizing(5.00, "EURUSD")
        assert result == 5.00

    def test_flag_on_normal_zone_no_change(self, tmp_path):
        """Normal zone (factor=1.0) → volume unchanged."""
        agent = self._make_agent(atr_enabled=True)
        _write_regime(tmp_path, atr_zone="normal", atr_zone_risk_factor_val=1.0)
        with patch("novatrade.execution.live_trading_agent.load_regime_safe", return_value=(1.0, "normal", "")):
            result = agent._apply_atr_regime_sizing(5.00, "EURUSD")
        assert result == 5.00

    def test_flag_on_low_vol_halves_volume(self):
        """Low vol zone (factor=0.5) → volume halved."""
        agent = self._make_agent(atr_enabled=True)
        with patch("novatrade.execution.live_trading_agent.load_regime_safe", return_value=(0.5, "low_vol", "")):
            result = agent._apply_atr_regime_sizing(4.00, "EURUSD")
        assert result == 2.00

    def test_flag_on_elevated_scales_75pct(self):
        """Elevated zone (factor=0.75) → volume at 75%."""
        agent = self._make_agent(atr_enabled=True)
        with patch("novatrade.execution.live_trading_agent.load_regime_safe", return_value=(0.75, "elevated", "")):
            result = agent._apply_atr_regime_sizing(4.00, "EURUSD")
        assert result == 3.00

    def test_flag_on_compression_floors_to_min(self):
        """Compression zone (factor=0.0) → volume floors to min_volume_per_trade."""
        agent = self._make_agent(atr_enabled=True)
        with patch("novatrade.execution.live_trading_agent.load_regime_safe", return_value=(0.0, "compression", "")):
            result = agent._apply_atr_regime_sizing(5.00, "EURUSD")
        assert result == 0.01  # min_volume_per_trade

    def test_flag_on_extreme_floors_to_min(self):
        """Extreme zone (factor=0.0) → volume floors to min_volume_per_trade."""
        agent = self._make_agent(atr_enabled=True)
        with patch("novatrade.execution.live_trading_agent.load_regime_safe", return_value=(0.0, "extreme", "")):
            result = agent._apply_atr_regime_sizing(5.00, "EURUSD")
        assert result == 0.01

    def test_flag_on_fallback_returns_base(self):
        """Regime file unavailable → base volume returned (fallback)."""
        agent = self._make_agent(atr_enabled=True)
        with patch(
            "novatrade.execution.live_trading_agent.load_regime_safe",
            return_value=(1.0, "unknown", "regime file missing"),
        ):
            result = agent._apply_atr_regime_sizing(5.00, "EURUSD")
        assert result == 5.00

    def test_flag_on_stale_returns_base(self):
        """Stale regime → base volume returned (fallback)."""
        agent = self._make_agent(atr_enabled=True)
        with patch(
            "novatrade.execution.live_trading_agent.load_regime_safe",
            return_value=(1.0, "unknown", "regime stale: age=1200s > threshold=900s"),
        ):
            result = agent._apply_atr_regime_sizing(5.00, "EURUSD")
        assert result == 5.00

    def test_rounding_to_2_decimals(self):
        """Adjusted volume should be rounded to 2 decimal places."""
        agent = self._make_agent(atr_enabled=True)
        # 3.33 * 0.75 = 2.4975 → 2.50
        with patch("novatrade.execution.live_trading_agent.load_regime_safe", return_value=(0.75, "elevated", "")):
            result = agent._apply_atr_regime_sizing(3.33, "EURUSD")
        assert result == 2.50

    def test_min_volume_floor(self):
        """Adjusted volume below min_volume floors to min_volume."""
        agent = self._make_agent(atr_enabled=True)
        # 0.02 * 0.5 = 0.01 → exactly min
        with patch("novatrade.execution.live_trading_agent.load_regime_safe", return_value=(0.5, "low_vol", "")):
            result = agent._apply_atr_regime_sizing(0.02, "EURUSD")
        assert result == 0.01


# ---------------------------------------------------------------------------
# Regression: flag OFF = identical to baseline
# ---------------------------------------------------------------------------


class TestRegressionFlagOff:
    """When ATR regime sizing is disabled, position sizing is identical to baseline.

    These tests prove that no code path in the ATR regime system alters
    behavior when the feature flag is off.
    """

    def test_volume_unchanged_at_various_sizes(self):
        """Multiple volume values pass through unmodified when flag is off."""
        from novatrade.config import NovaTradeCfg, RiskConfig
        from novatrade.execution.live_trading_agent import LiveTradingAgent

        cfg = NovaTradeCfg(risk=RiskConfig(atr_regime_sizing_enabled=False))
        agent = LiveTradingAgent.__new__(LiveTradingAgent)
        agent._cfg = cfg

        for vol in [0.01, 0.10, 0.50, 1.00, 5.00, 10.00]:
            result = agent._apply_atr_regime_sizing(vol, "EURUSD")
            assert result == vol, f"Expected {vol}, got {result} — flag OFF should not alter volume"

    def test_regime_file_not_read_when_disabled(self):
        """When flag is off, load_regime_safe should NOT be called."""
        from novatrade.config import NovaTradeCfg, RiskConfig
        from novatrade.execution.live_trading_agent import LiveTradingAgent

        cfg = NovaTradeCfg(risk=RiskConfig(atr_regime_sizing_enabled=False))
        agent = LiveTradingAgent.__new__(LiveTradingAgent)
        agent._cfg = cfg

        with patch("novatrade.execution.live_trading_agent.load_regime_safe") as mock_load:
            agent._apply_atr_regime_sizing(5.00, "EURUSD")
            mock_load.assert_not_called()
