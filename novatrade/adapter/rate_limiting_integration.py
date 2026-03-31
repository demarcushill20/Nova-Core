"""Rate Limiting Integration for MetaAPI Adapter.

Provides enhanced reconnection logic that prevents HTTP 429 rate limiting
by using the RateLimitGuardian to manage connection attempts intelligently.
"""

import logging
import time
from typing import Any

from novatrade.monitor.rate_limit_guardian import RateLimitGuardian, get_global_guardian

log = logging.getLogger("novatrade.adapter.rate_limiting_integration")


class RateLimitingReconnectMixin:
    """Mixin to add rate-limiting-aware reconnection to MetaAPI adapter."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rate_guardian: RateLimitGuardian = get_global_guardian()
        self._setup_guardian_listeners()

    def _setup_guardian_listeners(self):
        """Set up listeners for rate limit guardian events."""

        def on_connection_event(event: str, data: Any):
            if event == "failure" and data.get("error_code") == 429:
                log.error("Rate limiting detected - will enforce extended cooldown")
            elif event == "success":
                log.info("Connection success - rate limiting protection reset")

        def on_feed_staleness(staleness_seconds: float):
            log.warning("Feed staleness detected: %.1fs - monitoring for reconnection needs", staleness_seconds)

        self._rate_guardian.add_connection_listener(on_connection_event)
        self._rate_guardian.add_feed_listener(on_feed_staleness)

    def _is_subscription_quota_error(self, exc: Exception) -> bool:
        """Check if exception is a MetaApi subscription quota exceeded error."""
        exc_str = str(exc).lower()
        exc_name = exc.__class__.__name__

        # Check for TooManyRequestsException with subscription quota message
        return (exc_name == "TooManyRequestsException" and "subscription" in exc_str and "quota" in exc_str) or (
            "account subscriptions quota" in exc_str or "subscriptions available" in exc_str
        )

    def _handle_subscription_quota_error(self, exc: Exception) -> None:
        """Handle MetaApi subscription quota exceeded error."""
        try:
            # Try to extract quota information from the exception
            max_subscriptions = None
            used_subscriptions = None
            recommended_retry_time = None

            exc_str = str(exc)

            # Parse quota numbers from message like:
            # "You have 50 account subscriptions available and have used 70 subscriptions"
            import re

            quota_match = re.search(r"(\d+) account subscriptions available.*used (\d+) subscriptions", exc_str)
            if quota_match:
                max_subscriptions = int(quota_match.group(1))
                used_subscriptions = int(quota_match.group(2))

            # Try to get metadata from MetaApi exception
            if hasattr(exc, "metadata") and isinstance(exc.metadata, dict):
                metadata = exc.metadata
                max_subscriptions = metadata.get("maxSubscriptionsPerUser", max_subscriptions)
                used_subscriptions = metadata.get("subscriptionsCount", used_subscriptions)
                recommended_retry_time = metadata.get("recommendedRetryTime", recommended_retry_time)

            # Record quota exhaustion in rate guardian
            if max_subscriptions and used_subscriptions:
                self._rate_guardian.record_subscription_quota_exceeded(
                    max_subscriptions=max_subscriptions,
                    used_subscriptions=used_subscriptions,
                    recommended_retry_time=recommended_retry_time,
                )
            else:
                # Fallback - just record it as quota exhausted without specific numbers
                log.warning("Subscription quota exceeded but unable to parse exact numbers")
                self._rate_guardian.record_subscription_quota_exceeded(
                    max_subscriptions=50,  # default assumption
                    used_subscriptions=70,  # over the limit
                    recommended_retry_time=recommended_retry_time,
                )

        except Exception as parse_error:
            log.error("Error parsing subscription quota error: %s", parse_error)
            # Still record it as a quota issue
            self._rate_guardian.record_subscription_quota_exceeded(50, 70)

    async def _auto_reconnect_with_rate_limiting(self) -> bool:
        """Enhanced auto-reconnect with rate limiting protection.

        Replaces the standard auto-reconnect logic with rate-limiting-aware
        connection management.
        """
        if getattr(self, "_reconnecting", False):
            return False  # prevent concurrent reconnect loops

        self._reconnecting = True
        max_attempts = getattr(self._config, "reconnect_max_attempts", 3)

        try:
            for attempt in range(max_attempts):
                # Check if we should attempt connection
                can_connect, reason = self._rate_guardian.should_attempt_connection()
                if not can_connect:
                    log.warning(
                        "metaapi rate-limited reconnect: attempt %d/%d blocked - %s", attempt + 1, max_attempts, reason
                    )

                    # Wait for approval before continuing
                    log.info("Waiting for rate limit guardian approval...")
                    await self._rate_guardian.wait_for_connection_approval()
                    log.info("Rate limit guardian approved connection attempt")

                log.info(
                    "metaapi rate-limited reconnect: attempt %d/%d (with rate limiting protection)",
                    attempt + 1,
                    max_attempts,
                )

                # Clean up stale state
                try:
                    if getattr(self, "_connection", None):
                        await self._connection.close()
                    self._connected = False
                except Exception as exc:
                    log.debug("metaapi rate-limited reconnect: cleanup error: %s", exc)

                # Attempt reconnection
                try:
                    status = await self.connect()
                    if status.connected:
                        self._rate_guardian.record_connection_success()
                        log.info("metaapi rate-limited reconnect: succeeded on attempt %d", attempt + 1)
                        return True
                    else:
                        # Connection failed with error status
                        self._rate_guardian.record_connection_failure()
                        log.warning(
                            "metaapi rate-limited reconnect: attempt %d failed: %s", attempt + 1, status.message
                        )

                except Exception as exc:
                    # Check if this is a subscription quota exhaustion error
                    if self._is_subscription_quota_error(exc):
                        self._handle_subscription_quota_error(exc)
                        log.error("Subscription quota exhausted in reconnect attempt %d: %s", attempt + 1, exc)
                        break  # Stop immediately on quota exhaustion

                    # Check if this is a rate limiting error
                    error_code = None
                    error_str = str(exc).lower()
                    if "429" in error_str or "rate limit" in error_str or "too many requests" in error_str:
                        error_code = 429
                        log.error("Rate limiting detected in reconnect attempt %d: %s", attempt + 1, exc)

                    backoff_seconds = self._rate_guardian.record_connection_failure(error_code)
                    log.warning("metaapi rate-limited reconnect: attempt %d failed: %s", attempt + 1, exc)

                    # For rate limiting, respect the guardian's backoff completely
                    if error_code == 429:
                        log.warning("Rate limiting backoff enforced: %.1fs", backoff_seconds)
                        break  # Don't continue attempts immediately after rate limiting

                # If not the last attempt, the guardian will handle backoff timing
                if attempt < max_attempts - 1:
                    log.info("Will retry after rate limit guardian approval...")

            log.error("metaapi rate-limited reconnect: exhausted %d attempts", max_attempts)
            return False

        finally:
            self._reconnecting = False

    async def resubscribe_if_stale_with_rate_limiting(self) -> bool:
        """Enhanced resubscribe with rate limiting and feed health monitoring."""
        elapsed = time.monotonic() - getattr(self, "_last_resubscribe", 0)

        # Check feed health via rate guardian
        if hasattr(self, "_connection") and self._connection:
            try:
                # Get current price to check feed health
                for symbol in getattr(self, "_subscribed_symbols", []):
                    try:
                        price_data = await self.get_current_price(symbol)
                        if price_data and hasattr(price_data, "timestamp"):
                            # Calculate age of last tick
                            last_tick_age = time.time() - price_data.timestamp
                            action = self._rate_guardian.update_feed_health(last_tick_age)

                            if action == "reconnect":
                                log.warning("Feed staleness requires reconnection for %s", symbol)
                                return await self._attempt_resubscribe_with_rate_limiting()
                            elif action == "restart_required":
                                log.error("Critical feed staleness for %s - service restart may be required", symbol)
                                # For now, still attempt resubscribe
                                return await self._attempt_resubscribe_with_rate_limiting()
                        break  # Only check first symbol for now
                    except Exception as e:
                        log.debug("Error checking feed health for %s: %s", symbol, e)
            except Exception as e:
                log.debug("Error in feed health check: %s", e)

        # Standard stale resubscription logic
        if elapsed >= getattr(self, "_RESUB_INTERVAL_S", 540):
            return await self._attempt_resubscribe_with_rate_limiting()

        return False

    async def _attempt_resubscribe_with_rate_limiting(self) -> bool:
        """Attempt resubscription with rate limiting protection."""
        # Check if we should attempt connection
        can_connect, reason = self._rate_guardian.should_attempt_connection()
        if not can_connect:
            log.warning("Resubscribe blocked by rate limiting: %s", reason)
            return False

        try:
            log.info("Attempting feed resubscription with rate limiting protection")

            # Ensure connected
            await self._ensure_connected_or_reconnect()

            # Resubscribe to symbols
            for symbol in getattr(self, "_subscribed_symbols", []):
                await self._connection.subscribe_to_market_data(symbol, [{"type": "quotes"}])
                log.debug("Resubscribed to quotes for %s", symbol)

            self._last_resubscribe = time.monotonic()
            self._rate_guardian.record_connection_success()

            log.info("Feed resubscription successful for %d symbols", len(getattr(self, "_subscribed_symbols", [])))
            return True

        except Exception as exc:
            # Check for subscription quota exhaustion first
            if self._is_subscription_quota_error(exc):
                self._handle_subscription_quota_error(exc)
                log.error("Subscription quota exhausted during resubscribe: %s", exc)
                return False

            # Check for rate limiting
            error_code = None
            error_str = str(exc).lower()
            if "429" in error_str or "rate limit" in error_str:
                error_code = 429
                log.error("Rate limiting detected during resubscribe: %s", exc)

            self._rate_guardian.record_connection_failure(error_code)
            log.error("Feed resubscription failed: %s", exc)
            return False

    def get_rate_limiting_status(self) -> dict:
        """Get current rate limiting status and metrics."""
        return {
            "guardian_report": self._rate_guardian.generate_health_report(),
            "connection_metrics": self._rate_guardian.get_connection_metrics(),
            "adapter_status": {
                "connected": getattr(self, "_connected", False),
                "reconnecting": getattr(self, "_reconnecting", False),
                "subscribed_symbols": len(getattr(self, "_subscribed_symbols", [])),
                "last_resubscribe_age": time.monotonic() - getattr(self, "_last_resubscribe", 0),
            },
        }


# Monkey patch function to enhance existing MetaApiAdapter instances
def enhance_adapter_with_rate_limiting(adapter_instance):
    """Enhance an existing MetaApiAdapter with rate limiting protection.

    This allows us to add rate limiting to existing adapters without
    requiring code changes in the main adapter file.
    """
    log.info("Enhancing MetaApiAdapter with rate limiting protection")

    # Store original methods
    adapter_instance._original_auto_reconnect = getattr(adapter_instance, "_auto_reconnect", None)
    adapter_instance._original_resubscribe_if_stale = getattr(adapter_instance, "resubscribe_if_stale", None)

    # Set up rate guardian
    adapter_instance._rate_guardian = get_global_guardian()

    # Set up guardian listeners
    def on_connection_event(event: str, data: Any):
        if event == "failure" and data.get("error_code") == 429:
            log.error("Rate limiting detected on adapter %s", id(adapter_instance))
        elif event == "success":
            log.info("Connection success on adapter %s", id(adapter_instance))

    def on_feed_staleness(staleness_seconds: float):
        log.warning("Feed staleness detected on adapter %s: %.1fs", id(adapter_instance), staleness_seconds)

    adapter_instance._rate_guardian.add_connection_listener(on_connection_event)
    adapter_instance._rate_guardian.add_feed_listener(on_feed_staleness)

    # Replace methods with rate-limiting-aware versions
    async def enhanced_auto_reconnect() -> bool:
        if getattr(adapter_instance, "_reconnecting", False):
            return False

        adapter_instance._reconnecting = True
        max_attempts = getattr(adapter_instance._config, "reconnect_max_attempts", 3)

        try:
            for attempt in range(max_attempts):
                can_connect, reason = adapter_instance._rate_guardian.should_attempt_connection()
                if not can_connect:
                    log.warning("Reconnect attempt %d blocked: %s", attempt + 1, reason)
                    await adapter_instance._rate_guardian.wait_for_connection_approval()

                log.info("Enhanced reconnect attempt %d/%d", attempt + 1, max_attempts)

                try:
                    if hasattr(adapter_instance, "_connection") and adapter_instance._connection:
                        await adapter_instance._connection.close()
                    adapter_instance._connected = False
                except Exception as exc:
                    log.debug("Reconnect cleanup error: %s", exc)

                try:
                    status = await adapter_instance.connect()
                    if status.connected:
                        adapter_instance._rate_guardian.record_connection_success()
                        log.info("Enhanced reconnect succeeded on attempt %d", attempt + 1)
                        return True
                    else:
                        adapter_instance._rate_guardian.record_connection_failure()

                except Exception as exc:
                    error_code = 429 if ("429" in str(exc) or "rate limit" in str(exc).lower()) else None
                    adapter_instance._rate_guardian.record_connection_failure(error_code)
                    log.warning("Enhanced reconnect attempt %d failed: %s", attempt + 1, exc)

                    if error_code == 429:
                        break  # Stop immediately on rate limiting

            return False
        finally:
            adapter_instance._reconnecting = False

    # Replace the method
    adapter_instance._auto_reconnect = enhanced_auto_reconnect

    # Add status method
    adapter_instance.get_rate_limiting_status = lambda: {
        "guardian_report": adapter_instance._rate_guardian.generate_health_report(),
        "connection_metrics": adapter_instance._rate_guardian.get_connection_metrics(),
    }

    log.info("MetaApiAdapter enhanced with rate limiting protection")
