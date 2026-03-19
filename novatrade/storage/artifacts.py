"""Artifact management -- trade logs, config snapshots, experiment logs.

Each experiment produces artifacts stored under a campaign directory tree:
    campaign_dir/
        configs/{experiment_id}.json
        trades/{experiment_id}.json
        logs/{experiment_id}.log
"""

from __future__ import annotations

import json
from pathlib import Path


def save_config_snapshot(
    config_dict: dict,
    campaign_dir: Path,
    experiment_id: str,
) -> Path:
    """Save strategy config as a JSON artifact.

    Returns the path to the written file.
    """
    campaign_dir = Path(campaign_dir)
    config_dir = campaign_dir / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)

    out_path = config_dir / f"{experiment_id}.json"
    out_path.write_text(json.dumps(config_dict, indent=2, sort_keys=True))
    return out_path


def save_trade_log(
    trades: list[dict],
    campaign_dir: Path,
    experiment_id: str,
) -> Path:
    """Save trade list as a JSON artifact.

    Returns the path to the written file.
    """
    campaign_dir = Path(campaign_dir)
    trade_dir = campaign_dir / "trades"
    trade_dir.mkdir(parents=True, exist_ok=True)

    out_path = trade_dir / f"{experiment_id}.json"
    out_path.write_text(json.dumps(trades, indent=2))
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
    campaign_dir = Path(campaign_dir)
    log_dir = campaign_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    out_path = log_dir / f"{experiment_id}.log"
    content = f"=== STDOUT ===\n{stdout}\n\n=== STDERR ===\n{stderr}\n"
    out_path.write_text(content)
    return out_path


def load_config_snapshot(
    campaign_dir: Path,
    experiment_id: str,
) -> dict | None:
    """Load a previously saved config snapshot.

    Returns the config dict, or None if the file does not exist.
    """
    campaign_dir = Path(campaign_dir)
    config_path = campaign_dir / "configs" / f"{experiment_id}.json"
    if not config_path.exists():
        return None
    return json.loads(config_path.read_text())
