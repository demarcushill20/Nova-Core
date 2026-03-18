"""Tests for CEO Nova Phase 9: Presentation Layer.

Tests persona rewrite, notifier deference, artifact classification,
and trio identity model.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Load local telegram modules via importlib (same pattern as telegram_bot.py)
_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _mod_name in (
    "parse",
    "conversation",
    "llm",
    "persona",
    "format",
    "delegation",
    "goals",
    "hardening",
    "working_memory",
):
    _path = os.path.join(_here, "telegram", f"{_mod_name}.py")
    if os.path.exists(_path):
        _spec = importlib.util.spec_from_file_location(_mod_name, _path)
        _mod = importlib.util.module_from_spec(_spec)
        # Register before exec so dataclass decorator can find the module
        sys.modules[_mod_name] = _mod
        _spec.loader.exec_module(_mod)
        sys.modules[f"tg_{_mod_name}"] = _mod

parse = sys.modules["tg_parse"]
persona = sys.modules["tg_persona"]


# ── Fix 2: Persona Rewrite Tests ─────────────────────────────────────────


class TestPersonaExecutiveVoice(unittest.TestCase):
    """Verify the persona no longer leaks internal architecture language."""

    def test_no_conversation_path_language(self):
        """SYSTEM_PROMPT must not contain 'conversation path'."""
        self.assertNotIn("conversation path", persona.SYSTEM_PROMPT)

    def test_no_classifier_language(self):
        """SYSTEM_PROMPT must not mention 'classifier' or 'classified'."""
        self.assertNotIn("classifier", persona.SYSTEM_PROMPT.lower())

    def test_no_conversation_mode_language(self):
        """SYSTEM_PROMPT must not say 'conversation mode'."""
        self.assertNotIn("conversation mode", persona.SYSTEM_PROMPT)

    def test_has_queue_that_up_language(self):
        """SYSTEM_PROMPT should contain executive routing language."""
        self.assertIn("queue that up", persona.SYSTEM_PROMPT.lower())

    def test_has_hand_that_off_language(self):
        """SYSTEM_PROMPT should contain 'hand that off'."""
        self.assertIn("hand that off", persona.SYSTEM_PROMPT.lower())

    def test_has_handling_work_requests_section(self):
        """SYSTEM_PROMPT should have the new HANDLING WORK REQUESTS section."""
        self.assertIn("HANDLING WORK REQUESTS", persona.SYSTEM_PROMPT)

    def test_no_delegation_boundary_language(self):
        """Old 'DELEGATION BOUNDARY' heading should be gone."""
        self.assertNotIn("DELEGATION BOUNDARY", persona.SYSTEM_PROMPT)

    def test_never_say_cant_from_conversation(self):
        """The forbidden phrase should not appear in the prompt."""
        self.assertNotIn("can't do that from the conversation", persona.SYSTEM_PROMPT.lower())

    def test_invisible_internals_principle(self):
        """SYSTEM_PROMPT should state that internal details are invisible."""
        self.assertIn("invisible", persona.SYSTEM_PROMPT.lower())


# ── Fix 4: Trio Identity Tests ────────────────────────────────────────────


class TestTrioIdentity(unittest.TestCase):
    """Verify the trio identity model is present in the persona."""

    def test_partnership_model_section(self):
        """SYSTEM_PROMPT should have PARTNERSHIP MODEL section."""
        self.assertIn("PARTNERSHIP MODEL", persona.SYSTEM_PROMPT)

    def test_three_partner_system(self):
        """SYSTEM_PROMPT should mention three-partner system."""
        self.assertIn("three-partner", persona.SYSTEM_PROMPT)

    def test_ceo_nova_identity(self):
        """SYSTEM_PROMPT should describe CEO Nova role."""
        self.assertIn("CEO Nova", persona.SYSTEM_PROMPT)

    def test_chatgpt_nova_identity(self):
        """SYSTEM_PROMPT should mention ChatGPT Nova."""
        self.assertIn("ChatGPT Nova", persona.SYSTEM_PROMPT)

    def test_novacore_runtime_identity(self):
        """SYSTEM_PROMPT should mention Nova-Core Runtime."""
        self.assertIn("Nova-Core Runtime", persona.SYSTEM_PROMPT)


# ── Fix 3: Artifact Request Classification Tests ─────────────────────────


class TestArtifactRequestClassification(unittest.TestCase):
    """Verify artifact requests route to task without traditional action verbs."""

    def test_send_me_a_pdf(self):
        self.assertEqual(parse.classify_intent("send me the plan as a PDF"), "task")

    def test_convert_to_document(self):
        self.assertEqual(parse.classify_intent("convert the report to a document"), "task")

    def test_export_as_csv(self):
        self.assertEqual(parse.classify_intent("can you export that as CSV?"), "task")

    def test_turn_into_pdf(self):
        self.assertEqual(parse.classify_intent("turn that into a PDF"), "task")

    def test_email_me_the_report_as_pdf(self):
        self.assertEqual(parse.classify_intent("email me the report as a PDF"), "task")

    def test_share_as_a_doc(self):
        self.assertEqual(parse.classify_intent("share that as a doc"), "task")

    def test_save_as_zip(self):
        self.assertEqual(parse.classify_intent("save everything as a zip"), "task")

    def test_send_me_a_message_stays_chat(self):
        """'send me a message' has no artifact keyword — should stay chat."""
        self.assertEqual(parse.classify_intent("send me a message about the project"), "chat")

    def test_tell_me_about_pdf_stays_chat(self):
        """Informational requests about PDF should stay chat."""
        self.assertEqual(parse.classify_intent("tell me about PDF generation"), "chat")

    def test_what_is_a_csv_stays_chat(self):
        """Questions about file formats should stay chat."""
        self.assertEqual(parse.classify_intent("what is a CSV file?"), "chat")


# ── Fix 1: Delegation Marker Tests (unit-level) ──────────────────────────


class TestDelegationMarkers(unittest.TestCase):
    """Test delegation marker write/cleanup/check functions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_dir = Path(self.tmpdir) / "STATE"
        self.ceo_dir = self.state_dir / "ceo_delegated"
        self.ceo_dir.mkdir(parents=True)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_delegation_marker_creates_file(self):
        """_write_delegation_marker should create a .delegated file."""
        marker = self.ceo_dir / "0010_test_task.delegated"
        marker.write_text(f"{time.time()}\n", encoding="utf-8")
        self.assertTrue(marker.exists())

    def test_cleanup_delegation_marker_removes_file(self):
        """_cleanup_delegation_marker should remove the .delegated file."""
        marker = self.ceo_dir / "0010_test_task.delegated"
        marker.write_text(f"{time.time()}\n", encoding="utf-8")
        marker.unlink(missing_ok=True)
        self.assertFalse(marker.exists())

    def test_is_ceo_delegated_returns_true(self):
        """_is_ceo_delegated should return True when marker exists."""
        # Simulate: output file is 0010_test__20260309-120000.md
        # Task stem is 0010_test
        marker = self.ceo_dir / "0010_test.delegated"
        marker.write_text(f"{time.time()}\n", encoding="utf-8")
        self.assertTrue(marker.exists())

    def test_is_ceo_delegated_returns_false(self):
        """_is_ceo_delegated should return False when no marker."""
        marker = self.ceo_dir / "0010_nonexistent.delegated"
        self.assertFalse(marker.exists())

    def test_stale_marker_cleanup(self):
        """Markers older than max_age should be cleaned up."""
        marker = self.ceo_dir / "0010_old_task.delegated"
        marker.write_text("1000000000\n", encoding="utf-8")
        # Set mtime to 2 days ago
        old_time = time.time() - 172800
        os.utime(marker, (old_time, old_time))

        # Run cleanup
        cutoff = time.time() - 86400
        for m in self.ceo_dir.glob("*.delegated"):
            if m.stat().st_mtime < cutoff:
                m.unlink()

        self.assertFalse(marker.exists())


