"""Phase 7.4 — Workflow gate: critic/verifier integration for governed synthesis.

Wires CriticEngine and VerifierEngine into the WorkflowEngine lifecycle:
  - gate_completion(): blocks finalization until verifier approves
  - run_critic_review(): submits a node's output for structured critic review
  - check_replan_signals(): reads pending replan signals for orchestrator routing
  - validate_contract_fields(): shared contract field validation (dict-based)

The gate is the narrowest integration seam between the existing engines
and the workflow completion path.
"""

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agents.blackboard import Blackboard, ChildContract
from agents.critic import CriticEngine, CriticReview, ReplanSignal
from agents.verifier import VerifierEngine, VerificationReport


# ---------------------------------------------------------------------------
# Shared contract field requirements (single source of truth)
# ---------------------------------------------------------------------------

REQUIRED_CONTRACT_FIELDS = frozenset({
    "summary", "files_changed", "verification", "confidence",
})

VALID_CONFIDENCE_VALUES = frozenset({"high", "medium", "low"})


def validate_contract_fields(contract: dict) -> tuple[bool, list[str]]:
    """Validate required fields in a contract dict.

    Returns (valid, errors) tuple.  Fails closed: missing fields = invalid.
    """
    errors: list[str] = []
    for f in REQUIRED_CONTRACT_FIELDS:
        val = contract.get(f)
        if val is None or (isinstance(val, str) and not val.strip()):
            errors.append(f"missing required field: {f}")

    conf = contract.get("confidence", "")
    if isinstance(conf, str) and conf and conf not in VALID_CONFIDENCE_VALUES:
        try:
            fval = float(conf)
            if not (0.0 <= fval <= 1.0):
                errors.append(f"confidence '{conf}' out of range [0.0, 1.0]")
        except (ValueError, TypeError):
            errors.append(f"invalid confidence value: {conf}")

    return (len(errors) == 0, errors)


# ---------------------------------------------------------------------------
# Reroute decision (orchestrator-consumable)
# ---------------------------------------------------------------------------

@dataclass
class RerouteDecision:
    """Deterministic reroute/replan decision for the orchestrator.

    Produced when pending replan signals exist. The orchestrator reads this
    to decide whether to retry, reassign, or halt a workflow node.
    """
    workflow_id: str
    action: str                         # retry | reassign | halt
    affected_node: str
    reason_code: str
    remediation_hint: str
    source_signal_id: str
    source_review_id: str

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Workflow gate
# ---------------------------------------------------------------------------

