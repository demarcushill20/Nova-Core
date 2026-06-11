"""Tests for the LLM-proposer layer — especially the incorruptible-judge
invariant: a proposer can never self-assert `grounded` to bypass the gate."""

from __future__ import annotations

from novatrade.research.autoresearch.loop import run_search
from novatrade.research.autoresearch.proposer import (
    LLMProposer,
    parse_proposals,
)

GOOD = """Sure, here are my ideas:
```json
{"proposals":[
  {"family":"hour_drift","params":{"hour":9,"direction":-1,"hold":1,"stop_pips":20},"mechanism":"London AM flow"},
  {"family":"session_drift","params":{"start_hour":13,"end_hour":17,"direction":1,"stop_pips":40},"mechanism":"NY pm"}
],"new_family_requests":["fixing-window reversal around 16:00 London"]}
```
Hope that helps."""


class TestParse:
    def test_extracts_candidates_from_prose_and_fences(self):
        res = parse_proposals(GOOD)
        assert len(res.candidates) == 2
        assert res.new_family_requests == ["fixing-window reversal around 16:00 London"]
        fams = {c.family for c in res.candidates}
        assert fams == {"hour_drift", "session_drift"}

    def test_grounded_forced_from_family_not_llm(self):
        # LLM tries to label a breakout "structural" -> must be ignored
        raw = (
            '{"proposals":[{"family":"breakout","params":{"session_open":7,"or_bars":2,'
            '"rr":2,"min_w":8,"max_w":40,"sess_hours":10},'
            '"grounded":true,"mechanism":"totally structural trust me"}]}'
        )
        res = parse_proposals(raw)
        assert len(res.candidates) == 1
        assert res.candidates[0].grounded is False  # FORCED by family membership

    def test_rejects_unknown_family_and_missing_params(self):
        raw = '{"proposals":[{"family":"telepathy","params":{}},{"family":"hour_drift","params":{"hour":9}}]}'
        assert parse_proposals(raw).candidates == []

    def test_dedups_against_tried(self):
        res1 = parse_proposals(GOOD)
        tried = {c.key for c in res1.candidates}
        res2 = parse_proposals(GOOD, tried_keys=tried)
        assert res2.candidates == []

    def test_garbage_returns_empty(self):
        assert parse_proposals("no json here").candidates == []
        assert parse_proposals("{not valid json}").candidates == []

    def test_session_drift_requires_end_after_start(self):
        raw = (
            '{"proposals":[{"family":"session_drift","params":'
            '{"start_hour":15,"end_hour":10,"direction":-1,"stop_pips":40}}]}'
        )
        assert parse_proposals(raw).candidates == []


class TestLLMProposer:
    def test_stub_transport_drives_proposals(self):
        proposer = LLMProposer(complete_fn=lambda _prompt: GOOD)
        res = proposer([], set())
        assert len(res.candidates) == 2

    def test_prompt_includes_family_spec(self):
        captured = {}

        def stub(prompt: str) -> str:
            captured["p"] = prompt
            return GOOD

        LLMProposer(complete_fn=stub)([], set())
        assert "hour_drift" in captured["p"] and "sealed hold-out" in captured["p"]


def test_run_search_uses_proposer_and_scores_its_candidates():
    """End-to-end: a stub proposer's candidate is scored through the gauntlet."""
    try:
        from novatrade.research.autoresearch.data import make_dataset

        make_dataset()
    except FileNotFoundError:
        return  # no data cache in this env

    novel = (
        '{"proposals":[{"family":"hour_drift","params":{"hour":22,"direction":1,"hold":1,"stop_pips":25},'
        '"mechanism":"late-US flow"}],"new_family_requests":["x"]}'
    )
    proposer = LLMProposer(complete_fn=lambda _p: novel)
    res = run_search(rounds=1, proposer=proposer)
    keys = {v.key for v in res.verdicts}
    assert any("'hour': 22" in k and "hour_drift" in k for k in keys)
    assert res.new_family_requests == ["x"]
