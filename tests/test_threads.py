"""Tests for nova-link/threads.py — Thread model and ThreadStore."""

from __future__ import annotations

import importlib
import json
import time

import pytest

_mod = importlib.import_module("nova-link.threads")
Thread = _mod.Thread
Message = _mod.Message
ThreadStore = _mod.ThreadStore


@pytest.fixture()
def store(tmp_path):
    """ThreadStore backed by a temp directory."""
    return ThreadStore(threads_dir=tmp_path)


@pytest.fixture()
def legacy_dir(tmp_path):
    """Create a fake legacy conversation file and return the conversations dir."""
    conv_dir = tmp_path / "conversations"
    conv_dir.mkdir()
    data = {
        "messages": [
            {"role": "user", "content": "Hello Nova", "timestamp": 1000.0},
            {"role": "assistant", "content": "Hey there!", "timestamp": 1001.0},
            {"role": "user", "content": "How are you?", "timestamp": 1002.0},
            {"role": "assistant", "content": "Doing great.", "timestamp": 1003.0},
        ],
        "last_activity": 1003.0,
        "session_summary": "User discussed 1 topic(s): Hello Nova",
    }
    (conv_dir / "530812511.json").write_text(json.dumps(data))
    return conv_dir


# ---- Thread / Message dataclass tests ------------------------------------


class TestModels:
    def test_thread_roundtrip(self):
        t = Thread(
            id="abc123",
            title="Test thread",
            created_at=1000.0,
            updated_at=1001.0,
            summary={"type": "text", "content": "hello"},
            metadata={"custom": True},
            message_count=5,
        )
        d = t.to_dict()
        t2 = Thread.from_dict(d)
        assert t2.id == "abc123"
        assert t2.title == "Test thread"
        assert t2.summary == {"type": "text", "content": "hello"}
        assert t2.metadata == {"custom": True}
        assert t2.message_count == 5

    def test_thread_from_dict_defaults(self):
        t = Thread.from_dict({"id": "x", "title": "T"})
        assert t.status == "active"
        assert t.summary is None
        assert t.metadata == {}
        assert t.message_count == 0

    def test_message_roundtrip(self):
        m = Message(
            id="msg1",
            thread_id="t1",
            role="user",
            content="Hi",
            created_at=1000.0,
            metadata={"source": "test"},
        )
        d = m.to_dict()
        m2 = Message.from_dict(d)
        assert m2.id == "msg1"
        assert m2.role == "user"
        assert m2.metadata == {"source": "test"}

    def test_message_metadata_omitted_when_empty(self):
        m = Message(id="m", thread_id="t", role="user", content="x", created_at=0)
        d = m.to_dict()
        assert "metadata" not in d

    def test_summary_evolvable_schema(self):
        """Summary dict can hold structured data without breaking text fallback."""
        structured = {
            "type": "structured",
            "content": "fallback text",
            "goals": ["build feature X"],
            "decisions": ["use SQLite"],
        }
        t = Thread(id="x", title="T", created_at=0, updated_at=0, summary=structured)
        d = t.to_dict()
        t2 = Thread.from_dict(d)
        assert t2.summary["type"] == "structured"
        assert t2.summary["content"] == "fallback text"
        assert t2.summary["goals"] == ["build feature X"]


# ---- ThreadStore CRUD tests ----------------------------------------------


