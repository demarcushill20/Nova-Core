"""Tool runner for NovaCore.

Executes shell.run, git.run, and files.* tools with safety enforcement,
secret redaction, and audit logging.

Return format (consistent across all tools):
  ok:        bool   — True if the operation succeeded
  exit_code: int    — 0 on success, -1 on safety/validation error, else process code
  stdout:    str    — text output (empty for files.* tools)
  stderr:    str    — error message on failure (empty on success)
  result:    dict   — structured output from files.* tools (absent for shell/git)
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from tools.files import dispatch_files_tool
from tools.registry import load_registry, resolve_audit_log, resolve_sandbox_root

# --- Constants ---------------------------------------------------------------

_MAX_OUTPUT = 100 * 1024  # 100 KB truncation limit

_SECRET_KEYS = (
    "TELEGRAM_TOKEN",
    "BOT_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLAUDE_WEB_COOKIE",
    "SESSION_KEY",
)

_SECRET_RE = re.compile(
    r"(" + "|".join(re.escape(k) for k in _SECRET_KEYS) + r")"
    r"""([=:]["']?\s*)(\S+)""",
)

# Token-prefix patterns (GitHub PATs, Slack bot tokens, etc.)
_TOKEN_PREFIX_RE = re.compile(r"\b(ghp_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}|xoxb-[A-Za-z0-9\-]+)")

_CONFIRM_TOKEN = "ALLOW_DESTRUCTIVE"

# --- Shell denylist (regex-based, word-boundary aware) -----------------------

# High-risk destructive commands (always blocked unless confirmed)
_SHELL_DENY_PATTERNS = [
    # rm -rf / or ~ or /home etc.  (no \b after / — not a word char)
    (re.compile(r"\brm\s+(-\w*[rf]\w*\s+)*(\/|~|\$HOME|\/home|\/etc|\/usr|\/bin|\/lib)(\s|$)"),
     "rm -rf on critical path"),
    # dd writing to block devices or system paths
    (re.compile(r"\bdd\b.*\bof\s*=\s*\/"),
     "dd write to system path"),
    # Filesystem destructors
    (re.compile(r"\b(mkfs|wipefs|shred)\b"),
     "filesystem destructive command"),
    # Fork bomb
    (re.compile(r":\(\)\s*\{|:\(\){"),
     "fork bomb pattern"),
    # System power commands
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),
     "system power command"),
    (re.compile(r"\binit\s+[06]\b"),
     "init runlevel change"),
    # chmod/chown -R on critical paths (no \b after / — not a word char)
    (re.compile(r"\b(chmod|chown)\b.*-[Rr].*\s+(\/|\/home|\/etc|\/usr|\/bin|\/lib)(\s|$)"),
     "recursive permission change on critical path"),
    # Pipe to shell (curl|bash, wget|sh, etc.)
    (re.compile(r"\b(curl|wget)\b.*\|\s*(bash|sh|zsh)\b"),
     "pipe remote content to shell"),
    # Redirect writes into system directories
    (re.compile(r">\s*\/(etc|bin|usr|lib)\/"),
     "redirect write to system directory"),
    # fdisk
    (re.compile(r"\bfdisk\b"),
     "disk partition command"),
]

_PKG_MANAGERS = ("apt ", "apt-get ", "dnf ", "yum ")

# --- Git safety (expanded allowlist + forbidden patterns) --------------------

_GIT_ALLOWED = frozenset((
    "status", "diff", "log", "add", "commit", "branch", "checkout", "show",
    "switch", "restore", "fetch", "pull", "merge", "tag", "rev-parse",
    "stash", "remote",
))

_GIT_DENY_PATTERNS = [
    (re.compile(r"\b--force\b|(?<!\w)-f\b"),
     "force push/operation"),
    (re.compile(r"\breset\b.*--hard\b"),
     "hard reset"),
    (re.compile(r"\bclean\b.*-[a-z]*[fdx]"),
     "git clean (destructive)"),
    (re.compile(r"\brebase\b"),
     "rebase"),
    (re.compile(r"\bfilter-branch\b"),
     "filter-branch (history rewrite)"),
    (re.compile(r"\b--force-with-lease\b"),
     "force push with lease"),
    (re.compile(r"\bmerge\b.*--strategy[= ]ours\b"),
     "merge strategy=ours (discards changes)"),
]


# --- Helpers -----------------------------------------------------------------


def redact_secrets(text: str) -> str:
    """Replace secret values with <REDACTED>, preserving key names."""
    text = _SECRET_RE.sub(r"\1\2<REDACTED>", text)
    text = _TOKEN_PREFIX_RE.sub("<REDACTED>", text)
    return text


def append_audit(audit_path: Path, record: dict) -> None:
    """Append a single JSON-lines record to the audit log."""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def run_subprocess(command: list[str], cwd: Path, timeout: int) -> dict:
    """Run a subprocess, capture output, truncate, and redact secrets."""
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
        }
    except FileNotFoundError as exc:
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": str(exc),
        }

    stdout = redact_secrets(proc.stdout[:_MAX_OUTPUT])
    stderr = redact_secrets(proc.stderr[:_MAX_OUTPUT])
    return {"exit_code": proc.returncode, "stdout": stdout, "stderr": stderr}


# --- Safety enforcement -----------------------------------------------------


def _is_confirmed() -> bool:
    """Check if the destructive-action override token is set."""
    return os.environ.get("NOVACORE_CONFIRM") == _CONFIRM_TOKEN


