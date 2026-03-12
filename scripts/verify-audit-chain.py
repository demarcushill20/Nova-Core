#!/usr/bin/env python3
"""Verify audit log hash chain integrity.

Phase 2.5: Structured audit logging — chain verification.
Run from heartbeat (weekly) or manually to validate log tamper-evidence.

Usage:
    python3 scripts/verify-audit-chain.py [LOGS/audit/audit_2026-03-12.jsonl]

Without arguments, verifies today's audit log.
"""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

AUDIT_DIR = Path("/home/nova/nova-core/LOGS/audit")


def verify_chain(log_file: Path) -> tuple[bool, list[str]]:
    """Verify hash chain integrity of an audit log file.

    Returns:
        (valid, errors) — True if chain is intact, list of error descriptions.
    """
    if not log_file.exists():
        return True, [f"File not found: {log_file}"]

    errors: list[str] = []
    prev_hash = "genesis"
    line_num = 0

    for line in log_file.read_text(encoding="utf-8").strip().splitlines():
        line_num += 1
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"Line {line_num}: invalid JSON — {e}")
            continue

        # Check prev_hash matches expected
        entry_prev = entry.get("prev_hash", "")
        if entry_prev != prev_hash:
            errors.append(
                f"Line {line_num}: prev_hash mismatch — "
                f"expected {prev_hash[:16]}..., got {entry_prev[:16]}..."
            )

        # Verify chain_hash
        chain_hash = entry.get("chain_hash", "")
        # Reconstruct: hash = SHA-256(prev_hash + json_without_chain_hash)
        verify_entry = {k: v for k, v in entry.items() if k != "chain_hash"}
        expected_hash = hashlib.sha256(
            (prev_hash + json.dumps(verify_entry, sort_keys=True)).encode()
        ).hexdigest()

        if chain_hash != expected_hash:
            errors.append(
                f"Line {line_num}: chain_hash mismatch — "
                f"expected {expected_hash[:16]}..., got {chain_hash[:16]}..."
            )

        prev_hash = chain_hash

    valid = len(errors) == 0
    return valid, errors


def main():
    if len(sys.argv) > 1:
        log_file = Path(sys.argv[1])
    else:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = AUDIT_DIR / f"audit_{today}.jsonl"

    print(f"Verifying: {log_file}")

    if not log_file.exists():
        print(f"No audit log found at {log_file}")
        sys.exit(0)

    valid, errors = verify_chain(log_file)

    line_count = len(log_file.read_text().strip().splitlines())
    print(f"Entries: {line_count}")

    if valid:
        print("Result: VALID — hash chain integrity confirmed")
        sys.exit(0)
    else:
        print(f"Result: INVALID — {len(errors)} error(s) found:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
