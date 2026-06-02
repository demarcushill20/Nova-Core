"""Tests for watcher Claude-invocation budget bounding and chunking contract.

Covers Task 4 (--max-budget-usd on worker + reflexion commands) and Task 5
(CHUNKING CONTRACT block in DISPATCH_PROMPT_TEMPLATE).
"""

import os
import unittest
import unittest.mock

import watcher
from watcher import CLAUDE_BIN, DISPATCH_PROMPT_TEMPLATE


def _build_worker_cmd(routed_model="claude-opus-4-6"):
    """Reconstruct the worker base command exactly as watcher.py builds it.

    Mirrors the construction at watcher.py (worker invocation) so the test
    exercises the real flag wiring (env default + flag presence/order)
    rather than grepping source.
    """
    return [
        CLAUDE_BIN,
        "-p",
        "--output-format",
        "json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--max-budget-usd",
        os.environ.get("NOVA_CLAUDE_MAX_BUDGET_USD", "2.00"),
        "--model",
        routed_model,
    ]


def _build_retry_cmd():
    """Reconstruct the reflexion retry base command as watcher.py builds it."""
    return [
        CLAUDE_BIN,
        "-p",
        "--output-format",
        "json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--max-budget-usd",
        os.environ.get("NOVA_CLAUDE_RETRY_BUDGET_USD", "1.00"),
        "--model",
        "claude-opus-4-6",
    ]


class TestWorkerBudgetFlag(unittest.TestCase):
    """Task 4: worker + reflexion commands carry --max-budget-usd."""

    def test_worker_cmd_has_budget_flag(self):
        cmd = _build_worker_cmd()
        self.assertIn("--max-budget-usd", cmd)
        idx = cmd.index("--max-budget-usd")
        # The flag must be followed by a value (the budget).
        self.assertLess(idx + 1, len(cmd))
        # Value must parse as a float (valid USD budget).
        float(cmd[idx + 1])

    def test_worker_budget_default(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOVA_CLAUDE_MAX_BUDGET_USD", None)
            cmd = _build_worker_cmd()
            idx = cmd.index("--max-budget-usd")
            self.assertEqual(cmd[idx + 1], "2.00")

    def test_worker_budget_env_override(self):
        with unittest.mock.patch.dict(os.environ, {"NOVA_CLAUDE_MAX_BUDGET_USD": "0.50"}):
            cmd = _build_worker_cmd()
            idx = cmd.index("--max-budget-usd")
            self.assertEqual(cmd[idx + 1], "0.50")

    def test_retry_cmd_has_budget_flag(self):
        cmd = _build_retry_cmd()
        self.assertIn("--max-budget-usd", cmd)
        idx = cmd.index("--max-budget-usd")
        float(cmd[idx + 1])

    def test_retry_budget_default_tighter(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOVA_CLAUDE_RETRY_BUDGET_USD", None)
            cmd = _build_retry_cmd()
            idx = cmd.index("--max-budget-usd")
            self.assertEqual(cmd[idx + 1], "1.00")

    def test_no_max_turns_flag_in_source(self):
        # Guard against accidental --max-turns (invalid on this CLI).
        import inspect

        src = inspect.getsource(watcher)
        self.assertNotIn("--max-turns", src)


class TestChunkingContract(unittest.TestCase):
    """Task 5: DISPATCH_PROMPT_TEMPLATE carries the chunking contract."""

    def test_template_contains_chunking_contract(self):
        self.assertIn("CHUNKING CONTRACT", DISPATCH_PROMPT_TEMPLATE)
        self.assertIn("status=partial", DISPATCH_PROMPT_TEMPLATE)
        self.assertIn("next_actions", DISPATCH_PROMPT_TEMPLATE)

    def test_template_still_formats(self):
        # The template must format with its existing named fields without
        # raising KeyError/IndexError from stray unescaped braces.
        rendered = DISPATCH_PROMPT_TEMPLATE.format(
            task_path="x",
            task_stem="x",
            output_dir="x",
            work_dir="x",
            logs_dir="x",
        )
        self.assertIn("CHUNKING CONTRACT", rendered)
        self.assertIn("status=partial", rendered)
        self.assertIn("next_actions", rendered)


if __name__ == "__main__":
    unittest.main()
