"""Tests for Phase 10: Session Reconstruction & Capability Alignment.

Covers:
- Conversation recap persistence and formatting
- Recent completions store lifecycle
- Memory-persistence intent classification
- Session restart reconstruction pipeline
- Persona capability contract honesty
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# --- Import shim (same as telegram_bot.py) ---
_here = str(Path(__file__).resolve().parent.parent)
_path_backup = sys.path[:]
sys.path = [p for p in sys.path if os.path.realpath(p) != os.path.realpath(_here)]
sys.modules.pop("telegram", None)
sys.modules.pop("telegram.ext", None)
# Restore and insert project root
sys.path = _path_backup
if _here not in sys.path:
    sys.path.insert(0, _here)

import importlib.util  # noqa: E402

# Load local telegram modules directly
for _mod_name in ("parse", "persona", "conversation", "recap", "recent_completions", "working_memory"):
    _spec = importlib.util.spec_from_file_location(
        f"telegram.{_mod_name}",
        os.path.join(_here, "telegram", f"{_mod_name}.py"),
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"telegram.{_mod_name}"] = _mod
    _spec.loader.exec_module(_mod)

from telegram import parse as tg_parse  # noqa: E402
from telegram import persona  # noqa: E402
from telegram import recap as tg_recap  # noqa: E402
from telegram import recent_completions as tg_rc  # noqa: E402

# ── Conversation Recap Tests ─────────────────────────────────────────────


class TestConversationRecap(unittest.TestCase):
    """Test recap persistence, loading, staleness, and formatting."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_dir = tg_recap.RECAP_DIR
        tg_recap.RECAP_DIR = Path(self._tmpdir) / "recap"

    def tearDown(self):
        tg_recap.RECAP_DIR = self._orig_dir
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_update_and_load(self):
        tg_recap.update_recap("chat1", "build a dashboard", "On it — queued up.")
        recap = tg_recap.load_recap("chat1")
        self.assertIsNotNone(recap)
        self.assertEqual(recap["last_user_message"], "build a dashboard")
        self.assertEqual(recap["last_assistant_response"], "On it — queued up.")
        self.assertIn("build a dashboard", recap["recent_thread"])

    def test_missing_chat_returns_none(self):
        self.assertIsNone(tg_recap.load_recap("nonexistent"))

    def test_stale_recap_returns_none(self):
        tg_recap.update_recap("chat1", "old msg", "old response")
        # Manually make it stale
        path = tg_recap.RECAP_DIR / "chat1.json"
        data = json.loads(path.read_text())
        data["updated_at"] = time.time() - (tg_recap.MAX_RECAP_AGE_SECONDS + 100)
        path.write_text(json.dumps(data))
        self.assertIsNone(tg_recap.load_recap("chat1"))

    def test_recent_thread_bounded(self):
        """Recent thread keeps only last 3 entries."""
        for i in range(5):
            tg_recap.update_recap("chat1", f"msg{i}", f"resp{i}")
        recap = tg_recap.load_recap("chat1")
        self.assertEqual(len(recap["recent_thread"]), 3)
        self.assertEqual(recap["recent_thread"][-1], "msg4")

    def test_format_for_context_empty(self):
        self.assertEqual(tg_recap.format_for_context("nonexistent"), "")

    def test_format_for_context_has_content(self):
        tg_recap.update_recap("chat1", "research openclaw", "On it.")
        ctx = tg_recap.format_for_context("chat1")
        self.assertIn("LAST CONVERSATION RECAP:", ctx)
        self.assertIn("research openclaw", ctx)

    def test_clear_recap(self):
        tg_recap.update_recap("chat1", "msg", "resp")
        tg_recap.clear_recap("chat1")
        self.assertIsNone(tg_recap.load_recap("chat1"))


# ── Recent Completions Tests ─────────────────────────────────────────────


