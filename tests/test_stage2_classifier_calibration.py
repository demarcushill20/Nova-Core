"""Phase 7.12c — Tests for Stage 2 classifier calibration.

Tests cover:
  1. Natural-language research tasks classify with sufficient confidence
  2. Natural-language code_review tasks classify with sufficient confidence
  3. Telegram-style phrasing variants recognized
  4. Unsupported/mutation-capable/high-risk tasks fail closed
  5. Ambiguous/empty tasks do not enter orchestrator path
  6. End-to-end routing sends calibrated Stage 2 tasks to orchestrator
  7. Confidence formula produces predictable values
"""

import json
import pytest
from pathlib import Path

from tools.task_classifier import (
    classify_task,
    classify_and_route,
    has_mutation_signals,
    has_high_risk_signals,
    is_stageC_eligible,
    TASK_CLASSES,
)


# ---------------------------------------------------------------------------
# Feature flag helpers
# ---------------------------------------------------------------------------

def _stage2_flags(enabled=True, min_confidence=0.5):
    """Stage 2 feature flags: research + code_review."""
    return {
        "enabled": enabled,
        "stage": "C",
        "supported_classes": ["research", "code_review"],
        "allowed_roles": ["research", "coding"],
        "min_confidence": min_confidence,
        "verifier_required": True,
        "fallback_to_worker": True,
        "audit_routing": True,
    }


# ===================================================================
# Part 1 — Research classification with natural language
# ===================================================================

class TestResearchClassification:
    """Natural-language research requests classify as research with >= 0.5."""

    @pytest.mark.parametrize("text", [
        "Research Nova-Core's current rollout architecture",
        "Analyze the watcher dispatch flow",
        "Explain how the orchestrator adapter works",
        "Describe the multi-agent architecture",
        "Summarize how Stage 2 evidence accumulation works",
        "Investigate why tasks are not routing correctly",
        "Explore the current rollout gate criteria",
        "Compare the Stage 2 and Stage 3 requirements",
        "Look up how the verifier enforcement works",
        "Survey the available task classes and their patterns",
    ])
    def test_research_phrasing(self, text):
        cls, conf = classify_task(text)
        assert cls == "research", f"Expected research, got {cls} for: {text}"
        assert conf >= 0.5, f"Confidence {conf} < 0.5 for: {text}"

    def test_research_with_two_keywords_high_confidence(self):
        """Two research keywords should give confidence 1.0."""
        cls, conf = classify_task("Research and analyze the system architecture")
        assert cls == "research"
        assert conf == 1.0

    def test_research_eligible_for_stage2(self):
        """Research classification passes Stage C eligibility."""
        text = "Research how the watcher routes tasks"
        cls, conf = classify_task(text)
        eligible, reason = is_stageC_eligible(cls, conf, text, _stage2_flags())
        assert eligible is True
        assert reason == "stageC_research_eligible"


# ===================================================================
# Part 2 — Code review classification with natural language
# ===================================================================

class TestCodeReviewClassification:
    """Natural-language code review requests classify as code_review with >= 0.5."""

    @pytest.mark.parametrize("text", [
        "Review agents/rollout_gate.py for edge cases",
        "Review watcher.py routing logic",
        "Review tools/orchestrator_adapter.py for correctness",
        "Inspect the task classifier for safety issues",
        "Audit the rollout gate",
        "Check for bugs in the heartbeat monitor",
        "Check for edge cases in the watcher dispatch logic",
        "Inspect the verifier enforcement path for issues",
        "Review code for the Phase 7 orchestrator",
        "Check for quality issues in the test suite",
    ])
    def test_code_review_phrasing(self, text):
        cls, conf = classify_task(text)
        assert cls == "code_review", f"Expected code_review, got {cls} for: {text}"
        assert conf >= 0.5, f"Confidence {conf} < 0.5 for: {text}"

    def test_review_file_path_pattern(self):
        """Review followed by a file path should classify as code_review."""
        cls, conf = classify_task("Review tools/task_classifier.py")
        assert cls == "code_review"
        assert conf >= 0.5

    def test_inspect_pattern(self):
        """'inspect' is a code_review keyword."""
        cls, conf = classify_task("Inspect this module for potential issues")
        assert cls == "code_review"
        assert conf >= 0.5

    def test_check_for_bugs_pattern(self):
        """'check for bugs' is a code_review keyword."""
        cls, conf = classify_task("Check for bugs in the routing logic")
        assert cls == "code_review"
        assert conf >= 0.5

    def test_code_review_eligible_for_stage2(self):
        """code_review classification passes Stage C eligibility."""
        text = "Review agents/rollout_gate.py for edge cases"
        cls, conf = classify_task(text)
        eligible, reason = is_stageC_eligible(cls, conf, text, _stage2_flags())
        assert eligible is True
        assert reason == "stageC_coding_eligible"

    def test_audit_with_implementation_is_review(self):
        """'Audit the X implementation' should be code_review, not code_impl."""
        cls, conf = classify_task("Audit the rollout gate implementation")
        assert cls == "code_review"

    def test_code_review_two_keywords_high_confidence(self):
        """Two code_review keywords should give confidence 1.0."""
        cls, conf = classify_task("Audit and inspect the code for quality issues")
        assert cls == "code_review"
        assert conf == 1.0


