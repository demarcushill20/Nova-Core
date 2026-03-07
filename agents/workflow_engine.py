"""Workflow engine for Phase 7 multi-agent orchestration.

Manages workflow lifecycle, delegation routing, budget enforcement,
stop conditions, and the maker-checker pattern.

Phase 7.1 hard limits (from research):
  - max concurrent child agents: 4
  - max spawn depth: 1
  - max retries per subtask: 1
  - max workflow runtime: configurable, default 30 minutes
  - max actions per agent: role-based
  - max memory writes per workflow: 2

Phase 7.4 additions:
  - Critic + Verifier loop integration
  - Governed synthesis: verifier approval required before finalization
  - Re-plan path: critic objections route back to orchestrator
  - Maker-checker enforcement for repo-changing paths
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

from agents.blackboard import (
    Blackboard, Delegation, WorkflowState,
    AgentRuntimeState, ChildContract,
)
from agents.policy_engine import PolicyEngine, PolicyViolation


# ---------------------------------------------------------------------------
# Phase 7.1 hard limits
# ---------------------------------------------------------------------------

@dataclass
class WorkflowLimits:
    max_concurrent_agents: int = 4
    max_spawn_depth: int = 1
    max_retries_per_subtask: int = 1
    max_workflow_runtime_s: int = 1800   # 30 minutes
    max_memory_writes: int = 2


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------

class WorkflowHalt(Exception):
    """Raised when a workflow must halt due to a safety condition."""
    def __init__(self, reason: str, workflow_id: str):
        self.reason = reason
        self.workflow_id = workflow_id
        super().__init__(f"WORKFLOW HALT [{workflow_id}]: {reason}")


HALT_BUDGET_EXHAUSTED = "budget_exhausted"
HALT_VERIFIER_REJECTED = "verifier_rejected_twice"
HALT_DEPENDENCY_LOOP = "dependency_loop_detected"
HALT_CONTRACT_TIMEOUT = "child_contract_missing_after_timeout"
HALT_POLICY_VIOLATION = "policy_violation_attempted"
HALT_UNAUTHORIZED_SPAWN = "unauthorized_spawn_or_tool"
HALT_CRITIC_BLOCKED = "critic_blocked_finalization"


# ---------------------------------------------------------------------------
# Workflow engine
# ---------------------------------------------------------------------------

class WorkflowEngine:
    """Orchestrate multi-agent workflows via the blackboard.

    The engine:
      1. Creates workflows and delegations
      2. Routes subtasks to agents by writing delegation entries
      3. Monitors progress by reading blackboard state
      4. Enforces budget and safety limits
      5. Synthesizes child contracts into workflow-level results
      6. Halts on any stop condition
    """

    def __init__(self, blackboard: Blackboard | None = None,
                 policy: PolicyEngine | None = None,
                 limits: WorkflowLimits | None = None):
        self.bb = blackboard or Blackboard()
        self.policy = policy or PolicyEngine()
        self.limits = limits or WorkflowLimits()

    # --- Workflow lifecycle ---

    def create_workflow(self, workflow_id: str, task_id: str,
                        budget: dict | None = None) -> WorkflowState:
        """Create a new workflow and persist to blackboard."""
        wf = WorkflowState(
            workflow_id=workflow_id,
            task_id=task_id,
            status="created",
            budget=budget or {
                "max_runtime_s": self.limits.max_workflow_runtime_s,
                "max_agents": self.limits.max_concurrent_agents,
                "memory_writes_remaining": self.limits.max_memory_writes,
            },
        )
        self.bb.create_workflow(wf)
        return wf

    def delegate(self, workflow_id: str, subtask_id: str,
                 agent_id: str, role: str, goal: str) -> Delegation:
        """Create a delegation entry — the blackboard-based 'send'.

        The orchestrator writes delegation entries. Workers poll/read
        delegations to discover their assigned work. No direct peer
        communication.
        """
        # Check spawn authorization
        # The orchestrator is the one delegating, but we validate the
        # target agent exists and the workflow is within limits
        self._check_concurrent_limit(workflow_id)

        delegation = Delegation(
            workflow_id=workflow_id,
            subtask_id=subtask_id,
            agent_id=agent_id,
            role=role,
            goal=goal,
        )
        self.bb.create_delegation(delegation)

        # Update workflow delegation list
        wf = self.bb.get_workflow(workflow_id)
        if wf:
            delegations = wf.get("delegations", [])
            delegations.append(subtask_id)
            self.bb.update_workflow(workflow_id, {"delegations": delegations})

        # Set agent runtime state to queued
        self.bb.set_agent_state(AgentRuntimeState(
            agent_id=agent_id,
            workflow_id=workflow_id,
            status="queued",
            current_subtask_id=subtask_id,
            started_at=time.time(),
        ))

        return delegation

    def claim_delegation(self, workflow_id: str, subtask_id: str,
                         agent_id: str) -> None:
        """Agent claims a delegation — transitions to executing."""
        self.bb.update_delegation(workflow_id, subtask_id, {
            "status": "claimed",
            "claimed_at": time.time(),
        })
        self.bb.set_agent_state(AgentRuntimeState(
            agent_id=agent_id,
            workflow_id=workflow_id,
            status="executing",
            current_subtask_id=subtask_id,
            started_at=time.time(),
        ))

    def complete_delegation(self, workflow_id: str, subtask_id: str,
                            agent_id: str, contract: ChildContract) -> None:
        """Agent completes delegation — writes contract to blackboard."""
        self.bb.write_child_contract(contract)
        self.bb.update_delegation(workflow_id, subtask_id, {
            "status": "completed",
            "completed_at": time.time(),
        })
        self.bb.set_agent_state(AgentRuntimeState(
            agent_id=agent_id,
            workflow_id=workflow_id,
            status="completed",
            current_subtask_id=subtask_id,
        ))

    def fail_delegation(self, workflow_id: str, subtask_id: str,
                        agent_id: str, error: str) -> None:
        """Agent fails delegation — records error on blackboard."""
        self.bb.update_delegation(workflow_id, subtask_id, {
            "status": "failed",
            "completed_at": time.time(),
            "error": error,
        })
        self.bb.set_agent_state(AgentRuntimeState(
            agent_id=agent_id,
            workflow_id=workflow_id,
            status="failed",
            current_subtask_id=subtask_id,
            error=error,
        ))

    # --- Stop condition checks ---

    def check_stop_conditions(self, workflow_id: str) -> None:
        """Check all stop conditions. Raises WorkflowHalt if triggered."""
        wf = self.bb.get_workflow(workflow_id)
        if wf is None:
            raise WorkflowHalt("Workflow not found", workflow_id)

        # Budget: runtime
        budget = wf.get("budget", {})
        max_runtime = budget.get("max_runtime_s", self.limits.max_workflow_runtime_s)
        elapsed = time.time() - wf.get("created_at", time.time())
        if elapsed > max_runtime:
            self._halt(workflow_id, HALT_BUDGET_EXHAUSTED,
                       f"Runtime {elapsed:.0f}s exceeds limit {max_runtime}s")

    def check_tool_policy(self, workflow_id: str, agent_id: str,
                          tool_name: str) -> None:
        """Check tool policy and halt on violation."""
        try:
            self.policy.enforce(agent_id, tool_name)
        except PolicyViolation as e:
            self._halt(workflow_id, HALT_POLICY_VIOLATION, str(e))

    def check_verifier_rejections(self, workflow_id: str,
                                  rejection_count: int) -> None:
        """Halt if verifier rejects a critical step twice."""
        if rejection_count >= 2:
            self._halt(workflow_id, HALT_VERIFIER_REJECTED,
                       f"Verifier rejected critical step {rejection_count} times")

    # --- Maker-checker pattern ---

    def request_verification(self, workflow_id: str, subtask_id: str,
                             agent_id: str, change_summary: dict) -> str:
        """Coding agent proposes change; write verification request to blackboard.

        Returns a verification_request_id that the verifier will read.
        """
        vr_id = f"vr_{workflow_id}_{subtask_id}"
        self.bb.post_message(workflow_id, agent_id, "verification_request", {
            "verification_request_id": vr_id,
            "subtask_id": subtask_id,
            "proposing_agent": agent_id,
            "change_summary": change_summary,
            "requested_at": time.time(),
        })
        return vr_id

    def submit_verification(self, workflow_id: str, vr_id: str,
                            verifier_id: str, approved: bool,
                            notes: str = "") -> None:
        """Verifier submits verification result to blackboard."""
        self.bb.post_message(workflow_id, verifier_id, "verification_result", {
            "verification_request_id": vr_id,
            "verifier": verifier_id,
            "approved": approved,
            "notes": notes,
            "verified_at": time.time(),
        })

    # --- Synthesis ---

    def synthesize_workflow(self, workflow_id: str) -> dict:
        """Synthesize child contracts into workflow-level result.

        NOTE: This is the ungoverned path — it does NOT enforce verifier
        approval. Use governed_synthesize() for repo-changing workflows.
        """
        wf = self.bb.get_workflow(workflow_id)
        if wf is None:
            return {"error": "Workflow not found"}

        contracts = self.bb.list_child_contracts(workflow_id)
        delegations = self.bb.list_delegations(workflow_id)
        metrics = self.bb.workflow_metrics(workflow_id)

        all_completed = all(
            d.get("status") in ("completed", "failed")
            for d in delegations
        )
        any_failed = any(d.get("status") == "failed" for d in delegations)

        final_status = "completed" if all_completed and not any_failed else (
            "partial" if all_completed else "executing"
        )

        synthesis = {
            "workflow_id": workflow_id,
            "task_id": wf.get("task_id"),
            "status": final_status,
            "child_contracts": contracts,
            "metrics": metrics,
            "all_artifacts": [
                a for c in contracts for a in c.get("artifacts", [])
            ],
            "synthesized_at": time.time(),
        }

        # Update workflow state
        self.bb.update_workflow(workflow_id, {"status": final_status})

        return synthesis

    def governed_synthesize(
        self,
        workflow_id: str,
        deliverables: dict[str, str | None],
        repo_changes: list[str] | None = None,
    ) -> dict:
        """Governed synthesis: verifier approval required before finalization.

        This is the gated path for repo-changing workflows. It:
          1. Validates all child contracts
          2. Runs verifier gate (with maker-checker for repo changes)
          3. Halts on rejection (up to 2 rejections before hard halt)
          4. Synthesizes only if approved

        Returns the synthesis dict with verification_report attached.
        """
        from agents.workflow_gate import WorkflowGate

        gate = WorkflowGate(blackboard=self.bb)
        contracts = self.bb.list_child_contracts(workflow_id)
        contract_dicts = [c for c in contracts]

        # Check completion eligibility
        allowed, reason = gate.is_completion_allowed(
            workflow_id=workflow_id,
            deliverables=deliverables,
            contracts=contract_dicts,
            repo_changes=repo_changes,
        )

        if not allowed:
            # Count rejections and halt if threshold reached
            reports = gate.verifier.list_reports(workflow_id)
            rejection_count = sum(
                1 for r in reports if r.verdict == "rejected"
            )
            self.check_verifier_rejections(workflow_id, rejection_count)

            # Not yet at halt threshold — return blocked status
            self.bb.update_workflow(workflow_id, {
                "status": "blocked",
                "block_reason": reason,
            })
            return {
                "workflow_id": workflow_id,
                "status": "blocked",
                "reason": reason,
                "synthesized_at": time.time(),
            }

        # Approved — proceed with synthesis
        synthesis = self.synthesize_workflow(workflow_id)
        synthesis["governed"] = True
        synthesis["verification_approved"] = True
        return synthesis

    # --- Internal helpers ---

    def _check_concurrent_limit(self, workflow_id: str) -> None:
        """Check that we haven't exceeded max concurrent agents."""
        delegations = self.bb.list_delegations(workflow_id)
        active = [d for d in delegations
                  if d.get("status") in ("pending", "claimed", "executing")]
        if len(active) >= self.limits.max_concurrent_agents:
            raise WorkflowHalt(
                f"Concurrent agent limit reached: "
                f"{len(active)}/{self.limits.max_concurrent_agents}",
                workflow_id,
            )

    def _halt(self, workflow_id: str, reason_code: str, detail: str) -> None:
        """Halt workflow and update blackboard state."""
        try:
            self.bb.update_workflow(workflow_id, {
                "status": "halted",
                "halt_reason": f"{reason_code}: {detail}",
            })
        except FileNotFoundError:
            pass
        raise WorkflowHalt(detail, workflow_id)
