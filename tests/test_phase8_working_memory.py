"""Tests for Phase 8: Working Memory, Intent Classification, and Executive Continuity.

Tests cover:
- WorkingMemoryStore (add, update, complete, archive, format)
- Intent classifier edge cases (deliverable detector, request verbs)
- Integration: delegation → working memory → context injection
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import time
import unittest

# Load local telegram modules via importlib (same pattern as test_ceo_nova_phase1.py)
_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _mod_name in ("parse", "conversation", "llm", "persona", "delegation", "goals", "hardening", "working_memory"):
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
working_memory = sys.modules["tg_working_memory"]


# ── Working Memory Store Tests ────────────────────────────────────────────


class TestActiveTaskDataclass(unittest.TestCase):
    def test_fields_exist(self):
        t = working_memory.ActiveTask(
            task_stem="0001_test",
            chat_id="123",
            original_message="Build a thing",
            intent_summary="Build a thing",
            created_at=1000.0,
            status="pending",
            context_snapshot=[],
            task_file="/home/nova/nova-core/TASKS/0001_test.md",
        )
        self.assertEqual(t.task_stem, "0001_test")
        self.assertEqual(t.chat_id, "123")
        self.assertEqual(t.status, "pending")
        self.assertEqual(t.task_file, "/home/nova/nova-core/TASKS/0001_test.md")

    def test_task_file_default_empty(self):
        t = working_memory.ActiveTask(
            task_stem="0001_test",
            chat_id="123",
            original_message="Build a thing",
            intent_summary="Build a thing",
            created_at=1000.0,
            status="pending",
            context_snapshot=[],
        )
        self.assertEqual(t.task_file, "")


class TestWorkingMemoryStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = os.path.join(os.path.dirname(__file__), "_test_wm")
        os.makedirs(self.tmpdir, exist_ok=True)
        self._orig_state = working_memory.STATE_DIR
        self._orig_file = working_memory.WM_FILE
        self._orig_archive = working_memory.WM_ARCHIVE
        working_memory.STATE_DIR = type(working_memory.STATE_DIR)(self.tmpdir)
        working_memory.WM_FILE = working_memory.STATE_DIR / "working_memory.json"
        working_memory.WM_ARCHIVE = working_memory.STATE_DIR / "working_memory_archive.json"

    def tearDown(self):
        working_memory.STATE_DIR = self._orig_state
        working_memory.WM_FILE = self._orig_file
        working_memory.WM_ARCHIVE = self._orig_archive
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_task(self, stem="0001_test", chat_id="123", status="pending"):
        return working_memory.ActiveTask(
            task_stem=stem,
            chat_id=chat_id,
            original_message=f"Build {stem}",
            intent_summary=f"Build {stem}",
            created_at=time.time(),
            status=status,
            context_snapshot=[{"role": "user", "content": "hi"}],
            task_file=f"/home/nova/nova-core/TASKS/{stem}.md",
        )

    def test_add_and_get(self):
        store = working_memory.WorkingMemoryStore()
        task = self._make_task()
        store.add(task)
        retrieved = store.get("0001_test")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.task_stem, "0001_test")
        self.assertEqual(retrieved.original_message, "Build 0001_test")

    def test_get_nonexistent_returns_none(self):
        store = working_memory.WorkingMemoryStore()
        self.assertIsNone(store.get("nonexistent"))

    def test_update_status(self):
        store = working_memory.WorkingMemoryStore()
        store.add(self._make_task())
        store.update_status("0001_test", "running")
        self.assertEqual(store.get("0001_test").status, "running")

    def test_update_status_nonexistent_is_noop(self):
        store = working_memory.WorkingMemoryStore()
        store.update_status("nonexistent", "running")  # should not raise

    def test_complete_returns_task(self):
        store = working_memory.WorkingMemoryStore()
        store.add(self._make_task())
        completed = store.complete("0001_test")
        self.assertIsNotNone(completed)
        self.assertEqual(completed.status, "completed")
        # No longer in active tasks
        self.assertIsNone(store.get("0001_test"))

    def test_complete_nonexistent_returns_none(self):
        store = working_memory.WorkingMemoryStore()
        self.assertIsNone(store.complete("nonexistent"))

    def test_active_tasks(self):
        store = working_memory.WorkingMemoryStore()
        store.add(self._make_task("0001_a"))
        store.add(self._make_task("0002_b"))
        self.assertEqual(len(store.active_tasks()), 2)

    def test_active_for_chat(self):
        store = working_memory.WorkingMemoryStore()
        store.add(self._make_task("0001_a", chat_id="c1"))
        store.add(self._make_task("0002_b", chat_id="c2"))
        store.add(self._make_task("0003_c", chat_id="c1"))
        c1_tasks = store.active_for_chat("c1")
        self.assertEqual(len(c1_tasks), 2)
        stems = {t.task_stem for t in c1_tasks}
        self.assertEqual(stems, {"0001_a", "0003_c"})

    def test_active_for_chat_empty(self):
        store = working_memory.WorkingMemoryStore()
        self.assertEqual(store.active_for_chat("unknown"), [])


class TestWorkingMemoryPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = os.path.join(os.path.dirname(__file__), "_test_wm_persist")
        os.makedirs(self.tmpdir, exist_ok=True)
        self._orig_state = working_memory.STATE_DIR
        self._orig_file = working_memory.WM_FILE
        self._orig_archive = working_memory.WM_ARCHIVE
        working_memory.STATE_DIR = type(working_memory.STATE_DIR)(self.tmpdir)
        working_memory.WM_FILE = working_memory.STATE_DIR / "working_memory.json"
        working_memory.WM_ARCHIVE = working_memory.STATE_DIR / "working_memory_archive.json"

    def tearDown(self):
        working_memory.STATE_DIR = self._orig_state
        working_memory.WM_FILE = self._orig_file
        working_memory.WM_ARCHIVE = self._orig_archive
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_task(self, stem="0001_test"):
        return working_memory.ActiveTask(
            task_stem=stem,
            chat_id="123",
            original_message=f"Build {stem}",
            intent_summary=f"Build {stem}",
            created_at=1000.0,
            status="pending",
            context_snapshot=[],
        )

    def test_save_and_reload(self):
        store1 = working_memory.WorkingMemoryStore()
        store1.add(self._make_task("0001_a"))
        store1.add(self._make_task("0002_b"))

        # New store instance loads from disk
        store2 = working_memory.WorkingMemoryStore()
        self.assertEqual(len(store2.active_tasks()), 2)
        self.assertIsNotNone(store2.get("0001_a"))
        self.assertIsNotNone(store2.get("0002_b"))

    def test_json_file_created(self):
        store = working_memory.WorkingMemoryStore()
        store.add(self._make_task())
        self.assertTrue(working_memory.WM_FILE.exists())
        data = json.loads(working_memory.WM_FILE.read_text())
        self.assertIn("0001_test", data)

    def test_archive_on_complete(self):
        store = working_memory.WorkingMemoryStore()
        store.add(self._make_task())
        store.complete("0001_test")
        self.assertTrue(working_memory.WM_ARCHIVE.exists())
        archive = json.loads(working_memory.WM_ARCHIVE.read_text())
        self.assertEqual(len(archive), 1)
        self.assertEqual(archive[0]["task_stem"], "0001_test")
        self.assertEqual(archive[0]["status"], "completed")

    def test_archive_keeps_last_100(self):
        store = working_memory.WorkingMemoryStore()
        for i in range(105):
            stem = f"{i:04d}_task"
            store.add(self._make_task(stem))
            store.complete(stem)
        archive = json.loads(working_memory.WM_ARCHIVE.read_text())
        self.assertEqual(len(archive), 100)

    def test_empty_store_loads_cleanly(self):
        store = working_memory.WorkingMemoryStore()
        self.assertEqual(len(store.active_tasks()), 0)


class TestWorkingMemoryFormatting(unittest.TestCase):
    def setUp(self):
        self.tmpdir = os.path.join(os.path.dirname(__file__), "_test_wm_fmt")
        os.makedirs(self.tmpdir, exist_ok=True)
        self._orig_state = working_memory.STATE_DIR
        self._orig_file = working_memory.WM_FILE
        self._orig_archive = working_memory.WM_ARCHIVE
        working_memory.STATE_DIR = type(working_memory.STATE_DIR)(self.tmpdir)
        working_memory.WM_FILE = working_memory.STATE_DIR / "working_memory.json"
        working_memory.WM_ARCHIVE = working_memory.STATE_DIR / "working_memory_archive.json"

    def tearDown(self):
        working_memory.STATE_DIR = self._orig_state
        working_memory.WM_FILE = self._orig_file
        working_memory.WM_ARCHIVE = self._orig_archive
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_format_for_context_empty(self):
        store = working_memory.WorkingMemoryStore()
        self.assertEqual(store.format_for_context("123"), "")

    def test_format_for_context_with_tasks(self):
        store = working_memory.WorkingMemoryStore()
        task = working_memory.ActiveTask(
            task_stem="0001_billing",
            chat_id="123",
            original_message="Build a billing system with multi-provider support",
            intent_summary="Build billing system",
            created_at=time.time() - 300,  # 5 min ago
            status="pending",
            context_snapshot=[],
        )
        store.add(task)
        ctx = store.format_for_context("123")
        self.assertIn("ACTIVE BACKGROUND TASKS", ctx)
        self.assertIn("Build billing system", ctx)
        self.assertIn("Build a billing system", ctx)
        self.assertIn("pending", ctx)

    def test_format_for_context_chat_isolation(self):
        store = working_memory.WorkingMemoryStore()
        store.add(
            working_memory.ActiveTask(
                task_stem="0001_a",
                chat_id="c1",
                original_message="task for c1",
                intent_summary="c1 task",
                created_at=time.time(),
                status="pending",
                context_snapshot=[],
            )
        )
        store.add(
            working_memory.ActiveTask(
                task_stem="0002_b",
                chat_id="c2",
                original_message="task for c2",
                intent_summary="c2 task",
                created_at=time.time(),
                status="pending",
                context_snapshot=[],
            )
        )
        ctx_c1 = store.format_for_context("c1")
        self.assertIn("c1 task", ctx_c1)
        self.assertNotIn("c2 task", ctx_c1)

    def test_format_completion_context(self):
        task = working_memory.ActiveTask(
            task_stem="0001_billing",
            chat_id="123",
            original_message="Build a billing system",
            intent_summary="Build billing system",
            created_at=1000.0,
            status="completed",
            context_snapshot=[
                {"role": "user", "content": "I need billing"},
                {"role": "assistant", "content": "Got it, queuing now"},
            ],
        )
        ctx = working_memory.WorkingMemoryStore().format_completion_context(task)
        self.assertIn("ORIGINAL USER REQUEST", ctx)
        self.assertIn("Build a billing system", ctx)
        self.assertIn("GOAL: Build billing system", ctx)
        self.assertIn("CONVERSATION CONTEXT", ctx)
        self.assertIn("I need billing", ctx)


# ── Phase 8 Intent Classification Edge Cases ──────────────────────────────


class TestDeliverableDetector(unittest.TestCase):
    """Phase 8: deliverable detector overrides chat signals for concrete tasks."""

    def _classify(self, text):
        return parse.classify_intent(text)

    # -- Request verbs override chat signals --

    def test_can_you_build_is_task(self):
        self.assertEqual(self._classify("Can you build a billing system?"), "task")

    def test_please_deploy_is_task(self):
        self.assertEqual(self._classify("Please deploy the new version"), "task")

    def test_i_need_you_to_implement_is_task(self):
        self.assertEqual(self._classify("I need you to implement caching"), "task")

    def test_could_you_fix_is_task(self):
        self.assertEqual(self._classify("Could you fix the memory leak?"), "task")

    def test_i_want_you_to_research_is_task(self):
        self.assertEqual(self._classify("I want you to research database options"), "task")

    def test_go_ahead_and_create_is_task(self):
        self.assertEqual(self._classify("Go ahead and create the API endpoint"), "task")

    # -- Deliverable detector with chat signals --

    def test_should_we_build_billing_is_task(self):
        """Chat signal + action verb + concrete object → task."""
        self.assertEqual(self._classify("Should we build a billing system?"), "task")

    def test_sounds_good_research_databases_is_task(self):
        self.assertEqual(self._classify("Sounds good, research databases"), "task")

    # -- Chat signals without deliverables stay chat --

    def test_should_we_refactor_is_chat(self):
        """Chat signal + action verb + no concrete object → chat."""
        self.assertEqual(self._classify("Should we refactor?"), "chat")

    def test_can_you_explain_is_chat(self):
        self.assertEqual(self._classify("Can you explain how it works?"), "chat")

    def test_what_do_you_think_about_building_is_chat(self):
        """'what do you think' is a chat signal; no deliverable detector match."""
        result = self._classify("What do you think about building?")
        # This should be chat because "building" alone isn't a deliverable
        self.assertEqual(result, "chat")

    # -- Pronoun exclusion --

    def test_fix_it_with_chat_signal_is_chat(self):
        """Chat signal + 'fix it' (pronoun) → chat (deliverable detector excludes pronouns)."""
        self.assertEqual(self._classify("Hey, fix it"), "chat")

    def test_fix_it_bare_is_task(self):
        """Bare 'fix it' → task (action verb, no chat signal to override)."""
        self.assertEqual(self._classify("fix it"), "task")

    def test_fix_the_bug_is_task(self):
        """'fix the bug' has a concrete object → task."""
        self.assertEqual(self._classify("fix the bug"), "task")


# ── Persona: No Promise Without Queue ─────────────────────────────────────


class TestNoPromiseWithoutQueue(unittest.TestCase):
    """Verify the persona prompt enforces executive voice work request handling.

    Phase 9 replaced the old DELEGATION BOUNDARY with HANDLING WORK REQUESTS.
    """

    def test_handling_work_requests_in_prompt(self):
        self.assertIn("HANDLING WORK REQUESTS", persona.SYSTEM_PROMPT)

    def test_never_expose_internals(self):
        lower = persona.SYSTEM_PROMPT.lower()
        self.assertIn("invisible", lower)

    def test_run_command_suggestion(self):
        self.assertIn("/run", persona.SYSTEM_PROMPT)

    def test_concrete_next_step_guidance(self):
        lower = persona.SYSTEM_PROMPT.lower()
        self.assertIn("queue that up", lower)


if __name__ == "__main__":
    unittest.main()
