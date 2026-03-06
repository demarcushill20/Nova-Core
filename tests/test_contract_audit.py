"""Tests for Phase 5.3D — ContractAudit live compliance audit."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from planner.contract_audit import ContractAudit
from planner.schemas import ContractAuditRecord, ContractAuditSummary


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_CONTRACT_OUTPUT = """\
# Task Report

Did some work.

## CONTRACT
summary: completed task
files_changed: foo.py
verification: tests pass
confidence: high
"""

INVALID_CONTRACT_OUTPUT = """\
# Task Report

Did some work.

## CONTRACT
summary: completed task
confidence: high
"""

NO_CONTRACT_OUTPUT = """\
# Task Report

Did some work. No contract block here.
"""

LEGACY_CONTENT = """\
# Bootstrap Output

System bootstrapped successfully.
"""

POST_HARDENING_VALID = """\
# Task 0030

All done.

## CONTRACT
summary: hardened contract emission
files_changed: watcher.py, tests/test_worker.py
verification: 477 tests pass
confidence: high
"""


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    """Create a temporary output directory with sample files."""
    d = tmp_path / "OUTPUT"
    d.mkdir()
    (d / "0003_legacy__20260301-070000.md").write_text(LEGACY_CONTENT)
    (d / "0005_service__20260301-083000.md").write_text(NO_CONTRACT_OUTPUT)
    (d / "0026_orchestrator__20260306-165000.md").write_text(INVALID_CONTRACT_OUTPUT)
    (d / "0030_hardened__20260306-141500.md").write_text(POST_HARDENING_VALID)
    (d / "0031_another__20260306-150000.md").write_text(VALID_CONTRACT_OUTPUT)
    return d


@pytest.fixture
def auditor() -> ContractAudit:
    return ContractAudit()


# ---------------------------------------------------------------------------
# Tests: scan_outputs
# ---------------------------------------------------------------------------


class TestScanOutputs:
    def test_returns_list(self, auditor: ContractAudit, output_dir: Path):
        records = auditor.scan_outputs(str(output_dir))
        assert isinstance(records, list)
        assert len(records) == 5

    def test_deterministic_order(self, auditor: ContractAudit, output_dir: Path):
        r1 = auditor.scan_outputs(str(output_dir))
        r2 = auditor.scan_outputs(str(output_dir))
        assert [r.output_file for r in r1] == [r.output_file for r in r2]

    def test_limit(self, auditor: ContractAudit, output_dir: Path):
        records = auditor.scan_outputs(str(output_dir), limit=2)
        assert len(records) == 2

    def test_empty_dir(self, auditor: ContractAudit, tmp_path: Path):
        empty = tmp_path / "EMPTY"
        empty.mkdir()
        assert auditor.scan_outputs(str(empty)) == []

    def test_nonexistent_dir(self, auditor: ContractAudit):
        assert auditor.scan_outputs("/nonexistent/path") == []

    def test_valid_contract_detected(self, auditor: ContractAudit, output_dir: Path):
        records = auditor.scan_outputs(str(output_dir))
        by_name = {r.output_file: r for r in records}
        r = by_name["0030_hardened__20260306-141500.md"]
        assert r.has_contract is True
        assert r.valid_contract is True
        assert r.missing_fields == []

    def test_invalid_contract_detected(self, auditor: ContractAudit, output_dir: Path):
        records = auditor.scan_outputs(str(output_dir))
        by_name = {r.output_file: r for r in records}
        r = by_name["0026_orchestrator__20260306-165000.md"]
        assert r.has_contract is True
        assert r.valid_contract is False
        assert len(r.missing_fields) > 0

    def test_no_contract_detected(self, auditor: ContractAudit, output_dir: Path):
        records = auditor.scan_outputs(str(output_dir))
        by_name = {r.output_file: r for r in records}
        r = by_name["0005_service__20260301-083000.md"]
        assert r.has_contract is False
        assert r.valid_contract is False

    def test_task_id_extraction(self, auditor: ContractAudit, output_dir: Path):
        records = auditor.scan_outputs(str(output_dir))
        by_name = {r.output_file: r for r in records}
        assert by_name["0030_hardened__20260306-141500.md"].task_id == "0030"
        assert by_name["0003_legacy__20260301-070000.md"].task_id == "0003"

    def test_timestamp_extraction(self, auditor: ContractAudit, output_dir: Path):
        records = auditor.scan_outputs(str(output_dir))
        by_name = {r.output_file: r for r in records}
        assert by_name["0030_hardened__20260306-141500.md"].detected_timestamp == "20260306-141500"


# ---------------------------------------------------------------------------
# Tests: classify_output
# ---------------------------------------------------------------------------


class TestClassifyOutput:
    def test_legacy_no_contract(self, auditor: ContractAudit):
        rec = ContractAuditRecord(output_file="0003_x.md", task_id="0003", has_contract=False)
        assert auditor.classify_output(rec) == "legacy_pre_contract"

    def test_no_contract_post_threshold(self, auditor: ContractAudit):
        rec = ContractAuditRecord(output_file="0030_x.md", task_id="0030", has_contract=False)
        assert auditor.classify_output(rec) == "no_contract_detected"

    def test_valid_contract(self, auditor: ContractAudit):
        rec = ContractAuditRecord(
            output_file="0030_x.md", task_id="0030",
            has_contract=True, valid_contract=True,
        )
        assert auditor.classify_output(rec) == "post_contract_valid"

    def test_invalid_contract_post(self, auditor: ContractAudit):
        rec = ContractAuditRecord(
            output_file="0030_x.md", task_id="0030",
            has_contract=True, valid_contract=False,
        )
        assert auditor.classify_output(rec) == "post_contract_invalid"

    def test_invalid_contract_legacy(self, auditor: ContractAudit):
        rec = ContractAuditRecord(
            output_file="0010_x.md", task_id="0010",
            has_contract=True, valid_contract=False,
        )
        assert auditor.classify_output(rec) == "legacy_pre_contract"

    def test_no_task_id_no_contract(self, auditor: ContractAudit):
        rec = ContractAuditRecord(output_file="unknown.md", has_contract=False)
        assert auditor.classify_output(rec) == "no_contract_detected"


# ---------------------------------------------------------------------------
# Tests: summarize
# ---------------------------------------------------------------------------


class TestSummarize:
    def test_summary_counts(self, auditor: ContractAudit, output_dir: Path):
        records = auditor.scan_outputs(str(output_dir))
        summary = auditor.summarize(records, "test-audit-001")
        assert summary.audit_id == "test-audit-001"
        assert summary.total_outputs == 5
        assert summary.valid_contracts == 2  # 0030 and 0031
        assert summary.no_contract == 2  # 0003 (legacy) and 0005
        assert summary.invalid_contracts == 1  # 0026

    def test_compliance_rate_bounded(self, auditor: ContractAudit):
        summary = auditor.summarize([], "empty")
        assert summary.compliance_rate == 0.0

    def test_compliance_rate_calculation(self, auditor: ContractAudit, output_dir: Path):
        records = auditor.scan_outputs(str(output_dir))
        summary = auditor.summarize(records, "test")
        assert 0.0 <= summary.compliance_rate <= 1.0
        assert summary.compliance_rate == round(2 / 5, 4)

    def test_missing_field_counts(self, auditor: ContractAudit, output_dir: Path):
        records = auditor.scan_outputs(str(output_dir))
        summary = auditor.summarize(records, "test")
        # 0026 has invalid contract missing files_changed and verification
        assert summary.missing_field_counts.get("files_changed", 0) >= 1
        assert summary.missing_field_counts.get("verification", 0) >= 1

    def test_legacy_note(self, auditor: ContractAudit, output_dir: Path):
        records = auditor.scan_outputs(str(output_dir))
        summary = auditor.summarize(records, "test")
        legacy_notes = [n for n in summary.notes if "legacy" in n]
        assert len(legacy_notes) >= 1


# ---------------------------------------------------------------------------
# Tests: save_summary
# ---------------------------------------------------------------------------


class TestSaveSummary:
    def test_save_creates_file(self, auditor: ContractAudit, tmp_path: Path):
        summary = ContractAuditSummary(
            audit_id="save-test",
            total_outputs=1,
            valid_contracts=1,
            invalid_contracts=0,
            no_contract=0,
            compliance_rate=1.0,
        )
        out = tmp_path / "audits" / "result.json"
        auditor.save_summary(summary, str(out))
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["audit_id"] == "save-test"
        assert data["compliance_rate"] == 1.0

    def test_save_includes_records(self, auditor: ContractAudit, tmp_path: Path):
        rec = ContractAuditRecord(
            output_file="test.md",
            task_id="0001",
            has_contract=True,
            valid_contract=True,
            classification="post_contract_valid",
        )
        summary = ContractAuditSummary(
            audit_id="rec-test",
            total_outputs=1,
            valid_contracts=1,
            invalid_contracts=0,
            no_contract=0,
            compliance_rate=1.0,
            records=[rec],
        )
        out = tmp_path / "result.json"
        auditor.save_summary(summary, str(out))
        data = json.loads(out.read_text())
        assert len(data["records"]) == 1
        assert data["records"][0]["output_file"] == "test.md"


# ---------------------------------------------------------------------------
# Tests: dataclass integrity
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_audit_record_defaults(self):
        rec = ContractAuditRecord(output_file="x.md")
        assert rec.task_id is None
        assert rec.has_contract is False
        assert rec.valid_contract is False
        assert rec.missing_fields == []
        assert rec.detected_timestamp is None
        assert rec.classification == "unknown"

    def test_audit_summary_defaults(self):
        s = ContractAuditSummary(
            audit_id="x",
            total_outputs=0,
            valid_contracts=0,
            invalid_contracts=0,
            no_contract=0,
            compliance_rate=0.0,
        )
        assert s.missing_field_counts == {}
        assert s.records == []
        assert s.notes == []


# ---------------------------------------------------------------------------
# Tests: real OUTPUT/ integration (runs against actual repo if available)
# ---------------------------------------------------------------------------


class TestRealOutputIntegration:
    """Run audit against the actual OUTPUT/ directory if it exists."""

    @pytest.mark.skipif(
        not Path("OUTPUT").is_dir(),
        reason="No OUTPUT/ directory in working directory",
    )
    def test_scan_real_outputs(self, auditor: ContractAudit):
        records = auditor.scan_outputs("OUTPUT")
        assert isinstance(records, list)
        # Every record must have a classification assigned
        for rec in records:
            assert rec.classification != "unknown", f"{rec.output_file} has unknown classification"

    @pytest.mark.skipif(
        not Path("OUTPUT").is_dir(),
        reason="No OUTPUT/ directory in working directory",
    )
    def test_real_compliance_rate_bounded(self, auditor: ContractAudit):
        records = auditor.scan_outputs("OUTPUT")
        summary = auditor.summarize(records, "real-test")
        assert 0.0 <= summary.compliance_rate <= 1.0
