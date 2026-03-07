"""Tests for Phase 7.4 — Critic/Verifier/WorkflowGate integration.

Acceptance criteria covered:
  1. repo-changing workflow paths cannot finalize without verifier approval
  2. critic can emit a structured blocking objection
  3. critic objection produces a deterministic reroute/replan signal
  4. failed verification blocks completion
  5. accepted verification allows synthesis/finalization to proceed
  6. contract validation rejects missing required contract structure for gated paths
"""

import json
import time
from pathlib import Path

from agents.blackboard import Blackboard, ChildContract
from agents.critic import CriticEngine, CriticReview, CriticIssue, ReplanSignal
from agents.verifier import VerifierEngine, VerificationReport, VerificationCheck
from agents.workflow_gate import (
    WorkflowGate, RerouteDecision, validate_contract_fields,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bb(tmp_path: Path) -> Blackboard:
    """Create a Blackboard rooted in a temp directory."""
    return Blackboard(base=tmp_path)


def _good_contract() -> dict:
    return {
        "summary": "Built feature X",
        "files_changed": "module.py",
        "verification": "10/10 tests pass",
        "confidence": "high",
    }


def _bad_contract_missing_field() -> dict:
    return {
        "summary": "Incomplete",
        "confidence": "high",
        # missing: files_changed, verification
    }


def _bad_contract_bad_confidence() -> dict:
    return {
        "summary": "Something",
        "files_changed": "x.py",
        "verification": "checked",
        "confidence": "maybe",
    }


def _make_deliverable_file(tmp_path: Path, name: str = "output.md",
                           content: str = "result") -> str:
    p = tmp_path / name
    p.write_text(content)
    return str(p)


# ===========================================================================
# 1. Critic: structured blocking objection
# ===========================================================================

class TestCriticObjection:
    """AC: critic can emit a structured blocking objection."""

    def test_critical_issue_produces_objection(self, tmp_path):
        bb = _bb(tmp_path)
        critic = CriticEngine(blackboard=bb)
        review = critic.review(
            workflow_id="wf_1",
            target_node_id="node_a",
            deliverables={},  # no deliverables → critical issue
        )
        assert review.verdict == "objection"
        assert review.blocking is True
        assert len(review.issues) > 0
        assert any(i.severity == "critical" for i in review.issues)

    def test_objection_persisted_to_state(self, tmp_path):
        bb = _bb(tmp_path)
        critic = CriticEngine(blackboard=bb)
        review = critic.review("wf_2", "node_b", deliverables={})
        # Should be retrievable from disk
        loaded = critic.get_review(review.review_id)
        assert loaded is not None
        assert loaded.verdict == "objection"
        assert loaded.review_id == review.review_id

    def test_objection_has_remediation_hint(self, tmp_path):
        bb = _bb(tmp_path)
        critic = CriticEngine(blackboard=bb)
        review = critic.review("wf_3", "node_c", deliverables={})
        assert review.remediation_hint != ""

    def test_pass_verdict_on_good_deliverables(self, tmp_path):
        bb = _bb(tmp_path)
        critic = CriticEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "good.md", "content here")
        review = critic.review(
            "wf_4", "node_d",
            deliverables={"good.md": fpath},
            contract=_good_contract(),
        )
        assert review.verdict == "pass"
        assert review.blocking is False
        assert len(review.issues) == 0

    def test_high_issue_produces_needs_revision(self, tmp_path):
        bb = _bb(tmp_path)
        critic = CriticEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "out.md", "ok")
        review = critic.review(
            "wf_5", "node_e",
            deliverables={"out.md": fpath},
            contract=_bad_contract_missing_field(),  # high severity: missing fields
        )
        assert review.verdict == "needs_revision"

    def test_contract_compliance_checked(self, tmp_path):
        bb = _bb(tmp_path)
        critic = CriticEngine(blackboard=bb)
        compliance = critic.check_contract_compliance(_bad_contract_missing_field())
        assert compliance["files_changed"] == "fail"
        assert compliance["verification"] == "fail"
        assert compliance["summary"] == "pass"

    def test_empty_file_is_high_severity(self, tmp_path):
        bb = _bb(tmp_path)
        critic = CriticEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "empty.md", "")
        review = critic.review("wf_6", "node_f", deliverables={"empty.md": fpath})
        assert any(
            i.reason_code == "empty_file" or i.reason_code == "empty_deliverable"
            for i in review.issues
        )


