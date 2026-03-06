"""Live contract compliance audit for NovaCore task outputs.

Phase 5.3D — scans OUTPUT/ files, validates contract blocks using the
existing deterministic validator, and classifies each output.

No LLM calls.  Purely deterministic.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from planner.schemas import ContractAuditRecord, ContractAuditSummary
from tools.contracts import validate_contract

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Task IDs at or above this number are considered post-contract-hardening.
# Tasks 0001–0024 were created before the contract system existed.
# Tasks 0025+ were created during or after Phase 5.x contract work.
_POST_CONTRACT_TASK_THRESHOLD = 25

# Regex to extract a task number from the output filename.
# Matches patterns like "0032__task_..." or "0005_service_test__..."
_TASK_NUM_RE = re.compile(r"^(\d{4})")

# Regex to extract a timestamp suffix like "20260306-141500"
_TIMESTAMP_RE = re.compile(r"(\d{8}-\d{6})\.md$")


class ContractAudit:
    """Deterministic contract compliance auditor for OUTPUT/ files."""

    def scan_outputs(
        self,
        output_dir: str = "OUTPUT",
        limit: int | None = None,
    ) -> list[ContractAuditRecord]:
        """Scan markdown outputs and return an audit record per file.

        Files are sorted alphabetically for deterministic ordering.
        """
        out_path = Path(output_dir)
        if not out_path.is_dir():
            return []

        md_files = sorted(f.name for f in out_path.iterdir() if f.suffix == ".md")

        if limit is not None:
            md_files = md_files[:limit]

        records: list[ContractAuditRecord] = []
        for fname in md_files:
            full = out_path / fname
            try:
                content = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            result = validate_contract(content)

            task_id = self._extract_task_id(fname)
            timestamp = self._extract_timestamp(fname)

            missing = [
                e.replace("missing required field: ", "")
                for e in result.get("errors", [])
                if e.startswith("missing required field:")
            ]

            rec = ContractAuditRecord(
                output_file=fname,
                task_id=task_id,
                has_contract="no ## CONTRACT section found" not in " ".join(result.get("errors", [])),
                valid_contract=result["valid"],
                missing_fields=missing,
                detected_timestamp=timestamp,
            )
            rec.classification = self.classify_output(rec)
            records.append(rec)

        return records

    def summarize(
        self,
        records: list[ContractAuditRecord],
        audit_id: str,
    ) -> ContractAuditSummary:
        """Produce an aggregate summary from audit records."""
        total = len(records)
        valid = sum(1 for r in records if r.valid_contract)
        no_contract = sum(1 for r in records if not r.has_contract)
        invalid = total - valid - no_contract

        if total > 0:
            compliance_rate = round(valid / total, 4)
        else:
            compliance_rate = 0.0

        # Clamp to [0.0, 1.0]
        compliance_rate = max(0.0, min(1.0, compliance_rate))

        # Count missing fields across all records
        field_counts: dict[str, int] = {}
        for r in records:
            for f in r.missing_fields:
                field_counts[f] = field_counts.get(f, 0) + 1

        notes: list[str] = []
        legacy_count = sum(
            1 for r in records if r.classification == "legacy_pre_contract"
        )
        if legacy_count:
            notes.append(
                f"{legacy_count} output(s) classified as legacy (pre-contract system)"
            )

        return ContractAuditSummary(
            audit_id=audit_id,
            total_outputs=total,
            valid_contracts=valid,
            invalid_contracts=invalid,
            no_contract=no_contract,
            compliance_rate=compliance_rate,
            missing_field_counts=field_counts,
            records=records,
            notes=notes,
        )

    def classify_output(self, record: ContractAuditRecord) -> str:
        """Deterministically classify an output record."""
        task_num = self._task_number(record.task_id)

        if not record.has_contract:
            # If it's clearly a legacy task, mark it as such
            if task_num is not None and task_num < _POST_CONTRACT_TASK_THRESHOLD:
                return "legacy_pre_contract"
            return "no_contract_detected"

        if record.valid_contract:
            return "post_contract_valid"

        # Has a contract block but it's invalid
        if task_num is not None and task_num < _POST_CONTRACT_TASK_THRESHOLD:
            return "legacy_pre_contract"

        return "post_contract_invalid"

    def save_summary(self, summary: ContractAuditSummary, path: str) -> None:
        """Persist audit summary as JSON."""
        data = {
            "audit_id": summary.audit_id,
            "total_outputs": summary.total_outputs,
            "valid_contracts": summary.valid_contracts,
            "invalid_contracts": summary.invalid_contracts,
            "no_contract": summary.no_contract,
            "compliance_rate": summary.compliance_rate,
            "missing_field_counts": summary.missing_field_counts,
            "notes": summary.notes,
            "records": [
                {
                    "output_file": r.output_file,
                    "task_id": r.task_id,
                    "has_contract": r.has_contract,
                    "valid_contract": r.valid_contract,
                    "missing_fields": r.missing_fields,
                    "detected_timestamp": r.detected_timestamp,
                    "classification": r.classification,
                }
                for r in summary.records
            ],
        }
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_task_id(filename: str) -> str | None:
        """Extract a task ID like '0032' from the output filename."""
        m = _TASK_NUM_RE.match(filename)
        return m.group(1) if m else None

    @staticmethod
    def _extract_timestamp(filename: str) -> str | None:
        """Extract the timestamp suffix from the output filename."""
        m = _TIMESTAMP_RE.search(filename)
        return m.group(1) if m else None

    @staticmethod
    def _task_number(task_id: str | None) -> int | None:
        """Convert a task_id string like '0032' to an int, or None."""
        if task_id is None:
            return None
        try:
            return int(task_id)
        except (ValueError, TypeError):
            return None
