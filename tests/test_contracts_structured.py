"""Tests for Pydantic-based contract validation (Phase 5.4 + 5.5).

Tests validate_contract_structured(), contract_from_dict(), round-trip
compatibility between ContractOutput.to_markdown() and the regex parser,
and enhanced contract fields (Phase 5.5).
"""

import pytest
from pydantic import ValidationError

from tools.contracts import (
    contract_from_dict,
    evaluate_contract_success,
    validate_contract,
    validate_contract_structured,
)
from utils.schemas.contract import ContractConfidence, ContractOutput

# --- Sample data -------------------------------------------------------------

VALID_TEXT = """\
Task completed successfully.

## CONTRACT
summary: Created adapter for repo.git.commit
verification: 28/28 tests pass
confidence: high
files_changed: tools/adapters/git_repo.py, tools/runner.py
"""

VALID_TEXT_NUMERIC_CONFIDENCE = """\
## CONTRACT
summary: Restarted novacore-watcher service
verification: systemctl status shows active
confidence: 0.95
files_changed: none
commands_executed: systemctl restart novacore-watcher
"""

VALID_DICT = {
    "summary": "Unit test contract",
    "files_changed": ["foo.py", "bar.py"],
    "verification": "all tests pass",
    "confidence": ContractConfidence.HIGH,
}

VALID_DICT_STRING_CONFIDENCE = {
    "summary": "Unit test contract",
    "files_changed": "foo.py, bar.py",
    "verification": "all tests pass",
    "confidence": "medium",
}

MISSING_SUMMARY_DICT = {
    "files_changed": ["foo.py"],
    "verification": "checked",
    "confidence": ContractConfidence.HIGH,
}

INVALID_CONFIDENCE_DICT = {
    "summary": "Something",
    "files_changed": ["foo.py"],
    "verification": "checked",
    "confidence": "absolutely",  # not a valid enum value
}

MISSING_REQUIRED_TEXT = """\
## CONTRACT
summary: Did something
confidence: high
files_changed: foo.py
"""

INVALID_CONFIDENCE_TEXT = """\
## CONTRACT
summary: Did something
verification: checked
confidence: maybe
files_changed: foo.py
"""


# --- Tests: validate_contract_structured with text input ---------------------


class TestStructuredFromText:
    def test_valid_contract_text(self):
        result = validate_contract_structured(VALID_TEXT)
        assert result["valid"] is True
        assert isinstance(result["contract"], ContractOutput)
        assert result["contract"].summary == "Created adapter for repo.git.commit"
        assert result["contract"].confidence == ContractConfidence.HIGH
        assert "tools/adapters/git_repo.py" in result["contract"].files_changed
        assert "tools/runner.py" in result["contract"].files_changed
        assert result["errors"] == []
        assert result["legacy_compat"] is not None
        assert result["legacy_compat"]["summary"] == result["contract"].summary

    def test_numeric_confidence_mapped(self):
        result = validate_contract_structured(VALID_TEXT_NUMERIC_CONFIDENCE)
        assert result["valid"] is True
        assert result["contract"].confidence == ContractConfidence.HIGH  # 0.95 > 0.7
        # Extra field should be captured
        extras = {ef.key: ef.value for ef in result["contract"].extra_fields}
        assert "commands_executed" in extras

    def test_missing_required_in_text(self):
        """Text missing 'verification' should fail at the regex-parser level."""
        result = validate_contract_structured(MISSING_REQUIRED_TEXT)
        assert result["valid"] is False
        assert result["contract"] is None
        assert any("verification" in e for e in result["errors"])

    def test_invalid_confidence_in_text(self):
        """Invalid confidence word should fail at the regex-parser level."""
        result = validate_contract_structured(INVALID_CONFIDENCE_TEXT)
        assert result["valid"] is False
        assert result["contract"] is None
        assert any("confidence" in e for e in result["errors"])


# --- Tests: validate_contract_structured with dict input ---------------------


class TestStructuredFromDict:
    def test_valid_dict(self):
        result = validate_contract_structured(VALID_DICT)
        assert result["valid"] is True
        assert isinstance(result["contract"], ContractOutput)
        assert result["contract"].summary == "Unit test contract"
        assert result["contract"].files_changed == ["foo.py", "bar.py"]
        assert result["legacy_compat"] is not None

    def test_valid_dict_string_confidence(self):
        result = validate_contract_structured(VALID_DICT_STRING_CONFIDENCE)
        assert result["valid"] is True
        assert result["contract"].confidence == ContractConfidence.MEDIUM

    def test_missing_required_field_dict(self):
        result = validate_contract_structured(MISSING_SUMMARY_DICT)
        assert result["valid"] is False
        assert result["contract"] is None
        assert len(result["errors"]) > 0
        assert any("summary" in e.lower() for e in result["errors"])

    def test_invalid_confidence_dict(self):
        result = validate_contract_structured(INVALID_CONFIDENCE_DICT)
        assert result["valid"] is False
        assert result["contract"] is None
        assert len(result["errors"]) > 0

    def test_invalid_input_type(self):
        result = validate_contract_structured(42)
        assert result["valid"] is False
        assert any("int" in e for e in result["errors"])


