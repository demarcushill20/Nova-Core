"""Tests for novatrade.pipeline.progress — callback protocols and implementations."""

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from novatrade.pipeline.progress import (
    LoggingProgressCallback,
    ProgressCallback,
    TelegramProgressCallback,
    notify_campaign_progress,
    notify_experiment_complete,
    notify_experiment_start,
    notify_stage_complete,
    notify_stage_start,
)

# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProgressCallbackProtocol:
    """Verify that the ProgressCallback protocol is a runtime-checkable interface."""

    def test_logging_callback_satisfies_protocol(self) -> None:
        cb = LoggingProgressCallback()
        assert isinstance(cb, ProgressCallback)

    def test_telegram_callback_satisfies_protocol(self) -> None:
        cb = TelegramProgressCallback(send_fn=lambda _: None)
        assert isinstance(cb, ProgressCallback)

    def test_custom_class_satisfies_protocol(self) -> None:
        """A class with the right methods satisfies the protocol."""

        class Custom:
            def on_experiment_start(self, experiment_index: int, total: int) -> None:
                pass

            def on_experiment_complete(self, experiment_index: int, total: int, status: str, score: float) -> None:
                pass

            def on_stage_start(self, stage_name: str) -> None:
                pass

            def on_stage_complete(self, stage_name: str, result: dict[str, Any]) -> None:
                pass

            def on_campaign_progress(
                self, completed: int, total: int, best_score: float, gate_pass_rate: float
            ) -> None:
                pass

        assert isinstance(Custom(), ProgressCallback)

    def test_incomplete_class_fails_protocol(self) -> None:
        """A class missing required methods does not satisfy the protocol."""

        class Incomplete:
            def on_experiment_start(self, experiment_index: int, total: int) -> None:
                pass

        assert not isinstance(Incomplete(), ProgressCallback)


# ---------------------------------------------------------------------------
# LoggingProgressCallback
# ---------------------------------------------------------------------------


class TestLoggingProgressCallback:
    """Test the logging-based progress callback."""

    def test_on_experiment_start(self, caplog: pytest.LogCaptureFixture) -> None:
        cb = LoggingProgressCallback()
        with caplog.at_level(logging.INFO, logger="novatrade.pipeline.progress"):
            cb.on_experiment_start(0, 100)
        assert "Experiment 1/100 starting" in caplog.text

    def test_on_experiment_complete(self, caplog: pytest.LogCaptureFixture) -> None:
        cb = LoggingProgressCallback()
        with caplog.at_level(logging.INFO, logger="novatrade.pipeline.progress"):
            cb.on_experiment_complete(4, 100, "ranked", 1.85)
        assert "Experiment 5/100 complete" in caplog.text
        assert "ranked" in caplog.text
        assert "1.8500" in caplog.text

    def test_on_stage_start(self, caplog: pytest.LogCaptureFixture) -> None:
        cb = LoggingProgressCallback()
        with caplog.at_level(logging.INFO, logger="novatrade.pipeline.progress"):
            cb.on_stage_start("load_data")
        assert "load_data" in caplog.text

    def test_on_stage_complete(self, caplog: pytest.LogCaptureFixture) -> None:
        cb = LoggingProgressCallback()
        with caplog.at_level(logging.INFO, logger="novatrade.pipeline.progress"):
            cb.on_stage_complete("load_data", {"status": "ok", "duration_ms": 123})
        assert "load_data" in caplog.text
        assert "ok" in caplog.text
        assert "123" in caplog.text

    def test_on_campaign_progress(self, caplog: pytest.LogCaptureFixture) -> None:
        cb = LoggingProgressCallback()
        with caplog.at_level(logging.INFO, logger="novatrade.pipeline.progress"):
            cb.on_campaign_progress(50, 200, 1.85, 0.35)
        assert "50/200" in caplog.text
        assert "25.0%" in caplog.text
        assert "1.8500" in caplog.text

    def test_custom_logger(self) -> None:
        """Can pass a custom logger."""
        custom_log = logging.getLogger("test.custom")
        cb = LoggingProgressCallback(logger=custom_log)
        assert cb._log is custom_log

    def test_zero_total_no_crash(self) -> None:
        """on_campaign_progress with total=0 should not crash."""
        cb = LoggingProgressCallback()
        cb.on_campaign_progress(0, 0, 0.0, 0.0)  # should not raise


# ---------------------------------------------------------------------------
# TelegramProgressCallback
# ---------------------------------------------------------------------------


