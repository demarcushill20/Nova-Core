"""Tests for scripts/state_hygiene.py — state trimming and cleanup."""

from __future__ import annotations

# Import the module under test
import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

# Add scripts to path for import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
state_hygiene = importlib.import_module("state_hygiene")


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Create a temporary STATE directory with test data."""
    state = tmp_path / "STATE"
    state.mkdir()
    monkeypatch.setattr(state_hygiene, "STATE", state)
    monkeypatch.setattr(state_hygiene, "BASE", tmp_path)
    return state


class TestTrimJsonl:
    def test_trims_to_retention_limit(self, tmp_state: Path):
        """JSONL file with more lines than retention is trimmed."""
        f = tmp_state / "test.jsonl"
        lines = [json.dumps({"n": i}) for i in range(50)]
        f.write_text("\n".join(lines) + "\n")

        result = state_hygiene.trim_jsonl(f, keep_lines=10, apply=True)

        assert result is not None
        assert result.before_lines == 50
        assert result.after_lines == 10
        assert result.bytes_saved > 0

        # Verify last 10 lines are preserved
        kept = f.read_text().strip().splitlines()
        assert len(kept) == 10
        assert json.loads(kept[0])["n"] == 40
        assert json.loads(kept[-1])["n"] == 49

    def test_skips_within_limit(self, tmp_state: Path):
        """JSONL file within retention limit is not trimmed."""
        f = tmp_state / "small.jsonl"
        lines = [json.dumps({"n": i}) for i in range(5)]
        f.write_text("\n".join(lines) + "\n")

        result = state_hygiene.trim_jsonl(f, keep_lines=10, apply=True)
        assert result is None

    def test_skips_missing_file(self, tmp_state: Path):
        """Missing file returns None."""
        result = state_hygiene.trim_jsonl(tmp_state / "missing.jsonl", keep_lines=10, apply=True)
        assert result is None

    def test_dry_run_no_modification(self, tmp_state: Path):
        """Dry run calculates savings but does not modify the file."""
        f = tmp_state / "test.jsonl"
        lines = [json.dumps({"n": i}) for i in range(50)]
        content = "\n".join(lines) + "\n"
        f.write_text(content)

        result = state_hygiene.trim_jsonl(f, keep_lines=10, apply=False)

        assert result is not None
        assert result.bytes_saved > 0
        # File unchanged
        assert f.read_text() == content

    def test_atomic_write(self, tmp_state: Path):
        """Trimming uses atomic write (no partial file on crash)."""
        f = tmp_state / "test.jsonl"
        lines = [json.dumps({"n": i}) for i in range(100)]
        f.write_text("\n".join(lines) + "\n")

        state_hygiene.trim_jsonl(f, keep_lines=20, apply=True)

        # No temp files left
        tmp_files = list(tmp_state.glob(".*_*.tmp"))
        assert len(tmp_files) == 0


class TestCleanOldFiles:
    def test_removes_old_files(self, tmp_state: Path):
        """Files older than max_age_days are removed."""
        d = tmp_state / "notified"
        d.mkdir()

        # Create old files (mtime = 10 days ago)
        old_time = time.time() - 10 * 86400
        for i in range(5):
            f = d / f"old_{i}.notified"
            f.write_text("x")
            os.utime(f, (old_time, old_time))

        # Create recent files
        for i in range(3):
            f = d / f"new_{i}.notified"
            f.write_text("x")

        result = state_hygiene.clean_old_files(d, max_age_days=3, apply=True)

        assert result is not None
        assert result.files_removed == 5
        # Recent files still exist
        remaining = list(d.iterdir())
        assert len(remaining) == 3

    def test_skips_when_nothing_old(self, tmp_state: Path):
        """No files to clean returns None."""
        d = tmp_state / "notified"
        d.mkdir()

        f = d / "recent.notified"
        f.write_text("x")

        result = state_hygiene.clean_old_files(d, max_age_days=3, apply=True)
        assert result is None

    def test_skips_missing_dir(self, tmp_state: Path):
        """Missing directory returns None."""
        result = state_hygiene.clean_old_files(tmp_state / "nonexistent", max_age_days=3, apply=True)
        assert result is None

    def test_dry_run_preserves_files(self, tmp_state: Path):
        """Dry run does not remove files."""
        d = tmp_state / "notified"
        d.mkdir()
        old_time = time.time() - 10 * 86400
        f = d / "old.notified"
        f.write_text("x")
        os.utime(f, (old_time, old_time))

        result = state_hygiene.clean_old_files(d, max_age_days=3, apply=False)

        assert result is not None
        assert result.files_removed == 1
        assert f.exists()  # not deleted


class TestTrimArchiveJson:
    def test_trims_large_archive(self, tmp_state: Path):
        """JSON array exceeding max_bytes is trimmed from front."""
        f = tmp_state / "archive.json"
        data = [{"entry": i, "padding": "x" * 100} for i in range(200)]
        f.write_text(json.dumps(data))

        result = state_hygiene.trim_archive_json(f, max_bytes=4096, apply=True)

        assert result is not None
        assert result.before_lines == 200
        assert result.after_lines < 200
        assert result.bytes_saved > 0

        # Verify it's valid JSON and under budget
        trimmed = json.loads(f.read_text())
        assert isinstance(trimmed, list)
        assert f.stat().st_size <= 4096

    def test_skips_within_limit(self, tmp_state: Path):
        """Archive within max_bytes is not trimmed."""
        f = tmp_state / "small.json"
        f.write_text(json.dumps([{"x": 1}]))

        result = state_hygiene.trim_archive_json(f, max_bytes=4096, apply=True)
        assert result is None

    def test_preserves_newest_entries(self, tmp_state: Path):
        """Trimming removes from front (oldest), keeps end (newest)."""
        f = tmp_state / "archive.json"
        data = [{"seq": i, "pad": "z" * 50} for i in range(100)]
        f.write_text(json.dumps(data))

        state_hygiene.trim_archive_json(f, max_bytes=2048, apply=True)

        trimmed = json.loads(f.read_text())
        # Newest entries preserved
        assert trimmed[-1]["seq"] == 99

    def test_skips_non_array(self, tmp_state: Path):
        """Non-array JSON is not trimmed."""
        f = tmp_state / "obj.json"
        f.write_text(json.dumps({"key": "value"}))

        result = state_hygiene.trim_archive_json(f, max_bytes=10, apply=True)
        assert result is None


class TestRunHygiene:
    def test_full_run_dry(self, tmp_state: Path, monkeypatch: pytest.MonkeyPatch):
        """Full dry run returns results without modifying anything."""
        monkeypatch.setattr(state_hygiene, "JSONL_RETENTION", {"test.jsonl": 5})
        monkeypatch.setattr(state_hygiene, "DIR_RETENTION", {})

        f = tmp_state / "test.jsonl"
        lines = [json.dumps({"n": i}) for i in range(20)]
        f.write_text("\n".join(lines) + "\n")

        results = state_hygiene.run_hygiene(apply=False)

        assert results["mode"] == "dry-run"
        assert results["total_bytes_saved"] > 0
        assert len(results["trimmed"]) == 1
        # File unchanged
        assert len(f.read_text().strip().splitlines()) == 20

    def test_full_run_apply(self, tmp_state: Path, monkeypatch: pytest.MonkeyPatch):
        """Full apply run trims files and returns results."""
        monkeypatch.setattr(state_hygiene, "JSONL_RETENTION", {"test.jsonl": 5})
        monkeypatch.setattr(state_hygiene, "DIR_RETENTION", {})

        f = tmp_state / "test.jsonl"
        lines = [json.dumps({"n": i}) for i in range(20)]
        f.write_text("\n".join(lines) + "\n")

        # No working_memory_archive.json in test
        results = state_hygiene.run_hygiene(apply=True)

        assert results["mode"] == "applied"
        assert results["total_bytes_saved"] > 0
        assert len(f.read_text().strip().splitlines()) == 5


class TestFormatBytes:
    def test_bytes(self):
        assert state_hygiene._fmt_bytes(500) == "500 B"

    def test_kilobytes(self):
        assert state_hygiene._fmt_bytes(2048) == "2.0 KB"

    def test_megabytes(self):
        assert state_hygiene._fmt_bytes(1048576) == "1.0 MB"
