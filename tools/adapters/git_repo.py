"""Adapters: repo.git.status / repo.git.diff / repo.git.commit

Return dicts with parsed fields instead of raw stdout.
Delegate execution to the existing runner infrastructure.
"""

import re
from pathlib import Path

from tools.runner import run_subprocess

# --- Parsing helpers --------------------------------------------------------

# ## main...origin/main [ahead 2, behind 1]
_BRANCH_RE = re.compile(
    r"^## (?P<branch>[^\s.]+)"
    r"(?:\.\.\.(?P<remote>[^\s\[]+))?"
    r"(?:\s*\[(?P<diverge>[^\]]+)\])?"
)
_AHEAD_RE = re.compile(r"ahead (\d+)")
_BEHIND_RE = re.compile(r"behind (\d+)")


def parse_porcelain(output: str) -> dict:
    """Parse `git status --porcelain=v1 -b` output into structured fields."""
    branch = ""
    remote = ""
    ahead = 0
    behind = 0
    staged = []
    modified = []
    untracked = []

    for line in output.splitlines():
        if line.startswith("## "):
            m = _BRANCH_RE.match(line)
            if m:
                branch = m.group("branch") or ""
                remote = m.group("remote") or ""
                diverge = m.group("diverge") or ""
                am = _AHEAD_RE.search(diverge)
                if am:
                    ahead = int(am.group(1))
                bm = _BEHIND_RE.search(diverge)
                if bm:
                    behind = int(bm.group(1))
            continue

        if len(line) < 4:
            continue

        x = line[0]  # index (staged) status
        y = line[1]  # worktree status
        filepath = line[3:]

        if x == "?" and y == "?":
            untracked.append(filepath)
        else:
            if x not in (" ", "?"):
                staged.append({"status": x, "path": filepath})
            if y not in (" ", "?"):
                modified.append({"status": y, "path": filepath})

    clean = not staged and not modified and not untracked

    return {
        "branch": branch,
        "remote": remote,
        "ahead": ahead,
        "behind": behind,
        "staged": staged,
        "modified": modified,
        "untracked": untracked,
        "clean": clean,
    }


def git_status(sandbox: Path | None = None) -> dict:
    """Get structured Git status for the repository.

    Args:
        sandbox: working directory (defaults to cwd)

    Returns:
        dict with keys: branch, remote, ahead, behind, staged,
        modified, untracked, clean
    """
    cwd = sandbox or Path.cwd()
    result = run_subprocess(
        ["git", "status", "--porcelain=v1", "-b"],
        cwd=cwd,
        timeout=10,
    )

    if result["exit_code"] != 0:
        return {
            "ok": False,
            "exit_code": result["exit_code"],
            "stderr": result["stderr"],
            "branch": "",
            "remote": "",
            "ahead": 0,
            "behind": 0,
            "staged": [],
            "modified": [],
            "untracked": [],
            "clean": False,
        }

    parsed = parse_porcelain(result["stdout"])
    return {
        "ok": True,
        "exit_code": 0,
        "stderr": "",
        **parsed,
    }


# --- Diff parsing -----------------------------------------------------------

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
_EXCERPT_LINES = 20


def parse_diff(output: str) -> dict:
    """Parse `git diff --unified=3` output into structured per-file records."""
    files = []
    current = None

    for line in output.splitlines():
        hdr = _DIFF_HEADER_RE.match(line)
        if hdr:
            if current:
                files.append(current)
            current = {
                "path": hdr.group(2),
                "additions": 0,
                "deletions": 0,
                "excerpt": [],
            }
            continue

        if current is None:
            continue

        if len(current["excerpt"]) < _EXCERPT_LINES:
            current["excerpt"].append(line)

        if line.startswith("+") and not line.startswith("+++"):
            current["additions"] += 1
        elif line.startswith("-") and not line.startswith("---"):
            current["deletions"] += 1

    if current:
        files.append(current)

    # Convert excerpt lists to strings
    for f in files:
        f["excerpt"] = "\n".join(f["excerpt"])

    total_add = sum(f["additions"] for f in files)
    total_del = sum(f["deletions"] for f in files)

    return {
        "files": files,
        "total_files": len(files),
        "total_additions": total_add,
        "total_deletions": total_del,
        "empty": len(files) == 0,
    }


