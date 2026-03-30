"""Tests for RateLimitGuardian — MetaAPI rate-limiting protection."""

from __future__ import annotations

import time

import pytest

from novatrade.monitor.rate_limit_guardian import (
    ConnectionHealth,
    RateLimitConfig,
    RateLimitGuardian,
    get_global_guardian,
    reset_global_guardian,
)

# =====================================================================
# ConnectionHealth dataclass
# =====================================================================


def test_connection_health_defaults():
    h = ConnectionHealth()
    assert h.consecutive_failures == 0
    assert h.rate_limit_hits == 0
    assert h.backoff_until == 0
    assert h.subscription_quota_max == 50
    assert h.subscription_quota_used == 0
    assert h.quota_exhausted is False


# =====================================================================
# RateLimitConfig
# =====================================================================


def test_rate_limit_config_defaults():
    c = RateLimitConfig()
    assert c.max_consecutive_failures == 3
    assert c.base_backoff_seconds == 30.0
    assert c.max_backoff_seconds == 300.0
    assert c.rate_limit_cooldown == 120.0
    assert c.stale_feed_threshold == 300.0
    assert c.critical_staleness_threshold == 1800.0


def test_rate_limit_config_custom():
    c = RateLimitConfig(max_consecutive_failures=5, base_backoff_seconds=10.0)
    assert c.max_consecutive_failures == 5
    assert c.base_backoff_seconds == 10.0


# =====================================================================
# should_attempt_connection
# =====================================================================


def test_fresh_guardian_allows_connection():
    g = RateLimitGuardian()
    ok, reason = g.should_attempt_connection()
    assert ok is True
    assert "approved" in reason.lower()


def test_backoff_blocks_connection():
    g = RateLimitGuardian()
    g.connection_health.backoff_until = time.time() + 60
    ok, reason = g.should_attempt_connection()
    assert ok is False
    assert "backoff" in reason.lower()


def test_consecutive_failures_block_connection():
    g = RateLimitGuardian()
    g.connection_health.consecutive_failures = 3
    ok, reason = g.should_attempt_connection()
    assert ok is False
    assert "failures" in reason.lower()


def test_rate_limit_cooldown_blocks_connection():
    g = RateLimitGuardian()
    g.connection_health.rate_limit_hits = 1
    g.connection_health.last_rate_limit = time.time()
    ok, reason = g.should_attempt_connection()
    assert ok is False
    assert "cooldown" in reason.lower()


def test_expired_rate_limit_allows_connection():
    g = RateLimitGuardian()
    g.connection_health.rate_limit_hits = 1
    g.connection_health.last_rate_limit = time.time() - 300  # 5 min ago, cooldown is 2 min
    ok, _ = g.should_attempt_connection()
    assert ok is True


def test_quota_exhausted_blocks_connection():
    g = RateLimitGuardian()
    g.connection_health.quota_exhausted = True
    g.connection_health.subscription_quota_used = 50
    ok, reason = g.should_attempt_connection()
    assert ok is False
    assert "quota" in reason.lower()


# =====================================================================
# record_connection_success
# =====================================================================


def test_record_success_resets_failures():
    g = RateLimitGuardian()
    g.connection_health.consecutive_failures = 5
    g.connection_health.backoff_until = time.time() + 100
    g.record_connection_success()
    assert g.connection_health.consecutive_failures == 0
    assert g.connection_health.backoff_until == 0
    assert g.connection_health.last_success > 0


def test_record_success_clears_quota_exhaustion():
    g = RateLimitGuardian()
    g.connection_health.quota_exhausted = True
    g.record_connection_success()
    assert g.connection_health.quota_exhausted is False


def test_record_success_notifies_listeners():
    events = []
    g = RateLimitGuardian()
    g.add_connection_listener(lambda e, d: events.append(e))
    g.record_connection_success()
    assert events == ["success"]


# =====================================================================
# record_connection_failure
# =====================================================================


def test_failure_increments_counter():
    g = RateLimitGuardian()
    g.record_connection_failure()
    assert g.connection_health.consecutive_failures == 1


