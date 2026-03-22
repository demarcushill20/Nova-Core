"""Tests for utils.quota_manager — quota-aware task scheduling."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from utils.quota_manager import (
    SCHEDULE_MODES,
    QuotaStatus,
    check_emergency_brake,
    determine_schedule_mode,
    estimate_task_tokens,
    filter_tasks_for_quota,
    get_quota_status,
    update_quota_tracking,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def quota_file(tmp_path):
    """Provide a temporary quota status file and patch QUOTA_FILE to use it."""
    qf = tmp_path / "quota_status.json"
    with patch("utils.quota_manager.QUOTA_FILE", qf):
        yield qf


@pytest.fixture
def _make_quota_data():
    """Factory for quota JSON data."""

    def _factory(
        tokens_used=50000,
        tokens_remaining=150000,
        hours_until_reset=12,
        usage_rate=5000.0,
        projected_exhaustion_hours=30.0,
    ):
        now = datetime.now(timezone.utc)
        reset = now + timedelta(hours=hours_until_reset)
        proj = now + timedelta(hours=projected_exhaustion_hours) if projected_exhaustion_hours else None
        return {
            "tokens_used": tokens_used,
            "tokens_remaining": tokens_remaining,
            "reset_time": reset.isoformat(),
            "usage_rate": usage_rate,
            "projected_exhaustion": proj.isoformat() if proj else None,
            "last_updated": now.isoformat(),
        }

    return _factory


# ---------------------------------------------------------------------------
# get_quota_status
# ---------------------------------------------------------------------------


class TestGetQuotaStatus:
    def test_returns_none_when_file_missing(self, quota_file):
        assert get_quota_status() is None

    def test_returns_none_on_bad_json(self, quota_file):
        quota_file.write_text("not json", encoding="utf-8")
        assert get_quota_status() is None

    def test_returns_none_on_missing_keys(self, quota_file):
        quota_file.write_text('{"foo": 1}', encoding="utf-8")
        assert get_quota_status() is None

    def test_parses_valid_data(self, quota_file, _make_quota_data):
        data = _make_quota_data(tokens_used=60000, tokens_remaining=140000)
        quota_file.write_text(json.dumps(data), encoding="utf-8")

        status = get_quota_status()
        assert status is not None
        assert status.tokens_used == 60000
        assert status.tokens_remaining == 140000

    def test_handles_null_projected_exhaustion(self, quota_file, _make_quota_data):
        data = _make_quota_data(projected_exhaustion_hours=None)
        quota_file.write_text(json.dumps(data), encoding="utf-8")

        status = get_quota_status()
        assert status is not None
        assert status.projected_exhaustion is None


# ---------------------------------------------------------------------------
# determine_schedule_mode
# ---------------------------------------------------------------------------


class TestDetermineScheduleMode:
    def test_conservative_when_no_data(self):
        assert determine_schedule_mode(None) == "conservative"

    def test_conservative_when_low_pressure(self):
        now = datetime.now(timezone.utc)
        # 25% tokens used, 50% time elapsed → ratio ~0.5
        status = QuotaStatus(
            tokens_used=50000,
            tokens_remaining=150000,
            reset_time=now + timedelta(hours=12),
            usage_rate=4000.0,
            projected_exhaustion=now + timedelta(hours=36),
        )
        assert determine_schedule_mode(status) == "conservative"

    def test_moderate_when_medium_pressure(self):
        now = datetime.now(timezone.utc)
        # 60% tokens used, 50% time elapsed → ratio ~1.2
        status = QuotaStatus(
            tokens_used=120000,
            tokens_remaining=80000,
            reset_time=now + timedelta(hours=12),
            usage_rate=10000.0,
            projected_exhaustion=now + timedelta(hours=8),
        )
        assert determine_schedule_mode(status) == "moderate"

    def test_aggressive_when_high_pressure(self):
        now = datetime.now(timezone.utc)
        # 90% tokens used, 50% time elapsed → ratio ~1.8
        status = QuotaStatus(
            tokens_used=180000,
            tokens_remaining=20000,
            reset_time=now + timedelta(hours=12),
            usage_rate=15000.0,
            projected_exhaustion=now + timedelta(hours=1.3),
        )
        assert determine_schedule_mode(status) == "aggressive"

    def test_conservative_when_reset_imminent(self):
        now = datetime.now(timezone.utc)
        status = QuotaStatus(
            tokens_used=180000,
            tokens_remaining=20000,
            reset_time=now - timedelta(minutes=5),  # reset already due
            usage_rate=15000.0,
            projected_exhaustion=now + timedelta(hours=1),
        )
        assert determine_schedule_mode(status) == "conservative"


# ---------------------------------------------------------------------------
# estimate_task_tokens
# ---------------------------------------------------------------------------


class TestEstimateTaskTokens:
    def test_high_complexity(self):
        text = "---\npriority: high\n---\nImplement new feature with deep research"
        assert estimate_task_tokens(text) == 10000

    def test_medium_complexity(self):
        text = "---\npriority: medium\n---\nReview the pull request"
        assert estimate_task_tokens(text) == 5000

    def test_low_complexity(self):
        text = "---\npriority: low\n---\nSend a status message"
        assert estimate_task_tokens(text) == 2000

    def test_truncates_to_2048(self):
        # Even with a long body, only first 2048 chars checked
        text = "simple task\n" + "x" * 3000
        assert estimate_task_tokens(text) == 2000


# ---------------------------------------------------------------------------
# filter_tasks_for_quota
# ---------------------------------------------------------------------------


class TestFilterTasksForQuota:
    @pytest.fixture
    def task_files(self, tmp_path):
        """Create a set of task files with varying priorities."""
        files = []
        for name, content in [
            ("critical.md", "---\npriority: high\n---\nCRITICAL: Fix prod"),
            ("normal1.md", "---\npriority: medium\n---\nReview code"),
            ("normal2.md", "---\npriority: medium\n---\nUpdate docs"),
            ("low.md", "---\npriority: low\n---\nCleanup old files"),
            ("research.md", "---\n---\nResearch new framework"),
        ]:
            f = tmp_path / name
            f.write_text(content, encoding="utf-8")
            files.append(f)
        return files

    def test_conservative_accepts_up_to_3(self, task_files, quota_file):
        # No quota file → conservative mode (max 3)
        accepted, deferred = filter_tasks_for_quota(task_files)
        assert len(accepted) == 3
        assert len(deferred) == 2

    def test_aggressive_only_critical(self, task_files, quota_file, _make_quota_data):
        # Write aggressive-triggering quota data
        data = _make_quota_data(tokens_used=180000, tokens_remaining=20000, usage_rate=15000.0)
        quota_file.write_text(json.dumps(data), encoding="utf-8")

        accepted, deferred = filter_tasks_for_quota(task_files)
        # Only critical.md should be accepted
        assert len(accepted) == 1
        assert accepted[0].name == "critical.md"
        assert len(deferred) == 4

    def test_empty_input(self, quota_file):
        accepted, deferred = filter_tasks_for_quota([])
        assert accepted == []
        assert deferred == []


# ---------------------------------------------------------------------------
# check_emergency_brake
# ---------------------------------------------------------------------------


class TestCheckEmergencyBrake:
    def test_false_when_no_data(self, quota_file):
        assert check_emergency_brake() is False

    def test_true_when_tokens_very_low(self, quota_file, _make_quota_data):
        data = _make_quota_data(tokens_used=196000, tokens_remaining=4000)
        quota_file.write_text(json.dumps(data), encoding="utf-8")
        assert check_emergency_brake() is True

    def test_true_when_exhaustion_imminent(self, quota_file, _make_quota_data):
        data = _make_quota_data(
            tokens_used=150000,
            tokens_remaining=50000,
            projected_exhaustion_hours=0.5,
        )
        quota_file.write_text(json.dumps(data), encoding="utf-8")
        assert check_emergency_brake() is True

    def test_false_when_plenty_remaining(self, quota_file, _make_quota_data):
        data = _make_quota_data(
            tokens_used=50000,
            tokens_remaining=150000,
            projected_exhaustion_hours=30.0,
        )
        quota_file.write_text(json.dumps(data), encoding="utf-8")
        assert check_emergency_brake() is False


# ---------------------------------------------------------------------------
# update_quota_tracking
# ---------------------------------------------------------------------------


class TestUpdateQuotaTracking:
    def test_creates_file(self, quota_file):
        update_quota_tracking(80000)
        assert quota_file.exists()
        data = json.loads(quota_file.read_text(encoding="utf-8"))
        assert data["tokens_used"] == 80000
        assert data["tokens_remaining"] == 120000  # 200k - 80k
        assert "reset_time" in data
        assert "usage_rate" in data

    def test_creates_parent_dir(self, tmp_path):
        nested = tmp_path / "deep" / "quota_status.json"
        with patch("utils.quota_manager.QUOTA_FILE", nested):
            update_quota_tracking(10000)
        assert nested.exists()


# ---------------------------------------------------------------------------
# Schedule modes sanity
# ---------------------------------------------------------------------------


class TestScheduleModes:
    def test_all_modes_defined(self):
        assert set(SCHEDULE_MODES.keys()) == {"conservative", "moderate", "aggressive"}

    def test_conservative_most_permissive(self):
        assert SCHEDULE_MODES["conservative"].max_tasks_per_scan >= SCHEDULE_MODES["aggressive"].max_tasks_per_scan

    def test_aggressive_defers_non_critical(self):
        assert SCHEDULE_MODES["aggressive"].defer_non_critical is True

    def test_conservative_does_not_defer(self):
        assert SCHEDULE_MODES["conservative"].defer_non_critical is False