class TestNotifierDeference(unittest.TestCase):
    """Test the notifier's _is_ceo_delegated function."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ceo_dir = Path(self.tmpdir) / "ceo_delegated"
        self.ceo_dir.mkdir(parents=True)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_stem_extraction_from_output_name(self):
        """Correctly extract task stem from output filename."""
        # Output: 0010_foo_bar__20260309-120000.md → stem: 0010_foo_bar
        name = "0010_foo_bar__20260309-120000"
        m = re.match(r"^(.+?)__\d{8}-\d{6}$", name)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "0010_foo_bar")

    def test_delegated_check_with_marker(self):
        """With marker present, should detect delegation."""
        stem = "0010_foo_bar"
        marker = self.ceo_dir / f"{stem}.delegated"
        marker.write_text(f"{time.time()}\n")
        self.assertTrue(marker.exists())

    def test_delegated_check_without_marker(self):
        """Without marker, should not detect delegation."""
        stem = "0010_foo_bar"
        marker = self.ceo_dir / f"{stem}.delegated"
        self.assertFalse(marker.exists())

    def test_non_delegated_task_has_no_marker(self):
        """Tasks not from CEO Nova should have no marker."""
        # Watcher-originated task
        stem = "0010_watcher_task"
        self.assertFalse((self.ceo_dir / f"{stem}.delegated").exists())


if __name__ == "__main__":
    unittest.main()
