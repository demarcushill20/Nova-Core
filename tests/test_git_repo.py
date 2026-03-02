"""Tests for tools.adapters.git_repo — status + diff adapters.

These tests mock subprocess output so they work in any environment.
"""

from unittest.mock import patch

from tools.adapters.git_repo import (
    git_diff,
    git_status,
    parse_diff,
    parse_porcelain,
)


# --- Porcelain output samples ------------------------------------------------

CLEAN_REPO = """\
## main...origin/main
"""

MODIFIED_FILES = """\
## main...origin/main
 M tools/runner.py
 M tools/skills.py
"""

STAGED_AND_UNTRACKED = """\
## feature/new-skill...origin/feature/new-skill
A  tools/adapters/new_tool.py
M  tools/runner.py
?? tests/test_new_tool.py
?? scratch.txt
"""

AHEAD_BEHIND = """\
## main...origin/main [ahead 3, behind 1]
 M README.md
"""

AHEAD_ONLY = """\
## main...origin/main [ahead 2]
"""

NO_REMOTE = """\
## dev
A  newfile.py
"""

MIXED_STAGED_AND_MODIFIED = """\
## main...origin/main
MM tools/runner.py
A  tools/adapters/new.py
 M watcher.py
?? tmp.log
"""


# --- Tests for parse_porcelain -----------------------------------------------


def test_clean_repo():
    result = parse_porcelain(CLEAN_REPO)
    assert result["branch"] == "main"
    assert result["remote"] == "origin/main"
    assert result["ahead"] == 0
    assert result["behind"] == 0
    assert result["staged"] == []
    assert result["modified"] == []
    assert result["untracked"] == []
    assert result["clean"] is True


def test_modified_files():
    result = parse_porcelain(MODIFIED_FILES)
    assert result["branch"] == "main"
    assert result["clean"] is False
    assert len(result["modified"]) == 2
    assert result["modified"][0] == {"status": "M", "path": "tools/runner.py"}
    assert result["modified"][1] == {"status": "M", "path": "tools/skills.py"}
    assert result["staged"] == []
    assert result["untracked"] == []


def test_staged_and_untracked():
    result = parse_porcelain(STAGED_AND_UNTRACKED)
    assert result["branch"] == "feature/new-skill"
    assert result["remote"] == "origin/feature/new-skill"
    assert len(result["staged"]) == 2
    assert result["staged"][0] == {"status": "A", "path": "tools/adapters/new_tool.py"}
    assert result["staged"][1] == {"status": "M", "path": "tools/runner.py"}
    assert len(result["untracked"]) == 2
    assert "tests/test_new_tool.py" in result["untracked"]
    assert "scratch.txt" in result["untracked"]
    assert result["clean"] is False


def test_ahead_behind():
    result = parse_porcelain(AHEAD_BEHIND)
    assert result["ahead"] == 3
    assert result["behind"] == 1
    assert len(result["modified"]) == 1


def test_ahead_only():
    result = parse_porcelain(AHEAD_ONLY)
    assert result["ahead"] == 2
    assert result["behind"] == 0
    assert result["clean"] is True


def test_no_remote():
    result = parse_porcelain(NO_REMOTE)
    assert result["branch"] == "dev"
    assert result["remote"] == ""
    assert result["ahead"] == 0
    assert result["behind"] == 0
    assert len(result["staged"]) == 1


def test_mixed_staged_and_modified():
    result = parse_porcelain(MIXED_STAGED_AND_MODIFIED)
    # MM means staged AND modified (different changes)
    assert any(f["path"] == "tools/runner.py" for f in result["staged"])
    assert any(f["path"] == "tools/runner.py" for f in result["modified"])
    assert any(f["path"] == "tools/adapters/new.py" for f in result["staged"])
    assert any(f["path"] == "watcher.py" for f in result["modified"])
    assert "tmp.log" in result["untracked"]
    assert result["clean"] is False


def test_empty_output():
    result = parse_porcelain("")
    assert result["branch"] == ""
    assert result["clean"] is True


