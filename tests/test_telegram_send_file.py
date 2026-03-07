"""Tests for tools/adapters/telegram_send_file.py"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from tools.adapters.telegram_send_file import telegram_send_file


@pytest.fixture
def tmp_sandbox(tmp_path):
    """Create a temporary sandbox with a test file."""
    output_dir = tmp_path / "OUTPUT"
    output_dir.mkdir()
    test_file = output_dir / "test.pdf"
    test_file.write_bytes(b"%PDF-1.4 fake pdf content")
    return tmp_path


class TestTelegramSendFile:
    """Tests for the telegram_send_file adapter function."""

    def test_empty_path_rejected(self, tmp_sandbox):
        """Reject empty path."""
        result = telegram_send_file(path="", _sandbox=tmp_sandbox)
        assert result["ok"] is False
        assert "path is required" in result["error"]

    def test_none_path_rejected(self, tmp_sandbox):
        """Reject None path."""
        result = telegram_send_file(path=None, _sandbox=tmp_sandbox)
        assert result["ok"] is False

    def test_file_not_found(self, tmp_sandbox):
        """Reject nonexistent file."""
        result = telegram_send_file(
            path="OUTPUT/nonexistent.pdf",
            _sandbox=tmp_sandbox,
        )
        assert result["ok"] is False
        assert "File not found" in result["error"]

    def test_sandbox_escape_rejected(self, tmp_sandbox):
        """Reject paths that escape the sandbox."""
        result = telegram_send_file(
            path="/etc/passwd",
            _sandbox=tmp_sandbox,
        )
        assert result["ok"] is False
        assert "escapes sandbox" in result["error"]

    def test_missing_bot_token(self, tmp_sandbox):
        """Report missing TELEGRAM_BOT_TOKEN."""
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "ALLOWED_CHAT_ID": "123"}):
            result = telegram_send_file(
                path="OUTPUT/test.pdf",
                _sandbox=tmp_sandbox,
            )
        assert result["ok"] is False
        assert "TELEGRAM_BOT_TOKEN" in result["error"]

    def test_missing_chat_id(self, tmp_sandbox):
        """Report missing ALLOWED_CHAT_ID."""
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "fake:token", "ALLOWED_CHAT_ID": ""}):
            result = telegram_send_file(
                path="OUTPUT/test.pdf",
                _sandbox=tmp_sandbox,
            )
        assert result["ok"] is False
        assert "ALLOWED_CHAT_ID" in result["error"]

    def test_successful_send(self, tmp_sandbox):
        """Successful file send with mocked Telegram API."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "result": {"message_id": 42},
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "ALLOWED_CHAT_ID": "123456",
        }):
            with patch("tools.adapters.telegram_send_file.httpx.Client", return_value=mock_client):
                result = telegram_send_file(
                    path="OUTPUT/test.pdf",
                    caption="Test caption",
                    _sandbox=tmp_sandbox,
                )

        assert result["ok"] is True
        assert result["file_sent"] is True
        assert result["telegram_message_id"] == "42"

    def test_telegram_api_error(self, tmp_sandbox):
        """Handle Telegram API error gracefully."""
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden: bot was blocked"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            message="403",
            request=MagicMock(),
            response=mock_response,
        )

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake:token",
            "ALLOWED_CHAT_ID": "123456",
        }):
            with patch("tools.adapters.telegram_send_file.httpx.Client", return_value=mock_client):
                result = telegram_send_file(
                    path="OUTPUT/test.pdf",
                    _sandbox=tmp_sandbox,
                )

        assert result["ok"] is False
        assert result["file_sent"] is False
        assert "Telegram API error" in result["error"]


class TestRunnerIntegration:
    """Test that pdf.generate and telegram.send_file are wired in runner."""

    def test_pdf_generate_registered(self):
        """pdf.generate appears in the tool registry."""
        from tools.registry import load_registry
        reg = load_registry()
        assert "pdf.generate" in reg["tools"]

    def test_telegram_send_file_registered(self):
        """telegram.send_file appears in the tool registry."""
        from tools.registry import load_registry
        reg = load_registry()
        assert "telegram.send_file" in reg["tools"]

    def test_runner_dispatches_pdf_generate(self, tmp_sandbox):
        """Runner dispatches pdf.generate to the adapter."""
        from tools.runner import run_tool
        from tools.registry import load_registry

        reg = load_registry()
        result = run_tool(
            "pdf.generate",
            {"content": "Test PDF via runner", "filename": "runner_test.pdf"},
            registry=reg,
        )
        assert result["ok"] is True
        assert result["result"]["verified"] is True
