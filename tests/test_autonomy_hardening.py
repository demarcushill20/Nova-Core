"""Tests for Autonomy Hardening — Phase Next-Gen A.

Covers:
- H1: Collector exception resilience with cache fallback
- H2: Per-collector timeout (asyncio.wait_for)
- M1: Task dedup window including .done files
- M2: CLI per-dimension warnings display
- M3: PerformanceCollector no-data flag
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from novatrade.autonomy.collectors.performance import PerformanceCollector
from novatrade.autonomy.progress_scorer import ProgressScorer
from novatrade.autonomy.schemas import DimensionScore
from novatrade.autonomy.task_generator import TaskSpec, TaskSpecGenerator

# =====================================================================
# H1: Collector exception resilience with cache fallback
# =====================================================================


@pytest.fixture
def tmp_scorer(tmp_path):
    """Create a ProgressScorer with a tmp base_path."""
    (tmp_path / "STATE").mkdir()
    return ProgressScorer(base_path=str(tmp_path))


def _write_history(tmp_path, dimensions: dict[str, float]):
    """Write a fake progress_history.json with given dimension scores."""
    history_path = tmp_path / "STATE" / "progress_history.json"
    entry = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dimensions": {name: {"score": score} for name, score in dimensions.items()},
    }
    history_path.write_text(json.dumps([entry]))


@pytest.mark.asyncio
async def test_h1_cache_fallback_on_exception(tmp_path):
    """When a collector throws, ProgressScorer should fall back to cached score."""
    scorer = ProgressScorer(base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(parents=True)
    _write_history(tmp_path, {"system_health": 85.0, "execution_pipeline": 70.0})

    # Make one collector throw
    failing_collector = AsyncMock(side_effect=RuntimeError("boom"))
    ok_collector = AsyncMock(return_value=DimensionScore(name="ok", score=90.0))

    scorer._collectors = {
        "system_health": MagicMock(collect=failing_collector),
        "execution_pipeline": MagicMock(collect=ok_collector),
    }

    report = await scorer.score()

    # system_health should use cached 85.0, not 0.0
    assert report.dimensions["system_health"].score == 85.0
    assert any("cached score" in w for w in report.warnings)
    # execution_pipeline should be normal
    assert report.dimensions["execution_pipeline"].score == 90.0


@pytest.mark.asyncio
async def test_h1_no_cache_falls_to_zero(tmp_path):
    """When a collector throws and there's no cache, score should be 0.0."""
    scorer = ProgressScorer(base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(parents=True)
    # No history file exists

    failing_collector = AsyncMock(side_effect=RuntimeError("boom"))
    scorer._collectors = {
        "system_health": MagicMock(collect=failing_collector),
    }

    report = await scorer.score()
    assert report.dimensions["system_health"].score == 0.0


@pytest.mark.asyncio
async def test_h1_cache_fallback_preserves_warning(tmp_path):
    """Cache fallback should include a warning explaining the situation."""
    scorer = ProgressScorer(base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(parents=True)
    _write_history(tmp_path, {"system_health": 75.0})

    failing_collector = AsyncMock(side_effect=ValueError("bad data"))
    scorer._collectors = {
        "system_health": MagicMock(collect=failing_collector),
    }

    report = await scorer.score()
    dim = report.dimensions["system_health"]
    assert dim.score == 75.0
    assert any("Failed" in w or "cached" in w for w in dim.warnings)


# =====================================================================
# H2: Per-collector timeout
# =====================================================================


@pytest.mark.asyncio
async def test_h2_timeout_triggers_cache_fallback(tmp_path):
    """A collector that exceeds the timeout should trigger cache fallback."""
    scorer = ProgressScorer(base_path=str(tmp_path))
    scorer.COLLECTOR_TIMEOUT_S = 0.1  # 100ms for test speed
    (tmp_path / "STATE").mkdir(parents=True)
    _write_history(tmp_path, {"system_health": 65.0})

    async def slow_collect():
        await asyncio.sleep(5)  # Way longer than 100ms timeout
        return DimensionScore(name="system_health", score=100.0)

    slow_collector = MagicMock()
    slow_collector.collect = slow_collect

    scorer._collectors = {"system_health": slow_collector}

    report = await scorer.score()
    # Should use cached score, not 0.0 or 100.0
    assert report.dimensions["system_health"].score == 65.0
    assert any("timed out" in w.lower() for w in report.warnings)


@pytest.mark.asyncio
async def test_h2_timeout_no_cache_falls_to_zero(tmp_path):
    """Timeout with no cache should fall back to 0.0."""
    scorer = ProgressScorer(base_path=str(tmp_path))
    scorer.COLLECTOR_TIMEOUT_S = 0.05
    (tmp_path / "STATE").mkdir(parents=True)

    async def slow_collect():
        await asyncio.sleep(5)
        return DimensionScore(name="x", score=100.0)

    slow_collector = MagicMock()
    slow_collector.collect = slow_collect
    scorer._collectors = {"system_health": slow_collector}

    report = await scorer.score()
    assert report.dimensions["system_health"].score == 0.0


@pytest.mark.asyncio
async def test_h2_fast_collector_not_affected(tmp_path):
    """Collectors that complete within timeout should work normally."""
    scorer = ProgressScorer(base_path=str(tmp_path))
    scorer.COLLECTOR_TIMEOUT_S = 5.0
    (tmp_path / "STATE").mkdir(parents=True)

    fast_collector = AsyncMock(return_value=DimensionScore(name="test", score=88.0))
    scorer._collectors = {"system_health": MagicMock(collect=fast_collector)}

    report = await scorer.score()
    assert report.dimensions["system_health"].score == 88.0
    assert not report.warnings  # No warnings for normal operation


# =====================================================================
# H1+H2: _last_cached_scores helper
# =====================================================================


def test_last_cached_scores_empty_history(tmp_path):
    """No history file → empty dict."""
    scorer = ProgressScorer(base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(parents=True)
    assert scorer._last_cached_scores() == {}


def test_last_cached_scores_returns_most_recent(tmp_path):
    """Should return scores from the most recent history entry."""
    scorer = ProgressScorer(base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(parents=True)
    history = [
        {
            "generated_at": "2026-03-26T10:00:00",
            "dimensions": {"system_health": {"score": 50.0}},
        },
        {
            "generated_at": "2026-03-27T10:00:00",
            "dimensions": {"system_health": {"score": 85.0}, "risk_engine": {"score": 70.0}},
        },
    ]
    (tmp_path / "STATE" / "progress_history.json").write_text(json.dumps(history))
    cached = scorer._last_cached_scores()
    assert cached["system_health"] == 85.0
    assert cached["risk_engine"] == 70.0


# =====================================================================
# M1: Task dedup window including .done files
# =====================================================================


@pytest.fixture
def tmp_task_gen(tmp_path):
    """Create a TaskSpecGenerator with a tmp base_path."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    gen = TaskSpecGenerator(base_path=str(tmp_path))
    return gen, tasks_dir


def test_m1_done_within_window_is_duplicate(tmp_task_gen):
    """A .done file modified recently should block duplicate creation."""
    gen, tasks_dir = tmp_task_gen
    done_file = tasks_dir / "0001_repair_system_health.md.done"
    done_file.write_text("done")
    # mtime is now → within 24h window

    assert gen.has_pending_task("repair", "system_health") is True


def test_m1_done_outside_window_not_duplicate(tmp_task_gen):
    """A .done file older than 24h should NOT block duplicate creation."""
    gen, tasks_dir = tmp_task_gen
    done_file = tasks_dir / "0001_repair_system_health.md.done"
    done_file.write_text("done")
    # Set mtime to 25 hours ago
    old_time = time.time() - (25 * 3600)
    os.utime(done_file, (old_time, old_time))

    assert gen.has_pending_task("repair", "system_health") is False


def test_m1_pending_md_still_blocks(tmp_task_gen):
    """Active .md files should still block as before."""
    gen, tasks_dir = tmp_task_gen
    (tasks_dir / "0001_repair_system_health.md").write_text("pending")
    assert gen.has_pending_task("repair", "system_health") is True


def test_m1_inprogress_still_blocks(tmp_task_gen):
    """Active .inprogress files should still block as before."""
    gen, tasks_dir = tmp_task_gen
    (tasks_dir / "0001_repair_system_health.md.inprogress").write_text("wip")
    # .inprogress suffix — should match
    assert gen.has_pending_task("repair", "system_health") is True


def test_m1_no_match_returns_false(tmp_task_gen):
    """Unrelated files should not match."""
    gen, tasks_dir = tmp_task_gen
    (tasks_dir / "0001_validate_recent_improvements.md.done").write_text("done")
    assert gen.has_pending_task("repair", "system_health") is False


def test_m1_write_task_dedup_respects_done_window(tmp_task_gen):
    """write_task_file should skip if a .done exists within the dedup window."""
    gen, tasks_dir = tmp_task_gen
    done_file = tasks_dir / "0001_repair_system_health.md.done"
    done_file.write_text("done")

    spec = TaskSpec(
        title="Repair system health regression",
        body="Fix it",
        category="repair",
        target_dimension="system_health",
    )
    result = gen.write_task_file(spec)
    assert result is None  # Should be skipped as duplicate


# =====================================================================
# M2: CLI per-dimension warnings (tested via print_scores output)
# =====================================================================


def test_m2_dimension_warnings_displayed(capsys):
    """Per-dimension warnings should appear in dashboard output."""
    from novatrade.autonomy.__main__ import print_scores
    from novatrade.autonomy.schemas import ProgressReport, ScoreTrend

    report = ProgressReport(
        overall_score=60.0,
        dimensions={
            "system_health": DimensionScore(
                name="system_health",
                score=80.0,
                warnings=["service novacore-watcher is inactive"],
            ),
            "risk_engine": DimensionScore(
                name="risk_engine",
                score=40.0,
                warnings=[],
            ),
        },
        trends={
            "system_health": ScoreTrend(current=80.0, direction="stable"),
            "risk_engine": ScoreTrend(current=40.0, direction="stable"),
        },
    )

    print_scores(report)
    output = capsys.readouterr().out

    assert "service novacore-watcher is inactive" in output
    # risk_engine has no warnings, so its name should appear but no "!" line for it


def test_m2_no_warnings_clean_output(capsys):
    """When no warnings exist, no '!' lines should appear."""
    from novatrade.autonomy.__main__ import print_scores
    from novatrade.autonomy.schemas import ProgressReport, ScoreTrend

    report = ProgressReport(
        overall_score=90.0,
        dimensions={
            "system_health": DimensionScore(name="system_health", score=90.0, warnings=[]),
        },
        trends={
            "system_health": ScoreTrend(current=90.0, direction="stable"),
        },
    )

    print_scores(report)
    output = capsys.readouterr().out
    assert "!" not in output


# =====================================================================
# M3: PerformanceCollector no-data flag
# =====================================================================


@pytest.mark.asyncio
async def test_m3_no_equity_data_explicit_warning(tmp_path):
    """When no equity data exists, PerformanceCollector should flag it clearly."""
    collector = PerformanceCollector(base_path=str(tmp_path))
    (tmp_path / "STATE" / "novatrade").mkdir(parents=True)
    # No equity_history.json → no data

    result = await collector.collect()
    assert any("No equity data available" in w or "placeholder" in w for w in result.warnings)
    # No-data state scores neutrally at 75.0 (same as idle-market) to prevent
    # false regression alarms.  Confidence 0.1 ensures this doesn't inflate
    # the weighted overall score, and the decision engine's confidence gate
    # prevents repair task generation on sparse data.
    assert result.score == 75.0
    assert result.confidence <= 0.2


@pytest.mark.asyncio
async def test_m3_partial_data_warning(tmp_path):
    """When equity data has only 1 entry, partial data warning should appear."""
    collector = PerformanceCollector(base_path=str(tmp_path))
    state_dir = tmp_path / "STATE" / "novatrade"
    state_dir.mkdir(parents=True)
    # 1 entry → insufficient for most metrics
    (state_dir / "equity_history.json").write_text(json.dumps([{"equity": 100000}]))

    result = await collector.collect()
    assert any("No equity data" in w or "placeholder" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_m3_sufficient_data_no_warning(tmp_path):
    """When equity data is sufficient, no no-data warning should appear."""
    collector = PerformanceCollector(base_path=str(tmp_path))
    state_dir = tmp_path / "STATE" / "novatrade"
    state_dir.mkdir(parents=True)
    # 10 entries → enough data for all metrics
    entries = [{"equity": 100000 + i * 100} for i in range(10)]
    (state_dir / "equity_history.json").write_text(json.dumps(entries))

    result = await collector.collect()
    assert not any("No equity data" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_m3_no_data_description_tagged(tmp_path):
    """Sub-metrics with no data should have [NO DATA] in their description."""
    collector = PerformanceCollector(base_path=str(tmp_path))
    (tmp_path / "STATE" / "novatrade").mkdir(parents=True)

    result = await collector.collect()
    no_data_metrics = [sm for sm in result.sub_metrics if "[NO DATA]" in sm.description]
    # S7 conditional metrics are excluded when their data producers are absent,
    # so only the 4 core metrics (sharpe, drawdown, win_rate, profit_factor) remain.
    assert len(no_data_metrics) == 4


# =====================================================================
# Integration: multiple failing collectors with mixed cache
# =====================================================================


@pytest.mark.asyncio
async def test_mixed_failures_with_partial_cache(tmp_path):
    """Multiple collector failures should each use their own cached score."""
    scorer = ProgressScorer(base_path=str(tmp_path))
    (tmp_path / "STATE").mkdir(parents=True)
    _write_history(
        tmp_path,
        {
            "system_health": 90.0,
            "execution_pipeline": 60.0,
            "strategy_validity": 80.0,
        },
    )

    ok_collector = AsyncMock(return_value=DimensionScore(name="ok", score=75.0))
    fail_collector = AsyncMock(side_effect=RuntimeError("fail"))

    scorer._collectors = {
        "system_health": MagicMock(collect=fail_collector),  # fails → cache 90.0
        "execution_pipeline": MagicMock(collect=ok_collector),  # ok → 75.0
        "strategy_validity": MagicMock(collect=fail_collector),  # fails → cache 80.0
    }

    report = await scorer.score()
    assert report.dimensions["system_health"].score == 90.0
    assert report.dimensions["execution_pipeline"].score == 75.0
    assert report.dimensions["strategy_validity"].score == 80.0
    assert len(report.warnings) == 4  # Two failures + two low-confidence warnings
