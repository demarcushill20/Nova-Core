#!/usr/bin/env bash
# Activate the full-Python live pipeline for NovaTrade
# Switches from webhook (TradingView) to live (tick→strategy→execution)
#
# Usage:
#   sudo bash scripts/activate_live_pipeline.sh [strategy_config]
#
# Default strategy: configs/strategies/irb_v4_enhanced.yaml
#
# What this does:
#   1. Adds NOVATRADE_PIPELINE=live to /etc/novacore/novatrade.env
#   2. Adds NOVATRADE_STRATEGY_CONFIG to /etc/novacore/novatrade.env
#   3. Restarts the novacore-novatrade service
#
# Requirements:
#   - Must be run as root (sudo) to modify /etc/novacore/novatrade.env
#   - Service must be enabled: systemctl is-enabled novacore-novatrade

set -euo pipefail

ENV_FILE="/etc/novacore/novatrade.env"
STRATEGY_CONFIG="${1:-/home/nova/nova-core/configs/strategies/irb_v4_enhanced.yaml}"

echo "=== NovaTrade Live Pipeline Activation ==="
echo "  ENV_FILE:        $ENV_FILE"
echo "  STRATEGY_CONFIG: $STRATEGY_CONFIG"
echo ""

# Validate strategy config exists
if [[ ! -f "$STRATEGY_CONFIG" ]]; then
    echo "ERROR: Strategy config not found: $STRATEGY_CONFIG"
    exit 1
fi

# Validate env file exists
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: Env file not found: $ENV_FILE"
    exit 1
fi

# Check if already configured
if grep -q "^NOVATRADE_PIPELINE=" "$ENV_FILE" 2>/dev/null; then
    echo "WARNING: NOVATRADE_PIPELINE already set in $ENV_FILE"
    grep "^NOVATRADE_PIPELINE=" "$ENV_FILE"
    echo "Updating..."
    sed -i "s|^NOVATRADE_PIPELINE=.*|NOVATRADE_PIPELINE=live|" "$ENV_FILE"
else
    echo "Adding NOVATRADE_PIPELINE=live"
    echo "" >> "$ENV_FILE"
    echo "# --- Pipeline Mode (live = full-Python, webhook = TradingView) ---" >> "$ENV_FILE"
    echo "NOVATRADE_PIPELINE=live" >> "$ENV_FILE"
fi

if grep -q "^NOVATRADE_STRATEGY_CONFIG=" "$ENV_FILE" 2>/dev/null; then
    echo "WARNING: NOVATRADE_STRATEGY_CONFIG already set in $ENV_FILE"
    grep "^NOVATRADE_STRATEGY_CONFIG=" "$ENV_FILE"
    echo "Updating..."
    sed -i "s|^NOVATRADE_STRATEGY_CONFIG=.*|NOVATRADE_STRATEGY_CONFIG=$STRATEGY_CONFIG|" "$ENV_FILE"
else
    echo "Adding NOVATRADE_STRATEGY_CONFIG=$STRATEGY_CONFIG"
    echo "NOVATRADE_STRATEGY_CONFIG=$STRATEGY_CONFIG" >> "$ENV_FILE"
fi

echo ""
echo "Updated env file:"
grep -E "^NOVATRADE_(PIPELINE|STRATEGY)" "$ENV_FILE"
echo ""

# Show current service status
echo "Current service status:"
systemctl status novacore-novatrade.service --no-pager -l 2>/dev/null | head -5 || true
echo ""

echo "=== Ready to restart ==="
echo "Run:  sudo systemctl restart novacore-novatrade.service"
echo "Then: journalctl -u novacore-novatrade.service -f"
echo ""
echo "To verify live pipeline is active, check:"
echo "  curl http://localhost:8877/health"
echo "  (should show 'pipeline': 'live')"