class TestThreadStoreCRUD:
    def test_create_and_get(self, store):
        t = store.create_thread(title="My thread")
        assert t.title == "My thread"
        assert t.status == "active"
        assert t.message_count == 0

        loaded = store.get_thread(t.id)
        assert loaded is not None
        assert loaded.id == t.id
        assert loaded.title == "My thread"

    def test_get_nonexistent_returns_none(self, store):
        assert store.get_thread("nonexistent") is None

    def test_list_threads_ordered_by_updated(self, store):
        t1 = store.create_thread(title="Old")
        time.sleep(0.01)
        t2 = store.create_thread(title="New")

        threads = store.list_threads()
        assert len(threads) == 2
        assert threads[0].id == t2.id  # newest first (descending)
        assert threads[1].id == t1.id

    def test_list_threads_pagination(self, store):
        for i in range(5):
            store.create_thread(title=f"Thread {i}")
            time.sleep(0.01)

        page1 = store.list_threads(limit=2, offset=0)
        page2 = store.list_threads(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].id != page2[0].id

    def test_list_threads_filter_by_status(self, store):
        t1 = store.create_thread(title="Active")
        t2 = store.create_thread(title="To archive")
        store.archive_thread(t2.id)

        active = store.list_threads(status="active")
        archived = store.list_threads(status="archived")
        all_threads = store.list_threads(status=None)

        assert len(active) == 1
        assert active[0].id == t1.id
        assert len(archived) == 1
        assert archived[0].id == t2.id
        assert len(all_threads) == 2

    def test_update_thread(self, store):
        t = store.create_thread(title="Original")
        updated = store.update_thread(t.id, title="Renamed")
        assert updated.title == "Renamed"

        reloaded = store.get_thread(t.id)
        assert reloaded.title == "Renamed"

    def test_update_thread_immutable_fields(self, store):
        t = store.create_thread(title="Test")
        original_id = t.id
        original_created = t.created_at
        store.update_thread(t.id, id="hacked", created_at=0.0)

        reloaded = store.get_thread(original_id)
        assert reloaded.id == original_id
        assert reloaded.created_at == original_created

    def test_archive_thread(self, store):
        t = store.create_thread(title="To archive")
        assert store.archive_thread(t.id) is True

        reloaded = store.get_thread(t.id)
        assert reloaded.status == "archived"

    def test_archive_nonexistent(self, store):
        assert store.archive_thread("nonexistent") is False


# ---- Message CRUD tests --------------------------------------------------


class TestMessages:
    def test_add_and_get_messages(self, store):
        t = store.create_thread(title="Chat")
        store.add_message(t.id, "user", "Hello")
        store.add_message(t.id, "assistant", "Hi there")

        msgs = store.get_messages(t.id)
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[0].content == "Hello"
        assert msgs[1].role == "assistant"

    def test_add_message_updates_thread(self, store):
        t = store.create_thread(title="Chat")
        store.add_message(t.id, "user", "Test")

        reloaded = store.get_thread(t.id)
        assert reloaded.message_count == 1
        assert reloaded.updated_at >= t.updated_at

    def test_add_message_auto_titles(self, store):
        t = store.create_thread()  # default title "New conversation"
        store.add_message(t.id, "user", "What is the weather like today?")

        reloaded = store.get_thread(t.id)
        assert reloaded.title == "What is the weather like today?"

    def test_auto_title_only_first_line(self, store):
        t = store.create_thread()
        store.add_message(t.id, "user", "First line\nSecond line\nThird")

        reloaded = store.get_thread(t.id)
        assert reloaded.title == "First line"

    def test_add_message_to_nonexistent_thread(self, store):
        result = store.add_message("ghost", "user", "Hello")
        assert result is None

    def test_get_messages_pagination(self, store):
        t = store.create_thread(title="Paginate")
        for i in range(10):
            store.add_message(t.id, "user", f"Message {i}")

        page = store.get_messages(t.id, limit=3, offset=2)
        assert len(page) == 3
        assert page[0].content == "Message 2"

    def test_get_messages_desc_order(self, store):
        t = store.create_thread(title="Order")
        store.add_message(t.id, "user", "First")
        store.add_message(t.id, "user", "Second")

        msgs = store.get_messages(t.id, order="desc")
        assert msgs[0].content == "Second"
        assert msgs[1].content == "First"

    def test_get_message_count(self, store):
        t = store.create_thread()
        assert store.get_message_count(t.id) == 0
        store.add_message(t.id, "user", "One")
        store.add_message(t.id, "assistant", "Two")
        assert store.get_message_count(t.id) == 2

    def test_message_metadata(self, store):
        t = store.create_thread()
        msg = store.add_message(t.id, "user", "Hi", metadata={"tool_calls": []})
        assert msg.metadata == {"tool_calls": []}

        loaded = store.get_messages(t.id)
        assert loaded[0].metadata == {"tool_calls": []}


