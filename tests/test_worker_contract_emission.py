"""Tests for worker output contract emission.

Verifies that the dispatch prompt template instructs workers to emit
a valid ## CONTRACT block, and that compliant/non-compliant outputs
are correctly classified by the contract validator.

These tests exercise the generation path (prompt template) and validate
that outputs produced according to that template pass contract validation.
"""

from tools.contracts import validate_contract
from tools.adapters.contracts_validate import contracts_validate
from watcher import DISPATCH_PROMPT_TEMPLATE


# --- Tests for dispatch prompt template content ------------------------------


def test_dispatch_prompt_contains_contract_instruction():
    """The dispatch prompt must instruct workers to emit ## CONTRACT."""
    assert "## CONTRACT" in DISPATCH_PROMPT_TEMPLATE


def test_dispatch_prompt_requires_summary_field():
    assert "summary:" in DISPATCH_PROMPT_TEMPLATE


def test_dispatch_prompt_requires_files_changed_field():
    assert "files_changed:" in DISPATCH_PROMPT_TEMPLATE


def test_dispatch_prompt_requires_verification_field():
    assert "verification:" in DISPATCH_PROMPT_TEMPLATE


def test_dispatch_prompt_requires_confidence_field():
    assert "confidence:" in DISPATCH_PROMPT_TEMPLATE


def test_dispatch_prompt_mentions_none_for_no_files():
    """Prompt must tell workers to use 'none' when no files changed."""
    lower = DISPATCH_PROMPT_TEMPLATE.lower()
    assert "files_changed: none" in lower or "files_changed:" in lower and "none" in lower


def test_dispatch_prompt_mentions_not_run_for_verification():
    """Prompt must tell workers to use 'not run' when verification wasn't done."""
    lower = DISPATCH_PROMPT_TEMPLATE.lower()
    assert "not run" in lower


def test_dispatch_prompt_mentions_rejection():
    """Prompt must warn workers that output WILL BE REJECTED without contract."""
    assert "REJECTED" in DISPATCH_PROMPT_TEMPLATE or "rejected" in DISPATCH_PROMPT_TEMPLATE


# --- Tests for compliant worker outputs passing validation -------------------


COMPLIANT_OUTPUT_FILES_CHANGED = """\
# Output for: 0030_example

Task completed. Modified watcher.py to add contract emission.

## FILES MODIFIED
- watcher.py
- tests/test_worker_contract_emission.py

## CONTRACT
summary: Added contract emission requirement to dispatch prompt template
files_changed: watcher.py, tests/test_worker_contract_emission.py
verification: ran tests: 15/15 pass
confidence: high
"""

COMPLIANT_OUTPUT_NO_FILES = """\
# Output for: 0031_research

Researched contract emission patterns. No code changes made.

## CONTRACT
summary: Researched contract emission patterns across skill definitions
files_changed: none
verification: not run
confidence: medium
"""

COMPLIANT_OUTPUT_PARTIAL_VERIFICATION = """\
# Output for: 0032_partial

Made changes but could only partially verify.

## CONTRACT
summary: Updated config for deployment
files_changed: config.yml
verification: file exists and is valid YAML; deployment not tested
confidence: low
"""

COMPLIANT_OUTPUT_WITH_TASK_ID = """\
# Output for: 0033_deploy

Deployed service.

## CONTRACT
summary: Deployed novacore-watcher
task_id: 0033_deploy
status: done
files_changed: none
verification: systemctl status shows active
confidence: high
"""


def test_compliant_output_with_files_changed():
    result = validate_contract(COMPLIANT_OUTPUT_FILES_CHANGED)
    assert result["valid"] is True, f"Errors: {result['errors']}"
    assert result["contract"]["files_changed"] == "watcher.py, tests/test_worker_contract_emission.py"
    assert result["contract"]["confidence"] == "high"


