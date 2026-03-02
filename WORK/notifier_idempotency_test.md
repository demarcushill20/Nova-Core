# Notifier Idempotency Mechanism

## Root Cause of Duplicates

**Two independent causes found:**

### Cause 1: Two notifier processes running simultaneously
- PID 149703: systemd service `novacore-telegram-notifier.service` (Restart=always)
- PID 150798: manual `nohup` launch from Claude Code session
- Both watched OUTPUT/ independently, both saw the same file, both sent.

### Cause 2: Intra-process race (watchdog events)
Even with one process, watchdog fires both `on_created` (for the .tmp file rename)
and `on_moved` (for the final filename) for the same output file. The old
`Path.exists()` + `write_text()` dedup was NOT atomic — both callbacks could read
"no marker" before either wrote one.

## Solution: Atomic O_CREAT|O_EXCL Marker Files

### How it works
```python
fd = os.open(marker_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
```

`O_CREAT|O_EXCL` is **atomic at the kernel level**. If two processes (or threads)
race on the same path, exactly one gets the file descriptor and the other gets
`FileExistsError`. No race window, no TOCTOU bug.

### Marker contents
```
2026-03-01 23:17:02 UTC
pid=151783 host=vultr
```

PID + hostname in marker enables post-mortem diagnosis if duplicates ever recur.

### Failure rollback
If `send_message_chunked()` raises an exception, the marker is removed via
`unclaim_send()` so the notification can be retried on next event/restart.

### Debug footer
Each Telegram message now includes a footer line:
```
---
notifier_pid=151783 host=vultr
```
If duplicates recur, different PIDs = multiple processes; same PID = code bug.

### Cleanup
Markers older than 7 days are purged on startup (`MARKER_MAX_AGE_DAYS = 7`).

### Legacy migration
On first run, entries from `tg_sent_outputs.txt` are atomically converted to
marker files, then the file is renamed `.txt.migrated`.

## Process Discipline

The notifier MUST run as exactly one process. The canonical launch method is:
```
systemctl restart novacore-telegram-notifier
```

**Never** launch a second notifier via `nohup` or in tmux. The systemd service
has `Restart=always`, so killing it will auto-restart with the latest code.

## Validation

```
$ python3 WORK/notifier_idempotency_diag.py
OUTPUT files (tg_*): 15
Marker files:        15
Duplicates:          0
Result:              PASS
```

## Files Changed

- `telegram_notifier.py` — atomic claim_send/unclaim_send, PID footer, remove threading lock