# ---- Default thread resolution -------------------------------------------


class TestDefaultThread:
    @pytest.fixture(autouse=True)
    def _isolate_legacy(self, tmp_path, monkeypatch):
        """Point legacy dir to empty temp so migration doesn't trigger."""
        monkeypatch.setattr(_mod, "LEGACY_CONVERSATIONS_DIR", tmp_path / "empty")

    def test_creates_default_when_empty(self, store):
        t = store.get_or_create_default_thread()
        assert t.title == "Nova conversation"
        assert t.metadata.get("default") is True

    def test_returns_same_default_on_repeat_calls(self, store):
        t1 = store.get_or_create_default_thread()
        t2 = store.get_or_create_default_thread()
        assert t1.id == t2.id

    def test_default_thread_id_persisted(self, store):
        t = store.get_or_create_default_thread()
        assert store.get_default_thread_id() == t.id

    def test_recreates_if_default_file_deleted(self, store):
        t1 = store.get_or_create_default_thread()
        # Simulate file loss
        store._thread_path(t1.id).unlink()
        t2 = store.get_or_create_default_thread()
        assert t2.id != t1.id  # new thread created
        assert store.get_default_thread_id() == t2.id


# ---- Legacy migration tests ----------------------------------------------


class TestMigration:
    def test_migrate_idempotent(self, tmp_path, legacy_dir, monkeypatch):
        threads_dir = tmp_path / "threads"
        store = ThreadStore(threads_dir=threads_dir)
        monkeypatch.setattr(_mod, "LEGACY_CONVERSATIONS_DIR", legacy_dir)

        t1 = store.migrate_legacy_conversation()
        assert t1 is not None
        assert t1.message_count == 4
        assert t1.metadata.get("migrated_from") == "530812511"

        # Second call returns same thread
        t2 = store.migrate_legacy_conversation()
        assert t2.id == t1.id

    def test_migrate_preserves_summary(self, tmp_path, legacy_dir, monkeypatch):
        threads_dir = tmp_path / "threads"
        store = ThreadStore(threads_dir=threads_dir)
        monkeypatch.setattr(_mod, "LEGACY_CONVERSATIONS_DIR", legacy_dir)

        t = store.migrate_legacy_conversation()
        assert t.summary is not None
        assert t.summary["type"] == "text"
        assert "Hello Nova" in t.summary["content"]

    def test_migrate_preserves_messages(self, tmp_path, legacy_dir, monkeypatch):
        threads_dir = tmp_path / "threads"
        store = ThreadStore(threads_dir=threads_dir)
        monkeypatch.setattr(_mod, "LEGACY_CONVERSATIONS_DIR", legacy_dir)

        t = store.migrate_legacy_conversation()
        msgs = store.get_messages(t.id)
        assert len(msgs) == 4
        assert msgs[0].role == "user"
        assert msgs[0].content == "Hello Nova"
        assert msgs[0].metadata.get("migrated") is True
        assert msgs[1].role == "assistant"

    def test_migrate_preserves_timestamps(self, tmp_path, legacy_dir, monkeypatch):
        threads_dir = tmp_path / "threads"
        store = ThreadStore(threads_dir=threads_dir)
        monkeypatch.setattr(_mod, "LEGACY_CONVERSATIONS_DIR", legacy_dir)

        t = store.migrate_legacy_conversation()
        assert t.created_at == 1000.0
        assert t.updated_at == 1003.0
        msgs = store.get_messages(t.id)
        assert msgs[0].created_at == 1000.0

    def test_migrate_sets_default_thread(self, tmp_path, legacy_dir, monkeypatch):
        threads_dir = tmp_path / "threads"
        store = ThreadStore(threads_dir=threads_dir)
        monkeypatch.setattr(_mod, "LEGACY_CONVERSATIONS_DIR", legacy_dir)

        t = store.migrate_legacy_conversation()
        assert store.get_default_thread_id() == t.id

    def test_migrate_no_legacy_file_returns_none(self, store, tmp_path, monkeypatch):
        monkeypatch.setattr(_mod, "LEGACY_CONVERSATIONS_DIR", tmp_path / "empty")
        assert store.migrate_legacy_conversation() is None

    def test_default_thread_triggers_migration(self, tmp_path, legacy_dir, monkeypatch):
        threads_dir = tmp_path / "threads"
        store = ThreadStore(threads_dir=threads_dir)
        monkeypatch.setattr(_mod, "LEGACY_CONVERSATIONS_DIR", legacy_dir)

        # get_or_create_default_thread should auto-migrate
        t = store.get_or_create_default_thread()
        assert t.metadata.get("migrated_from") == "530812511"
        assert t.message_count == 4

    def test_migrate_titles_from_first_user_message(self, tmp_path, legacy_dir, monkeypatch):
        threads_dir = tmp_path / "threads"
        store = ThreadStore(threads_dir=threads_dir)
        monkeypatch.setattr(_mod, "LEGACY_CONVERSATIONS_DIR", legacy_dir)

        t = store.migrate_legacy_conversation()
        assert t.title == "Hello Nova"


