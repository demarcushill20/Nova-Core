"""Playwright browser automation adapter for NovaCore.

Wraps the Playwright CLI (npx playwright) for headless browser operations.
Supports screenshot capture and PDF generation as sandboxed one-shot operations.

For complex multi-step interactions (click, type, form fill, snapshot),
use the Playwright MCP tools (mcp__playwright__*) available in interactive
Claude sessions. This adapter covers the stateless CLI operations that
can be safely invoked by worker agents.

Requires:
  - Node.js 18+ with npx
  - Playwright browsers installed (npx playwright install chromium)
  - Chromium binary at the configured path
  - Shared libraries available via LD_LIBRARY_PATH

Environment:
  PLAYWRIGHT_CHROMIUM_PATH  — override the Chromium executable path
  PLAYWRIGHT_LD_LIBRARY_PATH — override the shared library path
"""

import os
import re
import subprocess
import time
from pathlib import Path

# --- Constants ---------------------------------------------------------------

_DEFAULT_CHROMIUM = (
    "/home/nova/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome"
)
_DEFAULT_LD_PATH = "/home/nova/.local/usr/lib/x86_64-linux-gnu"

_CHROMIUM_PATH = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", _DEFAULT_CHROMIUM)
_LD_LIBRARY_PATH = os.environ.get("PLAYWRIGHT_LD_LIBRARY_PATH", _DEFAULT_LD_PATH)

_TIMEOUT = 30  # seconds per CLI invocation
_MAX_FILENAME_LEN = 200

# URL validation: require http(s) scheme, reject file:// and javascript:
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

# Filename sanitization: allow alphanumeric, dash, underscore, dot
_SAFE_FILENAME_RE = re.compile(r"^[\w\-. ]+$")


# --- Helpers -----------------------------------------------------------------


def _validate_url(url: str) -> None:
    """Reject unsafe or malformed URLs."""
    if not url or not isinstance(url, str):
        raise ValueError("URL is required and must be a non-empty string")
    if not _URL_RE.match(url):
        raise ValueError(
            f"Invalid URL: must start with http:// or https://. Got: {url!r}"
        )
    if len(url) > 2048:
        raise ValueError("URL exceeds 2048 character limit")


def _validate_output_path(filename: str, output_dir: Path,
                          extension: str) -> Path:
    """Validate and resolve the output file path within sandbox."""
    if not filename or not isinstance(filename, str):
        raise ValueError("filename is required")

    # Strip path separators to prevent traversal
    basename = Path(filename).name
    if not basename:
        raise ValueError("filename cannot be empty after sanitization")

    if len(basename) > _MAX_FILENAME_LEN:
        raise ValueError(f"filename exceeds {_MAX_FILENAME_LEN} chars")

    # Ensure correct extension
    if not basename.lower().endswith(extension):
        basename += extension

    # Validate filename characters
    stem = Path(basename).stem
    if not _SAFE_FILENAME_RE.match(stem):
        raise ValueError(
            f"filename contains unsafe characters: {stem!r}. "
            "Use alphanumeric, dash, underscore, dot, or space."
        )

    output_path = output_dir / basename
    return output_path


def _run_playwright_cli(args: list[str], timeout: int = _TIMEOUT) -> dict:
    """Execute a Playwright CLI command with proper environment."""
    env = os.environ.copy()
    env["PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"] = _CHROMIUM_PATH
    env["LD_LIBRARY_PATH"] = _LD_LIBRARY_PATH

    cmd = ["npx", "playwright"] + args

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout[:10_000],
            "stderr": proc.stderr[:10_000],
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Playwright CLI timed out after {timeout}s",
        }
    except FileNotFoundError:
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": "npx not found — Node.js is required",
        }


# --- Tool implementations ---------------------------------------------------


