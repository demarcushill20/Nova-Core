#!/bin/bash
# Install the NovaCore daily executive summary systemd timer.
# Run as root (or with sudo) on the host system.
#
# Schedule: 23:20 America/Chicago (Central Time) daily
# Persistence: true (catches up if missed)
set -euo pipefail

SRC="/home/nova/nova-core/systemd"
DST="/etc/systemd/system"

echo "Installing novacore-daily-summary.{service,timer}..."
cp "$SRC/novacore-daily-summary.service" "$DST/"
cp "$SRC/novacore-daily-summary.timer" "$DST/"

systemctl daemon-reload
systemctl enable novacore-daily-summary.timer
systemctl start novacore-daily-summary.timer

echo "Done. Timer status:"
systemctl status novacore-daily-summary.timer --no-pager
echo ""
echo "Next trigger:"
systemctl list-timers novacore-daily-summary.timer --no-pager
