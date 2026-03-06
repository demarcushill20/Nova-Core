"""Adapter: repo.diff

Structured git diff for individual files with sandbox enforcement.
"""

import re
import subprocess
from pathlib import Path

# Safety: reject anything that looks like a git flag in path/against args
_FLAG_RE = re.compile(r"^-")
# Only allow safe ref patterns (branch names, tags, short SHAs, HEAD~N)
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/~^-]+$")


def _resolve_repo_root() -> Path:
    """Resolve the repo root dynamically (same approach as runner.py)."""
    return Path(__file__).resolve().parent.parent.parent


def repo_diff(
    path: str,
    against: str | None = None,
    _sandbox: Path | None = None,
) -> dict:
    """Return structured git diff for a file inside the repo sandbox.

    Args:
        path: Relative path within the repo.
        against: Optional git ref to diff against (e.g. HEAD, a branch name).
                 Defaults to showing uncommitted working-tree changes.
        _sandbox: Internal override for repo root (testing only). Do not
                  expose through runner or tool-call args.

    Returns:
        dict with keys: ok, path, changed, diff, summary
    """
    if not path or not isinstance(path, str):
        return {
            "ok": False,
            "path": "",
            "changed": False,
            "diff": "",
            "summary": {"lines_added": 0, "lines_removed": 0},
            "error": "path is required (non-empty str)",
        }

    # Reject flag injection through path
    if _FLAG_RE.match(path):
        return {
            "ok": False,
            "path": path,
            "changed": False,
            "diff": "",
            "summary": {"lines_added": 0, "lines_removed": 0},
            "error": "path must not start with '-' (flag injection)",
        }

    root = (_sandbox if _sandbox is not None else _resolve_repo_root()).resolve()

    # Resolve and sandbox-check
    target = (root / path).resolve()
    try:
        rel_path = target.relative_to(root)
    except ValueError:
        return {
            "ok": False,
            "path": path,
            "changed": False,
            "diff": "",
            "summary": {"lines_added": 0, "lines_removed": 0},
            "error": f"Path escapes sandbox: {path!r} resolves outside {root}",
        }

    norm_path = str(rel_path)

    # Check that target is a file (or at least exists in the working tree)
    if not target.is_file():
        return {
            "ok": False,
            "path": norm_path,
            "changed": False,
            "diff": "",
            "summary": {"lines_added": 0, "lines_removed": 0},
            "error": f"Not a file: {norm_path}",
        }

    # Validate 'against' ref if provided
    if against is not None:
        if not isinstance(against, str) or not against:
            return {
                "ok": False,
                "path": norm_path,
                "changed": False,
                "diff": "",
                "summary": {"lines_added": 0, "lines_removed": 0},
                "error": "against must be a non-empty string",
            }
        if _FLAG_RE.match(against):
            return {
                "ok": False,
                "path": norm_path,
                "changed": False,
                "diff": "",
                "summary": {"lines_added": 0, "lines_removed": 0},
                "error": "against must not start with '-' (flag injection)",
            }
        if not _SAFE_REF_RE.match(against):
            return {
                "ok": False,
                "path": norm_path,
                "changed": False,
                "diff": "",
                "summary": {"lines_added": 0, "lines_removed": 0},
                "error": f"against contains unsafe characters: {against!r}",
            }

    # Build git diff command
    cmd = ["git", "diff"]
    if against is not None:
        cmd.append(against)
    cmd.append("--")
    cmd.append(norm_path)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "path": norm_path,
            "changed": False,
            "diff": "",
            "summary": {"lines_added": 0, "lines_removed": 0},
            "error": "git diff timed out",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "path": norm_path,
            "changed": False,
            "diff": "",
            "summary": {"lines_added": 0, "lines_removed": 0},
            "error": "git not found",
        }

    if proc.returncode != 0:
        return {
            "ok": False,
            "path": norm_path,
            "changed": False,
            "diff": "",
            "summary": {"lines_added": 0, "lines_removed": 0},
            "error": f"git diff failed: {proc.stderr.strip()}",
        }

    diff_text = proc.stdout
    changed = len(diff_text.strip()) > 0

    # Parse summary: count +/- lines
    lines_added = 0
    lines_removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines_added += 1
        elif line.startswith("-") and not line.startswith("---"):
            lines_removed += 1

    return {
        "ok": True,
        "path": norm_path,
        "changed": changed,
        "diff": diff_text,
        "summary": {
            "lines_added": lines_added,
            "lines_removed": lines_removed,
        },
    }
