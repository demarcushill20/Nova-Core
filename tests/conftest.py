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
def _freeze_gate_time():
    """Freeze pre-trade gate clock to a known safe hour.

    Without this, tests that exercise the PreTradeGate fail when pytest
    runs during the rollover dead zone (21:00-23:00 UTC) or other
    time-sensitive windows.  Tests that explicitly mock time.time (e.g.
    test_novatrade_anti_ea_detection.py) override this fixture naturally.
    """
    with patch("novatrade.risk.pre_trade_gate._now", return_value=_SAFE_TIMESTAMP):
        yield
