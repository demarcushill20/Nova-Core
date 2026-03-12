"""Watchdog-based filesystem monitor for TASKS/ directory.

Phase 4.1: Replaces pure polling with event-driven file detection.
Falls back gracefully to polling if watchdog fails.
"""

import logging
import threading
from pathlib import Path

logger = logging.getLogger("novacore.file_watcher")

# Suffixes that indicate a task is NOT pending
_IGNORE_SUFFIXES = (".inprogress", ".done", ".failed", ".cancelled")

# Debounce window in seconds
DEBOUNCE_SECONDS = 0.1


def _is_new_task(path_str: str) -> bool:
    """Return True if the path looks like a new pending .md task file."""
    # Must end with .md (not .md.inprogress etc.)
    if not path_str.endswith(".md"):
        return False
    return all(not path_str.endswith(suffix) for suffix in _IGNORE_SUFFIXES)


class TaskFileHandler:
    """Watchdog event handler that signals when new .md task files appear.

    Uses a threading.Event to wake the main polling loop immediately
    when a new task is detected, with debouncing to coalesce rapid
    file creation events.
    """

    def __init__(self, wake_event: threading.Event):
        self._wake_event = wake_event
        self._debounce_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _on_debounce_fire(self):
        """Called after debounce window expires — signal the main loop."""
        logger.debug("file_watcher.debounce_fired")
        self._wake_event.set()

    def handle_event(self, event):
        """Process a watchdog filesystem event."""
        # Only care about file creation and move-to events
        if getattr(event, "is_directory", False):
            return

        src = getattr(event, "src_path", "") or ""
        dest = getattr(event, "dest_path", "") or ""

        # Check if either source or destination is a new task
        if not (_is_new_task(src) or _is_new_task(dest)):
            return

        logger.info("file_watcher.new_task_detected: %s", src or dest)

        with self._lock:
            # Cancel any pending debounce timer and reset
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(DEBOUNCE_SECONDS, self._on_debounce_fire)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def stop(self):
        """Cancel any pending debounce timer."""
        with self._lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None


class TaskFileWatcher:
    """Manages a watchdog Observer watching TASKS/ for new .md files.

    Usage:
        watcher = TaskFileWatcher(tasks_dir, wake_event)
        watcher.start()   # non-blocking
        ...
        wake_event.wait(timeout=60)  # wakes early on new task
        wake_event.clear()
        ...
        watcher.stop()

    Falls back gracefully: if watchdog import or observer start fails,
    the wake_event simply never gets set by watchdog — the caller's
    timeout-based polling continues to work as before.
    """

    def __init__(self, tasks_dir: Path, wake_event: threading.Event):
        self._tasks_dir = tasks_dir
        self._wake_event = wake_event
        self._handler = TaskFileHandler(wake_event)
        self._observer: object | None = None
        self._active = False

    @property
    def active(self) -> bool:
        """True if the watchdog observer is running."""
        return self._active

    def start(self) -> bool:
        """Start the watchdog observer. Returns True on success, False on fallback."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            # Create a bridge handler that delegates to our handler
            class _Bridge(FileSystemEventHandler):
                def __init__(self, handler):
                    super().__init__()
                    self._handler = handler

                def on_created(self, event):
                    self._handler.handle_event(event)

                def on_moved(self, event):
                    self._handler.handle_event(event)

            self._observer = Observer()
            self._observer.schedule(
                _Bridge(self._handler),
                str(self._tasks_dir),
                recursive=False,
            )
            self._observer.daemon = True
            self._observer.start()
            self._active = True
            logger.info(
                "file_watcher.started: watching %s (event-driven mode)",
                self._tasks_dir,
            )
            return True
        except Exception:
            logger.warning(
                "file_watcher.fallback: watchdog unavailable, using polling only",
                exc_info=True,
            )
            self._active = False
            return False

    def stop(self):
        """Stop the watchdog observer and clean up."""
        self._handler.stop()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception:
                logger.warning("file_watcher.stop_error", exc_info=True)
        self._active = False
        logger.info("file_watcher.stopped")
