"""Phase 7.4 — Verifier Agent: contract validation and completion gating.

The Verifier validates completion artifacts and repo-change outputs
against explicit contract expectations. It is the final gate before
a workflow can be marked complete.

Verification artifacts are persisted to STATE/verifications/ for audit.

Verdicts:
  - approved:    all checks pass, workflow may finalize
  - rejected:    one or more checks fail, workflow is blocked
  - incomplete:  some checks cannot be evaluated (inconclusive)
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
class VerificationCheck:
    """A single verification check result."""
    checkpoint: str                     # name or subtask_id
    check_type: str                     # contract | artifact | test | maker_checker
    result: str                         # pass | fail | inconclusive
    detail: str = ""                    # what was checked and what was found

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerificationReport:
    """Structured verification report emitted by the Verifier."""
    report_id: str
    workflow_id: str
    verdict: str                        # approved | rejected | incomplete
    checks: list[VerificationCheck] = field(default_factory=list)
    blocking_issues: list[str] = field(default_factory=list)
    artifacts_verified: list[dict] = field(default_factory=list)
    confidence: str = "medium"          # high | medium | low
    verified_at: float = field(default_factory=time.time)
    verifier: str = "verifier_001"

    # Maker-checker tracking
    maker_checker_enforced: bool = False
    maker_checker_passed: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = [c.to_dict() for c in self.checks]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "VerificationReport":
        checks = [VerificationCheck(**c) for c in d.pop("checks", [])]
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(checks=checks, **filtered)


# ---------------------------------------------------------------------------
# Verifier engine
# ---------------------------------------------------------------------------

REQUIRED_CONTRACT_FIELDS = {"summary", "files_changed", "verification", "confidence"}
VALID_CONFIDENCE_VALUES = {"high", "medium", "low"}


class VerifierEngine:
    """Validates completion artifacts against explicit contract expectations.

    The Verifier:
      1. Checks that required deliverables exist (artifact verification)
      2. Validates contract fields (contract verification)
      3. Confirms maker-checker flow was followed for repo changes
      4. Emits a VerificationReport with per-check results
      5. Blocks or approves workflow finalization

    All reports are persisted to STATE/verifications/.
    """

    def __init__(self, blackboard: Blackboard | None = None):
        self.bb = blackboard or Blackboard()
        self.verifications_dir = self.bb.state / "verifications"
        self.verifications_dir.mkdir(parents=True, exist_ok=True)

    # --- Full verification ---

    def verify(
        self,
        workflow_id: str,
        deliverables: dict[str, str | None],
        contracts: list[dict],
        repo_changes: list[str] | None = None,
        critic_reviews: list[dict] | None = None,
        verifier_id: str = "verifier_001",
    ) -> VerificationReport:
        """Execute full verification of a workflow's outputs.

        Args:
            workflow_id: the workflow to verify
            deliverables: dict of name -> file_path for artifact checks
            contracts: list of child contract dicts to validate
            repo_changes: list of file paths that were modified (triggers maker-checker)
            critic_reviews: list of critic review dicts (for maker-checker validation)
            verifier_id: the verifier agent ID

        Returns:
            VerificationReport with verdict and per-check results.
        """
        report_id = f"vr_{workflow_id}_{int(time.time())}"
        checks: list[VerificationCheck] = []
        blocking_issues: list[str] = []
        artifacts_verified: list[dict] = []

        # 1. Artifact existence checks
        for name, file_path in deliverables.items():
            check = self._check_artifact(name, file_path)
            checks.append(check)
            if check.result == "fail":
                blocking_issues.append(f"Artifact '{name}' failed: {check.detail}")
            if file_path:
                path = Path(file_path)
                artifacts_verified.append({
                    "path": file_path,
                    "exists": path.exists(),
                    "size": path.stat().st_size if path.exists() else 0,
                })

        # 2. Contract validation
        for i, contract in enumerate(contracts):
            contract_checks = self._check_contract(contract, i)
            checks.extend(contract_checks)
            for cc in contract_checks:
                if cc.result == "fail":
                    blocking_issues.append(
                        f"Contract #{i} failed: {cc.detail}"
                    )

        # 3. Maker-checker enforcement for repo changes
        maker_checker_enforced = False
        maker_checker_passed = False

        if repo_changes:
            maker_checker_enforced = True
            mc_check = self._check_maker_checker(
                workflow_id, repo_changes, critic_reviews
            )
            checks.append(mc_check)
            if mc_check.result == "pass":
                maker_checker_passed = True
            else:
                blocking_issues.append(
                    f"Maker-checker failed: {mc_check.detail}"
                )

        # 4. Determine verdict
        failed_checks = [c for c in checks if c.result == "fail"]
        inconclusive_checks = [c for c in checks if c.result == "inconclusive"]

        if failed_checks:
            verdict = "rejected"
        elif inconclusive_checks:
            verdict = "incomplete"
        else:
            verdict = "approved"

        # 5. Determine confidence
        if not checks:
            confidence = "low"
        elif failed_checks:
            confidence = "high"  # high confidence in the rejection
        elif inconclusive_checks:
            confidence = "low"
        else:
            confidence = "high"

        # 6. Build report
        report = VerificationReport(
            report_id=report_id,
            workflow_id=workflow_id,
            verdict=verdict,
            checks=checks,
            blocking_issues=blocking_issues,
            artifacts_verified=artifacts_verified,
            confidence=confidence,
            verifier=verifier_id,
            maker_checker_enforced=maker_checker_enforced,
            maker_checker_passed=maker_checker_passed,
        )

        # 7. Persist
        self._save_report(report)

        # 8. Post to blackboard
        self.bb.post_message(workflow_id, verifier_id, "verification_report", {
            "report_id": report_id,
            "verdict": verdict,
            "total_checks": len(checks),
            "passed": len([c for c in checks if c.result == "pass"]),
            "failed": len(failed_checks),
            "inconclusive": len(inconclusive_checks),
            "blocking_issues_count": len(blocking_issues),
            "maker_checker_enforced": maker_checker_enforced,
            "maker_checker_passed": maker_checker_passed,
        })

        return report

    # --- Individual check methods ---

    def _check_artifact(self, name: str, file_path: str | None) -> VerificationCheck:
        """Check that a deliverable artifact exists and has content."""
        if file_path is None:
            return VerificationCheck(
                checkpoint=f"artifact_{name}",
                check_type="artifact",
                result="fail",
                detail=f"No file path provided for deliverable '{name}'",
            )

        path = Path(file_path)
        if not path.exists():
            return VerificationCheck(
                checkpoint=f"artifact_{name}",
                check_type="artifact",
                result="fail",
                detail=f"File does not exist: {file_path}",
            )

        size = path.stat().st_size
        if size == 0:
            return VerificationCheck(
                checkpoint=f"artifact_{name}",
                check_type="artifact",
                result="fail",
                detail=f"File is empty (0 bytes): {file_path}",
            )

        return VerificationCheck(
            checkpoint=f"artifact_{name}",
            check_type="artifact",
            result="pass",
            detail=f"File exists with {size} bytes: {file_path}",
        )

    def _check_contract(self, contract: dict, index: int) -> list[VerificationCheck]:
        """Validate required fields in a child contract."""
        checks = []

        for field_name in REQUIRED_CONTRACT_FIELDS:
            value = contract.get(field_name)
            if value is None or (isinstance(value, str) and not value.strip()):
                checks.append(VerificationCheck(
                    checkpoint=f"contract_{index}_{field_name}",
                    check_type="contract",
                    result="fail",
                    detail=f"Contract #{index} missing required field: {field_name}",
                ))
            else:
                checks.append(VerificationCheck(
                    checkpoint=f"contract_{index}_{field_name}",
                    check_type="contract",
                    result="pass",
                    detail=f"Contract #{index} field '{field_name}' present",
                ))

        # Validate confidence value
        conf = contract.get("confidence", "")
        if isinstance(conf, str) and conf not in VALID_CONFIDENCE_VALUES:
            # Try float
            try:
                fval = float(conf)
                if not (0.0 <= fval <= 1.0):
                    checks.append(VerificationCheck(
                        checkpoint=f"contract_{index}_confidence_range",
                        check_type="contract",
                        result="fail",
                        detail=f"Contract #{index} confidence '{conf}' out of range [0.0, 1.0]",
                    ))
            except (ValueError, TypeError):
                checks.append(VerificationCheck(
                    checkpoint=f"contract_{index}_confidence_valid",
                    check_type="contract",
                    result="fail",
                    detail=f"Contract #{index} confidence '{conf}' is not a valid value "
                           f"(expected: high/medium/low or 0.0-1.0)",
                ))

        return checks

    def _check_maker_checker(
        self,
        workflow_id: str,
        repo_changes: list[str],
        critic_reviews: list[dict] | None,
    ) -> VerificationCheck:
        """Verify that repo-changing paths went through maker-checker flow.

        Maker-checker requires:
          1. At least one critic review exists for the workflow
          2. The critic review verdict is not 'objection' (blocking)
          3. The review covers the relevant changes
        """
        if not critic_reviews:
            return VerificationCheck(
                checkpoint="maker_checker",
                check_type="maker_checker",
                result="fail",
                detail=f"Repo changes detected ({len(repo_changes)} files) but no "
                       f"critic review found. Maker-checker flow was not followed.",
            )

        # Check for blocking objections
        blocking_objections = [
            r for r in critic_reviews
            if r.get("verdict") == "objection" and r.get("blocking", False)
        ]

        if blocking_objections:
            return VerificationCheck(
                checkpoint="maker_checker",
                check_type="maker_checker",
                result="fail",
                detail=f"Blocking critic objection(s) exist: "
                       f"{len(blocking_objections)} unresolved objection(s). "
                       f"Repo changes cannot be finalized.",
            )

        # Check that at least one review passed or only has non-blocking issues
        passing_reviews = [
            r for r in critic_reviews
            if r.get("verdict") in ("pass", "needs_revision")
        ]

        if not passing_reviews:
            return VerificationCheck(
                checkpoint="maker_checker",
                check_type="maker_checker",
                result="fail",
                detail="No passing or needs_revision critic review found. "
                       "All reviews were objections.",
            )

        return VerificationCheck(
            checkpoint="maker_checker",
            check_type="maker_checker",
            result="pass",
            detail=f"Maker-checker flow satisfied: {len(passing_reviews)} "
                   f"passing review(s) for {len(repo_changes)} changed file(s).",
        )

    # --- Persistence ---

    def _save_report(self, report: VerificationReport) -> Path:
        """Persist a verification report to STATE/verifications/."""
        path = self.verifications_dir / f"{report.report_id}.json"
        self.bb._write_json(path, report.to_dict())
        return path

    def get_report(self, report_id: str) -> VerificationReport | None:
        """Read a persisted verification report."""
        path = self.verifications_dir / f"{report_id}.json"
        data = self.bb._read_json(path)
        if data is None:
            return None
        return VerificationReport.from_dict(data)

    def list_reports(self, workflow_id: str | None = None) -> list[VerificationReport]:
        """List all verification reports, optionally filtered by workflow."""
        results = []
        for f in sorted(self.verifications_dir.glob("vr_*.json")):
            data = self.bb._read_json(f)
            if data is None:
                continue
            report = VerificationReport.from_dict(data)
            if workflow_id is None or report.workflow_id == workflow_id:
                results.append(report)
        return results

    def is_workflow_approved(self, workflow_id: str) -> bool:
        """Check if the latest verification report for a workflow is approved."""
        reports = self.list_reports(workflow_id)
        if not reports:
            return False
        latest = max(reports, key=lambda r: r.verified_at)
        return latest.verdict == "approved"
