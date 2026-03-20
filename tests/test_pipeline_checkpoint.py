"""Tests for novatrade.pipeline.checkpoint — save/load, atomic writes, resume."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novatrade.pipeline.checkpoint import (
    CampaignCheckpoint,
    delete_checkpoint,
    load_checkpoint,
    merge_status_counts,
    save_checkpoint,
    should_skip_experiment,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cp_dir(tmp_path: Path) -> Path:
    """Temporary checkpoint directory."""
    d = tmp_path / "checkpoints"
    d.mkdir()
    return d


def _make_checkpoint(**overrides: object) -> CampaignCheckpoint:
    """Helper to build a CampaignCheckpoint with defaults."""
    defaults: dict[str, object] = {
        "campaign_id": "campaign-test123",
        "current_experiment_index": 42,
        "best_score": 1.85,
        "best_experiment_id": "exp-abc",
        "completed_experiment_ids": {"exp-001", "exp-002", "exp-003"},
        "status_counts": {"ranked": 20, "gated": 15, "crashed": 7},
        "pipeline_config": {"mode": "campaign", "experiments": 200},
    }
    defaults.update(overrides)
    return CampaignCheckpoint(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CampaignCheckpoint dataclass
# ---------------------------------------------------------------------------


class TestCampaignCheckpoint:
    """Test the checkpoint dataclass serialisation."""

    def test_to_dict_roundtrip(self) -> None:
        cp = _make_checkpoint()
        d = cp.to_dict()
        assert d["campaign_id"] == "campaign-test123"
        assert d["current_experiment_index"] == 42
        assert d["best_score"] == 1.85
        # completed_experiment_ids should be a sorted list in dict form
        assert isinstance(d["completed_experiment_ids"], list)
        assert sorted(d["completed_experiment_ids"]) == d["completed_experiment_ids"]

    def test_from_dict_roundtrip(self) -> None:
        cp = _make_checkpoint()
        d = cp.to_dict()
        cp2 = CampaignCheckpoint.from_dict(d)
        assert cp2.campaign_id == cp.campaign_id
        assert cp2.current_experiment_index == cp.current_experiment_index
        assert cp2.best_score == cp.best_score
        assert cp2.best_experiment_id == cp.best_experiment_id
        assert cp2.completed_experiment_ids == cp.completed_experiment_ids
        assert cp2.status_counts == cp.status_counts
        assert cp2.pipeline_config == cp.pipeline_config

    def test_default_timestamp(self) -> None:
        cp = CampaignCheckpoint(campaign_id="x")
        assert cp.timestamp  # non-empty
        assert "T" in cp.timestamp  # ISO format

    def test_from_dict_missing_fields(self) -> None:
        """from_dict tolerates missing optional fields."""
        cp = CampaignCheckpoint.from_dict({"campaign_id": "test"})
        assert cp.campaign_id == "test"
        assert cp.current_experiment_index == 0
        assert cp.best_score == 0.0
        assert cp.completed_experiment_ids == set()

    def test_empty_completed_ids(self) -> None:
        cp = _make_checkpoint(completed_experiment_ids=set())
        d = cp.to_dict()
        assert d["completed_experiment_ids"] == []


# ---------------------------------------------------------------------------
# Save / Load
# ---------------------------------------------------------------------------


class TestSaveLoadCheckpoint:
    """Test atomic save and load of checkpoint files."""

    def test_save_creates_file(self, cp_dir: Path) -> None:
        cp = _make_checkpoint()
        path = save_checkpoint(cp, cp_dir)
        assert path.exists()
        assert path.suffix == ".json"

    def test_save_file_is_valid_json(self, cp_dir: Path) -> None:
        cp = _make_checkpoint()
        path = save_checkpoint(cp, cp_dir)
        data = json.loads(path.read_text("utf-8"))
        assert data["campaign_id"] == "campaign-test123"
        assert data["best_score"] == 1.85

    def test_load_returns_correct_checkpoint(self, cp_dir: Path) -> None:
        cp = _make_checkpoint()
        save_checkpoint(cp, cp_dir)
        loaded = load_checkpoint("campaign-test123", cp_dir)
        assert loaded is not None
        assert loaded.campaign_id == "campaign-test123"
        assert loaded.best_score == 1.85
        assert loaded.completed_experiment_ids == {"exp-001", "exp-002", "exp-003"}

    def test_load_nonexistent_returns_none(self, cp_dir: Path) -> None:
        loaded = load_checkpoint("nonexistent", cp_dir)
        assert loaded is None

    def test_load_nonexistent_dir_returns_none(self, tmp_path: Path) -> None:
        loaded = load_checkpoint("test", tmp_path / "no-such-dir")
        assert loaded is None

    def test_save_overwrites_previous(self, cp_dir: Path) -> None:
        cp1 = _make_checkpoint(best_score=1.0)
        save_checkpoint(cp1, cp_dir)
        cp2 = _make_checkpoint(best_score=2.0)
        save_checkpoint(cp2, cp_dir)
        loaded = load_checkpoint("campaign-test123", cp_dir)
        assert loaded is not None
        assert loaded.best_score == 2.0

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        deep_dir = tmp_path / "a" / "b" / "c"
        cp = _make_checkpoint()
        path = save_checkpoint(cp, deep_dir)
        assert path.exists()

    def test_load_corrupt_file_returns_none(self, cp_dir: Path) -> None:
        """Corrupt JSON returns None rather than raising."""
        cp = _make_checkpoint()
        path = save_checkpoint(cp, cp_dir)
        path.write_text("NOT VALID JSON", encoding="utf-8")
        loaded = load_checkpoint("campaign-test123", cp_dir)
        assert loaded is None

    def test_multiple_campaigns_independent(self, cp_dir: Path) -> None:
        cp_a = _make_checkpoint(campaign_id="campaign-alpha", best_score=1.0)
        cp_b = _make_checkpoint(campaign_id="campaign-beta", best_score=2.0)
        save_checkpoint(cp_a, cp_dir)
        save_checkpoint(cp_b, cp_dir)

        loaded_a = load_checkpoint("campaign-alpha", cp_dir)
        loaded_b = load_checkpoint("campaign-beta", cp_dir)
        assert loaded_a is not None
        assert loaded_b is not None
        assert loaded_a.best_score == 1.0
        assert loaded_b.best_score == 2.0

    def test_campaign_id_with_special_chars(self, cp_dir: Path) -> None:
        """Campaign IDs with slashes/backslashes are sanitized."""
        cp = _make_checkpoint(campaign_id="campaign/with\\slashes")
        path = save_checkpoint(cp, cp_dir)
        assert path.exists()
        assert "/" not in path.name or "\\" not in path.name
        loaded = load_checkpoint("campaign/with\\slashes", cp_dir)
        assert loaded is not None


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDeleteCheckpoint:
    """Test checkpoint deletion."""

    def test_delete_existing(self, cp_dir: Path) -> None:
        cp = _make_checkpoint()
        save_checkpoint(cp, cp_dir)
        assert delete_checkpoint("campaign-test123", cp_dir)
        assert load_checkpoint("campaign-test123", cp_dir) is None

    def test_delete_nonexistent(self, cp_dir: Path) -> None:
        assert not delete_checkpoint("nonexistent", cp_dir)


# ---------------------------------------------------------------------------
# Resume helpers
# ---------------------------------------------------------------------------


class TestShouldSkipExperiment:
    """Test dedup logic for resume."""

    def test_skip_completed(self) -> None:
        cp = _make_checkpoint(completed_experiment_ids={"exp-001", "exp-002"})
        assert should_skip_experiment("exp-001", cp)
        assert should_skip_experiment("exp-002", cp)

    def test_do_not_skip_new(self) -> None:
        cp = _make_checkpoint(completed_experiment_ids={"exp-001"})
        assert not should_skip_experiment("exp-999", cp)

    def test_no_checkpoint_never_skip(self) -> None:
        assert not should_skip_experiment("exp-001", None)


class TestMergeStatusCounts:
    """Test status count merging for resume."""

    def test_merge_with_checkpoint(self) -> None:
        cp = _make_checkpoint(status_counts={"ranked": 10, "crashed": 2})
        current = {"ranked": 5, "gated": 3}
        merged = merge_status_counts(current, cp)
        assert merged == {"ranked": 15, "crashed": 2, "gated": 3}

    def test_merge_without_checkpoint(self) -> None:
        current = {"ranked": 5, "gated": 3}
        merged = merge_status_counts(current, None)
        assert merged == {"ranked": 5, "gated": 3}

    def test_merge_empty_current(self) -> None:
        cp = _make_checkpoint(status_counts={"ranked": 10})
        merged = merge_status_counts({}, cp)
        assert merged == {"ranked": 10}

    def test_merge_both_empty(self) -> None:
        cp = _make_checkpoint(status_counts={})
        merged = merge_status_counts({}, cp)
        assert merged == {}


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    """Verify atomic write semantics."""

    def test_no_partial_writes_on_disk(self, cp_dir: Path) -> None:
        """After save, the file is either complete or absent (no .tmp leftover)."""
        cp = _make_checkpoint()
        save_checkpoint(cp, cp_dir)
        tmp_files = list(cp_dir.glob("*.tmp"))
        assert tmp_files == [], f"Leftover tmp files: {tmp_files}"

    def test_concurrent_saves_no_corruption(self, cp_dir: Path) -> None:
        """Sequential saves produce valid JSON each time."""
        for i in range(10):
            cp = _make_checkpoint(
                current_experiment_index=i,
                best_score=float(i),
            )
            save_checkpoint(cp, cp_dir)
        loaded = load_checkpoint("campaign-test123", cp_dir)
        assert loaded is not None
        assert loaded.current_experiment_index == 9
        assert loaded.best_score == 9.0
