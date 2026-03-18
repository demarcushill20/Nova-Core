"""Tests for utils.validate_workflow_record."""

import pytest

from utils.validate_workflow_record import VALID_STATUSES, validate_workflow_record

# ── happy path ──────────────────────────────────────────────────────────


def _good_record(**overrides):
    base = {"task_id": "0099", "workflow_id": "0099_test", "status": "completed", "task_class": "research"}
    base.update(overrides)
    return base


def test_valid_minimal_record():
    ok, errs = validate_workflow_record(_good_record())
    assert ok is True
    assert errs == []


def test_extra_keys_ignored():
    ok, _ = validate_workflow_record(_good_record(stage="B", created_at=1.0))
    assert ok is True


@pytest.mark.parametrize("status", sorted(VALID_STATUSES))
def test_all_known_statuses_accepted(status):
    ok, _ = validate_workflow_record(_good_record(status=status))
    assert ok is True


# ── missing keys ────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["task_id", "workflow_id", "status", "task_class"])
def test_missing_required_key(key):
    rec = _good_record()
    del rec[key]
    ok, errs = validate_workflow_record(rec)
    assert ok is False
    assert any(key in e for e in errs)


def test_all_keys_missing():
    ok, errs = validate_workflow_record({})
    assert ok is False
    assert len(errs) == 4


# ── wrong types ─────────────────────────────────────────────────────────


def test_not_a_dict():
    ok, errs = validate_workflow_record([1, 2, 3])
    assert ok is False
    assert "expected dict" in errs[0]


def test_none_input():
    ok, errs = validate_workflow_record(None)
    assert ok is False


def test_numeric_status():
    ok, errs = validate_workflow_record(_good_record(status=42))
    assert ok is False
    assert any("status" in e for e in errs)


# ── blank / whitespace values ──────────────────────────────────────────


def test_blank_workflow_id():
    ok, errs = validate_workflow_record(_good_record(workflow_id="  "))
    assert ok is False
    assert any("empty" in e for e in errs)


def test_blank_status():
    ok, errs = validate_workflow_record(_good_record(status=""))
    assert ok is False


# ── unknown status value ───────────────────────────────────────────────


def test_unknown_status_value():
    ok, errs = validate_workflow_record(_good_record(status="banana"))
    assert ok is False
    assert any("unknown value" in e for e in errs)
