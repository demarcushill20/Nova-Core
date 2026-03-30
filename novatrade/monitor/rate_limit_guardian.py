"""Rate Limiting Guardian for MetaAPI connections.

Prevents rate limiting by implementing intelligent backoff, connection
management, and feed staleness detection with graceful reconnection.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

log = logging.getLogger("novatrade.monitor.rate_limit_guardian")


@dataclass
class ConnectionHealth:
    """Track connection health metrics."""
    last_success: float = 0
    consecutive_failures: int = 0
    rate_limit_hits: int = 0
    last_rate_limit: float = 0
    backoff_until: float = 0
    feed_last_update: float = 0
    feed_staleness_seconds: float = 0
    # Subscription quota tracking
    subscription_quota_max: int = 50
    subscription_quota_used: int = 0
    last_quota_exceeded: float = 0
    quota_exhausted: bool = False


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting protection."""
    max_consecutive_failures: int = 3
    base_backoff_seconds: float = 30.0
    max_backoff_seconds: float = 300.0  # 5 minutes max
    rate_limit_cooldown: float = 120.0  # 2 minutes after 429
    stale_feed_threshold: float = 300.0  # 5 minutes
    critical_staleness_threshold: float = 1800.0  # 30 minutes
    reconnect_jitter_range: tuple[float, float] = (1.0, 10.0)


