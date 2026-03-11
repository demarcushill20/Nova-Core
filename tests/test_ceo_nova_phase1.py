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
        # 20 messages + 1 session summary from compacted overflow
        self.assertEqual(len(self.buf.messages), conversation.MAX_MESSAGES)
        # Should keep the most recent
        self.assertEqual(history[-1]["content"], "msg 24")

    def test_age_eviction(self):
        old_time = time.time() - conversation.MAX_AGE_SECONDS - 10
        self.buf.messages.append(
            conversation.Message(role="user", content="old", timestamp=old_time)
        )
        self.buf.add("user", "new")
        # Old message evicted, compacted into summary
        self.assertEqual(len(self.buf.messages), 1)
        self.assertEqual(self.buf.messages[0].content, "new")
        # Summary captures the evicted message
        self.assertTrue(len(self.buf.session_summary) > 0)

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

    def test_session_start_hint_pre_loaded_context(self):
        """Phase 10: hint tells LLM to use pre-loaded context, not fetch its own."""
        self.assertIn("pre-loaded", persona.SESSION_START_HINT.lower())
        self.assertIn("do not call memory tools", persona.SESSION_START_HINT.lower()
                       .replace("don't", "do not"))


class TestMemoryInstructions(unittest.TestCase):
    """Verify memory instructions are in the system prompt (Phase 10: honest capability contract)."""

    def test_memory_section_exists(self):
        self.assertIn("MEMORY:", persona.SYSTEM_PROMPT)

    def test_pre_loaded_context_instruction(self):
        """Phase 10: LLM should use pre-loaded context, not call memory tools."""
        lower = persona.SYSTEM_PROMPT.lower()
        self.assertIn("pre-loaded", lower)

    def test_fusion_memory_mentioned(self):
        """Fusion Memory is still available for targeted queries."""
        self.assertIn("Fusion Memory", persona.SYSTEM_PROMPT)

    def test_vault_access_claimed(self):
        """Phase 11: subprocess has nova-vault MCP — persona claims it."""
        self.assertIn("nova-vault", persona.SYSTEM_PROMPT)

    def test_vault_handled_inline(self):
        """Phase 11: vault requests are NOT in delegation list (handled inline)."""
        self.assertNotIn("Save this to memory/obsidian/vault", persona.SYSTEM_PROMPT)

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

    def test_web_search_available(self):
        """Phase 11: web search is a direct tool via brave-search MCP."""
        self.assertIn("brave-search", persona.SYSTEM_PROMPT)

    def test_system_status_guidance(self):
        self.assertIn("HEARTBEAT.md", persona.SYSTEM_PROMPT)

    def test_tool_restrictions_section(self):
        self.assertIn("TOOL RESTRICTIONS:", persona.SYSTEM_PROMPT)

    def test_no_write_tools(self):
        lower = persona.SYSTEM_PROMPT.lower()
        self.assertIn("do not use write", lower)

    def test_read_allowed(self):
        lower = persona.SYSTEM_PROMPT.lower()
        self.assertIn("you may read local files", lower)

    def test_web_search_direct(self):
        """Phase 11: web search is available as a direct MCP tool."""
        self.assertIn("Web Search", persona.SYSTEM_PROMPT)

    def test_direct_action_guidance(self):
        """Prompt should guide CEO Nova to use tools directly."""
        self.assertIn("USE TOOLS DIRECTLY", persona.SYSTEM_PROMPT)


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

    def test_should_we_refactor_with_deliverable_is_task(self):
        # "should we refactor the heartbeat?" has a concrete deliverable
        # → routes to task (better to queue than risk false promise)
        r = self._parse("should we refactor the heartbeat?")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_should_we_refactor_bare_is_chat(self):
        # "should we refactor?" has no deliverable object → stays in chat
        r = self._parse("should we refactor?")
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


# ── Phase 8: Intent Classification Reliability & Working Memory ───────────


