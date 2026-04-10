# shell-ops Examples

## Example 1: Safe Read-Only Command

**User request:** "Show me disk usage for the nova-core directory"

**Workflow:**

1. Validate command `du -sh ~/nova-core` — no deny patterns matched.
2. Execute within sandbox with default timeout.
3. Capture exit code 0, stdout shows size summary.
4. Audit record logged.

**Output Contract:**

```
## CONTRACT
summary: Reported disk usage for ~/nova-core
commands_executed:
  - du -sh ~/nova-core (exit 0)
verification: Exit code 0, stdout contains size in human-readable format
```

---

## Example 2: Service Status Check

**User request:** "Check if the watcher service is running"

**Workflow:**

1. Validate command `systemctl status novacore-watcher` — allowed (read-only status check).
2. Execute with default timeout.
3. Capture exit code 0 (active) or 3 (inactive).
4. Parse output for `Active:` line to report status.
5. Audit record logged.

**Output Contract:**

```
## CONTRACT
summary: Checked novacore-watcher service status — active (running) since 09:54 UTC
commands_executed:
  - systemctl status novacore-watcher (exit 0)
verification: Active line confirms "active (running)", PID present in output
```

---

## Example 3: Log Tailing

**User request:** "Show me the last 50 lines of the watcher log"

**Workflow:**

1. Validate command `tail -n 50 LOGS/watcher.log` — no deny patterns matched, path within sandbox.
2. Execute with default timeout.
3. Capture exit code 0, stdout contains log lines.
4. Redact any secret values that appear in log output.
5. Audit record logged.

**Output Contract:**

```
## CONTRACT
summary: Retrieved last 50 lines of LOGS/watcher.log
commands_executed:
  - tail -n 50 LOGS/watcher.log (exit 0)
verification: Exit code 0, output contains 50 lines with recent timestamps
```

---

## Example 4: Blocked Destructive Command

**User request:** "Wipe the disk with dd"

**Workflow:**

1. Runner checks command `dd if=/dev/zero of=/dev/sda` against deny patterns.
2. Pattern `\bdd\b.*\bof\s*=\s*\/` matches — **BLOCKED**.
3. Command never reaches the shell.
4. Return exit code 126 with reason: "dd write to system path".
5. Audit record logged with `ok: false`.

**Output Contract:**

```
## CONTRACT
summary: Refused to execute dd write to system path — blocked by runner deny pattern
commands_executed:
  - dd if=/dev/zero of=/dev/sda (exit 126, BLOCKED)
verification: Command was never executed; runner returned deny reason in stderr
```

---

## Example 5: Command Requiring Confirmation

**User request:** "Install requests via apt"

**Workflow:**

1. Runner checks command `apt install python3-requests` against deny patterns.
2. Package manager pattern matches — requires confirmation.
3. Check `NOVACORE_CONFIRM` environment variable — not set.
4. **BLOCKED** with exit code 126: "package manager (apt) requires approval."
5. Report to user: set `NOVACORE_CONFIRM=ALLOW_DESTRUCTIVE` to override, or use `pip install requests` instead.
6. Audit record logged with `ok: false`.

**Output Contract:**

```
## CONTRACT
summary: Refused apt install — package manager requires NOVACORE_CONFIRM=ALLOW_DESTRUCTIVE
commands_executed:
  - apt install python3-requests (exit 126, BLOCKED)
verification: Command was never executed; suggested pip alternative to user
```
