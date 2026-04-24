#!/usr/bin/env bash
# fix_ssh_keepalive.sh — Install SSH server keepalive settings
# Run with: sudo bash scripts/fix_ssh_keepalive.sh
#
# Prevents long-lived SSH sessions from silently dropping by enabling
# server-side keepalive probes every 60 seconds.
set -euo pipefail

CONF="/etc/ssh/sshd_config.d/60-keepalive.conf"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Must run as root.  Usage: sudo bash $0"
    exit 1
fi

cat > "$CONF" <<'EOF'
# Server-side SSH keepalive — prevents idle session drops
ClientAliveInterval 60
ClientAliveCountMax 3
EOF

chmod 600 "$CONF"
echo "Wrote $CONF"

# Validate and reload
sshd -t && systemctl reload sshd
echo "sshd reloaded. Server-side keepalives are now active."
echo ""
echo "CLIENT-SIDE: Also add this to ~/.ssh/config on your local machine:"
echo ""
echo "  Host *"
echo "      ServerAliveInterval 60"
echo "      ServerAliveCountMax 3"
