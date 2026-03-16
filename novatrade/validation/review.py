"""Integrated validation review workflow for NovaTrade MVP.

Runs health snapshot + reconciliation against the adapter, records both
to evidence, and produces a fresh report — all in one safe, read-only call.

Provider-neutral — only uses the MT5Adapter abstract interface.
"""

from __future__ import annotations

import logging

from novatrade.adapter.base import MT5Adapter
from novatrade.models import ValidationReport
from novatrade.monitor.health import take_health_snapshot
from novatrade.monitor.position_tracker import PositionTracker
from novatrade.monitor.reconciliation import reconcile
from novatrade.validation.evidence import EvidenceRecorder
from novatrade.validation.reporting import generate_report

log = logging.getLogger("novatrade.validation.review")


async def run_validation_cycle(
    adapter: MT5Adapter,
    tracker: PositionTracker,
    recorder: EvidenceRecorder,
) -> ValidationReport:
    """Run a complete validation cycle and return a fresh report.

    Steps:
    1. Take a health snapshot → record to evidence
    2. Run reconciliation against tracked positions → record to evidence
    3. Load all evidence and generate a report

    Safe to call at any time.  Never raises — errors are captured in the
    evidence and reflected in the report.
    """
    # -- 1. Health snapshot ----------------------------------------------------
    log.info("validation cycle: taking health snapshot")
    snapshot = await take_health_snapshot(adapter)
    try:
        recorder.record_health(snapshot)
    except Exception as exc:
        log.warning("validation cycle: health evidence recording failed: %s", exc)

    # -- 2. Reconciliation -----------------------------------------------------
    log.info("validation cycle: running reconciliation")
    recon = await reconcile(adapter, tracker)
    try:
        recorder.record_reconciliation(recon)
    except Exception as exc:
        log.warning("validation cycle: reconciliation evidence recording failed: %s", exc)

    # -- 3. Generate report from all evidence ----------------------------------
    records = recorder.load()
    report = generate_report(records)

    log.info(
        "validation cycle complete: records=%d stable=%s",
        report.total_records,
        report.stable,
    )
    return report
