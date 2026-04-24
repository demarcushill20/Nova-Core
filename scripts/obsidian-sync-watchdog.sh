#!/bin/bash
# Obsidian Sync Watchdog
# Restarts obsidian-headless-sync if sync.log hasn't been written to in 1 hour.
# Designed to catch silent WebSocket disconnects where the process stays alive
# but stops syncing.

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"

SYNC_LOG="/home/nova/.config/obsidian-headless/sync/976ea1245cbf37fc6ac815d4bf8d313c/sync.log"
SERVICE="obsidian-headless-sync.service"
STALE_SECONDS=3600  # 1 hour

if [ ! -f "$SYNC_LOG" ]; then
    echo "$(date -u +%FT%TZ) WARN: sync.log not found, skipping check"
    exit 0
fi

last_modified=$(stat -c %Y "$SYNC_LOG")
now=$(date +%s)
age=$(( now - last_modified ))

if [ "$age" -gt "$STALE_SECONDS" ]; then
    echo "$(date -u +%FT%TZ) ALERT: sync.log is ${age}s stale (threshold: ${STALE_SECONDS}s). Restarting $SERVICE"
    systemctl --user restart "$SERVICE"
    echo "$(date -u +%FT%TZ) Service restarted"
else
    echo "$(date -u +%FT%TZ) OK: sync.log age ${age}s (threshold: ${STALE_SECONDS}s)"
fi
