"""Tests for repo.files.read adapter.

Validates:
- Read small file success with structured output
- Truncation behavior at max_bytes boundary
- Rejects ../ traversal that escapes sandbox
- Rejects absolute path outside repo
- Clamps max_bytes to valid range
- Missing file returns ok=False
- Runner dispatch integration
"""

import json
from pathlib import Path

import pytest

from tools.adapters.repo_files import repo_read


# --- Read success -----------------------------------------------------------


def test_read_small_file(tmp_path):
    """Reading a small file returns correct structured output."""
    f = tmp_path / "hello.txt"
    f.write_text("Hello, NovaCore!", encoding="utf-8")

    result = repo_read("hello.txt", sandbox=tmp_path)

    assert result["ok"] is True
    assert result["content"] == "Hello, NovaCore!"
    assert result["bytes"] == 16
    assert result["truncated"] is False
    assert result["path"] == str(f)


def test_read_nested_path(tmp_path):
    """Reading a file in a subdirectory works."""
    sub = tmp_path / "sub" / "dir"
    sub.mkdir(parents=True)
    f = sub / "data.txt"
    f.write_text("nested content", encoding="utf-8")

    result = repo_read("sub/dir/data.txt", sandbox=tmp_path)

    assert result["ok"] is True
    assert result["content"] == "nested content"


def test_read_binary_file(tmp_path):
    """Binary files are decoded with errors='replace'."""
    f = tmp_path / "bin.dat"
    f.write_bytes(b"OK\xff\xfe end")

    result = repo_read("bin.dat", sandbox=tmp_path)

    assert result["ok"] is True
    assert "\ufffd" in result["content"]  # replacement character
    assert result["bytes"] == 8


# --- Truncation -------------------------------------------------------------


def test_truncation_at_max_bytes(tmp_path):
    """File larger than max_bytes is truncated."""
    f = tmp_path / "big.txt"
    content = "A" * 5000
    f.write_text(content, encoding="utf-8")

    result = repo_read("big.txt", max_bytes=2048, sandbox=tmp_path)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["bytes"] == 5000
    assert len(result["content"]) == 2048


def test_no_truncation_when_under_limit(tmp_path):
    """File smaller than max_bytes is not truncated."""
    f = tmp_path / "small.txt"
    f.write_text("tiny", encoding="utf-8")

    result = repo_read("small.txt", max_bytes=2048, sandbox=tmp_path)

    assert result["ok"] is True
    assert result["truncated"] is False
    assert result["content"] == "tiny"


# --- Sandbox enforcement ----------------------------------------------------


def test_rejects_dotdot_traversal(tmp_path):
    """Paths with ../ that escape sandbox are rejected."""
    with pytest.raises(ValueError, match="escapes sandbox"):
        repo_read("../../../etc/passwd", sandbox=tmp_path)


def test_rejects_dotdot_in_middle(tmp_path):
    """Paths with ../ in the middle that escape sandbox are rejected."""
    sub = tmp_path / "sub"
    sub.mkdir()
    with pytest.raises(ValueError, match="escapes sandbox"):
        repo_read("sub/../../etc/passwd", sandbox=tmp_path)


def test_allows_dotdot_within_sandbox(tmp_path):
    """../ that stays within sandbox is allowed."""
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    f = tmp_path / "a" / "file.txt"
    f.write_text("ok", encoding="utf-8")

    result = repo_read("a/b/../file.txt", sandbox=tmp_path)

    assert result["ok"] is True
    assert result["content"] == "ok"


def test_rejects_absolute_path_outside(tmp_path):
    """Absolute paths outside sandbox are rejected."""
    with pytest.raises(ValueError, match="escapes sandbox"):
        repo_read("/etc/passwd", sandbox=tmp_path)


def test_allows_absolute_path_inside(tmp_path):
    """Absolute paths inside sandbox are allowed."""
    f = tmp_path / "inside.txt"
    f.write_text("allowed", encoding="utf-8")

    result = repo_read(str(f), sandbox=tmp_path)

    assert result["ok"] is True
    assert result["content"] == "allowed"


# --- max_bytes clamping -----------------------------------------------------


def test_clamps_max_bytes_low(tmp_path):
    """max_bytes below minimum is clamped to 1024."""
    f = tmp_path / "data.txt"
    content = "X" * 2000
    f.write_text(content, encoding="utf-8")

    result = repo_read("data.txt", max_bytes=10, sandbox=tmp_path)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["content"]) == 1024  # clamped to minimum


def test_clamps_max_bytes_high(tmp_path):
    """max_bytes above maximum is clamped to 500000."""
    f = tmp_path / "data.txt"
    f.write_text("small file", encoding="utf-8")

    # Should not raise even with absurdly high value
    result = repo_read("data.txt", max_bytes=999_999_999, sandbox=tmp_path)

    assert result["ok"] is True
    assert result["truncated"] is False


# --- Error cases ------------------------------------------------------------


def test_missing_file(tmp_path):
    """Missing file returns ok=False with error message."""
    result = repo_read("nonexistent.txt", sandbox=tmp_path)

    assert result["ok"] is False
    assert result["bytes"] == 0
    assert "not found" in result.get("error", "").lower()


def test_empty_path_raises():
    """Empty path raises ValueError."""
    with pytest.raises(ValueError, match="path is required"):
        repo_read("")


# --- Runner integration -----------------------------------------------------


def test_runner_dispatch(tmp_path):
    """repo.files.read is callable through run_tool."""
    from tools.runner import run_tool
    from tools.registry import load_registry

    f = tmp_path / "runner_test.txt"
    f.write_text("via runner", encoding="utf-8")

    registry = load_registry()
    # Override sandbox to tmp_path for test isolation
    registry["sandbox_root"] = str(tmp_path)

    envelope = run_tool(
        "repo.files.read",
        {"path": "runner_test.txt"},
        registry=registry,
    )

    assert envelope["tool"] == "repo.files.read"
    assert envelope["ok"] is True
    result = envelope["result"]
    assert result["content"] == "via runner"
    assert result["truncated"] is False


# --- JSON output example (for verification) ---------------------------------


def test_json_output_shape(tmp_path):
    """Result is JSON-serializable with expected keys."""
    f = tmp_path / "shape.txt"
    f.write_text("shape test", encoding="utf-8")

    result = repo_read("shape.txt", sandbox=tmp_path)

    # Must be JSON-serializable
    serialized = json.dumps(result)
    parsed = json.loads(serialized)

    assert set(parsed.keys()) == {"ok", "path", "bytes", "truncated", "content"}
    assert isinstance(parsed["ok"], bool)
    assert isinstance(parsed["path"], str)
    assert isinstance(parsed["bytes"], int)
    assert isinstance(parsed["truncated"], bool)
    assert isinstance(parsed["content"], str)