# ---- Index consistency tests ---------------------------------------------


class TestIndex:
    def test_index_updated_on_create(self, store):
        t = store.create_thread(title="Indexed")
        index = store._load_index()
        assert t.id in index["threads"]
        assert index["threads"][t.id]["title"] == "Indexed"

    def test_index_updated_on_message(self, store):
        t = store.create_thread(title="Chat")
        store.add_message(t.id, "user", "Hello")
        index = store._load_index()
        assert index["threads"][t.id]["message_count"] == 1

    def test_index_updated_on_archive(self, store):
        t = store.create_thread(title="Archive me")
        store.archive_thread(t.id)
        index = store._load_index()
        assert index["threads"][t.id]["status"] == "archived"


# ---- Context assembly tests (Phase 4) ------------------------------------

_api_mod = importlib.import_module("nova-link.api")
_build_thread_context = _api_mod._build_thread_context


class TestBuildThreadContext:
    def test_empty_context(self):
        assert _build_thread_context([], None) == ""

    def test_messages_only(self):
        msgs = [
            Message(id="1", thread_id="t", role="user", content="Hi", created_at=0),
            Message(id="2", thread_id="t", role="assistant", content="Hello!", created_at=1),
        ]
        ctx = _build_thread_context(msgs, None)
        assert ctx == "Human: Hi\nYou: Hello!"

    def test_summary_only(self):
        ctx = _build_thread_context([], {"type": "text", "content": "User asked about weather"})
        assert ctx == "[Earlier conversation context: User asked about weather]"

    def test_summary_plus_messages(self):
        msgs = [
            Message(id="1", thread_id="t", role="user", content="What now?", created_at=0),
        ]
        summary = {"type": "text", "content": "Discussed project plans"}
        ctx = _build_thread_context(msgs, summary)
        assert "[Earlier conversation context: Discussed project plans]" in ctx
        assert "Human: What now?" in ctx
        # Summary comes before messages
        assert ctx.index("[Earlier") < ctx.index("Human:")

    def test_empty_summary_ignored(self):
        msgs = [
            Message(id="1", thread_id="t", role="user", content="Test", created_at=0),
        ]
        ctx = _build_thread_context(msgs, {"type": "text", "content": ""})
        assert "Earlier" not in ctx
        assert ctx == "Human: Test"

    def test_none_summary_ignored(self):
        msgs = [
            Message(id="1", thread_id="t", role="user", content="Test", created_at=0),
        ]
        ctx = _build_thread_context(msgs, None)
        assert ctx == "Human: Test"

    def test_structured_summary_uses_content_fallback(self):
        """Structured summaries use the 'content' field for context assembly."""
        summary = {
            "type": "structured",
            "content": "User is building a chat app",
            "goals": ["build chat"],
            "decisions": ["use FastAPI"],
        }
        ctx = _build_thread_context([], summary)
        assert ctx == "[Earlier conversation context: User is building a chat app]"


# ---- Thread compaction tests (Phase 5) -----------------------------------