# ===================================================================
# Part 3 — Negative safety cases
# ===================================================================

class TestNegativeSafety:
    """Unsupported, mutation-capable, and high-risk tasks fail closed."""

    @pytest.mark.parametrize("text,expected_class", [
        ("Deploy the new service to production", "system"),
        ("Implement a new feature for task routing", "code_impl"),
        ("Fix the bug in the watcher dispatch", "code_impl"),
        ("Create a function to validate input", "code_impl"),
        ("Refactor the orchestrator adapter", "code_impl"),
        ("Build a new monitoring dashboard", "code_impl"),
    ])
    def test_unsupported_class_not_routed(self, text, expected_class):
        """Tasks for unsupported classes are not routed to orchestrator."""
        cls, conf = classify_task(text)
        assert cls == expected_class
        eligible, reason = is_stageC_eligible(cls, conf, text, _stage2_flags())
        assert eligible is False
        assert "not_in_stageC" in reason

    @pytest.mark.parametrize("text", [
        "Delete all failed task files",
        "sudo apt-get install python3",
        "git push --force origin main",
        "rm -rf /tmp/old_files",
    ])
    def test_dangerous_commands_not_routed(self, text):
        """Dangerous command text should not route to orchestrator."""
        cls, conf = classify_task(text)
        # Either unknown or a non-supported class
        eligible, _ = is_stageC_eligible(cls, conf, text, _stage2_flags())
        assert eligible is False

    def test_empty_text_not_routed(self):
        cls, conf = classify_task("")
        assert cls == "unknown"
        assert conf == 0.0

    def test_nonsense_text_not_routed(self):
        cls, conf = classify_task("xyzzy plugh qwerty gibberish")
        assert cls == "unknown"
        assert conf == 0.0

    def test_mutation_signals_block_research(self):
        """Research task with mutation intent blocked by denylist."""
        text = "Research and implement a new caching layer"
        cls, conf = classify_task(text)
        # Even if classified as research, mutation signal blocks it
        has_mut, signals = has_mutation_signals(text)
        assert has_mut is True
        assert any("implement" in s.lower() for s in signals)

    def test_high_risk_signals_block_code_review(self):
        """Code review task with high-risk signals blocked."""
        text = "Review the deployment secrets configuration"
        has_risk, signals = has_high_risk_signals(text)
        assert has_risk is True

    def test_disabled_orchestrator_blocks_all(self):
        """Disabled orchestrator blocks all classes."""
        flags = _stage2_flags(enabled=False)
        eligible, reason = is_stageC_eligible("research", 0.9, "Research X", flags)
        assert eligible is False
        assert reason == "orchestrator_disabled"


# ===================================================================
# Part 4 — Confidence formula
# ===================================================================

class TestConfidenceFormula:
    """Verify confidence formula produces predictable values."""

    def test_single_match_gives_half(self):
        """One keyword match → confidence 0.5."""
        cls, conf = classify_task("Research topic")
        assert conf == 0.5

    def test_two_matches_gives_one(self):
        """Two keyword matches → confidence 1.0."""
        cls, conf = classify_task("Research and analyze the topic")
        assert conf == 1.0

    def test_three_matches_capped_at_one(self):
        """Three+ matches still capped at 1.0."""
        cls, conf = classify_task("Research, analyze, and summarize the topic")
        assert conf == 1.0

    def test_no_matches_gives_zero(self):
        cls, conf = classify_task("xyzzy")
        assert conf == 0.0


