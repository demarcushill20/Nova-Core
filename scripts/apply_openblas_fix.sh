#!/bin/bash
# Permanent OpenBLAS Threading Fix
# This script applies the OpenBLAS fix system-wide

set -e

echo "=== Applying OpenBLAS Threading Fix ==="

# 1. Add to shell profiles
if ! grep -q "OPENBLAS_NUM_THREADS" ~/.bashrc; then
    echo "" >> ~/.bashrc
    echo "# OpenBLAS Threading Fix" >> ~/.bashrc
    echo "export OPENBLAS_NUM_THREADS=2" >> ~/.bashrc
    echo "export OMP_NUM_THREADS=2" >> ~/.bashrc
    echo "export MKL_NUM_THREADS=2" >> ~/.bashrc
    echo "Added OpenBLAS fix to ~/.bashrc"
fi

# 2. Add to profile for non-interactive shells
if ! grep -q "OPENBLAS_NUM_THREADS" ~/.profile; then
    echo "" >> ~/.profile
    echo "# OpenBLAS Threading Fix" >> ~/.profile
    echo "export OPENBLAS_NUM_THREADS=2" >> ~/.profile
    echo "export OMP_NUM_THREADS=2" >> ~/.profile
    echo "export MKL_NUM_THREADS=2" >> ~/.profile
    echo "Added OpenBLAS fix to ~/.profile"
fi

# 3. Update systemd service files
echo "Updating systemd service files..."

# Update novacore services to include environment variables
for service in novacore-watcher novacore-telegram novacore-telegram-notifier; do
    service_file="/etc/systemd/system/${service}.service"
    if [ -f "$service_file" ] && ! grep -q "OPENBLAS_NUM_THREADS" "$service_file"; then
        echo "Adding OpenBLAS fix to $service_file (requires sudo)"
        # Create a temporary file with the fix
        cat > /tmp/${service}_env_fix << 'EOF'
# Add these lines to the [Service] section:
Environment="OPENBLAS_NUM_THREADS=2"
Environment="OMP_NUM_THREADS=2"
Environment="MKL_NUM_THREADS=2"
EOF
        echo "Created fix for $service_file in /tmp/${service}_env_fix"
        echo "Manual step required: sudo systemctl edit $service"
    fi
done

# 4. Apply to current session
export OPENBLAS_NUM_THREADS=2
export OMP_NUM_THREADS=2
export MKL_NUM_THREADS=2

echo "✅ OpenBLAS fix applied to current session and profiles"
echo "✅ Tests should now run without threading errors"
echo ""
echo "Manual steps (if services need updating):"
echo "  sudo systemctl edit novacore-watcher"
echo "  sudo systemctl edit novacore-telegram"
echo "  sudo systemctl edit novacore-telegram-notifier"
echo "  # Add the Environment lines from /tmp/*_env_fix files"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl restart novacore-*"