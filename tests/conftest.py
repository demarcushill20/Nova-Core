"""Shared test fixtures — isolate FTMO state from persisted files."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# Wednesday 2026-03-25 10:00:00 UTC — outside rollover (21-23), London fix
# (15:45-16:15), weekend close, and news windows.
_SAFE_TIMESTAMP = 1774432800


@pytest.fixture(autouse=True)
def _isolate_ftmo_state(tmp_path: Path):
    """Redirect FTMO state persistence to a temp directory per-test.

    Prevents test runs from reading/writing the production STATE/novatrade/
    directory, which would cause cross-test contamination and interfere
    with the running service.
    """
    with patch("novatrade.risk.ftmo_compliance._STATE_DIR", tmp_path):
        yield


@pytest.fixture(autouse=True)
def _isolate_metrics_file(tmp_path: Path):
    """Redirect watcher METRICS_FILE to a temp directory per-test.

    Prevents tests that directly or indirectly call _update_metrics
    from polluting the production STATE/metrics.json file.
    """
    try:
        import watcher
    except ImportError:
        yield
        return
    with patch.object(watcher, "METRICS_FILE", tmp_path / "metrics.json"):
        yield


@pytest.fixture(autouse=True)
def _isolate_watcher_log(tmp_path: Path):
    """Redirect watcher log FileHandler to a temp directory per-test.

    When tests import watcher, the module-level logging setup adds a
    FileHandler pointing at the production LOGS/watcher.log.  This
    fixture swaps it to a temp file so test output stays isolated.
    """
    import logging

    watcher_logger = logging.getLogger("watcher")
    original_handlers = []
    for h in watcher_logger.handlers:
        if isinstance(h, logging.FileHandler) and "watcher.log" in str(getattr(h, "baseFilename", "")):
            original_handlers.append((h, h.baseFilename))
            h.close()
            h.baseFilename = str(tmp_path / "watcher.log")
            h.stream = h._open()
    yield
    for h, orig_path in original_handlers:
        h.close()
        h.baseFilename = orig_path
        h.stream = h._open()


@pytest.fixture(autouse=True)
def _isolate_self_healing_state(tmp_path: Path):
    """Redirect self-healing state files to a temp directory per-test.

    Prevents tests that directly or indirectly call check_self_healing(),
    set_degradation_tier(), or evaluate_degradation() from writing
    reason="test" entries to the production STATE/degradation_state.json.
    """
    try:
        import utils.self_healing as sh
    except ImportError:
        yield
        return
    with (
        patch.object(sh, "STATE_DIR", tmp_path),
        patch.object(sh, "DEGRADATION_FILE", tmp_path / "degradation_state.json"),
        patch.object(sh, "ERROR_HISTORY_FILE", tmp_path / "errors.jsonl"),
        patch.object(sh, "DMS_FILE", tmp_path / "dms.json"),
        patch.object(sh, "RSS_HISTORY_FILE", tmp_path / "rss.jsonl"),
    ):
        yield


@pytest.fixture(autouse=True)
def _freeze_gate_time():
    """Freeze pre-trade gate clock to a known safe hour.

    Without this, tests that exercise the PreTradeGate fail when pytest
    runs during the rollover dead zone (21:00-23:00 UTC) or other
    time-sensitive windows.  Tests that explicitly mock time.time (e.g.
    test_novatrade_anti_ea_detection.py) override this fixture naturally.
    """
    try:
        # Eagerly import the module so the patch target resolves
        import novatrade.risk.pre_trade_gate  # noqa: F401

        with patch("novatrade.risk.pre_trade_gate._now", return_value=_SAFE_TIMESTAMP):
            yield
    except (ImportError, AttributeError):
        yield
