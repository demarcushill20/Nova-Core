"""Adapter: repo.files.read

Safe, sandboxed file reading inside the repo with structured output.
"""

from pathlib import Path

_MIN_BYTES = 1024
_MAX_BYTES = 500_000
_DEFAULT_BYTES = 200_000


def _resolve_repo_root() -> Path:
    """Resolve the repo root dynamically (same approach as runner.py)."""
    return Path(__file__).resolve().parent.parent.parent


def repo_read(
    path: str,
    max_bytes: int = _DEFAULT_BYTES,
    sandbox: Path | None = None,
) -> dict:
    """Read a file inside the repo sandbox with structured output.

    Args:
        path: Relative path within the repo (or absolute if inside sandbox).
        max_bytes: Maximum bytes to read (clamped to 1024–500000).
        sandbox: Override repo root (for testing). Defaults to auto-detect.

    Returns:
        dict with keys: ok, path, bytes, truncated, content

    Raises:
        ValueError: If path escapes the sandbox or is invalid.
    """
    if not path or not isinstance(path, str):
        raise ValueError("path is required (non-empty str)")

    root = sandbox if sandbox is not None else _resolve_repo_root()
    root = root.resolve()

    # Clamp max_bytes
    max_bytes = max(_MIN_BYTES, min(int(max_bytes), _MAX_BYTES))

    # Resolve the target path
    target = (root / path).resolve()

    # Sandbox check: target must be within root
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(
            f"Path escapes sandbox: {path!r} resolves to {target} "
            f"which is outside {root}"
        ) from None

    # Check existence
    if not target.is_file():
        return {
            "ok": False,
            "path": str(target),
            "bytes": 0,
            "truncated": False,
            "content": "",
            "error": f"File not found: {target}",
        }

    # Read file
    raw = target.read_bytes()
    file_size = len(raw)
    truncated = file_size > max_bytes
    if truncated:
        raw = raw[:max_bytes]

    content = raw.decode("utf-8", errors="replace")

    return {
        "ok": True,
        "path": str(target),
        "bytes": file_size,
        "truncated": truncated,
        "content": content,
    }
