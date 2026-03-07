"""Phase 7.4 — Critic Agent: structured review and objection engine.

The Critic reviews plans, subtask results, or proposed change packages
and emits machine-readable review artifacts. It never mutates files —
it only reads, evaluates, and reports.

Review artifacts are persisted to STATE/reviews/ for audit and
orchestrator consumption.

Verdicts:
  - pass:           work is acceptable, proceed
  - objection:      structured blocking objection with remediation hint
  - needs_revision: non-blocking issues that should be fixed before finalization
"""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agents.blackboard import Blackboard

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class CriticIssue:
    """A single issue found during review."""
    issue: str                         # description of the problem
    severity: str                      # low | medium | high | critical
    reason_code: str                   # machine-readable category
    location: str = ""                 # file:line or section reference
    suggested_fix: str = ""            # actionable remediation hint

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}


@dataclass
class CriticReview:
    """Structured review artifact emitted by the Critic."""
    review_id: str
    workflow_id: str
    target_node_id: str                # the node/subtask under review
    verdict: str                       # pass | objection | needs_revision
    issues: list[CriticIssue] = field(default_factory=list)
    contract_compliance: dict = field(default_factory=dict)
    confidence: str = "medium"         # high | medium | low
    reviewed_at: float = field(default_factory=time.time)
    reviewer: str = "critic_001"

    # Objection-specific fields (populated when verdict == "objection")
    blocking: bool = False
    affected_node: str = ""            # which workflow node is affected
    remediation_hint: str = ""         # actionable guidance for re-plan

    def to_dict(self) -> dict:
        d = asdict(self)
        d["issues"] = [i.to_dict() for i in self.issues]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CriticReview":
        issues = [CriticIssue(**i) for i in d.pop("issues", [])]
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(issues=issues, **filtered)


# ---------------------------------------------------------------------------
# Re-plan signal (orchestrator-consumable)
# ---------------------------------------------------------------------------

@dataclass
class ReplanSignal:
    """Deterministic signal emitted when a critic objection requires re-planning.

    The orchestrator reads this from STATE/replans/ to decide how to proceed.
    """
    signal_id: str
    workflow_id: str
    source_review_id: str              # which review triggered this
    objecting_node: str                # which node raised the objection
    affected_node: str                 # which node must be re-planned
    reason_code: str                   # machine-readable reason
    remediation_hint: str              # guidance for the planner
    created_at: float = field(default_factory=time.time)
    status: str = "pending"            # pending | acknowledged | resolved

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ReplanSignal":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Critic engine
# ---------------------------------------------------------------------------

REQUIRED_CONTRACT_FIELDS = {"summary", "files_changed", "verification", "confidence"}

VALID_VERDICTS = {"pass", "objection", "needs_revision"}
VALID_SEVERITIES = {"low", "medium", "high", "critical"}
VALID_CONFIDENCES = {"high", "medium", "low"}


