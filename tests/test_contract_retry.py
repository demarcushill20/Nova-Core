"""Tests for contract validation retry logic in watcher.py.

Validates:
- Contract failure triggers retry task creation exactly once
- Second failure (retry task) does NOT create another retry
- Retry task content includes required references and validation errors
- Successful contract => no retry task created
- Existing retry task (any lifecycle state) prevents duplicate
"""

from pathlib import Path
from unittest.mock import patch

from watcher import (
    _is_retry_task,
    _original_stem,
    _create_retry_task,
    _maybe_create_retry,
    verify_artifacts,
    TASKS_DIR,
    OUTPUT_DIR,
)


# --- Sample outputs ----------------------------------------------------------

VALID_OUTPUT = """\
# Output for: 0030_example

Task completed successfully.

## CONTRACT
summary: Built feature X
verification: all tests pass
confidence: high
files_changed: module.py
"""

MISSING_CONTRACT = """\
# Output for: 0031_broken

Did some work but forgot the contract block.
"""

BAD_CONFIDENCE = """\
# Output for: 0032_badconf

## CONTRACT
summary: Did something
verification: checked
confidence: maybe
files_changed: foo.py
"""


# --- Tests for _is_retry_task ------------------------------------------------


def test_is_retry_task_false():
    assert _is_retry_task("0031_broken") is False


def test_is_retry_task_true():
    assert _is_retry_task("0031_broken__retry1") is True


def test_is_retry_task_embedded():
    """__retry1 anywhere in stem counts."""
    assert _is_retry_task("foo__retry1_extra") is True


# --- Tests for _original_stem ------------------------------------------------


def test_original_stem_from_retry():
    assert _original_stem("0031_broken__retry1") == "0031_broken"


def test_original_stem_no_retry():
    assert _original_stem("0031_broken") == "0031_broken"


# --- Tests for _create_retry_task --------------------------------------------


def test_create_retry_task_content(tmp_path):
    """Retry task file includes original stem, output path, and errors."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    output_file = tmp_path / "OUTPUT" / "0031_broken__20260303-120000.md"
    output_file.parent.mkdir()
    output_file.write_text(MISSING_CONTRACT)

    errors = ["no ## CONTRACT section found"]
    warnings = []

    with patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", tmp_path / "OUTPUT"):
        retry_path = _create_retry_task("0031_broken", output_file, errors, warnings)

    assert retry_path.exists()
    assert retry_path.name == "0031_broken__retry1.md"

    content = retry_path.read_text(encoding="utf-8")

    # Must include original stem
    assert "0031_broken" in content
    # Must include output file path
    assert str(output_file) in content
    # Must include validation error text
    assert "no ## CONTRACT section found" in content
    # Must include required field names
    assert "summary" in content
    assert "verification" in content
    assert "confidence" in content
    # Must include at least one action-detail field name
    assert "files_changed" in content


def test_create_retry_task_with_warnings(tmp_path):
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    output_file = tmp_path / "OUTPUT" / "0032_x__20260303-120000.md"
    output_file.parent.mkdir()
    output_file.write_text("content")

    errors = ["missing required field: verification"]
    warnings = ["confidence value is borderline"]

    with patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", tmp_path / "OUTPUT"):
        retry_path = _create_retry_task("0032_x", output_file, errors, warnings)

    content = retry_path.read_text(encoding="utf-8")
    assert "missing required field: verification" in content
    assert "confidence value is borderline" in content


# --- Tests for _maybe_create_retry -------------------------------------------


def test_maybe_create_retry_creates_task(tmp_path):
    """First contract failure creates a retry task."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    output_file = tmp_path / "OUTPUT" / "0031_broken__20260303-120000.md"
    output_file.parent.mkdir()
    output_file.write_text(MISSING_CONTRACT)

    contract_msgs = [
        "CONTRACT FAILED: output missing valid ## CONTRACT",
        "  contract error: no ## CONTRACT section found",
    ]

    with patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", tmp_path / "OUTPUT"), \
         patch("watcher.METRICS_FILE", tmp_path / "metrics.json"):
        _maybe_create_retry("0031_broken", output_file, contract_msgs)

    retry_file = tasks_dir / "0031_broken__retry1.md"
    assert retry_file.exists()


