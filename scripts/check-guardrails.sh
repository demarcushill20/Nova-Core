#!/usr/bin/env bash
# NovaCore guardrail check — prevents runtime artifacts and secrets from being committed.
# Used by: pre-commit hook, GitHub Actions CI, manual invocation.
#
# Usage:
#   scripts/check-guardrails.sh          # check staged files (pre-commit mode)
#   scripts/check-guardrails.sh --all    # check all tracked files (CI mode)
#
# Exit codes: 0 = clean, 1 = violations found

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

MODE="${1:-staged}"
VIOLATIONS=0

# ─── File list ───────────────────────────────────────────────────────────────

if [ "$MODE" = "--all" ]; then
    FILES=$(git ls-files)
else
    FILES=$(git diff --cached --name-only --diff-filter=ACMR 2>/dev/null || true)
fi

if [ -z "$FILES" ]; then
    echo -e "${GREEN}No files to check.${NC}"
    exit 0
fi

# ─── Check 1: Runtime artifacts ──────────────────────────────────────────────

echo "Checking for runtime artifacts in staged files..."

RUNTIME_PATTERN='^(TASKS|OUTPUT|WORK|STATE)/.*'
ALLOWED_PATTERN='\.gitkeep$|^STATE/tools_registry\.json$'

while IFS= read -r file; do
    if echo "$file" | grep -qE "$RUNTIME_PATTERN"; then
        if ! echo "$file" | grep -qE "$ALLOWED_PATTERN"; then
            echo -e "${RED}BLOCKED: runtime artifact: ${file}${NC}"
            VIOLATIONS=$((VIOLATIONS + 1))
        fi
    fi
    if [ "$file" = "HEARTBEAT.md" ]; then
        echo -e "${RED}BLOCKED: runtime artifact: HEARTBEAT.md${NC}"
        VIOLATIONS=$((VIOLATIONS + 1))
    fi
done <<< "$FILES"

# ─── Check 2: Secret patterns ───────────────────────────────────────────────

echo "Checking for secret patterns..."

# Patterns that indicate real secrets (not env var references or redaction code)
SECRET_PATTERNS=(
    'bot[0-9]{8,}:[A-Za-z0-9_-]{30,}'           # Telegram bot token
    'ghp_[A-Za-z0-9]{36,}'                        # GitHub PAT (classic)
    'github_pat_[A-Za-z0-9_]{40,}'                # GitHub PAT (fine-grained)
    'AKIA[0-9A-Z]{16}'                             # AWS access key
    'xox[bps]-[0-9a-zA-Z]{10,}'                   # Slack token
    'sk-[A-Za-z0-9]{40,}'                          # OpenAI API key
    'sk-ant-[A-Za-z0-9-]{40,}'                     # Anthropic API key
)

# Files that are allowed to contain patterns (test fixtures, redaction code)
SAFE_FILES="dev_safety_smoke\.py|test_.*\.py|runner\.py"

while IFS= read -r file; do
    # Skip binary, deleted, and safe files
    [ ! -f "$file" ] && continue
    echo "$file" | grep -qE "$SAFE_FILES" && continue

    for pattern in "${SECRET_PATTERNS[@]}"; do
        MATCHES=$(grep -nE "$pattern" "$file" 2>/dev/null || true)
        if [ -n "$MATCHES" ]; then
            while IFS= read -r match; do
                LINE_NUM=$(echo "$match" | cut -d: -f1)
                # Print location only — never the value
                echo -e "${RED}SECRET DETECTED: ${file}:${LINE_NUM} (pattern: ${pattern%%[*}...)${NC}"
                VIOLATIONS=$((VIOLATIONS + 1))
            done <<< "$MATCHES"
        fi
    done
done <<< "$FILES"

# ─── Check 3: .env files ────────────────────────────────────────────────────

while IFS= read -r file; do
    if echo "$file" | grep -qE '\.env$'; then
        echo -e "${RED}BLOCKED: environment file: ${file}${NC}"
        VIOLATIONS=$((VIOLATIONS + 1))
    fi
done <<< "$FILES"

# ─── Summary ─────────────────────────────────────────────────────────────────

echo ""
if [ "$VIOLATIONS" -gt 0 ]; then
    echo -e "${RED}FAILED: ${VIOLATIONS} violation(s) found.${NC}"
    echo "Fix the issues above before committing."
    exit 1
else
    echo -e "${GREEN}PASSED: no violations found.${NC}"
    exit 0
fi
