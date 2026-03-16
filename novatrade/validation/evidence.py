"""Append-only evidence recorder for NovaTrade validation.

Records structured events (executions, health snapshots, reconciliation runs,
risk decisions, adapter errors) to a local JSONL file.  Each line is a
self-contained JSON object that can be loaded independently.

Provider-neutral — only uses NovaTrade-native models.
"""

from __future__ import annotations

import json
import logging
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

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path else DEFAULT_EVIDENCE_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    # -- Writers ---------------------------------------------------------------

    def _append(self, record: EvidenceRecord) -> None:
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
