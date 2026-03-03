"""Tests for the execution audit envelope in tools/runner.py.

Validates:
- _execute_with_audit produces a structured envelope
- Envelope contains tool, ok, duration_ms, result
- Unregistered tool raises ValueError without executing
- Underlying tool failure is wrapped correctly
- run_tool returns envelope for registered tools
"""

import time
from pathlib import Path
from unittest.mock import patch

from tools.runner import _execute_with_audit, run_tool


# --- Minimal registry for testing -------------------------------------------

MOCK_REGISTRY = {
    "sandbox_root": "/tmp/nova-test",
    "audit_log": "STATE/tool_audit.jsonl",
    "tools": {
        "test.echo": {
            "description": "Echo tool for testing",
            "args_schema": {"text": {"type": "string", "required": True}},
            "returns": {"text": "string"},
            "safety": ["No risk — test only."],
        },
        "test.fail": {
            "description": "Always-failing tool for testing",
            "args_schema": {},
            "returns": {},
            "safety": ["No risk — test only."],
        },
    },
}


# --- Mock tool functions -----------------------------------------------------

def _echo_tool(text="hello"):
    return {"ok": True, "exit_code": 0, "stdout": text, "stderr": ""}


def _slow_tool():
    time.sleep(0.05)
    return {"ok": True, "exit_code": 0, "stdout": "done", "stderr": ""}


def _failing_tool():
    return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "something broke"}


def _raising_tool():
    raise RuntimeError("kaboom")


# --- Tests for _execute_with_audit -------------------------------------------


def test_envelope_structure():
    """Envelope has tool, ok, duration_ms, result keys."""
    envelope = _execute_with_audit("test.echo", _echo_tool, MOCK_REGISTRY, text="hi")
    assert "tool" in envelope
    assert "ok" in envelope
    assert "duration_ms" in envelope
    assert "result" in envelope
    assert envelope["tool"] == "test.echo"
    assert envelope["ok"] is True
    assert isinstance(envelope["duration_ms"], int)
    assert envelope["result"]["stdout"] == "hi"


def test_envelope_duration_present():
    """duration_ms reflects actual execution time."""
    envelope = _execute_with_audit("test.echo", _slow_tool, MOCK_REGISTRY)
    assert envelope["duration_ms"] >= 40  # at least ~50ms sleep


def test_envelope_ok_matches_result():
    """ok in envelope reflects the tool's ok field."""
    envelope = _execute_with_audit("test.fail", _failing_tool, MOCK_REGISTRY)
    assert envelope["ok"] is False
    assert envelope["result"]["exit_code"] == 1
    assert envelope["result"]["stderr"] == "something broke"


def test_unregistered_tool_blocked():
    """Unregistered tool raises ValueError without execution."""
    call_count = 0

    def _tracked():
        nonlocal call_count
        call_count += 1
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}

    try:
        _execute_with_audit("fake.tool", _tracked, MOCK_REGISTRY)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "Unregistered tool" in str(e)
        assert "fake.tool" in str(e)
    assert call_count == 0, "Function must NOT be called for unregistered tool"


def test_unregistered_tool_lists_available():
    """Error message includes available tool names."""
    try:
        _execute_with_audit("nope.missing", _echo_tool, MOCK_REGISTRY)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "test.echo" in str(e)
        assert "test.fail" in str(e)


def test_tool_exception_propagates():
    """Exceptions from the underlying tool propagate through."""
    try:
        _execute_with_audit("test.echo", _raising_tool, MOCK_REGISTRY)
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "kaboom" in str(e)


def test_envelope_duration_zero_floor():
    """Fast tools get duration_ms >= 0."""
    envelope = _execute_with_audit("test.echo", _echo_tool, MOCK_REGISTRY)
    assert envelope["duration_ms"] >= 0


# --- Tests for run_tool returning envelope -----------------------------------


def test_run_tool_returns_envelope(tmp_path):
    """run_tool returns an envelope with tool, ok, duration_ms, result."""
    audit_file = tmp_path / "audit.jsonl"
    registry = {
        "sandbox_root": str(tmp_path),
        "audit_log": "audit.jsonl",
        "tools": {
            "contracts.validate": {
                "description": "Validate contract",
                "args_schema": {"text": {"type": "string", "required": True}},
                "returns": {},
                "safety": ["Read-only."],
            },
        },
    }

    valid_text = (
        "# Output\n\n## CONTRACT\nsummary: test\n"
        "verification: checked\nconfidence: high\nfiles_changed: a.py\n"
    )

    envelope = run_tool("contracts.validate", {"text": valid_text}, registry=registry)
    assert envelope["tool"] == "contracts.validate"
    assert envelope["ok"] is True
    assert isinstance(envelope["duration_ms"], int)
    assert envelope["result"]["result"]["valid"] is True


def test_run_tool_unregistered_returns_error_envelope(tmp_path):
    """Unregistered tool in run_tool returns error envelope, not exception."""
    audit_file = tmp_path / "audit.jsonl"
    registry = {
        "sandbox_root": str(tmp_path),
        "audit_log": "audit.jsonl",
        "tools": {},
    }

    envelope = run_tool("nope.missing", {"x": 1}, registry=registry)
    assert envelope["tool"] == "nope.missing"
    assert envelope["ok"] is False
    assert "Unregistered tool" in envelope["result"]["stderr"]


def test_run_tool_blocked_command_envelope(tmp_path):
    """Blocked shell command returns error envelope."""
    audit_file = tmp_path / "audit.jsonl"
    registry = {
        "sandbox_root": str(tmp_path),
        "audit_log": "audit.jsonl",
        "tools": {
            "shell.run": {
                "description": "Shell",
                "args_schema": {"command": {"type": "string", "required": True}},
                "returns": {},
                "safety": ["Denylist enforced."],
            },
        },
    }

    envelope = run_tool("shell.run", {"command": "rm -rf /"}, registry=registry)
    assert envelope["tool"] == "shell.run"
    assert envelope["ok"] is False
    assert envelope["result"]["exit_code"] == 126
    assert "BLOCKED" in envelope["result"]["stderr"]


# --- Run as script -----------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    no_arg_tests = [
        test_envelope_structure,
        test_envelope_duration_present,
        test_envelope_ok_matches_result,
        test_unregistered_tool_blocked,
        test_unregistered_tool_lists_available,
        test_tool_exception_propagates,
        test_envelope_duration_zero_floor,
    ]

    tmp_tests = [
        test_run_tool_returns_envelope,
        test_run_tool_unregistered_returns_error_envelope,
        test_run_tool_blocked_command_envelope,
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
