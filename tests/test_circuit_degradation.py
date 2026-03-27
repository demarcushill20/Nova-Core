"""Tests for circuit-breaker-to-degradation mapping — Phase 6B Step 6.12.

Covers:
  - evaluate_circuit_breakers() tier suggestions
  - _on_breaker_open() callback wiring and tier escalation
  - Callback does not downgrade when current tier is already worse
"""

import pytest

from utils.self_healing import (
    DegradationState,
    DegradationTier,
    _save_degradation_state,
    evaluate_circuit_breakers,
    get_degradation_tier,
    set_degradation_tier,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_degradation_state(tmp_path, monkeypatch):
    """Isolate degradation state to tmp_path so tests don't touch production."""
    import utils.self_healing as sh

    monkeypatch.setattr(sh, "STATE_DIR", tmp_path)
    monkeypatch.setattr(sh, "DEGRADATION_FILE", tmp_path / "degradation_state.json")
    tmp_path.mkdir(parents=True, exist_ok=True)
    _save_degradation_state(DegradationState())
    yield
    _save_degradation_state(DegradationState())


# ---------------------------------------------------------------------------
# evaluate_circuit_breakers — pure logic
# ---------------------------------------------------------------------------


class TestEvaluateCircuitBreakers:
    """evaluate_circuit_breakers() suggests the correct tier."""

    def test_no_breakers_open_suggests_full(self):
        assert evaluate_circuit_breakers([]) == DegradationTier.FULL

    def test_mcp_open_suggests_reduced(self):
        assert evaluate_circuit_breakers(["mcp"]) == DegradationTier.REDUCED

    def test_claude_api_open_suggests_reduced(self):
        assert evaluate_circuit_breakers(["claude_api"]) == DegradationTier.REDUCED

    def test_metaapi_open_suggests_minimal(self):
        assert evaluate_circuit_breakers(["metaapi"]) == DegradationTier.MINIMAL

    def test_two_critical_breakers_suggest_emergency(self):
        assert evaluate_circuit_breakers(["claude_api", "mcp"]) == DegradationTier.EMERGENCY

    def test_metaapi_plus_mcp_suggest_emergency(self):
        assert evaluate_circuit_breakers(["metaapi", "mcp"]) == DegradationTier.EMERGENCY

    def test_all_three_critical_suggest_emergency(self):
        assert evaluate_circuit_breakers(["claude_api", "metaapi", "mcp"]) == DegradationTier.EMERGENCY

    def test_non_critical_breaker_alone_suggests_full(self):
        assert evaluate_circuit_breakers(["redis"]) == DegradationTier.FULL
        assert evaluate_circuit_breakers(["webhook"]) == DegradationTier.FULL
        assert evaluate_circuit_breakers(["pinecone"]) == DegradationTier.FULL
        assert evaluate_circuit_breakers(["neo4j"]) == DegradationTier.FULL

    def test_non_critical_plus_one_critical_still_reduced(self):
        assert evaluate_circuit_breakers(["redis", "claude_api"]) == DegradationTier.REDUCED

    def test_non_critical_plus_one_critical_metaapi_still_minimal(self):
        assert evaluate_circuit_breakers(["webhook", "metaapi"]) == DegradationTier.MINIMAL


# ---------------------------------------------------------------------------
# set_degradation_tier — direct setter
# ---------------------------------------------------------------------------


class TestSetDegradationTier:
    """set_degradation_tier() persists and records transitions."""

    def test_set_tier_escalates(self):
        state = set_degradation_tier(DegradationTier.REDUCED, reason="test escalation")
        assert state.tier == DegradationTier.REDUCED
        assert state.reason == "test escalation"
        assert len(state.transition_history) == 1
        assert state.transition_history[-1]["direction"] == "ESCALATED"

    def test_set_tier_persists_to_disk(self):
        set_degradation_tier(DegradationTier.MINIMAL, reason="persist test")
        reloaded = get_degradation_tier()
        assert reloaded.tier == DegradationTier.MINIMAL

    def test_set_same_tier_is_noop(self):
        state = set_degradation_tier(DegradationTier.FULL, reason="no change")
        assert len(state.transition_history) == 0

    def test_set_tier_de_escalates(self):
        set_degradation_tier(DegradationTier.EMERGENCY, reason="up")
        state = set_degradation_tier(DegradationTier.REDUCED, reason="down")
        assert state.tier == DegradationTier.REDUCED
        assert state.transition_history[-1]["direction"] == "DE-ESCALATED"


# ---------------------------------------------------------------------------
# Callback integration — _on_breaker_open
# ---------------------------------------------------------------------------


class TestOnBreakerOpenCallback:
    """_on_breaker_open wires breaker state into degradation."""

    def test_callback_escalates_tier(self):
        """Tripping a critical breaker escalates the degradation tier."""
        from agents.circuit_breakers import SimpleCircuitBreaker
        from utils.breaker_registry import BREAKERS, _on_breaker_open

        # Save and replace ALL breakers with fresh instances so no stale
        # OPEN states from other tests leak in.
        originals = dict(BREAKERS)
        for bname in list(BREAKERS):
            BREAKERS[bname] = SimpleCircuitBreaker(
                name=bname,
                failure_threshold=2,
                reset_timeout_seconds=60.0,
                window_seconds=60.0,
                redis_client=False,
            )
        try:
            # Trip only claude_api
            breaker = BREAKERS["claude_api"]
            for _ in range(2):
                breaker.record_failure()
            assert breaker.state == "OPEN"

            # Fire the callback
            _on_breaker_open("claude_api")

            state = get_degradation_tier()
            assert state.tier == DegradationTier.REDUCED
            assert "circuit breaker claude_api opened" in state.reason
        finally:
            BREAKERS.update(originals)

    def test_callback_does_not_downgrade(self):
        """If current tier is already worse, callback does not change it."""
        from utils.breaker_registry import _on_breaker_open

        # Set tier to EMERGENCY manually
        set_degradation_tier(DegradationTier.EMERGENCY, reason="manual emergency")

        # Fire callback for a breaker that would suggest REDUCED
        # (no breakers actually open in BREAKERS, so suggestion is FULL)
        _on_breaker_open("mcp")

        state = get_degradation_tier()
        assert state.tier == DegradationTier.EMERGENCY
        assert state.reason == "manual emergency"

    def test_all_registry_breakers_have_callback(self):
        """Every breaker in the registry has the real on_open callback."""
        from utils.breaker_registry import BREAKERS, _on_breaker_open

        for name, breaker in BREAKERS.items():
            assert breaker.on_open is _on_breaker_open, f"breaker '{name}' has wrong on_open callback"
