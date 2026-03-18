"""Tests for utils/breaker_registry.py — Phase 6B Step 6.11."""

import pytest

from agents.circuit_breakers import SimpleCircuitBreaker
from utils.breaker_registry import BREAKERS, get_breaker

# ---------------------------------------------------------------------------
# Expected configuration for each breaker
# ---------------------------------------------------------------------------

EXPECTED = {
    "mcp": {"failure_threshold": 5, "reset_timeout_seconds": 30.0, "window_seconds": 60.0},
    "claude_api": {"failure_threshold": 3, "reset_timeout_seconds": 60.0, "window_seconds": 120.0},
    "metaapi": {"failure_threshold": 3, "reset_timeout_seconds": 120.0, "window_seconds": 60.0},
    "webhook": {"failure_threshold": 10, "reset_timeout_seconds": 30.0, "window_seconds": 60.0},
    "redis": {"failure_threshold": 5, "reset_timeout_seconds": 30.0, "window_seconds": 60.0},
    "pinecone": {"failure_threshold": 3, "reset_timeout_seconds": 60.0, "window_seconds": 60.0},
    "neo4j": {"failure_threshold": 3, "reset_timeout_seconds": 60.0, "window_seconds": 60.0},
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBreakerRegistryPopulation:
    """All 7 breakers exist and are accessible."""

    def test_all_seven_breakers_present(self):
        assert set(BREAKERS.keys()) == set(EXPECTED.keys())

    def test_breakers_are_circuit_breaker_instances(self):
        for name, breaker in BREAKERS.items():
            assert isinstance(breaker, SimpleCircuitBreaker), f"{name} is not a SimpleCircuitBreaker"


class TestGetBreaker:
    """get_breaker() returns correct instances."""

    @pytest.mark.parametrize("name", list(EXPECTED.keys()))
    def test_get_breaker_returns_registered_instance(self, name):
        assert get_breaker(name) is BREAKERS[name]

    def test_get_breaker_unknown_raises_key_error(self):
        with pytest.raises(KeyError):
            get_breaker("nonexistent_service")


class TestBreakerThresholds:
    """Thresholds match the spec for each breaker."""

    @pytest.mark.parametrize("name,spec", list(EXPECTED.items()))
    def test_failure_threshold(self, name, spec):
        breaker = get_breaker(name)
        assert breaker.failure_threshold == spec["failure_threshold"]

    @pytest.mark.parametrize("name,spec", list(EXPECTED.items()))
    def test_reset_timeout(self, name, spec):
        breaker = get_breaker(name)
        assert breaker.reset_timeout_seconds == spec["reset_timeout_seconds"]

    @pytest.mark.parametrize("name,spec", list(EXPECTED.items()))
    def test_window_seconds(self, name, spec):
        breaker = get_breaker(name)
        assert breaker.window_seconds == spec["window_seconds"]


class TestExcludedExceptions:
    """excluded_exceptions is set correctly on all breakers."""

    @pytest.mark.parametrize("name", list(EXPECTED.keys()))
    def test_excluded_exceptions_set(self, name):
        breaker = get_breaker(name)
        assert breaker.excluded_exceptions == (ValueError, KeyError, TypeError)


class TestBreakerIndependence:
    """Tripping one breaker does not affect others."""

    def test_tripping_one_does_not_affect_others(self):
        mcp = get_breaker("mcp")
        webhook = get_breaker("webhook")

        # Trip the mcp breaker by recording enough failures
        for _ in range(mcp.failure_threshold):
            mcp.record_failure()

        assert mcp.state == "OPEN"
        assert webhook.state == "CLOSED"


class TestOnOpenCallback:
    """Each breaker has an on_open callback set."""

    @pytest.mark.parametrize("name", list(EXPECTED.keys()))
    def test_on_open_is_callable(self, name):
        breaker = get_breaker(name)
        assert callable(breaker.on_open)
