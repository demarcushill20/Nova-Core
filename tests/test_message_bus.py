"""Tests for P11.2 — Message Bus and Inter-Agent Protocol.

Covers: typed messages, persistence, correlation IDs, backpressure,
broadcast, validation, drain, ordering, metrics, subscribers.
"""

from __future__ import annotations

import json

import pytest

from agents.message_bus import MessageBus, QueueFull
from agents.messages import (
    AgentMessage,
    Cancellation,
    Escalation,
    MessagePriority,
    MessageType,
    StatusUpdate,
    TaskAssignment,
    TaskResult,
    cancel_message,
    result_message,
    task_assign_message,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bus(tmp_path):
    return MessageBus(max_queue_size=10, persist_dir=tmp_path / "messages")


@pytest.fixture
def msg():
    """A basic test message."""
    return AgentMessage(
        from_role="orchestrator",
        to_role="research",
        message_type=MessageType.TASK_ASSIGN,
        payload={"subtask_id": "s1", "goal": "do stuff"},
        workflow_id="wf-1",
    )


# ---------------------------------------------------------------------------
# Message schema tests
# ---------------------------------------------------------------------------


class TestMessageSchema:
    def test_message_creation(self):
        msg = AgentMessage(
            from_role="orchestrator",
            to_role="coder",
            message_type=MessageType.TASK_ASSIGN,
            payload={"goal": "write code"},
        )
        assert msg.from_role == "orchestrator"
        assert msg.message_type == MessageType.TASK_ASSIGN
        assert len(msg.message_id) == 16

    def test_message_id_unique(self):
        ids = {
            AgentMessage(
                from_role="a",
                to_role="b",
                message_type=MessageType.STATUS,
                payload={},
            ).message_id
            for _ in range(100)
        }
        assert len(ids) == 100

    def test_serialization_roundtrip(self):
        msg = AgentMessage(
            from_role="orchestrator",
            to_role="coder",
            message_type=MessageType.RESULT,
            payload={"subtask_id": "s1", "success": True},
            workflow_id="wf-1",
            correlation_id="corr-123",
            priority=MessagePriority.HIGH,
        )
        d = msg.to_dict()
        restored = AgentMessage.from_dict(d)
        assert restored.message_id == msg.message_id
        assert restored.message_type == MessageType.RESULT
        assert restored.priority == MessagePriority.HIGH
        assert restored.correlation_id == "corr-123"

    def test_priority_ordering(self):
        critical = AgentMessage(
            from_role="a",
            to_role="b",
            message_type=MessageType.CANCEL,
            payload={},
            priority=MessagePriority.CRITICAL,
        )
        normal = AgentMessage(
            from_role="a",
            to_role="b",
            message_type=MessageType.STATUS,
            payload={},
            priority=MessagePriority.NORMAL,
        )
        low = AgentMessage(
            from_role="a",
            to_role="b",
            message_type=MessageType.STATUS,
            payload={},
            priority=MessagePriority.LOW,
        )
        assert critical < normal
        assert normal < low
        assert critical < low

    def test_all_message_types_exist(self):
        expected = {"task_assign", "result", "status", "escalation", "cancel"}
        actual = {mt.value for mt in MessageType}
        assert actual == expected


# ---------------------------------------------------------------------------
# Typed payloads
# ---------------------------------------------------------------------------


class TestTypedPayloads:
    def test_task_assignment(self):
        ta = TaskAssignment(subtask_id="s1", goal="research topic", deadline_seconds=300.0)
        d = ta.to_dict()
        restored = TaskAssignment.from_dict(d)
        assert restored.subtask_id == "s1"
        assert restored.deadline_seconds == 300.0

    def test_task_result_success(self):
        tr = TaskResult(subtask_id="s1", success=True, output="findings", confidence=0.95)
        d = tr.to_dict()
        restored = TaskResult.from_dict(d)
        assert restored.success is True
        assert restored.confidence == 0.95

    def test_task_result_failure(self):
        tr = TaskResult(subtask_id="s1", success=False, error="timeout")
        d = tr.to_dict()
        restored = TaskResult.from_dict(d)
        assert restored.success is False
        assert restored.error == "timeout"

    def test_status_update(self):
        su = StatusUpdate(agent_id="coder_001", status="executing", progress=0.5)
        d = su.to_dict()
        restored = StatusUpdate.from_dict(d)
        assert restored.progress == 0.5

    def test_escalation(self):
        esc = Escalation(agent_id="coder_001", reason="stuck", severity="error")
        d = esc.to_dict()
        restored = Escalation.from_dict(d)
        assert restored.severity == "error"

    def test_cancellation(self):
        cancel = Cancellation(reason="budget exceeded", graceful=False)
        d = cancel.to_dict()
        restored = Cancellation.from_dict(d)
        assert restored.graceful is False


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


class TestFactoryHelpers:
    def test_task_assign_message(self):
        msg = task_assign_message(
            from_role="orchestrator",
            to_role="coder",
            workflow_id="wf-1",
            subtask_id="s1",
            goal="implement feature",
        )
        assert msg.message_type == MessageType.TASK_ASSIGN
        assert msg.payload["subtask_id"] == "s1"
        assert msg.workflow_id == "wf-1"

    def test_result_message(self):
        msg = result_message(
            from_role="coder",
            to_role="orchestrator",
            workflow_id="wf-1",
            subtask_id="s1",
            success=True,
            output="code written",
            correlation_id="corr-1",
        )
        assert msg.message_type == MessageType.RESULT
        assert msg.correlation_id == "corr-1"
        assert msg.payload["success"] is True

    def test_cancel_message(self):
        msg = cancel_message(
            from_role="orchestrator",
            to_role="coder",
            workflow_id="wf-1",
            reason="timeout",
        )
        assert msg.message_type == MessageType.CANCEL
        assert msg.priority == MessagePriority.HIGH


# ---------------------------------------------------------------------------
# Message bus — send/receive
# ---------------------------------------------------------------------------


class TestMessageBusSendReceive:
    @pytest.mark.asyncio
    async def test_send_and_receive(self, bus, msg):
        await bus.send(msg)
        received = await bus.receive("research")
        assert received is not None
        assert received.message_id == msg.message_id

    @pytest.mark.asyncio
    async def test_receive_empty(self, bus):
        result = await bus.receive("research")
        assert result is None

    @pytest.mark.asyncio
    async def test_fifo_ordering(self, bus):
        for i in range(3):
            msg = AgentMessage(
                from_role="o",
                to_role="r",
                message_type=MessageType.STATUS,
                payload={"seq": i},
            )
            await bus.send(msg)

        results = []
        for _ in range(3):
            m = await bus.receive("r")
            results.append(m.payload["seq"])
        assert results == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_priority_ordering(self, bus):
        low = AgentMessage(
            from_role="o",
            to_role="r",
            message_type=MessageType.STATUS,
            payload={"p": "low"},
            priority=MessagePriority.LOW,
        )
        critical = AgentMessage(
            from_role="o",
            to_role="r",
            message_type=MessageType.CANCEL,
            payload={"p": "critical"},
            priority=MessagePriority.CRITICAL,
        )
        normal = AgentMessage(
            from_role="o",
            to_role="r",
            message_type=MessageType.STATUS,
            payload={"p": "normal"},
            priority=MessagePriority.NORMAL,
        )

        # Send in low, critical, normal order
        await bus.send(low)
        await bus.send(critical)
        await bus.send(normal)

        # Receive should be: critical, normal, low
        r1 = await bus.receive("r")
        r2 = await bus.receive("r")
        r3 = await bus.receive("r")
        assert r1.payload["p"] == "critical"
        assert r2.payload["p"] == "normal"
        assert r3.payload["p"] == "low"

    @pytest.mark.asyncio
    async def test_receive_with_timeout(self, bus, msg):
        await bus.send(msg)
        received = await bus.receive("research", timeout=1.0)
        assert received is not None

    @pytest.mark.asyncio
    async def test_receive_timeout_empty(self, bus):
        received = await bus.receive("research", timeout=0.05)
        assert received is None

    @pytest.mark.asyncio
    async def test_receive_all(self, bus):
        for i in range(5):
            await bus.send(
                AgentMessage(
                    from_role="o",
                    to_role="r",
                    message_type=MessageType.STATUS,
                    payload={"i": i},
                )
            )
        msgs = await bus.receive_all("r", max_messages=3)
        assert len(msgs) == 3
        # 2 remaining
        assert bus.queue_depth("r") == 2

    @pytest.mark.asyncio
    async def test_send_many(self, bus):
        messages = [
            AgentMessage(
                from_role="o",
                to_role="r",
                message_type=MessageType.STATUS,
                payload={"i": i},
            )
            for i in range(5)
        ]
        sent = await bus.send_many(messages)
        assert sent == 5
        assert bus.queue_depth("r") == 5


# ---------------------------------------------------------------------------
# Backpressure
# ---------------------------------------------------------------------------


class TestBackpressure:
    @pytest.mark.asyncio
    async def test_queue_full(self, bus):
        # Fill the queue (max=10)
        for i in range(10):
            await bus.send(
                AgentMessage(
                    from_role="o",
                    to_role="r",
                    message_type=MessageType.STATUS,
                    payload={"i": i},
                )
            )
        # 11th should fail
        with pytest.raises(QueueFull):
            await bus.send(
                AgentMessage(
                    from_role="o",
                    to_role="r",
                    message_type=MessageType.STATUS,
                    payload={"i": 10},
                )
            )

    @pytest.mark.asyncio
    async def test_queue_recovers_after_receive(self, bus):
        for i in range(10):
            await bus.send(
                AgentMessage(
                    from_role="o",
                    to_role="r",
                    message_type=MessageType.STATUS,
                    payload={"i": i},
                )
            )
        # Receive one to make space
        await bus.receive("r")
        # Now can send again
        await bus.send(
            AgentMessage(
                from_role="o",
                to_role="r",
                message_type=MessageType.STATUS,
                payload={"i": 10},
            )
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    @pytest.mark.asyncio
    async def test_messages_persisted(self, bus, tmp_path):
        msg = task_assign_message(
            from_role="orchestrator",
            to_role="coder",
            workflow_id="wf-persist",
            subtask_id="s1",
            goal="test",
        )
        await bus.send(msg)
        log_file = tmp_path / "messages" / "wf-persist" / "messages.jsonl"
        assert log_file.exists()
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["message_id"] == msg.message_id
        assert data["_direction"] == "sent"

    @pytest.mark.asyncio
    async def test_load_messages(self, bus):
        msg1 = task_assign_message(
            from_role="o",
            to_role="c",
            workflow_id="wf-load",
            subtask_id="s1",
            goal="a",
        )
        msg2 = result_message(
            from_role="c",
            to_role="o",
            workflow_id="wf-load",
            subtask_id="s1",
            success=True,
        )
        await bus.send(msg1)
        await bus.send(msg2)
        loaded = bus.load_messages("wf-load")
        assert len(loaded) >= 2
        ids = {m.message_id for m in loaded}
        assert msg1.message_id in ids

    @pytest.mark.asyncio
    async def test_load_nonexistent_workflow(self, bus):
        loaded = bus.load_messages("wf-nonexistent")
        assert loaded == []


# ---------------------------------------------------------------------------
# Subscribers
# ---------------------------------------------------------------------------


class TestSubscribers:
    @pytest.mark.asyncio
    async def test_subscriber_called(self, bus):
        received_messages = []

        async def handler(msg):
            received_messages.append(msg)

        bus.subscribe("coder", handler)
        msg = task_assign_message(
            from_role="o",
            to_role="coder",
            workflow_id="wf-1",
            subtask_id="s1",
            goal="code",
        )
        await bus.send(msg)
        assert len(received_messages) == 1
        assert received_messages[0].message_id == msg.message_id

    @pytest.mark.asyncio
    async def test_unsubscribe(self, bus):
        received = []

        async def handler(msg):
            received.append(msg)

        bus.subscribe("coder", handler)
        bus.unsubscribe("coder", handler)

        await bus.send(
            AgentMessage(
                from_role="o",
                to_role="coder",
                message_type=MessageType.STATUS,
                payload={},
            )
        )
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_subscriber_error_does_not_block(self, bus):
        async def bad_handler(msg):
            raise ValueError("subscriber error")

        bus.subscribe("coder", bad_handler)
        # Should not raise
        await bus.send(
            AgentMessage(
                from_role="o",
                to_role="coder",
                message_type=MessageType.STATUS,
                payload={},
            )
        )


# ---------------------------------------------------------------------------
# Drain / Clear
# ---------------------------------------------------------------------------


class TestDrainClear:
    @pytest.mark.asyncio
    async def test_drain_role(self, bus):
        for i in range(5):
            await bus.send(
                AgentMessage(
                    from_role="o",
                    to_role="r",
                    message_type=MessageType.STATUS,
                    payload={"i": i},
                )
            )
        msgs = await bus.drain("r")
        assert len(msgs) == 5
        assert bus.queue_depth("r") == 0

    @pytest.mark.asyncio
    async def test_drain_all(self, bus):
        await bus.send(AgentMessage(from_role="o", to_role="r", message_type=MessageType.STATUS, payload={}))
        await bus.send(AgentMessage(from_role="o", to_role="c", message_type=MessageType.STATUS, payload={}))
        msgs = await bus.drain()
        assert len(msgs) == 2

    @pytest.mark.asyncio
    async def test_clear(self, bus):
        for i in range(5):
            await bus.send(
                AgentMessage(
                    from_role="o",
                    to_role="r",
                    message_type=MessageType.STATUS,
                    payload={"i": i},
                )
            )
        count = await bus.clear("r")
        assert count == 5
        assert bus.queue_depth("r") == 0


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    @pytest.mark.asyncio
    async def test_metrics(self, bus):
        msg = AgentMessage(
            from_role="o",
            to_role="r",
            message_type=MessageType.STATUS,
            payload={},
        )
        await bus.send(msg)
        await bus.receive("r")

        metrics = bus.get_metrics()
        assert metrics["send_count"] == 1
        assert metrics["receive_count"] == 1
        assert metrics["send_by_type"]["status"] == 1
        assert metrics["avg_latency_s"] >= 0
        assert "queue_depths" in metrics

    @pytest.mark.asyncio
    async def test_queue_depth(self, bus):
        assert bus.queue_depth("r") == 0
        await bus.send(
            AgentMessage(
                from_role="o",
                to_role="r",
                message_type=MessageType.STATUS,
                payload={},
            )
        )
        assert bus.queue_depth("r") == 1
        depths = bus.all_queue_depths()
        assert depths["r"] == 1