def test_maybe_create_retry_skips_if_already_retry(tmp_path):
    """Retry task stem (__retry1) never creates another retry."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    output_file = tmp_path / "OUTPUT" / "0031_broken__retry1__20260303-120000.md"
    output_file.parent.mkdir()
    output_file.write_text(MISSING_CONTRACT)

    contract_msgs = [
        "CONTRACT FAILED: output missing valid ## CONTRACT",
        "  contract error: no ## CONTRACT section found",
    ]

    with patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", tmp_path / "OUTPUT"):
        _maybe_create_retry("0031_broken__retry1", output_file, contract_msgs)

    # No __retry1__retry1 should exist
    assert not any("retry1__retry1" in f.name for f in tasks_dir.iterdir())
    # No new files at all
    assert list(tasks_dir.iterdir()) == []


def test_maybe_create_retry_skips_if_retry_already_exists(tmp_path):
    """If retry task already exists (any state), don't create duplicate."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    output_file = tmp_path / "OUTPUT" / "0031_broken__20260303-120000.md"
    output_file.parent.mkdir()
    output_file.write_text(MISSING_CONTRACT)

    # Pre-create retry task in .failed state
    (tasks_dir / "0031_broken__retry1.md.failed").write_text("old retry")

    contract_msgs = [
        "CONTRACT FAILED: output missing valid ## CONTRACT",
        "  contract error: no ## CONTRACT section found",
    ]

    with patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", tmp_path / "OUTPUT"):
        _maybe_create_retry("0031_broken", output_file, contract_msgs)

    # Should NOT create a new .md retry
    assert not (tasks_dir / "0031_broken__retry1.md").exists()


def test_maybe_create_retry_skips_if_retry_inprogress(tmp_path):
    """Retry in progress also blocks new retry creation."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    output_file = tmp_path / "OUTPUT" / "0031_broken__20260303-120000.md"
    output_file.parent.mkdir()
    output_file.write_text(MISSING_CONTRACT)

    (tasks_dir / "0031_broken__retry1.md.inprogress").write_text("running")

    contract_msgs = [
        "CONTRACT FAILED: output missing valid ## CONTRACT",
        "  contract error: no ## CONTRACT section found",
    ]

    with patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", tmp_path / "OUTPUT"):
        _maybe_create_retry("0031_broken", output_file, contract_msgs)

    assert not (tasks_dir / "0031_broken__retry1.md").exists()


def test_maybe_create_retry_skips_if_retry_done(tmp_path):
    """Retry already completed also blocks new retry creation."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    output_file = tmp_path / "OUTPUT" / "0031_broken__20260303-120000.md"
    output_file.parent.mkdir()
    output_file.write_text(MISSING_CONTRACT)

    (tasks_dir / "0031_broken__retry1.md.done").write_text("completed")

    contract_msgs = [
        "CONTRACT FAILED: output missing valid ## CONTRACT",
        "  contract error: no ## CONTRACT section found",
    ]

    with patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", tmp_path / "OUTPUT"):
        _maybe_create_retry("0031_broken", output_file, contract_msgs)

    assert not (tasks_dir / "0031_broken__retry1.md").exists()


# --- Tests for verify_artifacts integration ----------------------------------


def test_verify_valid_contract_no_retry(tmp_path):
    """Successful contract validation => no retry task created."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    out_dir = tmp_path / "OUTPUT"
    out_dir.mkdir()
    out_file = out_dir / "0030_example__20260303-120000.md"
    out_file.write_text(VALID_OUTPUT)
    out_file.touch()

    with patch("watcher._find_recent_output", return_value=out_file), \
         patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.METRICS_FILE", tmp_path / "metrics.json"):
        passed, msgs = verify_artifacts("0030_example")

    assert passed is True
    # No retry task should be created
    assert not (tasks_dir / "0030_example__retry1.md").exists()


def test_verify_missing_contract_creates_retry(tmp_path):
    """Contract failure on original task creates retry."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    out_dir = tmp_path / "OUTPUT"
    out_dir.mkdir()
    out_file = out_dir / "0031_broken__20260303-120000.md"
    out_file.write_text(MISSING_CONTRACT)
    out_file.touch()

    with patch("watcher._find_recent_output", return_value=out_file), \
         patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", out_dir), \
         patch("watcher.METRICS_FILE", tmp_path / "metrics.json"):
        passed, msgs = verify_artifacts("0031_broken")

    assert passed is False
    retry_file = tasks_dir / "0031_broken__retry1.md"
    assert retry_file.exists()

    content = retry_file.read_text(encoding="utf-8")
    assert "0031_broken" in content
    assert "no ## CONTRACT section found" in content


