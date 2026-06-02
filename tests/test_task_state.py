from __future__ import annotations

import json

import pytest

import utils.task_state as task_state
from utils.task_state import (
    TaskState,
    advance_task_state,
    fail_task_state,
    load_task_state,
    save_task_state,
)


@pytest.fixture(autouse=True)
def _tmp_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(task_state, "STATE_DIR", tmp_path / "task_state")
    return tmp_path


def test_create_save_load_roundtrip():
    state = TaskState(task_id="job-1", status="pending", current_step=0, max_steps=3)
    save_task_state(state)
    loaded = load_task_state("job-1")
    assert loaded is not None
    assert loaded.task_id == "job-1"
    assert loaded.status == "pending"
    assert loaded.current_step == 0
    assert loaded.max_steps == 3


def test_load_missing_returns_none():
    assert load_task_state("does-not-exist") is None


def test_mark_started_running_then_advance():
    save_task_state(TaskState(task_id="job-2", status="running", current_step=0, max_steps=3))
    advanced = advance_task_state("job-2")
    assert advanced.current_step == 1
    assert advanced.status == "running"


def test_advance_to_completion():
    save_task_state(TaskState(task_id="job-3", status="running", current_step=2, max_steps=3))
    advanced = advance_task_state("job-3")
    assert advanced.current_step == 3
    assert advanced.status == "completed"


def test_advance_caps_at_max_steps():
    save_task_state(TaskState(task_id="job-cap", status="running", current_step=3, max_steps=3))
    advanced = advance_task_state("job-cap")
    assert advanced.current_step == 3
    assert advanced.status == "completed"


def test_advance_missing_raises():
    with pytest.raises(FileNotFoundError):
        advance_task_state("ghost")


def test_resume_after_partial():
    save_task_state(TaskState(task_id="job-4", status="running", current_step=1, max_steps=4))
    # simulate a fresh process resuming from disk
    resumed = load_task_state("job-4")
    assert resumed is not None
    assert resumed.current_step == 1
    advanced = advance_task_state("job-4")
    assert advanced.current_step == 2
    assert advanced.status == "running"


def test_fail_task_state():
    save_task_state(TaskState(task_id="job-5", status="running", current_step=1, max_steps=3))
    failed = fail_task_state("job-5", "boom: ran out of budget")
    assert failed.status == "failed"
    assert failed.last_error == "boom: ran out of budget"
    reloaded = load_task_state("job-5")
    assert reloaded is not None
    assert reloaded.status == "failed"
    assert reloaded.last_error == "boom: ran out of budget"


def test_json_serialize_deserialize_fidelity():
    state = TaskState(
        task_id="job-6",
        status="blocked",
        current_step=2,
        max_steps=5,
        last_error="waiting on dep",
        updated_at="2026-06-02T00:00:00+00:00",
    )
    save_task_state(state)
    path = task_state.STATE_DIR / "job-6.json"
    raw = json.loads(path.read_text())
    assert raw["task_id"] == "job-6"
    assert raw["status"] == "blocked"
    assert raw["current_step"] == 2
    assert raw["max_steps"] == 5
    assert raw["last_error"] == "waiting on dep"
    loaded = load_task_state("job-6")
    assert loaded is not None
    # save() refreshes updated_at; align it before comparing the rest of the fields.
    state.updated_at = loaded.updated_at
    assert loaded == state


def test_updated_at_set_on_save():
    state = TaskState(task_id="job-7", status="pending", current_step=0, max_steps=1)
    save_task_state(state)
    loaded = load_task_state("job-7")
    assert loaded is not None
    assert loaded.updated_at != ""


def test_corrupt_json_loads_as_none():
    # Simulate a truncated/corrupt state file (e.g., crash mid-write).
    task_state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = task_state.STATE_DIR / "job-corrupt.json"
    path.write_text('{"task_id": "job-corrupt", "status": "run')  # truncated
    assert load_task_state("job-corrupt") is None


def test_save_leaves_no_tmp_file():
    save_task_state(TaskState(task_id="job-tmp", status="pending", current_step=0, max_steps=1))
    leftovers = list(task_state.STATE_DIR.glob("*.tmp"))
    assert leftovers == []


def test_load_ignores_unknown_keys():
    # Forward compatibility: extra keys from a newer writer must not crash load.
    task_state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = task_state.STATE_DIR / "job-future.json"
    path.write_text(
        json.dumps(
            {
                "task_id": "job-future",
                "status": "running",
                "current_step": 1,
                "max_steps": 4,
                "future_field": "ignored",
                "another_unknown": 42,
            }
        )
    )
    loaded = load_task_state("job-future")
    assert loaded is not None
    assert loaded.task_id == "job-future"
    assert loaded.current_step == 1
    assert not hasattr(loaded, "future_field")
