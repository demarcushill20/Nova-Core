#!/bin/bash
# Setup user crontab for automatic risk state updates
# This ensures the risk monitoring system always has fresh data

set -euo pipefail

NOVA_HOME="/home/nova/nova-core"
CRON_JOB="*/5 * * * * cd ${NOVA_HOME} && /usr/bin/python3 scripts/risk_state_cron.py >> LOGS/risk_state_cron.log 2>&1"

echo "Setting up crontab for risk state updates..."

# Create logs directory if it doesn't exist
mkdir -p "${NOVA_HOME}/LOGS"

# Check if the cron job already exists
if crontab -l 2>/dev/null | grep -q "risk_state_cron.py"; then
    echo "Risk state cron job already exists. Updating..."

    # Remove existing job and add new one
    crontab -l 2>/dev/null | grep -v "risk_state_cron.py" | crontab -
    (crontab -l 2>/dev/null; echo "${CRON_JOB}") | crontab -
else
    echo "Adding new risk state cron job..."
    (crontab -l 2>/dev/null; echo "${CRON_JOB}") | crontab -
fi

echo "Cron job added successfully!"
echo "The risk state will be updated every 5 minutes."
echo ""
echo "Current crontab:"
crontab -l | grep -A1 -B1 risk_state_cron || echo "  (no other cron jobs)"
echo ""
echo "Monitor logs with: tail -f ${NOVA_HOME}/LOGS/risk_state_cron.log"

# Test the script once to make sure it works
echo "Running initial test..."
cd "${NOVA_HOME}"
python3 scripts/risk_state_cron.py

echo "Setup complete!"