def test_verify_retry_task_failure_no_second_retry(tmp_path):
    """Contract failure on a __retry1 task does NOT create __retry1__retry1."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    out_dir = tmp_path / "OUTPUT"
    out_dir.mkdir()
    out_file = out_dir / "0031_broken__retry1__20260303-130000.md"
    out_file.write_text(MISSING_CONTRACT)
    out_file.touch()

    with patch("watcher._find_recent_output", return_value=out_file), \
         patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", out_dir), \
         patch("watcher.METRICS_FILE", tmp_path / "metrics.json"):
        passed, msgs = verify_artifacts("0031_broken__retry1")

    assert passed is False
    # No chained retry
    assert not any("retry1__retry1" in f.name for f in tasks_dir.iterdir())
    assert list(tasks_dir.iterdir()) == []


def test_verify_no_output_no_retry(tmp_path):
    """Missing output file => no retry (contract check is skipped)."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()

    with patch("watcher._find_recent_output", return_value=None), \
         patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.METRICS_FILE", tmp_path / "metrics.json"):
        passed, msgs = verify_artifacts("0099_missing")

    assert passed is False
    assert list(tasks_dir.iterdir()) == []


# --- Test retry task content detail ------------------------------------------


def test_retry_task_has_repair_instructions(tmp_path):
    """Retry task contains actionable repair instructions."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    out_dir = tmp_path / "OUTPUT"
    out_dir.mkdir()
    out_file = out_dir / "0032_badconf__20260303-120000.md"
    out_file.write_text(BAD_CONFIDENCE)
    out_file.touch()

    with patch("watcher._find_recent_output", return_value=out_file), \
         patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", out_dir), \
         patch("watcher.METRICS_FILE", tmp_path / "metrics.json"):
        passed, msgs = verify_artifacts("0032_badconf")

    assert passed is False
    retry_file = tasks_dir / "0032_badconf__retry1.md"
    assert retry_file.exists()

    content = retry_file.read_text(encoding="utf-8")
    # Must reference the output file path
    assert str(out_file) in content
    # Must include the confidence error
    assert "invalid confidence" in content
    # Must contain the word "Repair" or "repair" in title
    assert "Repair" in content
    # Must instruct about required CONTRACT fields
    assert "## CONTRACT" in content


# --- Run as script -----------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    # Tests that don't need tmp_path
    no_arg_tests = [
        test_is_retry_task_false,
        test_is_retry_task_true,
        test_is_retry_task_embedded,
        test_original_stem_from_retry,
        test_original_stem_no_retry,
    ]

    # Tests that need tmp_path
    tmp_tests = [
        test_create_retry_task_content,
        test_create_retry_task_with_warnings,
        test_maybe_create_retry_creates_task,
        test_maybe_create_retry_skips_if_already_retry,
        test_maybe_create_retry_skips_if_retry_already_exists,
        test_maybe_create_retry_skips_if_retry_inprogress,
        test_maybe_create_retry_skips_if_retry_done,
        test_verify_valid_contract_no_retry,
        test_verify_missing_contract_creates_retry,
        test_verify_retry_task_failure_no_second_retry,
        test_verify_no_output_no_retry,
        test_retry_task_has_repair_instructions,
    ]

    all_tests = no_arg_tests + tmp_tests
    passed = 0

    for t in no_arg_tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")

    for t in tmp_tests:
        try:
            with tempfile.TemporaryDirectory() as td:
                t(Path(td))
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")

    print(f"\n{passed}/{len(all_tests)} tests passed")