class TestCompactThread:
    def test_compact_removes_old_messages(self, store):
        t = store.create_thread(title="Compact me")
        for i in range(10):
            store.add_message(t.id, "user", f"Message {i}")

        result = store.compact_thread(
            t.id,
            keep_recent=3,
            new_summary={"type": "text", "content": "First 7 messages discussed stuff"},
        )
        assert result is not None
        assert result.message_count == 3
        assert result.summary["content"] == "First 7 messages discussed stuff"

        msgs = store.get_messages(t.id)
        assert len(msgs) == 3
        assert msgs[0].content == "Message 7"
        assert msgs[2].content == "Message 9"

    def test_compact_updates_summary(self, store):
        t = store.create_thread(title="Summary test")
        store.add_message(t.id, "user", "Hello")

        result = store.compact_thread(
            t.id,
            keep_recent=1,
            new_summary={
                "type": "structured",
                "content": "fallback text",
                "goals": ["build feature"],
            },
        )
        assert result.summary["type"] == "structured"
        assert result.summary["goals"] == ["build feature"]

        reloaded = store.get_thread(t.id)
        assert reloaded.summary["type"] == "structured"

    def test_compact_nonexistent_returns_none(self, store):
        assert store.compact_thread("ghost", 5, {"type": "text", "content": ""}) is None

    def test_compact_noop_when_fewer_than_keep(self, store):
        t = store.create_thread(title="Small thread")
        store.add_message(t.id, "user", "Only one")

        result = store.compact_thread(
            t.id,
            keep_recent=10,
            new_summary={"type": "text", "content": "updated"},
        )
        assert result.message_count == 1
        assert result.summary["content"] == "updated"

    def test_compact_updates_index(self, store):
        t = store.create_thread(title="Index check")
        for i in range(5):
            store.add_message(t.id, "user", f"Msg {i}")

        store.compact_thread(
            t.id,
            keep_recent=2,
            new_summary={"type": "text", "content": "compacted"},
        )
        index = store._load_index()
        assert index["threads"][t.id]["message_count"] == 2


# ---- Summarization tests (Phase 5) --------------------------------------

_deterministic_fallback_summary = _api_mod._deterministic_fallback_summary


class TestDeterministicFallback:
    def test_basic_fallback(self):
        msgs = [
            Message(id="1", thread_id="t", role="user", content="How do I deploy?", created_at=0),
            Message(id="2", thread_id="t", role="assistant", content="Use docker", created_at=1),
            Message(id="3", thread_id="t", role="user", content="What about CI?", created_at=2),
        ]
        result = _deterministic_fallback_summary(msgs, None)
        assert result["type"] == "text"
        assert "How do I deploy?" in result["content"]
        assert "What about CI?" in result["content"]

    def test_preserves_existing_summary(self):
        msgs = [
            Message(id="1", thread_id="t", role="user", content="New topic", created_at=0),
        ]
        existing = {"type": "text", "content": "Earlier: discussed auth"}
        result = _deterministic_fallback_summary(msgs, existing)
        assert "Earlier: discussed auth" in result["content"]
        assert "New topic" in result["content"]

    def test_empty_messages(self):
        result = _deterministic_fallback_summary([], None)
        assert result["type"] == "text"
        assert result["content"] == "Conversation history"

    def test_no_user_messages(self):
        msgs = [
            Message(id="1", thread_id="t", role="assistant", content="Hello!", created_at=0),
        ]
        result = _deterministic_fallback_summary(msgs, None)
        assert result["type"] == "text"


