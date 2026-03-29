"""Tests for decision dedup in DecisionEngine (v87 P3, v88 race-condition fix)."""

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
            setattr(self, "_lock_path", tmp_path / "STATE" / ".decision_history.lock"),
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

    def test_concurrent_instances_dedup(self, tmp_path):
        """Regression: multiple DecisionEngine instances must not create duplicates.

        This was the v88 bug — each instance had its own asyncio.Lock, so
        concurrent calls would bypass dedup and create duplicate entries.
        Fixed by using fcntl.flock for cross-instance file locking.
        """
        from novatrade.autonomy.decision_engine import DecisionEngine

        (tmp_path / "STATE").mkdir(parents=True)

        async def _run_concurrent():
            # Create 5 separate engine instances (simulating multiple heartbeat cycles)
            engines = [DecisionEngine(base_path=str(tmp_path)) for _ in range(5)]
            decisions = [_make_decision("monitor", "All good") for _ in range(5)]
            # Run all 5 persist calls concurrently
            await asyncio.gather(*(e._persist_decision(d) for e, d in zip(engines, decisions, strict=True)))

        asyncio.run(_run_concurrent())

        history = json.loads((tmp_path / "STATE" / "decision_history.json").read_text())
        # All 5 should be deduped into 1 entry with dedup_count=5
        assert len(history) == 1, f"Expected 1 entry, got {len(history)} — dedup race condition!"
        assert history[0]["dedup_count"] == 5

    def test_concurrent_different_decisions_preserved(self, tmp_path):
        """Concurrent different decisions should all be preserved."""
        from novatrade.autonomy.decision_engine import DecisionEngine

        (tmp_path / "STATE").mkdir(parents=True)

        async def _run():
            engines = [DecisionEngine(base_path=str(tmp_path)) for _ in range(3)]
            decisions = [
                _make_decision("monitor", "All good"),
                _make_decision("repair", "Fix health"),
                _make_decision("validate", "Check results"),
            ]
            # Run sequentially to ensure deterministic ordering
            for e, d in zip(engines, decisions, strict=True):
                await e._persist_decision(d)

        asyncio.run(_run())

        history = json.loads((tmp_path / "STATE" / "decision_history.json").read_text())
        assert len(history) == 3