# ===================================================================
# Part 5 — End-to-end routing integration
# ===================================================================

class TestEndToEndRouting:
    """Verify calibrated tasks route correctly through classify_and_route."""

    def test_research_routes_to_orchestrator(self, monkeypatch):
        """Research task reaches orchestrator with live-like flags."""
        monkeypatch.setattr(
            "tools.task_classifier.load_feature_flags",
            lambda: _stage2_flags(),
        )
        result = classify_and_route("Research the rollout gate criteria")
        assert result["task_class"] == "research"
        assert result["use_orchestrator"] is True
        assert result["fallback_reason"] is None

    def test_code_review_routes_to_orchestrator(self, monkeypatch):
        """Code review task reaches orchestrator with live-like flags."""
        monkeypatch.setattr(
            "tools.task_classifier.load_feature_flags",
            lambda: _stage2_flags(),
        )
        result = classify_and_route("Review agents/rollout_gate.py for issues")
        assert result["task_class"] == "code_review"
        assert result["use_orchestrator"] is True
        assert result["verifier_required"] is True

    def test_code_impl_blocked_in_stage2(self, monkeypatch):
        """code_impl not in Stage 2 supported_classes → blocked."""
        monkeypatch.setattr(
            "tools.task_classifier.load_feature_flags",
            lambda: _stage2_flags(),
        )
        result = classify_and_route("Implement a new sorting algorithm")
        assert result["task_class"] == "code_impl"
        assert result["use_orchestrator"] is False

    def test_unknown_blocked(self, monkeypatch):
        """Unknown class → blocked."""
        monkeypatch.setattr(
            "tools.task_classifier.load_feature_flags",
            lambda: _stage2_flags(),
        )
        result = classify_and_route("xyzzy plugh qwerty")
        assert result["task_class"] == "unknown"
        assert result["use_orchestrator"] is False

    def test_research_with_mutation_blocked(self, monkeypatch):
        """Research with mutation signals → blocked in Stage C."""
        monkeypatch.setattr(
            "tools.task_classifier.load_feature_flags",
            lambda: _stage2_flags(),
        )
        result = classify_and_route("Research and implement a new cache")
        # Even if classified as research, mutation check blocks it
        # (or it may classify as code_impl, which is also blocked)
        if result["task_class"] == "research":
            assert result["use_orchestrator"] is False
        else:
            # code_impl is not in Stage 2 supported_classes
            assert result["use_orchestrator"] is False

    def test_disabled_orchestrator_blocks_everything(self, monkeypatch):
        """Disabled orchestrator blocks all tasks."""
        monkeypatch.setattr(
            "tools.task_classifier.load_feature_flags",
            lambda: _stage2_flags(enabled=False),
        )
        result = classify_and_route("Research the rollout gate criteria")
        assert result["use_orchestrator"] is False


# ===================================================================
# Part 6 — Pattern narrowing regression protection
# ===================================================================

class TestPatternNarrowing:
    """Verify narrowed patterns still catch intended cases."""

    def test_implement_verb_still_code_impl(self):
        """'implement' verb still classifies as code_impl."""
        cls, _ = classify_task("Implement a new feature")
        assert cls == "code_impl"

    def test_implementing_verb_still_code_impl(self):
        """'implementing' verb still classifies as code_impl."""
        cls, _ = classify_task("I am implementing the solution now")
        assert cls == "code_impl"

    def test_implementation_noun_not_code_impl(self):
        """'implementation' noun should not force code_impl classification."""
        # When the only code_impl signal is "implementation" (noun),
        # other class keywords should be able to win
        cls, _ = classify_task("Audit the rollout gate implementation")
        assert cls != "code_impl"

    def test_architect_verb_still_system(self):
        """'architect' verb still classifies as system."""
        cls, _ = classify_task("Architect a new pipeline for deployment")
        assert cls == "system"

    def test_architecture_noun_not_system(self):
        """'architecture' noun alone should not force system classification."""
        cls, _ = classify_task("Research the system architecture")
        assert cls == "research"

    def test_mutation_implement_still_blocked(self):
        """Mutation denylist still catches 'implement' verb."""
        has_mut, signals = has_mutation_signals("implement a new caching layer")
        assert has_mut is True

    def test_mutation_implementing_still_blocked(self):
        """Mutation denylist still catches 'implementing'."""
        has_mut, signals = has_mutation_signals("I am implementing the fix now")
        assert has_mut is True