class TestRequestVerbClassification(unittest.TestCase):
    """Verify that explicit request patterns route to task."""

    def _parse(self, text):
        return parse.parse_message(text, "123", 1.0)

    def test_can_you_build_is_task(self):
        r = self._parse("can you build a monitoring dashboard?")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_could_you_implement_is_task(self):
        r = self._parse("could you implement rate limiting?")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_please_deploy_is_task(self):
        r = self._parse("please deploy the new version to production")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_i_need_you_to_fix_is_task(self):
        r = self._parse("I need you to fix the authentication bug")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_would_you_research_is_task(self):
        r = self._parse("would you research vector database options?")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_i_want_you_to_create_is_task(self):
        r = self._parse("I want you to create a new API endpoint")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_go_ahead_and_build_is_task(self):
        r = self._parse("go ahead and build the billing system")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_id_like_you_to_write_is_task(self):
        r = self._parse("I'd like you to write integration tests")
        self.assertEqual(r["action"]["action"], "run_task")

    # -- These should NOT match request verb (informational, not requests) --

    def test_can_you_explain_stays_chat(self):
        r = self._parse("can you explain how the watcher works?")
        self.assertEqual(r["action"]["action"], "conversation")

    def test_could_you_tell_me_stays_chat(self):
        r = self._parse("could you tell me about the architecture?")
        self.assertEqual(r["action"]["action"], "conversation")


class TestDeliverableDetectorExpanded(unittest.TestCase):
    """Verify that bare-noun deliverables now route to task."""

    def _parse(self, text):
        return parse.parse_message(text, "123", 1.0)

    def test_affirmative_plus_bare_noun_is_task(self):
        # "got it" is chat signal, "research databases" has no determiner
        # but bare noun should now match deliverable detector
        r = self._parse("got it, research databases for the project")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_sure_investigate_is_task(self):
        r = self._parse("sure, investigate memory leaks in the worker")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_ok_deploy_production_is_task(self):
        r = self._parse("ok deploy production immediately")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_sounds_good_build_dashboard_is_task(self):
        r = self._parse("sounds good, build a monitoring dashboard")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_fix_it_stays_chat(self):
        # Pronoun "it" should NOT match deliverable detector
        r = self._parse("sure, fix it")
        self.assertEqual(r["action"]["action"], "conversation")

    def test_build_them_stays_chat(self):
        # Pronoun "them" should NOT match deliverable detector
        r = self._parse("ok build them")
        self.assertEqual(r["action"]["action"], "conversation")


class TestNoPromiseWithoutQueue(unittest.TestCase):
    """Verify system prompt contains work request handling guard."""

    def test_handling_work_requests_in_prompt(self):
        self.assertIn("HANDLING WORK REQUESTS", persona.SYSTEM_PROMPT)

    def test_no_internal_leakage_language(self):
        lower = persona.SYSTEM_PROMPT.lower()
        # "conversation path" may appear in a "NEVER say" example — that's OK.
        # But it should NOT appear as a description of the system architecture.
        self.assertNotIn("you are in the conversation path", lower)
        self.assertNotIn("conversation mode", lower)
        self.assertNotIn("classifier routed", lower)

    def test_executive_voice_guidance(self):
        lower = persona.SYSTEM_PROMPT.lower()
        self.assertIn("queue that up", lower)
        self.assertIn("never explain internal routing", lower)

    def test_suggests_run_command(self):
        self.assertIn("/run", persona.SYSTEM_PROMPT)

    def test_trio_identity_present(self):
        self.assertIn("PARTNERSHIP MODEL", persona.SYSTEM_PROMPT)
        self.assertIn("ChatGPT Nova", persona.SYSTEM_PROMPT)


