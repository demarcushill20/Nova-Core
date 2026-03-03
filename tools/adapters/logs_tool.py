"""Adapter: logs.tail

Read-only log tailing via journalctl with structured output.
"""

import re
from pathlib import Path

from tools.runner import run_subprocess

_MAX_BUFFER = 100 * 1024  # 100 KB — same as runner's global cap
_NAME_RE = re.compile(r"^[a-zA-Z0-9._@-]+$")


def logs_tail(service: str, lines: int = 200, sandbox: Path | None = None) -> dict:
    """Tail journalctl logs for a systemd service.

    Args:
        service: systemd unit name (e.g. "novacore-watcher")
        lines: number of lines to retrieve (clamped 1–500)
        sandbox: working directory for subprocess

    Returns:
        dict with keys: ok, exit_code, stderr, service, lines, entries, truncated
    """
    if not service or not isinstance(service, str):
        raise ValueError("service name is required (str)")
    if not _NAME_RE.match(service):
        raise ValueError(f"Invalid service name: {service!r}")

    # Clamp lines
    lines = max(1, min(int(lines), 500))

    cwd = sandbox or Path.cwd()
    result = run_subprocess(
        ["journalctl", "-u", service, "-n", str(lines),
         "--no-pager", "-o", "short-iso"],
        cwd=cwd,
        timeout=15,
    )

    if result["exit_code"] != 0:
        return {
            "ok": False,
            "exit_code": result["exit_code"],
            "stderr": result["stderr"],
            "service": service,
            "lines": 0,
            "entries": [],
            "truncated": False,
        }

    stdout = result["stdout"]
    truncated = len(stdout) > _MAX_BUFFER
    if truncated:
        stdout = stdout[:_MAX_BUFFER]

    entries = [line for line in stdout.splitlines() if line]

    return {
        "ok": True,
        "exit_code": 0,
        "stderr": "",
        "service": service,
        "lines": len(entries),
        "entries": entries,
        "truncated": truncated,
    }