def test_failure_returns_backoff():
    g = RateLimitGuardian()
    backoff = g.record_connection_failure()
    assert backoff > 0


def test_failure_rate_limit_429():
    g = RateLimitGuardian()
    backoff = g.record_connection_failure(error_code=429)
    assert g.connection_health.rate_limit_hits == 1
    assert backoff == g.config.rate_limit_cooldown


def test_failure_exponential_backoff():
    g = RateLimitGuardian(
        config=RateLimitConfig(
            base_backoff_seconds=10.0,
            reconnect_jitter_range=(0.0, 0.0),  # disable jitter for deterministic test
        )
    )
    b1 = g.record_connection_failure()
    # Reset for clean 2nd failure
    g.connection_health.backoff_until = 0
    b2 = g.record_connection_failure()
    # 2nd backoff should be ~2x first (10 * 2^0 = 10, 10 * 2^1 = 20)
    assert b2 > b1


def test_failure_backoff_capped():
    g = RateLimitGuardian(
        config=RateLimitConfig(
            base_backoff_seconds=100.0,
            max_backoff_seconds=150.0,
            reconnect_jitter_range=(0.0, 0.0),
        )
    )
    g.connection_health.consecutive_failures = 9  # would be 100 * 2^9 without cap
    backoff = g.record_connection_failure()
    assert backoff <= 150.0


def test_failure_notifies_listeners():
    events = []
    g = RateLimitGuardian()
    g.add_connection_listener(lambda e, d: events.append((e, d.get("error_code"))))
    g.record_connection_failure(error_code=500)
    assert events[0] == ("failure", 500)


# =====================================================================
# record_subscription_quota_exceeded
# =====================================================================


def test_quota_exceeded_marks_exhausted():
    g = RateLimitGuardian()
    g.record_subscription_quota_exceeded(max_subscriptions=50, used_subscriptions=50)
    assert g.connection_health.quota_exhausted is True
    assert g.connection_health.subscription_quota_used == 50


def test_quota_exceeded_with_retry_time():
    g = RateLimitGuardian()
    # Use a future time
    future_iso = "2099-12-31T23:59:59.000Z"
    g.record_subscription_quota_exceeded(
        max_subscriptions=50,
        used_subscriptions=50,
        recommended_retry_time=future_iso,
    )
    assert g.connection_health.backoff_until > time.time()


def test_quota_exceeded_invalid_retry_time_defaults_to_10min():
    g = RateLimitGuardian()
    before = time.time()
    g.record_subscription_quota_exceeded(
        max_subscriptions=50,
        used_subscriptions=50,
        recommended_retry_time="not-a-date",
    )
    # Should default to ~600s backoff
    assert g.connection_health.backoff_until >= before + 590


def test_quota_exceeded_notifies_listeners():
    events = []
    g = RateLimitGuardian()
    g.add_connection_listener(lambda e, d: events.append(e))
    g.record_subscription_quota_exceeded(50, 50)
    assert "quota_exceeded" in events


# =====================================================================
# update_feed_health
# =====================================================================


def test_feed_healthy():
    g = RateLimitGuardian()
    action = g.update_feed_health(10.0)  # 10s old — well within threshold
    assert action is None


def test_feed_stale_reconnect():
    g = RateLimitGuardian()
    action = g.update_feed_health(400.0)  # > 300s threshold
    assert action == "reconnect"


def test_feed_critical_restart():
    g = RateLimitGuardian()
    action = g.update_feed_health(2000.0)  # > 1800s threshold
    assert action == "restart_required"


def test_feed_staleness_notifies_listeners():
    staleness_values = []
    g = RateLimitGuardian()
    g.add_feed_listener(lambda s: staleness_values.append(s))
    g.update_feed_health(400.0)
    assert staleness_values == [400.0]


def test_feed_healthy_does_not_notify():
    staleness_values = []
    g = RateLimitGuardian()
    g.add_feed_listener(lambda s: staleness_values.append(s))
    g.update_feed_health(10.0)
    assert staleness_values == []


