#!/bin/bash
# Phase 7.1: Auto-remediation pipeline for NovaCore code quality.
# Run after any code generation to auto-fix lint and format issues.
# Exit code reflects whether ruff itself failed (not residual violations).

echo "=== NovaCore Auto-Remediation Pipeline ==="

rc=0

echo "[1/3] Ruff auto-fix..."
if ! ruff check . --fix --quiet 2>/dev/null; then
    # Exit code 1 from ruff check means violations remain (expected).
    # Exit code 2 means ruff itself crashed — that's a real failure.
    if [ $? -eq 2 ]; then
        echo "ERROR: ruff check crashed"
        rc=1
    fi
fi

echo "[2/3] Ruff format..."
if ! ruff format . --quiet 2>/dev/null; then
    echo "ERROR: ruff format failed"
    rc=1
fi

echo "[3/3] Generating ruff report..."
ruff check . --output-format=json > /tmp/ruff_report.json 2>/dev/null || true

REMAINING=$(python3 -c "import json; data=json.load(open('/tmp/ruff_report.json')); print(len(data))" 2>/dev/null || echo "?")
echo "=== Done. ${REMAINING} remaining violations (unfixable). ==="

exit $rc
