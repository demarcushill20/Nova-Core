"""Tests for memory retrieval quality metrics and evaluation framework (P9A.7).

Validates:
- IR metric calculations (NDCG@k, Recall@k, Precision@k, MRR)
- Golden retrieval corpus structure and coverage
- Evaluation harness corpus-level aggregation
- Edge cases (empty results, perfect ranking, no relevant docs)
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from tests.evals.retrieval_metrics import (
    dcg_at_k,
    evaluate_corpus,
    mean_mrr,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CORPUS_PATH = Path(__file__).resolve().parent / "memory_retrieval_corpus.json"

VALID_ROUTING_MODES = {"VECTOR", "GRAPH", "HYBRID", "TEMPORAL", "TEMPORAL_SEMANTIC", "DECISION", "PATTERN", "SESSION"}
VALID_CATEGORIES = {"temporal", "decision", "pattern", "semantic", "graph", "session", "dedup", "mixed", "edge"}


@pytest.fixture(scope="module")
def corpus() -> list[dict]:
    """Load the retrieval evaluation corpus."""
    assert CORPUS_PATH.exists(), f"Corpus missing: {CORPUS_PATH}"
    with open(CORPUS_PATH) as f:
        data = json.load(f)
    assert isinstance(data, list)
    return data


# ---------------------------------------------------------------------------
# NDCG tests
# ---------------------------------------------------------------------------


class TestNDCG:
    """Validate NDCG@k calculation."""

    def test_perfect_ranking(self) -> None:
        """If results are in ideal order, NDCG should be 1.0."""
        retrieved = ["a", "b", "c"]
        grades = {"a": 3, "b": 2, "c": 1}
        assert ndcg_at_k(retrieved, grades, k=3) == pytest.approx(1.0)

    def test_reversed_ranking(self) -> None:
        """Worst possible order should give NDCG < 1.0."""
        retrieved = ["c", "b", "a"]
        grades = {"a": 3, "b": 2, "c": 1}
        score = ndcg_at_k(retrieved, grades, k=3)
        assert 0.0 < score < 1.0

    def test_no_relevant_docs(self) -> None:
        """If no docs are relevant, NDCG should be 0.0."""
        retrieved = ["x", "y", "z"]
        grades = {"a": 3, "b": 2}
        assert ndcg_at_k(retrieved, grades, k=3) == pytest.approx(0.0)

    def test_empty_results(self) -> None:
        """Empty retrieved list should give 0.0."""
        assert ndcg_at_k([], {"a": 3}, k=5) == pytest.approx(0.0)

    def test_empty_grades(self) -> None:
        """No relevant docs in grades should give 0.0."""
        assert ndcg_at_k(["a", "b"], {}, k=5) == pytest.approx(0.0)

    def test_k_larger_than_results(self) -> None:
        """k > len(results) should work without error."""
        retrieved = ["a", "b"]
        grades = {"a": 3, "b": 2, "c": 1}
        score = ndcg_at_k(retrieved, grades, k=10)
        assert 0.0 < score <= 1.0

    def test_single_relevant_at_top(self) -> None:
        """Single relevant doc at position 1 should give NDCG = 1.0."""
        retrieved = ["a", "x", "y"]
        grades = {"a": 3}
        assert ndcg_at_k(retrieved, grades, k=3) == pytest.approx(1.0)

    def test_single_relevant_at_bottom(self) -> None:
        """Single relevant doc at last position should give NDCG < 1.0."""
        retrieved = ["x", "y", "a"]
        grades = {"a": 3}
        score = ndcg_at_k(retrieved, grades, k=3)
        assert 0.0 < score < 1.0


# ---------------------------------------------------------------------------
# DCG tests
# ---------------------------------------------------------------------------


class TestDCG:
    """Validate DCG@k calculation."""

    def test_known_value(self) -> None:
        """DCG for [3, 2, 1] at k=3 should be a known value."""
        # DCG = (2^3 - 1)/log2(2) + (2^2 - 1)/log2(3) + (2^1 - 1)/log2(4)
        expected = 7.0 / 1.0 + 3.0 / math.log2(3) + 1.0 / math.log2(4)
        assert dcg_at_k([3.0, 2.0, 1.0], k=3) == pytest.approx(expected)

    def test_all_zeros(self) -> None:
        """All irrelevant docs should give DCG = 0."""
        assert dcg_at_k([0.0, 0.0, 0.0], k=3) == pytest.approx(0.0)

    def test_empty(self) -> None:
        assert dcg_at_k([], k=5) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Recall tests
# ---------------------------------------------------------------------------


class TestRecall:
    """Validate Recall@k calculation."""

    def test_perfect_recall(self) -> None:
        retrieved = ["a", "b", "c"]
        expected = ["a", "b", "c"]
        assert recall_at_k(retrieved, expected, k=3) == pytest.approx(1.0)

    def test_partial_recall(self) -> None:
        retrieved = ["a", "x", "y"]
        expected = ["a", "b", "c"]
        assert recall_at_k(retrieved, expected, k=3) == pytest.approx(1.0 / 3.0)

    def test_zero_recall(self) -> None:
        retrieved = ["x", "y", "z"]
        expected = ["a", "b"]
        assert recall_at_k(retrieved, expected, k=3) == pytest.approx(0.0)

    def test_empty_expected(self) -> None:
        assert recall_at_k(["a", "b"], [], k=3) == pytest.approx(0.0)

    def test_empty_retrieved(self) -> None:
        assert recall_at_k([], ["a", "b"], k=3) == pytest.approx(0.0)

    def test_k_cutoff(self) -> None:
        """Recall should only consider top-k results."""
        retrieved = ["x", "y", "a"]  # 'a' is at position 3
        expected = ["a"]
        assert recall_at_k(retrieved, expected, k=2) == pytest.approx(0.0)
        assert recall_at_k(retrieved, expected, k=3) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Precision tests
# ---------------------------------------------------------------------------


class TestPrecision:
    """Validate Precision@k calculation."""

    def test_perfect_precision(self) -> None:
        retrieved = ["a", "b", "c"]
        expected = ["a", "b", "c"]
        assert precision_at_k(retrieved, expected, k=3) == pytest.approx(1.0)

    def test_half_precision(self) -> None:
        retrieved = ["a", "x", "b", "y"]
        expected = ["a", "b"]
        assert precision_at_k(retrieved, expected, k=4) == pytest.approx(0.5)

    def test_zero_precision(self) -> None:
        retrieved = ["x", "y", "z"]
        expected = ["a", "b"]
        assert precision_at_k(retrieved, expected, k=3) == pytest.approx(0.0)

    def test_empty_retrieved(self) -> None:
        assert precision_at_k([], ["a"], k=5) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# MRR tests
# ---------------------------------------------------------------------------


class TestMRR:
    """Validate Mean Reciprocal Rank calculation."""

    def test_first_position(self) -> None:
        assert mrr(["a", "b", "c"], ["a"]) == pytest.approx(1.0)

    def test_second_position(self) -> None:
        assert mrr(["x", "a", "c"], ["a"]) == pytest.approx(0.5)

    def test_third_position(self) -> None:
        assert mrr(["x", "y", "a"], ["a"]) == pytest.approx(1.0 / 3.0)

    def test_not_found(self) -> None:
        assert mrr(["x", "y", "z"], ["a"]) == pytest.approx(0.0)

    def test_multiple_expected(self) -> None:
        """MRR uses the first relevant result found."""
        assert mrr(["x", "b", "a"], ["a", "b"]) == pytest.approx(0.5)

    def test_mean_mrr(self) -> None:
        queries = [
            {"retrieved_ids": ["a", "b"], "expected_ids": ["a"]},
            {"retrieved_ids": ["x", "a"], "expected_ids": ["a"]},
        ]
        assert mean_mrr(queries) == pytest.approx(0.75)

    def test_empty_queries(self) -> None:
        assert mean_mrr([]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Corpus evaluation tests
# ---------------------------------------------------------------------------


class TestEvaluateCorpus:
    """Validate the full evaluation harness."""

    def test_perfect_corpus(self) -> None:
        """All queries return perfect results."""
        results = [
            {
                "query": "test query",
                "retrieved_ids": ["a", "b", "c"],
                "expected_ids": ["a", "b", "c"],
                "relevance_grades": {"a": 3, "b": 2, "c": 1},
            }
        ]
        metrics = evaluate_corpus(results, k_values=[3])
        assert metrics["ndcg@3"] == pytest.approx(1.0)
        assert metrics["recall@3"] == pytest.approx(1.0)
        assert metrics["precision@3"] == pytest.approx(1.0)
        assert metrics["mrr"] == pytest.approx(1.0)

    def test_empty_corpus(self) -> None:
        metrics = evaluate_corpus([], k_values=[5])
        assert metrics["ndcg@5"] == pytest.approx(0.0)
        assert metrics["mrr"] == pytest.approx(0.0)

    def test_mixed_quality(self) -> None:
        """Mix of good and bad results should give intermediate scores."""
        results = [
            {
                "query": "good",
                "retrieved_ids": ["a", "b"],
                "expected_ids": ["a", "b"],
                "relevance_grades": {"a": 3, "b": 2},
            },
            {
                "query": "bad",
                "retrieved_ids": ["x", "y"],
                "expected_ids": ["a", "b"],
                "relevance_grades": {"a": 3, "b": 2},
            },
        ]
        metrics = evaluate_corpus(results, k_values=[5])
        assert 0.0 < metrics["ndcg@5"] < 1.0
        assert metrics["recall@5"] == pytest.approx(0.5)
        assert metrics["mrr"] == pytest.approx(0.5)

    def test_default_k_values(self) -> None:
        """Default k_values should produce metrics for k=5 and k=10."""
        results = [
            {
                "query": "test",
                "retrieved_ids": ["a"],
                "expected_ids": ["a"],
                "relevance_grades": {"a": 3},
            }
        ]
        metrics = evaluate_corpus(results)
        assert "ndcg@5" in metrics
        assert "ndcg@10" in metrics
        assert "recall@5" in metrics
        assert "recall@10" in metrics


# ---------------------------------------------------------------------------
# Golden corpus structure tests
# ---------------------------------------------------------------------------


class TestCorpusStructure:
    """Validate the memory retrieval golden corpus."""

    def test_corpus_has_at_least_30_entries(self, corpus: list[dict]) -> None:
        assert len(corpus) >= 30, f"Need >= 30 entries, got {len(corpus)}"

    def test_ids_are_unique(self, corpus: list[dict]) -> None:
        ids = [c["id"] for c in corpus]
        assert len(ids) == len(set(ids)), f"Duplicate IDs: {[x for x in ids if ids.count(x) > 1]}"

    def test_required_fields(self, corpus: list[dict]) -> None:
        required = {"id", "query", "expected_routing", "expected_ids", "relevance_grades", "category", "description"}
        for entry in corpus:
            missing = required - set(entry.keys())
            assert not missing, f"Entry {entry.get('id', '?')} missing fields: {missing}"

    def test_all_categories_covered(self, corpus: list[dict]) -> None:
        categories = {c["category"] for c in corpus}
        required_categories = {"temporal", "decision", "pattern", "semantic", "graph", "session", "edge"}
        missing = required_categories - categories
        assert not missing, f"Missing categories: {missing}"

    def test_routing_modes_valid(self, corpus: list[dict]) -> None:
        for entry in corpus:
            assert entry["expected_routing"] in VALID_ROUTING_MODES, (
                f"Entry {entry['id']}: invalid routing mode '{entry['expected_routing']}'"
            )

    def test_relevance_grades_valid(self, corpus: list[dict]) -> None:
        """Grades should be 0-3 integers."""
        for entry in corpus:
            for mem_id, grade in entry["relevance_grades"].items():
                assert isinstance(grade, int) and 0 <= grade <= 3, (
                    f"Entry {entry['id']}: invalid grade {grade} for {mem_id}"
                )

    def test_expected_ids_are_in_grades(self, corpus: list[dict]) -> None:
        """All expected_ids should have a relevance grade."""
        for entry in corpus:
            if not entry["expected_ids"]:
                continue
            for eid in entry["expected_ids"]:
                assert eid in entry["relevance_grades"], (
                    f"Entry {entry['id']}: expected_id '{eid}' has no relevance grade"
                )

    def test_expected_ids_have_positive_grades(self, corpus: list[dict]) -> None:
        """Expected (relevant) IDs should have grade > 0."""
        for entry in corpus:
            for eid in entry["expected_ids"]:
                grade = entry["relevance_grades"].get(eid, 0)
                assert grade > 0, f"Entry {entry['id']}: expected_id '{eid}' has grade 0"

    def test_temporal_queries_have_temporal_routing(self, corpus: list[dict]) -> None:
        temporal_entries = [c for c in corpus if c["category"] == "temporal"]
        for entry in temporal_entries:
            assert entry["expected_routing"] in ("TEMPORAL", "TEMPORAL_SEMANTIC"), (
                f"Temporal entry {entry['id']} has routing '{entry['expected_routing']}'"
            )

    def test_decision_queries_have_decision_routing(self, corpus: list[dict]) -> None:
        decision_entries = [c for c in corpus if c["category"] == "decision"]
        for entry in decision_entries:
            assert entry["expected_routing"] == "DECISION", (
                f"Decision entry {entry['id']} has routing '{entry['expected_routing']}'"
            )

    def test_edge_cases_exist(self, corpus: list[dict]) -> None:
        edge_entries = [c for c in corpus if c["category"] == "edge"]
        assert len(edge_entries) >= 2, "Need at least 2 edge-case entries"
