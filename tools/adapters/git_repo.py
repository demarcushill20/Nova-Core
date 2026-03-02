"""Adapter: repo.git.status — structured Git status.

Returns a dict with parsed branch, ahead/behind, and file lists
instead of raw porcelain output.
Delegates execution to the existing runner infrastructure.
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