# ===========================================================================
# 2. Critic: deterministic reroute/replan signal
# ===========================================================================

class TestCriticReplanSignal:
    """AC: critic objection produces a deterministic reroute/replan signal."""

    def test_objection_emits_replan_signal(self, tmp_path):
        bb = _bb(tmp_path)
        critic = CriticEngine(blackboard=bb)
        review = critic.review("wf_10", "node_x", deliverables={})
        assert review.verdict == "objection"
        # Should have created a replan signal
        signals = critic.list_pending_replans("wf_10")
        assert len(signals) == 1
        signal = signals[0]
        assert signal.workflow_id == "wf_10"
        assert signal.status == "pending"
        assert signal.reason_code != ""

    def test_replan_signal_persisted(self, tmp_path):
        bb = _bb(tmp_path)
        critic = CriticEngine(blackboard=bb)
        critic.review("wf_11", "node_y", deliverables={})
        signals = critic.list_pending_replans("wf_11")
        signal = signals[0]
        loaded = critic.get_replan_signal(signal.signal_id)
        assert loaded is not None
        assert loaded.signal_id == signal.signal_id

    def test_replan_signal_acknowledged(self, tmp_path):
        bb = _bb(tmp_path)
        critic = CriticEngine(blackboard=bb)
        critic.review("wf_12", "node_z", deliverables={})
        signals = critic.list_pending_replans("wf_12")
        critic.acknowledge_replan(signals[0].signal_id)
        # Should no longer be in pending list
        pending = critic.list_pending_replans("wf_12")
        assert len(pending) == 0

    def test_replan_signal_resolved(self, tmp_path):
        bb = _bb(tmp_path)
        critic = CriticEngine(blackboard=bb)
        critic.review("wf_13", "node_w", deliverables={})
        signals = critic.list_pending_replans("wf_13")
        critic.resolve_replan(signals[0].signal_id)
        loaded = critic.get_replan_signal(signals[0].signal_id)
        assert loaded.status == "resolved"

    def test_pass_verdict_no_replan_signal(self, tmp_path):
        bb = _bb(tmp_path)
        critic = CriticEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "ok.md", "content")
        critic.review("wf_14", "node_v", deliverables={"ok.md": fpath},
                       contract=_good_contract())
        signals = critic.list_pending_replans("wf_14")
        assert len(signals) == 0


# ===========================================================================
# 3. Verifier: failed verification blocks completion
# ===========================================================================

