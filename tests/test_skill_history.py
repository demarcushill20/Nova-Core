"""Tests for planner.skill_history — SkillHistoryStore with exact spec fields."""

import json
import time
from pathlib import Path

import pytest

from planner.skill_history import SkillHistoryStore


@pytest.fixture
def tmp_store(tmp_path: Path) -> SkillHistoryStore:
    """Return a SkillHistoryStore backed by a temp file."""
    return SkillHistoryStore(path=tmp_path / "history.json")


# -- creates file if missing -------------------------------------------------

def test_creates_file_if_missing(tmp_path: Path):
    p = tmp_path / "subdir" / "history.json"
    assert not p.exists()
    store = SkillHistoryStore(path=p)
    store.record_run("file_ops", success=True, duration_ms=100, retries=0)
    assert p.exists()
    data = json.loads(p.read_text())
    assert "file_ops" in data


# -- record success -----------------------------------------------------------

def test_record_success(tmp_store: SkillHistoryStore):
    tmp_store.record_run("code_improve", success=True, duration_ms=1500, retries=0)
    stats = tmp_store.get_stats("code_improve")
    assert stats["runs"] == 1
    assert stats["successes"] == 1
    assert stats["failures"] == 0
    assert stats["avg_duration_ms"] == 1500
    assert stats["total_retries"] == 0
    assert stats["last_used_ts"] is not None


# -- record failure -----------------------------------------------------------

def test_record_failure(tmp_store: SkillHistoryStore):
    tmp_store.record_run("log_triage", success=False, duration_ms=3000, retries=2)
    stats = tmp_store.get_stats("log_triage")
    assert stats["runs"] == 1
    assert stats["successes"] == 0
    assert stats["failures"] == 1
    assert stats["avg_duration_ms"] == 3000
    assert stats["total_retries"] == 2


# -- computes success rate ----------------------------------------------------

def test_success_rate_basic(tmp_store: SkillHistoryStore):
    tmp_store.record_run("shell_ops", success=True, duration_ms=100, retries=0)
    tmp_store.record_run("shell_ops", success=True, duration_ms=100, retries=0)
    tmp_store.record_run("shell_ops", success=False, duration_ms=100, retries=1)
    assert tmp_store.get_success_rate("shell_ops") == pytest.approx(2 / 3)


def test_success_rate_all_success(tmp_store: SkillHistoryStore):
    tmp_store.record_run("file_ops", success=True, duration_ms=100, retries=0)
    tmp_store.record_run("file_ops", success=True, duration_ms=100, retries=0)
    assert tmp_store.get_success_rate("file_ops") == 1.0


def test_success_rate_all_failure(tmp_store: SkillHistoryStore):
    tmp_store.record_run("file_ops", success=False, duration_ms=100, retries=0)
    tmp_store.record_run("file_ops", success=False, duration_ms=100, retries=0)
    assert tmp_store.get_success_rate("file_ops") == 0.0


def test_success_rate_unknown_skill(tmp_store: SkillHistoryStore):
    assert tmp_store.get_success_rate("nonexistent") == 0.5


# -- computes recency score ---------------------------------------------------

def test_recency_score_recently_used(tmp_store: SkillHistoryStore):
    tmp_store.record_run("web_research", success=True, duration_ms=500, retries=0)
    score = tmp_store.get_recency_score("web_research")
    # Just used, so should be close to 1.0
    assert score > 0.9


def test_recency_score_unknown_skill(tmp_store: SkillHistoryStore):
    assert tmp_store.get_recency_score("nonexistent") == 0.5


def test_recency_score_updates_on_new_record(tmp_store: SkillHistoryStore):
    tmp_store.record_run("web_research", success=True, duration_ms=100, retries=0)
    score1 = tmp_store.get_recency_score("web_research")
    time.sleep(0.01)
    tmp_store.record_run("web_research", success=False, duration_ms=200, retries=0)
    score2 = tmp_store.get_recency_score("web_research")
    # Second record updates last_used_ts; both very recent so nearly equal
    assert abs(score2 - score1) < 0.01


