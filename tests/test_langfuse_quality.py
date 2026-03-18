"""Tests for utils/langfuse_quality.py — Phase 7 Step 7.10.

All tests mock the Langfuse client so no real connection is required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — reset module-level singleton between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_langfuse():
    """Reset the cached Langfuse client before each test."""
    from utils import langfuse_quality

    langfuse_quality._reset_client()
    yield
    langfuse_quality._reset_client()


def _make_mock_client():
    """Return a MagicMock that behaves like a Langfuse client."""
    client = MagicMock()
    client.create_score = MagicMock(return_value=None)
    return client


# =========================================================================
# record_quality_score
# =========================================================================


class TestRecordQualityScore:
    """Tests for record_quality_score."""

    def test_returns_false_when_langfuse_unavailable(self):
        """Should return False and not raise when Langfuse is missing."""
        from utils.langfuse_quality import record_quality_score

        with patch("utils.langfuse_quality._get_langfuse", return_value=None):
            result = record_quality_score(trace_id="t1", category="heartbeat", score=0.85)
        assert result is False

    def test_returns_true_when_langfuse_available(self):
        """Should call create_score and return True."""
        from utils.langfuse_quality import record_quality_score

        mock_client = _make_mock_client()
        with patch("utils.langfuse_quality._get_langfuse", return_value=mock_client):
            result = record_quality_score(
                trace_id="t1",
                category="codegen",
                score=0.92,
                rating="A",
                comment="Excellent output",
            )
        assert result is True
        mock_client.create_score.assert_called_once()
        call_kwargs = mock_client.create_score.call_args.kwargs
        assert call_kwargs["trace_id"] == "t1"
        assert call_kwargs["name"] == "quality_codegen"
        assert call_kwargs["value"] == 0.92
        assert call_kwargs["data_type"] == "NUMERIC"
        assert "Excellent output" in call_kwargs["comment"]

    def test_returns_false_when_create_score_raises(self):
        """Should catch exceptions from Langfuse and return False."""
        from utils.langfuse_quality import record_quality_score

        mock_client = _make_mock_client()
        mock_client.create_score.side_effect = RuntimeError("API down")
        with patch("utils.langfuse_quality._get_langfuse", return_value=mock_client):
            result = record_quality_score(trace_id="t1", category="heartbeat", score=0.5)
        assert result is False

    def test_default_comment_includes_rating(self):
        """When no comment is given, default includes the rating string."""
        from utils.langfuse_quality import record_quality_score

        mock_client = _make_mock_client()
        with patch("utils.langfuse_quality._get_langfuse", return_value=mock_client):
            record_quality_score(trace_id="t1", category="plan", score=0.7, rating="B")
        call_kwargs = mock_client.create_score.call_args.kwargs
        assert "(B)" in call_kwargs["comment"]

    def test_metadata_passed_through(self):
        """Custom metadata should be merged into the score metadata."""
        from utils.langfuse_quality import record_quality_score

        mock_client = _make_mock_client()
        with patch("utils.langfuse_quality._get_langfuse", return_value=mock_client):
            record_quality_score(
                trace_id="t1",
                category="research",
                score=0.8,
                metadata={"step": "7.10"},
            )
        call_kwargs = mock_client.create_score.call_args.kwargs
        assert call_kwargs["metadata"]["step"] == "7.10"
        assert call_kwargs["metadata"]["category"] == "research"

    def test_no_exception_on_any_failure(self):
        """Absolutely no exception should propagate — graceful degradation."""
        from utils.langfuse_quality import record_quality_score

        # Case 1: create_score raises an exception
        mock_client = _make_mock_client()
        mock_client.create_score.side_effect = Exception("network error")
        with patch("utils.langfuse_quality._get_langfuse", return_value=mock_client):
            result = record_quality_score(trace_id="t1", category="heartbeat", score=0.5)
        assert result is False

        # Case 2: Langfuse client is None (unavailable)
        with patch("utils.langfuse_quality._get_langfuse", return_value=None):
            result = record_quality_score(trace_id="t2", category="codegen", score=0.9)
        assert result is False


# =========================================================================
# record_batch_scores
# =========================================================================


class TestRecordBatchScores:
    """Tests for record_batch_scores."""

    def test_counts_successful_records(self):
        """Should return the number of successfully recorded scores."""
        from utils.langfuse_quality import record_batch_scores

        mock_client = _make_mock_client()
        with patch("utils.langfuse_quality._get_langfuse", return_value=mock_client):
            count = record_batch_scores(
                [
                    {"trace_id": "t1", "category": "heartbeat", "score": 0.9},
                    {"trace_id": "t2", "category": "codegen", "score": 0.8},
                    {"trace_id": "t3", "category": "plan", "score": 0.7},
                ]
            )
        assert count == 3
        assert mock_client.create_score.call_count == 3

    def test_partial_failure(self):
        """Should count only successful records when some fail."""
        from utils.langfuse_quality import record_batch_scores

        mock_client = _make_mock_client()
        # Fail on second call
        mock_client.create_score.side_effect = [None, RuntimeError("fail"), None]
        with patch("utils.langfuse_quality._get_langfuse", return_value=mock_client):
            count = record_batch_scores(
                [
                    {"trace_id": "t1", "category": "heartbeat", "score": 0.9},
                    {"trace_id": "t2", "category": "codegen", "score": 0.8},
                    {"trace_id": "t3", "category": "plan", "score": 0.7},
                ]
            )
        assert count == 2

    def test_empty_batch(self):
        """Empty input should return 0."""
        from utils.langfuse_quality import record_batch_scores

        assert record_batch_scores([]) == 0

    def test_all_fail_when_langfuse_unavailable(self):
        """All scores should fail gracefully when Langfuse is down."""
        from utils.langfuse_quality import record_batch_scores

        with patch("utils.langfuse_quality._get_langfuse", return_value=None):
            count = record_batch_scores(
                [
                    {"trace_id": "t1", "category": "heartbeat", "score": 0.9},
                    {"trace_id": "t2", "category": "codegen", "score": 0.8},
                ]
            )
        assert count == 0


# =========================================================================
# check_quality_regression
# =========================================================================


class TestCheckQualityRegression:
    """Tests for check_quality_regression."""

    def test_detects_regression_below_threshold(self):
        """Score below threshold should flag regression."""
        from utils.langfuse_quality import check_quality_regression

        result = check_quality_regression("heartbeat", 0.45)
        assert result["regression"] is True
        assert result["threshold"] == 0.60
        assert result["margin"] < 0
        assert result["category"] == "heartbeat"
        assert result["score"] == 0.45

    def test_no_regression_above_threshold(self):
        """Score above threshold should not flag regression."""
        from utils.langfuse_quality import check_quality_regression

        result = check_quality_regression("heartbeat", 0.85)
        assert result["regression"] is False
        assert result["margin"] > 0

    def test_exact_threshold_is_not_regression(self):
        """Score exactly at threshold should not be flagged."""
        from utils.langfuse_quality import check_quality_regression

        result = check_quality_regression("heartbeat", 0.60)
        assert result["regression"] is False
        assert result["margin"] == 0.0

    def test_just_below_threshold_is_regression(self):
        """Score just below threshold should be flagged."""
        from utils.langfuse_quality import check_quality_regression

        result = check_quality_regression("codegen", 0.6999)
        assert result["regression"] is True

    def test_unknown_category_uses_default_threshold(self):
        """Unknown categories should use 0.60 as default threshold."""
        from utils.langfuse_quality import check_quality_regression

        result = check_quality_regression("unknown_category", 0.55)
        assert result["regression"] is True
        assert result["threshold"] == 0.60

        result_ok = check_quality_regression("unknown_category", 0.65)
        assert result_ok["regression"] is False

    def test_all_known_categories_have_thresholds(self):
        """All expected categories should be in the threshold map."""
        from utils.langfuse_quality import REGRESSION_THRESHOLDS

        expected = {"heartbeat", "codegen", "research", "plan", "bugfix", "novatrade"}
        assert expected == set(REGRESSION_THRESHOLDS.keys())

    def test_all_thresholds_are_valid(self):
        """All thresholds should be between 0 and 1."""
        from utils.langfuse_quality import REGRESSION_THRESHOLDS

        for cat, threshold in REGRESSION_THRESHOLDS.items():
            assert 0.0 < threshold < 1.0, f"{cat} threshold {threshold} out of range"

    @pytest.mark.parametrize(
        "category,threshold",
        [
            ("heartbeat", 0.60),
            ("codegen", 0.70),
            ("research", 0.60),
            ("plan", 0.65),
            ("bugfix", 0.65),
            ("novatrade", 0.70),
        ],
    )
    def test_specific_threshold_values(self, category, threshold):
        """Each category should have its documented threshold."""
        from utils.langfuse_quality import REGRESSION_THRESHOLDS

        assert REGRESSION_THRESHOLDS[category] == threshold

    def test_score_zero_is_regression(self):
        """Score of 0.0 should always be a regression."""
        from utils.langfuse_quality import check_quality_regression

        for cat in ("heartbeat", "codegen", "research", "plan", "bugfix", "novatrade"):
            result = check_quality_regression(cat, 0.0)
            assert result["regression"] is True

    def test_score_one_is_never_regression(self):
        """Score of 1.0 should never be a regression."""
        from utils.langfuse_quality import check_quality_regression

        for cat in ("heartbeat", "codegen", "research", "plan", "bugfix", "novatrade"):
            result = check_quality_regression(cat, 1.0)
            assert result["regression"] is False

    def test_margin_calculation(self):
        """Margin should be score - threshold, rounded to 4 places."""
        from utils.langfuse_quality import check_quality_regression

        result = check_quality_regression("codegen", 0.75)
        assert result["margin"] == 0.05


# =========================================================================
# get_quality_trend
# =========================================================================


class TestGetQualityTrend:
    """Tests for get_quality_trend (currently a placeholder)."""

    def test_returns_none_when_unavailable(self):
        """Should return None when Langfuse is not available."""
        from utils.langfuse_quality import get_quality_trend

        with patch("utils.langfuse_quality._get_langfuse", return_value=None):
            assert get_quality_trend("heartbeat") is None

    def test_returns_none_placeholder(self):
        """Currently a placeholder — should return None even with client."""
        from utils.langfuse_quality import get_quality_trend

        mock_client = _make_mock_client()
        with patch("utils.langfuse_quality._get_langfuse", return_value=mock_client):
            assert get_quality_trend("heartbeat", days=7) is None


# =========================================================================
# Lazy initialization
# =========================================================================


class TestLazyInit:
    """Tests for the lazy initialization pattern."""

    def test_reuses_langfuse_tracing_client(self):
        """Should try to reuse the client from langfuse_tracing."""
        from utils import langfuse_quality

        mock_client = _make_mock_client()
        with patch(
            "utils.langfuse_quality.get_langfuse",
            create=True,
        ):
            # Patch the import inside _get_langfuse
            mock_get = MagicMock(return_value=mock_client)
            with patch.dict(
                "sys.modules",
                {"utils.langfuse_tracing": MagicMock(get_langfuse=mock_get)},
            ):
                langfuse_quality._reset_client()
                client = langfuse_quality._get_langfuse()
                assert client is mock_client

    def test_caches_client_after_first_init(self):
        """Subsequent calls should return the cached client."""
        from utils import langfuse_quality

        mock_client = _make_mock_client()
        with patch("utils.langfuse_tracing.get_langfuse", return_value=mock_client):
            langfuse_quality._reset_client()
            first = langfuse_quality._get_langfuse()
            second = langfuse_quality._get_langfuse()
            assert first is second is mock_client

    def test_does_not_retry_after_failed_init(self):
        """Once init fails, should not retry on subsequent calls."""
        from utils import langfuse_quality

        with (
            patch("utils.langfuse_tracing.get_langfuse", return_value=None),
            patch("langfuse.Langfuse", side_effect=RuntimeError("no keys")),
        ):
            langfuse_quality._reset_client()
            first = langfuse_quality._get_langfuse()
            assert first is None
            # Second call should not retry
            second = langfuse_quality._get_langfuse()
            assert second is None


# =========================================================================
# Graceful degradation (integration-style)
# =========================================================================


class TestGracefulDegradation:
    """Ensure no exceptions leak out under any failure condition."""

    def test_record_score_no_exception_on_import_error(self):
        """record_quality_score should not raise even if langfuse import fails."""
        from utils.langfuse_quality import record_quality_score

        with patch("utils.langfuse_quality._get_langfuse", return_value=None):
            result = record_quality_score(trace_id="t1", category="heartbeat", score=0.5)
            assert result is False

    def test_batch_scores_no_exception_on_failure(self):
        """record_batch_scores should not raise even if all scores fail."""
        from utils.langfuse_quality import record_batch_scores

        mock_client = _make_mock_client()
        mock_client.create_score.side_effect = Exception("total failure")
        with patch("utils.langfuse_quality._get_langfuse", return_value=mock_client):
            count = record_batch_scores(
                [
                    {"trace_id": "t1", "category": "heartbeat", "score": 0.9},
                    {"trace_id": "t2", "category": "codegen", "score": 0.8},
                ]
            )
            assert count == 0

    def test_check_regression_never_raises(self):
        """check_quality_regression is pure logic — should never raise."""
        from utils.langfuse_quality import check_quality_regression

        # Edge cases
        check_quality_regression("", 0.0)
        check_quality_regression("heartbeat", -1.0)
        check_quality_regression("heartbeat", 999.0)
        check_quality_regression("nonexistent", 0.5)
