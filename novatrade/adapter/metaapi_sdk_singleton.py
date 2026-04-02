"""MetaApi SDK Singleton Manager.

Prevents per-cycle re-instantiation of the MetaApi SDK object by maintaining
a global singleton instance keyed by connection configuration. This addresses
the FTMO compliance gap around SDK resource management.

The singleton pattern ensures that multiple MetaApiAdapter instances with the
same configuration (token, domain, region, application) share the same
underlying MetaApi SDK object.
"""

from __future__ import annotations

import logging
from threading import RLock
from typing import Any
from weakref import WeakValueDictionary

from metaapi_cloud_sdk import MetaApi

log = logging.getLogger("novatrade.adapter.metaapi_sdk_singleton")

# Global singleton instances, keyed by configuration hash
_METAAPI_INSTANCES: WeakValueDictionary[str, MetaApi] = WeakValueDictionary()
_LOCK = RLock()


def get_metaapi_sdk(
    token: str,
    domain: str,
    region: str,
    application: str = "NovaCore",
) -> MetaApi:
    """Get or create a singleton MetaApi SDK instance.

    Args:
        token: MetaApi API token
        domain: MetaApi domain (e.g. 'agiliumtrade.agiliumtrade.ai')
        region: MetaApi region (e.g. 'new-york')
        application: Application name for tracking

    Returns:
        Singleton MetaApi instance for this configuration

    Note:
        Multiple calls with identical parameters return the same instance.
        The instance is automatically garbage collected when no longer referenced.
    """
    # Create configuration hash for singleton key
    config_key = f"{token[:8]}...:{domain}:{region}:{application}"

    with _LOCK:
        # Check if we already have an instance
        if config_key in _METAAPI_INSTANCES:
            instance = _METAAPI_INSTANCES[config_key]
            log.debug("metaapi singleton: reusing existing SDK instance (key=%s)", config_key)
            return instance

        # Create new instance
        log.info("metaapi singleton: creating new SDK instance (key=%s)", config_key)
        instance = MetaApi(
            token,
            {
                "domain": domain,
                "region": region,
                "application": application,
            },
        )

        # Store in singleton cache
        _METAAPI_INSTANCES[config_key] = instance
        log.info("metaapi singleton: SDK instance created and cached")
        return instance


def close_all_instances() -> None:
    """Close all cached MetaApi SDK instances.

    This is primarily for testing cleanup and graceful shutdown scenarios.
    In normal operation, instances are automatically cleaned up when no
    longer referenced.
    """
    with _LOCK:
        for config_key, instance in list(_METAAPI_INSTANCES.items()):
            try:
                log.info("metaapi singleton: closing SDK instance (key=%s)", config_key)
                instance.close()
            except Exception as exc:
                log.warning("metaapi singleton: error closing SDK instance: %s", exc)

        _METAAPI_INSTANCES.clear()
        log.info("metaapi singleton: all SDK instances closed")


def get_singleton_stats() -> dict[str, Any]:
    """Get statistics about singleton instances.

    Returns:
        Dictionary with instance count and configuration keys
    """
    with _LOCK:
        return {
            "instance_count": len(_METAAPI_INSTANCES),
            "config_keys": list(_METAAPI_INSTANCES.keys()),
            "memory_usage_note": "instances auto-cleanup when unreferenced",
        }
