"""P11 Multi-Agent Runtime — factory and public API.

Usage:
    from agents import create_runtime
    runtime = create_runtime()
    dag = WorkflowDAG(...)
    result = await runtime.execute(dag)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.context_assembler import ContextAssembler
from agents.message_bus import MessageBus
from agents.orchestrator_runtime import (
    DAGNode,
    NodeStatus,
    OrchestratorRuntime,
    WorkflowDAG,
    WorkflowStatus,
)
from agents.runtime_bridge import RuntimeBridge
from agents.runtime_policy import RuntimePolicyEnforcer
from agents.spawner import AgentSpawner, AgentWorkFn
from agents.timeout_manager import RetryConfig, TimeoutManager

BASE = Path("/home/nova/nova-core")

__all__ = [
    "AgentSpawner",
    "AgentWorkFn",
    "ContextAssembler",
    "DAGNode",
    "MessageBus",
    "NodeStatus",
    "OrchestratorRuntime",
    "RetryConfig",
    "RuntimeBridge",
    "RuntimePolicyEnforcer",
    "TimeoutManager",
    "WorkflowDAG",
    "WorkflowStatus",
    "create_runtime",
]


def create_runtime(
    *,
    registry_path: Path | None = None,
    persist_dir: Path | None = None,
    max_concurrent_per_role: int = 1,
    max_total_concurrent: int = 4,
    max_queue_size: int = 100,
    token_budget: int = 8000,
    retry_config: RetryConfig | None = None,
    max_workflow_runtime_s: float = 1800.0,
    use_bridge: bool = True,
    work_fn_factory: Any = None,
) -> OrchestratorRuntime:
    """Assemble the full P11 multi-agent runtime stack.

    Creates and wires: Spawner, MessageBus, TimeoutManager,
    ContextAssembler, RuntimePolicyEnforcer, RuntimeBridge,
    and OrchestratorRuntime.

    All components are optional and degrade gracefully if
    their dependencies are unavailable.
    """
    persist = persist_dir or BASE / "STATE" / "orchestrator_workflows"

    spawner = AgentSpawner(
        registry_path=registry_path,
        max_concurrent_per_role=max_concurrent_per_role,
        max_total_concurrent=max_total_concurrent,
    )
    bus = MessageBus(
        max_queue_size=max_queue_size,
        persist_dir=BASE / "STATE" / "messages",
    )
    timeout_mgr = TimeoutManager(
        retry_config=retry_config or RetryConfig(),
        compensation_dir=BASE / "STATE" / "compensations",
    )
    assembler = ContextAssembler(
        token_budget=token_budget,
        persist_dir=BASE / "STATE" / "handoffs",
    )
    bridge = RuntimeBridge() if use_bridge else None
    enforcer = RuntimePolicyEnforcer(
        persist_dir=BASE / "STATE" / "policy_violations",
        bridge=bridge,
    )

    return OrchestratorRuntime(
        spawner=spawner,
        bus=bus,
        timeout_mgr=timeout_mgr,
        context_assembler=assembler,
        policy_enforcer=enforcer,
        bridge=bridge,
        work_fn_factory=work_fn_factory,
        persist_dir=persist,
        max_workflow_runtime_s=max_workflow_runtime_s,
    )
