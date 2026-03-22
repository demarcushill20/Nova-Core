"""Tests for heartbeat.get_daily_delta() — day-over-day health comparison."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import heartbeat


@pytest.fixture
def metrics_file(tmp_path, monkeypatch):
    """Redirect METRICS_JSONL to temp file."""
    f = tmp_path / "heartbeat_metrics.jsonl"
    monkeypatch.setattr(heartbeat, "METRICS_JSONL", f)
    return f


def _metric_line(ts: str, passed: int = 22, total: int = 22, failed_names: list | None = None) -> str:
    return json.dumps(
        {
            "ts": ts,
            "total_checks": total,
            "passed": passed,
            "failed_names": failed_names or [],
            "status": "healthy" if passed == total else "unhealthy",
        }
    )


class TestGetDailyDelta:
    def test_no_metrics_file(self, metrics_file):
        result = heartbeat.get_daily_delta()
        assert result["today_score"] == 100
        assert result["delta"] == 0
        assert result["new_failures"] == []

    def test_today_only(self, metrics_file):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = [
            _metric_line(f"{today}T10:00:00Z", passed=20, failed_names=["a", "b"]),
            _metric_line(f"{today}T10:30:00Z", passed=21, failed_names=["a"]),
        ]
        metrics_file.write_text("\n".join(lines))
        result = heartbeat.get_daily_delta()
        # avg of 20/22 (91) and 21/22 (95) = 93
        assert result["today_score"] == 93
        assert result["yesterday_score"] == 100  # no yesterday data
        assert result["delta"] == -7

    def test_two_days(self, metrics_file):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        lines = [
            _metric_line(f"{yesterday}T10:00:00Z", passed=19, failed_names=["a", "b", "c"]),
            _metric_line(f"{yesterday}T10:30:00Z", passed=20, failed_names=["a", "b"]),
            _metric_line(f"{today}T10:00:00Z", passed=21, failed_names=["a"]),
            _metric_line(f"{today}T10:30:00Z", passed=22, failed_names=[]),
        ]
        metrics_file.write_text("\n".join(lines))
        result = heartbeat.get_daily_delta()
        # yesterday avg: (86 + 91) / 2 = 89 (rounding: 19/22=86.4→86, 20/22=90.9→91)
        # today avg: (95 + 100) / 2 = 98 (rounding: 21/22=95.5→95, 22/22=100)
        assert result["today_score"] >= 97
        assert result["yesterday_score"] <= 90
        assert result["delta"] > 0
        assert result["yesterday_date"] == yesterday

    def test_resolved_and_new_failures(self, metrics_file):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        lines = [
            _metric_line(f"{yesterday}T10:00:00Z", passed=20, failed_names=["disk", "backup"]),
            _metric_line(f"{today}T10:00:00Z", passed=21, failed_names=["ruff"]),
        ]
        metrics_file.write_text("\n".join(lines))
        result = heartbeat.get_daily_delta()
        assert "disk" in result["resolved_failures"]
        assert "backup" in result["resolved_failures"]
        assert "ruff" in result["new_failures"]

    def test_empty_metrics_file(self, metrics_file):
        metrics_file.write_text("")
        result = heartbeat.get_daily_delta()
        assert result["today_score"] == 100
        assert result["today_samples"] == 0
