---
name: shell-ops
description: "Execute shell commands safely within the nova-core sandbox for system tasks, script execution, and environment inspection."
---

# shell-ops

## When to use

- Running Python scripts or other executables.
- Installing Python packages via pip.
- Inspecting system state (disk, processes, environment variables).
- Piping or chaining commands for data processing.

## Rules / Safety

- Working directory must be `~/nova-core` or a subdirectory thereof.
- Do not run commands that modify files outside `~/nova-core` without explicit user approval.
- No `rm -rf /`, `dd`, `mkfs`, or other destructive system-level commands.
- Timeout long-running commands (default: 120s).
- Do not install system packages (`apt`, `dnf`) without user confirmation.
- Capture both stdout and stderr for logging.

## Workflow

1. Validate the command does not violate safety rules.
2. Set working directory to `~/nova-core`.
3. Execute the command with a timeout.
4. Capture exit code, stdout, stderr.
5. Log the command and result to `LOGS/shell.log` if it mutates state.
6. Return output to the caller.

## Output format

```
[shell-ops] CMD: <command>
[shell-ops] EXIT: <code>
[shell-ops] STDOUT: <truncated output>
[shell-ops] STDERR: <truncated errors, if any>
```