class TestGenerateThreadSummary:
    @pytest.mark.asyncio()
    async def test_falls_back_on_llm_failure(self, monkeypatch):
        """When LLM call returns None, falls back to deterministic summary."""
        _generate = _api_mod._generate_thread_summary

        async def _mock_llm(_prompt):
            return None

        monkeypatch.setattr(_api_mod, "_summarize_via_llm", _mock_llm)

        msgs = [
            Message(id="1", thread_id="t", role="user", content="Test msg", created_at=0),
        ]
        result = await _generate(msgs, None)
        assert result["type"] == "text"
        assert "Test msg" in result["content"]

    @pytest.mark.asyncio()
    async def test_parses_valid_llm_json(self, monkeypatch):
        """When LLM returns valid JSON, it's parsed into the summary."""
        _generate = _api_mod._generate_thread_summary

        llm_response = json.dumps(
            {
                "type": "structured",
                "content": "User wants to build a chat app",
                "goals": ["build chat app"],
                "decisions": ["use FastAPI"],
                "open_questions": [],
                "artifacts": ["api.py"],
                "user_preferences": ["dark theme"],
            }
        )

        async def _mock_llm(_prompt):
            return llm_response

        monkeypatch.setattr(_api_mod, "_summarize_via_llm", _mock_llm)

        msgs = [
            Message(id="1", thread_id="t", role="user", content="Build chat", created_at=0),
        ]
        result = await _generate(msgs, None)
        assert result["type"] == "structured"
        assert result["goals"] == ["build chat app"]
        assert result["content"] == "User wants to build a chat app"

    @pytest.mark.asyncio()
    async def test_handles_markdown_fenced_json(self, monkeypatch):
        """LLM sometimes wraps JSON in ```json ... ``` fences."""
        _generate = _api_mod._generate_thread_summary

        llm_response = '```json\n{"type": "structured", "content": "summary"}\n```'

        async def _mock_llm(_prompt):
            return llm_response

        monkeypatch.setattr(_api_mod, "_summarize_via_llm", _mock_llm)

        msgs = [
            Message(id="1", thread_id="t", role="user", content="test", created_at=0),
        ]
        result = await _generate(msgs, None)
        assert result["type"] == "structured"
        assert result["content"] == "summary"

    @pytest.mark.asyncio()
    async def test_invalid_json_falls_back(self, monkeypatch):
        """Garbled LLM output falls back to deterministic summary."""
        _generate = _api_mod._generate_thread_summary

        async def _mock_llm(_prompt):
            return "This is not JSON at all"

        monkeypatch.setattr(_api_mod, "_summarize_via_llm", _mock_llm)

        msgs = [
            Message(id="1", thread_id="t", role="user", content="test", created_at=0),
        ]
        result = await _generate(msgs, None)
        assert result["type"] == "text"


class TestMaybeSummarizeThread:
    @pytest.fixture(autouse=True)
    def _patch_store_and_llm(self, store, monkeypatch):
        """Point _thread_store at our test store and mock the LLM."""
        monkeypatch.setattr(_api_mod, "_thread_store", store)

        async def _mock_llm(_prompt):
            return json.dumps(
                {
                    "type": "structured",
                    "content": "Mocked summary",
                    "goals": [],
                    "decisions": [],
                    "open_questions": [],
                    "artifacts": [],
                    "user_preferences": [],
                }
            )

        monkeypatch.setattr(_api_mod, "_summarize_via_llm", _mock_llm)
        self._store = store

    @pytest.mark.asyncio()
    async def test_no_op_below_threshold(self):
        t = self._store.create_thread(title="Small")
        for i in range(5):
            self._store.add_message(t.id, "user", f"Msg {i}")

        await _api_mod._maybe_summarize_thread(t.id)

        reloaded = self._store.get_thread(t.id)
        assert reloaded.message_count == 5  # unchanged
        assert reloaded.summary is None

    @pytest.mark.asyncio()
    async def test_compacts_above_threshold(self, monkeypatch):
        monkeypatch.setattr(_api_mod, "_SUMMARIZE_THRESHOLD", 10)
        monkeypatch.setattr(_api_mod, "_SUMMARIZE_KEEP_RECENT", 4)

        t = self._store.create_thread(title="Big thread")
        for i in range(12):
            self._store.add_message(t.id, "user", f"Message {i}")

        await _api_mod._maybe_summarize_thread(t.id)

        reloaded = self._store.get_thread(t.id)
        assert reloaded.message_count == 4
        assert reloaded.summary is not None
        assert reloaded.summary["type"] == "structured"
        assert reloaded.summary["content"] == "Mocked summary"

        msgs = self._store.get_messages(t.id)
        assert len(msgs) == 4
        assert msgs[0].content == "Message 8"

    @pytest.mark.asyncio()
    async def test_nonexistent_thread_no_error(self):
        await _api_mod._maybe_summarize_thread("nonexistent")  # should not raise
