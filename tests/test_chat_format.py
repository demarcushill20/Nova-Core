"""Tests for intent classification + report stripping."""

import sys
import os

# Ensure the project root is on sys.path so local telegram/ resolves.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from telegram.parse import classify_intent
from telegram.format import strip_report_sections


# ---------------------------------------------------------------------------
# classify_intent
# ---------------------------------------------------------------------------

class TestClassifyIntent:
    def test_plain_question_is_chat(self):
        assert classify_intent("How do I enter tmux?") == "chat"

    def test_plain_greeting_is_chat(self):
        assert classify_intent("Hey, what's up?") == "chat"

    def test_empty_is_chat(self):
        assert classify_intent("") == "chat"

    def test_whitespace_is_chat(self):
        assert classify_intent("   ") == "chat"

    def test_plain_sentence_is_chat(self):
        assert classify_intent("Explain how docker networking works") == "chat"

    def test_status_is_task(self):
        assert classify_intent("/status") == "task"

    def test_run_is_task(self):
        assert classify_intent("/run deploy the new version") == "task"

    def test_last_is_task(self):
        assert classify_intent("/last") == "task"

    def test_get_is_task(self):
        assert classify_intent("/get 0009") == "task"

    def test_tail_is_task(self):
        assert classify_intent("/tail 0009") == "task"

    def test_cancel_is_task(self):
        assert classify_intent("/cancel last") == "task"

    def test_keyword_report_is_task(self):
        assert classify_intent("Give me the contract + files referenced") == "task"

    def test_keyword_verbose_is_task(self):
        assert classify_intent("Show me verbose output for that") == "task"

    def test_keyword_audit_is_task(self):
        assert classify_intent("I need the audit trail") == "task"

    def test_keyword_debug_is_task(self):
        assert classify_intent("debug the watcher service") == "task"

    def test_keyword_show_files_is_task(self):
        assert classify_intent("show files created by the last task") == "task"

    def test_keyword_detailed_is_task(self):
        assert classify_intent("Give me a detailed breakdown") == "task"

    def test_chat_prefix_forces_chat(self):
        assert classify_intent("/chat Give me a short answer about tmux") == "chat"

    def test_report_prefix_forces_task(self):
        assert classify_intent("/report What is docker?") == "task"

    def test_chat_prefix_overrides_keyword(self):
        # /chat should win even if body contains "report"
        assert classify_intent("/chat Give me a report summary") == "chat"

    def test_mode_is_task(self):
        assert classify_intent("/mode compact") == "task"

    def test_help_is_task(self):
        assert classify_intent("/help") == "task"


# ---------------------------------------------------------------------------
# strip_report_sections
# ---------------------------------------------------------------------------

# Realistic worker output blob (matches the exact junk from the user example)
_SAMPLE_OUTPUT = """\
# Task 0010: How Is OpenClaw Autonomous?

**Task:** 0010_How_is_open_claw_autonomous_
**Completed:** 2026-03-05 16:02 UTC
**Source:** MEMORY/openclaw_research.md

---

## Short Answer

OpenClaw achieves autonomy through three mechanisms:
1. Heartbeat daemon for proactive polling
2. Cron system for precise scheduling
3. Serial queue with persistent memory

---

## The 3 Pillars of OpenClaw Autonomy

### 1. Heartbeat Daemon

The daemon polls for new tasks every 30 seconds.

### 2. Cron System

Precise scheduling via cron expressions.

### 3. Serial Queue

Tasks are queued and executed one at a time.

---

## Supporting Infrastructure

| Component | Role in Autonomy |
|-----------|-----------------|
| Memory | Markdown files stored across sessions |
| Tool safety | Sandboxed execution with audit trail |

## Security Notes

All commands run inside a restricted sandbox.
No network access by default.

## Files Referenced
- **Source:** `/home/nova/nova-core/MEMORY/openclaw_research.md`

## CONTRACT
summary: Answered how OpenClaw achieves autonomy
task_id: 0010_How_is_open_claw_autonomous_
status: done
verification: Output file exists with non-zero content
"""


