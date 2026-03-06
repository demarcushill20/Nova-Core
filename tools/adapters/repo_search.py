"""Adapter: repo.search

Fast substring search across repository files with sandbox enforcement.
"""

import os
from pathlib import Path

_MAX_FILE_SIZE = 1_000_000  # 1 MB
_MIN_RESULTS = 1
_MAX_RESULTS = 100
_DEFAULT_RESULTS = 50


def _resolve_repo_root() -> Path:
    """Resolve the repo root dynamically (same approach as runner.py)."""
    return Path(__file__).resolve().parent.parent.parent


def _is_binary(data: bytes, sample_size: int = 8192) -> bool:
    """Heuristic: file is binary if it contains null bytes in the first chunk."""
    return b"\x00" in data[:sample_size]


def repo_search(
    query: str,
    path: str | None = None,
    max_results: int = _DEFAULT_RESULTS,
    _sandbox: Path | None = None,
) -> dict:
    """Search for a substring across files in the repo sandbox.

    Args:
        query: Substring to search for (case-sensitive).
        path: Optional subdirectory to restrict the search to.
        max_results: Maximum matches to return (clamped to 1–100).
        _sandbox: Internal override for repo root (testing only). Do not
                  expose through runner or tool-call args.

    Returns:
        dict with keys: ok, query, results, count
    """
    if not query or not isinstance(query, str):
        return {
            "ok": False,
            "query": query if isinstance(query, str) else "",
            "results": [],
            "count": 0,
            "error": "query is required (non-empty str)",
        }

    root = (_sandbox if _sandbox is not None else _resolve_repo_root()).resolve()

    # Clamp max_results
    max_results = max(_MIN_RESULTS, min(int(max_results), _MAX_RESULTS))

    # Determine search base directory
    if path is not None and path != "":
        search_base = (root / path).resolve()
        # Sandbox check
        try:
            search_base.relative_to(root)
        except ValueError:
            return {
                "ok": False,
                "query": query,
                "results": [],
                "count": 0,
                "error": f"Path escapes sandbox: {path!r} resolves outside {root}",
            }
        if not search_base.is_dir():
            return {
                "ok": False,
                "query": query,
                "results": [],
                "count": 0,
                "error": f"Not a directory: {path!r}",
            }
    else:
        search_base = root

    matches = []

    for dirpath, dirnames, filenames in os.walk(search_base):
        # Skip hidden directories (e.g., .git)
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for fname in filenames:
            if len(matches) >= max_results:
                break

            filepath = Path(dirpath) / fname

            # Skip files larger than 1 MB
            try:
                size = filepath.stat().st_size
            except OSError:
                continue
            if size > _MAX_FILE_SIZE:
                continue

            # Read file
            try:
                raw = filepath.read_bytes()
            except OSError:
                continue

            # Skip binary files
            if _is_binary(raw):
                continue

            # Decode
            try:
                text = raw.decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                continue

            # Search line by line
            for line_num, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    try:
                        rel = str(filepath.resolve().relative_to(root))
                    except ValueError:
                        continue
                    matches.append({
                        "path": rel,
                        "line": line_num,
                        "snippet": line.rstrip(),
                    })
                    if len(matches) >= max_results:
                        break

        if len(matches) >= max_results:
            break

    return {
        "ok": True,
        "query": query,
        "results": matches,
        "count": len(matches),
    }
