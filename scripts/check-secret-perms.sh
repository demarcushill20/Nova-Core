#!/usr/bin/env bash
# check-secret-perms.sh — Verify secret file permissions are secure
# Phase 2.4: Secret management hardening
# Run from heartbeat or manually to audit secret file permissions.

set -euo pipefail

ERRORS=0
WARNINGS=0

check_perms() {
    local file="$1"
    local max_mode="$2"  # e.g., 600 or 640
    local description="$3"

    if [ ! -f "$file" ]; then
        echo "[SKIP] $file — does not exist ($description)"
        return
    fi

    local actual_mode
    actual_mode=$(stat -c '%a' "$file" 2>/dev/null)

    # Check world-readable (others have read)
    local others_read=$(( actual_mode % 10 ))
    if [ "$others_read" -ge 4 ]; then
        echo "[FAIL] $file — world-readable (mode=$actual_mode, expected<=$max_mode) [$description]"
        ERRORS=$((ERRORS + 1))
        return
    fi

    # Check group-readable when not allowed
    if [ "$max_mode" = "600" ]; then
        local group_read=$(( (actual_mode / 10) % 10 ))
        if [ "$group_read" -ge 4 ]; then
            echo "[WARN] $file — group-readable (mode=$actual_mode, expected=$max_mode) [$description]"
            WARNINGS=$((WARNINGS + 1))
            return
        fi
    fi

    echo "[OK]   $file (mode=$actual_mode) [$description]"
}

echo "=== Secret File Permission Audit ==="
echo "Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

# Core secret files
check_perms "/etc/novacore/telegram.env" "640" "Telegram bot token + API keys"
check_perms "/etc/novacore/mcp.env" "640" "MCP server API keys"
check_perms "/home/nova/nova-core/.mcp.env" "600" "MCP env (local copy)"
check_perms "/home/nova/.ssh/id_rsa" "600" "SSH private key"
check_perms "/home/nova/.ssh/id_ed25519" "600" "SSH private key (ed25519)"

# Check for any world-readable .env files in nova-core
echo ""
echo "--- Scanning for exposed .env files ---"
while IFS= read -r -d '' envfile; do
    actual_mode=$(stat -c '%a' "$envfile" 2>/dev/null)
    others=$(( actual_mode % 10 ))
    if [ "$others" -ge 4 ]; then
        echo "[FAIL] $envfile — world-readable (mode=$actual_mode)"
        ERRORS=$((ERRORS + 1))
    fi
done < <(find /home/nova/nova-core -name '*.env' -type f -print0 2>/dev/null)

echo ""
echo "=== Results: $ERRORS errors, $WARNINGS warnings ==="

if [ "$ERRORS" -gt 0 ]; then
    echo "ACTION REQUIRED: Fix file permissions before proceeding!"
    exit 1
fi

exit 0