def browser_screenshot(url: str, filename: str = "",
                       full_page: bool = False,
                       wait_timeout: int = 0,
                       _sandbox: Path | None = None) -> dict:
    """Take a screenshot of a URL using Playwright CLI.

    Args:
        url: The URL to screenshot (must be http:// or https://)
        filename: Output filename (written to OUTPUT/). Auto-generated if empty.
        full_page: Capture full scrollable page, not just viewport.
        wait_timeout: Wait N milliseconds before capturing (max 10000).
        _sandbox: Sandbox root path (injected by runner, never from args).

    Returns:
        Structured result with ok, path, size_bytes, url.
    """
    _validate_url(url)

    sandbox = _sandbox or Path("/home/nova/nova-core")
    output_dir = sandbox / "OUTPUT"
    output_dir.mkdir(parents=True, exist_ok=True)

    if not filename:
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{ts}.png"

    output_path = _validate_output_path(filename, output_dir, ".png")

    # Build CLI args
    cli_args = ["screenshot", "--browser", "chromium"]
    if full_page:
        cli_args.append("--full-page")
    if wait_timeout:
        wait_timeout = max(0, min(wait_timeout, 10_000))
        cli_args.extend(["--wait-for-timeout", str(wait_timeout)])
    cli_args.extend([url, str(output_path)])

    result = _run_playwright_cli(cli_args)

    if result["exit_code"] != 0:
        return {
            "ok": False,
            "exit_code": result["exit_code"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "message": f"Screenshot failed: {result['stderr'][:200]}",
        }

    if not output_path.exists():
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": result["stdout"],
            "stderr": "Screenshot file not created",
            "message": "Playwright reported success but file is missing",
        }

    size = output_path.stat().st_size
    rel_path = str(output_path.relative_to(sandbox))

    return {
        "ok": True,
        "exit_code": 0,
        "stdout": result["stdout"],
        "stderr": "",
        "path": rel_path,
        "size_bytes": size,
        "url": url,
        "full_page": full_page,
        "message": f"Screenshot saved: {rel_path} ({size} bytes)",
    }


def browser_pdf(url: str, filename: str = "",
                paper_format: str = "A4",
                wait_timeout: int = 0,
                _sandbox: Path | None = None) -> dict:
    """Generate a PDF of a URL using Playwright CLI.

    Args:
        url: The URL to render (must be http:// or https://)
        filename: Output filename (written to OUTPUT/). Auto-generated if empty.
        paper_format: Paper format (A4, Letter, Legal, etc.). Default: A4.
        wait_timeout: Wait N milliseconds before capture (max 10000).
        _sandbox: Sandbox root path (injected by runner, never from args).

    Returns:
        Structured result with ok, path, size_bytes, url.
    """
    _validate_url(url)

    sandbox = _sandbox or Path("/home/nova/nova-core")
    output_dir = sandbox / "OUTPUT"
    output_dir.mkdir(parents=True, exist_ok=True)

    if not filename:
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"page_{ts}.pdf"

    output_path = _validate_output_path(filename, output_dir, ".pdf")

    # Validate paper format
    valid_formats = {
        "letter", "legal", "tabloid", "ledger",
        "a0", "a1", "a2", "a3", "a4", "a5", "a6",
    }
    fmt = paper_format.strip()
    if fmt.lower() not in valid_formats:
        raise ValueError(
            f"Invalid paper format: {fmt!r}. "
            f"Must be one of: {', '.join(sorted(valid_formats))}"
        )

    # Build CLI args
    cli_args = ["pdf", "--browser", "chromium",
                "--paper-format", fmt]
    if wait_timeout:
        wait_timeout = max(0, min(wait_timeout, 10_000))
        cli_args.extend(["--wait-for-timeout", str(wait_timeout)])
    cli_args.extend([url, str(output_path)])

    result = _run_playwright_cli(cli_args)

    if result["exit_code"] != 0:
        return {
            "ok": False,
            "exit_code": result["exit_code"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "message": f"PDF generation failed: {result['stderr'][:200]}",
        }

    if not output_path.exists():
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": result["stdout"],
            "stderr": "PDF file not created",
            "message": "Playwright reported success but file is missing",
        }

    size = output_path.stat().st_size
    rel_path = str(output_path.relative_to(sandbox))

    return {
        "ok": True,
        "exit_code": 0,
        "stdout": result["stdout"],
        "stderr": "",
        "path": rel_path,
        "size_bytes": size,
        "url": url,
        "paper_format": fmt,
        "message": f"PDF saved: {rel_path} ({size} bytes)",
    }