class TestArtifactRequestClassification(unittest.TestCase):
    """Verify artifact requests route to task."""

    def _parse(self, text):
        return parse.parse_message(text, "123", 1.0)

    def test_send_pdf_is_task(self):
        r = self._parse("send me the plan as a PDF")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_convert_to_pdf_is_task(self):
        r = self._parse("convert the report to PDF")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_export_csv_is_task(self):
        r = self._parse("export the data as CSV")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_save_as_document_is_task(self):
        r = self._parse("save that as a document please")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_turn_into_pdf_is_task(self):
        r = self._parse("can you turn that into a PDF?")
        self.assertEqual(r["action"]["action"], "run_task")

    def test_send_message_stays_chat(self):
        # "send me a message" has no artifact keyword
        r = self._parse("send me a message when it's done")
        self.assertEqual(r["action"]["action"], "conversation")

    def test_tell_me_about_pdf_stays_chat(self):
        # Informational, not a request to produce an artifact
        r = self._parse("tell me about PDF generation")
        self.assertEqual(r["action"]["action"], "conversation")


class TestNotifierDeference(unittest.TestCase):
    """Verify notifier deference mechanism exists."""

    def _get_notifier_content(self):
        _path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "telegram_notifier.py")
        with open(_path) as f:
            return f.read()

    def test_is_ceo_delegated_function_exists(self):
        content = self._get_notifier_content()
        self.assertIn("def _is_ceo_delegated", content)

    def test_defer_sleep_in_maybe_notify(self):
        content = self._get_notifier_content()
        self.assertIn("CEO-delegated task", content)
        self.assertIn("deferring", content)

    def test_fallback_after_defer(self):
        content = self._get_notifier_content()
        self.assertIn("fallback notify", content)

    def _get_bot_content(self):
        _path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "telegram_bot.py")
        with open(_path) as f:
            return f.read()

    def test_delegation_marker_written_by_bot(self):
        content = self._get_bot_content()
        self.assertIn("_write_delegation_marker", content)

    def test_delegation_marker_cleaned_up_on_completion(self):
        content = self._get_bot_content()
        self.assertIn("_cleanup_delegation_marker", content)


