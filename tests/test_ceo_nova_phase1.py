"""Tests for CEO Nova Phase 1: Fast Conversational Core.

Tests conversation buffer, parse routing, LLM formatting,
and persona module.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
import unittest
from unittest.mock import patch, AsyncMock

# Load local telegram modules via importlib (same pattern as telegram_bot.py)
_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _mod_name in ("parse", "conversation", "llm", "persona", "format", "delegation", "goals", "hardening"):
    _path = os.path.join(_here, "telegram", f"{_mod_name}.py")
    if os.path.exists(_path):
        _spec = importlib.util.spec_from_file_location(_mod_name, _path)
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        sys.modules[f"tg_{_mod_name}"] = _mod

parse = sys.modules["tg_parse"]
conversation = sys.modules["tg_conversation"]
llm = sys.modules["tg_llm"]
persona = sys.modules["tg_persona"]
delegation = sys.modules["tg_delegation"]
goals = sys.modules["tg_goals"]
hardening = sys.modules["tg_hardening"]


# ── Persona Tests ──────────────────────────────────────────────────────────


class TestPersona(unittest.TestCase):
    def test_system_prompt_exists(self):
        self.assertIsInstance(persona.SYSTEM_PROMPT, str)
        self.assertGreater(len(persona.SYSTEM_PROMPT), 100)

    def test_system_prompt_contains_identity(self):
        self.assertIn("Nova", persona.SYSTEM_PROMPT)

    def test_system_prompt_contains_personality_guidance(self):
        self.assertIn("warm", persona.SYSTEM_PROMPT.lower())
        self.assertIn("concise", persona.SYSTEM_PROMPT.lower())

    def test_delegation_ack_prompt_exists(self):
        self.assertIsInstance(persona.DELEGATION_ACK_PROMPT, str)
        self.assertGreater(len(persona.DELEGATION_ACK_PROMPT), 50)

    def test_no_emoji_guidance(self):
        self.assertIn("emoji", persona.SYSTEM_PROMPT.lower())


# ── Conversation Buffer Tests ──────────────────────────────────────────────


class TestConversationBuffer(unittest.TestCase):
    def setUp(self):
        self.buf = conversation.ConversationBuffer()

    def test_add_and_get(self):
        self.buf.add("user", "hello")
        self.buf.add("assistant", "hi")
        history = self.buf.get_history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0], {"role": "user", "content": "hello"})
        self.assertEqual(history[1], {"role": "assistant", "content": "hi"})

    def test_max_messages_eviction(self):
        for i in range(25):
            self.buf.add("user", f"msg {i}")
        history = self.buf.get_history()
        self.assertEqual(len(history), conversation.MAX_MESSAGES)
        # Should keep the most recent
        self.assertEqual(history[-1]["content"], "msg 24")
        self.assertEqual(history[0]["content"], "msg 5")

    def test_age_eviction(self):
        old_time = time.time() - conversation.MAX_AGE_SECONDS - 10
        self.buf.messages.append(
            conversation.Message(role="user", content="old", timestamp=old_time)
        )
        self.buf.add("user", "new")
        history = self.buf.get_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["content"], "new")

    def test_session_start_empty(self):
        self.assertTrue(self.buf.is_session_start())

    def test_session_start_recent(self):
        self.buf.add("user", "hello")
        self.assertFalse(self.buf.is_session_start())

    def test_clear(self):
        self.buf.add("user", "hello")
        self.buf.clear()
        self.assertEqual(len(self.buf.get_history()), 0)
        self.assertTrue(self.buf.is_session_start())


class TestConversationManager(unittest.TestCase):
    def setUp(self):
        self.mgr = conversation.ConversationManager(persist=False)

    def test_separate_chats(self):
        self.mgr.add_user_message("chat1", "hello from chat1")
        self.mgr.add_user_message("chat2", "hello from chat2")
        h1 = self.mgr.get_history("chat1")
        h2 = self.mgr.get_history("chat2")
        self.assertEqual(len(h1), 1)
        self.assertEqual(len(h2), 1)
        self.assertEqual(h1[0]["content"], "hello from chat1")
        self.assertEqual(h2[0]["content"], "hello from chat2")

    def test_user_and_assistant_messages(self):
        self.mgr.add_user_message("c", "hi")
        self.mgr.add_assistant_message("c", "hello!")
        h = self.mgr.get_history("c")
        self.assertEqual(h[0]["role"], "user")
        self.assertEqual(h[1]["role"], "assistant")

    def test_session_start_new_chat(self):
        self.assertTrue(self.mgr.is_session_start("unknown_chat"))

    def test_session_start_active_chat(self):
        self.mgr.add_user_message("c", "hi")
        self.assertFalse(self.mgr.is_session_start("c"))


# ── Parse Routing Tests ────────────────────────────────────────────────────


class TestParseRouting(unittest.TestCase):
    """Verify that parse_message routes correctly between conversation and task."""

    def _parse(self, text):
        return parse.parse_message(text, "123", 1.0)

    # -- Conversation path --

    def test_plain_greeting_routes_to_conversation(self):
        r = self._parse("hello")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "conversation")
        self.assertEqual(r["action"]["text"], "hello")

    def test_plain_question_routes_to_conversation(self):
        r = self._parse("how are you doing today?")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "conversation")

    def test_chat_command_routes_to_conversation(self):
        r = self._parse("/chat what's up?")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "conversation")
        self.assertEqual(r["action"]["text"], "what's up?")

    def test_chat_command_no_text_errors(self):
        r = self._parse("/chat")
        self.assertFalse(r["ok"])

    def test_chat_command_whitespace_only_errors(self):
        r = self._parse("/chat   ")
        self.assertFalse(r["ok"])

    # -- Task path --

    def test_task_keywords_route_to_task(self):
        r = self._parse("show me a detailed report on the system")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "run_task")

    def test_report_command_routes_to_task(self):
        r = self._parse("/report analyze the codebase")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "run_task")

    def test_run_command_routes_to_task(self):
        r = self._parse("/run build feature X")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "run_task")

    def test_debug_keyword_routes_to_task(self):
        r = self._parse("debug the memory system")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "run_task")

    def test_audit_keyword_routes_to_task(self):
        r = self._parse("audit the rollout gate")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "run_task")

    # -- Existing commands unchanged --

    def test_status_command_unchanged(self):
        r = self._parse("/status")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "get_status")

    def test_last_command_unchanged(self):
        r = self._parse("/last")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "get_last")

    def test_help_command_unchanged(self):
        r = self._parse("/help")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "show_help")

    def test_get_command_unchanged(self):
        r = self._parse("/get somefile.md")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "get_output")

    def test_cancel_command_unchanged(self):
        r = self._parse("/cancel last")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "cancel_task")

    def test_mode_command_unchanged(self):
        r = self._parse("/mode compact")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "set_mode")

    def test_tail_command_unchanged(self):
        r = self._parse("/tail 0001")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "tail_log")

    # -- Edge cases --

    def test_empty_message_routes_to_conversation(self):
        # Empty after strip is caught by classify_intent → "chat"
        r = self._parse("   ")
        # parse_run will fail on empty text
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "conversation")

    def test_long_message_error(self):
        r = self._parse("x" * 5000)
        self.assertFalse(r["ok"])

    def test_conversation_action_has_text_field(self):
        r = self._parse("tell me about our goals")
        self.assertTrue(r["ok"])
        self.assertIn("text", r["action"])

    def test_conversation_action_has_chat_id(self):
        r = parse.parse_message("hello", "456", 1.0)
        self.assertEqual(r["action"]["chat_id"], "456")


# ── LLM Helper Tests ──────────────────────────────────────────────────────


class TestLLMHelpers(unittest.TestCase):
    def test_format_empty_history(self):
        result = llm.format_history_for_prompt([])
        self.assertEqual(result, "")

    def test_format_single_message(self):
        result = llm.format_history_for_prompt([
            {"role": "user", "content": "hello"}
        ])
        self.assertEqual(result, "Human: hello")

    def test_format_conversation(self):
        result = llm.format_history_for_prompt([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "how are you"},
        ])
        lines = result.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "Human: hello")
        self.assertEqual(lines[1], "You: hi there")
        self.assertEqual(lines[2], "Human: how are you")

    def test_constants(self):
        self.assertGreater(llm.CONVERSATION_TIMEOUT, 0)
        self.assertEqual(llm.MODEL, "claude-opus-4-6")


# ── Integration: Parse → Conversation Buffer ──────────────────────────────


class TestIntegration(unittest.TestCase):
    def test_conversation_flow(self):
        """Simulate a multi-turn conversation through parse + buffer."""
        mgr = conversation.ConversationManager(persist=False)

        # User sends greeting
        r = parse.parse_message("hey there", "c1", 1.0)
        self.assertEqual(r["action"]["action"], "conversation")
        mgr.add_user_message("c1", r["action"]["text"])
        mgr.add_assistant_message("c1", "Hey! How's it going?")

        # User asks a simple question
        r = parse.parse_message("what did we work on?", "c1", 2.0)
        self.assertEqual(r["action"]["action"], "conversation")
        mgr.add_user_message("c1", r["action"]["text"])

        # History should have 3 messages
        h = mgr.get_history("c1")
        self.assertEqual(len(h), 3)

    def test_delegation_flow(self):
        """Task-intent messages should still route to run_task."""
        r = parse.parse_message("/run refactor the heartbeat module", "c1", 1.0)
        self.assertEqual(r["action"]["action"], "run_task")
        self.assertEqual(r["action"]["title"], "refactor the heartbeat module")


# ── Phase 2: Memory-Aware Conversation Tests ─────────────────────────────


class TestSessionStartHint(unittest.TestCase):
    """Verify session-start detection and memory hint injection."""

    def test_session_start_hint_exists(self):
        self.assertIsInstance(persona.SESSION_START_HINT, str)
        self.assertGreater(len(persona.SESSION_START_HINT), 50)

    def test_session_start_hint_references_checkpoint(self):
        self.assertIn("get_last_checkpoint", persona.SESSION_START_HINT)

    def test_session_start_hint_references_project(self):
        self.assertIn("nova-core", persona.SESSION_START_HINT)


class TestMemoryInstructions(unittest.TestCase):
    """Verify memory instructions are in the system prompt."""

    def test_memory_section_exists(self):
        self.assertIn("MEMORY:", persona.SYSTEM_PROMPT)

    def test_query_memory_mentioned(self):
        self.assertIn("query_memory", persona.SYSTEM_PROMPT)

    def test_upsert_memory_mentioned(self):
        self.assertIn("upsert_memory", persona.SYSTEM_PROMPT)

    def test_vault_access_mentioned(self):
        self.assertIn("nova-vault", persona.SYSTEM_PROMPT)

    def test_no_write_tools_guidance(self):
        """Ensure the prompt tells Claude not to use write/edit/bash tools."""
        lower = persona.SYSTEM_PROMPT.lower()
        self.assertIn("do not use write, edit, or bash", lower)


class TestSessionStartIntegration(unittest.TestCase):
    """Verify that session start detection works with conversation manager."""

    def test_new_chat_is_session_start(self):
        mgr = conversation.ConversationManager(persist=False)
        self.assertTrue(mgr.is_session_start("brand_new_chat"))

    def test_active_chat_not_session_start(self):
        mgr = conversation.ConversationManager(persist=False)
        mgr.add_user_message("c1", "hello")
        self.assertFalse(mgr.is_session_start("c1"))

    def test_session_start_only_on_first_message(self):
        """Session start should be True only before the first message."""
        mgr = conversation.ConversationManager(persist=False)
        # First time — session start
        self.assertTrue(mgr.is_session_start("c1"))
        # Add a message
        mgr.add_user_message("c1", "hello")
        # Now it's not a session start
        self.assertFalse(mgr.is_session_start("c1"))


# ── Phase 3: Delegation Tracker Tests ─────────────────────────────────────


class TestDelegationTracker(unittest.TestCase):
    def setUp(self):
        self.tracker = delegation.DelegationTracker()

    def test_track_and_retrieve(self):
        self.tracker.track("0042_refactor_heartbeat", "12345")
        self.assertEqual(
            self.tracker.get_chat_id("0042_refactor_heartbeat"), "12345"
        )

    def test_complete_returns_chat_id(self):
        self.tracker.track("0042_foo", "12345")
        chat_id = self.tracker.complete("0042_foo")
        self.assertEqual(chat_id, "12345")
        # Second call returns None (already completed)
        self.assertIsNone(self.tracker.complete("0042_foo"))

    def test_pending_stems(self):
        self.tracker.track("0001_a", "c1")
        self.tracker.track("0002_b", "c2")
        stems = self.tracker.pending_stems()
        self.assertEqual(set(stems), {"0001_a", "0002_b"})

    def test_has_pending(self):
        self.assertFalse(self.tracker.has_pending())
        self.tracker.track("0001_a", "c1")
        self.assertTrue(self.tracker.has_pending())
        self.tracker.complete("0001_a")
        self.assertFalse(self.tracker.has_pending())

    def test_unknown_stem_returns_none(self):
        self.assertIsNone(self.tracker.get_chat_id("nonexistent"))
        self.assertIsNone(self.tracker.complete("nonexistent"))


class TestFindCompletedOutput(unittest.TestCase):
    def setUp(self):
        self.tmpdir = os.path.join(os.path.dirname(__file__), "_test_output")
        os.makedirs(self.tmpdir, exist_ok=True)
        # Patch OUTPUT path for testing
        self._orig_output = delegation.OUTPUT
        delegation.OUTPUT = type(delegation.OUTPUT)(self.tmpdir)

    def tearDown(self):
        delegation.OUTPUT = self._orig_output
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_finds_matching_output(self):
        # Create a fake output file
        stem = "0042_refactor_heartbeat"
        fname = f"{stem}__20260309-020000.md"
        (delegation.OUTPUT / fname).write_text("# Output", encoding="utf-8")
        result = delegation.find_completed_output(stem)
        self.assertIsNotNone(result)
        self.assertEqual(result.name, fname)

    def test_returns_none_when_no_match(self):
        result = delegation.find_completed_output("0099_nonexistent")
        self.assertIsNone(result)


class TestCompletionSummaryPrompt(unittest.TestCase):
    def test_prompt_exists(self):
        self.assertIsInstance(delegation.COMPLETION_SUMMARY_PROMPT, str)
        self.assertGreater(len(delegation.COMPLETION_SUMMARY_PROMPT), 50)

    def test_prompt_forbids_metadata(self):
        lower = delegation.COMPLETION_SUMMARY_PROMPT.lower()
        self.assertIn("do not include task ids", lower)


# ── Phase 4: Tool Integration Tests ──────────────────────────────────────


class TestToolInstructions(unittest.TestCase):
    """Verify tool integration instructions in system prompt."""

    def test_tools_section_exists(self):
        self.assertIn("TOOLS:", persona.SYSTEM_PROMPT)

    def test_web_search_mentioned(self):
        lower = persona.SYSTEM_PROMPT.lower()
        self.assertIn("brave search", lower)
        self.assertIn("tavily", lower)

    def test_system_status_guidance(self):
        self.assertIn("HEARTBEAT.md", persona.SYSTEM_PROMPT)

    def test_tool_restrictions_section(self):
        self.assertIn("TOOL RESTRICTIONS:", persona.SYSTEM_PROMPT)

    def test_no_write_tools(self):
        lower = persona.SYSTEM_PROMPT.lower()
        self.assertIn("do not use write", lower)

    def test_read_allowed(self):
        lower = persona.SYSTEM_PROMPT.lower()
        self.assertIn("you may use read", lower)

    def test_web_search_allowed(self):
        lower = persona.SYSTEM_PROMPT.lower()
        self.assertIn("you may use web search", lower)

    def test_delegation_threshold(self):
        """Prompt should guide when to delegate vs use tools directly."""
        self.assertIn("delegation candidate", persona.SYSTEM_PROMPT)


# ── Phase 5: Smart Intent Classification Tests ───────────────────────────


class TestExpandedIntentClassification(unittest.TestCase):
    """Verify that action verbs route to task and chat signals stay in chat."""

    def _parse(self, text):
        return parse.parse_message(text, "123", 1.0)

    # -- Action verbs → task --

    def test_refactor_routes_to_task(self):
        r = self._parse("refactor the heartbeat module")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_implement_routes_to_task(self):
        r = self._parse("implement the new API endpoint")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_build_routes_to_task(self):
        r = self._parse("build a PDF export feature")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_fix_routes_to_task(self):
        r = self._parse("fix the memory leak in the watcher")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_research_routes_to_task(self):
        r = self._parse("research the best approach for caching")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_deploy_routes_to_task(self):
        r = self._parse("deploy the new telegram bot version")
        self.assertEqual(r["action"]["action"], "run_task")

    # -- Chat signals override action verbs --

    def test_should_we_refactor_is_chat(self):
        r = self._parse("should we refactor the heartbeat?")
        self.assertEqual(r["action"]["action"], "conversation")

    def test_can_you_explain_is_chat(self):
        r = self._parse("can you explain how the watcher works?")
        self.assertEqual(r["action"]["action"], "conversation")

    def test_what_do_you_think_is_chat(self):
        r = self._parse("what do you think about implementing caching?")
        self.assertEqual(r["action"]["action"], "conversation")

    def test_tell_me_about_is_chat(self):
        r = self._parse("tell me about our deployment process")
        self.assertEqual(r["action"]["action"], "conversation")

    # -- Pure chat stays chat --

    def test_greeting_stays_chat(self):
        r = self._parse("hey how's it going")
        self.assertEqual(r["action"]["action"], "conversation")

    def test_thanks_stays_chat(self):
        r = self._parse("thanks that looks great")
        self.assertEqual(r["action"]["action"], "conversation")

    def test_opinion_stays_chat(self):
        r = self._parse("how are we doing on the project?")
        self.assertEqual(r["action"]["action"], "conversation")


class TestRecentCompletions(unittest.TestCase):
    """Test the recent completions context injection helper."""

    def setUp(self):
        self.tmpdir = os.path.join(os.path.dirname(__file__), "_test_output2")
        os.makedirs(self.tmpdir, exist_ok=True)
        self._orig_output = delegation.OUTPUT
        delegation.OUTPUT = type(delegation.OUTPUT)(self.tmpdir)

    def tearDown(self):
        delegation.OUTPUT = self._orig_output
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_recent_outputs(self):
        # Create a fresh output file
        fname = "0042_test__20260309-020000.md"
        (delegation.OUTPUT / fname).write_text(
            "# Report\n**Task:** test\nThis is the result.", encoding="utf-8"
        )
        results = delegation.get_recent_completions(max_age_seconds=3600)
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["stem"], "0042_test")

    def test_returns_empty_when_no_outputs(self):
        results = delegation.get_recent_completions(max_age_seconds=3600)
        self.assertEqual(results, [])

    def test_respects_limit(self):
        for i in range(5):
            fname = f"00{i:02d}_test_{i}__20260309-02000{i}.md"
            (delegation.OUTPUT / fname).write_text(
                f"# Report\nResult {i}", encoding="utf-8"
            )
        results = delegation.get_recent_completions(max_age_seconds=3600, limit=2)
        self.assertEqual(len(results), 2)


# ── Phase 6: Goal Tracking & Briefing Tests ──────────────────────────────


class TestGoalStore(unittest.TestCase):
    def setUp(self):
        self._orig_file = goals.GOALS_FILE
        self.tmpdir = os.path.join(os.path.dirname(__file__), "_test_goals")
        os.makedirs(self.tmpdir, exist_ok=True)
        goals.GOALS_FILE = type(goals.GOALS_FILE)(
            os.path.join(self.tmpdir, "goals.json")
        )

    def tearDown(self):
        goals.GOALS_FILE = self._orig_file
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_goal(self):
        g = goals.add_goal("Ship the API")
        self.assertEqual(g["id"], 1)
        self.assertEqual(g["text"], "Ship the API")
        self.assertEqual(g["status"], "active")

    def test_list_goals(self):
        goals.add_goal("Goal A")
        goals.add_goal("Goal B")
        active = goals.list_goals()
        self.assertEqual(len(active), 2)

    def test_complete_goal(self):
        g = goals.add_goal("Test goal")
        result = goals.complete_goal(g["id"])
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "completed")
        # Should no longer appear in active list
        active = goals.list_goals()
        self.assertEqual(len(active), 0)

    def test_complete_nonexistent(self):
        result = goals.complete_goal(999)
        self.assertIsNone(result)

    def test_remove_goal(self):
        g = goals.add_goal("Remove me")
        result = goals.remove_goal(g["id"])
        self.assertIsNotNone(result)
        self.assertEqual(goals.list_goals(include_completed=True), [])

    def test_clear_completed(self):
        g1 = goals.add_goal("Done goal")
        goals.add_goal("Active goal")
        goals.complete_goal(g1["id"])
        count = goals.clear_completed()
        self.assertEqual(count, 1)
        all_goals = goals.list_goals(include_completed=True)
        self.assertEqual(len(all_goals), 1)
        self.assertEqual(all_goals[0]["text"], "Active goal")

    def test_format_for_context_empty(self):
        ctx = goals.format_goals_for_context()
        self.assertEqual(ctx, "")

    def test_format_for_context_with_goals(self):
        goals.add_goal("Ship the API")
        ctx = goals.format_goals_for_context()
        self.assertIn("Ship the API", ctx)
        self.assertIn("ACTIVE GOALS", ctx)

    def test_format_for_display(self):
        goals.add_goal("Goal X")
        display = goals.format_goals_for_display()
        self.assertIn("Goal X", display)
        self.assertIn("#1", display)

    def test_auto_increment_id(self):
        g1 = goals.add_goal("First")
        g2 = goals.add_goal("Second")
        self.assertEqual(g1["id"], 1)
        self.assertEqual(g2["id"], 2)


class TestGoalsCommandParsing(unittest.TestCase):
    def _parse(self, text):
        return parse.parse_message(text, "123", 1.0)

    def test_goals_list(self):
        r = self._parse("/goals")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "goals")
        self.assertEqual(r["action"]["subcommand"], "list")

    def test_goals_add(self):
        r = self._parse("/goals add Ship the API this week")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["subcommand"], "add")
        self.assertEqual(r["action"]["text"], "Ship the API this week")

    def test_goals_add_no_text_errors(self):
        r = self._parse("/goals add")
        self.assertFalse(r["ok"])

    def test_goals_done(self):
        r = self._parse("/goals done 3")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["subcommand"], "done")
        self.assertEqual(r["action"]["goal_id"], 3)

    def test_goals_done_no_id_errors(self):
        r = self._parse("/goals done")
        self.assertFalse(r["ok"])

    def test_goals_remove(self):
        r = self._parse("/goals remove 5")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["subcommand"], "remove")
        self.assertEqual(r["action"]["goal_id"], 5)

    def test_goals_clear(self):
        r = self._parse("/goals clear")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["subcommand"], "clear")

    def test_goals_unknown_subcommand(self):
        r = self._parse("/goals foo")
        self.assertFalse(r["ok"])

    def test_briefing_command(self):
        r = self._parse("/briefing")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["action"], "briefing")


# ── Phase 7: Production Hardening Tests ──────────────────────────────────


class TestRateLimiter(unittest.TestCase):
    def test_allows_within_limit(self):
        rl = hardening.RateLimiter(per_chat_limit=3, per_chat_window=60,
                                   global_limit=10, global_window=60)
        for _ in range(3):
            allowed, _ = rl.check("c1")
            self.assertTrue(allowed)
            rl.record("c1")

    def test_blocks_over_per_chat_limit(self):
        rl = hardening.RateLimiter(per_chat_limit=2, per_chat_window=60,
                                   global_limit=10, global_window=60)
        rl.record("c1")
        rl.record("c1")
        allowed, reason = rl.check("c1")
        self.assertFalse(allowed)
        self.assertEqual(reason, "per_chat")

    def test_blocks_over_global_limit(self):
        rl = hardening.RateLimiter(per_chat_limit=10, per_chat_window=60,
                                   global_limit=2, global_window=60)
        rl.record("c1")
        rl.record("c2")
        allowed, reason = rl.check("c3")
        self.assertFalse(allowed)
        self.assertEqual(reason, "global")

    def test_separate_chats_have_own_limits(self):
        rl = hardening.RateLimiter(per_chat_limit=2, per_chat_window=60,
                                   global_limit=10, global_window=60)
        rl.record("c1")
        rl.record("c1")
        allowed, _ = rl.check("c2")  # different chat
        self.assertTrue(allowed)


class TestCircuitBreaker(unittest.TestCase):
    def test_closed_by_default(self):
        cb = hardening.CircuitBreaker(failure_threshold=3)
        self.assertFalse(cb.is_open())

    def test_opens_after_threshold(self):
        cb = hardening.CircuitBreaker(failure_threshold=3, cooldown_seconds=300)
        for _ in range(3):
            cb.record_failure()
        self.assertTrue(cb.is_open())

    def test_resets_on_success(self):
        cb = hardening.CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        self.assertEqual(cb.failure_count, 0)
        self.assertFalse(cb.is_open())

    def test_recovers_after_cooldown(self):
        cb = hardening.CircuitBreaker(failure_threshold=2, cooldown_seconds=0)
        cb.record_failure()
        cb.record_failure()
        # cooldown=0 means immediate recovery
        self.assertFalse(cb.is_open())


class TestMetricsCollector(unittest.TestCase):
    def test_records_messages(self):
        m = hardening.MetricsCollector()
        m.record_message()
        m.record_message()
        snap = m.snapshot()
        self.assertEqual(snap["total_messages"], 2)

    def test_avg_response_time(self):
        m = hardening.MetricsCollector()
        m.record_response_time(100)
        m.record_response_time(200)
        self.assertAlmostEqual(m.avg_response_time_ms, 150.0)

    def test_snapshot_has_uptime(self):
        m = hardening.MetricsCollector()
        snap = m.snapshot()
        self.assertIn("uptime_seconds", snap)


class TestResponseCache(unittest.TestCase):
    def test_cache_hit(self):
        c = hardening.ResponseCache(max_size=10, ttl_seconds=300)
        c.put("hello", "world")
        self.assertEqual(c.get("hello"), "world")

    def test_cache_miss(self):
        c = hardening.ResponseCache(max_size=10, ttl_seconds=300)
        self.assertIsNone(c.get("nonexistent"))

    def test_cache_eviction(self):
        c = hardening.ResponseCache(max_size=2, ttl_seconds=300)
        c.put("a", "1")
        c.put("b", "2")
        c.put("c", "3")  # should evict oldest
        self.assertEqual(c.size, 2)

    def test_cache_ttl_expiry(self):
        c = hardening.ResponseCache(max_size=10, ttl_seconds=0)
        c.put("a", "1")
        # TTL=0 means immediate expiry
        self.assertIsNone(c.get("a"))


class TestConversationPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = os.path.join(os.path.dirname(__file__), "_test_convos")
        os.makedirs(self.tmpdir, exist_ok=True)
        self._orig_dir = conversation.PERSIST_DIR
        conversation.PERSIST_DIR = type(conversation.PERSIST_DIR)(self.tmpdir)

    def tearDown(self):
        conversation.PERSIST_DIR = self._orig_dir
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_persist_and_restore(self):
        # Create a manager, add messages, which persists to disk
        mgr1 = conversation.ConversationManager(persist=True)
        mgr1.add_user_message("c1", "hello")
        mgr1.add_assistant_message("c1", "hi there")

        # Create a new manager that loads from disk
        mgr2 = conversation.ConversationManager(persist=True)
        h = mgr2.get_history("c1")
        self.assertEqual(len(h), 2)
        self.assertEqual(h[0]["content"], "hello")
        self.assertEqual(h[1]["content"], "hi there")

    def test_no_persist_mode(self):
        mgr = conversation.ConversationManager(persist=False)
        mgr.add_user_message("c1", "test")
        # Should not create files
        import glob
        files = glob.glob(os.path.join(self.tmpdir, "*.json"))
        self.assertEqual(len(files), 0)


if __name__ == "__main__":
    unittest.main()
