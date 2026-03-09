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
for _mod_name in ("parse", "conversation", "llm", "persona", "format"):
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
        self.mgr = conversation.ConversationManager()

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
        mgr = conversation.ConversationManager()

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

    def test_no_file_tools_guidance(self):
        """Ensure the prompt tells Claude not to use file editing tools."""
        lower = persona.SYSTEM_PROMPT.lower()
        self.assertIn("don't use file editing tools", lower)


class TestSessionStartIntegration(unittest.TestCase):
    """Verify that session start detection works with conversation manager."""

    def test_new_chat_is_session_start(self):
        mgr = conversation.ConversationManager()
        self.assertTrue(mgr.is_session_start("brand_new_chat"))

    def test_active_chat_not_session_start(self):
        mgr = conversation.ConversationManager()
        mgr.add_user_message("c1", "hello")
        self.assertFalse(mgr.is_session_start("c1"))

    def test_session_start_only_on_first_message(self):
        """Session start should be True only before the first message."""
        mgr = conversation.ConversationManager()
        # First time — session start
        self.assertTrue(mgr.is_session_start("c1"))
        # Add a message
        mgr.add_user_message("c1", "hello")
        # Now it's not a session start
        self.assertFalse(mgr.is_session_start("c1"))


if __name__ == "__main__":
    unittest.main()