# --- Tests for git_status (mocked subprocess) --------------------------------


@patch("tools.adapters.git_repo.run_subprocess")
def test_git_status_clean(mock_run):
    mock_run.return_value = {"exit_code": 0, "stdout": CLEAN_REPO, "stderr": ""}
    result = git_status()
    assert result["ok"] is True
    assert result["branch"] == "main"
    assert result["clean"] is True

    call_args = mock_run.call_args
    assert call_args[0][0] == ["git", "status", "--porcelain=v1", "-b"]


@patch("tools.adapters.git_repo.run_subprocess")
def test_git_status_dirty(mock_run):
    mock_run.return_value = {"exit_code": 0, "stdout": STAGED_AND_UNTRACKED, "stderr": ""}
    result = git_status()
    assert result["ok"] is True
    assert result["clean"] is False
    assert len(result["staged"]) == 2
    assert len(result["untracked"]) == 2


@patch("tools.adapters.git_repo.run_subprocess")
def test_git_status_not_a_repo(mock_run):
    mock_run.return_value = {
        "exit_code": 128,
        "stdout": "",
        "stderr": "fatal: not a git repository",
    }
    result = git_status()
    assert result["ok"] is False
    assert result["exit_code"] == 128
    assert "not a git repository" in result["stderr"]


@patch("tools.adapters.git_repo.run_subprocess")
def test_git_status_json_shape(mock_run):
    mock_run.return_value = {"exit_code": 0, "stdout": AHEAD_BEHIND, "stderr": ""}
    result = git_status()
    required_keys = {
        "ok", "exit_code", "stderr", "branch", "remote",
        "ahead", "behind", "staged", "modified", "untracked", "clean",
    }
    assert required_keys.issubset(set(result.keys())), f"Missing: {required_keys - set(result.keys())}"
    assert isinstance(result["clean"], bool)
    assert isinstance(result["ahead"], int)
    assert isinstance(result["staged"], list)


# --- Diff output samples -----------------------------------------------------

SINGLE_FILE_DIFF = """\
diff --git a/tools/runner.py b/tools/runner.py
index 466a0a1..b7eaba5 100644
--- a/tools/runner.py
+++ b/tools/runner.py
@@ -220,6 +220,8 @@ def run_tool(
             result = _run_shell(args, sandbox)
         elif tool_name == "git.run":
             result = _run_git(args, sandbox)
+        elif tool_name == "repo.git.diff":
+            result = _run_repo_git_diff(args, sandbox)
         elif tool_name.startswith("files."):
             result = _run_files(tool_name, args, registry)
"""

MULTI_FILE_DIFF = """\
diff --git a/README.md b/README.md
index abc1234..def5678 100644
--- a/README.md
+++ b/README.md
@@ -1,3 +1,5 @@
 # NovaCore
+## Overview
+NovaCore is an autonomous AI runtime.

-Old description here.
+New description here.
diff --git a/tools/runner.py b/tools/runner.py
index 111aaaa..222bbbb 100644
--- a/tools/runner.py
+++ b/tools/runner.py
@@ -10,7 +10,7 @@ import json
-_MAX_OUTPUT = 100 * 1024
+_MAX_OUTPUT = 200 * 1024
"""

NO_CHANGES_DIFF = ""

LARGE_FILE_DIFF = "diff --git a/big.py b/big.py\nindex aaa..bbb 100644\n--- a/big.py\n+++ b/big.py\n@@ -1,5 +1,30 @@\n" + "\n".join(
    f"+line {i}" for i in range(1, 51)
)


# --- Tests for parse_diff ---------------------------------------------------


def test_parse_single_file():
    result = parse_diff(SINGLE_FILE_DIFF)
    assert result["total_files"] == 1
    assert result["files"][0]["path"] == "tools/runner.py"
    assert result["files"][0]["additions"] == 2
    assert result["files"][0]["deletions"] == 0
    assert result["total_additions"] == 2
    assert result["total_deletions"] == 0
    assert result["empty"] is False


