"""Adapter: system.service.status — structured systemd service status.

Returns a dict with parsed fields instead of raw stdout.
Delegates shell execution to the existing runner infrastructure.
"""

import re
from pathlib import Path

from tools.runner import run_subprocess

# --- Parsing helpers --------------------------------------------------------

_LOADED_RE = re.compile(r"Loaded:\s*(.+)")
_ACTIVE_RE = re.compile(r"Active:\s*(.+)")
_ACTIVE_PARTS_RE = re.compile(r"^(\S+)\s*\(([^)]+)\)")
_MAIN_PID_RE = re.compile(r"Main PID:\s*(\d+)")


def parse_status_output(stdout: str) -> dict:
    """Parse systemctl status output into structured fields."""
    loaded = ""
    active_summary = ""
    active_state = ""
    sub_state = ""
    main_pid = None

    for line in stdout.splitlines():
        stripped = line.strip()

        if not loaded:
            m = _LOADED_RE.match(stripped)
            if m:
                loaded = m.group(1).strip()

        if not active_summary:
            m = _ACTIVE_RE.match(stripped)
            if m:
                active_summary = m.group(1).strip()
                pm = _ACTIVE_PARTS_RE.match(active_summary)
                if pm:
                    active_state = pm.group(1)
                    sub_state = pm.group(2)

        if main_pid is None:
            m = _MAIN_PID_RE.search(stripped)
            if m:
                main_pid = int(m.group(1))

    # Last ~10 lines as raw excerpt
    lines = stdout.rstrip("\n").splitlines()
    raw_excerpt = "\n".join(lines[-10:]) if lines else ""

    return {
        "loaded": loaded,
        "active_summary": active_summary,
        "active_state": active_state,
        "sub_state": sub_state,
        "main_pid": main_pid,
        "raw_excerpt": raw_excerpt,
    }


def service_status(name: str, sandbox: Path | None = None) -> dict:
    """Get structured status of a systemd service.

    Args:
        name: service unit name (e.g. "novacore-watcher")
        sandbox: working directory for subprocess (defaults to cwd)

    Returns:
        dict with keys: service, loaded, active_summary, active_state,
        sub_state, main_pid, raw_excerpt
    """
    if not name or not isinstance(name, str):
        raise ValueError("system.service.status requires 'name' (str)")

    # Sanitize: only allow alphanumeric, dash, underscore, dot, @
    if not re.match(r"^[a-zA-Z0-9._@-]+$", name):
        raise ValueError(f"Invalid service name: {name!r}")

    cwd = sandbox or Path.cwd()
    result = run_subprocess(
        ["systemctl", "status", name, "--no-pager", "-l"],
        cwd=cwd,
        timeout=10,
    )

    parsed = parse_status_output(result["stdout"])
    parsed["service"] = name

    # systemctl status returns exit 3 for inactive services — not an error
    exit_code = result["exit_code"]
    ok = exit_code in (0, 3)

    return {
        "ok": ok,
        "exit_code": exit_code,
        "stderr": result["stderr"],
        **parsed,
    }
