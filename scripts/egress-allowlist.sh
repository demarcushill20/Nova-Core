#!/usr/bin/env bash
# =============================================================================
# egress-allowlist.sh  --  Phase 2.1 Network Egress Allowlisting
# =============================================================================
# Restricts outbound traffic for the 'nova' user to an explicit allowlist
# using iptables.  Only the services NovaCore actually needs can be reached.
#
# Usage:
#   sudo ./egress-allowlist.sh              # Apply rules
#   sudo ./egress-allowlist.sh --dry-run    # Print rules without applying
#   sudo ./egress-allowlist.sh --remove     # Tear down all NOVA_EGRESS rules
#
# Requirements: root / sudo, iptables, dig (dnsutils)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CHAIN="NOVA_EGRESS"
NOVA_USER="nova"
LOG_PREFIX="EGRESS-BLOCKED: "
LOG_FILE="/home/nova/nova-core/LOGS/egress-allowlist.log"

# Allowed HTTPS (port 443) destinations -- hostnames resolved at script time
ALLOWED_HOSTS_443=(
    api.anthropic.com
    api.telegram.org
    github.com
    api.search.brave.com
    api.tavily.com
)

# Wildcard domains that need DNS resolution (*.pinecone.io)
PINECONE_LOOKUP_HOSTS=(
    controller.us-east-1-aws.pinecone.io
    us-east-1-aws.pinecone.io
)

# DNS servers allowed (port 53 UDP+TCP)
DNS_SERVERS=(1.1.1.1 9.9.9.9)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log_event() {
    local msg="$1"
    local ts
    ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "[$ts] $msg" | tee -a "$LOG_FILE"
}

resolve_host() {
    # Resolve a hostname to all A-record IPs, one per line.
    # Falls back to getent if dig is unavailable.
    local host="$1"
    if command -v dig &>/dev/null; then
        dig +short A "$host" 2>/dev/null | grep -E '^[0-9]+\.' || true
    elif command -v getent &>/dev/null; then
        getent ahosts "$host" 2>/dev/null | awk '{print $1}' | sort -u || true
    else
        echo "ERROR: neither dig nor getent available -- cannot resolve $host" >&2
        return 1
    fi
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --dry-run   Print the iptables commands without executing them
  --remove    Remove the NOVA_EGRESS chain and all references to it
  -h, --help  Show this help message

Must be run as root (sudo).
EOF
    exit 0
}

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: This script must be run as root (use sudo)." >&2
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

# Run or print an iptables command depending on DRY_RUN
ipt() {
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "[dry-run] iptables $*"
    else
        iptables "$@"
    fi
}

remove_rules() {
    echo "--- Removing NOVA_EGRESS rules ---"

    # Remove jump rule(s) from OUTPUT that reference our chain
    while iptables -C OUTPUT -m owner --uid-owner "$NOVA_USER" -j "$CHAIN" 2>/dev/null; do
        if [[ "$DRY_RUN" == "true" ]]; then
            echo "[dry-run] iptables -D OUTPUT -m owner --uid-owner $NOVA_USER -j $CHAIN"
            break
        else
            iptables -D OUTPUT -m owner --uid-owner "$NOVA_USER" -j "$CHAIN"
        fi
    done

    # Flush and delete the chain (if it exists)
    if iptables -L "$CHAIN" -n &>/dev/null; then
        ipt -F "$CHAIN"
        ipt -X "$CHAIN"
        echo "Chain $CHAIN removed."
        log_event "NOVA_EGRESS chain removed"
    else
        echo "Chain $CHAIN does not exist -- nothing to remove."
    fi
}

