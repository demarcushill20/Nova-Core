#!/bin/bash
# Install the novacore-auto-deploy timer.
# Must be run with sudo: sudo bash scripts/install_auto_deploy.sh

set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)/systemd"
DEST="/etc/systemd/system"

echo "Installing novacore-auto-deploy units..."

cp "$SRC_DIR/novacore-auto-deploy.service" "$DEST/"
cp "$SRC_DIR/novacore-auto-deploy.timer"   "$DEST/"

systemctl daemon-reload
systemctl enable --now novacore-auto-deploy.timer

echo "Installed and started. Verify with:"
echo "  systemctl status novacore-auto-deploy.timer"
echo "  systemctl list-timers novacore-auto-deploy.timer"