# =====================================================================
# get_connection_metrics / _get_feed_status
# =====================================================================


def test_get_connection_metrics_fresh():
    g = RateLimitGuardian()
    m = g.get_connection_metrics()
    assert m["consecutive_failures"] == 0
    assert m["rate_limit_hits"] == 0
    assert m["in_backoff"] is False
    assert m["can_connect"] is True
    assert m["feed_status"] == "healthy"
    assert "subscription_quota" in m


def test_get_feed_status_values():
    g = RateLimitGuardian()
    g.connection_health.feed_staleness_seconds = 10
    assert g._get_feed_status() == "healthy"
    g.connection_health.feed_staleness_seconds = 500
    assert g._get_feed_status() == "stale"
    g.connection_health.feed_staleness_seconds = 2000
    assert g._get_feed_status() == "critical"


# =====================================================================
# generate_health_report
# =====================================================================


def test_health_report_healthy():
    g = RateLimitGuardian()
    report = g.generate_health_report()
    assert report["overall_status"] == "healthy"
    assert report["recommendations"] == []
    assert "timestamp" in report
    assert "config" in report


def test_health_report_degraded():
    g = RateLimitGuardian()
    g.connection_health.consecutive_failures = 1  # < max (3)
    report = g.generate_health_report()
    assert report["overall_status"] == "degraded"
    assert any("failure" in r.lower() for r in report["recommendations"])


def test_health_report_unhealthy():
    g = RateLimitGuardian()
    g.connection_health.consecutive_failures = 5
    g.connection_health.feed_staleness_seconds = 2000
    report = g.generate_health_report()
    assert report["overall_status"] == "unhealthy"


def test_health_report_rate_limit_recommendation():
    g = RateLimitGuardian()
    g.connection_health.rate_limit_hits = 3
    report = g.generate_health_report()
    assert any("rate limit" in r.lower() for r in report["recommendations"])


def test_health_report_stale_feed_recommendation():
    g = RateLimitGuardian()
    g.connection_health.feed_staleness_seconds = 500
    report = g.generate_health_report()
    assert any("stale" in r.lower() for r in report["recommendations"])


def test_health_report_critical_feed_recommendation():
    g = RateLimitGuardian()
    g.connection_health.feed_staleness_seconds = 2000
    report = g.generate_health_report()
    assert any("critical" in r.lower() for r in report["recommendations"])


# =====================================================================
# Listener error handling
# =====================================================================


def test_connection_listener_error_does_not_propagate():
    g = RateLimitGuardian()
    g.add_connection_listener(lambda e, d: (_ for _ in ()).throw(RuntimeError("boom")))
    # Should not raise
    g.record_connection_success()


def test_feed_listener_error_does_not_propagate():
    g = RateLimitGuardian()
    g.add_feed_listener(lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    # Should not raise
    g.update_feed_health(500.0)


# =====================================================================
# wait_for_connection_approval
# =====================================================================


@pytest.mark.asyncio
async def test_wait_approval_immediate():
    """Should return immediately when no backoff."""
    g = RateLimitGuardian()
    await g.wait_for_connection_approval()  # should not hang


@pytest.mark.asyncio
async def test_wait_approval_backoff_expires():
    """Should return when backoff expires."""
    g = RateLimitGuardian()
    g.connection_health.backoff_until = time.time() + 0.1  # 100ms
    await g.wait_for_connection_approval()
    ok, _ = g.should_attempt_connection()
    assert ok is True


# =====================================================================
# Global guardian helpers
# =====================================================================


def test_global_guardian_singleton():
    reset_global_guardian()
    g1 = get_global_guardian()
    g2 = get_global_guardian()
    assert g1 is g2


def test_reset_global_guardian():
    reset_global_guardian()
    g1 = get_global_guardian()
    reset_global_guardian(RateLimitConfig(max_consecutive_failures=99))
    g2 = get_global_guardian()
    assert g1 is not g2
    assert g2.config.max_consecutive_failures == 99
