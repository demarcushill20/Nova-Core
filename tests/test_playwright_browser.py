"""Tests for the Playwright browser automation adapter.

Tests cover:
- URL validation (scheme enforcement, length limits, edge cases)
- Filename / output path validation (traversal, sanitization, extension)
- Paper format validation
- CLI argument construction (verified via subprocess mock)
- Screenshot and PDF success / failure paths
- Runner dispatch integration
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.adapters.playwright_browser import (
    _validate_url,
    _validate_output_path,
    _find_chromium,
    _find_ld_library_path,
    ensure_chromium,
    browser_screenshot,
    browser_pdf,
)


# ── URL Validation ──────────────────────────────────────────────────────────


class TestValidateUrl:
    def test_valid_http(self):
        _validate_url("http://example.com")

    def test_valid_https(self):
        _validate_url("https://example.com/page?q=1&b=2")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="non-empty string"):
            _validate_url("")

    def test_rejects_none(self):
        with pytest.raises(ValueError, match="non-empty string"):
            _validate_url(None)

    def test_rejects_file_scheme(self):
        with pytest.raises(ValueError, match="http:// or https://"):
            _validate_url("file:///etc/passwd")

    def test_rejects_javascript_scheme(self):
        with pytest.raises(ValueError, match="http:// or https://"):
            _validate_url("javascript:alert(1)")

    def test_rejects_ftp(self):
        with pytest.raises(ValueError, match="http:// or https://"):
            _validate_url("ftp://files.example.com/data")

    def test_rejects_no_scheme(self):
        with pytest.raises(ValueError, match="http:// or https://"):
            _validate_url("example.com")

    def test_rejects_long_url(self):
        url = "https://example.com/" + "a" * 2040
        with pytest.raises(ValueError, match="2048 character"):
            _validate_url(url)

    def test_accepts_max_length_url(self):
        # 2048 chars exactly should pass
        url = "https://example.com/" + "a" * (2048 - len("https://example.com/"))
        assert len(url) == 2048
        _validate_url(url)


# ── Output Path Validation ──────────────────────────────────────────────────


class TestValidateOutputPath:
    def setup_method(self):
        self.output_dir = Path("/tmp/test_pw_output")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def test_basic_filename(self):
        result = _validate_output_path("test.png", self.output_dir, ".png")
        assert result == self.output_dir / "test.png"

    def test_adds_extension(self):
        result = _validate_output_path("capture", self.output_dir, ".png")
        assert result.name == "capture.png"

    def test_preserves_correct_extension(self):
        result = _validate_output_path("my_shot.png", self.output_dir, ".png")
        assert result.name == "my_shot.png"

    def test_strips_path_traversal(self):
        result = _validate_output_path("../../etc/passwd.png",
                                       self.output_dir, ".png")
        assert result.parent == self.output_dir
        assert result.name == "passwd.png"

    def test_strips_absolute_path(self):
        result = _validate_output_path("/etc/shadow.png",
                                       self.output_dir, ".png")
        assert result.parent == self.output_dir
        assert result.name == "shadow.png"

    def test_rejects_empty_filename(self):
        with pytest.raises(ValueError, match="filename"):
            _validate_output_path("", self.output_dir, ".png")

    def test_rejects_long_filename(self):
        long_name = "a" * 201 + ".png"
        with pytest.raises(ValueError, match="200 chars"):
            _validate_output_path(long_name, self.output_dir, ".png")

    def test_rejects_unsafe_characters(self):
        with pytest.raises(ValueError, match="unsafe characters"):
            _validate_output_path("file;rm -rf.png", self.output_dir, ".png")

    def test_allows_spaces_dashes_underscores(self):
        result = _validate_output_path("my file-name_v2.png",
                                       self.output_dir, ".png")
        assert result.name == "my file-name_v2.png"

    def test_pdf_extension(self):
        result = _validate_output_path("report", self.output_dir, ".pdf")
        assert result.name == "report.pdf"


# ── Browser Screenshot ──────────────────────────────────────────────────────


class TestBrowserScreenshot:
    def setup_method(self):
        self.sandbox = Path("/tmp/test_pw_sandbox")
        self.output_dir = self.sandbox / "OUTPUT"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def test_raises_on_invalid_url(self):
        with pytest.raises(ValueError, match="http:// or https://"):
            browser_screenshot(url="file:///etc/passwd", _sandbox=self.sandbox)

    def test_raises_on_empty_url(self):
        with pytest.raises(ValueError, match="non-empty"):
            browser_screenshot(url="", _sandbox=self.sandbox)

    @patch("tools.adapters.playwright_browser._run_playwright_cli")
    def test_success_flow(self, mock_cli):
        mock_cli.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}

        # Create a fake output file so the function finds it
        out_file = self.output_dir / "test_shot.png"
        out_file.write_bytes(b"\x89PNG" + b"\x00" * 100)

        result = browser_screenshot(
            url="https://example.com",
            filename="test_shot.png",
            _sandbox=self.sandbox,
        )

        assert result["ok"] is True
        assert result["path"] == "OUTPUT/test_shot.png"
        assert result["size_bytes"] == 104
        assert result["url"] == "https://example.com"

        # Verify CLI was called with correct args
        cli_args = mock_cli.call_args[0][0]
        assert "screenshot" in cli_args
        assert "--browser" in cli_args
        assert "chromium" in cli_args
        assert "https://example.com" in cli_args

    @patch("tools.adapters.playwright_browser._run_playwright_cli")
    def test_full_page_flag(self, mock_cli):
        mock_cli.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
        out_file = self.output_dir / "full.png"
        out_file.write_bytes(b"\x89PNG" + b"\x00" * 50)

        browser_screenshot(
            url="https://example.com",
            filename="full.png",
            full_page=True,
            _sandbox=self.sandbox,
        )

        cli_args = mock_cli.call_args[0][0]
        assert "--full-page" in cli_args

    @patch("tools.adapters.playwright_browser._run_playwright_cli")
    def test_wait_timeout_flag(self, mock_cli):
        mock_cli.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
        out_file = self.output_dir / "wait.png"
        out_file.write_bytes(b"\x89PNG")

        browser_screenshot(
            url="https://example.com",
            filename="wait.png",
            wait_timeout=5000,
            _sandbox=self.sandbox,
        )

        cli_args = mock_cli.call_args[0][0]
        assert "--wait-for-timeout" in cli_args
        idx = cli_args.index("--wait-for-timeout")
        assert cli_args[idx + 1] == "5000"

    @patch("tools.adapters.playwright_browser._run_playwright_cli")
    def test_wait_timeout_clamped(self, mock_cli):
        mock_cli.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
        out_file = self.output_dir / "clamp.png"
        out_file.write_bytes(b"\x89PNG")

        browser_screenshot(
            url="https://example.com",
            filename="clamp.png",
            wait_timeout=99999,
            _sandbox=self.sandbox,
        )

        cli_args = mock_cli.call_args[0][0]
        idx = cli_args.index("--wait-for-timeout")
        assert cli_args[idx + 1] == "10000"

    @patch("tools.adapters.playwright_browser._run_playwright_cli")
    def test_cli_failure(self, mock_cli):
        mock_cli.return_value = {
            "exit_code": 1,
            "stdout": "",
            "stderr": "Browser launch failed",
        }

        result = browser_screenshot(
            url="https://example.com",
            filename="fail.png",
            _sandbox=self.sandbox,
        )

        assert result["ok"] is False
        assert "Browser launch failed" in result["stderr"]

    @patch("tools.adapters.playwright_browser._run_playwright_cli")
    def test_file_missing_after_success(self, mock_cli):
        mock_cli.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}
        # Don't create the output file

        result = browser_screenshot(
            url="https://example.com",
            filename="missing.png",
            _sandbox=self.sandbox,
        )

        assert result["ok"] is False
        assert "file not created" in result["stderr"]

    @patch("tools.adapters.playwright_browser._run_playwright_cli")
    def test_auto_generated_filename(self, mock_cli):
        mock_cli.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}

        # We need to create the file that will be auto-named
        ts = time.strftime("%Y%m%d_%H%M%S")
        expected = self.output_dir / f"screenshot_{ts}.png"
        expected.write_bytes(b"\x89PNG")

        result = browser_screenshot(
            url="https://example.com",
            _sandbox=self.sandbox,
        )

        assert result["ok"] is True
        assert "screenshot_" in result["path"]

    def teardown_method(self):
        import shutil
        if self.sandbox.exists():
            shutil.rmtree(self.sandbox, ignore_errors=True)


# ── Browser PDF ─────────────────────────────────────────────────────────────


class TestBrowserPdf:
    def setup_method(self):
        self.sandbox = Path("/tmp/test_pw_sandbox_pdf")
        self.output_dir = self.sandbox / "OUTPUT"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def test_raises_on_invalid_url(self):
        with pytest.raises(ValueError, match="http:// or https://"):
            browser_pdf(url="javascript:void(0)", _sandbox=self.sandbox)

    def test_raises_on_invalid_paper_format(self):
        with pytest.raises(ValueError, match="Invalid paper format"):
            browser_pdf(
                url="https://example.com",
                paper_format="Folio",
                _sandbox=self.sandbox,
            )

    def test_valid_paper_formats(self):
        for fmt in ["A4", "a4", "Letter", "letter", "Legal", "Tabloid", "A0", "A6"]:
            # Just verify no ValueError on format validation
            # (will still fail on CLI, but that's mocked in other tests)
            try:
                browser_pdf(url="https://example.com", paper_format=fmt,
                            _sandbox=self.sandbox)
            except ValueError as e:
                if "paper format" in str(e).lower():
                    pytest.fail(f"Format {fmt} should be valid but was rejected")

    @patch("tools.adapters.playwright_browser._run_playwright_cli")
    def test_success_flow(self, mock_cli):
        mock_cli.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}

        out_file = self.output_dir / "report.pdf"
        out_file.write_bytes(b"%PDF-1.4" + b"\x00" * 200)

        result = browser_pdf(
            url="https://example.com",
            filename="report.pdf",
            paper_format="A4",
            _sandbox=self.sandbox,
        )

        assert result["ok"] is True
        assert result["path"] == "OUTPUT/report.pdf"
        assert result["size_bytes"] == 208
        assert result["paper_format"] == "A4"

        cli_args = mock_cli.call_args[0][0]
        assert "pdf" in cli_args
        assert "--paper-format" in cli_args
        assert "--browser" in cli_args

    @patch("tools.adapters.playwright_browser._run_playwright_cli")
    def test_letter_format(self, mock_cli):
        mock_cli.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
        out_file = self.output_dir / "letter.pdf"
        out_file.write_bytes(b"%PDF")

        browser_pdf(
            url="https://example.com",
            filename="letter.pdf",
            paper_format="Letter",
            _sandbox=self.sandbox,
        )

        cli_args = mock_cli.call_args[0][0]
        idx = cli_args.index("--paper-format")
        assert cli_args[idx + 1] == "Letter"

    @patch("tools.adapters.playwright_browser._run_playwright_cli")
    def test_cli_failure(self, mock_cli):
        mock_cli.return_value = {
            "exit_code": 1,
            "stdout": "",
            "stderr": "Chromium not found",
        }

        result = browser_pdf(
            url="https://example.com",
            filename="fail.pdf",
            _sandbox=self.sandbox,
        )

        assert result["ok"] is False
        assert "Chromium not found" in result["stderr"]

    @patch("tools.adapters.playwright_browser._run_playwright_cli")
    def test_auto_generated_filename(self, mock_cli):
        mock_cli.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}

        ts = time.strftime("%Y%m%d_%H%M%S")
        expected = self.output_dir / f"page_{ts}.pdf"
        expected.write_bytes(b"%PDF")

        result = browser_pdf(url="https://example.com", _sandbox=self.sandbox)

        assert result["ok"] is True
        assert "page_" in result["path"]

    def teardown_method(self):
        import shutil
        if self.sandbox.exists():
            shutil.rmtree(self.sandbox, ignore_errors=True)


# ── CLI Helper ──────────────────────────────────────────────────────────────


class TestRunPlaywrightCli:
    @patch("tools.adapters.playwright_browser._find_chromium",
           return_value="/fake/chrome")
    @patch("tools.adapters.playwright_browser._find_ld_library_path",
           return_value="/fake/libs")
    @patch("tools.adapters.playwright_browser.subprocess.run")
    def test_timeout_returns_error(self, mock_run, _ld, _cr):
        from tools.adapters.playwright_browser import _run_playwright_cli
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd=["npx"], timeout=30)

        result = _run_playwright_cli(["screenshot", "http://x.com", "/tmp/x.png"])
        assert result["exit_code"] == -1
        assert "timed out" in result["stderr"]

    @patch("tools.adapters.playwright_browser._find_chromium",
           return_value="/fake/chrome")
    @patch("tools.adapters.playwright_browser._find_ld_library_path",
           return_value="/fake/libs")
    @patch("tools.adapters.playwright_browser.subprocess.run")
    def test_npx_not_found(self, mock_run, _ld, _cr):
        from tools.adapters.playwright_browser import _run_playwright_cli
        mock_run.side_effect = FileNotFoundError()

        result = _run_playwright_cli(["screenshot", "http://x.com", "/tmp/x.png"])
        assert result["exit_code"] == 127
        assert "npx not found" in result["stderr"]

    @patch("tools.adapters.playwright_browser._find_chromium",
           return_value="/detected/chrome")
    @patch("tools.adapters.playwright_browser._find_ld_library_path",
           return_value="/detected/libs")
    @patch("tools.adapters.playwright_browser.subprocess.run")
    def test_sets_chromium_env_from_autodetect(self, mock_run, _ld, _cr):
        from tools.adapters.playwright_browser import _run_playwright_cli
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )

        _run_playwright_cli(["screenshot", "http://x.com", "/tmp/x.png"])

        call_kwargs = mock_run.call_args[1]
        env = call_kwargs["env"]
        assert env["PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"] == "/detected/chrome"
        assert env["LD_LIBRARY_PATH"] == "/detected/libs"

    @patch("tools.adapters.playwright_browser._find_chromium",
           return_value="/fake/chrome")
    @patch("tools.adapters.playwright_browser._find_ld_library_path",
           return_value="/fake/libs")
    @patch("tools.adapters.playwright_browser.subprocess.run")
    def test_truncates_output(self, mock_run, _ld, _cr):
        from tools.adapters.playwright_browser import _run_playwright_cli
        mock_run.return_value = MagicMock(
            returncode=0, stdout="x" * 20_000, stderr="y" * 20_000
        )

        result = _run_playwright_cli(["screenshot", "http://x.com", "/tmp/x.png"])
        assert len(result["stdout"]) == 10_000
        assert len(result["stderr"]) == 10_000

    @patch("tools.adapters.playwright_browser._find_chromium",
           return_value="")
    @patch("tools.adapters.playwright_browser.ensure_chromium")
    def test_auto_install_on_missing_chromium(self, mock_install, _cr):
        from tools.adapters.playwright_browser import _run_playwright_cli
        mock_install.return_value = {
            "ok": False,
            "chromium_path": "",
            "installed": False,
            "message": "npx not found",
        }

        result = _run_playwright_cli(["screenshot", "http://x.com", "/tmp/x.png"])
        assert result["exit_code"] == 127
        assert "auto-install failed" in result["stderr"]
        mock_install.assert_called_once()

    @patch("tools.adapters.playwright_browser._find_chromium",
           return_value="")
    @patch("tools.adapters.playwright_browser._find_ld_library_path",
           return_value="/fake/libs")
    @patch("tools.adapters.playwright_browser.ensure_chromium")
    @patch("tools.adapters.playwright_browser.subprocess.run")
    def test_auto_install_success_then_runs(self, mock_run, mock_install,
                                            _ld, _cr):
        from tools.adapters.playwright_browser import _run_playwright_cli
        mock_install.return_value = {
            "ok": True,
            "chromium_path": "/new/chrome",
            "installed": True,
            "message": "installed",
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout="done", stderr=""
        )

        result = _run_playwright_cli(["screenshot", "http://x.com", "/tmp/x.png"])
        assert result["exit_code"] == 0
        env = mock_run.call_args[1]["env"]
        assert env["PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"] == "/new/chrome"


# ── Auto-detection ──────────────────────────────────────────────────────────


class TestAutoDetection:
    def test_find_chromium_detects_installed(self):
        # On this system, chromium should be installed
        path = _find_chromium()
        assert path, "Chromium should be detected on this system"
        assert Path(path).is_file()
        assert "chrome" in Path(path).name

    def test_find_chromium_env_override(self):
        with patch.dict(os.environ,
                        {"PLAYWRIGHT_CHROMIUM_PATH": "/tmp/fake_chrome"}):
            # Won't return this because file doesn't exist
            result = _find_chromium()
            # Falls through to glob detection
            assert result  # should still find the real one

    def test_find_ld_library_path_detects(self):
        path = _find_ld_library_path()
        assert path, "LD library path should be detected"
        assert Path(path).is_dir()

    def test_find_ld_library_path_env_override(self):
        with patch.dict(os.environ,
                        {"PLAYWRIGHT_LD_LIBRARY_PATH": "/tmp"}):
            result = _find_ld_library_path()
            assert result == "/tmp"

    def test_ensure_chromium_already_present(self):
        result = ensure_chromium()
        assert result["ok"] is True
        assert result["installed"] is False  # already there
        assert result["chromium_path"]

    @patch("tools.adapters.playwright_browser._find_chromium",
           return_value="")
    @patch("tools.adapters.playwright_browser.subprocess.run")
    def test_ensure_chromium_install_fails(self, mock_run, _cr):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="network error"
        )
        result = ensure_chromium()
        assert result["ok"] is False
        assert "Install failed" in result["message"]

    @patch("tools.adapters.playwright_browser._find_chromium",
           return_value="")
    @patch("tools.adapters.playwright_browser.subprocess.run")
    def test_ensure_chromium_npx_missing(self, mock_run, _cr):
        mock_run.side_effect = FileNotFoundError()
        result = ensure_chromium()
        assert result["ok"] is False
        assert "npx not found" in result["message"]


# ── Runner Dispatch Integration ─────────────────────────────────────────────


class TestRunnerDispatch:
    def test_registry_has_browser_tools(self):
        registry_path = Path(__file__).parent.parent / "tools" / "tools_registry.json"
        with open(registry_path) as f:
            reg = json.load(f)

        assert "browser.screenshot" in reg["tools"]
        assert "browser.pdf" in reg["tools"]

        # Check required fields
        for tool_name in ["browser.screenshot", "browser.pdf"]:
            tool = reg["tools"][tool_name]
            assert "description" in tool
            assert "args_schema" in tool
            assert "returns" in tool
            assert "safety" in tool
            assert "url" in tool["args_schema"]
            assert tool["args_schema"]["url"]["required"] is True
