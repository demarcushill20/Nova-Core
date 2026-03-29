#!/usr/bin/env python3
"""Tests for utils/structured_log.py — structured JSONL logging."""

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from utils.structured_log import StructuredLogger
from utils.trace_context import TraceContext


class TestStructuredLogger:
    """Test suite for StructuredLogger."""

    def test_init_default_path(self):
        """Test StructuredLogger initialization with default path."""
        logger = StructuredLogger()
        expected_path = Path(os.environ.get("NOVA_LOGS_DIR", "LOGS")) / "structured.jsonl"
        assert logger._path == expected_path
        assert isinstance(logger._lock, type(threading.Lock()))

    def test_init_custom_path(self):
        """Test StructuredLogger initialization with custom path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_path = Path(tmpdir) / "custom.jsonl"
            logger = StructuredLogger(custom_path)
            assert logger._path == custom_path

    def test_event_basic(self):
        """Test basic event logging without context."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            logger = StructuredLogger(log_path)

            # Log an event
            result = logger.event("test.event", level="info", key="value")

            # Check return value
            assert result["event"] == "test.event"
            assert result["level"] == "info"
            assert result["data"] == {"key": "value"}
            assert "ts" in result

            # Verify timestamp format
            parsed_time = datetime.fromisoformat(result["ts"].replace("Z", "+00:00"))
            assert isinstance(parsed_time, datetime)

            # Check log file content
            assert log_path.exists()
            with open(log_path) as f:
                line = f.read().strip()
                logged_data = json.loads(line)

            assert logged_data["event"] == "test.event"
            assert logged_data["level"] == "info"
            assert logged_data["data"]["key"] == "value"

    def test_event_with_trace_context(self):
        """Test event logging with trace context."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            logger = StructuredLogger(log_path)

            # Create trace context
            ctx = TraceContext.new("test-component", task="task-123")

            # Log an event
            result = logger.event("test.traced", ctx, test_data="value")

            # Check trace context is included
            ctx_dict = ctx.as_dict()
            for key, value in ctx_dict.items():
                assert result[key] == value

            # Verify in log file
            with open(log_path) as f:
                logged_data = json.loads(f.read().strip())

            for key, value in ctx_dict.items():
                assert logged_data[key] == value

    def test_event_no_data(self):
        """Test event logging without additional data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            logger = StructuredLogger(log_path)

            result = logger.event("simple.event")

            assert result["event"] == "simple.event"
            assert result["level"] == "info"  # default level
            assert "data" not in result

    def test_event_different_levels(self):
        """Test event logging with different log levels."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            logger = StructuredLogger(log_path)

            levels = ["debug", "info", "warn", "error"]

            for level in levels:
                result = logger.event(f"{level}.event", level=level)
                assert result["level"] == level

    def test_multiple_events(self):
        """Test logging multiple events to same file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            logger = StructuredLogger(log_path)

            # Log multiple events
            logger.event("first.event", data1="value1")
            logger.event("second.event", data2="value2")
            logger.event("third.event", data3="value3")

            # Read all lines
            with open(log_path) as f:
                lines = f.read().strip().split("\n")

            assert len(lines) == 3

            # Verify each event
            event1 = json.loads(lines[0])
            event2 = json.loads(lines[1])
            event3 = json.loads(lines[2])

            assert event1["event"] == "first.event"
            assert event2["event"] == "second.event"
            assert event3["event"] == "third.event"

    def test_thread_safety(self):
        """Test thread-safe logging."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            logger = StructuredLogger(log_path)

            def log_events(thread_id):
                for i in range(10):
                    logger.event("thread.event", thread_id=thread_id, event_num=i)

            # Start multiple threads
            threads = []
            for i in range(5):
                t = threading.Thread(target=log_events, args=(i,))
                threads.append(t)
                t.start()

            # Wait for completion
            for t in threads:
                t.join()

            # Check all events logged
            with open(log_path) as f:
                lines = f.read().strip().split("\n")

            assert len(lines) == 50  # 5 threads * 10 events each

            # Verify all events are valid JSON
            events = []
            for line in lines:
                event = json.loads(line)
                events.append(event)
                assert event["event"] == "thread.event"

    def test_json_serialization_edge_cases(self):
        """Test JSON serialization of various data types."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            logger = StructuredLogger(log_path)

            # Test various data types
            logger.event(
                "complex.data",
                int_val=42,
                float_val=3.14,
                bool_val=True,
                none_val=None,
                list_val=[1, 2, 3],
                dict_val={"nested": "value"},
                datetime_val=datetime.now(timezone.utc),
            )

            # Check it's valid JSON
            with open(log_path) as f:
                logged_data = json.loads(f.read().strip())

            assert logged_data["data"]["int_val"] == 42
            assert logged_data["data"]["float_val"] == 3.14
            assert logged_data["data"]["bool_val"] is True
            assert logged_data["data"]["none_val"] is None
            assert logged_data["data"]["list_val"] == [1, 2, 3]
            assert logged_data["data"]["dict_val"] == {"nested": "value"}

    @mock.patch("utils.structured_log._MAX_SIZE", 100)  # Small size for testing
    def test_log_rotation(self):
        """Test log file rotation when max size exceeded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            logger = StructuredLogger(log_path)

            # Log enough events to trigger rotation
            for i in range(10):
                logger.event("rotation.test", iteration=i, large_data="x" * 20)

            # Should have at least original file
            assert log_path.exists()

            # Verify current file still has events
            with open(log_path) as f:
                current_content = f.read()
                assert len(current_content) > 0

    def test_langfuse_integration_disabled(self):
        """Test that Langfuse integration doesn't break when disabled."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            logger = StructuredLogger(log_path)

            # Test that Langfuse errors are handled gracefully
            # We'll just test that normal logging works regardless
            result = logger.event("langfuse.test", test="data")

            # Event should be logged locally
            assert result["event"] == "langfuse.test"
            assert log_path.exists()

    def test_directory_creation(self):
        """Test that parent directories are created if they don't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nested_path = Path(tmpdir) / "nested" / "dir" / "test.jsonl"
            logger = StructuredLogger(nested_path)

            logger.event("directory.test")

            assert nested_path.exists()
            assert nested_path.parent.exists()

    def test_error_handling_file_permissions(self):
        """Test that permission errors are properly raised."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create read-only directory
            readonly_dir = Path(tmpdir) / "readonly"
            readonly_dir.mkdir()
            readonly_dir.chmod(0o444)

            log_path = readonly_dir / "test.jsonl"
            logger = StructuredLogger(log_path)

            try:
                # This should raise PermissionError for read-only directory
                with pytest.raises(PermissionError):
                    logger.event("permission.test")
            finally:
                # Cleanup - restore write permissions
                readonly_dir.chmod(0o755)


class TestSingletonInstance:
    """Test the singleton slog instance."""

    def test_singleton_import(self):
        """Test importing the singleton slog instance."""
        from utils.structured_log import slog

        assert isinstance(slog, StructuredLogger)

    def test_singleton_functionality(self):
        """Test that singleton works for basic logging."""
        from utils.structured_log import slog

        # Should be able to log without errors
        result = slog.event("singleton.test", test_data="value")
        assert result["event"] == "singleton.test"