class TestVerifierBlocking:
    """AC: failed verification blocks completion."""

    def test_missing_artifact_rejected(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        report = verifier.verify(
            "wf_20",
            deliverables={"missing": "/nonexistent/file.md"},
            contracts=[_good_contract()],
        )
        assert report.verdict == "rejected"
        assert len(report.blocking_issues) > 0

    def test_bad_contract_rejected(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "out.md", "content")
        report = verifier.verify(
            "wf_21",
            deliverables={"out.md": fpath},
            contracts=[_bad_contract_missing_field()],
        )
        assert report.verdict == "rejected"
        assert any("missing required field" in issue for issue in report.blocking_issues)

    def test_bad_confidence_rejected(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "out.md", "content")
        report = verifier.verify(
            "wf_22",
            deliverables={"out.md": fpath},
            contracts=[_bad_contract_bad_confidence()],
        )
        assert report.verdict == "rejected"

    def test_verification_report_persisted(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "out.md", "content")
        report = verifier.verify(
            "wf_23",
            deliverables={"out.md": fpath},
            contracts=[_good_contract()],
        )
        loaded = verifier.get_report(report.report_id)
        assert loaded is not None
        assert loaded.verdict == report.verdict

    def test_empty_artifact_rejected(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "empty.md", "")
        report = verifier.verify(
            "wf_24",
            deliverables={"empty.md": fpath},
            contracts=[_good_contract()],
        )
        assert report.verdict == "rejected"

    def test_workflow_not_approved_when_rejected(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        verifier.verify(
            "wf_25",
            deliverables={"missing": "/no/file"},
            contracts=[_good_contract()],
        )
        assert verifier.is_workflow_approved("wf_25") is False


# ===========================================================================
# 4. Verifier: accepted verification allows finalization
# ===========================================================================

class TestVerifierApproval:
    """AC: accepted verification allows synthesis/finalization to proceed."""

    def test_all_good_approved(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "result.md", "full content")
        report = verifier.verify(
            "wf_30",
            deliverables={"result.md": fpath},
            contracts=[_good_contract()],
        )
        assert report.verdict == "approved"
        assert len(report.blocking_issues) == 0
        assert report.confidence == "high"

    def test_workflow_approved_flag(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "ok.md", "data")
        verifier.verify("wf_31", deliverables={"ok.md": fpath},
                        contracts=[_good_contract()])
        assert verifier.is_workflow_approved("wf_31") is True

    def test_float_confidence_accepted(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "ok.md", "data")
        contract = _good_contract()
        contract["confidence"] = "0.85"
        report = verifier.verify("wf_32", deliverables={"ok.md": fpath},
                                 contracts=[contract])
        assert report.verdict == "approved"


# ===========================================================================
# 5. Maker-checker: repo-changing paths need verifier approval
# ===========================================================================

class TestMakerChecker:
    """AC: repo-changing workflow paths cannot finalize without verifier approval."""

    def test_repo_changes_without_critic_review_rejected(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "code.py", "print('hi')")
        report = verifier.verify(
            "wf_40",
            deliverables={"code.py": fpath},
            contracts=[_good_contract()],
            repo_changes=["code.py"],
            critic_reviews=None,  # no critic review
        )
        assert report.verdict == "rejected"
        assert report.maker_checker_enforced is True
        assert report.maker_checker_passed is False

    def test_repo_changes_with_blocking_objection_rejected(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "code.py", "print('hi')")
        report = verifier.verify(
            "wf_41",
            deliverables={"code.py": fpath},
            contracts=[_good_contract()],
            repo_changes=["code.py"],
            critic_reviews=[{"verdict": "objection", "blocking": True}],
        )
        assert report.verdict == "rejected"
        assert report.maker_checker_enforced is True
        assert report.maker_checker_passed is False

    def test_repo_changes_with_passing_review_approved(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "code.py", "print('hi')")
        report = verifier.verify(
            "wf_42",
            deliverables={"code.py": fpath},
            contracts=[_good_contract()],
            repo_changes=["code.py"],
            critic_reviews=[{"verdict": "pass", "blocking": False}],
        )
        assert report.verdict == "approved"
        assert report.maker_checker_enforced is True
        assert report.maker_checker_passed is True

    def test_no_repo_changes_skips_maker_checker(self, tmp_path):
        bb = _bb(tmp_path)
        verifier = VerifierEngine(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "report.md", "analysis")
        report = verifier.verify(
            "wf_43",
            deliverables={"report.md": fpath},
            contracts=[_good_contract()],
            repo_changes=None,
        )
        assert report.verdict == "approved"
        assert report.maker_checker_enforced is False


# ===========================================================================
# 6. Contract validation: rejects missing fields for gated paths
# ===========================================================================

class TestContractValidation:
    """AC: contract validation rejects missing required contract structure."""

    def test_valid_contract_passes(self):
        valid, errors = validate_contract_fields(_good_contract())
        assert valid is True
        assert errors == []

    def test_missing_field_rejected(self):
        valid, errors = validate_contract_fields(_bad_contract_missing_field())
        assert valid is False
        assert len(errors) >= 2  # missing files_changed, verification

    def test_bad_confidence_rejected(self):
        valid, errors = validate_contract_fields(_bad_contract_bad_confidence())
        assert valid is False
        assert any("confidence" in e for e in errors)

    def test_empty_string_field_rejected(self):
        contract = _good_contract()
        contract["summary"] = "   "
        valid, errors = validate_contract_fields(contract)
        assert valid is False
        assert any("summary" in e for e in errors)

    def test_float_confidence_accepted(self):
        contract = _good_contract()
        contract["confidence"] = "0.75"
        valid, errors = validate_contract_fields(contract)
        assert valid is True

    def test_float_out_of_range_rejected(self):
        contract = _good_contract()
        contract["confidence"] = "1.5"
        valid, errors = validate_contract_fields(contract)
        assert valid is False

    def test_empty_contract_all_fields_rejected(self):
        valid, errors = validate_contract_fields({})
        assert valid is False
        assert len(errors) == 4  # all 4 required fields missing


# ===========================================================================
# 7. WorkflowGate: integration seam
# ===========================================================================

class TestWorkflowGate:
    """AC: integration of critic + verifier into workflow gate."""

    def test_gate_completion_approved(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "out.md", "content")
        report = gate.gate_completion(
            "wf_50",
            deliverables={"out.md": fpath},
            contracts=[_good_contract()],
        )
        assert report.verdict == "approved"

    def test_gate_completion_rejected_on_bad_contract(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "out.md", "content")
        report = gate.gate_completion(
            "wf_51",
            deliverables={"out.md": fpath},
            contracts=[_bad_contract_missing_field()],
        )
        assert report.verdict == "rejected"

    def test_critic_objection_produces_reroute_decisions(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)
        # Trigger critic objection
        review = gate.run_critic_review("wf_52", "node_a", deliverables={})
        assert review.verdict == "objection"
        # Check reroute decisions
        decisions = gate.check_replan_signals("wf_52")
        assert len(decisions) == 1
        d = decisions[0]
        assert isinstance(d, RerouteDecision)
        assert d.workflow_id == "wf_52"
        assert d.action in ("retry", "reassign", "halt")

    def test_is_completion_allowed_with_good_data(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "out.md", "content")
        allowed, reason = gate.is_completion_allowed(
            "wf_53",
            deliverables={"out.md": fpath},
            contracts=[_good_contract()],
        )
        assert allowed is True

    def test_is_completion_blocked_by_bad_contract(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "out.md", "content")
        allowed, reason = gate.is_completion_allowed(
            "wf_54",
            deliverables={"out.md": fpath},
            contracts=[_bad_contract_missing_field()],
        )
        assert allowed is False
        assert "Contract validation failed" in reason

    def test_is_completion_blocked_by_pending_replan(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)
        # Create a pending replan signal via critic objection
        gate.run_critic_review("wf_55", "node_a", deliverables={})
        fpath = _make_deliverable_file(tmp_path, "out.md", "content")
        allowed, reason = gate.is_completion_allowed(
            "wf_55",
            deliverables={"out.md": fpath},
            contracts=[_good_contract()],
        )
        assert allowed is False
        assert "replan" in reason.lower()

    def test_validate_contracts_batch(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)
        valid, errors = gate.validate_contracts_for_completion([
            _good_contract(),
            _bad_contract_missing_field(),
        ])
        assert valid is False
        assert any("contract #1" in e for e in errors)

    def test_repo_changes_gate_requires_critic(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)
        fpath = _make_deliverable_file(tmp_path, "code.py", "print(1)")
        report = gate.gate_completion(
            "wf_56",
            deliverables={"code.py": fpath},
            contracts=[_good_contract()],
            repo_changes=["code.py"],
        )
        # No critic reviews exist → maker-checker fails
        assert report.verdict == "rejected"

    def test_reroute_retryable_reason(self, tmp_path):
        bb = _bb(tmp_path)
        gate = WorkflowGate(blackboard=bb)
        # Create objection with file_not_found (retryable)
        fpath = "/nonexistent/file.md"
        review = gate.run_critic_review(
            "wf_57", "node_a",
            deliverables={"missing": fpath},
        )
        decisions = gate.check_replan_signals("wf_57")
        if decisions:
            assert decisions[0].action in ("retry", "reassign", "halt")


# ===========================================================================
# Run as script
# ===========================================================================

if __name__ == "__main__":
    import sys
    import tempfile

    test_classes = [
        TestCriticObjection,
        TestCriticReplanSignal,
        TestVerifierBlocking,
        TestVerifierApproval,
        TestMakerChecker,
        TestContractValidation,
        TestWorkflowGate,
    ]

    passed = 0
    failed = 0
    errors = []

    for cls in test_classes:
        instance = cls()
        for name in sorted(dir(instance)):
            if not name.startswith("test_"):
                continue
            method = getattr(instance, name)
            try:
                import inspect
                sig = inspect.signature(method)
                if "tmp_path" in sig.parameters:
                    with tempfile.TemporaryDirectory() as td:
                        method(Path(td))
                else:
                    method()
                print(f"  PASS  {cls.__name__}.{name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {cls.__name__}.{name}: {e}")
                failed += 1
                errors.append(f"{cls.__name__}.{name}: {e}")

    print(f"\n{passed}/{passed + failed} tests passed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
