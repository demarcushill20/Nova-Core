"""Tests for evidence.jsonl rotation (EvidenceRecorder.rotate)."""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

from novatrade.validation.evidence import EvidenceRecorder, RotationResult

# -- Helpers -------------------------------------------------------------------


def _make_record(event_type: str = "MONITORING", ts: float | None = None, error: str = "") -> str:
    """Create a single JSONL evidence line."""
    return json.dumps(
        {
            "event_type": event_type,
            "data": {"ok": True},
            "error": error,
            "timestamp": ts or time.time(),
        }
    )


def _seed_evidence(path: Path, records: list[tuple[str, float]]) -> None:
    """Write records as (event_type, timestamp) tuples to the evidence file."""
    with path.open("w") as f:
        for etype, ts in records:
            f.write(_make_record(etype, ts) + "\n")


def _ts(d: date, hour: int = 12) -> float:
    """Convert a date to a timestamp at the given hour."""
    from datetime import datetime

    return datetime(d.year, d.month, d.day, hour).timestamp()


# -- Tests ---------------------------------------------------------------------


class TestRotateEmpty:
    def test_empty_file(self, tmp_path: Path):
        p = tmp_path / "evidence.jsonl"
        p.touch()
        recorder = EvidenceRecorder(p)
        result = recorder.rotate()
        assert result.records_moved == 0
        assert result.records_kept == 0

    def test_missing_file(self, tmp_path: Path):
        p = tmp_path / "evidence.jsonl"
        recorder = EvidenceRecorder(p)
        result = recorder.rotate()
        assert result.records_moved == 0


class TestRotateBasic:
    def test_all_today_no_rotation(self, tmp_path: Path):
        today = date.today()
        p = tmp_path / "evidence.jsonl"
        _seed_evidence(
            p,
            [
                ("MONITORING", _ts(today, 10)),
                ("HEALTH_SNAPSHOT", _ts(today, 11)),
            ],
        )
        recorder = EvidenceRecorder(p)
        result = recorder.rotate(today=today)
        assert result.records_moved == 0
        assert result.records_kept == 2
        # No archive files created
        assert list(tmp_path.glob("evidence_*.jsonl")) == []

    def test_splits_by_date(self, tmp_path: Path):
        today = date(2026, 3, 23)
        yesterday = today - timedelta(days=1)
        two_days_ago = today - timedelta(days=2)

        p = tmp_path / "evidence.jsonl"
        _seed_evidence(
            p,
            [
                ("MONITORING", _ts(two_days_ago)),
                ("EXECUTION", _ts(two_days_ago, 14)),
                ("MONITORING", _ts(yesterday)),
                ("HEALTH_SNAPSHOT", _ts(today)),
                ("MONITORING", _ts(today, 14)),
            ],
        )
        recorder = EvidenceRecorder(p)
        result = recorder.rotate(today=today)

        assert result.records_moved == 3
        assert result.records_kept == 2
        assert len(result.dates_rotated) == 2
        assert "2026-03-21" in result.dates_rotated
        assert "2026-03-22" in result.dates_rotated

        # Check archive files exist
        assert (tmp_path / "evidence_2026-03-21.jsonl").exists()
        assert (tmp_path / "evidence_2026-03-22.jsonl").exists()

        # Check main file only has today's records
        remaining = p.read_text().strip().split("\n")
        assert len(remaining) == 2

    def test_idempotent(self, tmp_path: Path):
        today = date(2026, 3, 23)
        yesterday = today - timedelta(days=1)
        p = tmp_path / "evidence.jsonl"
        _seed_evidence(
            p,
            [
                ("MONITORING", _ts(yesterday)),
                ("MONITORING", _ts(today)),
            ],
        )
        recorder = EvidenceRecorder(p)

        r1 = recorder.rotate(today=today)
        assert r1.records_moved == 1

        r2 = recorder.rotate(today=today)
        assert r2.records_moved == 0
        assert r2.records_kept == 1

    def test_appends_to_existing_archive(self, tmp_path: Path):
        today = date(2026, 3, 23)
        yesterday = today - timedelta(days=1)

        # Pre-existing archive
        archive = tmp_path / "evidence_2026-03-22.jsonl"
        archive.write_text(_make_record("EXECUTION", _ts(yesterday, 8)) + "\n")

        p = tmp_path / "evidence.jsonl"
        _seed_evidence(
            p,
            [
                ("MONITORING", _ts(yesterday, 12)),
                ("MONITORING", _ts(today)),
            ],
        )
        recorder = EvidenceRecorder(p)
        result = recorder.rotate(today=today)

        assert result.records_moved == 1
        # Archive now has 2 lines (1 pre-existing + 1 rotated)
        lines = archive.read_text().strip().split("\n")
        assert len(lines) == 2


class TestRotateEdgeCases:
    def test_malformed_lines_kept_in_today(self, tmp_path: Path):
        """Malformed lines are kept in the main file (not lost)."""
        today = date(2026, 3, 23)
        p = tmp_path / "evidence.jsonl"
        p.write_text('{"bad json\n' + _make_record("MONITORING", _ts(today)) + "\n")
        recorder = EvidenceRecorder(p)
        result = recorder.rotate(today=today)
        assert result.records_kept == 2  # malformed + today

    def test_bytes_moved_counted(self, tmp_path: Path):
        today = date(2026, 3, 23)
        yesterday = today - timedelta(days=1)
        p = tmp_path / "evidence.jsonl"
        _seed_evidence(
            p,
            [
                ("MONITORING", _ts(yesterday)),
                ("MONITORING", _ts(today)),
            ],
        )
        recorder = EvidenceRecorder(p)
        result = recorder.rotate(today=today)
        assert result.bytes_moved > 0


class TestLoadAll:
    def test_includes_rotated_files(self, tmp_path: Path):
        today = date(2026, 3, 23)
        yesterday = today - timedelta(days=1)

        # Main file
        p = tmp_path / "evidence.jsonl"
        _seed_evidence(p, [("MONITORING", _ts(today))])

        # Rotated file
        archive = tmp_path / "evidence_2026-03-22.jsonl"
        archive.write_text(_make_record("EXECUTION", _ts(yesterday)) + "\n")

        recorder = EvidenceRecorder(p)
        all_records = recorder.load_all()
        assert len(all_records) == 2
        # Sorted oldest first
        assert all_records[0].timestamp < all_records[1].timestamp

    def test_empty_when_no_files(self, tmp_path: Path):
        p = tmp_path / "evidence.jsonl"
        recorder = EvidenceRecorder(p)
        assert recorder.load_all() == []


class TestRotationResult:
    def test_defaults(self):
        r = RotationResult()
        assert r.records_moved == 0
        assert r.records_kept == 0
        assert r.bytes_moved == 0
        assert r.dates_rotated == []
