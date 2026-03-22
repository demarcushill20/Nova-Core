"""Tests for utils/daily_continuity.py — daily continuity engine."""

from __future__ import annotations

import json

import pytest

from utils.daily_continuity import (
    DailyDelta,
    _count_tests,
    _latest_health,
    _load_prior_snapshot,
    _save_snapshot,
    compute_daily_delta,
    format_briefing,
)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Patch STATE_DIR and METRICS_JSONL to tmp."""
    import utils.daily_continuity as mod

    monkeypatch.setattr(mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mod, "CONTINUITY_FILE", tmp_path / "daily_continuity.json")
    monkeypatch.setattr(mod, "METRICS_JSONL", tmp_path / "heartbeat_metrics.jsonl")
    monkeypatch.setattr(mod, "BASE", tmp_path)
    return tmp_path


class TestLoadSaveSnapshot:
    def test_empty_returns_dict(self, state_dir):
        assert _load_prior_snapshot() == {}

    def test_roundtrip(self, state_dir):
        data = {"date": "2026-03-18", "health_score": 87}
        _save_snapshot(data)
        loaded = _load_prior_snapshot()
        assert loaded["date"] == "2026-03-18"
        assert loaded["health_score"] == 87

    def test_corrupt_json_returns_empty(self, state_dir):
        (state_dir / "daily_continuity.json").write_text("{bad json", encoding="utf-8")
        assert _load_prior_snapshot() == {}


class TestLatestHealth:
    def test_no_metrics_file(self, state_dir):
        score, fails = _latest_health()
        assert score == 100
        assert fails == []

    def test_reads_last_line(self, state_dir):
        lines = [
            json.dumps({"total_checks": 22, "passed": 20, "failed_names": ["a", "b"]}),
            json.dumps({"total_checks": 22, "passed": 19, "failed_names": ["a", "b", "c"]}),
        ]
        (state_dir / "heartbeat_metrics.jsonl").write_text("\n".join(lines))
        score, fails = _latest_health()
        assert score == 86  # 19/22 = 86.4 → 86
        assert "c" in fails

    def test_empty_metrics_file(self, state_dir):
        (state_dir / "heartbeat_metrics.jsonl").write_text("")
        score, fails = _latest_health()
        assert score == 100
        assert fails == []


class TestCountTests:
    def test_counts_test_functions(self, state_dir):
        tests_dir = state_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_foo.py").write_text(
            "class TestFoo:\n    def test_a(self):\n        pass\n    def test_b(self):\n        pass\n"
        )
        (tests_dir / "test_bar.py").write_text("import pytest\n\ndef test_standalone():\n    pass\n")
        count = _count_tests()
        assert count == 3

    def test_no_tests_dir(self, state_dir):
        assert _count_tests() == 0


class TestComputeDailyDelta:
    def test_first_day_no_prior(self, state_dir):
        delta = compute_daily_delta()
        assert isinstance(delta, DailyDelta)
        assert delta.prior_date == ""
        assert delta.health_delta == 0  # 100 - 100

    def test_day_over_day_improvement(self, state_dir):
        # Simulate yesterday's snapshot
        _save_snapshot(
            {
                "date": "2026-03-18",
                "health_score": 82,
                "current_failures": ["service:novacore-watcher", "service:novacore-telegram"],
                "test_count": 200,
                "open_threads": ["fix services"],
                "next_actions": ["restart services"],
            }
        )
        # Simulate today's health (better)
        metric = json.dumps({"total_checks": 22, "passed": 21, "failed_names": ["service:novacore-telegram"]})
        (state_dir / "heartbeat_metrics.jsonl").write_text(metric)

        delta = compute_daily_delta(
            checkpoint_summary="Services partially restored",
            open_threads=["telegram still down"],
        )

        assert delta.health_score == 95  # 21/22
        assert delta.health_delta == 13  # 95 - 82
        assert "service:novacore-watcher" in delta.resolved_failures
        assert "service:novacore-telegram" in delta.persistent_failures
        assert "fix services" not in delta.open_threads
        assert "telegram still down" in delta.open_threads

    def test_day_over_day_regression(self, state_dir):
        _save_snapshot(
            {
                "date": "2026-03-18",
                "health_score": 100,
                "current_failures": [],
                "test_count": 300,
            }
        )
        metric = json.dumps({"total_checks": 22, "passed": 19, "failed_names": ["disk", "backup", "ruff"]})
        (state_dir / "heartbeat_metrics.jsonl").write_text(metric)

        delta = compute_daily_delta()
        assert delta.health_delta < 0
        assert "disk" in delta.new_failures

    def test_snapshot_persisted_for_next_day(self, state_dir):
        compute_daily_delta(checkpoint_summary="Did stuff")
        loaded = _load_prior_snapshot()
        assert loaded["checkpoint_summary"] == "Did stuff"


class TestFormatBriefing:
    def test_formats_cleanly(self):
        delta = DailyDelta(
            date="2026-03-19",
            prior_date="2026-03-18",
            health_score=91,
            health_delta=+4,
            new_failures=[],
            resolved_failures=["service:novacore-watcher"],
            persistent_failures=["service:novacore-telegram"],
            open_threads=["NovaTrade systemd service"],
            next_actions=["Create novacore-novatrade.service"],
            checkpoint_summary="Phase 11 complete",
            test_count=6200,
            test_delta=+3,
        )
        text = format_briefing(delta)
        assert "91/100" in text
        assert "+4" in text
        assert "Resolved:" in text
        assert "Phase 11 complete" in text
        assert "6200" in text

    def test_first_day_no_prior(self):
        delta = DailyDelta(
            date="2026-03-19",
            prior_date="",
            health_score=100,
            health_delta=0,
        )
        text = format_briefing(delta)
        assert "100/100" in text
        assert "Building on:" not in text
