"""Tests for repo.diff adapter.

Validates:
- Unchanged file returns ok=True, changed=False
- Changed file returns ok=True, changed=True with diff text
- Invalid (empty) path returns ok=False
- Traversal rejection (../ escaping sandbox)
- Non-file rejection (directory, missing file)
- Flag injection rejection in path and against
- Runner dispatch integration
- JSON output shape
- Safe handling of optional 'against' parameter
- Unsafe 'against' ref rejection
"""

import json
import subprocess
from pathlib import Path

import pytest

from tools.adapters.repo_diff import repo_diff


def _init_git_repo(tmp_path: Path) -> Path:
    """Initialise a minimal git repo in tmp_path and return the root."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path), capture_output=True, check=True,
    )
    return tmp_path


# --- Unchanged file ----------------------------------------------------------


def test_unchanged_file(tmp_path):
    """Committed file with no working-tree changes shows changed=False."""
    repo = _init_git_repo(tmp_path)
    f = repo / "stable.txt"
    f.write_text("stable content\n", encoding="utf-8")
    subprocess.run(["git", "add", "stable.txt"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add stable"], cwd=str(repo), capture_output=True, check=True)

    result = repo_diff("stable.txt", _sandbox=repo)

    assert result["ok"] is True
    assert result["changed"] is False
    assert result["diff"] == ""
    assert result["summary"]["lines_added"] == 0
    assert result["summary"]["lines_removed"] == 0
    assert result["path"] == "stable.txt"


# --- Changed file ------------------------------------------------------------


def test_changed_file(tmp_path):
    """Modified working-tree file shows changed=True with diff text."""
    repo = _init_git_repo(tmp_path)
    f = repo / "mutable.txt"
    f.write_text("line1\nline2\n", encoding="utf-8")
    subprocess.run(["git", "add", "mutable.txt"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add mutable"], cwd=str(repo), capture_output=True, check=True)

    # Modify the file
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")

    result = repo_diff("mutable.txt", _sandbox=repo)

    assert result["ok"] is True
    assert result["changed"] is True
    assert "+line3" in result["diff"]
    assert result["summary"]["lines_added"] >= 1
    assert result["path"] == "mutable.txt"


# --- Invalid path ------------------------------------------------------------


def test_empty_path(tmp_path):
    """Empty path returns ok=False."""
    result = repo_diff("", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "path is required" in result.get("error", "").lower()


def test_none_path(tmp_path):
    """None path returns ok=False."""
    result = repo_diff(None, _sandbox=tmp_path)

    assert result["ok"] is False
    assert "path is required" in result.get("error", "").lower()


# --- Traversal rejection ----------------------------------------------------


def test_rejects_dotdot_traversal(tmp_path):
    """Paths with ../ that escape sandbox are rejected."""
    _init_git_repo(tmp_path)
    result = repo_diff("../../../etc/passwd", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "escapes sandbox" in result.get("error", "").lower()


def test_rejects_absolute_outside(tmp_path):
    """Absolute paths outside sandbox are rejected."""
    _init_git_repo(tmp_path)
    result = repo_diff("/etc/passwd", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "escapes sandbox" in result.get("error", "").lower()


# --- Non-file rejection -----------------------------------------------------


def test_rejects_directory(tmp_path):
    """Directories return ok=False."""
    _init_git_repo(tmp_path)
    sub = tmp_path / "subdir"
    sub.mkdir()

    result = repo_diff("subdir", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "not a file" in result.get("error", "").lower()


def test_rejects_missing_file(tmp_path):
    """Non-existent file returns ok=False."""
    _init_git_repo(tmp_path)
    result = repo_diff("ghost.txt", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "not a file" in result.get("error", "").lower()


# --- Flag injection ----------------------------------------------------------


def test_rejects_flag_in_path(tmp_path):
    """Path starting with - is rejected as flag injection."""
    _init_git_repo(tmp_path)
    result = repo_diff("--staged", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "flag injection" in result.get("error", "").lower()


def test_rejects_flag_in_against(tmp_path):
    """Against starting with - is rejected as flag injection."""
    repo = _init_git_repo(tmp_path)
    f = repo / "file.txt"
    f.write_text("content\n", encoding="utf-8")

    result = repo_diff("file.txt", against="--staged", _sandbox=repo)

    assert result["ok"] is False
    assert "flag injection" in result.get("error", "").lower()


# --- Against parameter (safe ref) -------------------------------------------


def test_diff_against_head(tmp_path):
    """Diffing against HEAD works for a modified file."""
    repo = _init_git_repo(tmp_path)
    f = repo / "ref.txt"
    f.write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "ref.txt"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=str(repo), capture_output=True, check=True)

    f.write_text("v2\n", encoding="utf-8")

    result = repo_diff("ref.txt", against="HEAD", _sandbox=repo)

    assert result["ok"] is True
    assert result["changed"] is True
    assert "+v2" in result["diff"]


def test_diff_against_unchanged(tmp_path):
    """Diffing against HEAD with no changes shows changed=False."""
    repo = _init_git_repo(tmp_path)
    f = repo / "same.txt"
    f.write_text("unchanged\n", encoding="utf-8")
    subprocess.run(["git", "add", "same.txt"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "same"], cwd=str(repo), capture_output=True, check=True)

    result = repo_diff("same.txt", against="HEAD", _sandbox=repo)

    assert result["ok"] is True
    assert result["changed"] is False


# --- Unsafe against ref rejection -------------------------------------------


def test_rejects_unsafe_against_characters(tmp_path):
    """Against with shell metacharacters is rejected."""
    repo = _init_git_repo(tmp_path)
    f = repo / "safe.txt"
    f.write_text("content\n", encoding="utf-8")

    result = repo_diff("safe.txt", against="HEAD; rm -rf /", _sandbox=repo)

    assert result["ok"] is False
    assert "unsafe characters" in result.get("error", "").lower()


def test_rejects_empty_against(tmp_path):
    """Empty against string is rejected."""
    repo = _init_git_repo(tmp_path)
    f = repo / "file.txt"
    f.write_text("content\n", encoding="utf-8")

    result = repo_diff("file.txt", against="", _sandbox=repo)

    assert result["ok"] is False
    assert "non-empty" in result.get("error", "").lower()


# --- Runner dispatch integration --------------------------------------------


def test_runner_dispatch(tmp_path):
    """repo.diff is callable through run_tool."""
    from tools.runner import run_tool
    from tools.registry import load_registry

    repo = _init_git_repo(tmp_path)
    f = repo / "runner_test.txt"
    f.write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "runner_test.txt"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True)

    f.write_text("modified\n", encoding="utf-8")

    registry = load_registry()
    registry["sandbox_root"] = str(repo)

    envelope = run_tool(
        "repo.diff",
        {"path": "runner_test.txt"},
        registry=registry,
    )

    assert envelope["tool"] == "repo.diff"
    assert envelope["ok"] is True
    result = envelope["result"]
    assert result["changed"] is True
    assert "+modified" in result["diff"]


# --- JSON output shape -------------------------------------------------------


def test_json_output_shape(tmp_path):
    """Result is JSON-serializable with expected keys."""
    repo = _init_git_repo(tmp_path)
    f = repo / "shape.txt"
    f.write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "shape.txt"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "shape"], cwd=str(repo), capture_output=True, check=True)

    f.write_text("test\nline2\n", encoding="utf-8")

    result = repo_diff("shape.txt", _sandbox=repo)

    # Must be JSON-serializable
    serialized = json.dumps(result)
    parsed = json.loads(serialized)

    expected_keys = {"ok", "path", "changed", "diff", "summary"}
    assert set(parsed.keys()) == expected_keys
    assert isinstance(parsed["ok"], bool)
    assert isinstance(parsed["path"], str)
    assert isinstance(parsed["changed"], bool)
    assert isinstance(parsed["diff"], str)
    assert isinstance(parsed["summary"], dict)
    assert "lines_added" in parsed["summary"]
    assert "lines_removed" in parsed["summary"]
    assert isinstance(parsed["summary"]["lines_added"], int)
    assert isinstance(parsed["summary"]["lines_removed"], int)
    # path must be repo-relative
    assert not parsed["path"].startswith("/")


# --- Summary counts ----------------------------------------------------------


def test_summary_counts_accurate(tmp_path):
    """Summary lines_added and lines_removed reflect actual changes."""
    repo = _init_git_repo(tmp_path)
    f = repo / "count.txt"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    subprocess.run(["git", "add", "count.txt"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "count"], cwd=str(repo), capture_output=True, check=True)

    # Remove beta, add delta and epsilon
    f.write_text("alpha\ndelta\nepsilon\ngamma\n", encoding="utf-8")

    result = repo_diff("count.txt", _sandbox=repo)

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["summary"]["lines_added"] >= 2
    assert result["summary"]["lines_removed"] >= 1
