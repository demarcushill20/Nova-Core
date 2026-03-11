"""Tests for Phase 5 — Session Management & Context Compaction.

Covers:
- Session creation and expiration
- Task recording (start/completion)
- CONTRACT block extraction from output reports
- Context injection building
- Session persistence and archival
- Conversation buffer compaction
- Session summary in conversation history
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

_here = str(Path(__file__).resolve().parent.parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

from agents.session_manager import (  # noqa: E402
    MAX_CONTEXT_INJECTION_BYTES,
    SESSION_TIMEOUT_S,
    Session,
    SessionManager,
    extract_contract,
)
from telegram.conversation import MAX_MESSAGES, ConversationBuffer  # noqa: E402


class TestContractExtraction(unittest.TestCase):
    """Test CONTRACT block parsing from output reports."""

    def test_extracts_all_fields(self):
        text = """# Output Report

Some content here.

## CONTRACT
summary: Implemented user authentication
files_changed: auth.py, tests/test_auth.py
verification: ran tests: 5/5 pass
confidence: high
"""
        contract = extract_contract(text)
        self.assertEqual(contract["summary"], "Implemented user authentication")
        self.assertEqual(contract["files_changed"], "auth.py, tests/test_auth.py")
        self.assertEqual(contract["verification"], "ran tests: 5/5 pass")
        self.assertEqual(contract["confidence"], "high")

    def test_handles_missing_contract(self):
        text = "Just a plain report with no contract block."
        contract = extract_contract(text)
        self.assertEqual(contract["summary"], "")

    def test_handles_partial_contract(self):
        text = """## CONTRACT
summary: Did something
confidence: medium
"""
        contract = extract_contract(text)
        self.assertEqual(contract["summary"], "Did something")
        self.assertEqual(contract["confidence"], "medium")
        self.assertEqual(contract["files_changed"], "")

    def test_contract_at_end_of_file(self):
        text = """# Report
## CONTRACT
summary: Final task
files_changed: none
verification: not run
confidence: low"""
        contract = extract_contract(text)
        self.assertEqual(contract["summary"], "Final task")
        self.assertEqual(contract["confidence"], "low")


class TestSessionLifecycle(unittest.TestCase):
    """Test session creation, expiration, and archival."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._session_dir = Path(self._tmpdir) / "sessions"
        self._output_dir = Path(self._tmpdir) / "OUTPUT"
        self._output_dir.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_mgr(self):
        return SessionManager(
            session_dir=self._session_dir,
            output_dir=self._output_dir,
        )

    def test_creates_new_session(self):
        mgr = self._make_mgr()
        session = mgr.get_or_create_session()
        self.assertTrue(session.session_id.startswith("ses_"))
        self.assertEqual(session.status, "active")

    def test_returns_same_session(self):
        mgr = self._make_mgr()
        s1 = mgr.get_or_create_session()
        s2 = mgr.get_or_create_session()
        self.assertEqual(s1.session_id, s2.session_id)

    def test_persists_to_disk(self):
        mgr = self._make_mgr()
        session = mgr.get_or_create_session()
        path = self._session_dir / f"{session.session_id}.json"
        self.assertTrue(path.exists())

    def test_loads_from_disk(self):
        mgr1 = self._make_mgr()
        session = mgr1.get_or_create_session()
        mgr1.record_task_start("test_task_1")
        sid = session.session_id

        # New manager should load existing session
        mgr2 = self._make_mgr()
        loaded = mgr2.get_or_create_session()
        self.assertEqual(loaded.session_id, sid)
        self.assertEqual(len(loaded.tasks), 1)

    def test_expired_session_archived(self):
        mgr = self._make_mgr()
        session = mgr.get_or_create_session()
        old_sid = session.session_id

        # Force expiration
        session.last_activity = time.time() - SESSION_TIMEOUT_S - 10
        mgr._persist(session)
        mgr._current = None

        new_session = mgr.get_or_create_session()
        self.assertNotEqual(new_session.session_id, old_sid)

        # Old session should be archived
        old_data = json.loads(
            (self._session_dir / f"{old_sid}.json").read_text()
        )
        self.assertEqual(old_data["status"], "archived")


