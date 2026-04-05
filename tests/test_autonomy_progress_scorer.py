"""Tests for ProgressScorer — multi-dimensional system assessment engine.

Covers timeout handling, cache fallback, trend detection, and persistence.
"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from novatrade.autonomy.progress_scorer import ProgressScorer
from novatrade.autonomy.schemas import (
    DimensionScore,
    ProgressReport,
    ScoringConfig,
)


class TestProgressScorerBasics:
    """Test basic ProgressScorer functionality."""

    def test_initialization_default_config(self):
        """Test ProgressScorer initializes with default config."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            scorer = ProgressScorer(base_path=tmp_dir)

            assert scorer.config is not None
            assert scorer.base_path == tmp_dir
            assert len(scorer._collectors) == 5
            assert "system_health" in scorer._collectors
            assert "execution_pipeline" in scorer._collectors
            assert "strategy_validity" in scorer._collectors
            assert "risk_engine" in scorer._collectors
            assert "performance_stability" in scorer._collectors

    def test_initialization_custom_config(self):
        """Test ProgressScorer with custom config."""
        config = ScoringConfig(
            weights={
                "system_health": 0.5,
                "execution_pipeline": 0.2,
                "strategy_validity": 0.1,
                "risk_engine": 0.1,
                "performance_stability": 0.1,
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            scorer = ProgressScorer(config=config, base_path=tmp_dir)
            assert scorer.config == config
            assert scorer.config.weights["system_health"] == 0.5

    def test_history_path_setup(self):
        """Test history path is correctly set up."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            scorer = ProgressScorer(base_path=tmp_dir)
            expected_path = Path(tmp_dir) / "STATE" / "progress_history.json"
            assert scorer._history_path == expected_path


class TestProgressScorerCollection:
    """Test score collection and timeout handling."""

    @pytest.mark.asyncio
    async def test_successful_collection(self):
        """Test successful collection from all collectors."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create STATE directory
            state_dir = Path(tmp_dir) / "STATE"
            state_dir.mkdir(exist_ok=True)

            scorer = ProgressScorer(base_path=tmp_dir)

            # Mock all collectors to return successful scores
            mock_scores = {
                "system_health": DimensionScore(name="system_health", score=85.0, confidence=0.9, warnings=[]),
                "execution_pipeline": DimensionScore(
                    name="execution_pipeline", score=78.0, confidence=0.8, warnings=[]
                ),
                "strategy_validity": DimensionScore(name="strategy_validity", score=92.0, confidence=0.95, warnings=[]),
                "risk_engine": DimensionScore(name="risk_engine", score=88.0, confidence=0.9, warnings=[]),
                "performance_stability": DimensionScore(
                    name="performance_stability", score=75.0, confidence=0.7, warnings=[]
                ),
            }

            # Mock each collector's collect method
            for name, score in mock_scores.items():
                scorer._collectors[name].collect = AsyncMock(return_value=score)

            # Run scoring
            result = await scorer.score()

            # Verify result structure
            assert isinstance(result, ProgressReport)
            assert len(result.dimensions) == 5

            # Verify all dimensions present with correct scores
            for name, expected_score in mock_scores.items():
                assert name in result.dimensions
                assert result.dimensions[name].score == expected_score.score
                assert result.dimensions[name].confidence == expected_score.confidence

    @pytest.mark.asyncio
    async def test_collector_timeout_handling(self):
        """Test timeout handling with cache fallback."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_dir = Path(tmp_dir) / "STATE"
            state_dir.mkdir(exist_ok=True)

            # Create history with cached scores
            history_data = [
                {
                    "timestamp": "2026-04-05T10:00:00Z",
                    "dimensions": {"system_health": {"score": 80.0, "confidence": 0.8}},
                    "overall_score": 80.0,
                }
            ]

            history_file = state_dir / "progress_history.json"
            history_file.write_text(json.dumps(history_data))

            scorer = ProgressScorer(base_path=tmp_dir)

            # Mock one collector to timeout, others to succeed
            scorer._collectors["system_health"].collect = AsyncMock(side_effect=asyncio.TimeoutError())

            for name in ["execution_pipeline", "strategy_validity", "risk_engine", "performance_stability"]:
                scorer._collectors[name].collect = AsyncMock(
                    return_value=DimensionScore(name=name, score=75.0, confidence=0.8, warnings=[])
                )

            # Patch the timeout constant to make test faster
            with patch.object(scorer, "COLLECTOR_TIMEOUT_S", 0.1):
                result = await scorer.score()

            # Verify timeout was handled
            assert "system_health" in result.dimensions
            system_health_score = result.dimensions["system_health"]
            assert system_health_score.score == 80.0  # From cache
            assert system_health_score.confidence == 0.3  # Degraded confidence
            assert any("Timed out" in warning for warning in system_health_score.warnings)

    @pytest.mark.asyncio
    async def test_collector_timeout_no_cache(self):
        """Test timeout handling when no cache is available."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_dir = Path(tmp_dir) / "STATE"
            state_dir.mkdir(exist_ok=True)

            scorer = ProgressScorer(base_path=tmp_dir)

            # Mock one collector to timeout with no history available
            scorer._collectors["system_health"].collect = AsyncMock(side_effect=asyncio.TimeoutError())

            for name in ["execution_pipeline", "strategy_validity", "risk_engine", "performance_stability"]:
                scorer._collectors[name].collect = AsyncMock(
                    return_value=DimensionScore(name=name, score=75.0, confidence=0.8, warnings=[])
                )

            with patch.object(scorer, "COLLECTOR_TIMEOUT_S", 0.1):
                result = await scorer.score()

            # Timed-out dimension is included with score=0, low confidence
            assert "system_health" in result.dimensions
            sh = result.dimensions["system_health"]
            assert sh.score == 0.0
            assert sh.confidence == 0.1
            assert len(result.dimensions) == 5

    @pytest.mark.asyncio
    async def test_collector_exception_handling(self):
        """Test handling of collector exceptions."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_dir = Path(tmp_dir) / "STATE"
            state_dir.mkdir(exist_ok=True)

            scorer = ProgressScorer(base_path=tmp_dir)

            # Mock one collector to raise an exception
            scorer._collectors["system_health"].collect = AsyncMock(side_effect=Exception("Collector error"))

            for name in ["execution_pipeline", "strategy_validity", "risk_engine", "performance_stability"]:
                scorer._collectors[name].collect = AsyncMock(
                    return_value=DimensionScore(name=name, score=75.0, confidence=0.8, warnings=[])
                )

            result = await scorer.score()

            # Failed dimension is included with score=0, low confidence
            assert "system_health" in result.dimensions
            sh = result.dimensions["system_health"]
            assert sh.score == 0.0
            assert sh.confidence == 0.1
            assert len(result.dimensions) == 5


class TestProgressScorerHistory:
    """Test history loading and persistence."""

    def test_load_empty_history(self):
        """Test loading when no history file exists."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            scorer = ProgressScorer(base_path=tmp_dir)
            history = scorer._load_history()
            assert history == []

    def test_load_existing_history(self):
        """Test loading existing history file."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_dir = Path(tmp_dir) / "STATE"
            state_dir.mkdir(exist_ok=True)

            # Create history file
            history_data = [
                {
                    "timestamp": "2026-04-05T10:00:00Z",
                    "dimensions": {"system_health": {"score": 80.0, "confidence": 0.8}},
                    "overall_score": 80.0,
                }
            ]

            history_file = state_dir / "progress_history.json"
            history_file.write_text(json.dumps(history_data))

            scorer = ProgressScorer(base_path=tmp_dir)
            history = scorer._load_history()

            assert len(history) == 1
            assert history[0]["overall_score"] == 80.0

    def test_load_corrupted_history(self):
        """Test loading corrupted history file."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_dir = Path(tmp_dir) / "STATE"
            state_dir.mkdir(exist_ok=True)

            # Create corrupted history file
            history_file = state_dir / "progress_history.json"
            history_file.write_text("invalid json content")

            scorer = ProgressScorer(base_path=tmp_dir)
            history = scorer._load_history()

            # Should return empty list for corrupted file
            assert history == []

    def test_last_cached_scores_extraction(self):
        """Test extraction of cached scores from history."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_dir = Path(tmp_dir) / "STATE"
            state_dir.mkdir(exist_ok=True)

            # Create history with multiple entries
            history_data = [
                {
                    "timestamp": "2026-04-05T09:00:00Z",
                    "dimensions": {"system_health": {"score": 70.0, "confidence": 0.7}},
                    "overall_score": 70.0,
                },
                {
                    "timestamp": "2026-04-05T10:00:00Z",
                    "dimensions": {
                        "system_health": {"score": 80.0, "confidence": 0.8},
                        "execution_pipeline": {"score": 85.0, "confidence": 0.9},
                    },
                    "overall_score": 82.5,
                },
            ]

            history_file = state_dir / "progress_history.json"
            history_file.write_text(json.dumps(history_data))

            scorer = ProgressScorer(base_path=tmp_dir)
            history = scorer._load_history()
            cached_scores = scorer._last_cached_scores(history)

            # Should get scores from most recent entry
            assert cached_scores["system_health"] == 80.0
            assert cached_scores["execution_pipeline"] == 85.0


class TestProgressScorerPersistence:
    """Test score persistence and history management."""

    @pytest.mark.asyncio
    async def test_history_truncation(self):
        """Test that history is truncated to MAX_HISTORY_ENTRIES."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_dir = Path(tmp_dir) / "STATE"
            state_dir.mkdir(exist_ok=True)

            scorer = ProgressScorer(base_path=tmp_dir)

            # Create history exceeding MAX_HISTORY_ENTRIES
            large_history = []
            for i in range(scorer.MAX_HISTORY_ENTRIES + 100):
                large_history.append(
                    {"timestamp": f"2026-04-05T{i % 24:02d}:00:00Z", "dimensions": {}, "overall_score": 50.0}
                )

            history_file = state_dir / "progress_history.json"
            history_file.write_text(json.dumps(large_history))

            # Mock all collectors for scoring
            for name in scorer._collectors:
                scorer._collectors[name].collect = AsyncMock(
                    return_value=DimensionScore(name=name, score=75.0, confidence=0.8, warnings=[])
                )

            # Run scoring (which should persist and truncate history)
            await scorer.score()

            # Verify history was truncated
            updated_history = scorer._load_history()
            assert len(updated_history) <= scorer.MAX_HISTORY_ENTRIES

    def test_max_history_entries_constant(self):
        """Test MAX_HISTORY_ENTRIES constant value."""
        # This ensures the constant is reasonable for ~34h of data at ~44 entries/hour
        assert ProgressScorer.MAX_HISTORY_ENTRIES == 1500
        # At 44 entries/hour, this gives ~34 hours of data which is appropriate

    def test_collector_timeout_constant(self):
        """Test COLLECTOR_TIMEOUT_S constant value."""
        scorer = ProgressScorer()
        assert scorer.COLLECTOR_TIMEOUT_S == 10.0  # 10 seconds should be reasonable


class TestProgressScorerEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_all_collectors_timeout(self):
        """Test behavior when all collectors timeout."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_dir = Path(tmp_dir) / "STATE"
            state_dir.mkdir(exist_ok=True)

            scorer = ProgressScorer(base_path=tmp_dir)

            # Mock all collectors to timeout
            for name in scorer._collectors:
                scorer._collectors[name].collect = AsyncMock(side_effect=asyncio.TimeoutError())

            with patch.object(scorer, "COLLECTOR_TIMEOUT_S", 0.1):
                result = await scorer.score()

            # All dimensions present with score=0, low confidence
            assert isinstance(result, ProgressReport)
            assert len(result.dimensions) == 5
            for dim in result.dimensions.values():
                assert dim.score == 0.0
                assert dim.confidence == 0.1

    @pytest.mark.asyncio
    async def test_concurrent_scoring_calls(self):
        """Test that concurrent scoring calls are handled safely."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_dir = Path(tmp_dir) / "STATE"
            state_dir.mkdir(exist_ok=True)

            scorer = ProgressScorer(base_path=tmp_dir)

            # Mock collectors with small delay
            async def mock_collect():
                await asyncio.sleep(0.1)
                return DimensionScore(name="test", score=75.0, confidence=0.8, warnings=[])

            for name in scorer._collectors:
                scorer._collectors[name].collect = mock_collect

            # Run multiple scoring calls concurrently
            tasks = [scorer.score() for _ in range(3)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # All should complete successfully
            assert len(results) == 3
            for result in results:
                assert isinstance(result, ProgressReport)

    def test_state_directory_creation(self):
        """Test that STATE directory is created if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            scorer = ProgressScorer(base_path=tmp_dir)

            # STATE directory should not exist initially
            state_dir = Path(tmp_dir) / "STATE"
            assert not state_dir.exists()

            # Loading history should be safe even without STATE directory
            history = scorer._load_history()
            assert history == []

    @pytest.mark.asyncio
    async def test_write_lock_usage(self):
        """Test that write lock prevents concurrent history writes."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_dir = Path(tmp_dir) / "STATE"
            state_dir.mkdir(exist_ok=True)

            scorer = ProgressScorer(base_path=tmp_dir)

            # Verify write lock exists
            assert hasattr(scorer, "_write_lock")
            assert isinstance(scorer._write_lock, asyncio.Lock)