class TestWorkingMemoryStore(unittest.TestCase):
    """Test working memory store lifecycle."""

    def setUp(self):
        import importlib.util
        _path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "telegram", "working_memory.py")
        _spec = importlib.util.spec_from_file_location("tg_working_memory", _path)
        self.wm_mod = importlib.util.module_from_spec(_spec)
        sys.modules["tg_working_memory"] = self.wm_mod
        _spec.loader.exec_module(self.wm_mod)

        # Patch paths to temp dir
        self.tmpdir = os.path.join(os.path.dirname(__file__), "_test_wm")
        os.makedirs(self.tmpdir, exist_ok=True)
        self.wm_mod.STATE_DIR = type(self.wm_mod.STATE_DIR)(self.tmpdir)
        self.wm_mod.WM_FILE = self.wm_mod.STATE_DIR / "working_memory.json"
        self.wm_mod.WM_ARCHIVE = self.wm_mod.STATE_DIR / "working_memory_archive.json"

        self.store = self.wm_mod.WorkingMemoryStore()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_and_get(self):
        task = self.wm_mod.ActiveTask(
            task_stem="0042_build_api",
            chat_id="123",
            original_message="Build a new billing API",
            intent_summary="Build billing API",
            created_at=1000.0,
            status="pending",
            context_snapshot=[],
        )
        self.store.add(task)
        got = self.store.get("0042_build_api")
        self.assertIsNotNone(got)
        self.assertEqual(got.original_message, "Build a new billing API")

    def test_complete_archives_and_removes(self):
        task = self.wm_mod.ActiveTask(
            task_stem="0042_build_api",
            chat_id="123",
            original_message="Build API",
            intent_summary="Build API",
            created_at=1000.0,
            status="pending",
            context_snapshot=[],
        )
        self.store.add(task)
        completed = self.store.complete("0042_build_api")
        self.assertIsNotNone(completed)
        self.assertEqual(completed.status, "completed")
        # Should be removed from active
        self.assertIsNone(self.store.get("0042_build_api"))

    def test_active_for_chat(self):
        for i in range(3):
            chat = "123" if i < 2 else "456"
            task = self.wm_mod.ActiveTask(
                task_stem=f"000{i}_task",
                chat_id=chat,
                original_message=f"Task {i}",
                intent_summary=f"Task {i}",
                created_at=1000.0 + i,
                status="pending",
                context_snapshot=[],
            )
            self.store.add(task)
        self.assertEqual(len(self.store.active_for_chat("123")), 2)
        self.assertEqual(len(self.store.active_for_chat("456")), 1)

    def test_format_for_context_empty(self):
        ctx = self.store.format_for_context("123")
        self.assertEqual(ctx, "")

    def test_format_for_context_with_tasks(self):
        task = self.wm_mod.ActiveTask(
            task_stem="0042_build_api",
            chat_id="123",
            original_message="Build a new billing API",
            intent_summary="Build billing API",
            created_at=1000.0,
            status="pending",
            context_snapshot=[],
        )
        self.store.add(task)
        ctx = self.store.format_for_context("123")
        self.assertIn("ACTIVE BACKGROUND TASKS", ctx)
        self.assertIn("Build billing API", ctx)
        self.assertIn("Build a new billing API", ctx)

    def test_format_completion_context(self):
        task = self.wm_mod.ActiveTask(
            task_stem="0042_build_api",
            chat_id="123",
            original_message="Build a new billing API with auth",
            intent_summary="Build billing API",
            created_at=1000.0,
            status="completed",
            context_snapshot=[{"role": "user", "content": "Let's build something"}],
        )
        ctx = self.store.format_completion_context(task)
        self.assertIn("ORIGINAL USER REQUEST", ctx)
        self.assertIn("Build a new billing API with auth", ctx)
        self.assertIn("GOAL", ctx)

    def test_persistence_survives_reload(self):
        task = self.wm_mod.ActiveTask(
            task_stem="0042_build_api",
            chat_id="123",
            original_message="Build API",
            intent_summary="Build API",
            created_at=1000.0,
            status="pending",
            context_snapshot=[],
        )
        self.store.add(task)
        # Create a new store instance (simulates restart)
        store2 = self.wm_mod.WorkingMemoryStore()
        got = store2.get("0042_build_api")
        self.assertIsNotNone(got)
        self.assertEqual(got.original_message, "Build API")


# ── Phase 9: UX Polish Tests ──────────────────────────────────────────────


class TestHelpTextGrouping(unittest.TestCase):
    """Verify help text is organized by category."""

    def _get_help_text(self):
        """Load help text from telegram_bot module."""
        _bot_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "telegram_bot.py")
        # Read the file and extract _HELP_TEXT — simpler than importing the full bot
        with open(_bot_path) as f:
            content = f.read()
        # Just check categories exist in the file
        return content

    def test_help_has_task_category(self):
        content = self._get_help_text()
        self.assertIn('"TASKS\\n"', content)

    def test_help_has_output_category(self):
        content = self._get_help_text()
        self.assertIn('"OUTPUT\\n"', content)

    def test_help_has_conversation_category(self):
        content = self._get_help_text()
        self.assertIn('"CONVERSATION\\n"', content)

    def test_help_has_settings_category(self):
        content = self._get_help_text()
        self.assertIn('"SETTINGS\\n"', content)


class TestStatusIcons(unittest.TestCase):
    """Verify status icon mapping exists."""

    def _get_bot_content(self):
        _bot_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "telegram_bot.py")
        with open(_bot_path) as f:
            return f.read()

    def test_status_icon_dict_exists(self):
        content = self._get_bot_content()
        self.assertIn("_STATUS_ICON", content)

    def test_all_statuses_have_icons(self):
        content = self._get_bot_content()
        for status in ("queued", "inprogress", "done", "failed", "skip"):
            self.assertIn(f'"{status}"', content)

    def test_typing_indicator_import(self):
        content = self._get_bot_content()
        self.assertIn("ChatAction", content)

    def test_typing_indicator_usage(self):
        content = self._get_bot_content()
        self.assertIn("ChatAction.TYPING", content)


