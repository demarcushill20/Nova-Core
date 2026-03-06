"""Adapter: repo.files.read / repo.files.write / repo.files.patch

Safe, sandboxed file operations inside the repo with structured output.
"""

import os
import tempfile
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
    _sandbox: Path | None = None,
) -> dict:
    """Read a file inside the repo sandbox with structured output.

    Args:
        path: Relative path within the repo (or absolute if inside sandbox).
        max_bytes: Maximum bytes to read (clamped to 1024–500000).
        _sandbox: Internal override for repo root (testing only). Do not
                  expose through runner or tool-call args.

    Returns:
        dict with keys: ok, path, bytes, truncated, content

    Raises:
        ValueError: If path escapes the sandbox or is invalid.
    """
    if not path or not isinstance(path, str):
        raise ValueError("path is required (non-empty str)")

    root = _sandbox if _sandbox is not None else _resolve_repo_root()
    root = root.resolve()

    # Clamp max_bytes
    max_bytes = max(_MIN_BYTES, min(int(max_bytes), _MAX_BYTES))

    # Resolve the target path
    target = (root / path).resolve()

    # Sandbox check: target must be within root
    try:
        rel_path = target.relative_to(root)
    except ValueError:
        raise ValueError(
            f"Path escapes sandbox: {path!r} resolves to {target} "
            f"which is outside {root}"
        ) from None

    # Normalized repo-relative path for output
    norm_path = str(rel_path)

    # Check existence
    if not target.is_file():
        return {
            "ok": False,
            "path": norm_path,
            "bytes": 0,
            "truncated": False,
            "content": "",
            "error": f"File not found: {norm_path}",
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
        "path": norm_path,
        "bytes": file_size,
        "truncated": truncated,
        "content": content,
    }


def _resolve_and_check(path: str, sandbox: Path | None) -> tuple[Path, Path, str]:
    """Resolve *path* within the sandbox and return (root, target, norm_path).

    Raises ValueError on empty path or sandbox escape.
    """
    if not path or not isinstance(path, str):
        raise ValueError("path is required (non-empty str)")

    root = (sandbox if sandbox is not None else _resolve_repo_root()).resolve()
    target = (root / path).resolve()

    try:
        rel_path = target.relative_to(root)
    except ValueError:
        raise ValueError(
            f"Path escapes sandbox: {path!r} resolves to {target} "
            f"which is outside {root}"
        ) from None

    return root, target, str(rel_path)


def repo_write(
    path: str,
    content: str,
    make_dirs: bool = True,
    _sandbox: Path | None = None,
) -> dict:
    """Write a file inside the repo sandbox with atomic write + verification.

    Args:
        path: Relative path within the repo.
        content: String content to write.
        make_dirs: Create parent directories if they don't exist (default True).
        _sandbox: Internal override for repo root (testing only). Do not
                  expose through runner or tool-call args.

    Returns:
        dict with keys: ok, path, bytes_written, created, overwritten, verified, message
    """
    # --- Validate & resolve ---
    try:
        _root, target, norm_path = _resolve_and_check(path, _sandbox)
    except ValueError as exc:
        return {
            "ok": False,
            "path": path,
            "bytes_written": 0,
            "created": False,
            "overwritten": False,
            "verified": False,
            "message": str(exc),
        }

    # --- Parent directory handling ---
    parent = target.parent
    if not parent.exists():
        if not make_dirs:
            return {
                "ok": False,
                "path": norm_path,
                "bytes_written": 0,
                "created": False,
                "overwritten": False,
                "verified": False,
                "message": f"Parent directory does not exist: {parent.relative_to(_root)}",
            }
        parent.mkdir(parents=True, exist_ok=True)

    existed = target.exists()
    encoded = content.encode("utf-8")

    # --- Atomic write via temp file ---
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(parent), prefix=".repo_write_")
        os.write(fd, encoded)
        os.fsync(fd)
        os.close(fd)
        fd = None  # mark closed

        os.replace(tmp_path, str(target))
        tmp_path = None  # mark consumed
    except Exception as exc:
        # Clean up temp file on failure
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return {
            "ok": False,
            "path": norm_path,
            "bytes_written": 0,
            "created": False,
            "overwritten": False,
            "verified": False,
            "message": f"Write failed: {exc}",
        }

    # --- Post-write verification ---
    try:
        readback = target.read_bytes()
        verified = readback == encoded
    except Exception:
        verified = False

    if not verified:
        return {
            "ok": False,
            "path": norm_path,
            "bytes_written": len(encoded),
            "created": not existed,
            "overwritten": existed,
            "verified": False,
            "message": "Verification failed: written content does not match",
        }

    return {
        "ok": True,
        "path": norm_path,
        "bytes_written": len(encoded),
        "created": not existed,
        "overwritten": existed,
        "verified": True,
        "message": "overwritten" if existed else "created",
    }


