"""Adapters: system.service.status / system.service.restart

Return structured dicts instead of raw stdout.
Delegate shell execution to the existing runner infrastructure.
"""

import os
import re
from pathlib import Path

from tools.runner import run_subprocess

_CONFIRM_TOKEN = "ALLOW_DESTRUCTIVE"

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


def _validate_name(name: str) -> None:
    """Validate and sanitize a service unit name."""
    if not name or not isinstance(name, str):
        raise ValueError("service name is required (str)")
    if not re.match(r"^[a-zA-Z0-9._@-]+$", name):
        raise ValueError(f"Invalid service name: {name!r}")


def service_status(name: str, sandbox: Path | None = None) -> dict:
    """Get structured status of a systemd service.

    Args:
        name: service unit name (e.g. "novacore-watcher")
        sandbox: working directory for subprocess (defaults to cwd)

    Returns:
        dict with keys: service, loaded, active_summary, active_state,
        sub_state, main_pid, raw_excerpt
    """
    _validate_name(name)

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


def service_restart(name: str, sandbox: Path | None = None) -> dict:
    """Restart a systemd service and verify the result.

    Requires NOVACORE_CONFIRM=ALLOW_DESTRUCTIVE to be set.

    Args:
        name: service unit name (e.g. "novacore-watcher")
        sandbox: working directory for subprocess (defaults to cwd)

    Returns:
        dict with keys: service, action, success, active_state, sub_state,
        main_pid, verification, and optionally blocked/reason
    """
    _validate_name(name)

    # Confirmation gate — restart is a state-changing operation
    if os.environ.get("NOVACORE_CONFIRM") != _CONFIRM_TOKEN:
        return {
            "service": name,
            "action": "restart",
            "success": False,
            "blocked": True,
            "reason": (
                "BLOCKED: service restart requires approval. "
                "Set env NOVACORE_CONFIRM=ALLOW_DESTRUCTIVE to override."
            ),
            "active_state": "",
            "sub_state": "",
            "main_pid": None,
            "verification": "not attempted — blocked by confirmation policy",
        }

    cwd = sandbox or Path.cwd()
    result = run_subprocess(
        ["sudo", "systemctl", "restart", name],
        cwd=cwd,
        timeout=30,
    )

    if result["exit_code"] != 0:
        return {
            "service": name,
            "action": "restart",
            "success": False,
            "blocked": False,
            "reason": result["stderr"] or f"systemctl restart exited {result['exit_code']}",
            "active_state": "",
            "sub_state": "",
            "main_pid": None,
            "verification": "restart command failed",
        }

    # Verify via status check
    status = service_status(name, sandbox=sandbox)

    active_state = status.get("active_state", "")
    sub_state = status.get("sub_state", "")
    main_pid = status.get("main_pid")
    is_running = active_state == "active" and sub_state == "running"

    return {
        "service": name,
        "action": "restart",
        "success": is_running,
        "active_state": active_state,
        "sub_state": sub_state,
        "main_pid": main_pid,
        "verification": (
            f"active (running), PID {main_pid}" if is_running
            else f"post-restart state: {active_state} ({sub_state})"
        ),
    }
