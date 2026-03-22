"""Shared test fixtures — isolate FTMO state from persisted files."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_ftmo_state(tmp_path: Path):
    """Redirect FTMO state persistence to a temp directory per-test.

    Prevents test runs from reading/writing the production STATE/novatrade/
    directory, which would cause cross-test contamination and interfere
    with the running service.
    """
    with patch("novatrade.risk.ftmo_compliance._STATE_DIR", tmp_path):
        yield