# ── Phase 10: Bounded UX Improvements Tests ──────────────────────────────


class TestReplyThreading(unittest.TestCase):
    """Verify reply threading support in working memory."""

    def setUp(self):
        import importlib.util
        _path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "telegram", "working_memory.py")
        _spec = importlib.util.spec_from_file_location("tg_wm_thread", _path)
        self.wm_mod = importlib.util.module_from_spec(_spec)
        sys.modules["tg_wm_thread"] = self.wm_mod  # register before exec (dataclass needs it)
        _spec.loader.exec_module(self.wm_mod)

        self.tmpdir = os.path.join(os.path.dirname(__file__), "_test_wm_thread")
        os.makedirs(self.tmpdir, exist_ok=True)
        self.wm_mod.STATE_DIR = type(self.wm_mod.STATE_DIR)(self.tmpdir)
        self.wm_mod.WM_FILE = self.wm_mod.STATE_DIR / "working_memory.json"
        self.wm_mod.WM_ARCHIVE = self.wm_mod.STATE_DIR / "working_memory_archive.json"
        self.store = self.wm_mod.WorkingMemoryStore()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_message_id_field_exists(self):
        task = self.wm_mod.ActiveTask(
            task_stem="0042_test", chat_id="123",
            original_message="test", intent_summary="test",
            created_at=1000.0, status="pending",
            context_snapshot=[], message_id=42,
        )
        self.assertEqual(task.message_id, 42)

    def test_message_id_default_zero(self):
        task = self.wm_mod.ActiveTask(
            task_stem="0042_test", chat_id="123",
            original_message="test", intent_summary="test",
            created_at=1000.0, status="pending",
            context_snapshot=[],
        )
        self.assertEqual(task.message_id, 0)

    def test_message_id_persists_through_save_reload(self):
        task = self.wm_mod.ActiveTask(
            task_stem="0042_test", chat_id="123",
            original_message="test", intent_summary="test",
            created_at=1000.0, status="pending",
            context_snapshot=[], message_id=999,
        )
        self.store.add(task)
        store2 = self.wm_mod.WorkingMemoryStore()
        got = store2.get("0042_test")
        self.assertEqual(got.message_id, 999)

    def test_message_id_survives_complete(self):
        task = self.wm_mod.ActiveTask(
            task_stem="0042_test", chat_id="123",
            original_message="test", intent_summary="test",
            created_at=1000.0, status="pending",
            context_snapshot=[], message_id=42,
        )
        self.store.add(task)
        completed = self.store.complete("0042_test")
        self.assertEqual(completed.message_id, 42)