def enforce_shell_safety(cmd: str) -> None:
    """Raise ValueError if cmd matches deny patterns.

    Override: set NOVACORE_CONFIRM=ALLOW_DESTRUCTIVE in environment.
    """
    for pattern, reason in _SHELL_DENY_PATTERNS:
        if pattern.search(cmd):
            if _is_confirmed():
                return
            raise ValueError(
                f"BLOCKED: {reason}. "
                f"To override, set env NOVACORE_CONFIRM={_CONFIRM_TOKEN}"
            )
    lower = cmd.lower()
    for mgr in _PKG_MANAGERS:
        if mgr in lower:
            if _is_confirmed():
                return
            raise ValueError(
                f"BLOCKED: package manager ({mgr.strip()}) requires approval. "
                f"To override, set env NOVACORE_CONFIRM={_CONFIRM_TOKEN}"
            )


def enforce_git_safety(subcommand: str, args: list[str]) -> None:
    """Raise ValueError if git subcommand or args are forbidden.

    Override: set NOVACORE_CONFIRM=ALLOW_DESTRUCTIVE in environment.
    """
    if subcommand not in _GIT_ALLOWED:
        raise ValueError(
            f"BLOCKED: git subcommand {subcommand!r} not in allowlist: "
            + ", ".join(sorted(_GIT_ALLOWED))
        )
    combined = " ".join([subcommand] + args)
    for pattern, reason in _GIT_DENY_PATTERNS:
        if pattern.search(combined):
            if _is_confirmed():
                return
            raise ValueError(
                f"BLOCKED: {reason}. "
                f"To override, set env NOVACORE_CONFIRM={_CONFIRM_TOKEN}"
            )


# --- Main entry point --------------------------------------------------------


def run_tool(
    tool_name: str, args: dict, registry: dict | None = None
) -> dict:
    """Execute a registered tool and return the result with audit logging."""
    if registry is None:
        registry = load_registry()

    sandbox = resolve_sandbox_root(registry)
    audit_path = resolve_audit_log(registry)
    t0 = time.time()

    try:
        if tool_name == "shell.run":
            result = _run_shell(args, sandbox)
        elif tool_name == "git.run":
            result = _run_git(args, sandbox)
        elif tool_name.startswith("files."):
            result = _run_files(tool_name, args, registry)
        else:
            raise ValueError(f"Tool {tool_name!r} is not implemented in runner")
    except ValueError as exc:
        code = 126 if str(exc).startswith("BLOCKED:") else -1
        result = {"ok": False, "exit_code": code, "stdout": "", "stderr": str(exc)}

    elapsed_ms = round((time.time() - t0) * 1000, 1)

    # Sanitize args for audit (strip content that might contain secrets)
    safe_args = {k: (redact_secrets(str(v)) if isinstance(v, str) else v)
                 for k, v in args.items()}

    audit_record = {
        "ts": t0,
        "tool": tool_name,
        "args": safe_args,
        "ok": result.get("ok", False),
        "exit_code": result.get("exit_code", -1),
        "elapsed_ms": elapsed_ms,
    }

    # For files.* tools, include a compact summary of the structured result
    if tool_name.startswith("files.") and "result" in result:
        audit_record["result_summary"] = _summarize_files_result(
            tool_name, result["result"]
        )

    append_audit(audit_path, audit_record)

    return result


# --- Tool implementations ---------------------------------------------------


def _run_files(tool_name: str, args: dict, registry: dict) -> dict:
    """Execute a files.* tool via dispatch_files_tool."""
    raw = dispatch_files_tool(tool_name, args, registry)
    return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "", "result": raw}


def _summarize_files_result(tool_name: str, result: dict) -> dict:
    """Build a compact audit summary for files.* results."""
    if tool_name == "files.read":
        return {"path": result.get("path"), "lines": result.get("lines")}
    if tool_name == "files.write":
        return {"path": result.get("path"), "bytes": result.get("bytes")}
    if tool_name == "files.list":
        return {"count": result.get("count")}
    if tool_name == "files.diff":
        return {"changed": result.get("changed")}
    return {}


def _run_shell(args: dict, sandbox: Path) -> dict:
    """Execute shell.run tool."""
    cmd = args.get("command")
    if not cmd or not isinstance(cmd, str):
        raise ValueError("shell.run requires 'command' (str)")

    timeout = int(args.get("timeout", 120))
    timeout = max(1, min(timeout, 600))

    cwd = sandbox
    if "cwd" in args and args["cwd"]:
        cwd = Path(args["cwd"]).expanduser().resolve()
        try:
            cwd.relative_to(sandbox)
        except ValueError:
            raise ValueError(
                f"cwd {cwd} is outside sandbox_root {sandbox}"
            ) from None

    enforce_shell_safety(cmd)

    result = run_subprocess(["bash", "-lc", cmd], cwd=cwd, timeout=timeout)
    result["ok"] = result["exit_code"] == 0
    return result


def _run_git(args: dict, sandbox: Path) -> dict:
    """Execute git.run tool."""
    subcommand = args.get("subcommand")
    if not subcommand or not isinstance(subcommand, str):
        raise ValueError("git.run requires 'subcommand' (str)")

    git_args = args.get("args", [])
    if not isinstance(git_args, list):
        raise ValueError("git.run 'args' must be a list of strings")

    enforce_git_safety(subcommand, git_args)

    result = run_subprocess(
        ["git", subcommand] + git_args, cwd=sandbox, timeout=30
    )
    result["ok"] = result["exit_code"] == 0
    return result
