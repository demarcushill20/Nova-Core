"""Interference checking for the skill optimization pipeline.

Tests whether a candidate description steals triggers from neighboring skills.
This is the primary cross-skill safety mechanism.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Import vendored eval runner
sys.path.insert(0, str(BASE_DIR / ".claude" / "skills" / "skill-creator"))
from scripts.run_eval import find_project_root, run_single_query  # noqa: E402


@dataclass
class InterferenceResult:
    """Result of interference checking for one neighbor."""

    neighbor_skill: str
    queries_tested: int = 0
    false_triggers: int = 0
    conflict_rate: float = 0.0
    details: list[dict] = field(default_factory=list)


@dataclass
class InterferenceReport:
    """Aggregate interference report across all neighbors."""

    skill_name: str
    candidate_description: str
    neighbors_tested: int = 0
    total_queries: int = 0
    total_false_triggers: int = 0
    max_conflict_rate: float = 0.0
    aggregate_conflict_rate: float = 0.0
    results: list[InterferenceResult] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "skill_name": self.skill_name,
            "neighbors_tested": self.neighbors_tested,
            "total_queries": self.total_queries,
            "total_false_triggers": self.total_false_triggers,
            "max_conflict_rate": round(self.max_conflict_rate, 4),
            "aggregate_conflict_rate": round(self.aggregate_conflict_rate, 4),
            "elapsed_seconds": self.elapsed_seconds,
            "results": [
                {
                    "neighbor": r.neighbor_skill,
                    "queries_tested": r.queries_tested,
                    "false_triggers": r.false_triggers,
                    "conflict_rate": round(r.conflict_rate, 4),
                }
                for r in self.results
            ],
        }


def load_neighbor_eval_set(neighbor_name: str, evals_dir: Path | None = None) -> list[dict]:
    """Load positive examples from a neighbor's eval sets.

    These are queries that SHOULD trigger the neighbor but should NOT
    trigger the skill being optimized.
    """
    base = evals_dir or (BASE_DIR / "evals")
    queries: list[dict] = []

    # Load neighbor's positive training examples
    for split in ["train.jsonl", "test.jsonl"]:
        path = base / neighbor_name / split
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                import json

                record = json.loads(line)
                if record.get("should_trigger"):
                    queries.append(record)
            except Exception:  # noqa: S112
                continue

    return queries


def check_interference(
    skill_name: str,
    candidate_description: str,
    neighbor_names: list[str],
    evals_dir: Path | None = None,
    num_workers: int = 5,
    timeout: int = 30,
    model: str | None = None,
    max_queries_per_neighbor: int = 10,
) -> InterferenceReport:
    """Check if a candidate description interferes with neighbors.

    Tests the candidate description against each neighbor's positive queries.
    A "false trigger" means the candidate fires on a query that belongs
    to the neighbor.
    """
    project_root = find_project_root()
    report = InterferenceReport(
        skill_name=skill_name,
        candidate_description=candidate_description,
    )

    t0 = time.time()

    for neighbor in neighbor_names:
        queries = load_neighbor_eval_set(neighbor, evals_dir)
        if not queries:
            continue

        # Limit queries per neighbor for speed
        queries = queries[:max_queries_per_neighbor]

        result = InterferenceResult(
            neighbor_skill=neighbor,
            queries_tested=len(queries),
        )

        for query_record in queries:
            query_text = query_record["query"]
            try:
                triggered = run_single_query(
                    query=query_text,
                    skill_name=skill_name,
                    skill_description=candidate_description,
                    timeout=timeout,
                    project_root=str(project_root),
                    model=model,
                )
                if triggered:
                    result.false_triggers += 1
                    result.details.append(
                        {
                            "query": query_text,
                            "triggered": True,
                            "neighbor": neighbor,
                        }
                    )
            except Exception:  # noqa: S110
                pass  # Skip failed queries

        if result.queries_tested > 0:
            result.conflict_rate = result.false_triggers / result.queries_tested

        report.results.append(result)

    report.elapsed_seconds = round(time.time() - t0, 2)
    report.neighbors_tested = len(report.results)
    report.total_queries = sum(r.queries_tested for r in report.results)
    report.total_false_triggers = sum(r.false_triggers for r in report.results)

    if report.results:
        report.max_conflict_rate = max(r.conflict_rate for r in report.results)

    if report.total_queries > 0:
        report.aggregate_conflict_rate = report.total_false_triggers / report.total_queries

    return report
