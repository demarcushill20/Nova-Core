"""Tests for tools.contracts — contract validation."""

from tools.contracts import validate_contract


# --- Sample texts ------------------------------------------------------------

VALID_WITH_FILES = """\
Task completed successfully.

## CONTRACT
summary: Created adapter for repo.git.commit
verification: 28/28 tests pass
confidence: high
files_changed: tools/adapters/git_repo.py, tools/runner.py
"""

VALID_WITH_COMMANDS = """\
Service restarted.

## CONTRACT
summary: Restarted novacore-watcher service
verification: systemctl status shows active (running)
confidence: 0.95
files_changed: none
commands_executed: systemctl restart novacore-watcher
status: active
"""

VALID_WITH_GIT_COMMANDS = """\
Committed changes.

## CONTRACT
summary: Committed 3 files to main
verification: git log confirms abc1234
confidence: medium
files_changed: src/main.py, lib/utils.py, README.md
git_commands_executed: git add, git commit
"""

MISSING_HEADER = """\
This text has no contract block at all.
Just some random output.
"""

MISSING_REQUIRED = """\
## CONTRACT
summary: Did something
confidence: high
files_changed: foo.py
"""

BAD_CONFIDENCE_VALUE = """\
## CONTRACT
summary: Did something
verification: checked
confidence: maybe
files_changed: foo.py
"""

BAD_CONFIDENCE_OUT_OF_RANGE = """\
## CONTRACT
summary: Did something
verification: checked
confidence: 1.5
files_changed: foo.py
"""

MULTIPLE_CONTRACT_BLOCKS = """\
First attempt (wrong):

## CONTRACT
summary: Old attempt
verification: none
confidence: low
files_changed: old.py

Second attempt (correct):

## CONTRACT
summary: Final result
verification: all tests pass
confidence: 0.9
files_changed: new.py, runner.py
"""

WITH_CODE_FENCES = """\
## CONTRACT
```
summary: This is inside a fence and should be ignored
```
summary: Actual summary outside fence
verification: tests pass
confidence: high
files_changed: none
checks_performed: lint, unit tests
"""

MISSING_ACTION_DETAIL = """\
## CONTRACT
summary: Did something
verification: checked it
confidence: high
"""


# --- Tests -------------------------------------------------------------------


def test_valid_with_files_changed():
    result = validate_contract(VALID_WITH_FILES)
    assert result["valid"] is True
    assert result["errors"] == []
    assert result["contract"]["summary"] == "Created adapter for repo.git.commit"
    assert result["contract"]["confidence"] == "high"
    assert "files_changed" in result["contract"]


def test_valid_with_commands_executed():
    result = validate_contract(VALID_WITH_COMMANDS)
    assert result["valid"] is True
    assert result["errors"] == []
    assert result["contract"]["confidence"] == "0.95"
    assert "commands_executed" in result["contract"]
    assert "status" in result["contract"]


def test_valid_with_git_commands():
    result = validate_contract(VALID_WITH_GIT_COMMANDS)
    assert result["valid"] is True
    assert result["contract"]["confidence"] == "medium"
    assert "git_commands_executed" in result["contract"]


def test_missing_header():
    result = validate_contract(MISSING_HEADER)
    assert result["valid"] is False
    assert any("no ## CONTRACT" in e for e in result["errors"])
    assert result["contract"] == {}


def test_missing_required_field():
    result = validate_contract(MISSING_REQUIRED)
    assert result["valid"] is False
    assert any("verification" in e for e in result["errors"])


def test_bad_confidence_word():
    result = validate_contract(BAD_CONFIDENCE_VALUE)
    assert result["valid"] is False
    assert any("confidence" in e for e in result["errors"])


def test_bad_confidence_range():
    result = validate_contract(BAD_CONFIDENCE_OUT_OF_RANGE)
    assert result["valid"] is False
    assert any("confidence" in e for e in result["errors"])


def test_multiple_contracts_uses_last():
    result = validate_contract(MULTIPLE_CONTRACT_BLOCKS)
    assert result["valid"] is True
    assert result["contract"]["summary"] == "Final result"
    assert result["contract"]["files_changed"] == "new.py, runner.py"


def test_code_fences_ignored():
    result = validate_contract(WITH_CODE_FENCES)
    assert result["valid"] is True
    assert result["contract"]["summary"] == "Actual summary outside fence"
    assert "checks_performed" in result["contract"]


def test_missing_action_detail():
    """files_changed is now required — missing it is the primary error."""
    result = validate_contract(MISSING_ACTION_DETAIL)
    assert result["valid"] is False
    assert any("files_changed" in e for e in result["errors"])


def test_empty_string():
    result = validate_contract("")
    assert result["valid"] is False
    assert any("no ## CONTRACT" in e for e in result["errors"])


def test_json_shape():
    result = validate_contract(VALID_WITH_FILES)
    required_keys = {"valid", "errors", "warnings", "contract"}
    assert required_keys == set(result.keys()), f"Unexpected keys: {set(result.keys()) - required_keys}"
    assert isinstance(result["valid"], bool)
    assert isinstance(result["errors"], list)
    assert isinstance(result["warnings"], list)
    assert isinstance(result["contract"], dict)


def test_confidence_boundary_zero():
    text = "## CONTRACT\nsummary: x\nverification: y\nconfidence: 0.0\nfiles_changed: z\n"
    result = validate_contract(text)
    assert result["valid"] is True


def test_confidence_boundary_one():
    text = "## CONTRACT\nsummary: x\nverification: y\nconfidence: 1.0\nfiles_changed: z\n"
    result = validate_contract(text)
    assert result["valid"] is True


# --- Run as script -----------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_valid_with_files_changed,
        test_valid_with_commands_executed,
        test_valid_with_git_commands,
        test_missing_header,
        test_missing_required_field,
        test_bad_confidence_word,
        test_bad_confidence_range,
        test_multiple_contracts_uses_last,
        test_code_fences_ignored,
        test_missing_action_detail,
        test_empty_string,
        test_json_shape,
        test_confidence_boundary_zero,
        test_confidence_boundary_one,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
