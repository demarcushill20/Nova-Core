"""Pipeline checkpoint and resume — save/load campaign state for crash recovery.

Provides an opt-in checkpoint system that periodically snapshots campaign
progress so work can be resumed after a crash or deliberate stop.

Usage::

    from novatrade.pipeline.checkpoint import (
        CampaignCheckpoint,
        save_checkpoint,
        load_checkpoint,
    )

    # Save
    cp = CampaignCheckpoint(
        campaign_id="campaign-abc123",
        current_experiment_index=42,
        best_score=1.85,
        best_experiment_id="exp-xyz",
    )
    save_checkpoint(cp, Path("/tmp/checkpoints"))

    # Load
    cp = load_checkpoint("campaign-abc123", Path("/tmp/checkpoints"))
    if cp:
        print(f"Resuming from experiment {cp.current_experiment_index}")
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("novatrade.pipeline.checkpoint")


# ---------------------------------------------------------------------------
# Checkpoint dataclass
# ---------------------------------------------------------------------------


@dataclass
class CampaignCheckpoint:
    """Serialisable snapshot of pipeline campaign state.

    All fields needed to resume a campaign from this point.
    """

    campaign_id: str
    current_experiment_index: int = 0
    best_score: float = 0.0
    best_experiment_id: str = ""
    completed_experiment_ids: set[str] = field(default_factory=set)
    status_counts: dict[str, int] = field(default_factory=dict)
    pipeline_config: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat(),
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "campaign_id": self.campaign_id,
            "current_experiment_index": self.current_experiment_index,
            "best_score": self.best_score,
            "best_experiment_id": self.best_experiment_id,
            "completed_experiment_ids": sorted(self.completed_experiment_ids),
            "status_counts": self.status_counts,
            "pipeline_config": self.pipeline_config,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CampaignCheckpoint:
        """Deserialize from a dict (e.g. loaded from JSON)."""
        completed = data.get("completed_experiment_ids", [])
        return cls(
            campaign_id=data.get("campaign_id", ""),
            current_experiment_index=data.get("current_experiment_index", 0),
            best_score=data.get("best_score", 0.0),
            best_experiment_id=data.get("best_experiment_id", ""),
            completed_experiment_ids=set(completed),
            status_counts=data.get("status_counts", {}),
            pipeline_config=data.get("pipeline_config", {}),
            timestamp=data.get("timestamp", ""),
        )


# ---------------------------------------------------------------------------
# Save / Load
# ---------------------------------------------------------------------------


def _checkpoint_path(campaign_id: str, checkpoint_dir: Path) -> Path:
    """Deterministic path for a campaign's checkpoint file."""
    safe_id = campaign_id.replace("/", "_").replace("\\", "_")
    return Path(checkpoint_dir) / f"checkpoint_{safe_id}.json"


def save_checkpoint(
    checkpoint: CampaignCheckpoint,
    checkpoint_dir: Path,
) -> Path:
    """Atomically write a checkpoint JSON to *checkpoint_dir*.

    Uses write-to-temp + rename for crash safety (no partial writes).

    Returns:
        Path to the written checkpoint file.
    """
    checkpoint_dir = Path(checkpoint_dir)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    out_path = _checkpoint_path(checkpoint.campaign_id, checkpoint_dir)

    data = checkpoint.to_dict()
    encoded = json.dumps(data, indent=2, default=str)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(checkpoint_dir),
        suffix=".tmp",
        prefix="cp_",
    )
    closed = False
    try:
        os.write(fd, encoded.encode("utf-8"))
        os.close(fd)
        closed = True
        os.replace(tmp_path, str(out_path))
    except BaseException:
        if not closed:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    log.debug(
        "Checkpoint saved: campaign=%s experiment=%d score=%.4f",
        checkpoint.campaign_id,
        checkpoint.current_experiment_index,
        checkpoint.best_score,
    )
    return out_path


def load_checkpoint(
    campaign_id: str,
    checkpoint_dir: Path,
) -> CampaignCheckpoint | None:
    """Load a checkpoint for *campaign_id* from *checkpoint_dir*.

    Returns None if no checkpoint exists.
    """
    checkpoint_dir = Path(checkpoint_dir)
    path = _checkpoint_path(campaign_id, checkpoint_dir)

    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text("utf-8"))
        cp = CampaignCheckpoint.from_dict(data)
        log.info(
            "Checkpoint loaded: campaign=%s experiment=%d score=%.4f",
            cp.campaign_id,
            cp.current_experiment_index,
            cp.best_score,
        )
        return cp
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        log.warning("Corrupt checkpoint for %s: %s", campaign_id, exc)
        return None


def delete_checkpoint(
    campaign_id: str,
    checkpoint_dir: Path,
) -> bool:
    """Remove a checkpoint file. Returns True if deleted, False if not found."""
    path = _checkpoint_path(campaign_id, Path(checkpoint_dir))
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Resume helpers
# ---------------------------------------------------------------------------


def should_skip_experiment(
    experiment_id: str,
    checkpoint: CampaignCheckpoint | None,
) -> bool:
    """Return True if *experiment_id* was already completed in a prior run.

    Used for dedup during resume.
    """
    if checkpoint is None:
        return False
    return experiment_id in checkpoint.completed_experiment_ids


def merge_status_counts(
    current: dict[str, int],
    checkpoint: CampaignCheckpoint | None,
) -> dict[str, int]:
    """Merge current run status counts with checkpoint status counts.

    Used during resume to get an accurate total.
    """
    if checkpoint is None:
        return dict(current)
    merged = dict(checkpoint.status_counts)
    for status, count in current.items():
        merged[status] = merged.get(status, 0) + count
    return merged
