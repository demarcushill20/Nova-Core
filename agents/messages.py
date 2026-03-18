"""P11.2 — Inter-Agent Message Protocol.

Typed message schemas for agent-to-agent communication via the message bus.
All communication is routed through the Orchestrator — no direct agent-to-agent
messaging.

Message types:
  TASK_ASSIGN  — Orchestrator assigns work to an agent
  RESULT       — Agent returns work product
  STATUS       — Agent status update
  ESCALATION   — Agent escalates issue to orchestrator
  CANCEL       — Orchestrator cancels agent work
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MessageType(str, Enum):
    """Types of inter-agent messages."""

    TASK_ASSIGN = "task_assign"
    RESULT = "result"
    STATUS = "status"
    ESCALATION = "escalation"
    CANCEL = "cancel"


class MessagePriority(str, Enum):
    """Message priority levels."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


# Priority ordering for queue sorting
_PRIORITY_ORDER = {
    MessagePriority.CRITICAL: 0,
    MessagePriority.HIGH: 1,
    MessagePriority.NORMAL: 2,
    MessagePriority.LOW: 3,
}


@dataclass
class AgentMessage:
    """Core message exchanged between agents.

    Immutable once created. The message_id is generated automatically.
    """

    from_role: str
    to_role: str
    message_type: MessageType
    payload: dict[str, Any]
    workflow_id: str = ""
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    correlation_id: str = ""  # links request/response pairs
    timestamp: float = field(default_factory=time.time)
    priority: MessagePriority = MessagePriority.NORMAL

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "from_role": self.from_role,
            "to_role": self.to_role,
            "message_type": self.message_type.value,
            "payload": self.payload,
            "workflow_id": self.workflow_id,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "priority": self.priority.value,
        }

    @classmethod
    def from_dict(cls, d: dict) -> AgentMessage:
        return cls(
            message_id=d["message_id"],
            from_role=d["from_role"],
            to_role=d["to_role"],
            message_type=MessageType(d["message_type"]),
            payload=d.get("payload", {}),
            workflow_id=d.get("workflow_id", ""),
            correlation_id=d.get("correlation_id", ""),
            timestamp=d.get("timestamp", time.time()),
            priority=MessagePriority(d.get("priority", "normal")),
        )

    @property
    def priority_order(self) -> int:
        return _PRIORITY_ORDER.get(self.priority, 2)

    def __lt__(self, other: AgentMessage) -> bool:
        """Priority comparison for sorted queues."""
        if self.priority_order != other.priority_order:
            return self.priority_order < other.priority_order
        return self.timestamp < other.timestamp


# ---------------------------------------------------------------------------
# Typed payloads — structured content for each message type
# ---------------------------------------------------------------------------


@dataclass
class TaskAssignment:
    """Payload for TASK_ASSIGN messages."""

    subtask_id: str
    goal: str
    context: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    deadline_seconds: float | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "subtask_id": self.subtask_id,
            "goal": self.goal,
            "context": self.context,
            "constraints": self.constraints,
        }
        if self.deadline_seconds is not None:
            d["deadline_seconds"] = self.deadline_seconds
        return d

    @classmethod
    def from_dict(cls, d: dict) -> TaskAssignment:
        return cls(
            subtask_id=d["subtask_id"],
            goal=d["goal"],
            context=d.get("context", {}),
            constraints=d.get("constraints", {}),
            deadline_seconds=d.get("deadline_seconds"),
        )


@dataclass
class TaskResult:
    """Payload for RESULT messages."""

    subtask_id: str
    success: bool
    output: Any = None
    artifacts: list[str] = field(default_factory=list)  # paths to output files
    confidence: float = 1.0
    error: str | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "subtask_id": self.subtask_id,
            "success": self.success,
            "artifacts": self.artifacts,
            "confidence": self.confidence,
        }
        if self.output is not None:
            d["output"] = self.output
        if self.error is not None:
            d["error"] = self.error
        return d

    @classmethod
    def from_dict(cls, d: dict) -> TaskResult:
        return cls(
            subtask_id=d["subtask_id"],
            success=d["success"],
            output=d.get("output"),
            artifacts=d.get("artifacts", []),
            confidence=d.get("confidence", 1.0),
            error=d.get("error"),
        )


@dataclass
class StatusUpdate:
    """Payload for STATUS messages."""

    agent_id: str
    status: str  # matches AgentStatus values
    progress: float = 0.0  # 0.0 to 1.0
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "status": self.status,
            "progress": self.progress,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d: dict) -> StatusUpdate:
        return cls(
            agent_id=d["agent_id"],
            status=d["status"],
            progress=d.get("progress", 0.0),
            detail=d.get("detail", ""),
        )


@dataclass
class Escalation:
    """Payload for ESCALATION messages."""

    agent_id: str
    reason: str
    severity: str = "warning"  # warning | error | critical
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "reason": self.reason,
            "severity": self.severity,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Escalation:
        return cls(
            agent_id=d["agent_id"],
            reason=d["reason"],
            severity=d.get("severity", "warning"),
            context=d.get("context", {}),
        )


@dataclass
class Cancellation:
    """Payload for CANCEL messages."""

    reason: str
    graceful: bool = True  # if False, force-kill

    def to_dict(self) -> dict:
        return {"reason": self.reason, "graceful": self.graceful}

    @classmethod
    def from_dict(cls, d: dict) -> Cancellation:
        return cls(reason=d["reason"], graceful=d.get("graceful", True))


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def task_assign_message(
    *,
    from_role: str,
    to_role: str,
    workflow_id: str,
    subtask_id: str,
    goal: str,
    context: dict | None = None,
    constraints: dict | None = None,
    deadline_seconds: float | None = None,
    priority: MessagePriority = MessagePriority.NORMAL,
) -> AgentMessage:
    """Create a TASK_ASSIGN message."""
    payload = TaskAssignment(
        subtask_id=subtask_id,
        goal=goal,
        context=context or {},
        constraints=constraints or {},
        deadline_seconds=deadline_seconds,
    )
    return AgentMessage(
        from_role=from_role,
        to_role=to_role,
        message_type=MessageType.TASK_ASSIGN,
        payload=payload.to_dict(),
        workflow_id=workflow_id,
        priority=priority,
    )


def result_message(
    *,
    from_role: str,
    to_role: str,
    workflow_id: str,
    subtask_id: str,
    success: bool,
    output: Any = None,
    artifacts: list[str] | None = None,
    confidence: float = 1.0,
    error: str | None = None,
    correlation_id: str = "",
) -> AgentMessage:
    """Create a RESULT message."""
    payload = TaskResult(
        subtask_id=subtask_id,
        success=success,
        output=output,
        artifacts=artifacts or [],
        confidence=confidence,
        error=error,
    )
    return AgentMessage(
        from_role=from_role,
        to_role=to_role,
        message_type=MessageType.RESULT,
        payload=payload.to_dict(),
        workflow_id=workflow_id,
        correlation_id=correlation_id,
    )


def cancel_message(
    *,
    from_role: str,
    to_role: str,
    workflow_id: str,
    reason: str,
    graceful: bool = True,
) -> AgentMessage:
    """Create a CANCEL message."""
    payload = Cancellation(reason=reason, graceful=graceful)
    return AgentMessage(
        from_role=from_role,
        to_role=to_role,
        message_type=MessageType.CANCEL,
        payload=payload.to_dict(),
        workflow_id=workflow_id,
        priority=MessagePriority.HIGH,
    )