# --- Tests: contract_from_dict -----------------------------------------------


class TestContractFromDict:
    def test_valid(self):
        model = contract_from_dict(VALID_DICT)
        assert model is not None
        assert isinstance(model, ContractOutput)
        assert model.summary == "Unit test contract"

    def test_invalid(self):
        model = contract_from_dict({"not_a_field": "value"})
        assert model is None

    def test_missing_required(self):
        model = contract_from_dict(MISSING_SUMMARY_DICT)
        assert model is None

    def test_string_files_changed(self):
        model = contract_from_dict(VALID_DICT_STRING_CONFIDENCE)
        assert model is not None
        assert model.files_changed == ["foo.py", "bar.py"]


# --- Tests: round-trip (Pydantic -> markdown -> regex parser) ----------------


class TestRoundTrip:
    def test_to_markdown_round_trip(self):
        """ContractOutput.to_markdown() should produce text the regex parser
        can parse back into a valid contract."""
        model = ContractOutput(
            summary="Round-trip test",
            files_changed=["a.py", "b.py"],
            verification="pytest passes",
            confidence=ContractConfidence.HIGH,
        )
        md = model.to_markdown()

        # Regex parser should validate successfully
        legacy_result = validate_contract(md)
        assert legacy_result["valid"] is True, f"Regex parser errors: {legacy_result['errors']}"
        assert legacy_result["contract"]["summary"] == "Round-trip test"
        assert legacy_result["contract"]["confidence"] == "high"

    def test_round_trip_with_optional_fields(self):
        model = ContractOutput(
            summary="Full round-trip",
            files_changed=["x.py"],
            verification="all green",
            confidence=ContractConfidence.MEDIUM,
            tests_passed=True,
            test_count=42,
            lint_clean=True,
        )
        md = model.to_markdown()
        legacy_result = validate_contract(md)
        assert legacy_result["valid"] is True
        assert legacy_result["contract"]["test_count"] == "42"

    def test_full_round_trip_text_to_pydantic_to_text(self):
        """Text -> structured validation -> to_markdown -> regex parse."""
        result = validate_contract_structured(VALID_TEXT)
        assert result["valid"] is True

        md = result["contract"].to_markdown()
        legacy_result = validate_contract(md)
        assert legacy_result["valid"] is True
        assert legacy_result["contract"]["summary"] == "Created adapter for repo.git.commit"


# --- Tests: regression — existing regex parser unchanged ---------------------


class TestRegression:
    def test_existing_validate_contract_still_works(self):
        """The original validate_contract() must not be affected."""
        result = validate_contract(VALID_TEXT)
        assert result["valid"] is True
        assert result["errors"] == []
        assert result["contract"]["summary"] == "Created adapter for repo.git.commit"
        assert result["contract"]["confidence"] == "high"
        assert "files_changed" in result["contract"]

    def test_existing_validate_contract_missing_header(self):
        result = validate_contract("No contract here")
        assert result["valid"] is False
        assert any("no ## CONTRACT" in e for e in result["errors"])

    def test_existing_validate_contract_shape(self):
        result = validate_contract(VALID_TEXT)
        assert set(result.keys()) == {"valid", "errors", "warnings", "contract"}
        assert isinstance(result["contract"], dict)


# --- Tests: edge cases -------------------------------------------------------


class TestEdgeCases:
    def test_empty_files_changed_none_string(self):
        """'files_changed: none' should become an empty list."""
        result = validate_contract_structured(VALID_TEXT_NUMERIC_CONFIDENCE)
        assert result["valid"] is True
        assert result["contract"].files_changed == []

    def test_dict_with_extra_fields(self):
        d = dict(VALID_DICT)
        d["extra_fields"] = [{"key": "task_id", "value": "T-42"}]
        result = validate_contract_structured(d)
        assert result["valid"] is True
        assert len(result["contract"].extra_fields) == 1
        assert result["contract"].extra_fields[0].key == "task_id"

    def test_warnings_is_list(self):
        result = validate_contract_structured(VALID_TEXT)
        assert isinstance(result["warnings"], list)

    def test_legacy_compat_is_dict_when_valid(self):
        result = validate_contract_structured(VALID_TEXT)
        assert isinstance(result["legacy_compat"], dict)
        assert "summary" in result["legacy_compat"]
        assert "confidence" in result["legacy_compat"]

    def test_legacy_compat_is_none_when_invalid(self):
        result = validate_contract_structured(MISSING_SUMMARY_DICT)
        assert result["legacy_compat"] is None


