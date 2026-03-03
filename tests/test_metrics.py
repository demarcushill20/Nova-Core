"""Tests for observability metrics in watcher.py.

Validates:
- metrics.json creation on first event
- Counter increment on repeated events
- Per-tool (per-stem) tracking
- Corruption recovery (invalid JSON → reset)
- Retry metrics (retry_issued, retry_success, retry_failed)
- _update_metrics never throws
"""

import json
from pathlib import Path
from unittest.mock import patch

from watcher import (
    _update_metrics,
    _check_contract,
    _maybe_create_retry,
    verify_artifacts,
    METRICS_FILE,
)


# --- Sample outputs ----------------------------------------------------------

VALID_OUTPUT = """\
# Output for: 0040_metrics

Task completed.

## CONTRACT
summary: Built feature
verification: all tests pass
confidence: high
files_changed: module.py
"""

MISSING_CONTRACT = """\
# Output for: 0041_broken

Did some work but forgot the contract block.
"""


# --- Tests for _update_metrics -----------------------------------------------


def test_metrics_file_created(tmp_path):
    """First call creates metrics.json."""
    metrics = tmp_path / "metrics.json"
    with patch("watcher.METRICS_FILE", metrics):
        _update_metrics("contract_success", "0040_test")

    assert metrics.exists()
    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert data["contract_success"]["_total"] == 1
    assert data["contract_success"]["0040_test"] == 1


def test_metrics_increment(tmp_path):
    """Repeated calls increment counters."""
    metrics = tmp_path / "metrics.json"
    with patch("watcher.METRICS_FILE", metrics):
        _update_metrics("contract_success", "0040_test")
        _update_metrics("contract_success", "0040_test")
        _update_metrics("contract_success", "0040_test")

    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert data["contract_success"]["_total"] == 3
    assert data["contract_success"]["0040_test"] == 3


def test_metrics_per_tool_tracking(tmp_path):
    """Different tool_names get separate counters."""
    metrics = tmp_path / "metrics.json"
    with patch("watcher.METRICS_FILE", metrics):
        _update_metrics("contract_success", "0040_alpha")
        _update_metrics("contract_success", "0041_beta")
        _update_metrics("contract_success", "0040_alpha")

    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert data["contract_success"]["_total"] == 3
    assert data["contract_success"]["0040_alpha"] == 2
    assert data["contract_success"]["0041_beta"] == 1


def test_metrics_unknown_tool(tmp_path):
    """None tool_name defaults to 'unknown'."""
    metrics = tmp_path / "metrics.json"
    with patch("watcher.METRICS_FILE", metrics):
        _update_metrics("contract_failure", None)

    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert data["contract_failure"]["unknown"] == 1
    assert data["contract_failure"]["_total"] == 1


def test_metrics_multiple_events(tmp_path):
    """Different event types tracked independently."""
    metrics = tmp_path / "metrics.json"
    with patch("watcher.METRICS_FILE", metrics):
        _update_metrics("contract_success", "0040_a")
        _update_metrics("contract_failure", "0041_b")
        _update_metrics("retry_issued", "0041_b")

    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert data["contract_success"]["_total"] == 1
    assert data["contract_failure"]["_total"] == 1
    assert data["retry_issued"]["_total"] == 1


def test_metrics_corruption_recovery(tmp_path):
    """Corrupt JSON file is reset, not crash."""
    metrics = tmp_path / "metrics.json"
    metrics.write_text("NOT VALID JSON {{{{", encoding="utf-8")

    with patch("watcher.METRICS_FILE", metrics):
        _update_metrics("contract_success", "0040_test")

    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert data["contract_success"]["_total"] == 1
    assert data["contract_success"]["0040_test"] == 1


def test_metrics_non_dict_recovery(tmp_path):
    """Non-dict JSON (e.g. a list) is reset."""
    metrics = tmp_path / "metrics.json"
    metrics.write_text("[1, 2, 3]", encoding="utf-8")

    with patch("watcher.METRICS_FILE", metrics):
        _update_metrics("contract_failure", "0042_test")

    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert data["contract_failure"]["_total"] == 1


def test_metrics_event_overwritten_recovery(tmp_path):
    """If an event key is not a dict (e.g. was set to a string), it resets."""
    metrics = tmp_path / "metrics.json"
    metrics.write_text('{"contract_success": "not_a_dict"}', encoding="utf-8")

    with patch("watcher.METRICS_FILE", metrics):
        _update_metrics("contract_success", "0040_test")

    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert isinstance(data["contract_success"], dict)
    assert data["contract_success"]["_total"] == 1


def test_metrics_never_throws(tmp_path):
    """Even with an unwritable path, _update_metrics should not raise."""
    bad_path = tmp_path / "no_such_dir" / "metrics.json"
    with patch("watcher.METRICS_FILE", bad_path):
        # Should silently fail — no exception
        _update_metrics("contract_success", "0040_test")


# --- Integration: metrics wired into verify_artifacts ------------------------


