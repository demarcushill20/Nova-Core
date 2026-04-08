"""Service staleness detector — alerts when running services are out of date with code on disk.

Compares systemd service ActiveEnterTimestamp against the most recent git commit
that touches files relevant to that service.  If commits exist after the service
started, the service is running stale code.

Stdlib only — no pip installs required.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # ~/nova-core

# Map service names to the code paths they run.
# If ANY file under these paths was committed after the service started,
# the service is stale.
SERVICE_CODE_PATHS: dict[str, list[str]] = {
    "novacore-novatrade": [
        "novatrade/",
        "configs/strategies/",
    ],
    "novacore-watcher": [
        "watcher.py",
    ],
    "novacore-telegram": [
        "telegram_bridge.py",
    ],
    "novacore-telegram-notifier": [
        "telegram_notifier.py",
    ],
}


@dataclass
class StalenessReport:
    """Result of a staleness check for one service."""

    service: str
    is_stale: bool
    service_started_at: datetime | None
    latest_commit_at: datetime | None
    commits_since_start: int
    stale_hours: float
    detail: str


def _get_service_start_time(service: str) -> datetime | None:
    """Get the ActiveEnterTimestamp from systemd as a UTC datetime."""
    try:
        result = subprocess.run(
            ["systemctl", "show", service, "--property=ActiveEnterTimestamp"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        line = result.stdout.strip()
        # Format: ActiveEnterTimestamp=Thu 2026-03-27 18:10:51 UTC
        if "=" not in line:
            return None
        ts_str = line.split("=", 1)[1].strip()
        if not ts_str:
            return None
        # Parse systemd timestamp — format varies but strptime handles common ones
        for fmt in (
            "%a %Y-%m-%d %H:%M:%S %Z",
            "%a %Y-%m-%d %H:%M:%S %z",
            "%Y-%m-%d %H:%M:%S %Z",
            "%Y-%m-%d %H:%M:%S %z",
        ):
            try:
                dt = datetime.strptime(ts_str, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
        return None
    except Exception:
        return None


def _is_service_active(service: str) -> bool:
    """Check if the service is currently active."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() == "active"
    except Exception:
        return False


