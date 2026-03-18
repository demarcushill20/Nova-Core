"""Phase 10 — Obsidian Headless Auto-Sync integration.

Provides post-write sync status checking for audit visibility.
The actual sync is performed by the `ob sync --continuous` daemon
(Obsidian Headless), which watches the vault filesystem and syncs
automatically when files change.

This module does NOT perform the sync itself. It:
1. Checks if the sync daemon is running
2. Optionally triggers a one-shot sync nudge if the daemon is not running
3. Returns structured sync status for audit logging

Design constraints:
  - Sync is operationally separate from write logic.
  - Sync failure never invalidates a successful write.
  - Fail-open: all errors are captured, never raised.
  - No vault permissions are changed.
  - No secrets are stored in code.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Sync config path (operator-managed, not in repo)
_SYNC_CONFIG_PATH = Path("/home/nova/nova-vault/.nova-sync-config.json")

_DEFAULT_SYNC_CONFIG = {
    "enabled": False,
    "mode": "continuous",  # "continuous" (daemon) or "oneshot" (per-write)
    "ob_binary": "ob",  # path to obsidian-headless CLI
    "vault_path": "/home/nova/nova-vault",
    "sync_timeout_seconds": 30,  # timeout for oneshot sync
    "check_daemon": True,  # check if daemon is running
}


def _load_sync_config() -> dict[str, Any]:
    """Load sync configuration from operator-managed file.

    Falls back to defaults (disabled) if config is missing or corrupt.
    """
    try:
        if _SYNC_CONFIG_PATH.exists():
            raw = _SYNC_CONFIG_PATH.read_text(encoding="utf-8")
            config = json.loads(raw)
            # Merge with defaults
            merged = {**_DEFAULT_SYNC_CONFIG, **config}
            return merged
    except Exception as exc:
        logger.warning("SYNC CONFIG LOAD FAILED: %s — using defaults (disabled)", exc)

    return dict(_DEFAULT_SYNC_CONFIG)


# ---------------------------------------------------------------------------
# Daemon status check
# ---------------------------------------------------------------------------


def _is_daemon_running(ob_binary: str = "ob") -> bool:
    """Check if the obsidian-headless sync daemon is running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"{ob_binary} sync.*--continuous"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        # Also check for the systemd service
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "obsidian-headless-sync"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0 and b"active" in result.stdout
        except Exception:
            return False


def _check_ob_available(ob_binary: str = "ob") -> bool:
    """Check if the ob CLI binary is available."""
    return shutil.which(ob_binary) is not None


# ---------------------------------------------------------------------------
# One-shot sync nudge
# ---------------------------------------------------------------------------


def _trigger_oneshot_sync(
    ob_binary: str,
    vault_path: str,
    timeout: int,
) -> dict[str, Any]:
    """Trigger a one-shot sync via `ob sync`.

    Only used as fallback when continuous daemon is not running.
    Returns structured result.
    """
    try:
        result = subprocess.run(
            [ob_binary, "sync", "--path", vault_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return {
                "oneshot_triggered": True,
                "oneshot_result": "success",
                "oneshot_output": result.stdout[:500] if result.stdout else "",
            }
        else:
            return {
                "oneshot_triggered": True,
                "oneshot_result": "failed",
                "oneshot_error": result.stderr[:500] if result.stderr else f"exit_code={result.returncode}",
            }
    except subprocess.TimeoutExpired:
        return {
            "oneshot_triggered": True,
            "oneshot_result": "timeout",
            "oneshot_error": f"sync did not complete within {timeout}s",
        }
    except Exception as exc:
        return {
            "oneshot_triggered": True,
            "oneshot_result": "error",
            "oneshot_error": str(exc),
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_sync_after_write(
    write_path: str,
    write_status: str,
) -> dict[str, Any]:
    """Check/trigger sync after a successful vault write.

    Called after vault_write returns success. Returns structured
    sync status for audit logging.

    Args:
        write_path: Vault-relative path that was written.
        write_status: The write result status ("created", "error", etc.).

    Returns:
        Dict with sync audit fields. Never raises.
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    # Base result
    result: dict[str, Any] = {
        "obsidian_sync_attempted": False,
        "obsidian_sync_trigger": write_path,
        "obsidian_sync_mode": "none",
        "obsidian_sync_result": "skipped",
        "obsidian_sync_error": None,
        "obsidian_sync_timestamp": timestamp,
    }

    try:
        # Only sync after successful writes
        if write_status != "created":
            result["obsidian_sync_result"] = "skipped"
            result["obsidian_sync_error"] = f"write_status={write_status} (not created)"
            return result

        # Load config
        config = _load_sync_config()

        if not config.get("enabled", False):
            result["obsidian_sync_result"] = "disabled"
            result["obsidian_sync_error"] = "sync not enabled in .nova-sync-config.json"
            return result

        ob_binary = config.get("ob_binary", "ob")
        vault_path = config.get("vault_path", "/home/nova/nova-vault")
        mode = config.get("mode", "continuous")
        result["obsidian_sync_mode"] = mode

        # Check if ob binary is available
        if not _check_ob_available(ob_binary):
            result["obsidian_sync_result"] = "unavailable"
            result["obsidian_sync_error"] = f"ob binary not found: {ob_binary}"
            return result

        result["obsidian_sync_attempted"] = True

        if mode == "continuous":
            # Continuous mode: daemon should pick up the file change automatically
            daemon_running = _is_daemon_running(ob_binary)
            if daemon_running:
                result["obsidian_sync_result"] = "daemon_active"
                result["obsidian_sync_error"] = None
                logger.info(
                    "VAULT SYNC: daemon active, %s will sync automatically",
                    write_path,
                )
            else:
                # Daemon not running — try oneshot fallback
                result["obsidian_sync_result"] = "daemon_inactive_fallback"
                logger.warning(
                    "VAULT SYNC: daemon not running, attempting oneshot for %s",
                    write_path,
                )
                oneshot = _trigger_oneshot_sync(
                    ob_binary,
                    vault_path,
                    config.get("sync_timeout_seconds", 30),
                )
                result.update(oneshot)
                if oneshot.get("oneshot_result") == "success":
                    result["obsidian_sync_result"] = "oneshot_success"
                else:
                    result["obsidian_sync_result"] = "oneshot_failed"
                    result["obsidian_sync_error"] = oneshot.get("oneshot_error")

        elif mode == "oneshot":
            # Oneshot mode: always trigger sync after each write
            oneshot = _trigger_oneshot_sync(
                ob_binary,
                vault_path,
                config.get("sync_timeout_seconds", 30),
            )
            result.update(oneshot)
            if oneshot.get("oneshot_result") == "success":
                result["obsidian_sync_result"] = "oneshot_success"
            else:
                result["obsidian_sync_result"] = "oneshot_failed"
                result["obsidian_sync_error"] = oneshot.get("oneshot_error")

        else:
            result["obsidian_sync_result"] = "unknown_mode"
            result["obsidian_sync_error"] = f"unrecognized sync mode: {mode}"

    except Exception as exc:
        # Fail-open: capture error, never raise
        result["obsidian_sync_result"] = "error"
        result["obsidian_sync_error"] = str(exc)
        logger.warning("VAULT SYNC ERROR: %s — %s", write_path, exc)

    return result