class RateLimitGuardian:
    """Intelligent rate limiting protection for MetaAPI."""

    def __init__(self, config: RateLimitConfig | None = None):
        self.config = config or RateLimitConfig()
        self.connection_health = ConnectionHealth()
        self._connection_listeners: list[Callable[[str, Any], None]] = []
        self._feed_listeners: list[Callable[[float], None]] = []

    def add_connection_listener(self, listener: Callable[[str, Any], None]) -> None:
        """Add listener for connection events (success, failure, rate_limit)."""
        self._connection_listeners.append(listener)

    def add_feed_listener(self, listener: Callable[[float], None]) -> None:
        """Add listener for feed staleness events."""
        self._feed_listeners.append(listener)

    def should_attempt_connection(self) -> tuple[bool, str]:
        """Check if connection attempt should proceed.

        Returns:
            (should_proceed, reason)
        """
        now = time.time()

        # Check subscription quota exhaustion
        if self.connection_health.quota_exhausted:
            quota_usage = (self.connection_health.subscription_quota_used /
                          self.connection_health.subscription_quota_max) * 100
            return False, f"Subscription quota exhausted: {self.connection_health.subscription_quota_used}/{self.connection_health.subscription_quota_max} ({quota_usage:.1f}%)"

        # Check if we're in backoff period
        if now < self.connection_health.backoff_until:
            remaining = self.connection_health.backoff_until - now
            return False, f"In backoff period for {remaining:.1f} more seconds"

        # Check consecutive failures
        if self.connection_health.consecutive_failures >= self.config.max_consecutive_failures:
            return False, f"Too many consecutive failures ({self.connection_health.consecutive_failures})"

        # Check recent rate limiting
        if self.connection_health.rate_limit_hits > 0:
            time_since_rate_limit = now - self.connection_health.last_rate_limit
            if time_since_rate_limit < self.config.rate_limit_cooldown:
                remaining = self.config.rate_limit_cooldown - time_since_rate_limit
                return False, f"Rate limit cooldown for {remaining:.1f} more seconds"

        return True, "Connection attempt approved"

    def record_connection_success(self) -> None:
        """Record successful connection."""
        now = time.time()
        self.connection_health.last_success = now
        self.connection_health.consecutive_failures = 0
        self.connection_health.backoff_until = 0

        # Reset quota exhaustion on successful connection
        if self.connection_health.quota_exhausted:
            log.info("Subscription quota no longer exhausted - connection successful")
            self.connection_health.quota_exhausted = False

        log.info("Connection success recorded - resetting failure counters")
        self._notify_connection_listeners("success", {"timestamp": now})

    def record_subscription_quota_exceeded(
        self,
        max_subscriptions: int,
        used_subscriptions: int,
        recommended_retry_time: str | None = None
    ) -> None:
        """Record subscription quota exceeded event.

        Args:
            max_subscriptions: Maximum allowed subscriptions
            used_subscriptions: Currently used subscriptions
            recommended_retry_time: ISO timestamp when to retry (from broker API)
        """
        now = time.time()
        self.connection_health.subscription_quota_max = max_subscriptions
        self.connection_health.subscription_quota_used = used_subscriptions
        self.connection_health.quota_exhausted = True
        self.connection_health.last_quota_exceeded = now

        log.error(
            "Subscription quota exceeded: %d/%d subscriptions used",
            used_subscriptions, max_subscriptions
        )

        if recommended_retry_time:
            try:
                # Parse broker retry time: "2026-03-30T13:17:57.996Z"
                from datetime import datetime
                retry_dt = datetime.fromisoformat(recommended_retry_time.replace('Z', '+00:00'))
                retry_timestamp = retry_dt.timestamp()

                # Set backoff to recommended retry time
                self.connection_health.backoff_until = max(
                    self.connection_health.backoff_until,
                    retry_timestamp
                )

                log.warning(
                    "Broker recommended retry time: %s (backoff extended to %s)",
                    recommended_retry_time,
                    datetime.fromtimestamp(self.connection_health.backoff_until).isoformat()
                )
            except (ValueError, ImportError) as e:
                log.error("Failed to parse broker retry time '%s': %s", recommended_retry_time, e)
                # Default to 10-minute backoff for quota issues
                self.connection_health.backoff_until = now + 600

        self._notify_connection_listeners("quota_exceeded", {
            "max_subscriptions": max_subscriptions,
            "used_subscriptions": used_subscriptions,
            "quota_usage_pct": (used_subscriptions / max_subscriptions) * 100,
            "recommended_retry_time": recommended_retry_time
        })

    def record_connection_failure(self, error_code: int | None = None) -> float:
        """Record connection failure and calculate backoff.

        Args:
            error_code: HTTP error code (429 for rate limiting)

        Returns:
            Backoff duration in seconds
        """
        now = time.time()
        self.connection_health.consecutive_failures += 1

        if error_code == 429:
            self.connection_health.rate_limit_hits += 1
            self.connection_health.last_rate_limit = now
            backoff = self.config.rate_limit_cooldown
            log.warning("Rate limiting detected (HTTP 429) - extended cooldown: %ds", backoff)
        else:
            # Exponential backoff for regular failures
            backoff = min(
                self.config.base_backoff_seconds * (2 ** (self.connection_health.consecutive_failures - 1)),
                self.config.max_backoff_seconds
            )

            # Add jitter to prevent thundering herd
            jitter_min, jitter_max = self.config.reconnect_jitter_range
            import random
            jitter = random.uniform(jitter_min, jitter_max)
            backoff += jitter

        self.connection_health.backoff_until = now + backoff

        log.warning(
            "Connection failure %d recorded (error_code=%s) - backoff until %s",
            self.connection_health.consecutive_failures,
            error_code,
            datetime.fromtimestamp(self.connection_health.backoff_until).isoformat()
        )

        self._notify_connection_listeners("failure", {
            "error_code": error_code,
            "consecutive_failures": self.connection_health.consecutive_failures,
            "backoff_seconds": backoff
        })

        return backoff

    def update_feed_health(self, last_tick_age_seconds: float) -> str | None:
        """Update feed health and return action if needed.

        Args:
            last_tick_age_seconds: Age of last tick in seconds

        Returns:
            Recommended action: None, "reconnect", or "restart_required"
        """
        now = time.time()
        self.connection_health.feed_last_update = now
        self.connection_health.feed_staleness_seconds = last_tick_age_seconds

        # Determine feed health status and recommended action
        if last_tick_age_seconds < self.config.stale_feed_threshold:
            # Feed is healthy
            return None
        elif last_tick_age_seconds < self.config.critical_staleness_threshold:
            # Feed is stale but recoverable
            log.warning("Feed staleness detected: %ds old (threshold: %ds)",
                       last_tick_age_seconds, self.config.stale_feed_threshold)
            self._notify_feed_listeners(last_tick_age_seconds)
            return "reconnect"
        else:
            # Feed is critically stale
            log.error("Critical feed staleness: %ds old (threshold: %ds) - service restart may be required",
                     last_tick_age_seconds, self.config.critical_staleness_threshold)
            self._notify_feed_listeners(last_tick_age_seconds)
            return "restart_required"

    def get_connection_metrics(self) -> dict[str, Any]:
        """Get current connection health metrics."""
        now = time.time()
        quota_usage_pct = (self.connection_health.subscription_quota_used /
                          self.connection_health.subscription_quota_max * 100
                          if self.connection_health.subscription_quota_max > 0 else 0)

        return {
            "last_success_age": now - self.connection_health.last_success if self.connection_health.last_success else None,
            "consecutive_failures": self.connection_health.consecutive_failures,
            "rate_limit_hits": self.connection_health.rate_limit_hits,
            "in_backoff": now < self.connection_health.backoff_until,
            "backoff_remaining": max(0, self.connection_health.backoff_until - now),
            "feed_staleness_seconds": self.connection_health.feed_staleness_seconds,
            "feed_status": self._get_feed_status(),
            "can_connect": self.should_attempt_connection()[0],
            "subscription_quota": {
                "max": self.connection_health.subscription_quota_max,
                "used": self.connection_health.subscription_quota_used,
                "usage_pct": quota_usage_pct,
                "exhausted": self.connection_health.quota_exhausted,
                "last_exceeded": self.connection_health.last_quota_exceeded
            }
        }

    def _get_feed_status(self) -> str:
        """Get current feed status."""
        staleness = self.connection_health.feed_staleness_seconds
        if staleness < self.config.stale_feed_threshold:
            return "healthy"
        elif staleness < self.config.critical_staleness_threshold:
            return "stale"
        else:
            return "critical"

    def _notify_connection_listeners(self, event: str, data: Any) -> None:
        """Notify connection event listeners."""
        for listener in self._connection_listeners:
            try:
                listener(event, data)
            except Exception as e:
                log.error("Connection listener error: %s", e)

    def _notify_feed_listeners(self, staleness: float) -> None:
        """Notify feed staleness listeners."""
        for listener in self._feed_listeners:
            try:
                listener(staleness)
            except Exception as e:
                log.error("Feed listener error: %s", e)

    async def wait_for_connection_approval(self) -> None:
        """Async wait until connection is approved (not in backoff)."""
        while True:
            can_connect, reason = self.should_attempt_connection()
            if can_connect:
                break

            # Calculate sleep time (check every 5 seconds or remaining backoff time, whichever is less)
            sleep_time = min(5.0, self.connection_health.backoff_until - time.time())
            if sleep_time > 0:
                log.debug("Waiting for connection approval: %s", reason)
                await asyncio.sleep(sleep_time)
            else:
                break

    def generate_health_report(self) -> dict[str, Any]:
        """Generate comprehensive health report."""
        time.time()
        metrics = self.get_connection_metrics()

        # Determine overall health status
        if self.connection_health.consecutive_failures == 0 and metrics["feed_status"] == "healthy":
            overall_status = "healthy"
        elif self.connection_health.consecutive_failures < self.config.max_consecutive_failures and metrics["feed_status"] in ["healthy", "stale"]:
            overall_status = "degraded"
        else:
            overall_status = "unhealthy"

        # Generate recommendations
        recommendations = []

        if metrics["rate_limit_hits"] > 0:
            recommendations.append("Rate limiting detected - verify no duplicate connections")

        if metrics["feed_status"] == "stale":
            recommendations.append("Feed is stale - consider connection refresh")
        elif metrics["feed_status"] == "critical":
            recommendations.append("CRITICAL: Feed critically stale - service restart may be required")

        if self.connection_health.consecutive_failures > 0:
            recommendations.append(f"Connection failures detected ({self.connection_health.consecutive_failures}) - check broker connectivity")

        return {
            "timestamp": datetime.now().isoformat(),
            "overall_status": overall_status,
            "metrics": metrics,
            "recommendations": recommendations,
            "config": {
                "stale_threshold": self.config.stale_feed_threshold,
                "critical_threshold": self.config.critical_staleness_threshold,
                "max_failures": self.config.max_consecutive_failures
            }
        }


# Global instance for easy access
_global_guardian: RateLimitGuardian | None = None


def get_global_guardian() -> RateLimitGuardian:
    """Get global rate limit guardian instance."""
    global _global_guardian
    if _global_guardian is None:
        _global_guardian = RateLimitGuardian()
    return _global_guardian


def reset_global_guardian(config: RateLimitConfig | None = None) -> None:
    """Reset global guardian with new config."""
    global _global_guardian
    _global_guardian = RateLimitGuardian(config)


# Example usage for monitoring integration
async def monitor_connection_health(guardian: RateLimitGuardian, check_interval: float = 60.0) -> None:
    """Background monitor for connection health."""
    log.info("Starting connection health monitor (interval: %ds)", check_interval)

    while True:
        try:
            report = guardian.generate_health_report()
            status = report["overall_status"]

            if status == "unhealthy":
                log.error("Connection health: UNHEALTHY - %s", report["recommendations"])
            elif status == "degraded":
                log.warning("Connection health: DEGRADED - %s", report["recommendations"])
            else:
                log.debug("Connection health: HEALTHY")

            await asyncio.sleep(check_interval)

        except Exception as e:
            log.error("Connection health monitor error: %s", e)
            await asyncio.sleep(check_interval)