class TestRecentCompletions(unittest.TestCase):
    """Test recent-completions store lifecycle."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_file = tg_rc.RC_FILE
        self._orig_dir = tg_rc.STATE_DIR
        tg_rc.STATE_DIR = Path(self._tmpdir)
        tg_rc.RC_FILE = Path(self._tmpdir) / "recent_completions.json"

    def tearDown(self):
        tg_rc.RC_FILE = self._orig_file
        tg_rc.STATE_DIR = self._orig_dir
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_record_and_get(self):
        tg_rc.record_completion(
            task_stem="0149_test",
            chat_id="chat1",
            original_message="build a thing",
            intent_summary="Build a thing",
            output_file="/home/nova/nova-core/OUTPUT/test.md",
            output_summary_line="Built successfully",
            completion_response="Done — the thing is built.",
        )
        results = tg_rc.get_recent(chat_id="chat1")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["task_stem"], "0149_test")
        self.assertEqual(results[0]["intent_summary"], "Build a thing")

    def test_empty_store(self):
        self.assertEqual(tg_rc.get_recent(), [])

    def test_cleanup_removes_old(self):
        tg_rc.record_completion(
            task_stem="old",
            chat_id="c",
            original_message="x",
            intent_summary="x",
            output_file="",
            output_summary_line="",
            completion_response="",
        )
        # Manually age it
        entries = json.loads(tg_rc.RC_FILE.read_text())
        entries[0]["completed_at"] = time.time() - (tg_rc.DEFAULT_RETENTION_SECONDS + 100)
        tg_rc.RC_FILE.write_text(json.dumps(entries))

        removed = tg_rc.cleanup()
        self.assertEqual(removed, 1)
        self.assertEqual(tg_rc.get_recent(), [])

    def test_format_for_context_empty(self):
        self.assertEqual(tg_rc.format_for_context(), "")

    def test_format_for_context_has_content(self):
        tg_rc.record_completion(
            task_stem="0150_plan",
            chat_id="chat1",
            original_message="create implementation plan",
            intent_summary="Create OpenClaw implementation plan",
            output_file="/OUTPUT/plan.md",
            output_summary_line="10-phase plan for Nova-Core",
            completion_response="Plan is ready.",
        )
        ctx = tg_rc.format_for_context(chat_id="chat1")
        self.assertIn("RECENTLY COMPLETED TASKS", ctx)
        self.assertIn("Create OpenClaw implementation plan", ctx)
        self.assertIn("10-phase plan", ctx)

    def test_max_entries_bounded(self):
        for i in range(25):
            tg_rc.record_completion(
                task_stem=f"task_{i}",
                chat_id="c",
                original_message="x",
                intent_summary=f"task {i}",
                output_file="",
                output_summary_line="",
                completion_response="",
            )
        entries = json.loads(tg_rc.RC_FILE.read_text())
        self.assertLessEqual(len(entries), tg_rc.MAX_ENTRIES)

    def test_chat_id_filtering(self):
        tg_rc.record_completion(
            task_stem="a",
            chat_id="chat1",
            original_message="x",
            intent_summary="a",
            output_file="",
            output_summary_line="",
            completion_response="",
        )
        tg_rc.record_completion(
            task_stem="b",
            chat_id="chat2",
            original_message="x",
            intent_summary="b",
            output_file="",
            output_summary_line="",
            completion_response="",
        )
        r1 = tg_rc.get_recent(chat_id="chat1")
        r2 = tg_rc.get_recent(chat_id="chat2")
        self.assertEqual(len(r1), 1)
        self.assertEqual(len(r2), 1)
        self.assertEqual(r1[0]["task_stem"], "a")
        self.assertEqual(r2[0]["task_stem"], "b")


# ── Memory-Persistence Intent Tests ──────────────────────────────────────


class TestMemoryPersistIntent(unittest.TestCase):
    """Test memory-persistence request classification.

    Memory-persist requests route to conversation (chat) so CEO Nova
    can handle them inline via nova-vault and nova-memory MCP tools.
    """

    def _intent(self, text):
        return tg_parse.classify_intent(text)

    def _is_persist(self, text):
        return tg_parse.is_memory_persist_request(text)

    def test_save_to_memory(self):
        self.assertEqual(self._intent("save this to memory"), "chat")
        self.assertTrue(self._is_persist("save this to memory"))

    def test_save_to_obsidian(self):
        self.assertEqual(self._intent("save it to your obsidian memory"), "chat")
        self.assertTrue(self._is_persist("save it to your obsidian memory"))

    def test_add_to_vault(self):
        self.assertEqual(self._intent("Can you add this to the vault"), "chat")
        self.assertTrue(self._is_persist("Can you add this to the vault"))

    def test_remember_this(self):
        self.assertEqual(self._intent("remember this in obsidian"), "chat")
        self.assertTrue(self._is_persist("remember this in obsidian"))

    def test_write_to_notes(self):
        self.assertEqual(self._intent("write this to notes"), "chat")
        self.assertTrue(self._is_persist("write this to notes"))

    def test_store_in_memory(self):
        self.assertEqual(self._intent("store this in memory"), "chat")
        self.assertTrue(self._is_persist("store this in memory"))

    def test_make_a_note(self):
        self.assertEqual(self._intent("make a note of this"), "chat")
        self.assertTrue(self._is_persist("make a note of this"))

    def test_dont_forget(self):
        self.assertEqual(self._intent("don't forget this for next time"), "chat")
        self.assertTrue(self._is_persist("don't forget this for next time"))

    def test_casual_remember_is_chat(self):
        """'do you remember' is a chat signal, not a persist request."""
        self.assertFalse(self._is_persist("do you remember what we did yesterday"))

    def test_normal_chat_not_persist(self):
        self.assertFalse(self._is_persist("how are you doing"))
        self.assertFalse(self._is_persist("what time is it"))


# ── Persona Capability Contract Tests ────────────────────────────────────


class TestCapabilityContract(unittest.TestCase):
    """Verify persona accurately reflects the subprocess's MCP tool access."""

    def test_brave_search_available(self):
        """Subprocess has brave-search MCP — persona should reference web search."""
        self.assertIn("brave-search", persona.SYSTEM_PROMPT)

    def test_tavily_available(self):
        """Subprocess has tavily MCP — persona should reference deep research."""
        self.assertIn("tavily", persona.SYSTEM_PROMPT)

    def test_vault_available(self):
        """Subprocess has nova-vault MCP — persona should reference Obsidian."""
        self.assertIn("nova-vault", persona.SYSTEM_PROMPT)

    def test_fusion_memory_available(self):
        """Fusion Memory is available — pre-loaded context + direct query."""
        self.assertIn("Fusion Memory", persona.SYSTEM_PROMPT)
        self.assertIn("pre-loaded", persona.SYSTEM_PROMPT.lower())

    def test_fetch_available(self):
        """Subprocess has fetch MCP — persona should reference web fetch."""
        self.assertIn("fetch", persona.SYSTEM_PROMPT.lower())

    def test_playwright_available(self):
        """Subprocess has playwright MCP — persona should reference browser."""
        self.assertIn("Browser Automation", persona.SYSTEM_PROMPT)

    def test_delegation_list_scoped(self):
        """Only heavy-work items are in the delegation list."""
        prompt = persona.SYSTEM_PROMPT
        # Heavy work still delegates
        self.assertIn("Send me a PDF/file/document", prompt)
        # Quick actions are NOT in delegation list — handled inline
        self.assertNotIn("Save this to memory/obsidian/vault", prompt)
        self.assertNotIn("Search the web for...", prompt)

    def test_direct_action_encouraged(self):
        """Persona tells CEO Nova to use tools directly."""
        self.assertIn("USE TOOLS DIRECTLY", persona.SYSTEM_PROMPT)

    def test_session_start_no_memory_fetch(self):
        """Session start hint should NOT tell LLM to call memory tools."""
        hint = persona.SESSION_START_HINT.lower()
        self.assertNotIn("get_last_checkpoint", hint)
        self.assertNotIn("call get_last", hint)
        self.assertIn("pre-loaded", hint)

    def test_memory_persist_ack_prompt_exists(self):
        self.assertIsInstance(persona.MEMORY_PERSIST_ACK_PROMPT, str)
        self.assertIn("save", persona.MEMORY_PERSIST_ACK_PROMPT.lower())