# -- tolerates empty file -----------------------------------------------------

def test_empty_file(tmp_path: Path):
    p = tmp_path / "history.json"
    p.write_text("")
    store = SkillHistoryStore(path=p)
    assert store.get_success_rate("nonexistent") == 0.5
    assert store.get_stats("nonexistent") == {}
    assert store.get_recency_score("nonexistent") == 0.5


# -- tolerates corrupt JSON ---------------------------------------------------

def test_corrupt_json(tmp_path: Path):
    p = tmp_path / "history.json"
    p.write_text("{broken json!!!")
    store = SkillHistoryStore(path=p)
    assert store.get_success_rate("anything") == 0.5
    # Should still be able to record new data
    store.record_run("shell_ops", success=True, duration_ms=100, retries=0)
    assert store.get_stats("shell_ops")["runs"] == 1


# -- avg_duration_ms ----------------------------------------------------------

def test_avg_duration_ms(tmp_store: SkillHistoryStore):
    tmp_store.record_run("code_improve", success=True, duration_ms=1000, retries=0)
    tmp_store.record_run("code_improve", success=True, duration_ms=2000, retries=0)
    tmp_store.record_run("code_improve", success=True, duration_ms=3000, retries=0)
    stats = tmp_store.get_stats("code_improve")
    assert stats["avg_duration_ms"] == 2000


# -- total_retries ------------------------------------------------------------

def test_total_retries_accumulated(tmp_store: SkillHistoryStore):
    tmp_store.record_run("code_improve", success=True, duration_ms=100, retries=1)
    tmp_store.record_run("code_improve", success=False, duration_ms=100, retries=2)
    stats = tmp_store.get_stats("code_improve")
    assert stats["total_retries"] == 3


# -- persistence round-trip ---------------------------------------------------

def test_persistence_round_trip(tmp_path: Path):
    p = tmp_path / "history.json"
    s1 = SkillHistoryStore(path=p)
    s1.record_run("code_improve", success=True, duration_ms=1000, retries=0)
    s1.record_run("code_improve", success=False, duration_ms=2000, retries=1)

    # New instance reads from same file
    s2 = SkillHistoryStore(path=p)
    assert s2.get_stats("code_improve")["runs"] == 2
    assert s2.get_success_rate("code_improve") == 0.5


# -- load/save API ------------------------------------------------------------

def test_load_returns_dict(tmp_store: SkillHistoryStore):
    result = tmp_store.load()
    assert isinstance(result, dict)


def test_save_persists(tmp_path: Path):
    p = tmp_path / "history.json"
    store = SkillHistoryStore(path=p)
    store.save({
        "test_skill": {
            "runs": 1,
            "successes": 1,
            "failures": 0,
            "avg_duration_ms": 100,
            "last_used_ts": "2026-03-06T00:00:00Z",
            "total_retries": 0,
        }
    })
    data = json.loads(p.read_text())
    assert data["test_skill"]["runs"] == 1


# -- exact JSON shape ---------------------------------------------------------

def test_exact_json_shape(tmp_store: SkillHistoryStore):
    tmp_store.record_run("code_improve", success=True, duration_ms=1420, retries=0)
    tmp_store.record_run("code_improve", success=True, duration_ms=1420, retries=1)
    tmp_store.record_run("code_improve", success=True, duration_ms=1420, retries=0)
    tmp_store.record_run("code_improve", success=False, duration_ms=1420, retries=1)
    stats = tmp_store.get_stats("code_improve")
    # Verify all spec-required fields exist
    assert "runs" in stats
    assert "successes" in stats
    assert "failures" in stats
    assert "avg_duration_ms" in stats
    assert "last_used_ts" in stats
    assert "total_retries" in stats
    # Verify values
    assert stats["runs"] == 4
    assert stats["successes"] == 3
    assert stats["failures"] == 1
    assert stats["avg_duration_ms"] == 1420
    assert stats["total_retries"] == 2
    assert isinstance(stats["last_used_ts"], str)
