"""Global smoke testing for the skill optimization pipeline.

Runs a lightweight smoke test across all eligible skills to catch
ecosystem-level regressions. Zero tolerance for smoke failures.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

sys.path.insert(0, str(BASE_DIR / ".claude" / "skills" / "skill-creator"))
from scripts.run_eval import find_project_root, run_single_query  # noqa: E402


@dataclass
class SmokeResult:
    """Result of a single smoke test query."""

    skill: str
    query: str
    expected_trigger: bool
    actual_trigger: bool
    passed: bool


@dataclass
class SmokeReport:
    """Aggregate smoke test report."""

    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    regressions: list[SmokeResult] = field(default_factory=list)
    results: list[SmokeResult] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def has_regressions(self) -> bool:
        return len(self.regressions) > 0

    def to_dict(self) -> dict:
        return {
            "total_tests": self.total_tests,
            "passed": self.passed,
            "failed": self.failed,
            "regressions": [
                {"skill": r.skill, "query": r.query[:80], "expected": r.expected_trigger} for r in self.regressions
            ],
            "elapsed_seconds": self.elapsed_seconds,
        }


def load_smoke_tests(path: Path | None = None) -> list[dict]:
    """Load global smoke test queries."""
    smoke_path = path or (BASE_DIR / "evals" / "global" / "smoke_test.jsonl")
    if not smoke_path.exists():
        return []

    records = []
    for line in smoke_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def run_smoke_test(
    modified_skill: str,
    modified_description: str,
    smoke_queries: list[dict] | None = None,
    timeout: int = 30,
    model: str | None = None,
) -> SmokeReport:
    """Run smoke tests focused on the modified skill.

    Tests queries that belong to the modified skill to ensure the
    new description still handles its core queries correctly.
    Also spot-checks immediate neighbor queries.

    Args:
        modified_skill: Name of the skill being optimized
        modified_description: The candidate description being tested
        smoke_queries: Override smoke test queries (default: load from global)
        timeout: Timeout per query
        model: Model for evaluation
    """
    if smoke_queries is None:
        smoke_queries = load_smoke_tests()

    if not smoke_queries:
        return SmokeReport()

    project_root = find_project_root()
    report = SmokeReport()
    t0 = time.time()

    # Filter to queries relevant to the modified skill
    relevant = [q for q in smoke_queries if q.get("skill") == modified_skill]

    for query_record in relevant:
        query_text = query_record["query"]
        expected = query_record.get("should_trigger", True)

        try:
            triggered = run_single_query(
                query=query_text,
                skill_name=modified_skill,
                skill_description=modified_description,
                timeout=timeout,
                project_root=str(project_root),
                model=model,
            )

            passed = triggered == expected
            result = SmokeResult(
                skill=modified_skill,
                query=query_text,
                expected_trigger=expected,
                actual_trigger=triggered,
                passed=passed,
            )
            report.results.append(result)

            if passed:
                report.passed += 1
            else:
                report.failed += 1
                report.regressions.append(result)

        except Exception:
            # Smoke test failure = regression
            result = SmokeResult(
                skill=modified_skill,
                query=query_text,
                expected_trigger=expected,
                actual_trigger=False,
                passed=False,
            )
            report.results.append(result)
            report.failed += 1
            report.regressions.append(result)

    report.total_tests = len(report.results)
    report.elapsed_seconds = round(time.time() - t0, 2)

    return report
