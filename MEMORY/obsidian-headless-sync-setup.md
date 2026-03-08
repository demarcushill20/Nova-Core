# Obsidian Headless Sync — Operator Setup Guide

## Prerequisites

- Active Obsidian Sync subscription ($4/month)
- Vault exists at `/home/nova/nova-vault/`

## Current State (Session 35)

Already completed by automated setup:
- Node.js 22.22.1 installed via nvm at `/home/nova/.nvm/versions/node/v22.22.1/`
- obsidian-headless 0.0.6 installed globally (`ob` CLI)
- ob binary: `/home/nova/.nvm/versions/node/v22.22.1/bin/ob`
- systemd service file: `~/.config/systemd/user/obsidian-headless-sync.service`
- loginctl linger enabled
- `.nova-sync-config.json` created (disabled, awaiting auth)

## Remaining Operator Steps

### Step 1: Authenticate (MANUAL — requires credentials + 2FA)

```bash
export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh" && nvm use 22

ob login
# Enter your Obsidian account email, password, and 2FA code
```

### Step 2: Link the Vault

```bash
ob sync-list-remote          # find your vault name
ob sync-setup --vault "YOUR_VAULT_NAME" --device-name "nova-vps" --path /home/nova/nova-vault
```

### Step 3: Test One-Shot Sync

```bash
ob sync --path /home/nova/nova-vault
```

Verify notes appear on your phone within seconds.

### Step 4: Start the Daemon

```bash
systemctl --user daemon-reload
systemctl --user enable --now obsidian-headless-sync
systemctl --user status obsidian-headless-sync
```

### Step 5: Enable Nova-Core Sync

```bash
# Edit the existing config to enable sync:
python3 -c "
import json
p = '/home/nova/nova-vault/.nova-sync-config.json'
c = json.loads(open(p).read())
c['enabled'] = True
open(p, 'w').write(json.dumps(c, indent=2) + '\n')
print('Sync enabled:', c)
"
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
