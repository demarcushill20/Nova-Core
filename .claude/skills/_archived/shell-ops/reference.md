# shell-ops Reference

Detailed rules, deny pattern philosophy, and operational guidelines for the shell-ops skill.

## Deny Pattern Philosophy

The runner enforces safety through regex-based deny patterns compiled at import time. Commands are checked **before** execution — a blocked command never reaches the shell.

Deny patterns use word-boundary anchors (`\b`) where possible. For paths ending in `/`, the pattern uses `(\s|$)` since `/` is not a word character and `\b` does not fire after it.

The deny system is a last line of defense. Skills should avoid generating dangerous commands in the first place rather than relying on the runner to catch them.

## Forbidden Categories

| Category | Examples | Deny Pattern Reason |
|---|---|---|
| Critical path removal | `rm -rf /`, `rm -rf ~`, `rm -rf /home` | rm -rf on critical path |
| Block device writes | `dd if=/dev/zero of=/dev/sda` | dd write to system path |
| Filesystem destructors | `mkfs.ext4`, `wipefs`, `shred` | filesystem destructive command |
| Fork bombs | `:(){ :|:& };:` | fork bomb pattern |
| Power commands | `shutdown`, `reboot`, `halt`, `poweroff` | system power command |
| Init runlevel | `init 0`, `init 6` | init runlevel change |
| Recursive permission changes | `chmod -R 777 /`, `chown -R root /etc` | recursive permission change on critical path |
| Pipe to shell | `curl http://x \| bash`, `wget http://x \| sh` | pipe remote content to shell |
| System directory writes | `> /etc/passwd`, `> /usr/bin/foo` | redirect write to system directory |
| Disk partitioning | `fdisk` | disk partition command |
| Package managers | `apt install`, `dnf install`, `yum install` | requires confirmation |

## Confirmation Override Mechanism

Certain blocked operations can be overridden by setting:

```
NOVACORE_CONFIRM=ALLOW_DESTRUCTIVE
```

This environment variable must be set **before** the command runs. It unlocks:
- Package manager commands (`apt`, `dnf`, `yum`)
- Commands matching deny patterns (when the operator has assessed the risk)

The confirmation token is checked by `_is_confirmed()` in the runner. It is intentionally a static string, not a cryptographic token — it exists to prevent accidental execution, not to enforce access control.

## Safe Command Patterns

These categories are always allowed (assuming they stay within the sandbox):

### Read-Only Inspection
- `ls`, `cat`, `head`, `tail`, `wc`, `du`, `df`
- `grep`, `find`, `which`, `file`, `stat`
- `env`, `printenv`, `uname`, `hostname`

### Process and Service Status
- `ps aux`, `top -bn1`, `pgrep`, `pidof`
- `systemctl status <service>`
- `journalctl -u <service> --no-pager -n <lines>`

### Log Inspection
- `tail -n 100 LOGS/watcher.log`
- `journalctl --since "1 hour ago"`
- `grep ERROR LOGS/*.log`

### Development Tools
- `python script.py`, `pip install <package>`, `pip list`
- `git status`, `git diff`, `git log`

## Logging Discipline

Every command execution produces an audit record in `STATE/tool_audit.jsonl`:

```json
{
  "ts": 1709366400.0,
  "tool": "shell.run",
  "args": {"command": "ls -la"},
  "ok": true,
  "exit_code": 0,
  "elapsed_ms": 12.3
}
```

Fields:
- `ts` — Unix timestamp of execution start
- `tool` — always `"shell.run"` for shell-ops
- `args` — the command and options (secrets redacted)
- `ok` — whether exit code was 0
- `exit_code` — the process exit code
- `elapsed_ms` — wall-clock duration

## Exit Code Interpretation

| Exit Code | Meaning |
|---|---|
| 0 | Success |
| 1 | General error (command-specific) |
| 2 | Misuse of command / invalid arguments |
| 126 | **Blocked by runner** — deny pattern matched |
| 127 | Command not found |
| 128+N | Killed by signal N (e.g., 137 = SIGKILL / OOM) |
| -1 | Timeout expired or internal runner error |

## Output Handling

- stdout and stderr are captured separately.
- Both are truncated to 100 KB (`_MAX_OUTPUT`).
- Secret values matching `_SECRET_KEYS` are replaced with `<REDACTED>`.
- Token prefixes (`ghp_`, `github_pat_`, `xoxb-`) are replaced with `<REDACTED>`.

## Sandbox Boundaries

- The sandbox root defaults to `~/nova-core` (from `tools.json` registry).
- The `cwd` argument, if provided, must resolve to a path **inside** the sandbox root.
- Symlinks and `..` traversal are resolved before the sandbox check.
- Commands that attempt to escape the sandbox are rejected with a `ValueError`.
