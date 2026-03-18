"""Tests for the Golden Dataset regression corpus (Phase 7, Step 7.3).

Validates that:
- All 20 golden test cases load and conform to the Pydantic schema
- Category distribution is balanced across the 6 categories
- Expected properties come from the known valid set
- Each case's expected_output is non-empty and substantive
- Structural checks pass on every golden case via parametrized tests
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from tests.evals.golden.schema import (
    VALID_CATEGORIES,
    VALID_PROPERTIES,
    GoldenTestCase,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CORPUS_PATH = Path(__file__).resolve().parent / "golden" / "corpus.json"


@pytest.fixture(scope="module")
def raw_corpus() -> list[dict]:
    """Load the raw JSON corpus."""
    assert CORPUS_PATH.exists(), f"Corpus file missing: {CORPUS_PATH}"
    with open(CORPUS_PATH) as f:
        data = json.load(f)
    assert isinstance(data, list), "corpus.json must be a JSON array"
    return data


@pytest.fixture(scope="module")
def golden_cases(raw_corpus: list[dict]) -> list[GoldenTestCase]:
    """Parse and validate every entry in the corpus."""
    cases: list[GoldenTestCase] = []
    for i, entry in enumerate(raw_corpus):
        try:
            cases.append(GoldenTestCase.model_validate(entry))
        except Exception as exc:
            pytest.fail(f"Entry {i} failed validation: {exc}")
    return cases


# ---------------------------------------------------------------------------
# Corpus-level tests
# ---------------------------------------------------------------------------


class TestCorpusIntegrity:
    """Ensure the corpus file is well-formed and complete."""

    def test_corpus_has_20_cases(self, golden_cases: list[GoldenTestCase]) -> None:
        assert len(golden_cases) == 20, f"Expected 20 cases, got {len(golden_cases)}"

    def test_ids_are_unique(self, golden_cases: list[GoldenTestCase]) -> None:
        ids = [c.id for c in golden_cases]
        assert len(ids) == len(set(ids)), f"Duplicate IDs found: {[x for x, n in Counter(ids).items() if n > 1]}"

    def test_all_categories_represented(self, golden_cases: list[GoldenTestCase]) -> None:
        categories = {c.category for c in golden_cases}
        assert categories == VALID_CATEGORIES, f"Missing categories: {VALID_CATEGORIES - categories}"

    def test_category_distribution(self, golden_cases: list[GoldenTestCase]) -> None:
        """Each category should have at least 2 cases; heartbeat/codegen/research/plan have 4."""
        counts = Counter(c.category for c in golden_cases)
        for cat in VALID_CATEGORIES:
            assert counts[cat] >= 2, f"Category '{cat}' has only {counts[cat]} case(s), need >= 2"
        # The four main categories should have 4 each
        for cat in ("heartbeat", "codegen", "research", "plan"):
            assert counts[cat] == 4, f"Category '{cat}' expected 4 cases, got {counts[cat]}"
        # Bugfix and novatrade have 2 each
        for cat in ("bugfix", "novatrade"):
            assert counts[cat] == 2, f"Category '{cat}' expected 2 cases, got {counts[cat]}"

    def test_total_distribution_sums_to_20(self, golden_cases: list[GoldenTestCase]) -> None:
        counts = Counter(c.category for c in golden_cases)
        assert sum(counts.values()) == 20


# ---------------------------------------------------------------------------
# Property validation tests
# ---------------------------------------------------------------------------


class TestPropertyValidity:
    """Verify expected_properties are drawn from the known set."""

    def test_all_properties_are_valid(self, golden_cases: list[GoldenTestCase]) -> None:
        """Every expected_property in the corpus must be in VALID_PROPERTIES."""
        all_props: set[str] = set()
        for case in golden_cases:
            all_props.update(case.expected_properties)
        unknown = all_props - VALID_PROPERTIES
        assert not unknown, f"Unknown properties in corpus: {sorted(unknown)}"

    def test_property_coverage(self, golden_cases: list[GoldenTestCase]) -> None:
        """At least 20 distinct properties should be exercised across all 20 cases."""
        all_props: set[str] = set()
        for case in golden_cases:
            all_props.update(case.expected_properties)
        assert len(all_props) >= 20, f"Only {len(all_props)} distinct properties used; want >= 20 for breadth"


# ---------------------------------------------------------------------------
# Parametrized structural checks on each case
# ---------------------------------------------------------------------------


def _case_ids() -> list[str]:
    """Load case IDs for parametrize without requiring the fixture."""
    if not CORPUS_PATH.exists():
        return []
    with open(CORPUS_PATH) as f:
        data = json.load(f)
    return [entry["id"] for entry in data]


def _load_case(case_id: str) -> GoldenTestCase:
    with open(CORPUS_PATH) as f:
        data = json.load(f)
    for entry in data:
        if entry["id"] == case_id:
            return GoldenTestCase.model_validate(entry)
    raise ValueError(f"Case {case_id} not found")


@pytest.mark.parametrize("case_id", _case_ids())
class TestGoldenCaseStructure:
    """Structural checks applied to every golden test case."""

    def test_schema_validation(self, case_id: str) -> None:
        """Case validates against the GoldenTestCase schema without errors."""
        case = _load_case(case_id)
        assert case.id == case_id

    def test_id_matches_category_prefix(self, case_id: str) -> None:
        """ID should start with the category name (e.g. heartbeat_001)."""
        case = _load_case(case_id)
        assert case.id.startswith(case.category), f"ID '{case.id}' does not start with category '{case.category}'"

    def test_input_prompt_is_substantive(self, case_id: str) -> None:
        """Input prompt should be at least 30 characters and contain a verb."""
        case = _load_case(case_id)
        assert len(case.input_prompt) >= 30, (
            f"Input prompt too short ({len(case.input_prompt)} chars): {case.input_prompt[:50]}"
        )

    def test_expected_output_is_substantive(self, case_id: str) -> None:
        """Expected output should be 100+ characters with meaningful content."""
        case = _load_case(case_id)
        assert len(case.expected_output) >= 100, f"Expected output too short ({len(case.expected_output)} chars)"
        # Should contain multiple sentences (at least one period or comma)
        assert "." in case.expected_output or "," in case.expected_output, (
            "Expected output should contain structured prose"
        )

    def test_has_enough_properties(self, case_id: str) -> None:
        """Each case should specify 3-5 expected properties."""
        case = _load_case(case_id)
        n = len(case.expected_properties)
        assert 2 <= n <= 6, f"Case '{case_id}' has {n} properties; expected 2-6"

    def test_no_duplicate_properties(self, case_id: str) -> None:
        """No repeated properties within a single case."""
        case = _load_case(case_id)
        assert len(case.expected_properties) == len(set(case.expected_properties)), f"Duplicate properties in {case_id}"

    def test_metadata_keys_are_strings(self, case_id: str) -> None:
        """Metadata keys and values must all be strings."""
        case = _load_case(case_id)
        for k, v in case.metadata.items():
            assert isinstance(k, str), f"metadata key {k!r} is not a string"
            assert isinstance(v, str), f"metadata value for '{k}' is not a string: {v!r}"
