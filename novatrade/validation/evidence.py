"""Append-only evidence recorder for NovaTrade validation.

Records structured events (executions, health snapshots, reconciliation runs,
risk decisions, adapter errors) to a local JSONL file.  Each line is a
self-contained JSON object that can be loaded independently.

Includes daily rotation: records older than today are moved to dated archive
files (``evidence_YYYY-MM-DD.jsonl``), keeping the main file small.

Provider-neutral — only uses NovaTrade-native models.
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path

from novatrade.models import (
    EvidenceRecord,
    EvidenceType,
    ExecutionResult,
    HealthSnapshot,
    ReconciliationResult,
    RiskDecision,
)

log = logging.getLogger("novatrade.validation.evidence")

# Default evidence path under the NovaCore OUTPUT directory.
DEFAULT_EVIDENCE_PATH = Path("OUTPUT/novatrade/evidence.jsonl")


class EvidenceRecorder:
    """Append-only JSONL writer for NovaTrade audit events.

    Usage::

        recorder = EvidenceRecorder()          # uses default path
        recorder = EvidenceRecorder(path)      # custom path

        recorder.record_execution(result)
        recorder.record_health(snapshot)
        recorder.record_reconciliation(recon)
        recorder.record_risk_decision(decision, symbol)
        recorder.record_error(error_msg, context)

        records = recorder.load()              # read all records back
    """

    def __init__(
        self,
        path: Path | str | None = None,
        campaign: str = "",
    ) -> None:
        self._path = Path(path) if path else DEFAULT_EVIDENCE_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._campaign = campaign

    @property
    def path(self) -> Path:
        return self._path

    # -- Writers ---------------------------------------------------------------

    def _append(self, record: EvidenceRecord) -> None:
        if self._campaign:
            record.data["campaign"] = self._campaign
        with self._path.open("a") as f:
            f.write(json.dumps(record.to_dict()) + "\n")
        log.debug("evidence: recorded %s", record.event_type.value)

    def record_execution(self, result: ExecutionResult) -> None:
        data: dict = {
            "outcome": result.outcome.value,
            "elapsed_ms": result.elapsed_ms,
        }
        if result.risk_decision:
            data["verdict"] = result.risk_decision.verdict.value
            data["rule"] = result.risk_decision.rule
            data["failed_checks"] = [{"name": c.name, "detail": c.detail} for c in result.risk_decision.failed_checks]
        if result.order_result:
            data["order_id"] = result.order_result.order_id
            data["order_status"] = result.order_result.status.value
            data["fill_price"] = result.order_result.fill_price
        self._append(
            EvidenceRecord(
                event_type=EvidenceType.EXECUTION,
                data=data,
                error=result.error,
                timestamp=result.timestamp,
            )
        )

    def record_health(self, snapshot: HealthSnapshot) -> None:
        data: dict = {
            "state": snapshot.adapter_health.state.value,
            "connected": snapshot.adapter_health.connected,
            "ok": snapshot.ok,
        }
        if snapshot.account:
            data["balance"] = snapshot.account.balance
            data["equity"] = snapshot.account.equity
        data["position_count"] = snapshot.position_count
        data["total_unrealized_pnl"] = snapshot.total_unrealized_pnl
        self._append(
            EvidenceRecord(
                event_type=EvidenceType.HEALTH_SNAPSHOT,
                data=data,
                error=snapshot.error,
                timestamp=snapshot.timestamp,
            )
        )

    def record_reconciliation(self, result: ReconciliationResult) -> None:
        data: dict = {
            "ok": result.ok,
            "expected_count": result.expected_count,
            "actual_count": result.actual_count,
            "mismatch_count": len(result.mismatches),
            "mismatches": [
                {
                    "type": m.mismatch_type.value,
                    "symbol": m.symbol,
                    "detail": m.detail,
                }
                for m in result.mismatches
            ],
        }
        self._append(
            EvidenceRecord(
                event_type=EvidenceType.RECONCILIATION,
                data=data,
                timestamp=result.timestamp,
            )
        )

    def record_risk_decision(self, decision: RiskDecision, symbol: str = "") -> None:
        data: dict = {
            "verdict": decision.verdict.value,
            "rule": decision.rule,
            "reason": decision.reason,
            "symbol": symbol,
            "checks_run": len(decision.checks),
            "checks_failed": len(decision.failed_checks),
        }
        self._append(
            EvidenceRecord(
                event_type=EvidenceType.RISK_DECISION,
                data=data,
                timestamp=decision.timestamp,
            )
        )

    def record_error(self, error: str, context: dict | None = None) -> None:
        self._append(
            EvidenceRecord(
                event_type=EvidenceType.ADAPTER_ERROR,
                data=context or {},
                error=error,
            )
        )

    # -- Reader ----------------------------------------------------------------

    def load(self) -> list[EvidenceRecord]:
        """Load all records from the evidence file.

        Returns an empty list if the file does not exist.
        Skips malformed lines with a warning rather than failing.
        """
        if not self._path.exists():
            return []

        records: list[EvidenceRecord] = []
        with self._path.open() as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    records.append(EvidenceRecord.from_dict(d))
                except (json.JSONDecodeError, KeyError, ValueError) as exc:
                    log.warning("evidence: skipping malformed line %d: %s", lineno, exc)
        return records

    def load_all(self) -> list[EvidenceRecord]:
        """Load records from the current file *and* all rotated daily files.

        Returns records sorted by timestamp (oldest first).
        """
        records = self.load()
        for rotated in sorted(self._path.parent.glob("evidence_*.jsonl")):
            records.extend(_load_jsonl(rotated))
        records.sort(key=lambda r: r.timestamp)
        return records

    # -- Rotation --------------------------------------------------------------

    def rotate(self, *, today: date | None = None) -> RotationResult:
        """Rotate evidence: move records older than *today* to daily files.

        Records from today stay in the main file.  Older records are appended
        to ``evidence_YYYY-MM-DD.jsonl`` in the same directory.

        Safe to call repeatedly — already-rotated records are not duplicated.
        Uses atomic writes (temp + rename) to prevent data loss.

        Returns a :class:`RotationResult` with counts and bytes moved.
        """
        today = today or date.today()
        if not self._path.exists() or self._path.stat().st_size == 0:
            return RotationResult()

        # Partition lines by date (raw strings, not parsed objects — fast)
        today_lines: list[str] = []
        by_date: dict[str, list[str]] = defaultdict(list)
        total_moved = 0

        with self._path.open() as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    ts = json.loads(stripped).get("timestamp", 0)
                    record_date = date.fromtimestamp(ts)
                except (json.JSONDecodeError, ValueError, OSError):
                    today_lines.append(stripped)
                    continue

                if record_date >= today:
                    today_lines.append(stripped)
                else:
                    by_date[record_date.isoformat()].append(stripped)
                    total_moved += 1

        if not by_date:
            return RotationResult(records_kept=len(today_lines))

        # Write dated archive files (append if they already exist)
        bytes_moved = 0
        dates_rotated: list[str] = []
        for date_str, lines in sorted(by_date.items()):
            archive_path = self._path.parent / f"evidence_{date_str}.jsonl"
            payload = "\n".join(lines) + "\n"
            bytes_moved += len(payload.encode())
            dates_rotated.append(date_str)
            with archive_path.open("a") as f:
                f.write(payload)
            log.info(
                "evidence: rotated %d records to %s",
                len(lines),
                archive_path.name,
            )

        # Rewrite main file with only today's records (atomic)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=".evidence_rotate_",
            suffix=".tmp",
        )
        try:
            with open(tmp_fd, "w") as f:
                if today_lines:
                    f.write("\n".join(today_lines) + "\n")
            Path(tmp_path).replace(self._path)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise

        log.info(
            "evidence: rotation complete — %d records moved to %d daily files, %d records kept (%s freed)",
            total_moved,
            len(by_date),
            len(today_lines),
            _fmt_bytes(bytes_moved),
        )
        return RotationResult(
            records_moved=total_moved,
            records_kept=len(today_lines),
            bytes_moved=bytes_moved,
            dates_rotated=dates_rotated,
        )


# -- Helpers -------------------------------------------------------------------


class RotationResult:
    """Result of an evidence rotation operation."""

    __slots__ = ("bytes_moved", "dates_rotated", "records_kept", "records_moved")

    def __init__(
        self,
        records_moved: int = 0,
        records_kept: int = 0,
        bytes_moved: int = 0,
        dates_rotated: list[str] | None = None,
    ) -> None:
        self.records_moved = records_moved
        self.records_kept = records_kept
        self.bytes_moved = bytes_moved
        self.dates_rotated = dates_rotated or []


def _load_jsonl(path: Path) -> list[EvidenceRecord]:
    """Load EvidenceRecords from a JSONL file, skipping malformed lines."""
    records: list[EvidenceRecord] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(EvidenceRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError):
                pass
    return records


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
