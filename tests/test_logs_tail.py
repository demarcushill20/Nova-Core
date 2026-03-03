"""Tests for logs.tail adapter and runner wiring.

Validates:
- Structured output from logs_tail
- Lines clamped to 1–500
- Entries returned as list
- Command failure handled gracefully
- Runner dispatch produces envelope
"""

import json
from pathlib import Path
from unittest.mock import patch

from tools.adapters.logs_tool import logs_tail


# --- Mock journalctl output --------------------------------------------------

SAMPLE_OUTPUT = """\
2026-03-03T12:00:01+0000 vps systemd[1]: Started NovaCore Watcher.
2026-03-03T12:00:02+0000 vps python3[1234]: Dispatcher started. Monitoring TASKS/ every 60s.
2026-03-03T12:00:03+0000 vps python3[1234]: Scan complete — no new tasks.
2026-03-03T12:01:03+0000 vps python3[1234]: Scan complete — no new tasks.
2026-03-03T12:02:03+0000 vps python3[1234]: TASK DETECTED: 0010_example.md
"""


# --- Tests for logs_tail -----------------------------------------------------


@patch("tools.adapters.logs_tool.run_subprocess")
def test_basic_output(mock_run):
    """Returns structured dict with entries list."""
    mock_run.return_value = {"exit_code": 0, "stdout": SAMPLE_OUTPUT, "stderr": ""}
    result = logs_tail("novacore-watcher", lines=50)

    assert result["ok"] is True
    assert result["service"] == "novacore-watcher"
    assert result["lines"] == 5
    assert isinstance(result["entries"], list)
    assert len(result["entries"]) == 5
    assert result["truncated"] is False
    assert "Dispatcher started" in result["entries"][1]


@patch("tools.adapters.logs_tool.run_subprocess")
def test_entries_are_strings(mock_run):
    """Each entry is a string line."""
    mock_run.return_value = {"exit_code": 0, "stdout": SAMPLE_OUTPUT, "stderr": ""}
    result = logs_tail("novacore-watcher")
    for entry in result["entries"]:
        assert isinstance(entry, str)


@patch("tools.adapters.logs_tool.run_subprocess")
def test_clamp_lines_high(mock_run):
    """Lines > 500 clamped to 500."""
    mock_run.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
    logs_tail("novacore-watcher", lines=9999)

    call_args = mock_run.call_args[0][0]
    # -n flag should be 500
    n_idx = call_args.index("-n")
    assert call_args[n_idx + 1] == "500"


@patch("tools.adapters.logs_tool.run_subprocess")
def test_clamp_lines_low(mock_run):
    """Lines < 1 clamped to 1."""
    mock_run.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
    logs_tail("novacore-watcher", lines=-10)

    call_args = mock_run.call_args[0][0]
    n_idx = call_args.index("-n")
    assert call_args[n_idx + 1] == "1"


@patch("tools.adapters.logs_tool.run_subprocess")
def test_clamp_lines_zero(mock_run):
    """Lines == 0 clamped to 1."""
    mock_run.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
    logs_tail("novacore-watcher", lines=0)

    call_args = mock_run.call_args[0][0]
    n_idx = call_args.index("-n")
    assert call_args[n_idx + 1] == "1"


@patch("tools.adapters.logs_tool.run_subprocess")
def test_command_failure(mock_run):
    """journalctl failure returns ok=False with stderr."""
    mock_run.return_value = {
        "exit_code": 1,
        "stdout": "",
        "stderr": "Failed to get data: No such unit novacore-fake.service",
    }
    result = logs_tail("novacore-fake")

    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert "No such unit" in result["stderr"]
    assert result["entries"] == []
    assert result["lines"] == 0


@patch("tools.adapters.logs_tool.run_subprocess")
def test_truncation_flag(mock_run):
    """Truncated flag set when output exceeds buffer."""
    huge_output = "x" * (101 * 1024)  # > 100KB
    mock_run.return_value = {"exit_code": 0, "stdout": huge_output, "stderr": ""}
    result = logs_tail("novacore-watcher")

    assert result["truncated"] is True
    assert result["ok"] is True


@patch("tools.adapters.logs_tool.run_subprocess")
def test_empty_output(mock_run):
    """Empty journalctl output returns empty entries."""
    mock_run.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
    result = logs_tail("novacore-watcher")

    assert result["ok"] is True
    assert result["entries"] == []
    assert result["lines"] == 0


def test_invalid_service_name():
    """Invalid service name raises ValueError."""
    try:
        logs_tail("rm -rf /")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "Invalid service name" in str(e)


def test_empty_service_name():
    """Empty service name raises ValueError."""
    try:
        logs_tail("")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "required" in str(e)


@patch("tools.adapters.logs_tool.run_subprocess")
def test_journalctl_command_shape(mock_run):
    """Verify the journalctl command is well-formed."""
    mock_run.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
    logs_tail("novacore-watcher", lines=100)

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "journalctl"
    assert "-u" in cmd
    assert "novacore-watcher" in cmd
    assert "--no-pager" in cmd
    assert "-o" in cmd
    assert "short-iso" in cmd


@patch("tools.adapters.logs_tool.run_subprocess")
def test_json_shape(mock_run):
    """Result has all required keys."""
    mock_run.return_value = {"exit_code": 0, "stdout": SAMPLE_OUTPUT, "stderr": ""}
    result = logs_tail("novacore-watcher")

    required_keys = {"ok", "exit_code", "stderr", "service", "lines", "entries", "truncated"}
    assert required_keys.issubset(set(result.keys())), f"Missing: {required_keys - set(result.keys())}"


# --- Runner integration test -------------------------------------------------


def test_runner_dispatch_envelope(tmp_path):
    """run_tool('logs.tail', ...) returns a proper envelope."""
    from tools.runner import run_tool

    audit_file = tmp_path / "audit.jsonl"
    registry = {
        "sandbox_root": str(tmp_path),
        "audit_log": "audit.jsonl",
        "tools": {
            "logs.tail": {
                "description": "Tail logs",
                "args_schema": {"service": {"type": "string", "required": True}},
                "returns": {},
                "safety": ["Read-only."],
            },
        },
    }

    with patch("tools.adapters.logs_tool.run_subprocess") as mock_run:
        mock_run.return_value = {"exit_code": 0, "stdout": SAMPLE_OUTPUT, "stderr": ""}
        envelope = run_tool("logs.tail", {"service": "novacore-watcher", "lines": 50}, registry=registry)

    assert envelope["tool"] == "logs.tail"
    assert envelope["ok"] is True
    assert isinstance(envelope["duration_ms"], int)
    assert envelope["result"]["service"] == "novacore-watcher"
    assert len(envelope["result"]["entries"]) == 5


# --- Run as script -----------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    no_arg_tests = [
        test_basic_output,
        test_entries_are_strings,
        test_clamp_lines_high,
        test_clamp_lines_low,
        test_clamp_lines_zero,
        test_command_failure,
        test_truncation_flag,
        test_empty_output,
        test_invalid_service_name,
        test_empty_service_name,
        test_journalctl_command_shape,
        test_json_shape,
    ]

    tmp_tests = [
        test_runner_dispatch_envelope,
    ]

    passed = 0
    total = len(no_arg_tests) + len(tmp_tests)

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

    print(f"\n{passed}/{total} tests passed")
