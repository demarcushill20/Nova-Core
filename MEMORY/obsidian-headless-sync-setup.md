# Obsidian Headless Sync — Operator Setup Guide

## Prerequisites

- Node.js 22+ installed on the VPS
- Active Obsidian Sync subscription ($4/month)
- Vault exists at `/home/nova/nova-vault/`

## Step 1: Install Obsidian Headless

```bash
npm install -g obsidian-headless
```

Verify: `ob --version`

## Step 2: Authenticate

```bash
ob login
# Enter your Obsidian account email, password, and 2FA code
```

## Step 3: Link the Vault

```bash
ob sync-list-remote          # find your vault name
cd /home/nova/nova-vault
ob sync-setup --vault "Nova-Core Open Memory" --device-name "nova-vps"
```

## Step 4: Test One-Shot Sync

```bash
ob sync --path /home/nova/nova-vault
```

Verify notes appear on your phone within seconds.

## Step 5: Enable Continuous Daemon

Create systemd service:

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/obsidian-headless-sync.service << 'EOF'
[Unit]
Description=Obsidian Headless Sync Daemon
After=network.target

[Service]
ExecStart=/usr/local/bin/ob sync --path /home/nova/nova-vault --continuous
Restart=on-failure
RestartSec=30s
Environment=NODE_ENV=production

[Install]
WantedBy=default.target
EOF

loginctl enable-linger nova
systemctl --user daemon-reload
systemctl --user enable --now obsidian-headless-sync
```

Check status: `systemctl --user status obsidian-headless-sync`

## Step 6: Enable Nova-Core Sync Integration

Create the sync config in the vault:

```bash
cat > /home/nova/nova-vault/.nova-sync-config.json << 'EOF'
{
  "enabled": true,
  "mode": "continuous",
  "ob_binary": "ob",
  "vault_path": "/home/nova/nova-vault",
  "sync_timeout_seconds": 30,
  "check_daemon": true
}
EOF
```

## Verifying It Works

1. Run a task through the multi-agent path that produces a workflow learning
2. Check `LOGS/` or task output for `obsidian_sync` fields
3. Open Obsidian on phone — the new note should appear within seconds

## Disabling Sync

Set `"enabled": false` in `.nova-sync-config.json`, or stop the daemon:
```bash
systemctl --user stop obsidian-headless-sync
```

## Common Issues

| Issue | Fix |
|-------|-----|
| `ob: command not found` | Ensure Node.js 22+ and `npm install -g obsidian-headless` |
| Auth expired | Run `ob login` again |
| Daemon not starting | Check `journalctl --user -u obsidian-headless-sync` |
| Sync config missing | Nova-Core skips sync gracefully (fail-open) |
| Conflict on phone | Obsidian Sync handles conflicts natively |

## Important

- Do NOT run both the Obsidian desktop app's sync AND the headless daemon on the same machine simultaneously
- The daemon watches the filesystem — new files are synced automatically
- Nova-Core's sync integration only checks/reports status; the daemon does the actual syncing
- Sync failures never invalidate successful writes
