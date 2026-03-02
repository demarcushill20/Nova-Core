---
name: shell-ops
description: "Execute safe shell commands inside the NovaCore runtime sandbox while respecting runner deny rules and confirmation policies."
activation:
  keywords:
    - bash
    - shell
    - sudo
    - pip
    - python
    - script
    - command
    - process
  when:
    - Shell command execution requested
    - System inspection required (disk, processes, environment)
    - Service status or logs requested
tool_doctrine:
  runtime:
    workflow:
      - sandbox_only
      - never_bypass_runner
      - respect_deny_patterns
      - require_confirmation_for_sensitive
output_contract:
  required:
    - summary
    - commands_executed
    - verification
---

# When To Use

- Running Python scripts or other executables inside `~/nova-core`.
- Installing Python packages via pip.
- Inspecting system state (disk usage, processes, environment variables).
- Checking service status or tailing logs.
- Piping or chaining commands for data processing within the sandbox.

# Workflow

1. **Validate command** — runner checks the command against deny patterns before execution. Blocked commands never reach the shell.
2. **Confirm sandbox** — working directory must be `~/nova-core` or a subdirectory. Paths outside the sandbox are rejected.
3. **Execute** — run the command via `bash -lc` with a timeout (default 120s, max 600s).
4. **Capture output** — collect exit code, stdout, and stderr. Truncate at 100 KB.
5. **Redact secrets** — strip known secret keys and token prefixes from all output.
6. **Log** — append an audit record to `STATE/tool_audit.jsonl`.

For deny pattern details, confirmation overrides, and exit code interpretation, see `reference.md`.

# Tool Usage Rules

- All commands execute within `~/nova-core`. Subdirectory `cwd` is allowed but must stay inside the sandbox.
- Never attempt to bypass the runner's deny patterns. They exist to prevent catastrophic operations.
- Destructive commands (`rm -rf /`, `dd`, `mkfs`, `shutdown`, etc.) are blocked by regex deny rules.
- Package managers (`apt`, `dnf`, `yum`) require confirmation via `NOVACORE_CONFIRM=ALLOW_DESTRUCTIVE`.
- Pipe-to-shell patterns (`curl | bash`) are always blocked.
- Timeout long-running commands — default 120s, configurable up to 600s.
- Capture both stdout and stderr for logging and debugging.

# Verification

After every command execution:

1. Check the exit code — 0 means success, non-zero means failure.
2. Review stderr for warnings or errors even when exit code is 0.
3. For state-changing commands, run a follow-up read-only command to confirm the effect.
4. Confirm the audit log entry was written to `STATE/tool_audit.jsonl`.

# Failure Handling

- **Exit code 126** (blocked by runner): the command matched a deny pattern. Do not retry; rephrase or use a safer alternative.
- **Exit code 127** (command not found): the binary does not exist. Check the path or install the dependency.
- **Exit code -1** (timeout): the command exceeded its timeout. Consider increasing the timeout or breaking the work into smaller steps.
- **Permission denied**: report the error clearly. Do not attempt `sudo` or `chmod` workarounds unless explicitly sanctioned.
- **Secret leaked in output**: the runner redacts known patterns automatically, but if you suspect a leak, flag it immediately.

# Output Contract

Every shell-ops execution must end with a machine-checkable contract:

```
## CONTRACT
summary: <one-line description of what was done>
commands_executed:
  - <command> (exit <code>)
verification: <how correctness was confirmed>
```

See `examples.md` for concrete instances.
