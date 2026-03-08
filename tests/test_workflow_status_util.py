"""Tests for utils.workflow_status.read_workflow_status."""

import json
from pathlib import Path

import pytest

from utils.workflow_status import read_workflow_status


@pytest.fixture
def wdir(tmp_path):
    """Provide an isolated workflows directory."""
    d = tmp_path / "workflows"
    d.mkdir()
    return d


def _write(wdir: Path, wid: str, content: str):
    (wdir / f"{wid}.json").write_text(content, encoding="utf-8")


# --- Happy path ---

def test_returns_status_completed(wdir):
    _write(wdir, "w1", json.dumps({"workflow_id": "w1", "status": "completed"}))
    assert read_workflow_status("w1", workflows_dir=wdir) == "completed"


def test_returns_status_running(wdir):
    _write(wdir, "w2", json.dumps({"status": "running", "extra": 42}))
    assert read_workflow_status("w2", workflows_dir=wdir) == "running"


def test_returns_status_failed(wdir):
    _write(wdir, "w3", json.dumps({"status": "failed"}))
    assert read_workflow_status("w3", workflows_dir=wdir) == "failed"


# --- Missing / unreadable ---

def test_missing_file_returns_none(wdir):
    assert read_workflow_status("nonexistent", workflows_dir=wdir) is None


def test_empty_file_returns_none(wdir):
    _write(wdir, "empty", "")
    assert read_workflow_status("empty", workflows_dir=wdir) is None


# --- Malformed ---

def test_invalid_json_returns_none(wdir):
    _write(wdir, "bad", "{not json!!")
    assert read_workflow_status("bad", workflows_dir=wdir) is None


def test_json_array_returns_none(wdir):
    _write(wdir, "arr", json.dumps([{"status": "completed"}]))
    assert read_workflow_status("arr", workflows_dir=wdir) is None


def test_missing_status_key_returns_none(wdir):
    _write(wdir, "nokey", json.dumps({"workflow_id": "nokey"}))
    assert read_workflow_status("nokey", workflows_dir=wdir) is None


def test_non_string_status_returns_none(wdir):
    _write(wdir, "intstat", json.dumps({"status": 42}))
    assert read_workflow_status("intstat", workflows_dir=wdir) is None


# --- Default directory (smoke) ---

def test_default_dir_resolves():
    """read_workflow_status with no override should not raise even if file missing."""
    assert read_workflow_status("definitely_not_a_real_workflow") is None
