"""Adapters: repo.git.status / repo.git.diff — structured Git queries.

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
