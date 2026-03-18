"""Information Retrieval metrics for memory retrieval quality evaluation.

Implements standard IR metrics (NDCG@k, Recall@k, MRR) used to measure
how well the Fusion Memory retrieval pipeline surfaces relevant results.

These are pure-math functions with no external dependencies beyond stdlib.
"""

from __future__ import annotations

import math


def dcg_at_k(relevance_scores: list[float], k: int = 10) -> float:
    """Discounted Cumulative Gain at position k.

    DCG = sum_{i=1}^{k} (2^rel_i - 1) / log2(i + 1)

    Args:
        relevance_scores: Graded relevance of results in rank order.
        k: Cutoff position (must be >= 0).

    Returns:
        DCG value (non-negative float).

    Raises:
        ValueError: If k is negative.
    """
    if k < 0:
        raise ValueError(f"k must be non-negative, got {k}")
    if k == 0:
        return 0.0
    dcg = 0.0
    for i, rel in enumerate(relevance_scores[:k]):
        dcg += (2**rel - 1) / math.log2(i + 2)  # i+2 because log2(1)=0
    return dcg


def ndcg_at_k(
    retrieved_ids: list[str],
    relevance_grades: dict[str, int],
    k: int = 10,
) -> float:
    """Normalized Discounted Cumulative Gain at position k.

    Compares the actual ranking against the ideal ranking (sorted by
    relevance). Returns a value in [0, 1] where 1.0 means perfect ranking.

    Args:
        retrieved_ids: IDs returned by the retrieval system, in rank order.
        relevance_grades: Mapping of memory_id → relevance grade (0-3).
            0 = not relevant, 1 = marginally, 2 = relevant, 3 = highly relevant.
        k: Cutoff position.

    Returns:
        NDCG@k in [0, 1]. Returns 0.0 if no relevant docs exist.
    """
    if k < 0:
        raise ValueError(f"k must be non-negative, got {k}")
    if k == 0:
        return 0.0
    # Build actual relevance vector from retrieved IDs
    actual_rels = [float(relevance_grades.get(doc_id, 0)) for doc_id in retrieved_ids[:k]]

    # Build ideal relevance vector (best possible ranking)
    ideal_rels: list[float] = [float(r) for r in sorted(relevance_grades.values(), reverse=True)[:k]]

    actual_dcg = dcg_at_k(actual_rels, k)
    ideal_dcg = dcg_at_k(ideal_rels, k)

    if ideal_dcg == 0.0:
        return 0.0
    return actual_dcg / ideal_dcg


def recall_at_k(
    retrieved_ids: list[str],
    expected_ids: list[str],
    k: int = 10,
) -> float:
    """Recall at position k.

    Fraction of expected (relevant) IDs that appear in the top-k results.

    Args:
        retrieved_ids: IDs returned by the retrieval system, in rank order.
        expected_ids: IDs that should ideally be retrieved.
        k: Cutoff position.

    Returns:
        Recall@k in [0, 1]. Returns 0.0 if expected_ids is empty.
    """
    if k < 0:
        raise ValueError(f"k must be non-negative, got {k}")
    if k == 0:
        return 0.0
    if not expected_ids:
        return 0.0
    retrieved_set = set(retrieved_ids[:k])
    hits = sum(1 for eid in expected_ids if eid in retrieved_set)
    return hits / len(expected_ids)


def precision_at_k(
    retrieved_ids: list[str],
    expected_ids: list[str],
    k: int = 10,
) -> float:
    """Precision at position k.

    Fraction of top-k results that are relevant.

    Args:
        retrieved_ids: IDs returned by the retrieval system, in rank order.
        expected_ids: IDs that are considered relevant.
        k: Cutoff position.

    Returns:
        Precision@k in [0, 1]. Returns 0.0 if k is 0.
    """
    if k < 0:
        raise ValueError(f"k must be non-negative, got {k}")
    if k == 0:
        return 0.0
    top_k = list(dict.fromkeys(retrieved_ids[:k]))  # preserves order, removes dupes
    if not top_k:
        return 0.0
    expected_set = set(expected_ids)
    hits = sum(1 for rid in top_k if rid in expected_set)
    return hits / len(top_k)


def mrr(
    retrieved_ids: list[str],
    expected_ids: list[str],
) -> float:
    """Mean Reciprocal Rank.

    Reciprocal of the rank position of the first relevant result.
    For a single query, this is just RR (reciprocal rank).

    Args:
        retrieved_ids: IDs returned by the retrieval system, in rank order.
        expected_ids: IDs that are considered relevant.

    Returns:
        RR in [0, 1]. Returns 0.0 if no relevant result found.
    """
    expected_set = set(expected_ids)
    for i, doc_id in enumerate(retrieved_ids):
        if doc_id in expected_set:
            return 1.0 / (i + 1)
    return 0.0


def mean_mrr(
    queries: list[dict],
) -> float:
    """Mean Reciprocal Rank across multiple queries.

    Args:
        queries: List of dicts with 'retrieved_ids' and 'expected_ids' keys.

    Returns:
        Mean MRR across all queries.
    """
    if not queries:
        return 0.0
    total = sum(mrr(q["retrieved_ids"], q["expected_ids"]) for q in queries)
    return total / len(queries)


def evaluate_corpus(
    results: list[dict],
    k_values: list[int] | None = None,
) -> dict[str, float]:
    """Run all metrics across a full evaluation corpus.

    Args:
        results: List of dicts, each containing:
            - query: str (the query text)
            - retrieved_ids: List[str] (IDs returned by system)
            - expected_ids: List[str] (relevant IDs)
            - relevance_grades: Dict[str, int] (ID → grade mapping)
        k_values: Cutoff positions to evaluate. Defaults to [5, 10].

    Returns:
        Dict of metric_name → average_value across all queries.
    """
    if k_values is None:
        k_values = [5, 10]

    metrics: dict[str, float] = {}

    for k in k_values:
        ndcg_scores = []
        recall_scores = []
        precision_scores = []

        for r in results:
            retrieved = r["retrieved_ids"]
            expected = r["expected_ids"]
            grades = r.get("relevance_grades", {eid: 1 for eid in expected})

            ndcg_scores.append(ndcg_at_k(retrieved, grades, k))
            recall_scores.append(recall_at_k(retrieved, expected, k))
            precision_scores.append(precision_at_k(retrieved, expected, k))

        n = len(results)
        metrics[f"ndcg@{k}"] = sum(ndcg_scores) / n if n else 0.0
        metrics[f"recall@{k}"] = sum(recall_scores) / n if n else 0.0
        metrics[f"precision@{k}"] = sum(precision_scores) / n if n else 0.0

    # MRR (no k cutoff)
    mrr_scores = [mrr(r["retrieved_ids"], r["expected_ids"]) for r in results]
    metrics["mrr"] = sum(mrr_scores) / len(results) if results else 0.0

    return metrics
