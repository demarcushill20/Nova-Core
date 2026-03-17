"""Tests for the pytest test gate in watcher.py.

Validates that _run_test_gate() and _apply_test_gate_result() work correctly:
- Pass: continue delivery
- Failure: append summary, downgrade confidence to low
- Timeout: skip gate, continue delivery
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from watcher import _apply_test_gate_result, _run_test_gate

# --- Helpers -----------------------------------------------------------------


def _write_output(tmp_path: Path, stem: str, content: str) -> Path:
    """Write an output file and return its path."""
    out_dir = tmp_path / "OUTPUT"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{stem}__20260310-120000.md"
    out_file.write_text(content, encoding="utf-8")
    return out_file


VALID_OUTPUT = """\
# Output for: 0500_test

Task completed.

## CONTRACT
summary: Did the thing
verification: checked
confidence: high
files_changed: foo.py
"""

VALID_OUTPUT_MEDIUM = """\
# Output for: 0501_test

Task completed.

## CONTRACT
summary: Did the thing
verification: checked
confidence: medium
files_changed: foo.py
"""


# --- Helpers for async subprocess mocking -----------------------------------


def _make_mock_process(returncode: int, stdout: str = "", stderr: str = ""):
    """Create a mock asyncio subprocess process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(
        return_value=(
            stdout.encode("utf-8"),
            stderr.encode("utf-8"),
        )
    )
    proc.kill = MagicMock()
    return proc


# --- Tests for _run_test_gate ------------------------------------------------


@pytest.mark.asyncio
async def test_run_test_gate_pass():
    """When pytest returns 0, status should be 'pass'."""
    mock_proc = _make_mock_process(0, stdout="10 passed in 1.5s")

    with patch("watcher.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        result = await _run_test_gate("0500_test")

    assert result["status"] == "pass"
    assert "passed" in result["summary"].lower()


@pytest.mark.asyncio
async def test_run_test_gate_fail():
    """When pytest returns non-zero, status should be 'fail'."""
    mock_proc = _make_mock_process(1, stdout="FAILED tests/test_foo.py::test_bar\n1 failed")

    with patch("watcher.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        result = await _run_test_gate("0500_test")

    assert result["status"] == "fail"
    assert "1" in result["summary"]


@pytest.mark.asyncio
async def test_run_test_gate_timeout():
    """When pytest times out, status should be 'timeout'."""
    mock_proc = _make_mock_process(1)
    # The second communicate() call (after kill) must succeed
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.kill = MagicMock()

    async def _fake_wait_for(coro, *, timeout=None):
        # Consume the coroutine to avoid warnings
        coro.close()
        raise asyncio.TimeoutError()

    with (
        patch("watcher.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc),
        patch("watcher.asyncio.wait_for", side_effect=_fake_wait_for),
    ):
        result = await _run_test_gate("0500_test")

    assert result["status"] == "timeout"
    assert "timed out" in result["summary"].lower()


@pytest.mark.asyncio
async def test_run_test_gate_error():
    """When an exception occurs, status should be 'error'."""
    with patch(
        "watcher.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        side_effect=FileNotFoundError("pytest not found"),
    ):
        result = await _run_test_gate("0500_test")

    assert result["status"] == "error"
    assert "error" in result["summary"].lower()


# --- Tests for _apply_test_gate_result ---------------------------------------


def test_apply_pass_returns_true(tmp_path):
    """On pass, delivery should proceed."""
    gate = {"status": "pass", "summary": "All tests passed", "output": ""}
    result = _apply_test_gate_result("0500_test", gate)
    assert result is True


def test_apply_timeout_returns_true(tmp_path):
    """On timeout, delivery should proceed (skip gate)."""
    gate = {"status": "timeout", "summary": "timed out", "output": ""}
    result = _apply_test_gate_result("0500_test", gate)
    assert result is True


def test_apply_error_returns_true(tmp_path):
    """On error, delivery should proceed (skip gate)."""
    gate = {"status": "error", "summary": "error", "output": ""}
    result = _apply_test_gate_result("0500_test", gate)
    assert result is True


def test_apply_fail_downgrades_confidence(tmp_path):
    """On failure, confidence should be downgraded to 'low'."""
    out_file = _write_output(tmp_path, "0500_test", VALID_OUTPUT)
    gate = {"status": "fail", "summary": "pytest exited with code 1", "output": "FAILED test_foo.py::test_bar"}

    with patch("watcher._find_recent_output", return_value=out_file):
        result = _apply_test_gate_result("0500_test", gate)

    assert result is False
    text = out_file.read_text(encoding="utf-8")
    assert "## TEST GATE FAILURE" in text
    assert "confidence: low" in text
    assert "confidence: high" not in text


def test_apply_fail_downgrades_medium_confidence(tmp_path):
    """On failure, medium confidence should also be downgraded to 'low'."""
    out_file = _write_output(tmp_path, "0501_test", VALID_OUTPUT_MEDIUM)
    gate = {"status": "fail", "summary": "pytest exited with code 1", "output": "FAILED test_foo.py::test_bar"}

    with patch("watcher._find_recent_output", return_value=out_file):
        result = _apply_test_gate_result("0501_test", gate)

    assert result is False
    text = out_file.read_text(encoding="utf-8")
    assert "confidence: low" in text
    assert "confidence: medium" not in text


def test_apply_fail_appends_summary(tmp_path):
    """On failure, test failure summary should be appended to output."""
    out_file = _write_output(tmp_path, "0500_test", VALID_OUTPUT)
    gate = {
        "status": "fail",
        "summary": "pytest exited with code 1",
        "output": "FAILED test_foo.py::test_bar\n1 failed, 9 passed",
    }

    with patch("watcher._find_recent_output", return_value=out_file):
        _apply_test_gate_result("0500_test", gate)

    text = out_file.read_text(encoding="utf-8")
    assert "## TEST GATE FAILURE" in text
    assert "FAILED test_foo.py::test_bar" in text
    assert "1 failed, 9 passed" in text


def test_apply_fail_no_output_file(tmp_path):
    """On failure with no output file, should return False."""
    gate = {"status": "fail", "summary": "failed", "output": ""}

    with patch("watcher._find_recent_output", return_value=None):
        result = _apply_test_gate_result("0999_missing", gate)

    assert result is False