class TestTaskRecording(unittest.TestCase):
    """Test task start/completion recording."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._session_dir = Path(self._tmpdir) / "sessions"
        self._output_dir = Path(self._tmpdir) / "OUTPUT"
        self._output_dir.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_mgr(self):
        return SessionManager(
            session_dir=self._session_dir,
            output_dir=self._output_dir,
        )

    def test_records_task_start(self):
        mgr = self._make_mgr()
        mgr.record_task_start("0200_test_task")
        session = mgr.get_or_create_session()
        self.assertEqual(len(session.tasks), 1)
        self.assertEqual(session.tasks[0]["stem"], "0200_test_task")
        self.assertEqual(session.tasks[0]["status"], "pending")

    def test_records_task_completion(self):
        mgr = self._make_mgr()
        mgr.record_task_start("0200_test_task")

        # Create output report
        report = """# Report
## CONTRACT
summary: Completed the test task
files_changed: test.py
verification: ran tests
confidence: high
"""
        (self._output_dir / "0200_test_task__20260310-120000.md").write_text(report)

        mgr.record_task_completion("0200_test_task", success=True)
        session = mgr.get_or_create_session()
        task = session.tasks[0]
        self.assertEqual(task["status"], "completed")
        self.assertIn("Completed the test task", task["summary"])

    def test_records_task_failure(self):
        mgr = self._make_mgr()
        mgr.record_task_start("0200_fail_task")
        mgr.record_task_completion("0200_fail_task", success=False)
        session = mgr.get_or_create_session()
        self.assertEqual(session.tasks[0]["status"], "failed")


class TestContextInjection(unittest.TestCase):
    """Test context injection for worker prompts."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._session_dir = Path(self._tmpdir) / "sessions"
        self._output_dir = Path(self._tmpdir) / "OUTPUT"
        self._output_dir.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_mgr(self):
        return SessionManager(
            session_dir=self._session_dir,
            output_dir=self._output_dir,
        )

    def test_no_context_when_empty(self):
        mgr = self._make_mgr()
        ctx = mgr.build_context_injection("0200_new_task")
        self.assertEqual(ctx, "")

    def test_injects_completed_task_summary(self):
        mgr = self._make_mgr()
        mgr.record_task_start("0200_first_task")

        report = """# Report
## CONTRACT
summary: Set up database schema
files_changed: schema.sql
verification: verified
confidence: high
"""
        (self._output_dir / "0200_first_task__20260310-120000.md").write_text(report)
        mgr.record_task_completion("0200_first_task", success=True)

        ctx = mgr.build_context_injection("0200_second_task")
        self.assertIn("SESSION CONTEXT", ctx)
        self.assertIn("0200_first_task", ctx)
        self.assertIn("Set up database schema", ctx)

    def test_excludes_current_task(self):
        mgr = self._make_mgr()
        mgr.record_task_start("0200_task_a")
        report = "## CONTRACT\nsummary: Did A\nfiles_changed: none\nverification: ok\nconfidence: medium\n"
        (self._output_dir / "0200_task_a__20260310-120000.md").write_text(report)
        mgr.record_task_completion("0200_task_a", success=True)

        mgr.record_task_start("0200_task_b")
        ctx = mgr.build_context_injection("0200_task_b")
        self.assertIn("0200_task_a", ctx)
        self.assertNotIn("[done] 0200_task_b", ctx)

    def test_context_bounded(self):
        mgr = self._make_mgr()
        # Create many completed tasks
        for i in range(20):
            stem = f"0200_task_{i:03d}"
            mgr.record_task_start(stem)
            report = (
                f"## CONTRACT\nsummary: Task {i} done with lots"
                f" of detail\nfiles_changed: file{i}.py\n"
                "verification: ok\nconfidence: medium\n"
            )
            (self._output_dir / f"{stem}__20260310-120000.md").write_text(report)
            mgr.record_task_completion(stem, success=True)

        ctx = mgr.build_context_injection("0200_new_task")
        self.assertLessEqual(len(ctx.encode("utf-8")), MAX_CONTEXT_INJECTION_BYTES + 50)

    def test_includes_failed_task_status(self):
        mgr = self._make_mgr()
        mgr.record_task_start("0200_broke")
        report = (
            "## CONTRACT\nsummary: Attempted X but it broke\n"
            "files_changed: none\nverification: not run\n"
            "confidence: low\n"
        )
        (self._output_dir / "0200_broke__20260310-120000.md").write_text(report)
        mgr.record_task_completion("0200_broke", success=False)

        ctx = mgr.build_context_injection("0200_next")
        self.assertIn("FAILED", ctx)
        self.assertIn("0200_broke", ctx)


