#!/bin/bash
# Nova-Link setup — run with: sudo bash nova-link/setup.sh
set -e

echo "=== Nova-Link Setup ==="

# 1. Install nginx + certbot if needed
if ! command -v nginx &>/dev/null; then
    echo "[1/5] Installing nginx and certbot..."
    apt-get update -qq
    apt-get install -y -qq nginx certbot python3-certbot-nginx
else
    echo "[1/5] nginx already installed"
fi

# 2. Create nginx config
echo "[2/5] Configuring nginx..."
cat > /etc/nginx/sites-available/nova-link << 'NGINX'
server {
    listen 80;
    server_name nova-link.duckdns.org;

    location / {
        proxy_pass http://127.0.0.1:8420;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /ws {
        proxy_pass http://127.0.0.1:8420/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/nova-link /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
echo "  nginx configured"

# 3. SSL certificate
echo "[3/5] Getting SSL certificate..."
certbot --nginx -d nova-link.duckdns.org --non-interactive --agree-tos --email noreply@nova-link.duckdns.org --redirect || {
    echo "  SSL failed — app will work on HTTP for now. Run certbot manually later."
}

# 4. Create systemd service
echo "[4/5] Creating systemd service..."
cat > /etc/systemd/system/nova-link.service << SERVICE
[Unit]
Description=Nova-Link API & PWA
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=nova
WorkingDirectory=/home/nova/nova-core
ExecStart=/home/nova/.local/bin/uvicorn nova-link.api:app --host 127.0.0.1 --port 8420 --workers 1
Restart=always
RestartSec=5
Environment=PATH=/home/nova/.local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable nova-link
systemctl restart nova-link
echo "  nova-link service started"

# 5. Open firewall if ufw is active
if command -v ufw &>/dev/null && ufw status | grep -q "active"; then
    ufw allow 80/tcp
    ufw allow 443/tcp
    echo "[5/5] Firewall ports opened"
else
    echo "[5/5] No firewall or not active — skipping"
fi

echo ""
echo "=== Nova-Link is live! ==="
echo "  URL: https://nova-link.duckdns.org"
echo "  API docs: https://nova-link.duckdns.org/api/docs"
echo ""
echo "On your iPhone:"
echo "  1. Open Safari → https://nova-link.duckdns.org"
echo "  2. Tap Share → Add to Home Screen"
echo "  3. Nova-Link appears as an app!"