apply_rules() {
    echo "--- Applying NOVA_EGRESS egress allowlist ---"

    # If the chain already exists, tear it down first for idempotency
    if iptables -L "$CHAIN" -n &>/dev/null 2>&1; then
        echo "Chain $CHAIN already exists -- removing before re-applying."
        DRY_RUN_SAVED="$DRY_RUN"
        DRY_RUN="false"
        remove_rules
        DRY_RUN="$DRY_RUN_SAVED"
    fi

    # -----------------------------------------------------------------------
    # 1. Create the custom chain
    # -----------------------------------------------------------------------
    ipt -N "$CHAIN"

    # -----------------------------------------------------------------------
    # 2. Allow established / related connections (return traffic)
    # -----------------------------------------------------------------------
    ipt -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # -----------------------------------------------------------------------
    # 3. Allow loopback (localhost -- any port for Redis, MCP, etc.)
    # -----------------------------------------------------------------------
    ipt -A "$CHAIN" -o lo -j ACCEPT
    ipt -A "$CHAIN" -d 127.0.0.0/8 -j ACCEPT

    # -----------------------------------------------------------------------
    # 4. Allow DNS (UDP + TCP port 53) to specific resolvers
    # -----------------------------------------------------------------------
    for dns in "${DNS_SERVERS[@]}"; do
        ipt -A "$CHAIN" -p udp -d "$dns" --dport 53 -j ACCEPT
        ipt -A "$CHAIN" -p tcp -d "$dns" --dport 53 -j ACCEPT
    done

    # -----------------------------------------------------------------------
    # 5. Allow NTP (UDP port 123) to any destination
    # -----------------------------------------------------------------------
    ipt -A "$CHAIN" -p udp --dport 123 -j ACCEPT

    # -----------------------------------------------------------------------
    # 6. Resolve and allow HTTPS (port 443) for each allowed host
    # -----------------------------------------------------------------------
    for host in "${ALLOWED_HOSTS_443[@]}"; do
        local ips
        ips="$(resolve_host "$host")"
        if [[ -z "$ips" ]]; then
            echo "WARNING: Could not resolve $host -- skipping" >&2
            continue
        fi
        while IFS= read -r ip; do
            [[ -z "$ip" ]] && continue
            ipt -A "$CHAIN" -p tcp -d "$ip" --dport 443 -j ACCEPT \
                -m comment --comment "allow $host"
        done <<< "$ips"
        echo "  allowed: $host -> $(echo "$ips" | tr '\n' ' ')"
    done

    # Pinecone wildcard -- resolve known subdomains
    for host in "${PINECONE_LOOKUP_HOSTS[@]}"; do
        local ips
        ips="$(resolve_host "$host")"
        if [[ -z "$ips" ]]; then
            echo "WARNING: Could not resolve $host -- skipping" >&2
            continue
        fi
        while IFS= read -r ip; do
            [[ -z "$ip" ]] && continue
            ipt -A "$CHAIN" -p tcp -d "$ip" --dport 443 -j ACCEPT \
                -m comment --comment "allow $host (pinecone)"
        done <<< "$ips"
        echo "  allowed: $host -> $(echo "$ips" | tr '\n' ' ')"
    done

    # -----------------------------------------------------------------------
    # 7. Log and DROP everything else
    # -----------------------------------------------------------------------
    ipt -A "$CHAIN" -j LOG --log-prefix "$LOG_PREFIX" --log-level 4
    ipt -A "$CHAIN" -j DROP

    # -----------------------------------------------------------------------
    # 8. Jump from OUTPUT into our chain for the nova user
    # -----------------------------------------------------------------------
    ipt -A OUTPUT -m owner --uid-owner "$NOVA_USER" -j "$CHAIN"

    echo "--- NOVA_EGRESS applied ---"
    log_event "NOVA_EGRESS chain applied ($(echo "${ALLOWED_HOSTS_443[*]}" | tr ' ' ','))"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
DRY_RUN="false"
ACTION="apply"

for arg in "$@"; do
    case "$arg" in
        --dry-run)  DRY_RUN="true" ;;
        --remove)   ACTION="remove" ;;
        -h|--help)  usage ;;
        *)
            echo "Unknown argument: $arg" >&2
            usage
            ;;
    esac
done

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

if [[ "$DRY_RUN" != "true" ]]; then
    require_root
fi

case "$ACTION" in
    apply)  apply_rules  ;;
    remove) remove_rules ;;
esac

echo "Done."
