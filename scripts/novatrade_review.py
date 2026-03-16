#!/usr/bin/env python3
"""NovaTrade evidence review — read-only MVP validation summary.

Loads previously captured evidence from the JSONL log and prints a
structured validation report.  Does NOT place trades or modify state.

With --live-check, connects to the adapter to run a health snapshot and
reconciliation cycle before generating the report.

Usage:
    # Default evidence path (OUTPUT/novatrade/evidence.jsonl):
    python3 scripts/novatrade_review.py

    # Custom evidence file:
    python3 scripts/novatrade_review.py --file /path/to/evidence.jsonl

    # JSON output for programmatic consumption:
    python3 scripts/novatrade_review.py --json

    # Show raw records:
    python3 scripts/novatrade_review.py --raw

    # Live health + reconciliation check (read-only, no trades):
    python3 scripts/novatrade_review.py --live-check

    # MVP go/no-go verdict:
    python3 scripts/novatrade_review.py --verdict

    # Full readiness/remediation package:
    python3 scripts/novatrade_review.py --readiness

    # Migration-readiness audit (provider swap):
    python3 scripts/novatrade_review.py --audit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.validation.evidence import DEFAULT_EVIDENCE_PATH, EvidenceRecorder
from novatrade.validation.reporting import format_report, generate_report


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


async def _run_live_check(
    recorder: EvidenceRecorder,
    env_file: str | None,
) -> None:
    """Connect to adapter, run validation cycle, disconnect."""
    from novatrade.adapter.metaapi_provider import MetaApiAdapter
    from novatrade.config import NovaTradeCfg
    from novatrade.monitor.position_tracker import PositionTracker
    from novatrade.validation.review import run_validation_cycle

    env_path = Path(env_file) if env_file else None
    cfg = NovaTradeCfg.load(env_file=env_path)
    cfg.dry_run = True  # safety: live-check is always read-only

    errors = cfg.validate()
    if errors:
        print("CONFIG ERRORS:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    adapter = MetaApiAdapter(cfg.metaapi)
    health = await adapter.connect()
    if not health.connected:
        print("Connection failed — cannot run live check")
        sys.exit(2)

    try:
        tracker = PositionTracker()  # fresh tracker for live check
        await run_validation_cycle(adapter, tracker, recorder)
        print("Live check complete — evidence recorded.\n")
    finally:
        await adapter.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NovaTrade evidence review — read-only validation report",
    )
    parser.add_argument(
        "--file",
        "-f",
        default=None,
        help=f"Path to evidence JSONL file (default: {DEFAULT_EVIDENCE_PATH})",
    )
    parser.add_argument(
        "--json",
        "-j",
        action="store_true",
        dest="json_output",
        help="Output report as JSON instead of text",
    )
    parser.add_argument(
        "--raw",
        "-r",
        action="store_true",
        help="Print raw evidence records",
    )
    parser.add_argument(
        "--last",
        "-n",
        type=int,
        default=0,
        help="Show only the last N records (0 = all)",
    )
    parser.add_argument(
        "--live-check",
        action="store_true",
        help="Connect to adapter for health + reconciliation before reporting (read-only)",
    )
    parser.add_argument(
        "--verdict",
        action="store_true",
        help="Generate MVP go/no-go verdict from current evidence",
    )
    parser.add_argument(
        "--readiness",
        action="store_true",
        help="Generate full MVP readiness/remediation package",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Run migration-readiness audit (provider swap analysis)",
    )
    parser.add_argument("--env-file", default=None, help="Path to env file (for --live-check)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.live_check or args.verbose:
        _setup_logging(args.verbose)

    # -- Migration audit (does not need evidence) --------------------------
    if args.audit:
        from novatrade.validation.migration_audit import format_audit, run_migration_audit

        result = run_migration_audit()
        if args.json_output:
            from dataclasses import asdict

            print(json.dumps(asdict(result), indent=2))
        else:
            print(format_audit(result))
        return

    path = Path(args.file) if args.file else DEFAULT_EVIDENCE_PATH
    recorder = EvidenceRecorder(path)

    if args.live_check:
        asyncio.get_event_loop().run_until_complete(
            _run_live_check(recorder, args.env_file),
        )

    records = recorder.load()

    if not records:
        print(f"No evidence found at {path}")
        print("Run demo executions first to generate evidence.")
        sys.exit(0)

    if args.last > 0:
        records = records[-args.last :]

    if args.raw:
        for rec in records:
            print(json.dumps(rec.to_dict(), indent=2))
        print(f"\n--- {len(records)} record(s) ---")
        return

    report = generate_report(records)

    if args.readiness:
        from novatrade.validation.decision import evaluate_mvp
        from novatrade.validation.readiness import build_readiness_package, format_readiness

        decision = evaluate_mvp(report)
        package = build_readiness_package(report, decision)
        if args.json_output:
            from dataclasses import asdict

            print(json.dumps(asdict(package), indent=2))
        else:
            print(format_report(report))
            print(format_readiness(package))
        return

    if args.verdict:
        from novatrade.validation.decision import evaluate_mvp, format_decision

        decision = evaluate_mvp(report)
        if args.json_output:
            from dataclasses import asdict

            print(json.dumps(asdict(decision), indent=2))
        else:
            print(format_report(report))
            print(format_decision(decision))
        return

    if args.json_output:
        from dataclasses import asdict

        print(json.dumps(asdict(report), indent=2))
    else:
        print(format_report(report))


if __name__ == "__main__":
    main()
