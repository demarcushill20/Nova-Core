"""Tests for the contract enforcement gate in watcher.py.

Validates that verify_artifacts() enforces ## CONTRACT presence
before allowing a task to transition to .done.
"""

import time
from pathlib import Path
from unittest.mock import patch

from watcher import _check_contract, verify_artifacts

# --- Sample outputs ----------------------------------------------------------

VALID_OUTPUT_FILES_CHANGED = """\
# Output for: 0010_example

Task completed. Built new feature.

## CONTRACT
summary: Added feature X to module Y
verification: 10/10 tests pass
confidence: high
files_changed: module_y.py, tests/test_y.py
"""

VALID_OUTPUT_COMMANDS = """\
# Output for: 0011_deploy

Deployed service.

## CONTRACT
summary: Deployed novacore-watcher to production
verification: systemctl status shows active
confidence: 0.95
files_changed: none
commands_executed: systemctl restart novacore-watcher
status: active
"""

MISSING_CONTRACT = """\
# Output for: 0012_broken

Did some work but forgot the contract block.
All done!
"""

BAD_CONFIDENCE = """\
# Output for: 0013_bad_conf

## CONTRACT
summary: Did something
verification: checked
confidence: maybe
files_changed: foo.py
"""

MISSING_VERIFICATION = """\
# Output for: 0014_missing_field

## CONTRACT
summary: Partial contract
confidence: high
files_changed: foo.py
"""

MULTIPLE_CONTRACTS = """\
# Output for: 0015_multi

First attempt:

## CONTRACT
summary: Old wrong attempt
verification: none
confidence: low
files_changed: old.py

Corrected:

## CONTRACT
summary: Final correct result
verification: all 5 tests pass
confidence: 0.9
files_changed: new.py, runner.py
"""


# --- Helpers -----------------------------------------------------------------


def _write_output(tmp_path: Path, stem: str, content: str) -> Path:
    """Write an output file in the expected location and return its path."""
    out_dir = tmp_path / "OUTPUT"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{stem}__20260303-120000.md"
    out_file.write_text(content, encoding="utf-8")
    return out_file


# --- Tests for _check_contract -----------------------------------------------


def test_check_contract_valid(tmp_path):
    out_file = _write_output(tmp_path, "0010_example", VALID_OUTPUT_FILES_CHANGED)
    ok, msgs = _check_contract(out_file)
    assert ok is True
    assert any("CONTRACT validated" in m for m in msgs)
    # File should NOT have failure section appended
    text = out_file.read_text(encoding="utf-8")
    assert "CONTRACT VALIDATION FAILED" not in text


def test_check_contract_valid_commands(tmp_path):
    out_file = _write_output(tmp_path, "0011_deploy", VALID_OUTPUT_COMMANDS)
    ok, msgs = _check_contract(out_file)
    assert ok is True


def test_check_contract_missing(tmp_path):
    out_file = _write_output(tmp_path, "0012_broken", MISSING_CONTRACT)
    ok, msgs = _check_contract(out_file)
    assert ok is False
    assert any("CONTRACT FAILED" in m for m in msgs)
    # Failure section should be appended to file
    text = out_file.read_text(encoding="utf-8")
    assert "## CONTRACT VALIDATION FAILED" in text
    assert "no ## CONTRACT section found" in text
    assert "Suggestion:" in text


def test_check_contract_bad_confidence(tmp_path):
    out_file = _write_output(tmp_path, "0013_bad_conf", BAD_CONFIDENCE)
    ok, msgs = _check_contract(out_file)
    assert ok is False
    assert any("confidence" in m for m in msgs)
    text = out_file.read_text(encoding="utf-8")
    assert "## CONTRACT VALIDATION FAILED" in text


def test_check_contract_missing_field(tmp_path):
    out_file = _write_output(tmp_path, "0014_missing_field", MISSING_VERIFICATION)
    ok, msgs = _check_contract(out_file)
    assert ok is False
    assert any("verification" in m for m in msgs)


def test_check_contract_multiple_uses_last(tmp_path):
    out_file = _write_output(tmp_path, "0015_multi", MULTIPLE_CONTRACTS)
    ok, msgs = _check_contract(out_file)
    assert ok is True
    assert any("CONTRACT validated" in m for m in msgs)


# --- Tests for verify_artifacts with contract gate ---------------------------


def _make_recent_output(tmp_path, stem, content):
    """Create a recent output file and patch OUTPUT_DIR to find it."""
    out_dir = tmp_path / "OUTPUT"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{stem}__20260303-120000.md"
    out_file.write_text(content, encoding="utf-8")
    # Touch to ensure it's recent
    out_file.touch()
    return out_dir, out_file