class TestTelegramProgressCallback:
    """Test the Telegram notification callback (mocked transport)."""

    def test_periodic_notification(self) -> None:
        """Sends every notify_every_n experiments."""
        sent: list[str] = []
        cb = TelegramProgressCallback(notify_every_n=10, send_fn=sent.append)
        # Simulate 25 experiments
        for i in range(25):
            cb.on_experiment_complete(i, 100, "ranked", 0.5)
        # Should have been notified at experiment 10 and 20
        periodic = [m for m in sent if "Progress:" in m]
        assert len(periodic) == 2
        assert "10/100" in periodic[0]
        assert "20/100" in periodic[1]

    def test_new_best_notification(self) -> None:
        """Sends when a new best score is found."""
        sent: list[str] = []
        cb = TelegramProgressCallback(notify_every_n=1000, send_fn=sent.append)
        # First experiment sets the baseline
        cb.on_experiment_complete(0, 100, "ranked", 1.0)
        # Second experiment with better score triggers new-best notification
        cb.on_experiment_complete(1, 100, "ranked", 1.5)
        new_best_msgs = [m for m in sent if "New best" in m]
        assert len(new_best_msgs) == 1
        assert "1.5000" in new_best_msgs[0]

    def test_no_notification_on_worse_score(self) -> None:
        """No new-best notification for worse scores."""
        sent: list[str] = []
        cb = TelegramProgressCallback(notify_every_n=1000, send_fn=sent.append)
        cb.on_experiment_complete(0, 100, "ranked", 2.0)
        cb.on_experiment_complete(1, 100, "ranked", 1.0)
        new_best_msgs = [m for m in sent if "New best" in m]
        assert len(new_best_msgs) == 0

    def test_campaign_complete_notification(self) -> None:
        """Sends when campaign completes."""
        sent: list[str] = []
        cb = TelegramProgressCallback(send_fn=sent.append)
        cb.on_campaign_progress(100, 100, 1.85, 0.35)
        assert len(sent) == 1
        assert "Campaign complete" in sent[0]
        assert "1.8500" in sent[0]
        assert "35.0%" in sent[0]

    def test_no_notification_mid_campaign(self) -> None:
        """No campaign-complete notification when still in progress."""
        sent: list[str] = []
        cb = TelegramProgressCallback(send_fn=sent.append)
        cb.on_campaign_progress(50, 100, 1.0, 0.2)
        assert len(sent) == 0

    def test_stage_methods_are_no_op(self) -> None:
        """Stage start/complete don't send Telegram messages."""
        sent: list[str] = []
        cb = TelegramProgressCallback(send_fn=sent.append)
        cb.on_stage_start("test")
        cb.on_stage_complete("test", {"status": "ok"})
        cb.on_experiment_start(0, 100)
        assert len(sent) == 0

    def test_notify_every_n_minimum_one(self) -> None:
        """notify_every_n cannot be less than 1."""
        cb = TelegramProgressCallback(notify_every_n=0)
        assert cb.notify_every_n == 1

    def test_default_send_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default send is a no-op when Telegram env vars are not set."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("ALLOWED_CHAT_ID", raising=False)
        # Should not raise
        TelegramProgressCallback._default_send("test message")

    def test_default_send_with_env_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default send handles import error gracefully when env is set."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("ALLOWED_CHAT_ID", "12345")
        # The telegram_notifier module may not be importable in test context,
        # but it should not raise — just log a warning
        TelegramProgressCallback._default_send("test message")


# ---------------------------------------------------------------------------
# Fan-out helpers
# ---------------------------------------------------------------------------


class TestFanOutHelpers:
    """Test the notify_* fan-out functions."""

    def test_notify_experiment_start_fans_out(self) -> None:
        cb1 = MagicMock()
        cb2 = MagicMock()
        notify_experiment_start([cb1, cb2], 5, 100)
        cb1.on_experiment_start.assert_called_once_with(5, 100)
        cb2.on_experiment_start.assert_called_once_with(5, 100)

    def test_notify_experiment_complete_fans_out(self) -> None:
        cb = MagicMock()
        notify_experiment_complete([cb], 3, 100, "ranked", 1.5)
        cb.on_experiment_complete.assert_called_once_with(3, 100, "ranked", 1.5)

    def test_notify_stage_start_fans_out(self) -> None:
        cb = MagicMock()
        notify_stage_start([cb], "load_data")
        cb.on_stage_start.assert_called_once_with("load_data")

    def test_notify_stage_complete_fans_out(self) -> None:
        cb = MagicMock()
        notify_stage_complete([cb], "load_data", {"status": "ok"})
        cb.on_stage_complete.assert_called_once_with("load_data", {"status": "ok"})

    def test_notify_campaign_progress_fans_out(self) -> None:
        cb = MagicMock()
        notify_campaign_progress([cb], 50, 100, 1.85, 0.35)
        cb.on_campaign_progress.assert_called_once_with(50, 100, 1.85, 0.35)

    def test_error_in_one_callback_does_not_block_others(self) -> None:
        """If one callback raises, others still get called."""
        cb_bad = MagicMock()
        cb_bad.on_experiment_start.side_effect = RuntimeError("boom")
        cb_good = MagicMock()
        notify_experiment_start([cb_bad, cb_good], 0, 100)
        cb_good.on_experiment_start.assert_called_once_with(0, 100)

    def test_empty_callback_list_no_error(self) -> None:
        """Empty callback list is a valid no-op."""
        notify_experiment_start([], 0, 100)
        notify_experiment_complete([], 0, 100, "ranked", 1.0)
        notify_stage_start([], "test")
        notify_stage_complete([], "test", {})
        notify_campaign_progress([], 0, 100, 0.0, 0.0)

    def test_fan_out_all_methods_swallow_errors(self) -> None:
        """All fan-out helpers swallow errors from callbacks."""
        cb = MagicMock()
        cb.on_experiment_start.side_effect = ValueError("nope")
        cb.on_experiment_complete.side_effect = ValueError("nope")
        cb.on_stage_start.side_effect = ValueError("nope")
        cb.on_stage_complete.side_effect = ValueError("nope")
        cb.on_campaign_progress.side_effect = ValueError("nope")

        # None of these should raise
        notify_experiment_start([cb], 0, 1)
        notify_experiment_complete([cb], 0, 1, "ok", 0.0)
        notify_stage_start([cb], "x")
        notify_stage_complete([cb], "x", {})
        notify_campaign_progress([cb], 0, 1, 0.0, 0.0)