# --- Tests: Phase 5.5 — Enhanced contract fields ----------------------------


class TestEnhancedFieldsOptional:
    """Enhanced fields are all optional — existing contracts still work."""

    def test_existing_contract_still_valid(self):
        """A contract without any Phase 5.5 fields is still valid."""
        c = ContractOutput(
            summary="Old-style contract",
            files_changed=["a.py"],
            verification="pytest passes",
            confidence=ContractConfidence.HIGH,
        )
        assert c.max_retries is None
        assert c.timeout_seconds is None
        assert c.success_criteria == []
        assert c.resource_constraints == {}
        assert c.preconditions == []
        assert c.postconditions == []
        assert c.rollback_steps == []

    def test_contract_with_all_enhanced_fields(self):
        c = ContractOutput(
            summary="Enhanced contract",
            max_retries=3,
            timeout_seconds=120,
            success_criteria=["tests_pass", "lint_clean"],
            resource_constraints={"max_tokens": 4000, "max_files": 10},
            preconditions=["file exists", "env loaded"],
            postconditions=["tests green", "no regressions"],
            rollback_steps=["git checkout .", "restore backup"],
        )
        assert c.max_retries == 3
        assert c.timeout_seconds == 120
        assert c.success_criteria == ["tests_pass", "lint_clean"]
        assert c.resource_constraints == {"max_tokens": 4000, "max_files": 10}
        assert c.preconditions == ["file exists", "env loaded"]
        assert c.postconditions == ["tests green", "no regressions"]
        assert c.rollback_steps == ["git checkout .", "restore backup"]


class TestEnhancedFieldsValidation:
    """Boundary validation on max_retries and timeout_seconds."""

    def test_max_retries_zero(self):
        c = ContractOutput(summary="Test", max_retries=0)
        assert c.max_retries == 0

    def test_max_retries_ten(self):
        c = ContractOutput(summary="Test", max_retries=10)
        assert c.max_retries == 10

    def test_max_retries_negative_raises(self):
        with pytest.raises(ValidationError):
            ContractOutput(summary="Test", max_retries=-1)

    def test_max_retries_too_high_raises(self):
        with pytest.raises(ValidationError):
            ContractOutput(summary="Test", max_retries=11)

    def test_timeout_seconds_one(self):
        c = ContractOutput(summary="Test", timeout_seconds=1)
        assert c.timeout_seconds == 1

    def test_timeout_seconds_3600(self):
        c = ContractOutput(summary="Test", timeout_seconds=3600)
        assert c.timeout_seconds == 3600

    def test_timeout_seconds_zero_raises(self):
        with pytest.raises(ValidationError):
            ContractOutput(summary="Test", timeout_seconds=0)

    def test_timeout_seconds_too_high_raises(self):
        with pytest.raises(ValidationError):
            ContractOutput(summary="Test", timeout_seconds=3601)


class TestEnhancedToMarkdown:
    """to_markdown() renders enhanced fields when present, omits when default."""

    def test_enhanced_fields_in_markdown(self):
        c = ContractOutput(
            summary="Enhanced output",
            confidence=ContractConfidence.HIGH,
            max_retries=3,
            timeout_seconds=60,
            success_criteria=["tests_pass", "lint_clean"],
            resource_constraints={"max_tokens": 4000},
            preconditions=["env loaded"],
            postconditions=["tests green"],
            rollback_steps=["git checkout ."],
        )
        md = c.to_markdown()
        assert "max_retries: 3" in md
        assert "timeout_seconds: 60" in md
        assert "success_criteria: tests_pass; lint_clean" in md
        assert '"max_tokens": 4000' in md
        assert "preconditions: env loaded" in md
        assert "postconditions: tests green" in md
        assert "rollback_steps: git checkout ." in md

    def test_enhanced_fields_omitted_when_default(self):
        c = ContractOutput(summary="Minimal")
        md = c.to_markdown()
        assert "max_retries" not in md
        assert "timeout_seconds" not in md
        assert "success_criteria" not in md
        assert "resource_constraints" not in md
        assert "preconditions" not in md
        assert "postconditions" not in md
        assert "rollback_steps" not in md

    def test_enhanced_fields_omitted_when_empty_lists(self):
        c = ContractOutput(
            summary="Empty lists",
            success_criteria=[],
            preconditions=[],
            postconditions=[],
            rollback_steps=[],
            resource_constraints={},
        )
        md = c.to_markdown()
        assert "success_criteria" not in md
        assert "preconditions" not in md
        assert "postconditions" not in md
        assert "rollback_steps" not in md
        assert "resource_constraints" not in md