def test_compliant_output_no_files_changed():
    """files_changed: none must still pass validation."""
    result = validate_contract(COMPLIANT_OUTPUT_NO_FILES)
    assert result["valid"] is True, f"Errors: {result['errors']}"
    assert result["contract"]["files_changed"] == "none"


def test_compliant_output_partial_verification():
    """Partial verification with honest value must pass."""
    result = validate_contract(COMPLIANT_OUTPUT_PARTIAL_VERIFICATION)
    assert result["valid"] is True, f"Errors: {result['errors']}"
    assert result["contract"]["confidence"] == "low"


def test_compliant_output_with_task_id():
    """Output with task_id as action-detail field must pass."""
    result = validate_contract(COMPLIANT_OUTPUT_WITH_TASK_ID)
    assert result["valid"] is True, f"Errors: {result['errors']}"


# --- Tests for non-compliant outputs failing validation ----------------------


MISSING_CONTRACT_BLOCK = """\
# Output for: 0034_broken

Did work but forgot the contract block entirely.
All done!
"""

MISSING_FILES_CHANGED = """\
# Output for: 0035_missing_files

## CONTRACT
summary: Did something
verification: checked
confidence: high
"""

MISSING_CONFIDENCE = """\
# Output for: 0036_missing_conf

## CONTRACT
summary: Did something
files_changed: foo.py
verification: checked
"""

MISSING_VERIFICATION = """\
# Output for: 0037_missing_verif

## CONTRACT
summary: Did something
files_changed: foo.py
confidence: high
"""

MISSING_SUMMARY = """\
# Output for: 0038_missing_summary

## CONTRACT
files_changed: foo.py
verification: checked
confidence: high
"""


def test_missing_contract_block_fails():
    result = validate_contract(MISSING_CONTRACT_BLOCK)
    assert result["valid"] is False
    assert any("no ## CONTRACT" in e for e in result["errors"])


def test_missing_files_changed_fails():
    """Missing files_changed must fail — it is a required field."""
    result = validate_contract(MISSING_FILES_CHANGED)
    assert result["valid"] is False
    assert any("files_changed" in e for e in result["errors"])


def test_missing_confidence_fails():
    result = validate_contract(MISSING_CONFIDENCE)
    assert result["valid"] is False
    assert any("confidence" in e for e in result["errors"])


def test_missing_verification_fails():
    result = validate_contract(MISSING_VERIFICATION)
    assert result["valid"] is False
    assert any("verification" in e for e in result["errors"])


def test_missing_summary_fails():
    result = validate_contract(MISSING_SUMMARY)
    assert result["valid"] is False
    assert any("summary" in e for e in result["errors"])


# --- Regression: contract block at end of large output -----------------------


LARGE_OUTPUT_WITH_CONTRACT_AT_END = """\
# Output for: 0040_large

## Summary
Did a lot of work. Here is a very detailed report.

## Step 1
Analyzed the codebase extensively.

## Step 2
Made comprehensive changes across multiple files.

## Step 3
Ran all tests and verified the changes.

## Details
Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod
tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam,
quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo
consequat.

## CONTRACT
summary: Comprehensive update to task processing pipeline
files_changed: watcher.py, tools/contracts.py, tests/test_contracts.py
verification: 28/28 tests pass, manual review of output format
confidence: high
"""


def test_contract_at_end_of_large_output():
    result = validate_contract(LARGE_OUTPUT_WITH_CONTRACT_AT_END)
    assert result["valid"] is True, f"Errors: {result['errors']}"


# --- Cross-validator compatibility tests -------------------------------------
# Outputs valid under tools/contracts.py must also be valid under
# tools/adapters/contracts_validate.py (planner validator).
# This guarantees that worker outputs accepted by the watcher will not
# be rejected by the planner/orchestrator.


_COMPLIANT_OUTPUTS = [
    COMPLIANT_OUTPUT_FILES_CHANGED,
    COMPLIANT_OUTPUT_NO_FILES,
    COMPLIANT_OUTPUT_PARTIAL_VERIFICATION,
    COMPLIANT_OUTPUT_WITH_TASK_ID,
    LARGE_OUTPUT_WITH_CONTRACT_AT_END,
]