def git_diff(path: str | None = None, sandbox: Path | None = None) -> dict:
    """Get structured diff of the working tree.

    Args:
        path: optional file path to scope the diff
        sandbox: working directory (defaults to cwd)

    Returns:
        dict with keys: files, total_files, total_additions,
        total_deletions, empty
    """
    cwd = sandbox or Path.cwd()
    cmd = ["git", "diff", "--unified=3"]

    if path:
        # Sanitize: reject flags disguised as paths
        if path.startswith("-"):
            raise ValueError(f"Invalid path (looks like a flag): {path!r}")
        cmd.append("--")
        cmd.append(path)

    result = run_subprocess(cmd, cwd=cwd, timeout=15)

    if result["exit_code"] != 0:
        return {
            "ok": False,
            "exit_code": result["exit_code"],
            "stderr": result["stderr"],
            "files": [],
            "total_files": 0,
            "total_additions": 0,
            "total_deletions": 0,
            "empty": True,
        }

    parsed = parse_diff(result["stdout"])
    return {
        "ok": True,
        "exit_code": 0,
        "stderr": "",
        **parsed,
    }


# --- Commit -----------------------------------------------------------------

# Forbidden flags that must never appear in commit messages or paths
_COMMIT_FORBIDDEN_RE = re.compile(
    r"--amend|--no-verify|--force|--allow-empty|-a\b"
)


def git_commit(
    message: str,
    paths: list[str] | None = None,
    sandbox: Path | None = None,
) -> dict:
    """Create a git commit with audit discipline.

    Args:
        message: commit message (required, non-empty)
        paths: optional list of file paths to stage before committing
        sandbox: working directory (defaults to cwd)

    Returns:
        dict with keys: action, message, commit_hash, files, success,
        verification, and optionally reason
    """
    if not message or not isinstance(message, str):
        raise ValueError("repo.git.commit requires 'message' (non-empty str)")

    if _COMMIT_FORBIDDEN_RE.search(message):
        raise ValueError("Commit message contains forbidden flags")

    cwd = sandbox or Path.cwd()

    # 1. Check status first
    status_result = run_subprocess(
        ["git", "status", "--porcelain=v1"], cwd=cwd, timeout=10
    )
    if status_result["exit_code"] != 0:
        return {
            "action": "commit",
            "message": message,
            "commit_hash": "",
            "files": [],
            "success": False,
            "reason": status_result["stderr"] or "git status failed",
            "verification": "pre-commit status check failed",
        }

    # 2. Stage specified paths
    staged_paths = []
    if paths:
        for p in paths:
            if not isinstance(p, str) or not p:
                continue
            if p.startswith("-"):
                raise ValueError(f"Invalid path (looks like a flag): {p!r}")
            add_result = run_subprocess(
                ["git", "add", "--", p], cwd=cwd, timeout=10
            )
            if add_result["exit_code"] != 0:
                return {
                    "action": "commit",
                    "message": message,
                    "commit_hash": "",
                    "files": [],
                    "success": False,
                    "reason": add_result["stderr"] or f"git add failed for {p}",
                    "verification": "staging failed",
                }
            staged_paths.append(p)

    # 3. Check if there is anything to commit
    staged_check = run_subprocess(
        ["git", "diff", "--cached", "--name-only"], cwd=cwd, timeout=10
    )
    cached_files = [
        f for f in staged_check["stdout"].strip().splitlines() if f
    ]
    if not cached_files:
        return {
            "action": "commit",
            "message": message,
            "commit_hash": "",
            "files": [],
            "success": False,
            "reason": "nothing to commit — no staged changes",
            "verification": "git diff --cached shows no files",
        }

    # 4. Commit
    commit_result = run_subprocess(
        ["git", "commit", "-m", message], cwd=cwd, timeout=30
    )
    if commit_result["exit_code"] != 0:
        return {
            "action": "commit",
            "message": message,
            "commit_hash": "",
            "files": cached_files,
            "success": False,
            "reason": commit_result["stderr"] or "git commit failed",
            "verification": "commit command failed",
        }

    # 5. Verify via git log
    log_result = run_subprocess(
        ["git", "log", "-1", "--oneline"], cwd=cwd, timeout=10
    )
    commit_hash = ""
    if log_result["exit_code"] == 0 and log_result["stdout"].strip():
        commit_hash = log_result["stdout"].strip().split()[0]

    return {
        "action": "commit",
        "message": message,
        "commit_hash": commit_hash,
        "files": cached_files,
        "success": True,
        "verification": f"git log confirms {commit_hash}",
    }
