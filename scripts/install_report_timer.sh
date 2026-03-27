#!/bin/bash
# Install the NovaCore autonomous report systemd timer.
# Run as root (or with sudo) on the host system.
set -euo pipefail

SRC="/home/nova/nova-core/systemd"
DST="/etc/systemd/system"

echo "Installing novacore-report.{service,timer}..."
cp "$SRC/novacore-report.service" "$DST/"
cp "$SRC/novacore-report.timer" "$DST/"

systemctl daemon-reload
systemctl enable novacore-report.timer
systemctl start novacore-report.timer

echo "Done. Timer status:"
systemctl status novacore-report.timer --no-pager
