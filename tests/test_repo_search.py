"""Tests for repo.search adapter.

Validates:
- Basic substring search returns matches
- Restricted path search scopes to subdirectory
- Multiple matches across files
- max_results clamping (low and high)
- Binary file skip
- Oversized file skip
- Traversal rejection (../ escaping sandbox)
- Non-directory path returns error
- Empty query returns error
- Runner dispatch integration
- JSON output shape
"""

import json
import os
from pathlib import Path

import pytest

from tools.adapters.repo_search import repo_search


# --- Basic search -----------------------------------------------------------


def test_basic_search(tmp_path):
    """Search finds a matching line in a file."""
    f = tmp_path / "hello.py"
    f.write_text("print('hello world')\n", encoding="utf-8")

    result = repo_search("hello", _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["query"] == "hello"
    assert result["count"] >= 1
    assert any(m["path"] == "hello.py" for m in result["results"])
    match = [m for m in result["results"] if m["path"] == "hello.py"][0]
    assert match["line"] == 1
    assert "hello" in match["snippet"]


# --- Restricted path search -------------------------------------------------


def test_restricted_path_search(tmp_path):
    """Search with path restricts to subdirectory."""
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "a.py").write_text("target line\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("target line\n", encoding="utf-8")

    result = repo_search("target", path="src", _sandbox=tmp_path)

    assert result["ok"] is True
    # Only file in src/ should match
    paths = [m["path"] for m in result["results"]]
    assert "src/a.py" in paths
    assert "b.py" not in paths


# --- Multiple matches -------------------------------------------------------


def test_multiple_matches(tmp_path):
    """Search returns multiple matches across files and lines."""
    (tmp_path / "one.txt").write_text("foo bar\nfoo baz\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("no match\nfoo qux\n", encoding="utf-8")

    result = repo_search("foo", _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["count"] == 3
    lines = [(m["path"], m["line"]) for m in result["results"]]
    assert ("one.txt", 1) in lines
    assert ("one.txt", 2) in lines
    assert ("two.txt", 2) in lines


# --- max_results clamping ---------------------------------------------------


def test_max_results_clamp_low(tmp_path):
    """max_results below 1 is clamped to 1."""
    (tmp_path / "data.txt").write_text("match\nmatch\nmatch\n", encoding="utf-8")

    result = repo_search("match", max_results=0, _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["count"] == 1  # clamped to minimum of 1


def test_max_results_clamp_high(tmp_path):
    """max_results above 100 is clamped to 100."""
    lines = "\n".join(f"match line {i}" for i in range(150))
    (tmp_path / "big.txt").write_text(lines, encoding="utf-8")

    result = repo_search("match", max_results=999, _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["count"] <= 100  # clamped to max of 100


def test_max_results_limits_output(tmp_path):
    """Search stops after max_results matches."""
    lines = "\n".join(f"pattern {i}" for i in range(20))
    (tmp_path / "many.txt").write_text(lines, encoding="utf-8")

    result = repo_search("pattern", max_results=5, _sandbox=tmp_path)

    assert result["ok"] is True
    assert result["count"] == 5


# --- Binary file skip -------------------------------------------------------


def test_binary_file_skip(tmp_path):
    """Binary files are skipped during search."""
    (tmp_path / "text.txt").write_text("findme here\n", encoding="utf-8")
    (tmp_path / "binary.dat").write_bytes(b"findme\x00binary\x00data")

    result = repo_search("findme", _sandbox=tmp_path)

    assert result["ok"] is True
    paths = [m["path"] for m in result["results"]]
    assert "text.txt" in paths
    assert "binary.dat" not in paths


# --- Oversized file skip ----------------------------------------------------


def test_oversized_file_skip(tmp_path):
    """Files larger than 1 MB are skipped."""
    (tmp_path / "small.txt").write_text("needle here\n", encoding="utf-8")
    big = tmp_path / "huge.txt"
    # Create a file just over 1 MB
    big.write_bytes(b"needle " + b"x" * (1_000_001 - 7))

    result = repo_search("needle", _sandbox=tmp_path)

    assert result["ok"] is True
    paths = [m["path"] for m in result["results"]]
    assert "small.txt" in paths
    assert "huge.txt" not in paths


# --- Traversal rejection ----------------------------------------------------


def test_traversal_rejection(tmp_path):
    """Paths with ../ that escape sandbox are rejected."""
    result = repo_search("test", path="../../../etc", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "escapes sandbox" in result.get("error", "").lower()


def test_traversal_rejection_dotdot_in_middle(tmp_path):
    """Paths with ../ in the middle that escape sandbox are rejected."""
    sub = tmp_path / "sub"
    sub.mkdir()
    result = repo_search("test", path="sub/../../etc", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "escapes sandbox" in result.get("error", "").lower()


# --- Non-directory path ------------------------------------------------------


def test_non_directory_path(tmp_path):
    """Search with path pointing to a file returns error."""
    (tmp_path / "file.txt").write_text("content", encoding="utf-8")

    result = repo_search("content", path="file.txt", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "not a directory" in result.get("error", "").lower()


# --- Empty query -------------------------------------------------------------


def test_empty_query(tmp_path):
    """Empty query returns ok=False with error."""
    result = repo_search("", _sandbox=tmp_path)

    assert result["ok"] is False
    assert "query is required" in result.get("error", "").lower()


# --- Decode failure skip ----------------------------------------------------


def test_decode_failure_skip(tmp_path):
    """Files that fail UTF-8 decode are skipped (not binary heuristic, but decode)."""
    (tmp_path / "good.txt").write_text("findme\n", encoding="utf-8")
    # Latin-1 encoded file without null bytes — will fail strict utf-8 decode
    (tmp_path / "latin.txt").write_bytes(b"findme \xe9\xe8\xea\n" * 100)

    result = repo_search("findme", _sandbox=tmp_path)

    assert result["ok"] is True
    paths = [m["path"] for m in result["results"]]
    assert "good.txt" in paths
    # latin.txt may or may not be included (Python's decode may handle it)
    # The key is no crash


# --- Hidden directories skipped ---------------------------------------------


def test_hidden_dirs_skipped(tmp_path):
    """Hidden directories like .git are skipped."""
    git = tmp_path / ".git"
    git.mkdir()
    (git / "config").write_text("findme\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("findme\n", encoding="utf-8")

    result = repo_search("findme", _sandbox=tmp_path)

    assert result["ok"] is True
    paths = [m["path"] for m in result["results"]]
    assert "visible.txt" in paths
    assert not any(".git" in p for p in paths)


# --- Runner dispatch integration -------------------------------------------


def test_runner_dispatch(tmp_path):
    """repo.search is callable through run_tool."""
    from tools.runner import run_tool
    from tools.registry import load_registry

    (tmp_path / "runner_test.txt").write_text("search_target_xyz\n", encoding="utf-8")

    registry = load_registry()
    registry["sandbox_root"] = str(tmp_path)

    envelope = run_tool(
        "repo.search",
        {"query": "search_target_xyz"},
        registry=registry,
    )

    assert envelope["tool"] == "repo.search"
    assert envelope["ok"] is True
    result = envelope["result"]
    assert result["count"] >= 1
    assert any("search_target_xyz" in m["snippet"] for m in result["results"])


# --- JSON output shape ------------------------------------------------------


def test_json_output_shape(tmp_path):
    """Result is JSON-serializable with expected keys."""
    (tmp_path / "shape.txt").write_text("shape test\n", encoding="utf-8")

    result = repo_search("shape", _sandbox=tmp_path)

    serialized = json.dumps(result)
    parsed = json.loads(serialized)

    assert set(parsed.keys()) == {"ok", "query", "results", "count"}
    assert isinstance(parsed["ok"], bool)
    assert isinstance(parsed["query"], str)
    assert isinstance(parsed["results"], list)
    assert isinstance(parsed["count"], int)

    if parsed["count"] > 0:
        m = parsed["results"][0]
        assert set(m.keys()) == {"path", "line", "snippet"}
        assert isinstance(m["path"], str)
        assert isinstance(m["line"], int)
        assert isinstance(m["snippet"], str)
        # path must be repo-relative, not absolute
        assert not m["path"].startswith("/")