def test_verify_success_records_metric(tmp_path):
    """Successful contract validation emits contract_success metric."""
    metrics = tmp_path / "metrics.json"
    out_dir = tmp_path / "OUTPUT"
    out_dir.mkdir()
    out_file = out_dir / "0040_metrics__20260303-120000.md"
    out_file.write_text(VALID_OUTPUT, encoding="utf-8")
    out_file.touch()

    with patch("watcher._find_recent_output", return_value=out_file), \
         patch("watcher.METRICS_FILE", metrics), \
         patch("watcher.TASKS_DIR", tmp_path / "TASKS"):
        passed, msgs = verify_artifacts("0040_metrics")

    assert passed is True
    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert data["contract_success"]["_total"] == 1
    assert data["contract_success"]["0040_metrics"] == 1
    assert "contract_failure" not in data


def test_verify_failure_records_metric(tmp_path):
    """Contract failure emits contract_failure metric."""
    metrics = tmp_path / "metrics.json"
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    out_dir = tmp_path / "OUTPUT"
    out_dir.mkdir()
    out_file = out_dir / "0041_broken__20260303-120000.md"
    out_file.write_text(MISSING_CONTRACT, encoding="utf-8")
    out_file.touch()

    with patch("watcher._find_recent_output", return_value=out_file), \
         patch("watcher.METRICS_FILE", metrics), \
         patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", out_dir):
        passed, msgs = verify_artifacts("0041_broken")

    assert passed is False
    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert data["contract_failure"]["_total"] == 1
    assert "contract_success" not in data


def test_verify_failure_records_retry_issued(tmp_path):
    """Contract failure on original task emits retry_issued metric."""
    metrics = tmp_path / "metrics.json"
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    out_dir = tmp_path / "OUTPUT"
    out_dir.mkdir()
    out_file = out_dir / "0041_broken__20260303-120000.md"
    out_file.write_text(MISSING_CONTRACT, encoding="utf-8")
    out_file.touch()

    with patch("watcher._find_recent_output", return_value=out_file), \
         patch("watcher.METRICS_FILE", metrics), \
         patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", out_dir):
        verify_artifacts("0041_broken")

    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert data["retry_issued"]["_total"] == 1
    assert data["retry_issued"]["0041_broken"] == 1


def test_retry_success_metric(tmp_path):
    """Successful retry task emits retry_success metric."""
    metrics = tmp_path / "metrics.json"
    out_dir = tmp_path / "OUTPUT"
    out_dir.mkdir()
    out_file = out_dir / "0041_broken__retry1__20260303-130000.md"
    out_file.write_text(VALID_OUTPUT, encoding="utf-8")
    out_file.touch()

    with patch("watcher._find_recent_output", return_value=out_file), \
         patch("watcher.METRICS_FILE", metrics), \
         patch("watcher.TASKS_DIR", tmp_path / "TASKS"):
        passed, msgs = verify_artifacts("0041_broken__retry1")

    assert passed is True
    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert data["retry_success"]["_total"] == 1
    assert data["retry_success"]["0041_broken__retry1"] == 1
    assert data["contract_success"]["_total"] == 1


def test_retry_failure_metric(tmp_path):
    """Failed retry task emits retry_failed metric."""
    metrics = tmp_path / "metrics.json"
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    out_dir = tmp_path / "OUTPUT"
    out_dir.mkdir()
    out_file = out_dir / "0041_broken__retry1__20260303-130000.md"
    out_file.write_text(MISSING_CONTRACT, encoding="utf-8")
    out_file.touch()

    with patch("watcher._find_recent_output", return_value=out_file), \
         patch("watcher.METRICS_FILE", metrics), \
         patch("watcher.TASKS_DIR", tasks_dir), \
         patch("watcher.OUTPUT_DIR", out_dir):
        passed, msgs = verify_artifacts("0041_broken__retry1")

    assert passed is False
    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert data["retry_failed"]["_total"] == 1
    assert data["retry_failed"]["0041_broken__retry1"] == 1
    assert data["contract_failure"]["_total"] == 1
    # No retry_issued because retry tasks don't chain
    assert "retry_issued" not in data


def test_no_output_no_metrics(tmp_path):
    """Missing output file doesn't emit any contract/retry metrics."""
    metrics = tmp_path / "metrics.json"
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()

    with patch("watcher._find_recent_output", return_value=None), \
         patch("watcher.METRICS_FILE", metrics), \
         patch("watcher.TASKS_DIR", tasks_dir):
        passed, msgs = verify_artifacts("0099_missing")

    assert passed is False
    # No metrics file should be created (no contract check happened)
    assert not metrics.exists()


# --- Run as script -----------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    tests = [
        test_metrics_file_created,
        test_metrics_increment,
        test_metrics_per_tool_tracking,
        test_metrics_unknown_tool,
        test_metrics_multiple_events,
        test_metrics_corruption_recovery,
        test_metrics_non_dict_recovery,
        test_metrics_event_overwritten_recovery,
        test_metrics_never_throws,
        test_verify_success_records_metric,
        test_verify_failure_records_metric,
        test_verify_failure_records_retry_issued,
        test_retry_success_metric,
        test_retry_failure_metric,
        test_no_output_no_metrics,
    ]

    passed = 0
    for t in tests:
        try:
            with tempfile.TemporaryDirectory() as td:
                t(Path(td))
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")

    print(f"\n{passed}/{len(tests)} tests passed")
