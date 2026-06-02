"""Tests for Telegram Claude Code resilience (plan 2026-06-01).

Task 2: Claude health surfaced in /status via _format_claude_health.
Task 6: Deterministic completion-summary fallback when the LLM CLI is
unhealthy (_is_llm_error_response / _fallback_completion_summary).

The full telegram_bot module is loaded via importlib (same shim pattern the
module itself uses) so we exercise the real helper functions.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from typing import ClassVar
from unittest import mock


def _load_telegram_bot():
    if "telegram_bot" in sys.modules:
        return sys.modules["telegram_bot"]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "telegram_bot.py")
    if root not in sys.path:
        sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location("telegram_bot", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["telegram_bot"] = mod
    spec.loader.exec_module(mod)
    return mod


tb = _load_telegram_bot()


class _Health:
    def __init__(self, ok: bool, reason: str):
        self.ok = ok
        self.reason = reason


class TestFormatClaudeHealth(unittest.TestCase):
    def setUp(self):
        # Reset the module-level TTL cache so each test probes fresh.
        tb._claude_health_cache = None

    def test_healthy(self):
        with mock.patch(
            "utils.claude_health.check_claude_health",
            return_value=_Health(True, "ok"),
        ):
            self.assertEqual(tb._format_claude_health(), "Claude Code: healthy")

    def test_degraded_includes_reason(self):
        with mock.patch(
            "utils.claude_health.check_claude_health",
            return_value=_Health(False, "auth_failed"),
        ):
            out = tb._format_claude_health()
        self.assertIn("degraded", out)
        self.assertIn("auth_failed", out)

    def test_handle_status_includes_health_line(self):
        # Patch the health probe and ensure the line is appended to /status.
        with mock.patch(
            "utils.claude_health.check_claude_health",
            return_value=_Health(False, "usage_limited"),
        ):
            text = tb.handle_status("test-chat")
        self.assertIn("Claude Code: degraded (usage_limited)", text)

    def test_uses_reduced_timeout(self):
        # Probe timeout was reduced from 10s to 5s to avoid stalling the loop.
        with mock.patch(
            "utils.claude_health.check_claude_health",
            return_value=_Health(True, "ok"),
        ) as probe:
            tb._format_claude_health()
        probe.assert_called_once()
        self.assertEqual(probe.call_args.kwargs.get("timeout_s"), 5)

    def test_ttl_cache_avoids_second_subprocess(self):
        # Within the TTL window, repeated calls must reuse the cached result
        # and NOT spawn a second billable subprocess.
        with mock.patch(
            "utils.claude_health.check_claude_health",
            return_value=_Health(True, "ok"),
        ) as probe:
            first = tb._format_claude_health()
            second = tb._format_claude_health()
        self.assertEqual(first, second)
        probe.assert_called_once()

    def test_ttl_cache_expires(self):
        # After the TTL window elapses, the probe runs again.
        with mock.patch(
            "utils.claude_health.check_claude_health",
            return_value=_Health(True, "ok"),
        ) as probe:
            tb._format_claude_health()
            # Force the cached timestamp into the past beyond the TTL.
            ts, result = tb._claude_health_cache
            tb._claude_health_cache = (ts - tb._CLAUDE_HEALTH_TTL_S - 1, result)
            tb._format_claude_health()
        self.assertEqual(probe.call_count, 2)


class TestIsLlmErrorResponse(unittest.TestCase):
    SENTINELS: ClassVar[list[str]] = [
        "Claude auth failed for the Telegram bot. Re-authenticate Claude Code on the VPS, then retry.",
        "Claude usage limit reached for the Telegram bot. Try later.",
        "Sorry, I hit a snag processing that. Want to try again?",
        "Hmm, I didn't get a response back. Could you try rephrasing?",
        "That took too long — let me try a simpler approach. What did you need?",
        "Something went wrong on my end. Give me a moment and try again.",
        "Budget limit reached: daily cap. Will resume next period.",
    ]

    def test_all_sentinels_detected(self):
        for s in self.SENTINELS:
            self.assertTrue(tb._is_llm_error_response(s), s)

    def test_robust_to_whitespace(self):
        self.assertTrue(tb._is_llm_error_response("  Sorry, I hit a snag processing that. Want to try again?  \n"))

    def test_normal_summary_is_not_error(self):
        self.assertFalse(tb._is_llm_error_response("Summary: finished the analysis and wrote output."))
        self.assertFalse(tb._is_llm_error_response("All tests passed and the branch is ready."))

    def test_empty_is_not_error(self):
        self.assertFalse(tb._is_llm_error_response(""))


class TestFallbackCompletionSummary(unittest.TestCase):
    def test_extracts_summary_line(self):
        content = "Task log\nSummary: built the widget and deployed it\nDetails follow"
        self.assertEqual(
            tb._fallback_completion_summary(content),
            "Summary: built the widget and deployed it",
        )

    def test_case_insensitive_summary(self):
        content = "header\nsummary - lowercased result line\nfooter"
        self.assertEqual(
            tb._fallback_completion_summary(content),
            "summary - lowercased result line",
        )

    def test_default_when_no_summary_line(self):
        self.assertEqual(
            tb._fallback_completion_summary("just some output\nwith no summary keyword"),
            "Task completed; see output for details.",
        )

    def test_truncates_to_1000_chars(self):
        long_line = "Summary: " + ("x" * 5000)
        out = tb._fallback_completion_summary(long_line)
        self.assertEqual(len(out), 1000)

    def test_empty_input_gives_default(self):
        self.assertEqual(
            tb._fallback_completion_summary(""),
            "Task completed; see output for details.",
        )


if __name__ == "__main__":
    unittest.main()