def repo_patch(
    path: str,
    operations: list[dict],
    create_if_missing: bool = False,
    _sandbox: Path | None = None,
) -> dict:
    """Apply structured patch operations to a file inside the repo sandbox.

    Args:
        path: Relative path within the repo.
        operations: List of operation dicts (replace or append).
        create_if_missing: If True, create the file when it doesn't exist.
        _sandbox: Internal override for repo root (testing only). Do not
                  expose through runner or tool-call args.

    Returns:
        dict with keys: ok, path, operations_applied, created, verified, message
    """
    # --- Validate & resolve ---
    try:
        _root, target, norm_path = _resolve_and_check(path, _sandbox)
    except ValueError as exc:
        return {
            "ok": False,
            "path": path,
            "operations_applied": 0,
            "created": False,
            "verified": False,
            "message": str(exc),
        }

    # --- Read or create initial content ---
    created = False
    if not target.exists():
        if not create_if_missing:
            return {
                "ok": False,
                "path": norm_path,
                "operations_applied": 0,
                "created": False,
                "verified": False,
                "message": f"File not found: {norm_path}",
            }
        content = ""
        created = True
    else:
        if not target.is_file():
            return {
                "ok": False,
                "path": norm_path,
                "operations_applied": 0,
                "created": False,
                "verified": False,
                "message": f"Not a regular file: {norm_path}",
            }
        content = target.read_text(encoding="utf-8")

    # --- Apply operations sequentially ---
    ops_applied = 0
    for i, op in enumerate(operations):
        op_type = op.get("type")
        if op_type == "replace":
            old = op.get("old")
            new = op.get("new")
            if old is None or new is None:
                return {
                    "ok": False,
                    "path": norm_path,
                    "operations_applied": ops_applied,
                    "created": created,
                    "verified": False,
                    "message": f"Operation {i}: replace requires 'old' and 'new'",
                }
            if old not in content:
                return {
                    "ok": False,
                    "path": norm_path,
                    "operations_applied": ops_applied,
                    "created": created,
                    "verified": False,
                    "message": f"Operation {i}: text not found for replace",
                }
            count = op.get("count")
            if count is not None:
                content = content.replace(old, new, int(count))
            else:
                content = content.replace(old, new)
            ops_applied += 1

        elif op_type == "append":
            text = op.get("text")
            if text is None:
                return {
                    "ok": False,
                    "path": norm_path,
                    "operations_applied": ops_applied,
                    "created": created,
                    "verified": False,
                    "message": f"Operation {i}: append requires 'text'",
                }
            content += text
            ops_applied += 1

        else:
            return {
                "ok": False,
                "path": norm_path,
                "operations_applied": ops_applied,
                "created": created,
                "verified": False,
                "message": f"Operation {i}: unsupported type {op_type!r}",
            }

    # --- Write result using atomic write (same pattern as repo_write) ---
    parent = target.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)

    encoded = content.encode("utf-8")
    fd = None
    tmp_path_str = None
    try:
        fd, tmp_path_str = tempfile.mkstemp(dir=str(parent), prefix=".repo_patch_")
        os.write(fd, encoded)
        os.fsync(fd)
        os.close(fd)
        fd = None

        os.replace(tmp_path_str, str(target))
        tmp_path_str = None
    except Exception as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path_str is not None:
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
        return {
            "ok": False,
            "path": norm_path,
            "operations_applied": ops_applied,
            "created": created,
            "verified": False,
            "message": f"Write failed: {exc}",
        }

    # --- Post-write verification ---
    try:
        readback = target.read_bytes()
        verified = readback == encoded
    except Exception:
        verified = False

    if not verified:
        return {
            "ok": False,
            "path": norm_path,
            "operations_applied": ops_applied,
            "created": created,
            "verified": False,
            "message": "Verification failed: written content does not match",
        }

    return {
        "ok": True,
        "path": norm_path,
        "operations_applied": ops_applied,
        "created": created,
        "verified": True,
        "message": f"{ops_applied} operation(s) applied"
        + (" (file created)" if created else ""),
    }
