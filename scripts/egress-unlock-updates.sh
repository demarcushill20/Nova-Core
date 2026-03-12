#!/usr/bin/env bash
# =============================================================================
# egress-unlock-updates.sh  --  Temporary egress unlock for pip/apt updates
# =============================================================================
# Temporarily opens all outbound port 443 for the 'nova' user so that pip,
# apt, and other package managers can reach arbitrary HTTPS endpoints.
#
# After the specified duration the original NOVA_EGRESS rules are re-applied
# automatically.
#
# Usage:
#   sudo ./egress-unlock-updates.sh          # Unlock for 10 minutes (default)
#   sudo ./egress-unlock-updates.sh 5        # Unlock for 5 minutes
#   sudo ./egress-unlock-updates.sh 30       # Unlock for 30 minutes
#
# Requirements: root / sudo, iptables
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CHAIN="NOVA_EGRESS"
NOVA_USER="nova"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALLOWLIST_SCRIPT="$SCRIPT_DIR/egress-allowlist.sh"
LOG_FILE="/home/nova/nova-core/LOGS/egress-allowlist.log"
DEFAULT_DURATION_MINUTES=10

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log_event() {
    local msg="$1"
    local ts
    ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "[$ts] $msg" | tee -a "$LOG_FILE"
}

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: This script must be run as root (use sudo)." >&2
        exit 1
    fi
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [MINUTES]

Temporarily opens all outbound HTTPS (port 443) for the nova user.
After MINUTES (default: $DEFAULT_DURATION_MINUTES), the NOVA_EGRESS allowlist
is re-applied automatically.

Arguments:
  MINUTES   Duration of the unlock window (default: $DEFAULT_DURATION_MINUTES)
  -h, --help  Show this help

Must be run as root (sudo).
EOF
    exit 0
}

cleanup() {
    echo ""
    echo "--- Re-locking egress ---"
    relock
    exit 0
}

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

unlock() {
    # If the NOVA_EGRESS chain exists, flush it and replace with a permissive
    # HTTPS rule.  If it doesn't exist, create a temporary permissive chain.

    if iptables -L "$CHAIN" -n &>/dev/null; then
        # Flush existing restrictive rules
        iptables -F "$CHAIN"
        echo "Flushed existing $CHAIN rules."
    else
        # Chain doesn't exist -- create it and wire into OUTPUT
        iptables -N "$CHAIN"
        iptables -A OUTPUT -m owner --uid-owner "$NOVA_USER" -j "$CHAIN"
        echo "Created $CHAIN chain."
    fi

    # Allow all established/related
    iptables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # Allow loopback
    iptables -A "$CHAIN" -o lo -j ACCEPT
    iptables -A "$CHAIN" -d 127.0.0.0/8 -j ACCEPT

    # Allow DNS to configured resolvers
    for dns in 1.1.1.1 9.9.9.9; do
        iptables -A "$CHAIN" -p udp -d "$dns" --dport 53 -j ACCEPT
        iptables -A "$CHAIN" -p tcp -d "$dns" --dport 53 -j ACCEPT
    done

    # Allow NTP
    iptables -A "$CHAIN" -p udp --dport 123 -j ACCEPT

    # Allow ALL outbound HTTPS (the temporary unlock)
    iptables -A "$CHAIN" -p tcp --dport 443 -j ACCEPT \
        -m comment --comment "TEMP-UNLOCK: all HTTPS"

    # Allow HTTP too (apt often uses port 80)
    iptables -A "$CHAIN" -p tcp --dport 80 -j ACCEPT \
        -m comment --comment "TEMP-UNLOCK: all HTTP (apt)"

    # Still log+drop non-web traffic
    iptables -A "$CHAIN" -j LOG --log-prefix "EGRESS-BLOCKED: " --log-level 4
    iptables -A "$CHAIN" -j DROP

    log_event "EGRESS UNLOCKED for updates (${DURATION_MINUTES}m)"
    echo "Egress unlocked: all HTTPS/HTTP traffic allowed for nova user."
}

relock() {
    # Re-apply the full allowlist by running the companion script
    if [[ -x "$ALLOWLIST_SCRIPT" ]]; then
        echo "Re-applying egress allowlist via $ALLOWLIST_SCRIPT ..."
        "$ALLOWLIST_SCRIPT"
    else
        echo "WARNING: $ALLOWLIST_SCRIPT not found or not executable." >&2
        echo "Manually flushing $CHAIN and dropping all non-established traffic."
        if iptables -L "$CHAIN" -n &>/dev/null; then
            iptables -F "$CHAIN"
            iptables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
            iptables -A "$CHAIN" -o lo -j ACCEPT
            iptables -A "$CHAIN" -d 127.0.0.0/8 -j ACCEPT
            iptables -A "$CHAIN" -j LOG --log-prefix "EGRESS-BLOCKED: " --log-level 4
            iptables -A "$CHAIN" -j DROP
        fi
    fi
    log_event "EGRESS RE-LOCKED after update window"
    echo "Egress re-locked."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
DURATION_MINUTES="$DEFAULT_DURATION_MINUTES"

for arg in "$@"; do
    case "$arg" in
        -h|--help) usage ;;
        *)
            if [[ "$arg" =~ ^[0-9]+$ ]]; then
                DURATION_MINUTES="$arg"
            else
                echo "ERROR: Invalid argument '$arg'. Expected a number of minutes." >&2
                usage
            fi
            ;;
    esac
done

# Validate duration
if [[ "$DURATION_MINUTES" -lt 1 || "$DURATION_MINUTES" -gt 120 ]]; then
    echo "ERROR: Duration must be between 1 and 120 minutes." >&2
    exit 1
fi

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

require_root

echo "============================================="
echo "  Egress Unlock for Updates"
echo "  Duration: ${DURATION_MINUTES} minutes"
echo "============================================="
echo ""
echo "Press Ctrl+C at any time to re-lock immediately."
echo ""

# Trap SIGINT/SIGTERM to re-lock on early exit
trap cleanup INT TERM

# Unlock
unlock

# Countdown
TOTAL_SECONDS=$((DURATION_MINUTES * 60))
REMAINING=$TOTAL_SECONDS

echo ""
while [[ $REMAINING -gt 0 ]]; do
    MINS=$((REMAINING / 60))
    SECS=$((REMAINING % 60))
    printf "\r  Time remaining: %02d:%02d  " "$MINS" "$SECS"
    sleep 1
    REMAINING=$((REMAINING - 1))
done

echo ""
echo ""
echo "--- Timer expired ---"

# Re-lock
relock

echo ""
echo "Done. Egress is back to allowlist-only mode."
