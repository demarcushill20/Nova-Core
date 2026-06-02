from __future__ import annotations

import subprocess
from unittest import mock

from utils.claude_health import (
    ClaudeHealth,
    check_claude_health,
    classify_claude_error,
)


def test_claude_health_ok():
    completed = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout="OK\n", stderr="")
    with mock.patch("utils.claude_health.subprocess.run", return_value=completed):
        result = check_claude_health("/fake/claude", timeout_s=1)
    assert result.ok is True
    assert result.reason == "ok"


def test_claude_health_auth_failure():
    completed = subprocess.CompletedProcess(
        args=["claude"],
        returncode=1,
        stdout="Failed to authenticate. API Error: 401 Invalid authentication credentials",
        stderr="",
    )
    with mock.patch("utils.claude_health.subprocess.run", return_value=completed):
        result = check_claude_health("/fake/claude", timeout_s=1)
    assert result.ok is False
    assert result.reason == "auth_failed"


def test_claude_health_missing_binary():
    with mock.patch(
        "utils.claude_health.subprocess.run",
        side_effect=FileNotFoundError("No such file or directory: '/fake/claude'"),
    ):
        result = check_claude_health("/fake/claude", timeout_s=1)
    assert result.ok is False
    assert result.reason == "missing_binary"


def test_claude_health_timeout():
    with mock.patch(
        "utils.claude_health.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1),
    ):
        result = check_claude_health("/fake/claude", timeout_s=1)
    assert result.ok is False
    assert result.reason == "timeout"


def test_claude_health_usage_limited():
    completed = subprocess.CompletedProcess(
        args=["claude"],
        returncode=1,
        stdout="",
        stderr="Error: usage limit reached for this account",
    )
    with mock.patch("utils.claude_health.subprocess.run", return_value=completed):
        result = check_claude_health("/fake/claude", timeout_s=1)
    assert result.ok is False
    assert result.reason == "usage_limited"


def test_classify_claude_error_direct():
    assert classify_claude_error("Failed to authenticate") == "auth_failed"
    assert classify_claude_error("rate limit exceeded") == "usage_limited"
    assert classify_claude_error("command not found") == "missing_binary"
    assert classify_claude_error("something weird happened") == "cli_error"


def test_claude_health_dataclass_defaults():
    health = ClaudeHealth(ok=True, reason="ok")
    assert health.detail == ""