def test_verify_artifacts_valid_contract(tmp_path):
    stem = "0010_example"
    out_dir, out_file = _make_recent_output(tmp_path, stem, VALID_OUTPUT_FILES_CHANGED)
    with patch("watcher._find_recent_output", return_value=out_file), \
         patch("watcher.METRICS_FILE", tmp_path / "metrics.json"):
        passed, msgs = verify_artifacts(stem)
    assert passed is True
    assert any("OUTPUT verified" in m for m in msgs)
    assert any("CONTRACT validated" in m for m in msgs)


def test_verify_artifacts_missing_contract_fails(tmp_path):
    stem = "0012_broken"
    out_dir, out_file = _make_recent_output(tmp_path, stem, MISSING_CONTRACT)
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir(exist_ok=True)
    with patch("watcher._find_recent_output", return_value=out_file), \
         patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", out_dir), \
         patch("watcher.METRICS_FILE", tmp_path / "metrics.json"):
        passed, msgs = verify_artifacts(stem)
    assert passed is False
    assert any("CONTRACT FAILED" in m for m in msgs)
    # Output file should have failure section
    text = out_file.read_text(encoding="utf-8")
    assert "## CONTRACT VALIDATION FAILED" in text


def test_verify_artifacts_bad_confidence_fails(tmp_path):
    stem = "0013_bad_conf"
    out_dir, out_file = _make_recent_output(tmp_path, stem, BAD_CONFIDENCE)
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir(exist_ok=True)
    with patch("watcher._find_recent_output", return_value=out_file), \
         patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", out_dir), \
         patch("watcher.METRICS_FILE", tmp_path / "metrics.json"):
        passed, msgs = verify_artifacts(stem)
    assert passed is False


def test_verify_artifacts_multiple_contracts_ok(tmp_path):
    stem = "0015_multi"
    out_dir, out_file = _make_recent_output(tmp_path, stem, MULTIPLE_CONTRACTS)
    with patch("watcher._find_recent_output", return_value=out_file), \
         patch("watcher.METRICS_FILE", tmp_path / "metrics.json"):
        passed, msgs = verify_artifacts(stem)
    assert passed is True


def test_verify_artifacts_no_output_skips_contract(tmp_path):
    """If no output file exists, contract check is skipped (but still fails on missing output)."""
    stem = "0099_missing"
    with patch("watcher._find_recent_output", return_value=None), \
         patch("watcher.METRICS_FILE", tmp_path / "metrics.json"):
        passed, msgs = verify_artifacts(stem)
    assert passed is False
    # Should fail on missing output, NOT on contract
    assert any("OUTPUT missing" in m for m in msgs)
    assert not any("CONTRACT" in m for m in msgs)


# --- Test failure report content ---------------------------------------------


def test_failure_report_includes_errors(tmp_path):
    out_file = _write_output(tmp_path, "0020_report", MISSING_CONTRACT)
    ok, msgs = _check_contract(out_file)
    assert ok is False
    text = out_file.read_text(encoding="utf-8")
    assert "## CONTRACT VALIDATION FAILED" in text
    assert "- no ## CONTRACT section found" in text
    assert "Fix output to include ## CONTRACT with required fields" in text


def test_failure_report_bad_confidence_detail(tmp_path):
    out_file = _write_output(tmp_path, "0021_report", BAD_CONFIDENCE)
    ok, msgs = _check_contract(out_file)
    text = out_file.read_text(encoding="utf-8")
    assert "invalid confidence" in text
    assert "maybe" in text


# --- Run as script -----------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    tests = [
        test_check_contract_valid,
        test_check_contract_valid_commands,
        test_check_contract_missing,
        test_check_contract_bad_confidence,
        test_check_contract_missing_field,
        test_check_contract_multiple_uses_last,
        test_failure_report_includes_errors,
        test_failure_report_bad_confidence_detail,
    ]

    # Tests that need mock patches — run separately
    mock_tests = [
        test_verify_artifacts_valid_contract,
        test_verify_artifacts_missing_contract_fails,
        test_verify_artifacts_bad_confidence_fails,
        test_verify_artifacts_multiple_contracts_ok,
        test_verify_artifacts_no_output_skips_contract,
    ]

    passed = 0
    total = len(tests) + len(mock_tests)

    for t in tests:
        try:
            with tempfile.TemporaryDirectory() as td:
                t(Path(td))
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")

    for t in mock_tests:
        try:
            with tempfile.TemporaryDirectory() as td:
                t(Path(td))
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")

    print(f"\n{passed}/{total} tests passed")
