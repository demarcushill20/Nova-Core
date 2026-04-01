"""Test that _persist_strategy_config writes to the correct STATE directory.

Regression test for path bug where data_dir=OUTPUT/novatrade caused writes
to OUTPUT/STATE/novatrade/ instead of STATE/novatrade/.
"""

import json
from pathlib import Path

from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.models import AccountMode
from novatrade.runtime.runner import _persist_strategy_config


def _cfg(tmp_path: Path) -> NovaTradeCfg:
    return NovaTradeCfg(
        mode=AccountMode.DEMO,
        symbols=["EURUSD"],
        risk=RiskConfig(),
        data_dir=tmp_path / "OUTPUT" / "novatrade",
    )


def test_persist_writes_to_state_not_output_state(tmp_path):
    """strategy_config.json should land in STATE/novatrade/, not OUTPUT/STATE/novatrade/."""
    cfg = _cfg(tmp_path)

    _persist_strategy_config(cfg, "EURUSD", "H1", "H4", "webhook")

    correct_path = tmp_path / "STATE" / "novatrade" / "strategy_config.json"
    wrong_path = tmp_path / "OUTPUT" / "STATE" / "novatrade" / "strategy_config.json"

    assert correct_path.exists(), f"Expected file at {correct_path}"
    assert not wrong_path.exists(), f"File incorrectly written to {wrong_path}"

    data = json.loads(correct_path.read_text())
    assert data["strategy"] == "IRB"
    assert data["symbol"] == "EURUSD"
    assert data["pipeline"] == "webhook"
    assert "started_at" in data


def test_persist_preserves_all_fields(tmp_path):
    """All config fields should be present in the output."""
    cfg = _cfg(tmp_path)
    _persist_strategy_config(cfg, "GBPUSD", "M15", "H1", "live")

    path = tmp_path / "STATE" / "novatrade" / "strategy_config.json"
    data = json.loads(path.read_text())

    assert data["symbol"] == "GBPUSD"
    assert data["primary_timeframe"] == "M15"
    assert data["higher_timeframe"] == "H1"
    assert data["pipeline"] == "live"
    assert data["symbols"] == ["EURUSD"]
