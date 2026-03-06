"""Tests for repo.files.read, repo.files.write, and repo.files.patch adapters.

Validates:
- Read small file success with structured output
- Truncation behavior at max_bytes boundary
- Rejects ../ traversal that escapes sandbox
- Rejects absolute path outside repo
- Clamps max_bytes to valid range
- Missing file returns ok=False
- Runner dispatch integration
- Write: create new file
- Write: overwrite existing file
- Write: nested file with make_dirs=True
- Write: nested file with make_dirs=False fails
- Write: rejects ../ traversal
- Write: rejects absolute path outside repo
- Write: verification after write
- Write: atomic temp file cleanup on failure
- Write: runner dispatch integration
- Write: output path is normalized relative path
- Patch: replace success
- Patch: replace count=1
- Patch: replace missing text => ok=false
- Patch: append success
- Patch: multiple operations in sequence
- Patch: create_if_missing=True
- Patch: create_if_missing=False on missing file
- Patch: rejects traversal / outside repo
- Patch: runner dispatch integration
- Patch: normalized relative path output
- Patch: verification after patch
"""

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from tools.adapters.repo_files import repo_patch, repo_read, repo_write


# --- Read success -----------------------------------------------------------


def test_read_small_file(tmp_path):
    """Reading a small file returns correct structured output."""
    f = tmp_path / "hello.txt"
    f.write_text("Hello, NovaCore!", encoding="utf-8")

    result = repo_read("hello.txt", _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["content"] == "Hello, NovaCore!"
    assert result["bytes"] == 16
    assert result["truncated"] is False
    assert result["path"] == "hello.txt"


def test_read_nested_path(tmp_path):
    """Reading a file in a subdirectory works."""
    sub = tmp_path / "sub" / "dir"
    sub.mkdir(parents=True)
    f = sub / "data.txt"
    f.write_text("nested content", encoding="utf-8")

    result = repo_read("sub/dir/data.txt", _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["content"] == "nested content"
    assert result["path"] == "sub/dir/data.txt"


def test_read_binary_file(tmp_path):
    """Binary files are decoded with errors='replace'."""
    f = tmp_path / "bin.dat"
    f.write_bytes(b"OK\xff\xfe end")

    result = repo_read("bin.dat", _sandbox=tmp_path)

    assert result["ok"] is True
    assert "\ufffd" in result["content"]  # replacement character
    assert result["bytes"] == 8


# --- Truncation -------------------------------------------------------------


def test_truncation_at_max_bytes(tmp_path):
    """File larger than max_bytes is truncated."""
    f = tmp_path / "big.txt"
    content = "A" * 5000
    f.write_text(content, encoding="utf-8")

    result = repo_read("big.txt", max_bytes=2048, _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["bytes"] == 5000
    assert len(result["content"]) == 2048


def test_no_truncation_when_under_limit(tmp_path):
    """File smaller than max_bytes is not truncated."""
    f = tmp_path / "small.txt"
    f.write_text("tiny", encoding="utf-8")

    result = repo_read("small.txt", max_bytes=2048, _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["truncated"] is False
    assert result["content"] == "tiny"


# --- Sandbox enforcement ----------------------------------------------------


def test_rejects_dotdot_traversal(tmp_path):
    """Paths with ../ that escape sandbox are rejected."""
    with pytest.raises(ValueError, match="escapes sandbox"):
        repo_read("../../../etc/passwd", _sandbox=tmp_path)


def test_rejects_dotdot_in_middle(tmp_path):
    """Paths with ../ in the middle that escape sandbox are rejected."""
    sub = tmp_path / "sub"
    sub.mkdir()
    with pytest.raises(ValueError, match="escapes sandbox"):
        repo_read("sub/../../etc/passwd", _sandbox=tmp_path)


def test_allows_dotdot_within_sandbox(tmp_path):
    """../ that stays within sandbox is allowed."""
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    f = tmp_path / "a" / "file.txt"
    f.write_text("ok", encoding="utf-8")

    result = repo_read("a/b/../file.txt", _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["content"] == "ok"
    assert result["path"] == "a/file.txt"  # normalized, no ..


def test_rejects_absolute_path_outside(tmp_path):
    """Absolute paths outside sandbox are rejected."""
    with pytest.raises(ValueError, match="escapes sandbox"):
        repo_read("/etc/passwd", _sandbox=tmp_path)


def test_allows_absolute_path_inside(tmp_path):
    """Absolute paths inside sandbox are allowed."""
    f = tmp_path / "inside.txt"
    f.write_text("allowed", encoding="utf-8")

    result = repo_read(str(f), _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["content"] == "allowed"


# --- max_bytes clamping -----------------------------------------------------


def test_clamps_max_bytes_low(tmp_path):
    """max_bytes below minimum is clamped to 1024."""
    f = tmp_path / "data.txt"
    content = "X" * 2000
    f.write_text(content, encoding="utf-8")

    result = repo_read("data.txt", max_bytes=10, _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["content"]) == 1024  # clamped to minimum


def test_clamps_max_bytes_high(tmp_path):
    """max_bytes above maximum is clamped to 500000."""
    f = tmp_path / "data.txt"
    f.write_text("small file", encoding="utf-8")

    # Should not raise even with absurdly high value
    result = repo_read("data.txt", max_bytes=999_999_999, _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["truncated"] is False


# --- Error cases ------------------------------------------------------------


def test_missing_file(tmp_path):
    """Missing file returns ok=False with error message."""
    result = repo_read("nonexistent.txt", _sandbox=tmp_path)

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

    result = repo_read("shape.txt", _sandbox=tmp_path)

    # Must be JSON-serializable
    serialized = json.dumps(result)
    parsed = json.loads(serialized)

    assert set(parsed.keys()) == {"ok", "path", "bytes", "truncated", "content"}
    assert isinstance(parsed["ok"], bool)
    assert isinstance(parsed["path"], str)
    assert isinstance(parsed["bytes"], int)
    assert isinstance(parsed["truncated"], bool)
    assert isinstance(parsed["content"], str)
    # path must be repo-relative, not absolute
    assert parsed["path"] == "shape.txt"
    assert not parsed["path"].startswith("/")


# =============================================================================
# repo.files.write tests
# =============================================================================


# --- Write: create new file -------------------------------------------------


def test_write_create_new_file(tmp_path):
    """Writing to a non-existent path creates the file."""
    result = repo_write("new.txt", "hello world", _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["created"] is True
    assert result["overwritten"] is False
    assert result["verified"] is True
    assert result["bytes_written"] == len("hello world".encode("utf-8"))
    assert result["message"] == "created"
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello world"


# --- Write: overwrite existing file -----------------------------------------


def test_write_overwrite_existing(tmp_path):
    """Writing to an existing file overwrites it."""
    f = tmp_path / "exist.txt"
    f.write_text("old content", encoding="utf-8")

    result = repo_write("exist.txt", "new content", _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["created"] is False
    assert result["overwritten"] is True
    assert result["verified"] is True
    assert result["message"] == "overwritten"
    assert f.read_text(encoding="utf-8") == "new content"


# --- Write: nested with make_dirs=True --------------------------------------


def test_write_nested_make_dirs(tmp_path):
    """Writing to a nested path with make_dirs=True creates directories."""
    result = repo_write("a/b/c/deep.txt", "nested", make_dirs=True, _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["created"] is True
    assert result["path"] == "a/b/c/deep.txt"
    assert (tmp_path / "a" / "b" / "c" / "deep.txt").read_text() == "nested"


# --- Write: nested with make_dirs=False fails --------------------------------


def test_write_nested_no_make_dirs(tmp_path):
    """Writing to a nested path with make_dirs=False returns ok=False."""
    result = repo_write("x/y/z/file.txt", "content", make_dirs=False, _sandbox=tmp_path)

    assert result["ok"] is False
    assert result["created"] is False
    assert "does not exist" in result["message"].lower()
    assert not (tmp_path / "x").exists()


# --- Write: rejects ../ traversal -------------------------------------------


def test_write_rejects_dotdot_traversal(tmp_path):
    """Paths with ../ that escape sandbox return ok=False."""
    result = repo_write("../../../etc/evil.txt", "pwned", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "escapes sandbox" in result["message"].lower()


def test_write_rejects_dotdot_in_middle(tmp_path):
    """Paths with ../ in the middle that escape sandbox return ok=False."""
    sub = tmp_path / "sub"
    sub.mkdir()
    result = repo_write("sub/../../etc/evil.txt", "pwned", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "escapes sandbox" in result["message"].lower()


# --- Write: rejects absolute path outside repo ------------------------------


def test_write_rejects_absolute_outside(tmp_path):
    """Absolute paths outside sandbox return ok=False."""
    result = repo_write("/etc/evil.txt", "pwned", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "escapes sandbox" in result["message"].lower()


# --- Write: empty path ------------------------------------------------------


def test_write_empty_path(tmp_path):
    """Empty path returns ok=False."""
    result = repo_write("", "content", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "path is required" in result["message"].lower()


# --- Write: verification after write ----------------------------------------


def test_write_verification(tmp_path):
    """Verify that written content is read back correctly."""
    content = "Unicode: \u00e9\u00e8\u00ea \u2603 \U0001f600"
    result = repo_write("unicode.txt", content, _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["verified"] is True
    # Double-check via direct read
    assert (tmp_path / "unicode.txt").read_text(encoding="utf-8") == content


# --- Write: atomic temp file cleanup on failure ------------------------------


def test_write_temp_cleanup_on_failure(tmp_path):
    """Temp files are cleaned up if the write fails."""
    # Make parent dir read-only to force os.replace to fail
    sub = tmp_path / "readonly_parent"
    sub.mkdir()
    target = sub / "file.txt"
    target.write_text("original", encoding="utf-8")

    # Mock os.replace to simulate a failure
    with mock.patch("tools.adapters.repo_files.os.replace", side_effect=OSError("mock replace failure")):
        result = repo_write("readonly_parent/file.txt", "new content", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "mock replace failure" in result["message"].lower()

    # Verify no .repo_write_ temp files left behind
    temps = [f for f in sub.iterdir() if f.name.startswith(".repo_write_")]
    assert temps == [], f"Temp files not cleaned up: {temps}"

    # Original file should be untouched
    assert target.read_text(encoding="utf-8") == "original"


# --- Write: runner dispatch integration -------------------------------------


def test_write_runner_dispatch(tmp_path):
    """repo.files.write is callable through run_tool."""
    from tools.runner import run_tool
    from tools.registry import load_registry

    registry = load_registry()
    registry["sandbox_root"] = str(tmp_path)

    envelope = run_tool(
        "repo.files.write",
        {"path": "runner_out.txt", "content": "via runner"},
        registry=registry,
    )

    assert envelope["tool"] == "repo.files.write"
    assert envelope["ok"] is True
    result = envelope["result"]
    assert result["created"] is True
    assert result["verified"] is True
    assert (tmp_path / "runner_out.txt").read_text() == "via runner"


# --- Write: output path is normalized relative path -------------------------


def test_write_normalized_path(tmp_path):
    """Output path is always repo-relative, never absolute."""
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)

    result = repo_write("a/b/../b/file.txt", "normalized", _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["path"] == "a/b/file.txt"
    assert not result["path"].startswith("/")


# --- Write: JSON output shape -----------------------------------------------


def test_write_json_output_shape(tmp_path):
    """Write result is JSON-serializable with expected keys."""
    result = repo_write("shape_write.txt", "test", _sandbox=tmp_path)

    serialized = json.dumps(result)
    parsed = json.loads(serialized)

    expected_keys = {"ok", "path", "bytes_written", "created", "overwritten", "verified", "message"}
    assert set(parsed.keys()) == expected_keys
    assert isinstance(parsed["ok"], bool)
    assert isinstance(parsed["path"], str)
    assert isinstance(parsed["bytes_written"], int)
    assert isinstance(parsed["created"], bool)
    assert isinstance(parsed["overwritten"], bool)
    assert isinstance(parsed["verified"], bool)
    assert isinstance(parsed["message"], str)
    assert not parsed["path"].startswith("/")


# =============================================================================
# repo.files.patch tests
# =============================================================================


# --- Patch: replace success --------------------------------------------------


def test_patch_replace_success(tmp_path):
    """Replace operation substitutes all occurrences by default."""
    f = tmp_path / "greet.txt"
    f.write_text("hello world, hello again", encoding="utf-8")

    result = repo_patch(
        "greet.txt",
        [{"type": "replace", "old": "hello", "new": "hi"}],
        _sandbox=tmp_path,
    )

    assert result["ok"] is True
    assert result["operations_applied"] == 1
    assert result["verified"] is True
    assert f.read_text(encoding="utf-8") == "hi world, hi again"


# --- Patch: replace count=1 -------------------------------------------------


def test_patch_replace_count_one(tmp_path):
    """Replace with count=1 only substitutes the first occurrence."""
    f = tmp_path / "multi.txt"
    f.write_text("aaa bbb aaa bbb aaa", encoding="utf-8")

    result = repo_patch(
        "multi.txt",
        [{"type": "replace", "old": "aaa", "new": "xxx", "count": 1}],
        _sandbox=tmp_path,
    )

    assert result["ok"] is True
    assert result["operations_applied"] == 1
    assert f.read_text(encoding="utf-8") == "xxx bbb aaa bbb aaa"


# --- Patch: replace missing text => ok=false --------------------------------


def test_patch_replace_missing_text(tmp_path):
    """Replace returns ok=False when old text is not found."""
    f = tmp_path / "nope.txt"
    f.write_text("nothing here", encoding="utf-8")

    result = repo_patch(
        "nope.txt",
        [{"type": "replace", "old": "missing", "new": "found"}],
        _sandbox=tmp_path,
    )

    assert result["ok"] is False
    assert "not found" in result["message"].lower()
    assert result["operations_applied"] == 0
    # File should be unchanged
    assert f.read_text(encoding="utf-8") == "nothing here"


# --- Patch: append success ---------------------------------------------------


def test_patch_append_success(tmp_path):
    """Append operation adds text to end of file."""
    f = tmp_path / "log.txt"
    f.write_text("line1\n", encoding="utf-8")

    result = repo_patch(
        "log.txt",
        [{"type": "append", "text": "line2\n"}],
        _sandbox=tmp_path,
    )

    assert result["ok"] is True
    assert result["operations_applied"] == 1
    assert result["verified"] is True
    assert f.read_text(encoding="utf-8") == "line1\nline2\n"


# --- Patch: multiple operations in sequence ----------------------------------


def test_patch_multiple_operations(tmp_path):
    """Multiple operations are applied in order."""
    f = tmp_path / "combo.txt"
    f.write_text("foo bar", encoding="utf-8")

    result = repo_patch(
        "combo.txt",
        [
            {"type": "replace", "old": "foo", "new": "baz"},
            {"type": "append", "text": " qux"},
        ],
        _sandbox=tmp_path,
    )

    assert result["ok"] is True
    assert result["operations_applied"] == 2
    assert result["verified"] is True
    assert f.read_text(encoding="utf-8") == "baz bar qux"


# --- Patch: create_if_missing=True ------------------------------------------


def test_patch_create_if_missing_true(tmp_path):
    """When file doesn't exist and create_if_missing=True, create it."""
    result = repo_patch(
        "brand_new.txt",
        [{"type": "append", "text": "fresh content"}],
        create_if_missing=True,
        _sandbox=tmp_path,
    )

    assert result["ok"] is True
    assert result["created"] is True
    assert result["operations_applied"] == 1
    assert result["verified"] is True
    assert (tmp_path / "brand_new.txt").read_text(encoding="utf-8") == "fresh content"


# --- Patch: create_if_missing=False on missing file --------------------------


def test_patch_create_if_missing_false(tmp_path):
    """When file doesn't exist and create_if_missing=False, return ok=False."""
    result = repo_patch(
        "ghost.txt",
        [{"type": "append", "text": "nope"}],
        create_if_missing=False,
        _sandbox=tmp_path,
    )

    assert result["ok"] is False
    assert "not found" in result["message"].lower()
    assert result["created"] is False
    assert not (tmp_path / "ghost.txt").exists()


# --- Patch: rejects traversal / outside repo --------------------------------


def test_patch_rejects_dotdot_traversal(tmp_path):
    """Paths with ../ that escape sandbox return ok=False."""
    result = repo_patch(
        "../../../etc/passwd",
        [{"type": "append", "text": "evil"}],
        _sandbox=tmp_path,
    )

    assert result["ok"] is False
    assert "escapes sandbox" in result["message"].lower()


def test_patch_rejects_absolute_outside(tmp_path):
    """Absolute paths outside sandbox return ok=False."""
    result = repo_patch(
        "/etc/passwd",
        [{"type": "append", "text": "evil"}],
        _sandbox=tmp_path,
    )

    assert result["ok"] is False
    assert "escapes sandbox" in result["message"].lower()


# --- Patch: runner dispatch integration --------------------------------------


def test_patch_runner_dispatch(tmp_path):
    """repo.files.patch is callable through run_tool."""
    from tools.runner import run_tool
    from tools.registry import load_registry

    f = tmp_path / "runner_patch.txt"
    f.write_text("old value", encoding="utf-8")

    registry = load_registry()
    registry["sandbox_root"] = str(tmp_path)

    envelope = run_tool(
        "repo.files.patch",
        {
            "path": "runner_patch.txt",
            "operations": [{"type": "replace", "old": "old", "new": "new"}],
        },
        registry=registry,
    )

    assert envelope["tool"] == "repo.files.patch"
    assert envelope["ok"] is True
    result = envelope["result"]
    assert result["operations_applied"] == 1
    assert result["verified"] is True
    assert f.read_text(encoding="utf-8") == "new value"


# --- Patch: normalized relative path output ----------------------------------


def test_patch_normalized_path(tmp_path):
    """Output path is always repo-relative, never absolute."""
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    f = sub / "file.txt"
    f.write_text("content", encoding="utf-8")

    result = repo_patch(
        "a/b/../b/file.txt",
        [{"type": "replace", "old": "content", "new": "patched"}],
        _sandbox=tmp_path,
    )

    assert result["ok"] is True
    assert result["path"] == "a/b/file.txt"
    assert not result["path"].startswith("/")


# --- Patch: verification after patch ----------------------------------------


def test_patch_verification(tmp_path):
    """Verify that patched content matches after write."""
    content = "Unicode: \u00e9\u00e8\u00ea \u2603"
    f = tmp_path / "unicode_patch.txt"
    f.write_text(content, encoding="utf-8")

    result = repo_patch(
        "unicode_patch.txt",
        [{"type": "append", "text": " \U0001f600"}],
        _sandbox=tmp_path,
    )

    assert result["ok"] is True
    assert result["verified"] is True
    assert f.read_text(encoding="utf-8") == content + " \U0001f600"


# --- Patch: JSON output shape -----------------------------------------------


def test_patch_json_output_shape(tmp_path):
    """Patch result is JSON-serializable with expected keys."""
    f = tmp_path / "shape_patch.txt"
    f.write_text("test", encoding="utf-8")

    result = repo_patch(
        "shape_patch.txt",
        [{"type": "append", "text": "!"}],
        _sandbox=tmp_path,
    )

    serialized = json.dumps(result)
    parsed = json.loads(serialized)

    expected_keys = {"ok", "path", "operations_applied", "created", "verified", "message"}
    assert set(parsed.keys()) == expected_keys
    assert isinstance(parsed["ok"], bool)
    assert isinstance(parsed["path"], str)
    assert isinstance(parsed["operations_applied"], int)
    assert isinstance(parsed["created"], bool)
    assert isinstance(parsed["verified"], bool)
    assert isinstance(parsed["message"], str)
    assert not parsed["path"].startswith("/")


# --- Patch: unsupported operation type ---------------------------------------


def test_patch_unsupported_operation(tmp_path):
    """Unsupported operation type returns ok=False."""
    f = tmp_path / "ops.txt"
    f.write_text("data", encoding="utf-8")

    result = repo_patch(
        "ops.txt",
        [{"type": "delete", "line": 1}],
        _sandbox=tmp_path,
    )

    assert result["ok"] is False
    assert "unsupported" in result["message"].lower()
