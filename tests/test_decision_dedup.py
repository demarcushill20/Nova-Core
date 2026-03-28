"""Tests for decision dedup in DecisionEngine (v87 P3)."""

import asyncio
import json

import pytest


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Redirect decision history to tmp."""
    monkeypatch.setattr(
        "novatrade.autonomy.decision_engine.DecisionEngine.__init__",
        lambda self, config=None, base_path=str(tmp_path): (
            setattr(self, "config", config or _default_config()),
            setattr(self, "base_path", tmp_path),
            setattr(self, "_history_path", tmp_path / "STATE" / "decision_history.json"),
            setattr(self, "_write_lock", asyncio.Lock()),
        )[-1],
    )


def _default_config():
    from novatrade.autonomy.decision_engine import DecisionConfig

    return DecisionConfig()


def _make_decision(mode="monitor", reason="All good", target=None):
    from novatrade.autonomy.decision_engine import ActionMode, Decision

    return Decision(
        mode=ActionMode(mode),
        reason=reason,
        target_dimension=target,
    )


class TestDecisionDedup:
    def test_identical_decisions_deduped(self, tmp_path):
        from novatrade.autonomy.decision_engine import DecisionEngine

        engine = DecisionEngine(base_path=str(tmp_path))
        (tmp_path / "STATE").mkdir(parents=True)

        d1 = _make_decision("monitor", "All good")
        d2 = _make_decision("monitor", "All good")
        d3 = _make_decision("monitor", "All good")

        asyncio.run(engine._persist_decision(d1))
        asyncio.run(engine._persist_decision(d2))
        asyncio.run(engine._persist_decision(d3))

        history = json.loads((tmp_path / "STATE" / "decision_history.json").read_text())
        # Should be 1 entry with dedup_count=3
        assert len(history) == 1
        assert history[0]["dedup_count"] == 3

    def test_different_decisions_not_deduped(self, tmp_path):
        from novatrade.autonomy.decision_engine import DecisionEngine

        engine = DecisionEngine(base_path=str(tmp_path))
        (tmp_path / "STATE").mkdir(parents=True)

        d1 = _make_decision("monitor", "All good")
        d2 = _make_decision("repair", "Fix system health")
        d3 = _make_decision("monitor", "All good")

        asyncio.run(engine._persist_decision(d1))
        asyncio.run(engine._persist_decision(d2))
        asyncio.run(engine._persist_decision(d3))

        history = json.loads((tmp_path / "STATE" / "decision_history.json").read_text())
        assert len(history) == 3

    def test_same_mode_different_reason_not_deduped(self, tmp_path):
        from novatrade.autonomy.decision_engine import DecisionEngine

        engine = DecisionEngine(base_path=str(tmp_path))
        (tmp_path / "STATE").mkdir(parents=True)

        d1 = _make_decision("monitor", "System stable")
        d2 = _make_decision("monitor", "All dimensions green")

        asyncio.run(engine._persist_decision(d1))
        asyncio.run(engine._persist_decision(d2))

        history = json.loads((tmp_path / "STATE" / "decision_history.json").read_text())
        assert len(history) == 2
