#!/bin/bash
# Install NovaCore Morning Schedule systemd timer.
# Runs: daily at 6:00 AM Central Time (America/Chicago)
#
# Usage: sudo bash scripts/install_morning_schedule.sh

set -euo pipefail

SRC="/home/nova/nova-core/systemd"
DEST="/etc/systemd/system"

echo "Installing NovaCore Morning Schedule timer..."

# Copy unit files
cp "$SRC/novacore-morning-schedule.service" "$DEST/"
cp "$SRC/novacore-morning-schedule.timer" "$DEST/"

# Reload systemd
systemctl daemon-reload

# Enable and start the timer
systemctl enable novacore-morning-schedule.timer
systemctl start novacore-morning-schedule.timer

echo ""
echo "Installed successfully."
echo "Timer status:"
systemctl status novacore-morning-schedule.timer --no-pager
echo ""
echo "Next trigger:"
systemctl list-timers novacore-morning-schedule.timer --no-pager
echo ""
echo "To test manually: systemctl start novacore-morning-schedule.service"
