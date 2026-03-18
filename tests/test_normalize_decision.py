"""Focused tests for normalize_decision helper in rollout_gate."""

import pytest

from agents.rollout_gate import VALID_DECISIONS, normalize_decision


class TestNormalizeDecision:
    """Canonical, case-insensitive, whitespace/hyphen-tolerant normalisation."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Already canonical
            ("ready_to_expand", "ready_to_expand"),
            ("hold", "hold"),
            ("rollback_recommended", "rollback_recommended"),
            # Mixed case
            ("Ready_To_Expand", "ready_to_expand"),
            ("HOLD", "hold"),
            ("Rollback_Recommended", "rollback_recommended"),
            ("ROLLBACK_RECOMMENDED", "rollback_recommended"),
            # Hyphens instead of underscores
            ("ready-to-expand", "ready_to_expand"),
            ("rollback-recommended", "rollback_recommended"),
            # Spaces instead of underscores
            ("ready to expand", "ready_to_expand"),
            ("rollback recommended", "rollback_recommended"),
            # Leading/trailing whitespace
            ("  hold  ", "hold"),
            (" ready_to_expand\n", "ready_to_expand"),
            # Mixed separators
            ("Ready-To_Expand", "ready_to_expand"),
            ("READY TO EXPAND", "ready_to_expand"),
        ],
    )
    def test_valid_inputs(self, raw: str, expected: str):
        assert normalize_decision(raw) == expected

    @pytest.mark.parametrize(
        "bad_input",
        [
            "",
            "unknown",
            "expand",
            "rollback",
            "ready",
            "hold_forever",
            "ready_to_expand_more",
        ],
    )
    def test_invalid_inputs_raise(self, bad_input: str):
        with pytest.raises(ValueError, match="Unknown rollout decision"):
            normalize_decision(bad_input)

    def test_valid_decisions_frozenset(self):
        assert VALID_DECISIONS == {"ready_to_expand", "hold", "rollback_recommended"}
        # Immutable
        with pytest.raises(AttributeError):
            VALID_DECISIONS.add("bad")  # type: ignore[attr-defined]
