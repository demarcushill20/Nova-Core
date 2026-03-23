"""Tests for LLM-driven proactive heartbeat agent.

Covers:
- Active hours gating
- System state gathering
- JSON action parsing
- Proactive task injection rate limiting
- Agent log writing
- Checklist file requirement
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
_here = str(Path(__file__).resolve().parent.parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

import heartbeat  # noqa: E402


class TestActiveHoursGating(unittest.TestCase):
    """LLM heartbeat should only run during active hours."""

    @patch("heartbeat.subprocess.run")
    @patch("heartbeat.CHECKLIST_FILE", Path("/tmp/nonexistent_checklist.md"))
    def test_skips_outside_active_hours(self, mock_run):
        """Agent should not run outside active hours."""
        with patch("heartbeat.ACTIVE_HOURS_START", 8), patch("heartbeat.ACTIVE_HOURS_END", 23):
            # Mock current hour to 3 AM UTC (outside active hours)
            mock_dt = MagicMock()
            mock_dt.hour = 3
            with patch("heartbeat.datetime") as mock_datetime:
                mock_datetime.now.return_value = mock_dt
                mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
                heartbeat._run_heartbeat_agent([])
        mock_run.assert_not_called()

    @patch("heartbeat.subprocess.run")
    def test_runs_during_active_hours(self, mock_run):
        """Agent should attempt to run during active hours."""
        tmpdir = tempfile.mkdtemp()
        checklist = Path(tmpdir) / "checklist.md"
        checklist.write_text("# Test Checklist\n- Check stuff")
        state_dir = Path(tmpdir) / "STATE"
        state_dir.mkdir(parents=True, exist_ok=True)
        mock_run.return_value = MagicMock(stdout="HEARTBEAT_OK", returncode=0)

        with (
            patch("heartbeat.CHECKLIST_FILE", checklist),
            patch("heartbeat.ACTIVE_HOURS_START", 0),
            patch("heartbeat.ACTIVE_HOURS_END", 24),
            patch("heartbeat.HEARTBEAT_AGENT_LOG", Path(tmpdir) / "agent.log"),
            patch("heartbeat.STATE_DIR", state_dir),
            patch("heartbeat.HEARTBEAT_AGENT_COOLDOWN_FILE", state_dir / "last_heartbeat_agent.json"),
        ):
            heartbeat._run_heartbeat_agent([])

        mock_run.assert_called_once()
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)

    @patch("heartbeat.subprocess.run")
    def test_skips_during_cooldown(self, mock_run):
        """Agent should skip if it ran recently (within cooldown period)."""
        tmpdir = tempfile.mkdtemp()
        checklist = Path(tmpdir) / "checklist.md"
        checklist.write_text("# Test Checklist\n- Check stuff")
        state_dir = Path(tmpdir) / "STATE"
        state_dir.mkdir(parents=True, exist_ok=True)

        # Write a recent cooldown timestamp (just now)
        cooldown_file = state_dir / "last_heartbeat_agent.json"
        cooldown_file.write_text(
            json.dumps(
                {
                    "last_run_utc": datetime.now().astimezone().isoformat(),
                    "success": True,
                }
            )
        )

        with (
            patch("heartbeat.CHECKLIST_FILE", checklist),
            patch("heartbeat.ACTIVE_HOURS_START", 0),
            patch("heartbeat.ACTIVE_HOURS_END", 24),
            patch("heartbeat.HEARTBEAT_AGENT_COOLDOWN_FILE", cooldown_file),
        ):
            heartbeat._run_heartbeat_agent([])

        mock_run.assert_not_called()
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


class TestChecklistRequirement(unittest.TestCase):
    """Agent should skip if no checklist file exists."""

    @patch("heartbeat.subprocess.run")
    @patch("heartbeat.ACTIVE_HOURS_START", 0)
    @patch("heartbeat.ACTIVE_HOURS_END", 24)
    def test_skips_without_checklist(self, mock_run):
        with patch("heartbeat.CHECKLIST_FILE", Path("/tmp/no_such_file.md")):
            heartbeat._run_heartbeat_agent([])
        mock_run.assert_not_called()


class TestExtendedStateGathering(unittest.TestCase):
    """Test system state collection for the agent."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_tasks = heartbeat.TASKS_DIR
        self._orig_output = heartbeat.OUTPUT_DIR
        self._orig_state = heartbeat.STATE_DIR
        self._orig_logs = heartbeat.LOGS_DIR
        self._orig_agent_log = heartbeat.HEARTBEAT_AGENT_LOG
        heartbeat.TASKS_DIR = Path(self._tmpdir) / "TASKS"
        heartbeat.OUTPUT_DIR = Path(self._tmpdir) / "OUTPUT"
        heartbeat.STATE_DIR = Path(self._tmpdir) / "STATE"
        heartbeat.LOGS_DIR = Path(self._tmpdir) / "LOGS"
        heartbeat.HEARTBEAT_AGENT_LOG = Path(self._tmpdir) / "LOGS" / "heartbeat_agent.log"
        for d in (heartbeat.TASKS_DIR, heartbeat.OUTPUT_DIR, heartbeat.STATE_DIR, heartbeat.LOGS_DIR):
            d.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        heartbeat.TASKS_DIR = self._orig_tasks
        heartbeat.OUTPUT_DIR = self._orig_output
        heartbeat.STATE_DIR = self._orig_state
        heartbeat.LOGS_DIR = self._orig_logs
        heartbeat.HEARTBEAT_AGENT_LOG = self._orig_agent_log
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_includes_check_results(self):
        checks = [{"name": "disk", "ok": True, "detail": "25% used"}]
        state = heartbeat._gather_extended_state(checks)
        self.assertIn("Other Health Checks", state)
        self.assertIn("[PASS] disk", state)

    def test_includes_failed_checks(self):
        checks = [{"name": "service:watcher", "ok": False, "detail": "NOT ACTIVE"}]
        state = heartbeat._gather_extended_state(checks)
        # Service failures show in SERVICE STATUS section with ✗ FAILED prefix
        self.assertIn("FAILED", state)
        self.assertIn("service:watcher", state)
        self.assertIn("NOT ACTIVE", state)

    def test_includes_pending_tasks(self):
        (heartbeat.TASKS_DIR / "0200_test_task.md").write_text("test")
        state = heartbeat._gather_extended_state([])
        self.assertIn("Pending Tasks", state)
        self.assertIn("0200_test_task", state)

    def test_excludes_lifecycle_tasks(self):
        (heartbeat.TASKS_DIR / "0200_done.md.done").write_text("done")
        (heartbeat.TASKS_DIR / "0201_failed.md.failed").write_text("failed")
        state = heartbeat._gather_extended_state([])
        self.assertNotIn("Pending Tasks", state)

    def test_includes_goals(self):
        goals = [{"id": 1, "text": "Ship Phase 2", "status": "active"}]
        (heartbeat.STATE_DIR / "goals.json").write_text(json.dumps(goals))
        state = heartbeat._gather_extended_state([])
        self.assertIn("Active Goals", state)
        self.assertIn("Ship Phase 2", state)

    def test_includes_last_agent_action(self):
        heartbeat.HEARTBEAT_AGENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        heartbeat.HEARTBEAT_AGENT_LOG.write_text("[2026-03-10] HEARTBEAT_OK\n")
        state = heartbeat._gather_extended_state([])
        self.assertIn("Last Agent Action", state)
        self.assertIn("HEARTBEAT_OK", state)


