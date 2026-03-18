"""P11 Integration Bridge — connects P11 runtime to existing NovaCore subsystems.

Bridges the gap between P11's standalone modules and the existing:
  - Blackboard (atomic state I/O)
  - CoordinationLayer (leases, checkpoints, node states)
  - PolicyEngine (5-layer policy enforcement)
  - SimpleCircuitBreaker (mature circuit breakers)

Usage:
    from agents.runtime_bridge import RuntimeBridge
    bridge = RuntimeBridge()  # auto-discovers existing subsystems
    runtime = OrchestratorRuntime(spawner, bus, bridge=bridge)

The bridge is optional — P11 modules remain fully functional without it
for testing and standalone operation.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _try_import_blackboard():
    """Lazy import to avoid hard dependency."""
    try:
        from agents.blackboard import AgentRuntimeState, Blackboard, WorkflowState

        return Blackboard, AgentRuntimeState, WorkflowState
    except ImportError:
        return None, None, None


def _try_import_coordination():
    try:
        from agents.coordination import CoordinationLayer

        return CoordinationLayer
    except ImportError:
        return None


def _try_import_policy_engine():
    try:
        from agents.policy_engine import PolicyEngine

        return PolicyEngine
    except ImportError:
        return None


def _try_import_circuit_breaker():
    try:
        from agents.circuit_breakers import SimpleCircuitBreaker

        return SimpleCircuitBreaker
    except ImportError:
        return None


class RuntimeBridge:
    """Connects P11 runtime modules to existing NovaCore subsystems.

    All integration is optional — if a subsystem is unavailable, the bridge
    gracefully degrades and logs a warning.
    """

    def __init__(
        self,
        blackboard=None,
        coordination=None,
        policy_engine=None,
    ):
        # Auto-discover if not provided
        Blackboard, AgentRuntimeState, WorkflowState = _try_import_blackboard()
        CoordinationLayer = _try_import_coordination()
        PolicyEngine = _try_import_policy_engine()

        self._blackboard = blackboard
        self._AgentRuntimeState = AgentRuntimeState
        self._WorkflowState = WorkflowState
        self._coordination = coordination
        self._policy_engine = policy_engine

        if self._blackboard is None and Blackboard is not None:
            try:
                self._blackboard = Blackboard()
                logger.info("RuntimeBridge: connected to Blackboard")
            except Exception as e:
                logger.warning("RuntimeBridge: Blackboard unavailable: %s", e)

        if self._coordination is None and CoordinationLayer is not None and self._blackboard is not None:
            try:
                self._coordination = CoordinationLayer(blackboard=self._blackboard)
                logger.info("RuntimeBridge: connected to CoordinationLayer")
            except Exception as e:
                logger.warning("RuntimeBridge: CoordinationLayer unavailable: %s", e)

        if self._policy_engine is None and PolicyEngine is not None:
            try:
                self._policy_engine = PolicyEngine()
                logger.info("RuntimeBridge: connected to PolicyEngine")
            except Exception as e:
                logger.warning("RuntimeBridge: PolicyEngine unavailable: %s", e)

    @property
    def has_blackboard(self) -> bool:
        return self._blackboard is not None

    @property
    def has_coordination(self) -> bool:
        return self._coordination is not None

    @property
    def has_policy_engine(self) -> bool:
        return self._policy_engine is not None

    # -------------------------------------------------------------------
    # Blackboard state sync
    # -------------------------------------------------------------------

    def sync_agent_state(
        self,
        agent_id: str,
        workflow_id: str | None,
        status: str,
        action_count: int = 0,
        error: str | None = None,
    ) -> None:
        """Write agent state to the Blackboard so existing observability can see it."""
        if self._blackboard is None or self._AgentRuntimeState is None:
            return
        try:
            import time

            state = self._AgentRuntimeState(
                agent_id=agent_id,
                workflow_id=workflow_id,
                status=status,
                action_count=action_count,
                error=error,
                updated_at=time.time(),
            )
            self._blackboard.set_agent_state(state)
        except Exception as e:
            logger.warning("Failed to sync agent state to blackboard: %s", e)

    def sync_workflow_state(
        self,
        workflow_id: str,
        status: str,
        halt_reason: str | None = None,
    ) -> None:
        """Update workflow status on the Blackboard."""
        if self._blackboard is None:
            return
        try:
            updates: dict[str, Any] = {"status": status}
            if halt_reason:
                updates["halt_reason"] = halt_reason
            self._blackboard.update_workflow(workflow_id, updates)
        except Exception as e:
            logger.warning("Failed to sync workflow state to blackboard: %s", e)

    # -------------------------------------------------------------------
    # CoordinationLayer — lease-based node coordination
    # -------------------------------------------------------------------

    def claim_node(self, workflow_id: str, node_id: str, agent_id: str) -> bool:
        """Claim a node via CoordinationLayer lease. Returns True if claimed."""
        if self._coordination is None:
            return True  # no coordination → always allowed
        try:
            self._coordination.claim_node(workflow_id, node_id, agent_id)
            return True
        except Exception as e:
            logger.warning("Failed to claim node %s/%s: %s", workflow_id, node_id, e)
            return False

    def complete_node(
        self,
        workflow_id: str,
        node_id: str,
        agent_id: str,
        output_ref: str | None = None,
    ) -> None:
        """Mark a node as completed in the CoordinationLayer."""
        if self._coordination is None:
            return
        try:
            self._coordination.complete_node(workflow_id, node_id, agent_id, output_ref)
        except Exception as e:
            logger.warning("Failed to complete node %s/%s: %s", workflow_id, node_id, e)

    def fail_node(self, workflow_id: str, node_id: str, agent_id: str, error: str) -> None:
        """Mark a node as failed in the CoordinationLayer."""
        if self._coordination is None:
            return
        try:
            self._coordination.fail_node(workflow_id, node_id, agent_id, error)
        except Exception as e:
            logger.warning("Failed to fail node %s/%s: %s", workflow_id, node_id, e)

    def save_checkpoint(self, workflow_id: str, checkpoint: dict) -> None:
        """Save a workflow checkpoint for crash recovery."""
        if self._coordination is None:
            return
        try:
            self._coordination.save_checkpoint(workflow_id, checkpoint)
        except Exception as e:
            logger.warning("Failed to save checkpoint for %s: %s", workflow_id, e)

    # -------------------------------------------------------------------
    # PolicyEngine delegation
    # -------------------------------------------------------------------

    def check_tool_access(self, agent_id: str, tool_name: str) -> bool:
        """Check tool access via the 5-layer PolicyEngine.

        Returns True if allowed. Falls back to True if no policy engine.
        """
        if self._policy_engine is None:
            return True
        try:
            decision = self._policy_engine.check_tool_access(agent_id, tool_name)
            return decision.allowed
        except Exception:
            # PolicyViolation from policy_engine.py
            return False

    # -------------------------------------------------------------------
    # Metrics
    # -------------------------------------------------------------------

    def get_status(self) -> dict[str, bool]:
        """Report which integrations are active."""
        return {
            "blackboard": self.has_blackboard,
            "coordination": self.has_coordination,
            "policy_engine": self.has_policy_engine,
        }