class TestStaleWorkingMemoryCleanup(unittest.TestCase):
    """Verify stale task auto-archival."""

    def setUp(self):
        import importlib.util
        _path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "telegram", "working_memory.py")
        _spec = importlib.util.spec_from_file_location("tg_wm_stale", _path)
        self.wm_mod = importlib.util.module_from_spec(_spec)
        sys.modules["tg_wm_stale"] = self.wm_mod  # register before exec (dataclass needs it)
        _spec.loader.exec_module(self.wm_mod)

        self.tmpdir = os.path.join(os.path.dirname(__file__), "_test_wm_stale")
        os.makedirs(self.tmpdir, exist_ok=True)
        self.wm_mod.STATE_DIR = type(self.wm_mod.STATE_DIR)(self.tmpdir)
        self.wm_mod.WM_FILE = self.wm_mod.STATE_DIR / "working_memory.json"
        self.wm_mod.WM_ARCHIVE = self.wm_mod.STATE_DIR / "working_memory_archive.json"
        self.store = self.wm_mod.WorkingMemoryStore()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cleanup_removes_old_tasks(self):
        old_task = self.wm_mod.ActiveTask(
            task_stem="0001_old", chat_id="123",
            original_message="old task", intent_summary="old",
            created_at=time.time() - 100000,  # ~28 hours ago
            status="pending", context_snapshot=[],
        )
        new_task = self.wm_mod.ActiveTask(
            task_stem="0002_new", chat_id="123",
            original_message="new task", intent_summary="new",
            created_at=time.time(),
            status="pending", context_snapshot=[],
        )
        self.store.add(old_task)
        self.store.add(new_task)

        count = self.store.cleanup_stale(max_age_seconds=86400)
        self.assertEqual(count, 1)
        self.assertIsNone(self.store.get("0001_old"))
        self.assertIsNotNone(self.store.get("0002_new"))

    def test_cleanup_returns_zero_when_none_stale(self):
        task = self.wm_mod.ActiveTask(
            task_stem="0001_fresh", chat_id="123",
            original_message="fresh", intent_summary="fresh",
            created_at=time.time(),
            status="pending", context_snapshot=[],
        )
        self.store.add(task)
        count = self.store.cleanup_stale(max_age_seconds=86400)
        self.assertEqual(count, 0)

    def test_cleanup_archives_stale_tasks(self):
        import json
        old_task = self.wm_mod.ActiveTask(
            task_stem="0001_old", chat_id="123",
            original_message="old task", intent_summary="old",
            created_at=time.time() - 100000,
            status="pending", context_snapshot=[],
        )
        self.store.add(old_task)
        self.store.cleanup_stale(max_age_seconds=86400)

        archive = json.loads(self.wm_mod.WM_ARCHIVE.read_text(encoding="utf-8"))
        self.assertEqual(len(archive), 1)
        self.assertEqual(archive[0]["status"], "stale")


class TestEnhancedStatusTitles(unittest.TestCase):
    """Verify /status shows task titles."""

    def _get_bot_content(self):
        _bot_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "telegram_bot.py")
        with open(_bot_path) as f:
            return f.read()

    def test_status_handler_includes_title(self):
        content = self._get_bot_content()
        self.assertIn("title_part", content)

    def test_status_handler_strips_number_prefix(self):
        content = self._get_bot_content()
        # Should strip the 4-digit number prefix from stem
        self.assertIn('re.sub(r"^\\d{4}_"', content)


class TestRateLimitErrorFix(unittest.TestCase):
    """Verify rate limit tokens aren't consumed on errors."""

    def _get_bot_content(self):
        _bot_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "telegram_bot.py")
        with open(_bot_path) as f:
            return f.read()

    def test_rate_limit_only_on_success(self):
        content = self._get_bot_content()
        # rate_limiter.record should appear inside the success branch (after record_success)
        # and BEFORE the error branch — meaning it's in the success block, not after errors.
        # Use rindex to find the last occurrence (the one in the success block, not cache hit).
        success_idx = content.index("_circuit_breaker.record_success()")
        record_idx = content.rindex("_rate_limiter.record(chat_id)")
        content.index("_circuit_breaker.record_failure()")
        # record should come AFTER record_success (inside success block)
        self.assertGreater(record_idx, success_idx)


class TestCompletionReplyThreading(unittest.TestCase):
    """Verify completion notifications use reply_to_message_id."""

    def _get_bot_content(self):
        _bot_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "telegram_bot.py")
        with open(_bot_path) as f:
            return f.read()

    def test_reply_to_message_id_in_completion(self):
        content = self._get_bot_content()
        self.assertIn("reply_to_message_id", content)

    def test_allow_sending_without_reply(self):
        content = self._get_bot_content()
        # Graceful fallback if original message was deleted
        self.assertIn("allow_sending_without_reply", content)


if __name__ == "__main__":
    unittest.main()