def test_parse_multi_file():
    result = parse_diff(MULTI_FILE_DIFF)
    assert result["total_files"] == 2
    paths = [f["path"] for f in result["files"]]
    assert "README.md" in paths
    assert "tools/runner.py" in paths
    # README: +3 additions (Overview, description line, New description), -1 deletion (Old description)
    readme = next(f for f in result["files"] if f["path"] == "README.md")
    assert readme["additions"] == 3
    assert readme["deletions"] == 1
    # runner: +1 addition, -1 deletion
    runner = next(f for f in result["files"] if f["path"] == "tools/runner.py")
    assert runner["additions"] == 1
    assert runner["deletions"] == 1
    assert result["total_additions"] == 4
    assert result["total_deletions"] == 2


def test_parse_no_changes():
    result = parse_diff(NO_CHANGES_DIFF)
    assert result["total_files"] == 0
    assert result["files"] == []
    assert result["empty"] is True


def test_parse_excerpt_truncation():
    result = parse_diff(LARGE_FILE_DIFF)
    assert result["total_files"] == 1
    assert result["files"][0]["additions"] == 50
    # Excerpt should be limited to ~20 lines
    excerpt_lines = result["files"][0]["excerpt"].splitlines()
    assert len(excerpt_lines) == 20


# --- Tests for git_diff (mocked subprocess) ----------------------------------


@patch("tools.adapters.git_repo.run_subprocess")
def test_git_diff_full(mock_run):
    mock_run.return_value = {"exit_code": 0, "stdout": MULTI_FILE_DIFF, "stderr": ""}
    result = git_diff()
    assert result["ok"] is True
    assert result["total_files"] == 2
    assert result["empty"] is False
    call_args = mock_run.call_args
    assert call_args[0][0] == ["git", "diff", "--unified=3"]


@patch("tools.adapters.git_repo.run_subprocess")
def test_git_diff_scoped_path(mock_run):
    mock_run.return_value = {"exit_code": 0, "stdout": SINGLE_FILE_DIFF, "stderr": ""}
    result = git_diff(path="tools/runner.py")
    assert result["ok"] is True
    assert result["total_files"] == 1
    call_args = mock_run.call_args
    assert call_args[0][0] == ["git", "diff", "--unified=3", "--", "tools/runner.py"]


@patch("tools.adapters.git_repo.run_subprocess")
def test_git_diff_empty(mock_run):
    mock_run.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
    result = git_diff()
    assert result["ok"] is True
    assert result["empty"] is True
    assert result["total_files"] == 0


def test_git_diff_rejects_flag_path():
    try:
        git_diff(path="--staged")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "flag" in str(e).lower()


@patch("tools.adapters.git_repo.run_subprocess")
def test_git_diff_json_shape(mock_run):
    mock_run.return_value = {"exit_code": 0, "stdout": SINGLE_FILE_DIFF, "stderr": ""}
    result = git_diff()
    required_keys = {"ok", "exit_code", "stderr", "files", "total_files", "total_additions", "total_deletions", "empty"}
    assert required_keys.issubset(set(result.keys())), f"Missing: {required_keys - set(result.keys())}"
    assert isinstance(result["files"], list)
    assert isinstance(result["empty"], bool)
    assert isinstance(result["total_additions"], int)
    # Verify file object shape
    f = result["files"][0]
    assert "path" in f
    assert "additions" in f
    assert "deletions" in f
    assert "excerpt" in f


# --- Run as script -----------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_clean_repo,
        test_modified_files,
        test_staged_and_untracked,
        test_ahead_behind,
        test_ahead_only,
        test_no_remote,
        test_mixed_staged_and_modified,
        test_empty_output,
        test_git_status_clean,
        test_git_status_dirty,
        test_git_status_not_a_repo,
        test_git_status_json_shape,
        test_parse_single_file,
        test_parse_multi_file,
        test_parse_no_changes,
        test_parse_excerpt_truncation,
        test_git_diff_full,
        test_git_diff_scoped_path,
        test_git_diff_empty,
        test_git_diff_rejects_flag_path,
        test_git_diff_json_shape,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
