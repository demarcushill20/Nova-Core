"""P11.2 — Message Bus for Inter-Agent Communication.

Provides a centralized async message bus that routes typed messages between
agents. All messages flow through the bus — no direct agent-to-agent
communication.

Features:
  - Per-role queues with bounded capacity (backpressure)
  - Priority ordering (CRITICAL > HIGH > NORMAL > LOW)
  - Message persistence to STATE/messages/
  - Correlation tracking (request/response pairs)
  - Metrics: send/receive counts, latency, queue depths
  - Drain support for graceful shutdown
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from agents.messages import AgentMessage
from agents.validation import validate_id

logger = logging.getLogger(__name__)

BASE = Path("/home/nova/nova-core")
STATE = BASE / "STATE"
MESSAGES_DIR = STATE / "messages"


class MessageBusError(Exception):
    """Base error for message bus operations."""


class QueueFull(MessageBusError):
    """Queue is at capacity — backpressure."""


class MessageBus:
    """Centralized message routing for multi-agent workflows.

    Each role gets a bounded asyncio.PriorityQueue. Messages are routed
    by to_role. The bus persists messages to disk and tracks metrics.
    """

    def __init__(
        self,
        max_queue_size: int = 100,
        persist_dir: Path | None = None,
    ):
        self._max_queue_size = max_queue_size
        self._queues: dict[str, asyncio.PriorityQueue] = {}
        self._persist_dir = persist_dir or MESSAGES_DIR
        self._lock = asyncio.Lock()

        # Metrics
        self._send_count = 0
        self._receive_count = 0
        self._send_by_type: dict[str, int] = {}
        self._latencies: list[float] = []
        self._started_at = time.time()

        # Subscribers: role → list of async callbacks
        self._subscribers: dict[str, list] = {}

    async def _get_queue(self, role: str) -> asyncio.PriorityQueue:
        """Get or create a priority queue for a role.

        Uses _lock to prevent two concurrent coroutines from creating
        duplicate queues for the same role.
        """
        if role in self._queues:
            return self._queues[role]
        async with self._lock:
            # Double-check after acquiring lock
            if role not in self._queues:
                self._queues[role] = asyncio.PriorityQueue(maxsize=self._max_queue_size)
            return self._queues[role]

    # -------------------------------------------------------------------
    # Send
    # -------------------------------------------------------------------

    async def send(self, message: AgentMessage) -> None:
        """Send a message to the target role's queue.

        Raises:
            QueueFull: If the target queue is at capacity.
        """
        queue = await self._get_queue(message.to_role)

        if queue.full():
            raise QueueFull(f"Queue for role '{message.to_role}' is full ({queue.qsize()}/{self._max_queue_size})")

        # Priority queue needs a comparable item
        await queue.put((message.priority_order, message.timestamp, message))

        # Metrics
        self._send_count += 1
        mt = message.message_type.value
        self._send_by_type[mt] = self._send_by_type.get(mt, 0) + 1

        # Persist
        self._persist_message(message, direction="sent")

        # Notify subscribers
        if message.to_role in self._subscribers:
            for callback in self._subscribers[message.to_role]:
                try:
                    await callback(message)
                except Exception as e:
                    logger.error("Subscriber error for role %s: %s", message.to_role, e)

        logger.debug(
            "Sent %s from %s to %s (msg_id=%s, wf=%s)",
            message.message_type.value,
            message.from_role,
            message.to_role,
            message.message_id,
            message.workflow_id,
        )

    async def send_many(self, messages: list[AgentMessage]) -> int:
        """Send multiple messages. Returns count successfully sent."""
        sent = 0
        for msg in messages:
            try:
                await self.send(msg)
                sent += 1
            except QueueFull:
                logger.warning("Queue full for %s, dropped message %s", msg.to_role, msg.message_id)
        return sent

    # -------------------------------------------------------------------
    # Receive
    # -------------------------------------------------------------------

    async def receive(self, role: str, timeout: float | None = None) -> AgentMessage | None:
        """Receive the next message for a role.

        Args:
            role: The target role to receive for.
            timeout: Max seconds to wait. None = non-blocking check.

        Returns:
            The next message, or None if no message available within timeout.
        """
        queue = await self._get_queue(role)

        try:
            if timeout is None:
                if queue.empty():
                    return None
                _, _, message = queue.get_nowait()
            else:
                _, _, message = await asyncio.wait_for(queue.get(), timeout=timeout)
        except (asyncio.QueueEmpty, asyncio.TimeoutError):
            return None

        # Metrics
        self._receive_count += 1
        latency = time.time() - message.timestamp
        self._latencies.append(latency)
        # Keep latency list bounded
        if len(self._latencies) > 1000:
            self._latencies = self._latencies[-500:]

        self._persist_message(message, direction="received")
        return message

    async def receive_all(self, role: str, max_messages: int = 50) -> list[AgentMessage]:
        """Drain up to max_messages from a role's queue (non-blocking)."""
        messages = []
        for _ in range(max_messages):
            msg = await self.receive(role)
            if msg is None:
                break
            messages.append(msg)
        return messages

    # -------------------------------------------------------------------
    # Subscribe
    # -------------------------------------------------------------------

    def subscribe(self, role: str, callback) -> None:
        """Register an async callback for messages to a role.

        The callback receives the AgentMessage and is called on send().
        """
        if role not in self._subscribers:
            self._subscribers[role] = []
        self._subscribers[role].append(callback)

    def unsubscribe(self, role: str, callback) -> None:
        """Remove a subscriber callback."""
        if role in self._subscribers:
            self._subscribers[role] = [cb for cb in self._subscribers[role] if cb is not callback]

    # -------------------------------------------------------------------
    # Query
    # -------------------------------------------------------------------

    def queue_depth(self, role: str) -> int:
        """Current queue depth for a role."""
        if role not in self._queues:
            return 0
        return self._queues[role].qsize()

    def all_queue_depths(self) -> dict[str, int]:
        """Queue depths for all roles."""
        return {role: q.qsize() for role, q in self._queues.items()}

    def pending_for(self, role: str) -> int:
        """Alias for queue_depth."""
        return self.queue_depth(role)

    # -------------------------------------------------------------------
    # Drain / Shutdown
    # -------------------------------------------------------------------

    async def drain(self, role: str | None = None) -> list[AgentMessage]:
        """Drain all messages from a role's queue (or all queues).

        Used for graceful shutdown or testing.
        """
        messages = []
        roles = [role] if role else list(self._queues.keys())
        for r in roles:
            while not self._queues[r].empty():
                try:
                    _, _, msg = self._queues[r].get_nowait()
                    messages.append(msg)
                except asyncio.QueueEmpty:
                    break
        return messages

    async def clear(self, role: str | None = None) -> int:
        """Clear all messages from a role's queue (or all queues).

        Returns count of messages cleared.
        """
        msgs = await self.drain(role)
        return len(msgs)

    # -------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------

    def _persist_message(self, message: AgentMessage, direction: str) -> None:
        """Append message to a per-workflow JSONL file."""
        try:
            wf_id = message.workflow_id or "unscoped"
            validate_id(wf_id, "workflow_id")
            wf_dir = self._persist_dir / wf_id
            wf_dir.mkdir(parents=True, exist_ok=True)
            log_file = wf_dir / "messages.jsonl"
            entry = message.to_dict()
            entry["_direction"] = direction
            entry["_logged_at"] = time.time()
            with open(log_file, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as e:
            logger.error("Failed to persist message %s: %s", message.message_id, e)

    def load_messages(self, workflow_id: str) -> list[AgentMessage]:
        """Load persisted messages for a workflow (for replay/debugging)."""
        log_file = self._persist_dir / workflow_id / "messages.jsonl"
        if not log_file.exists():
            return []
        messages = []
        for line in log_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                d.pop("_direction", None)
                d.pop("_logged_at", None)
                messages.append(AgentMessage.from_dict(d))
            except (json.JSONDecodeError, KeyError):
                continue
        return messages

    # -------------------------------------------------------------------
    # Metrics
    # -------------------------------------------------------------------

    def get_metrics(self) -> dict[str, Any]:
        """Return bus metrics for observability."""
        avg_latency = sum(self._latencies) / len(self._latencies) if self._latencies else 0.0
        return {
            "send_count": self._send_count,
            "receive_count": self._receive_count,
            "send_by_type": dict(self._send_by_type),
            "avg_latency_s": avg_latency,
            "max_latency_s": max(self._latencies) if self._latencies else 0.0,
            "queue_depths": self.all_queue_depths(),
            "uptime_s": time.time() - self._started_at,
        }
