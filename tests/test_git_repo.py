"""Tests for tools.adapters.git_repo — parse_porcelain + git_status.

These tests mock subprocess output so they work in any environment.
"""

from unittest.mock import patch

from tools.adapters.git_repo import parse_porcelain, git_status


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