def _get_latest_commit_time(paths: list[str]) -> tuple[datetime | None, int]:
    """Get the latest git commit time affecting any of the given paths.

    Returns (latest_commit_datetime, total_commits_count_since_epoch_0).
    The count is not bounded by a start time here — caller should filter.
    """
    if not paths:
        return None, 0
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%aI", "--"] + paths,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(BASE),
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None, 0
        dt = datetime.fromisoformat(result.stdout.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt, 0  # count filled separately
    except Exception:
        return None, 0


def _count_commits_since(since: datetime, paths: list[str]) -> int:
    """Count commits to paths since the given datetime."""
    try:
        since_str = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        result = subprocess.run(
            ["git", "rev-list", "--count", f"--since={since_str}", "HEAD", "--"] + paths,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(BASE),
        )
        if result.returncode != 0:
            return 0
        return int(result.stdout.strip())
    except Exception:
        return 0


def check_staleness(service: str) -> StalenessReport:
    """Check if a running service is stale relative to its code paths."""
    paths = SERVICE_CODE_PATHS.get(service, [])
    if not paths:
        return StalenessReport(
            service=service,
            is_stale=False,
            service_started_at=None,
            latest_commit_at=None,
            commits_since_start=0,
            stale_hours=0.0,
            detail=f"no code paths configured for {service}",
        )

    if not _is_service_active(service):
        return StalenessReport(
            service=service,
            is_stale=False,
            service_started_at=None,
            latest_commit_at=None,
            commits_since_start=0,
            stale_hours=0.0,
            detail=f"{service} is not active",
        )

    start_time = _get_service_start_time(service)
    if start_time is None:
        return StalenessReport(
            service=service,
            is_stale=False,
            service_started_at=None,
            latest_commit_at=None,
            commits_since_start=0,
            stale_hours=0.0,
            detail=f"could not determine start time for {service}",
        )

    latest_commit, _ = _get_latest_commit_time(paths)
    if latest_commit is None:
        return StalenessReport(
            service=service,
            is_stale=False,
            service_started_at=start_time,
            latest_commit_at=None,
            commits_since_start=0,
            stale_hours=0.0,
            detail=f"{service} — no git commits found for monitored paths",
        )

    commits_since = _count_commits_since(start_time, paths)
    is_stale = latest_commit > start_time and commits_since > 0

    if is_stale:
        now = datetime.now(timezone.utc)
        stale_hours = (now - start_time).total_seconds() / 3600
        detail = f"STALE — {commits_since} commit(s) since service started ({stale_hours:.1f}h ago); restart needed"
    else:
        stale_hours = 0.0
        detail = f"{service} is up to date"

    return StalenessReport(
        service=service,
        is_stale=is_stale,
        service_started_at=start_time,
        latest_commit_at=latest_commit,
        commits_since_start=commits_since,
        stale_hours=stale_hours,
        detail=detail,
    )


def check_all_staleness() -> list[StalenessReport]:
    """Check staleness for all configured services."""
    return [check_staleness(svc) for svc in SERVICE_CODE_PATHS]


# ---------------------------------------------------------------------------
# Auto-restart
# ---------------------------------------------------------------------------


@dataclass
class RestartResult:
    """Outcome of a service restart attempt."""

    service: str
    success: bool
    detail: str


def _watcher_has_active_tasks() -> bool:
    """Check if the watcher has active worker tasks (PID files in STATE/running/).

    Returns True if any PID file in STATE/running/ corresponds to a live process,
    meaning the watcher is actively executing tasks and should not be restarted.
    """
    running_dir = BASE / "STATE" / "running"
    if not running_dir.is_dir():
        return False
    for pid_file in running_dir.glob("*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            # Check if process is alive (signal 0 = existence check)
            import os

            os.kill(pid, 0)
            return True
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            continue
    return False


def restart_service(service: str) -> RestartResult:
    """Restart a systemd service.  Returns RestartResult.

    Uses ``systemctl restart`` without sudo — caller must run in a context
    that has permission (e.g. root, or a polkit rule, or a systemd unit
    without NoNewPrivileges).
    """
    if service not in SERVICE_CODE_PATHS:
        return RestartResult(service, False, f"unknown service: {service}")

    # Guard: don't restart the watcher while it has active tasks
    if service == "novacore-watcher" and _watcher_has_active_tasks():
        return RestartResult(
            service,
            False,
            "deferred — watcher has active tasks (PID files in STATE/running/)",
        )

    try:
        result = subprocess.run(
            ["systemctl", "restart", f"{service}.service"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()[:200]
            return RestartResult(service, False, f"restart failed (rc={result.returncode}): {stderr}")

        # Verify it came back
        if _is_service_active(service):
            return RestartResult(service, True, f"{service} restarted successfully")
        return RestartResult(service, False, "restart issued but service not active")
    except subprocess.TimeoutExpired:
        return RestartResult(service, False, "restart timed out (30s)")
    except Exception as e:
        return RestartResult(service, False, f"restart error: {e}")


def restart_stale_services(*, dry_run: bool = False) -> list[tuple[StalenessReport, RestartResult | None]]:
    """Check all services for staleness and restart any that are stale.

    Returns a list of (StalenessReport, RestartResult | None) tuples.
    RestartResult is None for services that are not stale.
    """
    results: list[tuple[StalenessReport, RestartResult | None]] = []
    for report in check_all_staleness():
        if report.is_stale:
            if dry_run:
                results.append((report, RestartResult(report.service, True, "dry-run — would restart")))
            else:
                rr = restart_service(report.service)
                results.append((report, rr))
        else:
            results.append((report, None))
    return results
