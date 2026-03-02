"""File-operation tools for NovaCore.

Implements files.read, files.write, files.list, and files.diff
with sandbox enforcement and binary detection.
"""

import difflib
import glob as globmod
from pathlib import Path

from tools.registry import resolve_sandbox_root

# --- Constants ---------------------------------------------------------------

_BINARY_CHECK_SIZE = 8192
_MAX_ENTRIES = 1000


# --- Path safety -------------------------------------------------------------


def resolve_path(sandbox_root: Path, user_path: str) -> Path:
    """Resolve a user-supplied path and enforce sandbox containment.

    Accepts absolute paths or paths relative to sandbox_root.
    Raises ValueError if the resolved path escapes the sandbox.
    """
    p = Path(user_path).expanduser()
    if not p.is_absolute():
        p = sandbox_root / p
    resolved = p.resolve()
    try:
        resolved.relative_to(sandbox_root)
    except ValueError:
        raise ValueError(
            f"Path {resolved} is outside sandbox {sandbox_root}"
        ) from None
    return resolved


# --- Core operations ---------------------------------------------------------


def read_text(
    path: Path,
    offset: int | None = None,
    limit: int | None = None,
) -> tuple[str, int]:
    """Read a UTF-8 text file, optionally slicing by line.

    Args:
        path: Resolved absolute path.
        offset: 1-based starting line number.
        limit: Maximum lines to return.

    Returns:
        (content, total_lines)

    Raises:
        FileNotFoundError: If path does not exist.
        ValueError: If file appears to be binary.
    """
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not path.is_file():
        raise ValueError(f"Not a regular file: {path}")

    # Binary detection: check for NUL bytes in first 8 KB
    with path.open("rb") as fb:
        chunk = fb.read(_BINARY_CHECK_SIZE)
    if b"\x00" in chunk:
        raise ValueError(f"Binary file detected, refusing to read: {path}")

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    total = len(lines)

    if offset is not None or limit is not None:
        start = max((offset or 1) - 1, 0)
        end = start + limit if limit is not None else total
        lines = lines[start:end]
        text = "".join(lines)

    return text, total


def write_text(path: Path, content: str, create_dirs: bool = False) -> int:
    """Write UTF-8 text to a file.

    Args:
        path: Resolved absolute path.
        content: Text to write.
        create_dirs: Create parent directories if missing.

    Returns:
        Number of bytes written.
    """
    if create_dirs:
        path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    path.write_bytes(data)
    return len(data)


def list_glob(
    sandbox_root: Path,
    pattern: str,
    recursive: bool = False,
) -> list[str]:
    """List files matching a glob pattern within the sandbox.

    Args:
        sandbox_root: Resolved sandbox root.
        pattern: Glob pattern relative to sandbox_root.
        recursive: Allow ** patterns.

    Returns:
        List of matching paths relative to sandbox_root, capped at 1000.
    """
    # Block patterns that try to escape
    if pattern.startswith("/") or ".." in pattern.split("/"):
        raise ValueError(f"Pattern must not escape sandbox: {pattern!r}")

    full_pattern = str(sandbox_root / pattern)
    matches = sorted(globmod.glob(full_pattern, recursive=recursive))

    results = []
    for m in matches:
        mp = Path(m).resolve()
        try:
            rel = mp.relative_to(sandbox_root)
        except ValueError:
            continue  # skip anything outside sandbox
        results.append(str(rel))
        if len(results) >= _MAX_ENTRIES:
            break

    return results


def unified_diff(
    a_text: str,
    b_text: str,
    fromfile: str = "a",
    tofile: str = "b",
    context_lines: int = 3,
) -> str:
    """Compute a unified diff between two text strings."""
    a_lines = a_text.splitlines(keepends=True)
    b_lines = b_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        a_lines, b_lines,
        fromfile=fromfile, tofile=tofile,
        n=context_lines,
    )
    return "".join(diff)


# --- Dispatcher --------------------------------------------------------------


def dispatch_files_tool(tool_name: str, args: dict, registry: dict) -> dict:
    """Execute a files.* tool and return a result dict.

    Supported tools: files.read, files.write, files.list, files.diff
    """
    sandbox = resolve_sandbox_root(registry)

    if tool_name == "files.read":
        return _do_read(args, sandbox)
    if tool_name == "files.write":
        return _do_write(args, sandbox)
    if tool_name == "files.list":
        return _do_list(args, sandbox)
    if tool_name == "files.diff":
        return _do_diff(args, sandbox)

    raise ValueError(f"Unknown files tool: {tool_name!r}")


# --- Tool implementations ---------------------------------------------------


def _do_read(args: dict, sandbox: Path) -> dict:
    path_str = args.get("path")
    if not path_str:
        raise ValueError("files.read requires 'path'")

    path = resolve_path(sandbox, path_str)
    offset = args.get("offset")
    limit = args.get("limit")

    content, total = read_text(path, offset=offset, limit=limit)
    return {"path": str(path), "content": content, "lines": total}


def _do_write(args: dict, sandbox: Path) -> dict:
    path_str = args.get("path")
    if not path_str:
        raise ValueError("files.write requires 'path'")

    content = args.get("content")
    if content is None:
        raise ValueError("files.write requires 'content'")

    path = resolve_path(sandbox, path_str)
    create_dirs = bool(args.get("create_dirs", False))

    nbytes = write_text(path, content, create_dirs=create_dirs)
    return {"path": str(path), "bytes": nbytes}


def _do_list(args: dict, sandbox: Path) -> dict:
    pattern = args.get("pattern")
    if not pattern:
        raise ValueError("files.list requires 'pattern'")

    recursive = bool(args.get("recursive", False))
    entries = list_glob(sandbox, pattern, recursive=recursive)
    return {"entries": entries, "count": len(entries)}


def _do_diff(args: dict, sandbox: Path) -> dict:
    path_a_str = args.get("path_a")
    if not path_a_str:
        raise ValueError("files.diff requires 'path_a'")

    path_a = resolve_path(sandbox, path_a_str)
    a_text, _ = read_text(path_a)
    fromfile = str(path_a)

    # Either path_b or content_b
    path_b_str = args.get("path_b")
    content_b = args.get("content_b")

    if path_b_str and content_b is not None:
        raise ValueError("files.diff: 'path_b' and 'content_b' are mutually exclusive")

    if path_b_str:
        path_b = resolve_path(sandbox, path_b_str)
        b_text, _ = read_text(path_b)
        tofile = str(path_b)
    elif content_b is not None:
        b_text = content_b
        tofile = "<content_b>"
    else:
        raise ValueError("files.diff requires 'path_b' or 'content_b'")

    ctx = int(args.get("context_lines", 3))
    diff_text = unified_diff(a_text, b_text, fromfile=fromfile, tofile=tofile, context_lines=ctx)
    return {"diff": diff_text, "changed": bool(diff_text)}