class TestStripReportSections:
    def test_removes_contract_block(self):
        result = strip_report_sections(_SAMPLE_OUTPUT)
        assert "## CONTRACT" not in result
        assert "task_id:" not in result
        assert "verification:" not in result
        assert "status: done" not in result

    def test_removes_files_referenced(self):
        result = strip_report_sections(_SAMPLE_OUTPUT)
        assert "## Files Referenced" not in result
        assert "openclaw_research.md" not in result

    def test_removes_security_notes(self):
        result = strip_report_sections(_SAMPLE_OUTPUT)
        assert "## Security Notes" not in result
        assert "restricted sandbox" not in result

    def test_removes_metadata_lines(self):
        result = strip_report_sections(_SAMPLE_OUTPUT)
        assert "**Task:**" not in result
        assert "**Completed:**" not in result
        assert "**Source:**" not in result

    def test_removes_title_line(self):
        result = strip_report_sections(_SAMPLE_OUTPUT)
        assert "# Task 0010:" not in result

    def test_removes_tool_audit_table(self):
        result = strip_report_sections(_SAMPLE_OUTPUT)
        assert "| Memory | Markdown files" not in result
        assert "| Tool safety |" not in result

    def test_preserves_answer_content(self):
        result = strip_report_sections(_SAMPLE_OUTPUT)
        assert "OpenClaw achieves autonomy through three mechanisms" in result
        assert "Heartbeat daemon" in result
        assert "Cron System" in result
        assert "Serial Queue" in result
        assert "proactive polling" in result

    def test_preserves_section_headers_in_answer(self):
        result = strip_report_sections(_SAMPLE_OUTPUT)
        assert "## Short Answer" in result
        assert "## The 3 Pillars" in result

    def test_no_trailing_separators(self):
        result = strip_report_sections(_SAMPLE_OUTPUT)
        assert not result.rstrip().endswith("---")

    def test_no_notifier_footer(self):
        text = _SAMPLE_OUTPUT + "\n---\nnotifier_pid=12345 host=vultr\n"
        result = strip_report_sections(text)
        assert "notifier_pid=" not in result
        assert "host=vultr" not in result

    def test_no_contract_fields_standalone(self):
        text = "summary: This is a test\ntask_id: 0010\nstatus: done\nverification: ok\n"
        result = strip_report_sections(text)
        assert result.strip() == ""

    def test_plain_text_passthrough(self):
        """Plain text without any report markers should pass through unchanged."""
        text = "This is just a normal answer about Docker networking."
        result = strip_report_sections(text)
        assert result == text

    def test_empty_input(self):
        assert strip_report_sections("") == ""

    def test_files_created_modified_variant(self):
        text = "Good answer here.\n\n## Files Created/Modified\n- `foo.txt`\n"
        result = strip_report_sections(text)
        assert "Files Created/Modified" not in result
        assert "Good answer here" in result

    def test_bare_contract_block(self):
        text = "The answer.\n\nCONTRACT\nsummary: blah\ntask_id: x\n"
        result = strip_report_sections(text)
        assert "CONTRACT" not in result
        assert "The answer" in result


# ---------------------------------------------------------------------------
# Integration: classify + strip together
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_chat_intent_gets_clean_output(self):
        """Full flow: chat intent → stripped output."""
        intent = classify_intent("How does docker networking work?")
        assert intent == "chat"
        result = strip_report_sections(_SAMPLE_OUTPUT)
        # Should have content but no junk
        assert len(result) > 50
        assert "## CONTRACT" not in result
        assert "notifier_pid" not in result

    def test_task_intent_preserves_raw(self):
        """Task intent means we don't strip."""
        intent = classify_intent("/run deploy new version")
        assert intent == "task"
        # In task mode the notifier uses build_message, not strip_report_sections
