"""Zero-dependency sd_notify helper for systemd integration.

Sends notifications to systemd via the NOTIFY_SOCKET Unix datagram socket.
All functions are no-ops when NOTIFY_SOCKET is not set (i.e., not running
under systemd with Type=notify), so callers never need to guard calls.

Phase 6B.15: sd_notify watchdog integration.
"""

import os
import socket


def sd_notify(state: str) -> bool:
    """Send notification to systemd via NOTIFY_SOCKET.

    Returns True if notification sent, False if not running under systemd.
    No-op when NOTIFY_SOCKET is not set.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    # Handle abstract socket (starts with @)
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(addr)
            sock.sendall(state.encode())
        finally:
            sock.close()
        return True
    except OSError:
        return False


def notify_ready() -> bool:
    """Signal that service startup is complete."""
    return sd_notify("READY=1")


def notify_watchdog() -> bool:
    """Signal that service is still alive (resets WatchdogSec timer)."""
    return sd_notify("WATCHDOG=1")


def notify_stopping() -> bool:
    """Signal that service is beginning graceful shutdown."""
    return sd_notify("STOPPING=1")


def notify_status(status: str) -> bool:
    """Set human-readable status string visible in systemctl status."""
    return sd_notify(f"STATUS={status}")
