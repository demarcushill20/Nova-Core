"""Tests for utils/service_staleness.py — service staleness detector."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from utils.service_staleness import (
    SERVICE_CODE_PATHS,
    RestartResult,
    StalenessReport,
    _count_commits_since,
    _get_latest_commit_time,
    _get_service_start_time,
    _is_service_active,
    _watcher_has_active_tasks,
    check_all_staleness,
    check_staleness,
    restart_service,
    restart_stale_services,
)

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _mock_run(stdout: str = "", returncode: int = 0):
    """Create a mock subprocess.run result."""
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


NOW = datetime(2026, 3, 30, 0, 0, 0, tzinfo=timezone.utc)
TWO_DAYS_AGO = NOW - timedelta(days=2)
ONE_HOUR_AGO = NOW - timedelta(hours=1)


# ---------------------------------------------------------------------------
# _get_service_start_time
# ---------------------------------------------------------------------------


class TestGetServiceStartTime:
    @patch("utils.service_staleness.subprocess.run")
    def test_parses_systemd_timestamp(self, mock_run):
        mock_run.return_value = _mock_run("ActiveEnterTimestamp=Thu 2026-03-27 18:10:51 UTC")
        result = _get_service_start_time("novacore-telegram")
        assert result is not None
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 27

    @patch("utils.service_staleness.subprocess.run")
    def test_returns_none_on_empty(self, mock_run):
        mock_run.return_value = _mock_run("ActiveEnterTimestamp=")
        result = _get_service_start_time("novacore-telegram")
        assert result is None

    @patch("utils.service_staleness.subprocess.run")
    def test_returns_none_on_failure(self, mock_run):
        mock_run.return_value = _mock_run("", returncode=1)
        result = _get_service_start_time("novacore-telegram")
        assert result is None

    @patch("utils.service_staleness.subprocess.run")
    def test_handles_exception(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("cmd", 10)
        result = _get_service_start_time("novacore-telegram")
        assert result is None


# ---------------------------------------------------------------------------
# _is_service_active
# ---------------------------------------------------------------------------


class TestIsServiceActive:
    @patch("utils.service_staleness.subprocess.run")
    def test_active(self, mock_run):
        mock_run.return_value = _mock_run("active")
        assert _is_service_active("test-svc") is True

    @patch("utils.service_staleness.subprocess.run")
    def test_inactive(self, mock_run):
        mock_run.return_value = _mock_run("inactive")
        assert _is_service_active("test-svc") is False

    @patch("utils.service_staleness.subprocess.run")
    def test_exception(self, mock_run):
        mock_run.side_effect = Exception("fail")
        assert _is_service_active("test-svc") is False


# ---------------------------------------------------------------------------
# _get_latest_commit_time
# ---------------------------------------------------------------------------


class TestGetLatestCommitTime:
    @patch("utils.service_staleness.subprocess.run")
    def test_parses_iso_timestamp(self, mock_run):
        mock_run.return_value = _mock_run("2026-03-28T10:21:04+00:00")
        dt, _ = _get_latest_commit_time(["novatrade/"])
        assert dt is not None
        assert dt.day == 28

    @patch("utils.service_staleness.subprocess.run")
    def test_empty_paths(self, mock_run):
        dt, count = _get_latest_commit_time([])
        assert dt is None
        mock_run.assert_not_called()

    @patch("utils.service_staleness.subprocess.run")
    def test_no_commits(self, mock_run):
        mock_run.return_value = _mock_run("", returncode=0)
        dt, _ = _get_latest_commit_time(["nonexistent/"])
        assert dt is None


# ---------------------------------------------------------------------------
# _count_commits_since
# ---------------------------------------------------------------------------


class TestCountCommitsSince:
    @patch("utils.service_staleness.subprocess.run")
    def test_returns_count(self, mock_run):
        mock_run.return_value = _mock_run("7")
        count = _count_commits_since(TWO_DAYS_AGO, ["novatrade/"])
        assert count == 7

    @patch("utils.service_staleness.subprocess.run")
    def test_returns_zero_on_error(self, mock_run):
        mock_run.return_value = _mock_run("", returncode=1)
        count = _count_commits_since(TWO_DAYS_AGO, ["novatrade/"])
        assert count == 0


# ---------------------------------------------------------------------------
# check_staleness (integration-style with mocks)
# ---------------------------------------------------------------------------


class TestCheckStaleness:
    @patch("utils.service_staleness._count_commits_since", return_value=5)
    @patch("utils.service_staleness._get_latest_commit_time", return_value=(NOW - timedelta(hours=1), 0))
    @patch("utils.service_staleness._get_service_start_time", return_value=TWO_DAYS_AGO)
    @patch("utils.service_staleness._is_service_active", return_value=True)
    def test_detects_stale_service(self, mock_active, mock_start, mock_commit, mock_count):
        report = check_staleness("novacore-telegram")
        assert report.is_stale is True
        assert report.commits_since_start == 5
        assert "STALE" in report.detail
        assert report.stale_hours > 0

    @patch("utils.service_staleness._count_commits_since", return_value=0)
    @patch("utils.service_staleness._get_latest_commit_time", return_value=(TWO_DAYS_AGO - timedelta(hours=1), 0))
    @patch("utils.service_staleness._get_service_start_time", return_value=TWO_DAYS_AGO)
    @patch("utils.service_staleness._is_service_active", return_value=True)
    def test_fresh_service(self, mock_active, mock_start, mock_commit, mock_count):
        report = check_staleness("novacore-telegram")
        assert report.is_stale is False
        assert "up to date" in report.detail

    @patch("utils.service_staleness._is_service_active", return_value=False)
    def test_inactive_service(self, mock_active):
        report = check_staleness("novacore-telegram")
        assert report.is_stale is False
        assert "not active" in report.detail

    def test_unknown_service(self):
        report = check_staleness("unknown-service")
        assert report.is_stale is False
        assert "no code paths configured" in report.detail

    def test_novatrade_excluded_from_auto_deploy(self):
        """novatrade is intentionally excluded — restarts must be manual."""
        report = check_staleness("novacore-novatrade")
        assert report.is_stale is False
        assert "no code paths configured" in report.detail

    @patch("utils.service_staleness._get_latest_commit_time", return_value=(None, 0))
    @patch("utils.service_staleness._get_service_start_time", return_value=TWO_DAYS_AGO)
    @patch("utils.service_staleness._is_service_active", return_value=True)
    def test_no_commits_found(self, mock_active, mock_start, mock_commit):
        report = check_staleness("novacore-telegram")
        assert report.is_stale is False
        assert "no git commits" in report.detail

    @patch("utils.service_staleness._get_service_start_time", return_value=None)
    @patch("utils.service_staleness._is_service_active", return_value=True)
    def test_no_start_time(self, mock_active, mock_start):
        report = check_staleness("novacore-telegram")
        assert report.is_stale is False
        assert "could not determine" in report.detail


# ---------------------------------------------------------------------------
# check_all_staleness
# ---------------------------------------------------------------------------


class TestCheckAllStaleness:
    @patch("utils.service_staleness.check_staleness")
    def test_checks_all_configured_services(self, mock_check):
        mock_check.return_value = StalenessReport(
            service="test",
            is_stale=False,
            service_started_at=None,
            latest_commit_at=None,
            commits_since_start=0,
            stale_hours=0.0,
            detail="ok",
        )
        reports = check_all_staleness()
        assert len(reports) == len(SERVICE_CODE_PATHS)
        assert mock_check.call_count == len(SERVICE_CODE_PATHS)


# ---------------------------------------------------------------------------
# StalenessReport dataclass
# ---------------------------------------------------------------------------


class TestStalenessReport:
    def test_dataclass_fields(self):
        r = StalenessReport(
            service="test-svc",
            is_stale=True,
            service_started_at=TWO_DAYS_AGO,
            latest_commit_at=ONE_HOUR_AGO,
            commits_since_start=3,
            stale_hours=47.0,
            detail="STALE — 3 commits",
        )
        assert r.service == "test-svc"
        assert r.is_stale is True
        assert r.commits_since_start == 3
        assert r.stale_hours == 47.0


# ---------------------------------------------------------------------------
# SERVICE_CODE_PATHS configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_novatrade_excluded(self):
        """novatrade must NOT be in the auto-deploy list — it is a stateful
        live trading process and restarts must remain manual."""
        assert "novacore-novatrade" not in SERVICE_CODE_PATHS

    def test_watcher_paths_configured(self):
        assert "novacore-watcher" in SERVICE_CODE_PATHS
        assert "watcher.py" in SERVICE_CODE_PATHS["novacore-watcher"]

    def test_all_services_have_paths(self):
        for svc, paths in SERVICE_CODE_PATHS.items():
            assert len(paths) > 0, f"{svc} has no code paths configured"


# ---------------------------------------------------------------------------
# restart_service
# ---------------------------------------------------------------------------


class TestRestartService:
    def test_unknown_service(self):
        result = restart_service("unknown-service")
        assert result.success is False
        assert "unknown" in result.detail

    def test_novatrade_unknown_to_auto_deploy(self):
        """novatrade is excluded from SERVICE_CODE_PATHS, so restart_service refuses it."""
        result = restart_service("novacore-novatrade")
        assert result.success is False
        assert "unknown" in result.detail

    @patch("utils.service_staleness._is_service_active", return_value=True)
    @patch("utils.service_staleness.subprocess.run")
    def test_successful_restart(self, mock_run, mock_active):
        mock_run.return_value = _mock_run("")
        result = restart_service("novacore-telegram")
        assert result.success is True
        assert "restarted successfully" in result.detail

    @patch("utils.service_staleness.subprocess.run")
    def test_restart_failure(self, mock_run):
        mock_run.return_value = _mock_run("access denied", returncode=1)
        result = restart_service("novacore-telegram")
        assert result.success is False
        assert "failed" in result.detail

    @patch("utils.service_staleness.subprocess.run")
    def test_restart_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("cmd", 30)
        result = restart_service("novacore-telegram")
        assert result.success is False
        assert "timed out" in result.detail

    @patch("utils.service_staleness._is_service_active", return_value=False)
    @patch("utils.service_staleness.subprocess.run")
    def test_restart_issued_but_not_active(self, mock_run, mock_active):
        mock_run.return_value = _mock_run("")
        result = restart_service("novacore-telegram")
        assert result.success is False
        assert "not active" in result.detail


# ---------------------------------------------------------------------------
# restart_stale_services
# ---------------------------------------------------------------------------


class TestRestartStaleServices:
    @patch("utils.service_staleness.check_all_staleness")
    def test_dry_run_does_not_restart(self, mock_check):
        mock_check.return_value = [
            StalenessReport(
                service="novacore-telegram",
                is_stale=True,
                service_started_at=TWO_DAYS_AGO,
                latest_commit_at=ONE_HOUR_AGO,
                commits_since_start=3,
                stale_hours=47.0,
                detail="STALE",
            )
        ]
        results = restart_stale_services(dry_run=True)
        assert len(results) == 1
        report, rr = results[0]
        assert report.is_stale is True
        assert rr is not None
        assert rr.success is True
        assert "dry-run" in rr.detail

    @patch("utils.service_staleness.check_all_staleness")
    def test_fresh_services_not_restarted(self, mock_check):
        mock_check.return_value = [
            StalenessReport(
                service="novacore-watcher",
                is_stale=False,
                service_started_at=TWO_DAYS_AGO,
                latest_commit_at=None,
                commits_since_start=0,
                stale_hours=0.0,
                detail="up to date",
            )
        ]
        results = restart_stale_services()
        assert len(results) == 1
        _, rr = results[0]
        assert rr is None

    @patch("utils.service_staleness.restart_service")
    @patch("utils.service_staleness.check_all_staleness")
    def test_stale_services_restarted(self, mock_check, mock_restart):
        mock_check.return_value = [
            StalenessReport(
                service="novacore-telegram",
                is_stale=True,
                service_started_at=TWO_DAYS_AGO,
                latest_commit_at=ONE_HOUR_AGO,
                commits_since_start=5,
                stale_hours=47.0,
                detail="STALE",
            )
        ]
        mock_restart.return_value = RestartResult("novacore-telegram", True, "ok")
        results = restart_stale_services()
        assert len(results) == 1
        _, rr = results[0]
        assert rr is not None
        assert rr.success is True
        mock_restart.assert_called_once_with("novacore-telegram")


# ---------------------------------------------------------------------------
# RestartResult dataclass
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _watcher_has_active_tasks
# ---------------------------------------------------------------------------


class TestWatcherActiveTaskGuard:
    """Guard that prevents watcher restart when tasks are in-flight."""

    def test_no_running_dir(self, tmp_path):
        with patch("utils.service_staleness.BASE", tmp_path):
            assert _watcher_has_active_tasks() is False

    def test_empty_running_dir(self, tmp_path):
        (tmp_path / "STATE" / "running").mkdir(parents=True)
        with patch("utils.service_staleness.BASE", tmp_path):
            assert _watcher_has_active_tasks() is False

    def test_pid_file_with_dead_process(self, tmp_path):
        running_dir = tmp_path / "STATE" / "running"
        running_dir.mkdir(parents=True)
        (running_dir / "test_task.pid").write_text("999999999")  # unlikely PID
        with patch("utils.service_staleness.BASE", tmp_path):
            assert _watcher_has_active_tasks() is False

    def test_pid_file_with_live_process(self, tmp_path):
        import os

        running_dir = tmp_path / "STATE" / "running"
        running_dir.mkdir(parents=True)
        (running_dir / "test_task.pid").write_text(str(os.getpid()))
        with patch("utils.service_staleness.BASE", tmp_path):
            assert _watcher_has_active_tasks() is True

    def test_restart_deferred_when_active_tasks(self):
        with patch("utils.service_staleness._watcher_has_active_tasks", return_value=True):
            result = restart_service("novacore-watcher")
            assert result.success is False
            assert "deferred" in result.detail
            assert "active tasks" in result.detail

    def test_restart_proceeds_when_no_active_tasks(self):
        with (
            patch("utils.service_staleness._watcher_has_active_tasks", return_value=False),
            patch("subprocess.run") as mock_run,
            patch("utils.service_staleness._is_service_active", return_value=True),
        ):
            mock_run.return_value = _mock_run(returncode=0)
            result = restart_service("novacore-watcher")
            assert result.success is True

    def test_guard_only_applies_to_watcher(self):
        """Other services should not be affected by the active-task guard."""
        with (
            patch("utils.service_staleness._watcher_has_active_tasks", return_value=True),
            patch("subprocess.run") as mock_run,
            patch("utils.service_staleness._is_service_active", return_value=True),
        ):
            mock_run.return_value = _mock_run(returncode=0)
            result = restart_service("novacore-telegram")
            assert result.success is True


class TestRestartResult:
    def test_dataclass_fields(self):
        r = RestartResult("test-svc", True, "restarted ok")
        assert r.service == "test-svc"
        assert r.success is True
        assert r.detail == "restarted ok"
