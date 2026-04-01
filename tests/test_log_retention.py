"""Tests for scripts/log_retention.py — focused on the STATE cleanup policies."""

from __future__ import annotations

import gzip
import json

# Import cleanup functions
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from log_retention import (
    prune_old_improvement_runs,
    prune_old_investigations,
    prune_old_notified,
)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Create a mock STATE directory."""
    sd = tmp_path / "STATE"
    sd.mkdir()
    return sd


def _make_old_file(p: Path, age_days: float, content: str = "{}") -> None:
    """Create a file and backdate its mtime."""
    p.write_text(content)
    old_time = time.time() - (age_days * 86400)
    import os

    os.utime(p, (old_time, old_time))


# --- prune_old_investigations ---


class TestPruneInvestigations:
    def test_archives_old_files(self, state_dir: Path):
        inv = state_dir / "investigations"
        inv.mkdir()
        archive = state_dir / "_archive" / "investigations"

        # 5 old files (4 days old), 2 recent files (1 day old)
        for i in range(5):
            _make_old_file(inv / f"inv_old_{i}.json", 4.0, json.dumps({"id": i}))
        for i in range(2):
            _make_old_file(inv / f"inv_new_{i}.json", 1.0, json.dumps({"id": 100 + i}))

        with patch("log_retention.STATE_DIR", state_dir):
            actions = prune_old_investigations()

        # Old files archived, new files kept
        remaining = list(inv.glob("*.json"))
        assert len(remaining) == 2
        assert all("new" in f.name for f in remaining)

        # Archive created
        archives = list(archive.glob("*.jsonl.gz"))
        assert len(archives) == 1
        assert actions and "archived 5" in actions[0]

    def test_dry_run_no_changes(self, state_dir: Path):
        inv = state_dir / "investigations"
        inv.mkdir()
        for i in range(3):
            _make_old_file(inv / f"inv_old_{i}.json", 5.0)

        with patch("log_retention.STATE_DIR", state_dir):
            actions = prune_old_investigations(dry_run=True)

        # Files still present
        assert len(list(inv.glob("*.json"))) == 3
        assert actions and "[dry-run]" in actions[0]

    def test_no_old_files_no_action(self, state_dir: Path):
        inv = state_dir / "investigations"
        inv.mkdir()
        for i in range(3):
            _make_old_file(inv / f"inv_recent_{i}.json", 0.5)

        with patch("log_retention.STATE_DIR", state_dir):
            actions = prune_old_investigations()

        assert len(actions) == 0
        assert len(list(inv.glob("*.json"))) == 3

    def test_missing_dir_no_error(self, state_dir: Path):
        with patch("log_retention.STATE_DIR", state_dir):
            actions = prune_old_investigations()
        assert actions == []

    def test_archive_is_valid_gzip(self, state_dir: Path):
        inv = state_dir / "investigations"
        inv.mkdir()
        for i in range(3):
            _make_old_file(inv / f"inv_{i}.json", 5.0, json.dumps({"index": i}))

        with patch("log_retention.STATE_DIR", state_dir):
            prune_old_investigations()

        archive_dir = state_dir / "_archive" / "investigations"
        archives = list(archive_dir.glob("*.jsonl.gz"))
        assert len(archives) == 1

        # Decompress and verify content
        with gzip.open(archives[0], "rt") as gz:
            lines = gz.readlines()
        assert len(lines) == 3
        for line in lines:
            obj = json.loads(line.strip())
            assert "index" in obj


# --- prune_old_notified ---


class TestPruneNotified:
    def test_deletes_old_markers(self, state_dir: Path):
        notif = state_dir / "notified"
        notif.mkdir()
        for i in range(5):
            _make_old_file(notif / f"task_{i}.notified", 10.0, "sent")
        for i in range(3):
            _make_old_file(notif / f"task_new_{i}.notified", 1.0, "sent")

        with patch("log_retention.STATE_DIR", state_dir):
            actions = prune_old_notified()

        remaining = list(notif.iterdir())
        assert len(remaining) == 3
        assert actions and "deleted 5" in actions[0]

    def test_dry_run_no_deletions(self, state_dir: Path):
        notif = state_dir / "notified"
        notif.mkdir()
        for i in range(4):
            _make_old_file(notif / f"old_{i}.notified", 10.0, "sent")

        with patch("log_retention.STATE_DIR", state_dir):
            actions = prune_old_notified(dry_run=True)

        assert len(list(notif.iterdir())) == 4
        assert actions and "[dry-run]" in actions[0]

    def test_no_old_markers_no_action(self, state_dir: Path):
        notif = state_dir / "notified"
        notif.mkdir()
        for i in range(3):
            _make_old_file(notif / f"recent_{i}.notified", 2.0, "sent")

        with patch("log_retention.STATE_DIR", state_dir):
            actions = prune_old_notified()

        assert len(actions) == 0


# --- prune_old_improvement_runs ---


class TestPruneImprovementRuns:
    def test_deletes_old_runs(self, state_dir: Path):
        imp = state_dir / "improvement_runs"
        imp.mkdir()
        for i in range(6):
            _make_old_file(imp / f"imp_{i}.json", 10.0, json.dumps({"run": i}))
        for i in range(2):
            _make_old_file(imp / f"imp_new_{i}.json", 2.0, json.dumps({"run": 100 + i}))

        with patch("log_retention.STATE_DIR", state_dir):
            actions = prune_old_improvement_runs()

        remaining = list(imp.glob("*.json"))
        assert len(remaining) == 2
        assert actions and "deleted 6" in actions[0]

    def test_dry_run_no_changes(self, state_dir: Path):
        imp = state_dir / "improvement_runs"
        imp.mkdir()
        for i in range(4):
            _make_old_file(imp / f"imp_{i}.json", 10.0)

        with patch("log_retention.STATE_DIR", state_dir):
            actions = prune_old_improvement_runs(dry_run=True)

        assert len(list(imp.glob("*.json"))) == 4
        assert actions and "[dry-run]" in actions[0]

    def test_missing_dir_no_error(self, state_dir: Path):
        with patch("log_retention.STATE_DIR", state_dir):
            actions = prune_old_improvement_runs()
        assert actions == []
