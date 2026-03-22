"""Global smoke testing for the skill optimization pipeline.

Runs a lightweight smoke test across all eligible skills to catch
ecosystem-level regressions. Uses multi-shot majority-vote evaluation
for deterministic results. Zero tolerance for smoke failures.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent.parent
THRESHOLDS_PATH = BASE_DIR / "evals" / "thresholds.yaml"

# Import skill-creator's run_eval without permanently polluting sys.path.
# The skill-creator has its own `scripts` package that would shadow the
# project-root `scripts` package if left on sys.path.
_skill_creator_path = str(BASE_DIR / ".claude" / "skills" / "skill-creator")
_saved_scripts = sys.modules.pop("scripts", None)
sys.path.insert(0, _skill_creator_path)
try:
    from scripts.run_eval import find_project_root, run_single_query
finally:
    try:
        sys.path.remove(_skill_creator_path)
    except ValueError:
        pass
    # Restore the original scripts module so project-root scripts/ stays importable
    if _saved_scripts is not None:
        sys.modules["scripts"] = _saved_scripts
    else:
        sys.modules.pop("scripts", None)


def _load_eval_config() -> dict:
    """Load eval config from thresholds.yaml."""
    if not THRESHOLDS_PATH.exists():
        return {}
    try:
        data = yaml.safe_load(THRESHOLDS_PATH.read_text())
        return data.get("eval", {}) if data else {}
    except Exception:
        return {}


@dataclass
class SmokeResult:
    """Result of a single smoke test query (multi-shot)."""

    skill: str
    query: str
    expected_trigger: bool
    actual_trigger: bool
    passed: bool
    run_outcomes: list[bool] = field(default_factory=list)
    trigger_count: int = 0
    trigger_rate: float = 0.0


@dataclass
class SmokeReport:
    """Aggregate smoke test report."""

    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    regressions: list[SmokeResult] = field(default_factory=list)
    results: list[SmokeResult] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    runs_per_query: int = 1
    trigger_threshold: float = 0.5

    @property
    def has_regressions(self) -> bool:
        return len(self.regressions) > 0

    def to_dict(self) -> dict:
        return {
            "total_tests": self.total_tests,
            "passed": self.passed,
            "failed": self.failed,
            "runs_per_query": self.runs_per_query,
            "trigger_threshold": self.trigger_threshold,
            "regressions": [
                {
                    "skill": r.skill,
                    "query": r.query[:80],
                    "expected": r.expected_trigger,
                    "trigger_rate": r.trigger_rate,
                    "run_outcomes": r.run_outcomes,
                }
                for r in self.regressions
            ],
            "results": [
                {
                    "skill": r.skill,
                    "query": r.query[:80],
                    "expected": r.expected_trigger,
                    "actual": r.actual_trigger,
                    "passed": r.passed,
                    "run_outcomes": r.run_outcomes,
                    "trigger_rate": r.trigger_rate,
                    "trigger_count": r.trigger_count,
                    "runs": len(r.run_outcomes),
                }
                for r in self.results
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
    runs_per_query: int | None = None,
    trigger_threshold: float | None = None,
) -> SmokeReport:
    """Run smoke tests focused on the modified skill.

    Uses multi-shot majority-vote: each query is run `runs_per_query` times
    and the trigger decision is based on whether trigger_rate >= trigger_threshold.

    Args:
        modified_skill: Name of the skill being optimized
        modified_description: The candidate description being tested
        smoke_queries: Override smoke test queries (default: load from global)
        timeout: Timeout per query
        model: Model for evaluation
        runs_per_query: Number of runs per query (default: from config or 1)
        trigger_threshold: Threshold for majority vote (default: from config or 0.5)
    """
    if smoke_queries is None:
        smoke_queries = load_smoke_tests()

    if not smoke_queries:
        return SmokeReport()

    # Load config defaults
    cfg = _load_eval_config()
    effective_runs = runs_per_query if runs_per_query is not None else cfg.get("runs_per_query", 1)
    effective_threshold = trigger_threshold if trigger_threshold is not None else cfg.get("trigger_threshold", 0.5)

    project_root = find_project_root()
    report = SmokeReport(runs_per_query=effective_runs, trigger_threshold=effective_threshold)
    t0 = time.time()

    # Filter to queries relevant to the modified skill
    relevant = [q for q in smoke_queries if q.get("skill") == modified_skill]

    for query_record in relevant:
        query_text = query_record["query"]
        expected = query_record.get("should_trigger", True)

        run_outcomes: list[bool] = []
        for _ in range(effective_runs):
            try:
                triggered = run_single_query(
                    query=query_text,
                    skill_name=modified_skill,
                    skill_description=modified_description,
                    timeout=timeout,
                    project_root=str(project_root),
                    model=model,
                )
                run_outcomes.append(bool(triggered))
            except Exception:
                run_outcomes.append(False)

        trigger_count = sum(run_outcomes)
        trigger_rate = trigger_count / effective_runs if effective_runs > 0 else 0.0
        voted_trigger = trigger_rate >= effective_threshold

        passed = voted_trigger == expected
        result = SmokeResult(
            skill=modified_skill,
            query=query_text,
            expected_trigger=expected,
            actual_trigger=voted_trigger,
            passed=passed,
            run_outcomes=run_outcomes,
            trigger_count=trigger_count,
            trigger_rate=round(trigger_rate, 4),
        )
        report.results.append(result)

        if passed:
            report.passed += 1
        else:
            report.failed += 1
            report.regressions.append(result)

    report.total_tests = len(report.results)
    report.elapsed_seconds = round(time.time() - t0, 2)

    return report
