"""P11 — Input validation for multi-agent runtime.

Prevents path traversal and injection by validating all IDs that touch
filesystem paths or are used as message routing keys.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# IDs must be alphanumeric with hyphens and underscores, 1-128 chars.
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


class UnsafeIDError(ValueError):
    """Raised when an ID contains characters that could cause path traversal or injection."""


def validate_id(value: str, field_name: str = "id") -> str:
    """Validate that an ID is safe for use in file paths and routing.

    Args:
        value: The ID string to validate.
        field_name: Human-readable name for error messages.

    Returns:
        The validated string (unchanged).

    Raises:
        UnsafeIDError: If the value contains unsafe characters.
    """
    if not isinstance(value, str) or not _SAFE_ID_RE.match(value):
        raise UnsafeIDError(f"Invalid {field_name}: {value!r} — must match {_SAFE_ID_RE.pattern}")
    # Extra guard: reject path traversal patterns even if regex somehow passes
    if ".." in value or "/" in value or "\\" in value:
        raise UnsafeIDError(f"Invalid {field_name}: {value!r} — contains path traversal characters")
    return value


def atomic_write(path: Path, content: str) -> None:
    """Write content to a file atomically using write-to-temp-then-rename.

    Prevents corruption from partial writes on crash/SIGKILL.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