class TestJsonActionParsing(unittest.TestCase):
    """Test extraction of structured actions from agent response."""

    def test_extracts_json_array(self):
        text = 'Here are my recommendations:\n[{"type": "notify", "message": "Check disk"}]'
        actions = heartbeat._extract_json_actions(text)
        self.assertIsNotNone(actions)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["type"], "notify")

    def test_extracts_from_code_block(self):
        text = '```json\n[{"type": "task", "title": "cleanup", "body": "Remove old files"}]\n```'
        actions = heartbeat._extract_json_actions(text)
        self.assertIsNotNone(actions)
        self.assertEqual(actions[0]["type"], "task")

    def test_returns_none_for_plain_text(self):
        self.assertIsNone(heartbeat._extract_json_actions("HEARTBEAT_OK"))

    def test_returns_none_for_invalid_json(self):
        self.assertIsNone(heartbeat._extract_json_actions("[not valid json"))

    def test_returns_none_for_non_dict_array(self):
        self.assertIsNone(heartbeat._extract_json_actions("[1, 2, 3]"))

    def test_multiple_actions(self):
        text = '[{"type": "notify", "message": "hi"}, {"type": "task", "title": "t", "body": "b"}]'
        actions = heartbeat._extract_json_actions(text)
        self.assertEqual(len(actions), 2)