class TestSessionCleanup(unittest.TestCase):
    """Test old session cleanup."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._session_dir = Path(self._tmpdir) / "sessions"
        self._session_dir.mkdir(parents=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_removes_old_sessions(self):
        mgr = SessionManager(
            session_dir=self._session_dir,
            output_dir=Path(self._tmpdir) / "OUTPUT",
        )
        # Create old session
        old = Session(
            session_id="ses_old",
            created_at=time.time() - 30 * 86400,
            last_activity=time.time() - 30 * 86400,
            status="archived",
        )
        mgr._persist(old)

        removed = mgr.cleanup_old_sessions(max_age_days=7)
        self.assertEqual(removed, 1)
        self.assertFalse((self._session_dir / "ses_old.json").exists())

    def test_keeps_recent_sessions(self):
        mgr = SessionManager(
            session_dir=self._session_dir,
            output_dir=Path(self._tmpdir) / "OUTPUT",
        )
        recent = Session(
            session_id="ses_recent",
            created_at=time.time() - 3600,
            last_activity=time.time() - 3600,
            status="archived",
        )
        mgr._persist(recent)

        removed = mgr.cleanup_old_sessions(max_age_days=7)
        self.assertEqual(removed, 0)


class TestConversationCompaction(unittest.TestCase):
    """Test conversation buffer context compaction."""

    def test_compacts_on_overflow(self):
        buf = ConversationBuffer()
        # Add MAX_MESSAGES + 5 messages
        for i in range(MAX_MESSAGES + 5):
            buf.add("user", f"Message about topic {i}")
            buf.add("assistant", f"Response {i}")

        # Should have session_summary from evicted messages
        self.assertTrue(len(buf.session_summary) > 0)
        self.assertEqual(len(buf.messages), MAX_MESSAGES)

    def test_summary_in_history(self):
        buf = ConversationBuffer()
        buf.session_summary = "User discussed API design and testing"
        buf.add("user", "What about deployment?")
        buf.add("assistant", "Let me help with that.")

        history = buf.get_history()
        # First entry should be the summary
        self.assertIn("Prior conversation summary", history[0]["content"])
        self.assertIn("API design", history[0]["content"])

    def test_no_summary_when_empty(self):
        buf = ConversationBuffer()
        buf.add("user", "Hello")
        history = buf.get_history()
        # Should not have summary entry
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["role"], "user")

    def test_clear_resets_summary(self):
        buf = ConversationBuffer()
        buf.session_summary = "prior context"
        buf.clear()
        self.assertEqual(buf.session_summary, "")

    def test_summary_bounded(self):
        buf = ConversationBuffer()
        # Add lots of messages to generate long summary
        for i in range(100):
            buf.add("user", f"A very detailed discussion about topic number {i} with lots of context")
            buf.add("assistant", f"Response {i}")
        self.assertLessEqual(len(buf.session_summary), 800)


class TestConversationPersistence(unittest.TestCase):
    """Test that session_summary persists across saves/loads."""

    def test_summary_persisted(self):
        tmpdir = tempfile.mkdtemp()
        try:
            import telegram.conversation as conv_mod
            from telegram.conversation import ConversationManager
            orig_persist = conv_mod.PERSIST_DIR
            conv_mod.PERSIST_DIR = Path(tmpdir)

            mgr = ConversationManager(persist=True)
            buf = mgr.get("test_chat")
            buf.session_summary = "Prior context about auth system"
            buf.add("user", "Continue auth work")
            mgr._save_chat("test_chat")

            # Reload
            mgr2 = ConversationManager(persist=True)
            buf2 = mgr2.get("test_chat")
            self.assertEqual(buf2.session_summary, "Prior context about auth system")

            conv_mod.PERSIST_DIR = orig_persist
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestArchivedSessionContext(unittest.TestCase):
    """Test context injection from archived sessions."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._session_dir = Path(self._tmpdir) / "sessions"
        self._output_dir = Path(self._tmpdir) / "OUTPUT"
        self._output_dir.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_falls_back_to_archived_session(self):
        mgr = SessionManager(
            session_dir=self._session_dir,
            output_dir=self._output_dir,
        )

        # Create an archived session with completed tasks
        archived = Session(
            session_id="ses_archived",
            created_at=time.time() - 3 * 3600,
            last_activity=time.time() - 3 * 3600,
            status="archived",
            tasks=[{
                "stem": "0200_prior_work",
                "status": "completed",
                "started_at": time.time() - 3 * 3600,
                "completed_at": time.time() - 3 * 3600,
                "summary": "Set up the auth system",
                "files_changed": "auth.py",
                "confidence": "high",
            }],
        )
        mgr._persist(archived)

        # New session has no completed tasks yet
        ctx = mgr.build_context_injection("0200_new_task")
        self.assertIn("0200_prior_work", ctx)
        self.assertIn("Set up the auth system", ctx)


if __name__ == "__main__":
    unittest.main()