def test_cross_validator_compliant_outputs():
    """Every output valid under tools/contracts.py must also be valid
    under tools/adapters/contracts_validate.py (planner validator)."""
    for output in _COMPLIANT_OUTPUTS:
        watcher_result = validate_contract(output)
        planner_result = contracts_validate(output)
        assert watcher_result["valid"] is True, (
            f"Watcher rejected output it should accept: {watcher_result['errors']}"
        )
        assert planner_result["valid"] is True, (
            f"Planner rejected output accepted by watcher: {planner_result['errors']}"
        )


def test_cross_validator_required_fields_match():
    """Both validators must require the same four fields."""
    from tools.contracts import _REQUIRED_FIELDS as watcher_required
    from tools.adapters.contracts_validate import REQUIRED_FIELDS as planner_required
    assert set(watcher_required) == set(planner_required), (
        f"Required fields diverge: watcher={watcher_required}, planner={planner_required}"
    )


def test_cross_validator_missing_files_changed_fails_both():
    """An output missing files_changed must fail both validators."""
    watcher_result = validate_contract(MISSING_FILES_CHANGED)
    planner_result = contracts_validate(MISSING_FILES_CHANGED)
    assert watcher_result["valid"] is False
    assert planner_result["valid"] is False


def test_cross_validator_no_contract_fails_both():
    """An output with no ## CONTRACT must fail both validators."""
    watcher_result = validate_contract(MISSING_CONTRACT_BLOCK)
    planner_result = contracts_validate(MISSING_CONTRACT_BLOCK)
    assert watcher_result["valid"] is False
    assert planner_result["valid"] is False


# --- Evaluator/supervisor compatibility tests --------------------------------


def test_evaluator_accepts_valid_contract():
    """Evaluator correctly scores a step with contract_valid=True."""
    from planner.evaluator import Evaluator
    from planner.schemas import PlanStep, StepResult

    step = PlanStep(step_id="s1", skill_name="file-ops", goal="test")
    result = StepResult(step_id="s1", status="success", contract_valid=True)
    ev = Evaluator().evaluate_step(step, result, duration_ms=500)
    assert ev.contract_valid is True
    assert ev.total_score >= 0.85  # exec(0.40) + contract(0.25) + verif(0.20) + dur(0.15)
    assert ev.grade == "A"


def test_evaluator_penalises_invalid_contract():
    """Evaluator correctly penalises a step with contract_valid=False."""
    from planner.evaluator import Evaluator
    from planner.schemas import PlanStep, StepResult

    step = PlanStep(step_id="s1", skill_name="file-ops", goal="test")
    result = StepResult(step_id="s1", status="success", contract_valid=False)
    ev = Evaluator().evaluate_step(step, result, duration_ms=500)
    assert ev.contract_valid is False
    assert ev.total_score < 0.75  # no contract bonus, no verification score


def test_supervisor_continues_on_valid_contract():
    """Supervisor returns action=continue for valid contracts."""
    from planner.supervisor import Supervisor
    from planner.schemas import PlanStep, StepResult

    step = PlanStep(step_id="s1", skill_name="file-ops", goal="test")
    result = StepResult(step_id="s1", status="success", contract_valid=True)
    decision = Supervisor().evaluate_step(step, result)
    assert decision.action == "continue"


def test_supervisor_retries_on_invalid_contract():
    """Supervisor returns action=retry for invalid contracts under retry limit."""
    from planner.supervisor import Supervisor
    from planner.schemas import PlanStep, StepResult

    step = PlanStep(step_id="s1", skill_name="file-ops", goal="test")
    result = StepResult(
        step_id="s1", status="failed", contract_valid=False,
        validation_errors=["missing required field: files_changed"],
        retry_count=0,
    )
    decision = Supervisor().evaluate_step(step, result)
    assert decision.action == "retry"


# --- Run as script -----------------------------------------------------------

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
