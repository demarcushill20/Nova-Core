"""Artifact management -- trade logs, config snapshots, experiment logs.

Each experiment produces artifacts stored under a campaign directory tree:
    campaign_dir/
        configs/{experiment_id}.json
        trades/{experiment_id}.json
        logs/{experiment_id}.log
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path


def _validate_id(value: str, name: str = "id") -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError(f"Invalid {name}: must be 1-128 alphanumeric/dash/underscore chars, got {value!r}")


def _atomic_write(out_path: Path, data: str) -> None:
    """Write *data* to *out_path* atomically via tmp-file + rename."""
    parent = str(out_path.parent)
    fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        os.write(fd, data.encode())
        os.close(fd)
        os.replace(tmp_path, str(out_path))
    except BaseException:
        os.close(fd)
        os.unlink(tmp_path)
        raise


def save_config_snapshot(
    config_dict: dict,
    campaign_dir: Path,
    experiment_id: str,
) -> Path:
    """Save strategy config as a JSON artifact.

    Returns the path to the written file.
    """
    _validate_id(experiment_id, "experiment_id")
    campaign_dir = Path(campaign_dir)
    config_dir = campaign_dir / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)

    out_path = config_dir / f"{experiment_id}.json"
    _atomic_write(out_path, json.dumps(config_dict, indent=2, sort_keys=True))
    return out_path


def save_trade_log(
    trades: list[dict],
    campaign_dir: Path,
    experiment_id: str,
) -> Path:
    """Save trade list as a JSON artifact.

    Returns the path to the written file.
    """
    _validate_id(experiment_id, "experiment_id")
    campaign_dir = Path(campaign_dir)
    trade_dir = campaign_dir / "trades"
    trade_dir.mkdir(parents=True, exist_ok=True)

    out_path = trade_dir / f"{experiment_id}.json"
    _atomic_write(out_path, json.dumps(trades, indent=2))
    return out_path


def save_experiment_log(
    stdout: str,
    stderr: str,
    campaign_dir: Path,
    experiment_id: str,
) -> Path:
    """Save experiment stdout/stderr to a log file.

    Returns the path to the written file.
    """
    _validate_id(experiment_id, "experiment_id")
    campaign_dir = Path(campaign_dir)
    log_dir = campaign_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    out_path = log_dir / f"{experiment_id}.log"
    content = f"=== STDOUT ===\n{stdout}\n\n=== STDERR ===\n{stderr}\n"
    _atomic_write(out_path, content)
    return out_path


def load_config_snapshot(
    campaign_dir: Path,
    experiment_id: str,
) -> dict | None:
    """Load a previously saved config snapshot.

    Returns the config dict, or None if the file does not exist.
    """
    _validate_id(experiment_id, "experiment_id")
    campaign_dir = Path(campaign_dir)
    config_path = campaign_dir / "configs" / f"{experiment_id}.json"
    if not config_path.exists():
        return None
    return json.loads(config_path.read_text())