class TestEvaluateContractSuccess:
    """evaluate_contract_success() checks results against criteria."""

    def test_all_criteria_met(self):
        c = ContractOutput(
            summary="Test",
            success_criteria=["tests_pass", "lint_clean"],
        )
        results = {"tests_passed": True, "lint_clean": True}
        ev = evaluate_contract_success(c, results)
        assert ev["met"] is True
        assert ev["passed"] == ["tests_pass", "lint_clean"]
        assert ev["failed"] == []
        assert ev["skipped"] == []

    def test_some_criteria_failed(self):
        c = ContractOutput(
            summary="Test",
            success_criteria=["tests_pass", "lint_clean"],
        )
        results = {"tests_passed": True, "lint_clean": False}
        ev = evaluate_contract_success(c, results)
        assert ev["met"] is False
        assert "tests_pass" in ev["passed"]
        assert "lint_clean" in ev["failed"]

    def test_no_criteria_vacuously_true(self):
        c = ContractOutput(summary="Test", success_criteria=[])
        results = {"tests_passed": False}
        ev = evaluate_contract_success(c, results)
        assert ev["met"] is True
        assert ev["passed"] == []
        assert ev["failed"] == []
        assert ev["skipped"] == []

    def test_unrecognized_criteria_skipped(self):
        c = ContractOutput(
            summary="Test",
            success_criteria=["tests_pass", "coverage_above_80"],
        )
        results = {"tests_passed": True}
        ev = evaluate_contract_success(c, results)
        assert ev["met"] is True  # only recognized criteria count
        assert "tests_pass" in ev["passed"]
        assert "coverage_above_80" in ev["skipped"]

    def test_no_errors_criterion_met(self):
        c = ContractOutput(summary="Test", success_criteria=["no_errors"])
        results = {"errors": []}
        ev = evaluate_contract_success(c, results)
        assert ev["met"] is True
        assert "no_errors" in ev["passed"]

    def test_no_errors_criterion_failed(self):
        c = ContractOutput(summary="Test", success_criteria=["no_errors"])
        results = {"errors": ["Something broke"]}
        ev = evaluate_contract_success(c, results)
        assert ev["met"] is False
        assert "no_errors" in ev["failed"]

    def test_missing_result_key_skipped(self):
        """If a criterion's result key isn't in results, it goes to skipped."""
        c = ContractOutput(summary="Test", success_criteria=["tests_pass"])
        results = {}  # no tests_passed key
        ev = evaluate_contract_success(c, results)
        assert ev["met"] is True  # nothing failed
        assert "tests_pass" in ev["skipped"]


class TestEnhancedRoundTrip:
    """Round-trip: enhanced contract -> to_markdown() -> validate_contract_structured()."""

    def test_enhanced_contract_round_trip(self):
        c = ContractOutput(
            summary="Round-trip enhanced",
            files_changed=["a.py"],
            verification="pytest passes",
            confidence=ContractConfidence.HIGH,
            max_retries=3,
            timeout_seconds=60,
            success_criteria=["tests_pass"],
            preconditions=["env loaded"],
        )
        md = c.to_markdown()

        # The regex parser should still consider this valid (it only checks
        # the 4 required fields + confidence)
        result = validate_contract_structured(md)
        assert result["valid"] is True
        assert result["contract"].summary == "Round-trip enhanced"
        assert result["contract"].confidence == ContractConfidence.HIGH

    def test_enhanced_fields_via_dict(self):
        """Enhanced fields passed as dict should validate."""
        d = {
            "summary": "Dict enhanced",
            "confidence": "high",
            "max_retries": 5,
            "timeout_seconds": 300,
            "success_criteria": ["tests_pass", "lint_clean"],
            "resource_constraints": {"max_tokens": 8000},
            "preconditions": ["db ready"],
            "postconditions": ["data migrated"],
            "rollback_steps": ["restore snapshot"],
        }
        result = validate_contract_structured(d)
        assert result["valid"] is True
        assert result["contract"].max_retries == 5
        assert result["contract"].timeout_seconds == 300
        assert result["contract"].success_criteria == ["tests_pass", "lint_clean"]
        assert result["contract"].resource_constraints == {"max_tokens": 8000}
        assert result["contract"].preconditions == ["db ready"]
        assert result["contract"].postconditions == ["data migrated"]
        assert result["contract"].rollback_steps == ["restore snapshot"]