class TestProactiveTaskInjection(unittest.TestCase):
    """Test proactive task creation with rate limiting."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_tasks = heartbeat.TASKS_DIR
        heartbeat.TASKS_DIR = Path(self._tmpdir) / "TASKS"
        heartbeat.TASKS_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        heartbeat.TASKS_DIR = self._orig_tasks
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_creates_task_file(self):
        heartbeat._inject_proactive_task("Test cleanup", "Clean up old files")
        tasks = list(heartbeat.TASKS_DIR.glob("hb_proactive_*.md"))
        self.assertEqual(len(tasks), 1)
        content = tasks[0].read_text()
        self.assertIn("Clean up old files", content)

    def test_rate_limits_at_four(self):
        """Should not create more than 4 pending proactive tasks."""
        heartbeat._inject_proactive_task("Task 1", "body 1")
        heartbeat._inject_proactive_task("Task 2", "body 2")
        heartbeat._inject_proactive_task("Task 3", "body 3")
        heartbeat._inject_proactive_task("Task 4", "body 4")
        heartbeat._inject_proactive_task("Task 5", "body 5")  # should be blocked
        tasks = list(heartbeat.TASKS_DIR.glob("hb_proactive_*.md"))
        self.assertEqual(len(tasks), 4)

    def test_completed_tasks_dont_count(self):
        """Completed (.done) proactive tasks shouldn't count toward rate limit."""
        # Create a completed one
        (heartbeat.TASKS_DIR / "hb_proactive_old_task.md.done").write_text("done")
        heartbeat._inject_proactive_task("New task", "body")
        pending = [p for p in heartbeat.TASKS_DIR.glob("hb_proactive_*.md") if not p.name.endswith(".done")]
        self.assertEqual(len(pending), 1)


class TestAgentLogging(unittest.TestCase):
    """Test heartbeat agent log writing."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig = heartbeat.HEARTBEAT_AGENT_LOG
        self._orig_logs = heartbeat.LOGS_DIR
        heartbeat.LOGS_DIR = Path(self._tmpdir)
        heartbeat.HEARTBEAT_AGENT_LOG = Path(self._tmpdir) / "heartbeat_agent.log"

    def tearDown(self):
        heartbeat.HEARTBEAT_AGENT_LOG = self._orig
        heartbeat.LOGS_DIR = self._orig_logs
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_creates_log_entry(self):
        heartbeat._log_agent("TEST_MESSAGE")
        content = heartbeat.HEARTBEAT_AGENT_LOG.read_text()
        self.assertIn("TEST_MESSAGE", content)

    def test_appends_to_log(self):
        heartbeat._log_agent("FIRST")
        heartbeat._log_agent("SECOND")
        lines = heartbeat.HEARTBEAT_AGENT_LOG.read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)


class TestHandleAgentActions(unittest.TestCase):
    """Test action dispatch from agent response."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_tasks = heartbeat.TASKS_DIR
        heartbeat.TASKS_DIR = Path(self._tmpdir) / "TASKS"
        heartbeat.TASKS_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        heartbeat.TASKS_DIR = self._orig_tasks
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @patch("heartbeat._telegram_cooldown_gate", return_value=True)
    @patch("heartbeat._ground_service_alert", return_value=True)
    @patch("heartbeat._send_telegram")
    def test_notify_action_sends_telegram(self, mock_tg, _mock_ground, _mock_cooldown):
        response = '[{"type": "notify", "message": "Disk getting full"}]'
        heartbeat._handle_agent_actions(response)
        mock_tg.assert_called_once()
        self.assertIn("Disk getting full", mock_tg.call_args[0][0])

    def test_task_action_creates_file(self):
        response = '[{"type": "task", "title": "cleanup_logs", "body": "Remove old log files"}]'
        heartbeat._handle_agent_actions(response)
        tasks = list(heartbeat.TASKS_DIR.glob("hb_proactive_*.md"))
        self.assertEqual(len(tasks), 1)

    @patch("heartbeat._telegram_cooldown_gate", return_value=True)
    @patch("heartbeat._ground_service_alert", return_value=True)
    @patch("heartbeat._send_telegram")
    def test_plain_text_sends_as_notification(self, mock_tg, _mock_ground, _mock_cooldown):
        heartbeat._handle_agent_actions("Something needs attention: disk is at 90%")
        mock_tg.assert_called_once()
        self.assertIn("disk is at 90%", mock_tg.call_args[0][0])


class TestChecklistFileExists(unittest.TestCase):
    """Verify the checklist file is properly structured."""

    def test_checklist_exists(self):
        checklist = Path("/home/nova/nova-core/HEARTBEAT_CHECKLIST.md")
        self.assertTrue(checklist.exists())

    def test_checklist_has_sections(self):
        content = Path("/home/nova/nova-core/HEARTBEAT_CHECKLIST.md").read_text()
        self.assertIn("Task Queue", content)
        self.assertIn("Research", content)
        self.assertIn("Planning", content)
        self.assertIn("System Health", content)
        self.assertIn("Memory", content)

    def test_checklist_has_response_format(self):
        content = Path("/home/nova/nova-core/HEARTBEAT_CHECKLIST.md").read_text()
        self.assertIn("HEARTBEAT_OK", content)
        self.assertIn("json", content.lower())


if __name__ == "__main__":
    unittest.main()