class WorkflowGate:
    """Governs workflow completion through critic review and verifier approval.

    Usage by the orchestrator:

        gate = WorkflowGate(blackboard)

        # After a coding agent completes work:
        review = gate.run_critic_review(workflow_id, node_id, deliverables, contract)

        if review.verdict == "objection":
            decisions = gate.check_replan_signals(workflow_id)
            # handle reroute ...
            return

        # When all nodes complete, before finalization:
        report = gate.gate_completion(workflow_id, deliverables, contracts, repo_changes)

        if report.verdict != "approved":
            # workflow blocked
            return

        # safe to finalize
    """

    def __init__(self, blackboard: Blackboard | None = None):
        self.bb = blackboard or Blackboard()
        self.critic = CriticEngine(blackboard=self.bb)
        self.verifier = VerifierEngine(blackboard=self.bb)

    # --- Critic review integration ---

    def run_critic_review(
        self,
        workflow_id: str,
        target_node_id: str,
        deliverables: dict[str, Any],
        contract: dict | None = None,
        acceptance_criteria: list[str] | None = None,
        reviewer: str = "critic_001",
    ) -> CriticReview:
        """Submit a node's output for structured critic review.

        If the critic objects, a ReplanSignal is automatically emitted
        and can be consumed via check_replan_signals().
        """
        return self.critic.review(
            workflow_id=workflow_id,
            target_node_id=target_node_id,
            deliverables=deliverables,
            acceptance_criteria=acceptance_criteria,
            contract=contract,
            reviewer=reviewer,
        )

    # --- Replan signal consumption ---

    def check_replan_signals(self, workflow_id: str) -> list[RerouteDecision]:
        """Read pending replan signals and produce deterministic reroute decisions.

        The orchestrator calls this to discover critic objections that require
        workflow rerouting. Each pending signal produces exactly one RerouteDecision.
        """
        pending = self.critic.list_pending_replans(workflow_id)
        decisions: list[RerouteDecision] = []

        for signal in pending:
            # Determine action based on reason code
            action = self._decide_reroute_action(signal)

            decisions.append(RerouteDecision(
                workflow_id=workflow_id,
                action=action,
                affected_node=signal.affected_node,
                reason_code=signal.reason_code,
                remediation_hint=signal.remediation_hint,
                source_signal_id=signal.signal_id,
                source_review_id=signal.source_review_id,
            ))

            # Mark the signal as acknowledged
            self.critic.acknowledge_replan(signal.signal_id)

        return decisions

    def _decide_reroute_action(self, signal: ReplanSignal) -> str:
        """Map a replan signal to a deterministic reroute action.

        Actions:
          - retry:    re-execute the same node (e.g., missing deliverable)
          - reassign: assign to a different agent (e.g., policy violation)
          - halt:     stop the workflow (e.g., unrecoverable)
        """
        # Reason codes that are retryable
        retryable = {
            "empty_deliverable", "contract_field_missing",
            "empty_file", "file_not_found",
        }
        # Reason codes that require reassignment
        reassignable = {
            "no_deliverables",
        }

        if signal.reason_code in retryable:
            return "retry"
        elif signal.reason_code in reassignable:
            return "reassign"
        else:
            return "halt"

    # --- Verifier gate ---

    def gate_completion(
        self,
        workflow_id: str,
        deliverables: dict[str, str | None],
        contracts: list[dict],
        repo_changes: list[str] | None = None,
    ) -> VerificationReport:
        """Gate workflow completion through verifier approval.

        This is the final checkpoint before a workflow can be marked complete.
        For repo-changing paths, maker-checker enforcement is automatic.

        Returns VerificationReport. Caller must check report.verdict == "approved"
        before finalizing.
        """
        # Gather critic reviews for maker-checker validation
        critic_reviews = None
        if repo_changes:
            reviews = self.critic.list_reviews(workflow_id)
            critic_reviews = [r.to_dict() for r in reviews]

        report = self.verifier.verify(
            workflow_id=workflow_id,
            deliverables=deliverables,
            contracts=contracts,
            repo_changes=repo_changes,
            critic_reviews=critic_reviews,
        )

        return report

    # --- Contract validation gate ---

    def validate_contracts_for_completion(
        self, contracts: list[dict],
    ) -> tuple[bool, list[str]]:
        """Validate all contracts meet required structure for gated paths.

        Fails closed: if any contract is missing required fields, the
        entire batch is rejected.
        """
        all_errors: list[str] = []
        for i, contract in enumerate(contracts):
            valid, errors = validate_contract_fields(contract)
            for e in errors:
                all_errors.append(f"contract #{i}: {e}")

        return (len(all_errors) == 0, all_errors)

    # --- Convenience: full governed synthesis check ---

    def is_completion_allowed(
        self,
        workflow_id: str,
        deliverables: dict[str, str | None],
        contracts: list[dict],
        repo_changes: list[str] | None = None,
    ) -> tuple[bool, str]:
        """Check if workflow completion is allowed.

        Returns (allowed, reason) tuple.
        Runs contract validation first, then verifier gate.
        """
        # 1. Contract validation (fail-closed)
        valid, errors = self.validate_contracts_for_completion(contracts)
        if not valid:
            return (False, f"Contract validation failed: {'; '.join(errors)}")

        # 2. Check for pending replan signals (blocking)
        pending_replans = self.critic.list_pending_replans(workflow_id)
        if pending_replans:
            return (False, f"Pending replan signals: {len(pending_replans)} unresolved")

        # 3. Verifier gate
        report = self.gate_completion(
            workflow_id=workflow_id,
            deliverables=deliverables,
            contracts=contracts,
            repo_changes=repo_changes,
        )

        if report.verdict == "approved":
            return (True, "Verifier approved")
        else:
            issues = "; ".join(report.blocking_issues) if report.blocking_issues else report.verdict
            return (False, f"Verifier {report.verdict}: {issues}")
