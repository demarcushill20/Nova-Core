#!/bin/bash
# Phase 7.1: Auto-remediation pipeline for NovaCore code quality.
# Run after any code generation to auto-fix lint and format issues.
set -e

echo "=== NovaCore Auto-Remediation Pipeline ==="

echo "[1/3] Ruff auto-fix..."
ruff check . --fix --quiet 2>/dev/null || true

echo "[2/3] Ruff format..."
ruff format . --quiet 2>/dev/null || true

echo "[3/3] Generating ruff report..."
ruff check . --output-format=json > /tmp/ruff_report.json 2>/dev/null || true

REMAINING=$(python3 -c "import json; data=json.load(open('/tmp/ruff_report.json')); print(len(data))" 2>/dev/null || echo "?")
echo "=== Done. ${REMAINING} remaining violations (unfixable). ==="
