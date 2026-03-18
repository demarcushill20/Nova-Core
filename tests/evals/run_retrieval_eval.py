# ruff: noqa: ASYNC230
#!/usr/bin/env python3
"""Memory retrieval evaluation harness (P9A.7).

Runs golden queries through the Fusion Memory MCP and measures retrieval
quality using NDCG@k, Recall@k, Precision@k, and MRR.

Usage:
    # Run evaluation against live system
    python tests/evals/run_retrieval_eval.py

    # Save baseline
    python tests/evals/run_retrieval_eval.py --save-baseline

    # Compare against baseline
    python tests/evals/run_retrieval_eval.py --compare baseline_2026-03-18.json

    # A/B comparison with custom config
    python tests/evals/run_retrieval_eval.py --ab-config '{"reranker": false}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.evals.retrieval_metrics import evaluate_corpus, mrr, ndcg_at_k, recall_at_k

CORPUS_PATH = Path(__file__).resolve().parent / "memory_retrieval_corpus.json"
BASELINE_DIR = Path(__file__).resolve().parent / "baselines"


def load_corpus() -> list[dict[str, Any]]:
    """Load the golden retrieval evaluation corpus."""
    with open(CORPUS_PATH) as f:
        return json.load(f)


async def run_query_via_mcp(
    query: str,
    top_k: int = 15,
) -> list[dict[str, Any]]:
    """Execute a query_memory call against the Fusion Memory MCP.

    Returns a list of result dicts with at least an 'id' field.

    Note: This requires the nova-memory MCP server to be running.
    For offline testing, use mock_query_results() instead.
    """
    try:
        # Import the MCP client — this depends on the nova-memory server being accessible
        # In production, this connects via stdio or SSE
        # For now, attempt a direct HTTP call if the server exposes a REST endpoint
        import aiohttp

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                "http://localhost:8765/query",
                json={"query": query, "top_k_final": top_k},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp,
        ):
            if resp.status == 200:
                data = await resp.json()
                return data.get("results", [])
            return []
    except Exception:
        return []


def mock_query_results(query_entry: dict[str, Any]) -> list[str]:
    """Generate mock retrieved IDs for offline evaluation testing.

    Returns expected_ids + some noise to simulate realistic retrieval.
    This is only for testing the eval harness itself.
    """
    # Return expected IDs in order, plus some irrelevant noise
    expected = list(query_entry["expected_ids"])
    noise = [f"noise-{i}" for i in range(5)]
    # Simulate imperfect retrieval: shuffle some noise in
    if len(expected) >= 2:
        return [expected[0], noise[0], expected[1]] + noise[1:3] + expected[2:]
    return expected + noise


async def run_evaluation(
    corpus: list[dict[str, Any]],
    live: bool = False,
    top_k: int = 15,
) -> dict[str, Any]:
    """Run the full evaluation.

    Args:
        corpus: Golden evaluation corpus entries.
        live: If True, query the live MCP server. If False, use mock results.
        top_k: Number of results to request.

    Returns:
        Dict with per-query results and aggregate metrics.
    """
    results = []
    query_details = []

    for entry in corpus:
        query = entry["query"]
        expected_ids = entry["expected_ids"]
        grades = entry["relevance_grades"]

        if not query:
            # Skip empty queries for live evaluation
            retrieved_ids: list[str] = []
        elif live:
            raw_results = await run_query_via_mcp(query, top_k)
            retrieved_ids = [r.get("id", "") for r in raw_results]
        else:
            retrieved_ids = mock_query_results(entry)

        result_entry = {
            "query": query,
            "retrieved_ids": retrieved_ids,
            "expected_ids": expected_ids,
            "relevance_grades": grades,
        }
        results.append(result_entry)

        # Per-query metrics
        query_details.append(
            {
                "id": entry["id"],
                "query": query,
                "category": entry["category"],
                "expected_routing": entry["expected_routing"],
                "ndcg@5": ndcg_at_k(retrieved_ids, grades, k=5),
                "ndcg@10": ndcg_at_k(retrieved_ids, grades, k=10),
                "recall@5": recall_at_k(retrieved_ids, expected_ids, k=5),
                "recall@10": recall_at_k(retrieved_ids, expected_ids, k=10),
                "mrr": mrr(retrieved_ids, expected_ids),
                "retrieved_count": len(retrieved_ids),
                "expected_count": len(expected_ids),
            }
        )

    # Aggregate metrics
    # Filter out edge cases with no expected results for fair scoring
    scorable = [r for r in results if r["expected_ids"]]
    aggregate = evaluate_corpus(scorable, k_values=[5, 10])

    # Category breakdown
    categories: dict[str, list[dict]] = {}
    for detail in query_details:
        cat = detail["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(detail)

    category_metrics: dict[str, dict[str, float]] = {}
    for cat, _entries in categories.items():
        cat_results = [
            r for r, d in zip(results, query_details, strict=False) if d["category"] == cat and r["expected_ids"]
        ]
        if cat_results:
            category_metrics[cat] = evaluate_corpus(cat_results, k_values=[5, 10])

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "corpus_size": len(corpus),
        "scorable_queries": len(scorable),
        "mode": "live" if live else "mock",
        "aggregate_metrics": aggregate,
        "category_metrics": category_metrics,
        "query_details": query_details,
    }


def print_report(eval_result: dict[str, Any]) -> None:
    """Print a human-readable evaluation report."""
    print("=" * 70)
    print("  MEMORY RETRIEVAL QUALITY EVALUATION")
    print("=" * 70)
    print(f"  Timestamp:  {eval_result['timestamp']}")
    print(f"  Mode:       {eval_result['mode']}")
    print(f"  Corpus:     {eval_result['corpus_size']} queries ({eval_result['scorable_queries']} scorable)")
    print()

    agg = eval_result["aggregate_metrics"]
    print("  AGGREGATE METRICS")
    print("  " + "-" * 40)
    for metric, value in sorted(agg.items()):
        print(f"  {metric:20s} {value:.4f}")
    print()

    print("  CATEGORY BREAKDOWN")
    print("  " + "-" * 40)
    for cat, metrics in sorted(eval_result["category_metrics"].items()):
        ndcg = metrics.get("ndcg@10", 0)
        recall = metrics.get("recall@10", 0)
        print(f"  {cat:20s} NDCG@10={ndcg:.3f}  Recall@10={recall:.3f}")
    print()

    # Show worst-performing queries
    details = eval_result["query_details"]
    scorable_details = [d for d in details if d["expected_count"] > 0]
    worst = sorted(scorable_details, key=lambda d: d["ndcg@10"])[:5]
    if worst:
        print("  LOWEST NDCG@10 QUERIES")
        print("  " + "-" * 40)
        for d in worst:
            print(f"  [{d['id']}] NDCG@10={d['ndcg@10']:.3f} | {d['query'][:50]}")
    print("=" * 70)


def compare_baselines(
    current: dict[str, Any],
    baseline: dict[str, Any],
) -> None:
    """Compare current evaluation against a saved baseline."""
    print()
    print("  BASELINE COMPARISON")
    print("  " + "-" * 50)
    print(f"  {'Metric':20s} {'Baseline':>10s} {'Current':>10s} {'Delta':>10s}")
    print("  " + "-" * 50)

    base_agg = baseline["aggregate_metrics"]
    curr_agg = current["aggregate_metrics"]

    for metric in sorted(set(base_agg) | set(curr_agg)):
        base_val = base_agg.get(metric, 0.0)
        curr_val = curr_agg.get(metric, 0.0)
        delta = curr_val - base_val
        sign = "+" if delta >= 0 else ""
        print(f"  {metric:20s} {base_val:10.4f} {curr_val:10.4f} {sign}{delta:9.4f}")

    print()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Memory Retrieval Quality Evaluation")
    parser.add_argument("--live", action="store_true", help="Query live MCP server")
    parser.add_argument("--save-baseline", action="store_true", help="Save results as baseline")
    parser.add_argument("--compare", type=str, help="Compare against a baseline JSON file")
    parser.add_argument("--output", type=str, help="Write results to JSON file")
    parser.add_argument("--top-k", type=int, default=15, help="Number of results to request")
    args = parser.parse_args()

    corpus = load_corpus()
    print(f"Loaded {len(corpus)} evaluation queries")

    eval_result = await run_evaluation(corpus, live=args.live, top_k=args.top_k)
    print_report(eval_result)

    # Save baseline
    if args.save_baseline:
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        baseline_path = BASELINE_DIR / f"baseline_{date_str}.json"
        with open(baseline_path, "w") as f:
            json.dump(eval_result, f, indent=2)
        print(f"  Baseline saved to: {baseline_path}")

    # Compare against baseline
    if args.compare:
        baseline_path = Path(args.compare)
        if not baseline_path.exists():
            baseline_path = BASELINE_DIR / args.compare
        if baseline_path.exists():
            with open(baseline_path) as f:
                baseline = json.load(f)
            compare_baselines(eval_result, baseline)
        else:
            print(f"  WARNING: Baseline not found: {args.compare}")

    # Write output
    if args.output:
        with open(args.output, "w") as f:
            json.dump(eval_result, f, indent=2)
        print(f"  Results saved to: {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
