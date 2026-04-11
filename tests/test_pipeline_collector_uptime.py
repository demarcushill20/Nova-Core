"""Tests for PipelineCollector._check_feed_health — uptime guard fix.

Verifies that live_metrics.json scoring works when:
- ticks > 0 (primary path)
- ticks == 0 and uptime_seconds == 0.0 (the fixed edge case)
- ticks == 0 and uptime_seconds > 0 (startup path)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from novatrade.autonomy.collectors.pipeline import PipelineCollector


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Create a temporary STATE/novatrade directory."""
    d = tmp_path / "STATE" / "novatrade"
    d.mkdir(parents=True)
    return d


def _write_live_metrics(state_dir: Path, data: dict) -> None:
    """Write live_metrics.json with a fresh mtime."""
    path = state_dir / "live_metrics.json"
    path.write_text(json.dumps(data))


def _make_collector(tmp_path: Path) -> PipelineCollector:
    return PipelineCollector(base_path=str(tmp_path))


class TestFeedHealthLiveMetrics:
    """Tests for the live_metrics.json fallback path in _check_feed_health."""

    def test_ticks_flowing_low_errors(self, tmp_path: Path, state_dir: Path) -> None:
        """ticks > 0 with low error ratio → 50 pts."""
        _write_live_metrics(state_dir, {"ticks": 100, "errors": 1, "uptime_seconds": 300})
        collector = _make_collector(tmp_path)
        score, checks = collector._check_feed_health()
        assert score == 50.0
        assert checks == 1.0

    def test_ticks_flowing_moderate_errors(self, tmp_path: Path, state_dir: Path) -> None:
        """ticks > 0 with moderate error ratio → 30 pts."""
        _write_live_metrics(state_dir, {"ticks": 100, "errors": 10, "uptime_seconds": 300})
        collector = _make_collector(tmp_path)
        score, checks = collector._check_feed_health()
        assert score == 30.0
        assert checks == 1.0

    def test_ticks_flowing_high_errors(self, tmp_path: Path, state_dir: Path) -> None:
        """ticks > 0 with high error ratio → 10 pts."""
        _write_live_metrics(state_dir, {"ticks": 100, "errors": 50, "uptime_seconds": 300})
        collector = _make_collector(tmp_path)
        score, checks = collector._check_feed_health()
        assert score == 10.0
        assert checks == 1.0

    def test_zero_ticks_zero_uptime_still_scores(self, tmp_path: Path, state_dir: Path) -> None:
        """REGRESSION: ticks=0 + uptime_seconds=0.0 must still score (the fix).

        Before the fix, this combination would skip both branches and
        contribute 0 to feed_health. Now the else-branch fires → 20 pts.
        Mock market-closed to False so we exercise the else-branch
        regardless of what day the test runs.
        """
        _write_live_metrics(state_dir, {"ticks": 0, "errors": 0, "uptime_seconds": 0.0})
        collector = _make_collector(tmp_path)
        with (
            patch.object(collector, "_is_forex_market_closed", return_value=False),
            patch.object(collector, "_runtime_reports_market_closed", return_value=False),
        ):
            score, checks = collector._check_feed_health()
        assert score == 20.0, "Fresh live_metrics with ticks=0 uptime=0 should still score 20"
        assert checks == 1.0

    def test_zero_ticks_positive_uptime(self, tmp_path: Path, state_dir: Path) -> None:
        """ticks=0 + uptime>0 → 20 pts (service running, no ticks yet)."""
        _write_live_metrics(state_dir, {"ticks": 0, "errors": 0, "uptime_seconds": 60.0})
        collector = _make_collector(tmp_path)
        with (
            patch.object(collector, "_is_forex_market_closed", return_value=False),
            patch.object(collector, "_runtime_reports_market_closed", return_value=False),
        ):
            score, checks = collector._check_feed_health()
        assert score == 20.0
        assert checks == 1.0

    def test_stale_live_metrics_ignored(self, tmp_path: Path, state_dir: Path) -> None:
        """live_metrics.json older than 30 min → skipped entirely."""
        path = state_dir / "live_metrics.json"
        path.write_text(json.dumps({"ticks": 100, "errors": 0, "uptime_seconds": 9999}))
        # Backdate the file to 2 hours ago
        old_time = time.time() - 7200
        import os

        os.utime(str(path), (old_time, old_time))

        collector = _make_collector(tmp_path)
        score, checks = collector._check_feed_health()
        # Should fall through to generic mtime check (stale file → low score)
        assert score <= 25.0

    def test_no_state_dir_returns_zero(self, tmp_path: Path) -> None:
        """No STATE/novatrade directory → 0."""
        collector = _make_collector(tmp_path)
        score, checks = collector._check_feed_health()
        assert score == 0.0
        assert checks == 0.0