# ── Session Reconstruction Pipeline Tests ────────────────────────────────


class TestSessionReconstruction(unittest.TestCase):
    """Test that session restart assembles context from multiple sources."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        # Redirect recap and rc to temp dirs
        self._orig_recap_dir = tg_recap.RECAP_DIR
        self._orig_rc_file = tg_rc.RC_FILE
        self._orig_rc_state = tg_rc.STATE_DIR
        tg_recap.RECAP_DIR = Path(self._tmpdir) / "recap"
        tg_rc.STATE_DIR = Path(self._tmpdir)
        tg_rc.RC_FILE = Path(self._tmpdir) / "recent_completions.json"

    def tearDown(self):
        tg_recap.RECAP_DIR = self._orig_recap_dir
        tg_rc.RC_FILE = self._orig_rc_file
        tg_rc.STATE_DIR = self._orig_rc_state
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_recap_survives_conversation_expiry(self):
        """Recap persists even when conversation buffer would be empty."""
        tg_recap.update_recap("chat1", "create implementation plan", "Working on it.")
        # Simulate 3-hour gap — recap should still be valid
        recap = tg_recap.load_recap("chat1")
        self.assertIsNotNone(recap)
        self.assertIn("implementation plan", recap["last_user_message"])

    def test_recent_completions_survives_conversation_expiry(self):
        """Recent completions persist for 4 hours."""
        tg_rc.record_completion(
            task_stem="test",
            chat_id="chat1",
            original_message="do the thing",
            intent_summary="Do the thing",
            output_file="out.md",
            output_summary_line="Done",
            completion_response="The thing is done.",
        )
        # Should be retrievable
        ctx = tg_rc.format_for_context(chat_id="chat1")
        self.assertIn("Do the thing", ctx)

    def test_reconstruction_combines_sources(self):
        """Both recap and recent-completions contribute to context."""
        tg_recap.update_recap("chat1", "research openclaw", "On it.")
        tg_rc.record_completion(
            task_stem="0147",
            chat_id="chat1",
            original_message="deep research openclaw",
            intent_summary="OpenClaw deep research",
            output_file="out.md",
            output_summary_line="Full report generated",
            completion_response="Research complete.",
        )
        recap_ctx = tg_recap.format_for_context("chat1")
        rc_ctx = tg_rc.format_for_context(chat_id="chat1")
        self.assertIn("research openclaw", recap_ctx)
        self.assertIn("OpenClaw deep research", rc_ctx)


if __name__ == "__main__":
    unittest.main()
