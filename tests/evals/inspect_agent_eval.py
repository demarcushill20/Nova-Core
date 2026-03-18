"""Inspect AI evaluation task for NovaCore agent-level evals.

Phase 7.4 — Agent-Level Evals via Inspect AI.

Evaluates end-to-end task completion: does NovaCore correctly complete
tasks from TASKS/ to OUTPUT/?

Usage (requires LLM API key):
    inspect eval tests/evals/inspect_agent_eval.py --model anthropic/claude-sonnet-4-20250514

When inspect-ai is not available, a mock fallback is provided so that
the module can be imported and tested without the dependency.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

_log = logging.getLogger("nova.evals.inspect_agent")

# ---------------------------------------------------------------------------
# Golden corpus path
# ---------------------------------------------------------------------------
_CORPUS_PATH = Path(__file__).resolve().parent / "golden" / "inspect_corpus.json"

# ---------------------------------------------------------------------------
# Try importing inspect-ai; fall back to mock if unavailable
# ---------------------------------------------------------------------------
_HAS_INSPECT = False
try:
    from inspect_ai import Task, task
    from inspect_ai.dataset import json_dataset
    from inspect_ai.scorer import model_graded_fact
    from inspect_ai.solver import generate, system_message

    _HAS_INSPECT = True
except ImportError:
    _log.info("inspect-ai not installed — using mock fallback")


# ---------------------------------------------------------------------------
# Inspect AI task definition (only functional when inspect-ai is installed)
# ---------------------------------------------------------------------------
if _HAS_INSPECT:

    @task
    def novacore_task_completion() -> Task:
        """Evaluate: does NovaCore correctly complete tasks from TASKS/ to OUTPUT/?

        Uses a golden corpus of input/target pairs scored by model-graded
        fact checking.  The system message primes the model with NovaCore's
        identity and operating rules.
        """
        return Task(
            dataset=json_dataset(str(_CORPUS_PATH)),
            solver=[
                system_message(
                    "You are NovaCore, an autonomous agent runtime on a VPS. "
                    "You read tasks from TASKS/, perform the requested work, "
                    "and write structured outputs to OUTPUT/ with timestamps. "
                    "Always follow the operating rules: check TASKS/ first, "
                    "log major actions to LOGS/, and keep solutions modular "
                    "and automation-friendly."
                ),
                generate(),
            ],
            scorer=model_graded_fact(),
        )

    @task
    def novacore_output_quality() -> Task:
        """Evaluate: are NovaCore outputs well-structured and complete?

        Scores whether the agent output contains the expected structural
        properties (metrics, citations, code blocks, etc.) as defined
        in the golden corpus metadata.
        """
        return Task(
            dataset=json_dataset(str(_CORPUS_PATH)),
            solver=[
                system_message(
                    "You are NovaCore, an autonomous agent runtime. "
                    "Produce high-quality, structured outputs with clear "
                    "sections, proper formatting, and actionable content."
                ),
                generate(),
            ],
            scorer=model_graded_fact(),
        )


# ---------------------------------------------------------------------------
# Mock fallback for environments without inspect-ai
# ---------------------------------------------------------------------------


class MockEvalResult:
    """Lightweight stand-in for an Inspect AI eval result."""

    def __init__(
        self,
        task_name: str,
        scores: dict[str, float],
        status: str = "success",
    ) -> None:
        self.task_name = task_name
        self.scores = scores
        self.status = status
        self.samples: list[dict[str, Any]] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task_name,
            "status": self.status,
            "scores": self.scores,
            "num_samples": len(self.samples),
        }


def _load_corpus() -> list[dict[str, Any]]:
    """Load the golden corpus from disk."""
    if not _CORPUS_PATH.exists():
        return []
    with _CORPUS_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def run_eval(task_name: str = "novacore_task_completion") -> MockEvalResult:
    """Run a mock evaluation using the golden corpus.

    This is a deterministic fallback that validates corpus structure
    and returns mock scores.  Useful for CI environments where
    inspect-ai or an LLM API key is not available.

    Returns
    -------
    MockEvalResult
        A lightweight result object with task name, mock scores,
        and sample details.
    """
    corpus = _load_corpus()

    if not corpus:
        return MockEvalResult(
            task_name=task_name,
            scores={"completeness": 0.0, "accuracy": 0.0},
            status="no_corpus",
        )

    # Validate corpus entries have required fields
    valid = 0
    samples: list[dict[str, Any]] = []
    for entry in corpus:
        has_input = bool(entry.get("input"))
        has_target = bool(entry.get("target"))
        has_id = bool(entry.get("id"))
        is_valid = has_input and has_target and has_id

        if is_valid:
            valid += 1

        samples.append(
            {
                "id": entry.get("id", "unknown"),
                "valid": is_valid,
                "category": entry.get("metadata", {}).get("category", "unknown"),
                "input_length": len(entry.get("input", "")),
                "target_length": len(entry.get("target", "")),
            }
        )

    completeness = valid / len(corpus) if corpus else 0.0

    result = MockEvalResult(
        task_name=task_name,
        scores={
            "completeness": completeness,
            "corpus_validity": completeness,
            "num_valid": float(valid),
            "num_total": float(len(corpus)),
        },
        status="success" if completeness == 1.0 else "partial",
    )
    result.samples = samples
    return result


# ---------------------------------------------------------------------------
# Pytest tests — always runnable (no LLM required)
# ---------------------------------------------------------------------------


class TestGoldenCorpus:
    """Validate the golden corpus structure (deterministic, no LLM)."""

    def test_corpus_file_exists(self) -> None:
        assert _CORPUS_PATH.exists(), f"Golden corpus not found at {_CORPUS_PATH}"

    def test_corpus_is_valid_json(self) -> None:
        corpus = _load_corpus()
        assert isinstance(corpus, list), "Corpus must be a JSON array"
        assert len(corpus) > 0, "Corpus must have at least one entry"

    def test_corpus_entries_have_required_fields(self) -> None:
        corpus = _load_corpus()
        for entry in corpus:
            assert "input" in entry, f"Entry missing 'input': {entry.get('id')}"
            assert "target" in entry, f"Entry missing 'target': {entry.get('id')}"
            assert "id" in entry, "Entry missing 'id'"
            assert len(entry["input"]) >= 10, f"Input too short for {entry['id']}: {len(entry['input'])} chars"
            assert len(entry["target"]) >= 20, f"Target too short for {entry['id']}: {len(entry['target'])} chars"

    def test_corpus_ids_are_unique(self) -> None:
        corpus = _load_corpus()
        ids = [e["id"] for e in corpus]
        assert len(ids) == len(set(ids)), f"Duplicate IDs found: {ids}"

    def test_corpus_categories_are_valid(self) -> None:
        from tests.evals.golden.schema import VALID_CATEGORIES

        corpus = _load_corpus()
        for entry in corpus:
            cat = entry.get("metadata", {}).get("category")
            assert cat in VALID_CATEGORIES, (
                f"Invalid category '{cat}' for {entry['id']}. Valid: {sorted(VALID_CATEGORIES)}"
            )

    def test_mock_eval_returns_full_score(self) -> None:
        """The mock eval should report 100% completeness on a valid corpus."""
        result = run_eval()
        assert result.status == "success"
        assert result.scores["completeness"] == 1.0
        assert result.scores["num_valid"] == result.scores["num_total"]


@pytest.mark.eval_llm
class TestInspectAITasks:
    """Tests that require inspect-ai and LLM API access.

    Skipped automatically when inspect-ai is not installed or no API key
    is available.
    """

    def test_inspect_ai_available(self) -> None:
        pytest.importorskip("inspect_ai")
        assert _HAS_INSPECT, "inspect-ai imported but _HAS_INSPECT is False"

    def test_task_completion_task_defined(self) -> None:
        pytest.importorskip("inspect_ai")
        task_obj = novacore_task_completion()
        assert task_obj is not None
        assert hasattr(task_obj, "dataset")
        assert hasattr(task_obj, "solver")
        assert hasattr(task_obj, "scorer")

    def test_output_quality_task_defined(self) -> None:
        pytest.importorskip("inspect_ai")
        task_obj = novacore_output_quality()
        assert task_obj is not None
        assert hasattr(task_obj, "dataset")
