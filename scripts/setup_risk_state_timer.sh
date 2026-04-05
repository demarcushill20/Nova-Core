#!/bin/bash
# Setup systemd timer for automatic risk state updates
# This ensures the risk monitoring system always has fresh data

set -euo pipefail

NOVA_HOME="/home/nova/nova-core"
SERVICE_NAME="novacore-risk-state-updater"

echo "Setting up systemd timer for risk state updates..."

# Create the service file
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=NovaCore Risk State Updater
After=novacore-novatrade.service

[Service]
Type=oneshot
User=nova
Group=nova
WorkingDirectory=${NOVA_HOME}
Environment=PYTHONPATH=${NOVA_HOME}
ExecStart=/usr/bin/python3 ${NOVA_HOME}/scripts/risk_state_cron.py
StandardOutput=append:${NOVA_HOME}/LOGS/risk_state_cron.log
StandardError=append:${NOVA_HOME}/LOGS/risk_state_cron.log

[Install]
WantedBy=multi-user.target
EOF

# Create the timer file (runs every 5 minutes)
sudo tee /etc/systemd/system/${SERVICE_NAME}.timer > /dev/null <<EOF
[Unit]
Description=Run NovaCore Risk State Updater every 5 minutes
Requires=${SERVICE_NAME}.service

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
AccuracySec=30sec

[Install]
WantedBy=timers.target
EOF

# Create logs directory if it doesn't exist
mkdir -p "${NOVA_HOME}/LOGS"

# Reload systemd and enable the timer
echo "Reloading systemd and enabling timer..."
sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}.timer
sudo systemctl start ${SERVICE_NAME}.timer

# Show status
echo "Timer status:"
sudo systemctl status ${SERVICE_NAME}.timer --no-pager

echo "Setup complete! Risk state will be updated every 5 minutes."
echo "Monitor with: journalctl -u ${SERVICE_NAME}.service -f"
echo "Check logs: tail -f ${NOVA_HOME}/LOGS/risk_state_cron.log"