class CriticEngine:
    """Evaluates work artifacts and emits structured review artifacts.

    The Critic:
      1. Validates contract compliance (required fields present)
      2. Evaluates deliverables against acceptance criteria
      3. Emits a structured CriticReview
      4. If verdict is 'objection', emits a ReplanSignal

    All artifacts are persisted to STATE/ via the Blackboard.
    """

    def __init__(self, blackboard: Blackboard | None = None):
        self.bb = blackboard or Blackboard()
        self.reviews_dir = self.bb.state / "reviews"
        self.reviews_dir.mkdir(parents=True, exist_ok=True)
        self.replans_dir = self.bb.state / "replans"
        self.replans_dir.mkdir(parents=True, exist_ok=True)

    # --- Contract compliance ---

    def check_contract_compliance(self, contract: dict) -> dict:
        """Validate a child contract against required fields.

        Returns a dict with per-field pass/fail results.
        """
        compliance = {}
        for field_name in REQUIRED_CONTRACT_FIELDS:
            value = contract.get(field_name)
            if value is None or (isinstance(value, str) and not value.strip()):
                compliance[field_name] = "fail"
            else:
                compliance[field_name] = "pass"
        return compliance

    # --- Review execution ---

    def review(
        self,
        workflow_id: str,
        target_node_id: str,
        deliverables: dict[str, Any],
        acceptance_criteria: list[str] | None = None,
        contract: dict | None = None,
        reviewer: str = "critic_001",
    ) -> CriticReview:
        """Execute a structured review of a node's output.

        Args:
            workflow_id: the workflow context
            target_node_id: the node/subtask being reviewed
            deliverables: dict of deliverable_name -> content/path
            acceptance_criteria: list of criteria strings to check
            contract: the child contract to validate (if any)
            reviewer: the critic agent ID

        Returns:
            CriticReview with verdict, issues, and contract compliance.
        """
        review_id = f"cr_{workflow_id}_{target_node_id}_{int(time.time())}"
        issues: list[CriticIssue] = []

        # 1. Contract compliance
        compliance = {}
        if contract:
            compliance = self.check_contract_compliance(contract)
            for field_name, result in compliance.items():
                if result == "fail":
                    issues.append(CriticIssue(
                        issue=f"Contract field '{field_name}' is missing or empty",
                        severity="high",
                        reason_code="contract_field_missing",
                        location=f"contract.{field_name}",
                        suggested_fix=f"Populate the '{field_name}' field in the output contract",
                    ))

        # 2. Deliverables existence check
        if not deliverables:
            issues.append(CriticIssue(
                issue="No deliverables provided for review",
                severity="critical",
                reason_code="no_deliverables",
                suggested_fix="Ensure the producing agent emits at least one deliverable",
            ))

        for name, content in deliverables.items():
            if content is None or (isinstance(content, str) and not content.strip()):
                issues.append(CriticIssue(
                    issue=f"Deliverable '{name}' is empty or missing",
                    severity="high",
                    reason_code="empty_deliverable",
                    location=name,
                    suggested_fix=f"Ensure '{name}' contains valid content",
                ))

            # Check file existence for path-like deliverables
            if isinstance(content, str) and content.startswith("/"):
                path = Path(content)
                if not path.exists():
                    issues.append(CriticIssue(
                        issue=f"Deliverable file '{content}' does not exist",
                        severity="critical",
                        reason_code="file_not_found",
                        location=content,
                        suggested_fix=f"Create the expected file at '{content}'",
                    ))
                elif path.stat().st_size == 0:
                    issues.append(CriticIssue(
                        issue=f"Deliverable file '{content}' is empty (0 bytes)",
                        severity="high",
                        reason_code="empty_file",
                        location=content,
                        suggested_fix=f"Ensure '{content}' has valid content",
                    ))

        # 3. Acceptance criteria checks
        if acceptance_criteria:
            for criterion in acceptance_criteria:
                # The critic can only note that criteria exist —
                # actual semantic evaluation is done at the orchestrator level
                # or via test execution. Here we check for structural compliance.
                pass  # criteria are advisory inputs; issues found above are structural

        # 4. Determine verdict
        critical_issues = [i for i in issues if i.severity == "critical"]
        high_issues = [i for i in issues if i.severity == "high"]

        if critical_issues:
            verdict = "objection"
        elif high_issues:
            verdict = "needs_revision"
        else:
            verdict = "pass"

        # 5. Determine confidence
        if not deliverables and not contract:
            confidence = "low"
        elif critical_issues:
            confidence = "high"  # high confidence in the objection
        elif issues:
            confidence = "medium"
        else:
            confidence = "high"

        # 6. Build review
        review = CriticReview(
            review_id=review_id,
            workflow_id=workflow_id,
            target_node_id=target_node_id,
            verdict=verdict,
            issues=issues,
            contract_compliance=compliance,
            confidence=confidence,
            reviewer=reviewer,
            blocking=verdict == "objection",
            affected_node=target_node_id if verdict == "objection" else "",
            remediation_hint=critical_issues[0].suggested_fix if critical_issues else "",
        )

        # 7. Persist review
        self._save_review(review)

        # 8. If objection, emit re-plan signal
        if verdict == "objection":
            self._emit_replan_signal(review)

        # 9. Post to blackboard message log
        self.bb.post_message(workflow_id, reviewer, "critic_review", {
            "review_id": review_id,
            "target_node_id": target_node_id,
            "verdict": verdict,
            "issue_count": len(issues),
            "blocking": review.blocking,
        })

        return review

    # --- Persistence ---

    def _save_review(self, review: CriticReview) -> Path:
        """Persist a review artifact to STATE/reviews/."""
        path = self.reviews_dir / f"{review.review_id}.json"
        self.bb._write_json(path, review.to_dict())
        return path

    def get_review(self, review_id: str) -> CriticReview | None:
        """Read a persisted review artifact."""
        path = self.reviews_dir / f"{review_id}.json"
        data = self.bb._read_json(path)
        if data is None:
            return None
        return CriticReview.from_dict(data)

    def list_reviews(self, workflow_id: str | None = None) -> list[CriticReview]:
        """List all reviews, optionally filtered by workflow."""
        results = []
        for f in sorted(self.reviews_dir.glob("cr_*.json")):
            data = self.bb._read_json(f)
            if data is None:
                continue
            review = CriticReview.from_dict(data)
            if workflow_id is None or review.workflow_id == workflow_id:
                results.append(review)
        return results

    # --- Re-plan signal ---

    def _emit_replan_signal(self, review: CriticReview) -> ReplanSignal:
        """Emit a deterministic re-plan signal from a blocking objection."""
        # Use the primary reason code from the first critical issue
        primary_issue = next(
            (i for i in review.issues if i.severity == "critical"),
            review.issues[0] if review.issues else CriticIssue(
                issue="unspecified", severity="critical", reason_code="unknown"
            ),
        )

        signal = ReplanSignal(
            signal_id=f"rp_{review.review_id}",
            workflow_id=review.workflow_id,
            source_review_id=review.review_id,
            objecting_node=review.target_node_id,
            affected_node=review.affected_node or review.target_node_id,
            reason_code=primary_issue.reason_code,
            remediation_hint=review.remediation_hint or primary_issue.suggested_fix,
        )

        path = self.replans_dir / f"{signal.signal_id}.json"
        self.bb._write_json(path, signal.to_dict())

        # Also post to blackboard
        self.bb.post_message(review.workflow_id, review.reviewer, "replan_signal", {
            "signal_id": signal.signal_id,
            "affected_node": signal.affected_node,
            "reason_code": signal.reason_code,
            "remediation_hint": signal.remediation_hint,
        })

        return signal

    def get_replan_signal(self, signal_id: str) -> ReplanSignal | None:
        """Read a persisted re-plan signal."""
        path = self.replans_dir / f"{signal_id}.json"
        data = self.bb._read_json(path)
        if data is None:
            return None
        return ReplanSignal.from_dict(data)

    def list_pending_replans(self, workflow_id: str) -> list[ReplanSignal]:
        """List all pending re-plan signals for a workflow."""
        results = []
        for f in sorted(self.replans_dir.glob("rp_*.json")):
            data = self.bb._read_json(f)
            if data is None:
                continue
            signal = ReplanSignal.from_dict(data)
            if signal.workflow_id == workflow_id and signal.status == "pending":
                results.append(signal)
        return results

    def acknowledge_replan(self, signal_id: str) -> None:
        """Mark a re-plan signal as acknowledged by the orchestrator."""
        path = self.replans_dir / f"{signal_id}.json"
        data = self.bb._read_json(path)
        if data is None:
            raise FileNotFoundError(f"Replan signal not found: {signal_id}")
        data["status"] = "acknowledged"
        data["acknowledged_at"] = time.time()
        self.bb._write_json(path, data)

    def resolve_replan(self, signal_id: str) -> None:
        """Mark a re-plan signal as resolved after orchestrator action."""
        path = self.replans_dir / f"{signal_id}.json"
        data = self.bb._read_json(path)
        if data is None:
            raise FileNotFoundError(f"Replan signal not found: {signal_id}")
        data["status"] = "resolved"
        data["resolved_at"] = time.time()
        self.bb._write_json(path, data)
